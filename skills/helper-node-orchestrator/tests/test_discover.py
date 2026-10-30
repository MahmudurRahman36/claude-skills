"""Unit tests for scripts/orch_discover.py. No network, ssh or docker: every process goes through a scripted fake run=;
the probe-script tests run the real sh / PowerShell script locally against stub docker, curl and uname commands.
Run from the skill root:  python -B -m unittest discover -s tests -p "test_discover.py"
"""
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
import orch_discover as d  # noqa: E402

LINUX = (FIX / "discover-linux.txt").read_text(encoding="utf-8")
WINDOWS = (FIX / "discover-windows.txt").read_bytes().decode("utf-8")  # CRLF kept, as ssh delivers it
BASE = " ".join(d.SSH_BASE)
NO_NS = {"ORCH_LABEL": "", "ORCH_PREFIX": ""}  # other test modules may set the test namespace in os.environ
TWO_EPS = [{"name": "app1-web :8080", "url": "http://127.0.0.1:8080/", "insecure": False},
           {"name": "shop-gw :8443", "url": "https://127.0.0.1:8443/", "insecure": True}]


def P(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def G(host, user="deploy", port=22, err=""):
    """Canned `ssh -v -G` answer."""
    return P(0, f"user {user}\nhostname {host}\nport {port}\nbatchmode no\nconnecttimeout none\n", err)


class FakeRun:
    """Stand-in for subprocess.run: rules (predicate(argv), result | exception | callable(argv, kw)), first match
    wins; every call is recorded. An unmatched command fails the test."""

    def __init__(self, *rules):
        self.rules, self.calls, self.lock = list(rules), [], threading.Lock()

    def __call__(self, argv, **kw):
        with self.lock:
            self.calls.append((list(argv), kw))
        for pred, res in self.rules:
            if pred(argv):
                if isinstance(res, BaseException):
                    raise res
                return res(argv, kw) if callable(res) else res
        raise AssertionError(f"unexpected command: {argv}")

    @property
    def argvs(self):
        return [c[0] for c in self.calls]


def is_resolve(a):
    return a[:3] == ["ssh", "-v", "-G"]


def is_sh(a):
    return a[0] == "ssh" and a[-1] == "sh -s"


def is_ps(a):
    return a[0] == "ssh" and a[-1].startswith(d.PS_PREFIX)


def is_true(a):
    return a[0] == "ssh" and a[-1] == "true"


def docker_info(**kw):
    i = {"ID": "0a1b2c3d-0000-4000-8000-000000000001", "ServerVersion": "27.3.1", "DockerRootDir": "/var/lib/docker",
         "CgroupVersion": "2", "SecurityOptions": ["name=seccomp,profile=builtin"]}
    i.update(kw)
    return json.dumps(i, separators=(",", ":"))  # compact, as docker's {{json .}} prints it


def sh_out(*lines, info=None, socket="default", user="deploy"):
    head = ["orch_probe=1", "uname=Linux", "arch=x86_64", f"user={user}", "cpus=4", "mem_kb=8000000", "psi=1",
            "docker_cli=1"]
    tail = [f"docker_socket={socket}"] + ([f"docker_info={info or docker_info()}"] if socket != "none" else [])
    return "\n".join(head + list(lines) + tail) + "\n"


# ----------------------------------------------------------------------------- spec -> argv
class Spec(unittest.TestCase):
    def test_ssh_base_is_the_contract(self):
        # host keys stay strict whatever ~/.ssh/config says (a command-line -o wins over the config file)
        self.assertEqual(d.SSH_BASE, ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                                      "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR",
                                      "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"])
        self.assertIn("-o StrictHostKeyChecking=yes", d.ssh_cmdline("deploy@192.0.2.20:2222"))

    def test_ssh_args_forms(self):
        cases = {"app1": ["app1"], "h11": ["h11"], "deploy@192.0.2.11": ["deploy@192.0.2.11"],
                 "deploy@192.0.2.20:2222": ["-p", "2222", "deploy@192.0.2.20"],
                 "192.0.2.30:2200": ["-p", "2200", "192.0.2.30"], "shop.example": ["shop.example"],
                 "deploy@[2001:db8::1]:2222": ["-p", "2222", "deploy@2001:db8::1"],
                 "[2001:db8::5]": ["2001:db8::5"], "ci_user@app1": ["ci_user@app1"]}
        for spec, want in cases.items():
            self.assertEqual(d.ssh_args(spec), want, spec)

    def test_bad_specs_are_refused_before_ssh_sees_them(self):
        for spec in ("", "-oProxyCommand=x", "app1 -v", "a;b", "a|b", "$(id)", "`id`", "u@", "@h", "h:0",
                     "h:65536", "u@2001:db8::1", "[zz::1]", "[1234]", "h:22:33", "u@h@x", "app1\n", "-p", None, 22):
            with self.assertRaises(ValueError, msg=repr(spec)) as cm:
                d.ssh_args(spec)
            self.assertIn("user@host:port", str(cm.exception))

    def test_cmdline_is_the_exact_command(self):
        self.assertEqual(d.ssh_cmdline("deploy@192.0.2.20:2222"), BASE + " -p 2222 deploy@192.0.2.20 true")
        self.assertEqual(d.ssh_cmdline("app1", "sh -s"), BASE + " app1 'sh -s'")
        self.assertEqual(d.ssh_cmdline("app1", None), BASE + " app1")

    def test_docker_url(self):
        self.assertEqual(d.docker_url("app1"), "ssh://app1")
        self.assertEqual(d.docker_url("deploy@192.0.2.11"), "ssh://deploy@192.0.2.11")
        self.assertEqual(d.docker_url("deploy@192.0.2.20:2222"), "ssh://deploy@192.0.2.20:2222")
        self.assertEqual(d.docker_url("deploy@[2001:db8::1]:2222"), "ssh://deploy@[2001:db8::1]:2222")
        self.assertEqual(d.docker_url("[2001:db8::5]"), "ssh://[2001:db8::5]")


class Resolve(unittest.TestCase):
    def test_alias_by_hostname(self):
        run = FakeRun((is_resolve, G("192.0.2.11", "deploy", 2222)))
        self.assertEqual(d.ssh_resolve("app1", run=run),
                         {"hostname": "192.0.2.11", "user": "deploy", "port": 2222, "alias": True})
        argv, kw = run.calls[0]
        self.assertEqual(argv, ["ssh", "-v", "-G", "app1"])
        self.assertIs(kw["stdin"], subprocess.DEVNULL)  # ssh never reads orch's own stdin

    def test_alias_by_matched_host_block(self):
        err = ("debug1: Reading configuration data /home/u/.ssh/config\n"
               "debug1: /home/u/.ssh/config line 5: Applying options for shop\n"
               "debug1: /home/u/.ssh/config line 9: Applying options for *\n")
        self.assertTrue(d.ssh_resolve("shop", run=FakeRun((is_resolve, G("shop", err=err))))["alias"])

    def test_wildcard_block_or_same_name_is_not_an_alias(self):
        err = "debug1: /etc/ssh/ssh_config line 19: Applying options for *\n"
        self.assertFalse(d.ssh_resolve("shop", run=FakeRun((is_resolve, G("shop", err=err))))["alias"])
        self.assertFalse(d.ssh_resolve("APP1", run=FakeRun((is_resolve, G("app1"))))["alias"])
        self.assertFalse(d.ssh_resolve("192.0.2.12", run=FakeRun((is_resolve, G("192.0.2.12"))))["alias"])

    def test_user_host_port_goes_through_p(self):
        run = FakeRun((is_resolve, G("192.0.2.20", "deploy", 2222, "debug1: Applying options for 192.0.2.20\n")))
        self.assertEqual(d.ssh_resolve("deploy@192.0.2.20:2222", run=run),
                         {"hostname": "192.0.2.20", "user": "deploy", "port": 2222, "alias": False})
        self.assertEqual(run.argvs[0], ["ssh", "-v", "-G", "-p", "2222", "deploy@192.0.2.20"])

    def test_bad_config_names_the_spec_and_the_fix(self):
        err = ("debug1: Reading configuration data /home/u/.ssh/config\n"
               "/home/u/.ssh/config: line 3: Bad configuration option: hostnme\n"
               "/home/u/.ssh/config: terminating, 1 bad configuration options\n")
        with self.assertRaises(d.DiscoverError) as cm:
            d.ssh_resolve("app1", run=FakeRun((is_resolve, P(255, "", err))))
        m = str(cm.exception)
        self.assertIn("for app1: /home/u/.ssh/config: terminating, 1 bad configuration options", m)
        self.assertIn("fix ~/.ssh/config, then retry: ssh -v -G app1", m)

    def test_ssh_missing(self):
        with self.assertRaises(d.DiscoverError) as cm:
            d.ssh_resolve("app1", run=FakeRun((is_resolve, FileNotFoundError(2, "no ssh"))))
        self.assertEqual(cm.exception.kind, "missing")
        self.assertIn("ssh is not installed", str(cm.exception))

    def test_config_that_accepts_any_host_key_is_a_hostkey_problem(self):
        def g(extra):
            return P(0, f"user deploy\nhostname 192.0.2.20\nport 2222\n{extra}")
        for extra, want in (("stricthostkeychecking accept-new\n", "StrictHostKeyChecking accept-new"),
                            ("stricthostkeychecking false\n", "StrictHostKeyChecking false"),
                            ("stricthostkeychecking ask\nuserknownhostsfile /dev/null\n",
                             "UserKnownHostsFile /dev/null")):
            with self.subTest(extra=extra):
                r = d.ssh_resolve("deploy@192.0.2.20:2222", run=FakeRun((is_resolve, g(extra))))
                p = r["hostkey"]
                self.assertEqual(p["class"], "hostkey")
                self.assertIn(want, p["what"])
                self.assertIn("near the top of ~/.ssh/config (above any Host * block): Host 192.0.2.20 / "
                              "StrictHostKeyChecking yes", p["fix"])
                self.assertEqual("UserKnownHostsFile" in want, "UserKnownHostsFile ~/.ssh/known_hosts" in p["fix"])
                self.assertTrue(p["fix"].endswith("check the fingerprint: ssh -p 2222 deploy@192.0.2.20"), p["fix"])
                self.assertEqual(p["tried"], "ssh -v -G -p 2222 deploy@192.0.2.20")
        for extra in ("stricthostkeychecking ask\n", "stricthostkeychecking true\n"
                      "userknownhostsfile ~/.ssh/known_hosts ~/.ssh/known_hosts2\n", ""):
            r = d.ssh_resolve("deploy@192.0.2.20:2222", run=FakeRun((is_resolve, g(extra))))
            self.assertNotIn("hostkey", r, extra)

    def test_probe_refuses_a_helper_whose_config_accepts_any_host_key(self):
        loose = P(0, "user deploy\nhostname 192.0.2.11\nport 22\nstricthostkeychecking accept-new\n")
        run = FakeRun((is_resolve, loose))
        rec = d.probe_host("h11", "app1", run=run)
        self.assertEqual((rec["ok"], rec["problem"]["class"]), (False, "hostkey"))
        self.assertNotIn("hostkey", rec["resolved"])  # the state record keeps only the resolution
        self.assertEqual(len(run.calls), 1)  # no probe over ssh at all
        # a production-only host is only checked with orch's own (strict) ssh: not a problem there
        run = FakeRun((is_resolve, loose), (is_true, P(0)))
        rec = d.probe_host("p11", "app1", docker=False, run=run)
        self.assertEqual((rec["ok"], rec["problem"]), (True, None))
        self.assertIn("StrictHostKeyChecking=yes", run.argvs[1])


# ----------------------------------------------------------------------------- failure classes
class Classify(unittest.TestCase):
    S, SP = "deploy@192.0.2.12", "deploy@192.0.2.20:2222"

    def c(self, err, spec=None, rc=255, **kw):
        return d.classify_ssh_error(rc, err, spec or self.S, **kw)

    def test_auth(self):
        p = self.c("deploy@192.0.2.12: Permission denied (publickey).\r\n")
        self.assertEqual(p, {"class": "auth", "what": "ssh: deploy@192.0.2.12: Permission denied (publickey).",
                             "fix": "ssh-copy-id deploy@192.0.2.12", "tried": BASE + " deploy@192.0.2.12 true"})
        self.assertEqual(self.c("x: Permission denied (publickey,password).", self.SP)["fix"],
                         "ssh-copy-id -p 2222 deploy@192.0.2.20")
        self.assertEqual(self.c("Permission denied (publickey).", "app1")["fix"], "ssh-copy-id app1")
        self.assertEqual(self.c("Received disconnect: Too many authentication failures")["class"], "auth")

    def test_unknown_host_key_is_never_accepted(self):
        p = self.c("No ED25519 host key is known for [192.0.2.20]:2222 and you have requested strict checking.\r\n"
                   "Host key verification failed.\r\n", self.SP)
        self.assertEqual(p["class"], "hostkey")
        self.assertIn("No ED25519 host key is known", p["what"])
        self.assertEqual(p["fix"],
                         "connect once interactively and check the fingerprint: ssh -p 2222 deploy@192.0.2.20")
        self.assertEqual(self.c("Host key verification failed.")["class"], "hostkey")

    def test_changed_host_key(self):
        err = ("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
               "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
               "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
               "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!\n"
               "Offending ECDSA key in /home/u/.ssh/known_hosts:3\n"
               "  remove with:\n"
               "  ssh-keygen -f '/home/u/.ssh/known_hosts' -R '[192.0.2.20]:2222'\n"
               "ECDSA host key for [192.0.2.20]:2222 has changed and you have requested strict checking.\n"
               "Host key verification failed.\n")
        p = self.c(err, self.SP)
        self.assertEqual(p["class"], "hostkey")
        self.assertIn("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!)", p["what"])
        self.assertIn("it can also be an attack", p["fix"])
        self.assertIn("(ssh-keygen -f '/home/u/.ssh/known_hosts' -R '[192.0.2.20]:2222')", p["fix"])
        self.assertTrue(p["fix"].endswith("check the fingerprint: ssh -p 2222 deploy@192.0.2.20"))
        for bad in ("accept-new", "StrictHostKeyChecking=no", "yes |"):
            self.assertNotIn(bad, p["fix"])
        p = self.c("ECDSA host key for [192.0.2.20]:2222 has changed and you have requested strict checking.",
                   self.SP)
        self.assertIn("(ssh-keygen -R '[192.0.2.20]:2222')", p["fix"])

    def test_network(self):
        p = self.c("ssh: connect to host 192.0.2.40 port 22: Connection timed out")
        self.assertEqual(p["class"], "network")
        self.assertEqual(p["fix"], "check that 192.0.2.40 is up and that port 22 is reachable from this machine "
                                   "(VPN, firewall)")  # the command to retry is in tried, once
        self.assertEqual(p["tried"], f"{BASE} deploy@192.0.2.12 true")
        self.assertEqual(self.c("ssh: connect to host 192.0.2.40 port 22: Operation timed out")["class"], "network")
        p = self.c("ssh: connect to host 192.0.2.20 port 2222: Connection refused", self.SP)
        self.assertTrue(p["fix"].startswith("nothing accepts ssh on 192.0.2.20 port 2222: start sshd there"))
        self.assertIn("no network route to 192.0.2.40",
                      self.c("ssh: connect to host 192.0.2.40 port 22: No route to host")["fix"])
        p = self.c("kex_exchange_identification: read: Connection reset by peer", self.SP)
        self.assertEqual(p["class"], "network")
        self.assertTrue(p["fix"].startswith("192.0.2.20 closed the connection before login"))
        for err in ("Connection to 192.0.2.12 closed by remote host.", "client_loop: send disconnect: Broken pipe",
                    "Timeout, server 192.0.2.12 not responding."):
            p = self.c(err)
            self.assertEqual(p["class"], "network", err)
            self.assertTrue(p["fix"].startswith("the connection to 192.0.2.12 dropped after login"), err)

    def test_dns(self):
        p = self.c("ssh: Could not resolve hostname app9: Name or service not known", "app9")
        self.assertEqual((p["class"], p["what"]),
                         ("dns", "ssh: Could not resolve hostname app9: Name or service not known"))
        self.assertIn("the name in app9 does not resolve", p["fix"])
        self.assertEqual(self.c("ssh: Could not resolve hostname app9: No such host is known.", "app9")["class"], "dns")

    def test_docker_missing(self):
        p = self.c("bash: line 1: docker: command not found", rc=127)
        self.assertEqual(p, {"class": "docker-missing",
                             "what": "docker is not installed on deploy@192.0.2.12: bash: line 1: docker: command "
                                     "not found",
                             "fix": "install Docker Engine (Linux) or Docker Desktop (Windows) on deploy@192.0.2.12",
                             "tried": BASE + " deploy@192.0.2.12 'docker info'"})
        p = self.c("'docker' is not recognized as an internal or external command,", rc=1)
        self.assertEqual(p["class"], "docker-missing")

    def test_docker_permission(self):
        err = ("permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock: "
               "Get \"http://%2Fvar%2Frun%2Fdocker.sock/v1.47/info\": dial unix /var/run/docker.sock: connect: "
               "permission denied")
        p = self.c(err, rc=1, user="ci")
        self.assertEqual(p["class"], "docker-permission")
        self.assertEqual(p["fix"], "on deploy@192.0.2.12: sudo usermod -aG docker ci, then log in again")
        self.assertTrue(p["what"].startswith("ci may not use Docker on deploy@192.0.2.12: permission denied"))
        self.assertTrue(self.c(err, "app1", rc=1)["what"].startswith("the ssh user may not use Docker on app1"))
        self.assertEqual(self.c(err, rc=1)["fix"],
                         "on deploy@192.0.2.12: sudo usermod -aG docker deploy, then log in again")
        self.assertIn("sudo usermod -aG docker $USER,", self.c(err, "app1", rc=1)["fix"])
        p = self.c("error during connect: open //./pipe/docker_engine: Access is denied.", "app1", rc=1, user="builder")
        self.assertEqual(p["class"], "docker-permission")
        self.assertIn("net localgroup docker-users builder /add", p["fix"])

    def test_docker_down(self):
        p = self.c("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
                   rc=1)
        self.assertEqual((p["class"], p["fix"]),
                         ("docker-down", "start Docker on deploy@192.0.2.12: sudo systemctl start docker"))
        self.assertEqual(p["tried"], BASE + " deploy@192.0.2.12 'docker info'")
        p = self.c("Cannot connect to the Docker daemon at unix:///run/user/1000/docker.sock. Is the docker daemon "
                   "running?", rc=1)
        self.assertTrue(p["fix"].endswith("systemctl --user start docker"))
        p = self.c("error during connect: Get \"http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.47/info\": open "
                   "//./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.", rc=1)
        self.assertEqual(p["class"], "docker-down")
        self.assertIn("start Docker Desktop on", p["fix"])
        self.assertEqual(self.c("docker info did not answer within 20 s", rc=1)["class"], "docker-down")

    def test_other(self):
        p = self.c("Bad owner or permissions on /home/u/.ssh/config")
        self.assertEqual(p["class"], "other")
        self.assertEqual(p["what"], "ssh: Bad owner or permissions on /home/u/.ssh/config")
        self.assertEqual(p["fix"], f"run it yourself to see the full error: {BASE} deploy@192.0.2.12 true")
        self.assertEqual(self.c("", rc=1)["what"], "exit code 1")
        p = self.c("first\nsecond line\n", rc=3, remote="docker info")
        self.assertEqual((p["what"], p["tried"]), ("second line", BASE + " deploy@192.0.2.12 'docker info'"))

    def test_ssh_classes_need_rc_255(self):
        self.assertEqual(self.c("Permission denied (publickey).", rc=1)["class"], "other")

    def test_output_is_sanitised(self):
        p = self.c("\x1b[31mdeploy@192.0.2.12: Permission denied (publickey).\x1b[0m\x07")
        self.assertEqual(p["what"], "ssh: deploy@192.0.2.12: Permission denied (publickey).")
        self.assertLessEqual(len(self.c("x" * 5000, rc=1)["what"]), 300)

    def test_bad_spec_does_not_crash(self):
        self.assertEqual(self.c("Permission denied (publickey).", "a b;c")["class"], "auth")


# ----------------------------------------------------------------------------- probe output -> facts
@mock.patch.dict(os.environ, NO_NS)
class ParseSh(unittest.TestCase):
    def test_linux_fixture(self):
        f = d.parse_probe_sh(LINUX)
        self.assertEqual(f, {
            "os": "linux", "arch": "x86_64", "cpus": 8, "mem_bytes": 16113548 * 1024,
            "disk_free_bytes": 390625000 * 1024, "docker": "27.3.1",
            "docker_id": "5b1f7c2e-9a41-4d0b-8c3e-2f6a1d9e7b40",
            "rootless": False, "buildx": True, "psi": True, "cgroup": 2, "socket": "default",
            "cotenants": [{"name": "app1-db", "image": "postgres:16", "ports": [5432]},
                          {"name": "app1-web", "image": "app1:1", "ports": [8080]},
                          {"name": "dns", "image": "coredns:1.11", "ports": []},
                          {"name": "metrics", "image": "exporter:1", "ports": [9100, 9101]},
                          {"name": "shop-api", "image": "shop-api:2", "ports": [9000]},
                          {"name": "shop-gw", "image": "nginx:1.27", "ports": [8443]}],
            "endpoints": [{"name": "app1-web :8080", "url": "http://127.0.0.1:8080/", "insecure": False},
                          {"name": "shop-gw :8443", "url": "https://127.0.0.1:8443/", "insecure": True}]})

    def test_contract_fact_keys_present(self):
        want = {"os", "arch", "cpus", "mem_bytes", "disk_free_bytes", "docker", "docker_id", "rootless", "buildx",
                "psi", "cgroup", "cotenants", "endpoints"}
        self.assertLessEqual(want, set(d.parse_probe_sh(LINUX)))
        self.assertLessEqual(want, set(d.parse_probe_sh("")))

    def test_endpoint_filter_keeps_2xx_3xx_only_max_8_sorted(self):
        lines = ["orch_probe=1", "ep=8080 http 404 0.01 app1-web", "ep=8081 http 500 0.01 app1-web",
                 "ep=8082 http 000 0.01 app1-web", "ep=8083 ftp 200 0.01 app1-web", "ep=x http 200 0.01 app1-web",
                 "ep=8084 http 200 0.01 agentx-t-ok-1", "ep=8085 http 2000 0.01 app1-web", "ep=8086 http 204",
                 "ep=² http 200 0.01 app1-web", "ep=0 http 200 0.01 app1-web", "ep=70000 http 200 0.01 x"]
        lines += [f"ep={p} http 301 0.01 web{p}" for p in (9010, 9009, 9008, 9007, 9006, 9005, 9004, 9003, 9002)]
        lines += ["ep=9002 https 200 0.01 web9002"]
        eps = d.parse_probe_sh("\n".join(lines))["endpoints"]
        self.assertEqual([e["url"] for e in eps], [f"http://127.0.0.1:{p}/" for p in range(9002, 9010)])
        self.assertEqual(eps[0], {"name": "web9002 :9002", "url": "http://127.0.0.1:9002/", "insecure": False})

    def test_rootless_and_explicit_socket(self):
        f = d.parse_probe_sh(sh_out(info=docker_info(SecurityOptions=["name=seccomp,profile=builtin", "name=rootless",
                                                                      "name=cgroupns"]),
                                    socket="unix:///run/user/1000/docker.sock"))
        self.assertTrue(f["rootless"])
        self.assertEqual(f["socket"], "unix:///run/user/1000/docker.sock")
        self.assertEqual(f["mem_bytes"], 8000000 * 1024)

    def test_msys_sh_means_windows(self):
        self.assertEqual(d.parse_probe_sh("orch_probe=1\nuname=MINGW64_NT-10.0-19045\n")["os"], "windows")

    def test_missing_and_garbage_fields(self):
        f = d.parse_probe_sh("orch_probe=1\ncpus=\nmem_kb=lots\ndocker_info={not json\ndocker_socket=none\nnoise\n")
        for k in ("os", "arch", "cpus", "mem_bytes", "disk_free_bytes", "docker", "docker_id", "cgroup", "socket"):
            self.assertIsNone(f[k], k)
        self.assertEqual((f["cotenants"], f["endpoints"], f["rootless"], f["buildx"], f["psi"]),
                         ([], [], False, False, False))
        self.assertIsNone(d.parse_probe_sh("docker_info=[1, 2]\n")["docker"])

    def test_cgroup_falls_back_to_the_host_file(self):
        self.assertEqual(d.parse_probe_sh(sh_out("cgroupfs=2", info=docker_info(CgroupVersion="")))["cgroup"], 2)

    def test_orch_containers_are_never_cotenants(self):
        f = d.parse_probe_sh("ctr=agentx-t-ok-1\talpine:3.20\t\nctr=web\tnginx:1\t0.0.0.0:80->80/tcp\n")
        self.assertEqual([c["name"] for c in f["cotenants"]], ["web"])

    def test_host_ports(self):
        cases = {"0.0.0.0:8080->80/tcp, :::8080->80/tcp": [8080], "[::]:8443->443/tcp": [8443], "80/tcp": [],
                 "0.0.0.0:53->53/udp": [], "0.0.0.0:9100-9101->9100-9101/tcp": [9100, 9101],
                 "127.0.0.1:5432->5432/tcp, 0.0.0.0:6000->6000/tcp": [5432, 6000], "": [],
                 "0.0.0.0:30000-32767->30000-32767/tcp": list(range(30000, 30016))}
        for text, want in cases.items():
            self.assertEqual(d._host_ports(text), want, text)

    def test_test_namespace_from_env(self):
        env = {"ORCH_LABEL": "io.orchtest.agent", "ORCH_PREFIX": "agentt-"}
        with mock.patch.dict(os.environ, env):
            f = d.parse_probe_sh("ctr=agentt-x\talpine\t\nctr=agentx-y\talpine\t\nctr=web\tnginx\t\n")
            self.assertEqual([c["name"] for c in f["cotenants"]], ["web"])
            s, ps = d.probe_script_sh(), d.probe_script_ps()
            self.assertIn("-v P='agentx- agentt-'", s)
            self.assertIn('{{.Label "io.orch.agent"}}{{.Label "io.orchtest.agent"}}', s)
            self.assertIn("$g -like '*,io.orchtest.agent=1,*'", ps)
        with mock.patch.dict(os.environ, {"ORCH_LABEL": "x'; rm -rf /", "ORCH_PREFIX": "a b"}):
            s = d.probe_script_sh()
            self.assertNotIn("rm -rf", s)
            self.assertIn("-v P='agentx-'", s)


@mock.patch.dict(os.environ, NO_NS)
class ParsePs(unittest.TestCase):
    def test_windows_fixture(self):
        f = d.parse_probe_ps(WINDOWS)
        self.assertEqual(f, {
            "os": "windows", "arch": "x86_64", "cpus": 16, "mem_bytes": 34252779520, "disk_free_bytes": 512110190592,
            "docker": "27.3.1", "docker_id": "c7d2a9e4-3b6f-4e21-9d0a-6f5e4c3b2a19", "rootless": False,
            "buildx": True, "psi": False, "cgroup": 2, "socket": "default",
            "cotenants": [{"name": "app1-web", "image": "app1:1", "ports": [8080]},
                          {"name": "cache", "image": "redis:7", "ports": [6379]}],
            "endpoints": [{"name": "app1-web :8080", "url": "http://127.0.0.1:8080/", "insecure": False}]})

    def test_crlf_and_lf_parse_the_same(self):
        self.assertEqual(d.parse_probe_ps(WINDOWS), d.parse_probe_ps(WINDOWS.replace("\r\n", "\n")))

    def test_bad_json_line_is_skipped(self):
        f = d.parse_probe_ps('ctrj={"Names": "a"\nctrj=[1]\nctrj={"Names":"web","Image":"x","Ports":""}\n')
        self.assertEqual([c["name"] for c in f["cotenants"]], ["web"])

    def test_ps_command_fits_cmd_exe_and_decodes(self):
        c = d.probe_command_ps()
        self.assertLess(len(c), d.PS_MAX)
        self.assertEqual(base64.b64decode(c[len(d.PS_PREFIX):]).decode("utf-16-le"), d.probe_script_ps())
        for ph in ("__OURS__", "__SKIP__", "__MAX__", "__NSP__"):
            self.assertNotIn(ph, d.probe_script_ps())
        for ph in ("__FMT__", "__PFX__", "__SKIP__", "__MAX__", "__NSP__"):
            self.assertNotIn(ph, d.probe_script_sh())


# ----------------------------------------------------------------------------- probe_host / probe_hosts
@mock.patch.dict(os.environ, NO_NS)
class ProbeHost(unittest.TestCase):
    def test_linux_ok(self):
        run = FakeRun((is_resolve, G("192.0.2.11", "deploy", 2222)), (is_sh, P(0, LINUX)))
        rec = d.probe_host("h11", "app1", run=run)
        self.assertEqual(set(rec), {"name", "ok", "probed", "spec", "resolved", "facts", "problem"})
        self.assertTrue(rec["ok"])
        self.assertIsNone(rec["problem"])
        self.assertEqual((rec["name"], rec["spec"]), ("h11", "app1"))
        self.assertEqual(rec["resolved"], {"hostname": "192.0.2.11", "user": "deploy", "port": 2222, "alias": True})
        self.assertEqual(rec["facts"], d.parse_probe_sh(LINUX))
        self.assertRegex(rec["probed"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual(len(run.calls), 2)  # one ssh round trip after the local resolve
        argv, kw = run.calls[1]
        self.assertEqual(argv, d.SSH_BASE + ["app1", "sh -s"])
        self.assertEqual(kw["input"], d.probe_script_sh().encode())  # bytes: no \r\n translation on Windows
        self.assertNotIn("stdin", kw)
        self.assertEqual(kw["timeout"], 40)

    def test_auth_failure_stops_at_one_round_trip(self):
        run = FakeRun((is_resolve, G("192.0.2.12")),
                      (is_sh, P(255, "", "deploy@192.0.2.12: Permission denied (publickey).\r\n")))
        rec = d.probe_host("h12", "deploy@192.0.2.12", run=run)
        self.assertFalse(rec["ok"])
        self.assertEqual((rec["problem"]["class"], rec["problem"]["fix"]), ("auth", "ssh-copy-id deploy@192.0.2.12"))
        self.assertFalse(any(is_ps(a) for a in run.argvs))

    def test_windows_cmd_shell_gets_the_powershell_probe(self):
        for err in ("'sh' is not recognized as an internal or external command,\r\noperable program or batch file.\r\n",
                    "sh : The term 'sh' is not recognized as the name of a cmdlet, function, script file, or operable "
                    "program.\r\n"):
            run = FakeRun((is_resolve, G("192.0.2.13", "builder")), (is_sh, P(1, "", err)), (is_ps, P(0, WINDOWS)))
            rec = d.probe_host("h13", "builder@192.0.2.13", run=run)
            self.assertTrue(rec["ok"], rec["problem"])
            self.assertEqual(rec["facts"], d.parse_probe_ps(WINDOWS))
            ps = run.argvs[-1]
            self.assertEqual(ps[:-1], d.SSH_BASE + ["builder@192.0.2.13"])
            self.assertEqual(ps[-1], d.probe_command_ps())
            self.assertIs(run.calls[-1][1]["stdin"], subprocess.DEVNULL)

    def test_msys_sh_on_windows_prefers_powershell_facts(self):
        msys = sh_out().replace("uname=Linux", "uname=MINGW64_NT-10.0-19045")
        run = FakeRun((is_resolve, G("192.0.2.13")), (is_sh, P(0, msys)), (is_ps, P(0, WINDOWS)))
        self.assertEqual(d.probe_host("h13", "builder@192.0.2.13", run=run)["facts"]["cpus"], 16)
        run = FakeRun((is_resolve, G("192.0.2.13")), (is_sh, P(0, msys)), (is_ps, P(127, "", "powershell: not found")))
        rec = d.probe_host("h13", "builder@192.0.2.13", run=run)
        self.assertTrue(rec["ok"])
        self.assertEqual((rec["facts"]["os"], rec["facts"]["cpus"]), ("windows", 4))

    def test_neither_sh_nor_powershell(self):
        run = FakeRun((is_resolve, G("192.0.2.14")), (is_sh, P(127, "", "bash: sh: command not found")),
                      (is_ps, P(127, "", "bash: powershell: command not found")))
        p = d.probe_host("h14", "deploy@192.0.2.14", run=run)["problem"]
        self.assertEqual(p["class"], "other")
        self.assertIn("sh said 'bash: sh: command not found', powershell said 'bash: powershell: command not found'",
                      p["what"])
        self.assertEqual(p["tried"], BASE + " deploy@192.0.2.14 'sh -s'")

    def test_docker_problems(self):
        cases = [
            ("orch_probe=1\nuname=Linux\nuser=deploy\ndocker_cli=0\n", "docker-missing", None),
            (sh_out("docker_err=permission denied while trying to connect to the Docker daemon socket at "
                    "unix:///var/run/docker.sock: connect: permission denied", socket="none", user="ci"),
             "docker-permission", "on deploy@192.0.2.15: sudo usermod -aG docker ci, then log in again"),
            (sh_out("docker_err=Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker "
                    "daemon running?", socket="none"), "docker-down",
             "start Docker on deploy@192.0.2.15: sudo systemctl start docker"),
            (sh_out(socket="none"), "other", None),
        ]
        for out, cls, fix in cases:
            run = FakeRun((is_resolve, G("192.0.2.15")), (is_sh, P(0, out)))
            rec = d.probe_host("h15", "deploy@192.0.2.15", run=run)
            self.assertFalse(rec["ok"], cls)
            self.assertEqual(rec["problem"]["class"], cls)
            self.assertEqual(rec["problem"]["tried"], BASE + " deploy@192.0.2.15 'docker info'")
            self.assertEqual(rec["facts"]["os"], "linux")
            if fix:
                self.assertEqual(rec["problem"]["fix"], fix)

    def test_odd_remote_user_never_reaches_the_fix(self):
        out = sh_out("docker_err=permission denied while trying to connect to the Docker daemon socket", socket="none",
                     user="x;reboot")
        rec = d.probe_host("h15", "app1", run=FakeRun((is_resolve, G("192.0.2.15", "deploy")), (is_sh, P(0, out))))
        self.assertEqual(rec["problem"]["fix"], "on app1: sudo usermod -aG docker deploy, then log in again")

    def test_socket_fallback_is_ok(self):
        out = sh_out("docker_err=Cannot connect to the Docker daemon at unix:///run/user/1000/docker.sock. Is the "
                     "docker daemon running?", socket="unix:///var/run/docker.sock")
        rec = d.probe_host("h16", "app1", run=FakeRun((is_resolve, G("192.0.2.16")), (is_sh, P(0, out))))
        self.assertTrue(rec["ok"])
        self.assertIsNone(rec["problem"])
        self.assertEqual(rec["facts"]["socket"], "unix:///var/run/docker.sock")

    def test_prod_only_host_is_ssh_true_only(self):
        run = FakeRun((is_resolve, G("192.0.2.20", "deploy", 2222)), (is_true, P(0)))
        rec = d.probe_host("p20", "deploy@192.0.2.20:2222", docker=False, run=run)
        self.assertEqual((rec["ok"], rec["facts"], rec["problem"]), (True, {}, None))
        self.assertEqual(run.argvs[1], d.SSH_BASE + ["-p", "2222", "deploy@192.0.2.20", "true"])
        self.assertEqual(len(run.calls), 2)
        run = FakeRun((is_resolve, G("192.0.2.20")), (is_true, P(255, "", "ssh: connect to host 192.0.2.20 port "
                                                                         "2222: Connection refused")))
        rec = d.probe_host("p20", "deploy@192.0.2.20:2222", docker=False, run=run)
        self.assertEqual((rec["ok"], rec["problem"]["class"]), (False, "network"))

    def test_local_failures_become_problems(self):
        run = FakeRun((is_resolve, G("192.0.2.17")), (is_sh, subprocess.TimeoutExpired(["ssh"], 40)))
        p = d.probe_host("h17", "app1", run=run)["problem"]
        self.assertEqual(p["class"], "other")
        self.assertIn("did not finish within 40 s", p["what"])
        self.assertEqual(p["fix"], "check that app1 answers (machine load, a hung Docker daemon)")
        self.assertEqual(p["tried"], f"{BASE} app1 'docker info'")
        p = d.probe_host("h17", "app1", run=FakeRun((is_resolve, FileNotFoundError(2, "x"))))["problem"]
        self.assertEqual(p["tried"], "ssh -V")
        self.assertIn("install the OpenSSH client", p["fix"])
        p = d.probe_host("h17", "app1", run=FakeRun((is_resolve, P(255, "", "line 1: Bad configuration option"))))
        self.assertIn("fix ~/.ssh/config", p["problem"]["fix"])
        self.assertIsNone(p["resolved"])

    def test_no_fix_repeats_the_tried_command(self):
        cases = [d.classify_ssh_error(255, "ssh: connect to host 192.0.2.40 port 22: Connection refused", "app1"),
                 d.probe_host("h17", "app1", run=FakeRun((is_resolve, G("192.0.2.17")),
                                                          (is_sh, subprocess.TimeoutExpired(["ssh"], 40))))["problem"],
                 d.probe_host("h14", "app1", run=FakeRun((is_resolve, G("192.0.2.14")), (is_sh, P(127, "", "x")),
                                                          (is_ps, P(127, "", "y"))))["problem"],
                 d.probe_host("h18", "app1", run=FakeRun((is_resolve, subprocess.TimeoutExpired(["ssh"], 15)))
                              )["problem"]]
        for p in cases:
            self.assertNotIn("then retry", p["fix"], p)
            self.assertNotIn(p["tried"], p["fix"], p)

    def test_bad_spec_runs_nothing(self):
        run = FakeRun()
        rec = d.probe_host("h1", "app1; rm -rf ~", run=run)
        self.assertEqual((rec["ok"], rec["problem"]["class"], run.calls), (False, "other", []))


class ProbeHosts(unittest.TestCase):
    def test_parallel_and_prod_only(self):
        barrier = threading.Barrier(3, timeout=10)  # all three resolves must be in flight at once

        def resolve(argv, kw):
            barrier.wait()
            return G({"app1": "192.0.2.11", "deploy@192.0.2.12": "192.0.2.12"}.get(argv[-1], "192.0.2.20"))

        run = FakeRun((is_resolve, resolve), (is_sh, P(0, LINUX)), (is_true, P(0)))
        out = d.probe_hosts({"h11": "app1", "h12": "deploy@192.0.2.12", "p20": "deploy@192.0.2.20:2222"},
                            {"h11", "h12"}, run=run)
        self.assertEqual(list(out), ["h11", "h12", "p20"])
        self.assertTrue(all(r["ok"] for r in out.values()), {k: r["problem"] for k, r in out.items()})
        self.assertEqual(out["p20"]["facts"], {})
        self.assertEqual(sum(is_sh(a) for a in run.argvs), 2)
        self.assertEqual(sum(is_true(a) for a in run.argvs), 1)

    def test_one_crash_keeps_the_others(self):
        def resolve(argv, kw):
            if argv[-1] == "app1":
                raise RuntimeError("boom")
            return G("192.0.2.12")

        out = d.probe_hosts({"h11": "app1", "h12": "deploy@192.0.2.12"}, {"h11", "h12"},
                            run=FakeRun((is_resolve, resolve), (is_sh, P(0, LINUX))))
        self.assertFalse(out["h11"]["ok"])
        self.assertIn("failed inside orch: RuntimeError: boom", out["h11"]["problem"]["what"])
        self.assertTrue(out["h12"]["ok"])
        self.assertEqual(d.probe_hosts({}, set(), run=FakeRun()), {})


# ----------------------------------------------------------------------------- master, transport, contexts
@mock.patch.dict(os.environ, NO_NS)
class ForeignNamespace(unittest.TestCase):
    """Council 2 MF5: objects named with this master's prefix that another orch namespace made (an older orch, another
    master) keep a helper from being ready, with the exact fix; orch never removes them itself."""
    ROWS = [("container", "agentx-1030-job", "io.other.agent=1,io.other.job=1030"),
            ("volume", "agentx-cache-npm", ""),
            ("network", "agentx-1030-net", "com.docker.compose.project=x"),
            ("container", "agentx-mine", "io.orch.agent=1,io.orch.owner=m1"),  # ours
            ("container", "agentx-half", "io.orch.owner=m1"),  # our family without the agent label: a collision
            ("container", "buildx_buildkit_agentx-builder0", ""),  # the name filter matches inside names too
            ("container", "agentx-x;rm -rf ~", ""),  # docker never prints such a name: never reaches a fix line
            ("image", "agentx-img:1", ""), ("container", "agentx-1030-job", "io.other.agent=1")]
    FIX = ("run that orch's gc --session-end on this host (removing volumes needs the user's OK); or, if no other "
           "master uses this host, remove them by hand: ")

    def test_which_objects_belong_to_another_namespace(self):
        self.assertEqual(d.foreign_objects(self.ROWS, "agentx-", "io.orch.agent"), [
            {"kind": "container", "name": "agentx-1030-job", "family": "io.other"},
            {"kind": "volume", "name": "agentx-cache-npm", "family": None},
            {"kind": "network", "name": "agentx-1030-net", "family": None}])
        self.assertEqual(d.foreign_objects(self.ROWS, "agentt-", "io.orchtest.agent"), [])  # another prefix: none
        self.assertEqual(d.labels_of("a=1,b=x=y,,c,d= 2"), {"a": "1", "b": "x=y", "d": "2"})

    def test_problem_names_them_and_the_exact_fix(self):
        fo = d.foreign_objects(self.ROWS, "agentx-", "io.orch.agent")
        self.assertEqual(d.foreign_problem(fo, "agentx-", "tried-x"), {
            "class": "foreign-namespace", "tried": "tried-x",
            "what": "3 objects named agentx-* belong to another orch namespace (label io.other.*, none): an older "
                    "orch or another master",
            "fix": self.FIX + "docker rm -f agentx-1030-job; docker volume rm agentx-cache-npm; docker network rm "
                              "agentx-1030-net"})
        one = d.foreign_problem(fo[:1], "agentx-")
        self.assertEqual(one["what"], "1 object named agentx-* belongs to another orch namespace (label io.other.*): "
                                      "an older orch or another master")
        self.assertEqual(one["fix"], self.FIX + "docker rm -f agentx-1030-job")
        many = [{"kind": "container", "name": f"agentx-{i}-job", "family": None} for i in range(30)]
        fix = d.foreign_problem(many, "agentx-")["fix"]
        self.assertIn("agentx-23-job; then list the other 6 with docker ps -a, docker volume ls and docker network "
                      "ls, each with --filter name=agentx-", fix)
        self.assertNotIn("agentx-24-job", fix)

    def test_probe_reports_them_with_facts_kept(self):
        ns = ["ns=container\tagentx-1030-job\tio.other.agent=1,io.other.job=1030", "ns=volume\tagentx-cache-npm\t",
              "ns=container\tagentx-mine\tio.orch.agent=1,io.orch.owner=m1"]
        run = FakeRun((is_resolve, G("192.0.2.12")), (is_sh, P(0, sh_out(*ns))))
        rec = d.probe_host("h12", "deploy@192.0.2.12", run=run)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["problem"], {
            "class": "foreign-namespace", "tried": BASE + " deploy@192.0.2.12 'docker ps -a --filter name=agentx-'",
            "what": "2 objects named agentx-* belong to another orch namespace (label io.other.*, none): an older "
                    "orch or another master",
            "fix": self.FIX + "docker rm -f agentx-1030-job; docker volume rm agentx-cache-npm"})
        self.assertEqual(rec["facts"]["docker"], "27.3.1")  # Docker answers: facts kept
        self.assertEqual(len(run.calls), 2)  # still one ssh round trip
        run = FakeRun((is_resolve, G("192.0.2.12")), (is_sh, P(0, sh_out(ns[2]))))
        self.assertTrue(d.probe_host("h12", "deploy@192.0.2.12", run=run)["ok"])  # only ours

    def test_this_masters_namespace_comes_from_env(self):
        with mock.patch.dict(os.environ, {"ORCH_LABEL": "io.orchtest.agent", "ORCH_PREFIX": "agentt-"}):
            self.assertEqual(d.own_namespace(), ("io.orchtest.agent", "agentt-"))
            self.assertIn("--filter 'name=agentt-'", d.probe_script_sh())
            self.assertIn("' --filter name=agentt- --format", d.probe_script_ps())
            ns = ["ns=container\tagentx-1-run\tio.orch.agent=1", "ns=container\tagentt-old\tio.orch.agent=1"]
            run = FakeRun((is_resolve, G("192.0.2.12")), (is_sh, P(0, sh_out(*ns))))
            pr = d.probe_host("h12", "deploy@192.0.2.12", run=run)["problem"]
            self.assertEqual(pr["what"], "1 object named agentt-* belongs to another orch namespace (label io.orch.*): "
                                         "an older orch or another master")
        with mock.patch.dict(os.environ, {"ORCH_LABEL": "x'; rm -rf /", "ORCH_PREFIX": "a b"}):
            self.assertEqual(d.own_namespace(), (d.DEFAULT_LABEL, d.DEFAULT_PREFIX))
            self.assertNotIn("rm -rf", d.probe_script_sh() + d.probe_script_ps())


class Master(unittest.TestCase):
    def test_tools_present(self):
        run = FakeRun((lambda a: a == ["docker", "--version"], P(0, "Docker version 27.3.1, build ce12230\n")),
                      (lambda a: a == ["git", "--version"], P(0, "git version 2.47.1\n")))
        m = d.master_facts(run=run)
        self.assertEqual(m, {"os": "windows" if os.name == "nt" else platform.system().lower(),
                             "python": platform.python_version(), "git": True, "docker_cli": "27.3.1"})

    def test_tools_missing(self):
        run = FakeRun((lambda a: a[0] == "docker", FileNotFoundError(2, "x")), (lambda a: a[0] == "git", P(1)))
        m = d.master_facts(run=run)
        self.assertEqual((m["docker_cli"], m["git"]), (None, False))


class Transport(unittest.TestCase):
    CLI, NOCLI = {"docker_cli": "27.3.1"}, {"docker_cli": None}

    def rec(self, os_name="linux", socket="default", ok=True, name="h11"):
        return {"name": name, "ok": ok, "spec": "app1", "facts": {"os": os_name, "socket": socket},
                "problem": None if ok else {"class": "auth"}}

    def test_matrix(self):
        ctx = {"transport": "context", "context": "orch-h11", "docker_host": None}
        self.assertEqual(d.choose_transport(self.rec(), self.CLI), ctx)
        self.assertEqual(d.choose_transport(self.rec("windows"), self.CLI), ctx)
        self.assertEqual(d.choose_transport(self.rec(), self.NOCLI),
                         {"transport": "ssh-docker", "context": None, "docker_host": None})
        sock = "unix:///run/user/1000/docker.sock"
        for m in (self.CLI, self.NOCLI):
            self.assertEqual(d.choose_transport(self.rec(socket=sock), m),
                             {"transport": "ssh-docker", "context": None, "docker_host": sock})

    def test_windows_without_cli(self):
        p = d.choose_transport(self.rec("windows"), self.NOCLI)["problem"]
        self.assertEqual((p["class"], p["tried"]), ("windows-no-cli", "docker --version"))
        self.assertIn("install the Docker CLI on this machine", p["fix"])

    def test_problems_pass_through(self):
        self.assertEqual(d.choose_transport(self.rec(ok=False), self.CLI), {"problem": {"class": "auth"}})
        self.assertEqual(d.choose_transport(self.rec("darwin"), self.CLI)["problem"]["class"], "other")
        self.assertEqual(d.choose_transport({"ok": True, "spec": "p", "facts": {}}, self.CLI),
                         {"transport": None, "context": None, "docker_host": None})

    def test_name(self):
        self.assertEqual(d.choose_transport(self.rec(name=None), self.CLI, name="h12b")["context"], "orch-h12b")
        with self.assertRaises(ValueError):
            d.choose_transport(self.rec(name=None), self.CLI)
        with self.assertRaises(ValueError):
            d.context_name("../x")


class Context(unittest.TestCase):
    ENV = {"DOCKER_CONFIG": "/tmp/orch-test-docker"}

    @staticmethod
    def inspect(host):
        return P(0, json.dumps([{"Name": "orch-h11", "Endpoints": {"docker": {"Host": host, "SkipTLSVerify": False}}}]))

    def test_create(self):
        run = FakeRun((lambda a: a[2] == "inspect", P(1, "", 'context "orch-h11": context not found')),
                      (lambda a: a[2] == "create", P(0, "orch-h11\n")))
        self.assertEqual(d.ensure_context("h11", "deploy@192.0.2.11:2222", run=run, env=self.ENV), "orch-h11")
        self.assertEqual(run.argvs, [["docker", "context", "inspect", "orch-h11"],
                                     ["docker", "context", "create", "orch-h11", "--description",
                                      "orch helper h11 (deploy@192.0.2.11:2222)", "--docker",
                                      "host=ssh://deploy@192.0.2.11:2222"]])
        self.assertTrue(all(kw["env"] is self.ENV for _, kw in run.calls))

    def test_update_when_the_endpoint_moved(self):
        # only this project's own earlier context (own) is updated in place
        run = FakeRun((lambda a: a[2] == "inspect", self.inspect("ssh://deploy@192.0.2.99")),
                      (lambda a: a[2] == "update", P(0)))
        self.assertEqual(d.ensure_context("h11", "app1", run=run, own={"orch-h11"}), "orch-h11")
        self.assertEqual(run.argvs[1], ["docker", "context", "update", "orch-h11", "--description",
                                        "orch helper h11 (app1)", "--docker", "host=ssh://app1"])
        self.assertNotIn("env", run.calls[0][1])  # env None = inherit

    def test_another_projects_context_is_never_changed(self):
        # orch-h30 reaches the machine as deploy@ for another project: ci@ gets its own endpoint context
        alt = d.endpoint_context("h30", "ci@192.0.2.30")
        self.assertRegex(alt, r"^orch-h30-[0-9a-f]{8}$")
        self.assertNotEqual(alt, d.endpoint_context("h30", "deploy@192.0.2.30"))
        store = {"orch-h30": "ssh://deploy@192.0.2.30"}

        def docker(argv, kw):
            verb, name = argv[2], argv[3]
            if verb == "inspect":
                return self.inspect(store[name]) if name in store else P(1, "", f'context "{name}": not found')
            store[name] = argv[-1].split("=", 1)[1]
            return P(0)
        run = FakeRun((lambda a: a[:2] == ["docker", "context"], docker))
        self.assertEqual(d.ensure_context("h30", "ci@192.0.2.30", run=run), alt)
        self.assertEqual(store, {"orch-h30": "ssh://deploy@192.0.2.30", alt: "ssh://ci@192.0.2.30"})
        self.assertFalse([a for a in run.argvs if a[2] == "update"])
        n = len(run.calls)  # both endpoints again: no change, the right context each
        self.assertEqual(d.ensure_context("h30", "deploy@192.0.2.30", run=run), "orch-h30")
        self.assertEqual(d.ensure_context("h30", "ci@192.0.2.30", run=run), alt)
        self.assertFalse([a for a in run.argvs[n:] if a[2] != "inspect"])
        store[alt] = "ssh://someone@192.0.2.30"  # both names point elsewhere and neither is ours: refused
        with self.assertRaises(d.DiscoverError) as cm:
            d.ensure_context("h30", "ci@192.0.2.30", run=run)
        self.assertIn(f"docker contexts orch-h30 and {alt} both point at other endpoints", str(cm.exception))
        self.assertEqual(store["orch-h30"], "ssh://deploy@192.0.2.30")

    def test_noop_when_current(self):
        run = FakeRun((lambda a: a[2] == "inspect", self.inspect("ssh://deploy@[2001:db8::1]:2222")))
        self.assertEqual(d.ensure_context("h11", "deploy@[2001:db8::1]:2222", run=run), "orch-h11")
        self.assertEqual(len(run.calls), 1)

    def test_create_failure_names_the_command(self):
        run = FakeRun((lambda a: a[2] == "inspect", P(1)), (lambda a: a[2] == "create", P(1, "", "bad things\n")))
        with self.assertRaises(d.DiscoverError) as cm:
            d.ensure_context("h11", "app1", run=run)
        self.assertIn("docker could not create context orch-h11: bad things", str(cm.exception))
        self.assertIn("docker context create orch-h11", str(cm.exception))
        with self.assertRaises(d.DiscoverError) as cm:
            d.ensure_context("h11", "app1", run=FakeRun((lambda a: True, FileNotFoundError(2, "x"))))
        self.assertIn("docker is not installed", str(cm.exception))

    def test_remove(self):
        run = FakeRun((lambda a: a[2] == "rm", P(0)))
        d.remove_context("h11", run=run, env=self.ENV)
        self.assertEqual(run.argvs, [["docker", "context", "rm", "--force", "orch-h11"]])
        self.assertIs(run.calls[0][1]["env"], self.ENV)
        d.remove_context("h11", run=FakeRun((lambda a: True, P(1, "", 'context "orch-h11" does not exist'))))
        with self.assertRaises(d.DiscoverError):
            d.remove_context("h11", run=FakeRun((lambda a: True, P(1, "", "permission denied"))))
        run = FakeRun((lambda a: a[2] == "rm", P(0)))  # the context the state names (an endpoint context)
        d.remove_context("h11", run=run, context="orch-h11-0a1b2c3d")
        self.assertEqual(run.argvs, [["docker", "context", "rm", "--force", "orch-h11-0a1b2c3d"]])
        for bad in ("default", "-f", "orch-", "orch-h11;rm", "desktop-linux"):
            with self.assertRaises(ValueError, msg=bad):
                d.remove_context("h11", run=FakeRun(), context=bad)


# ----------------------------------------------------------------------------- the probe scripts themselves
_SH = shutil.which("sh")
_PS = shutil.which("powershell") if os.name == "nt" else None
INFO = docker_info(ID="0a1b2c3d-0000-4000-8000-0000000000aa")
PS_ROWS = ["app1-web\tapp1:1\t0.0.0.0:8080->80/tcp, :::8080->80/tcp\t",
           "app1-db\tpostgres:16\t127.0.0.1:5432->5432/tcp\t",
           "shop-gw\tnginx:1.27\t0.0.0.0:8443->443/tcp, [::]:8443->443/tcp\t",
           "shop-api\tshop-api:2\t0.0.0.0:9000->9000/tcp\t",
           "metrics\texporter:1\t0.0.0.0:9100-9101->9100-9101/tcp\t",
           "dns\tcoredns:1.11\t0.0.0.0:53->53/udp\t",
           "agentx-t-ok-1\talpine:3.20\t\t1",
           "labelled\tbuildkit:0.16\t0.0.0.0:7000->7000/tcp\t1"]
STUB_DOCKER = r"""#!/bin/sh
H=
if [ "$1" = -H ]; then H=$2; shift 2; fi
echo "${H:-default} $1" >> "$FAKE_DIR/log"
case $1 in ps|volume|network) echo "$*" >> "$FAKE_DIR/args" ;; esac
case $1 in
ps) if [ "$2" = -a ]; then cat "$FAKE_DIR/psa.txt" 2>/dev/null; exit 0; fi; cat "$FAKE_DIR/ps.txt" ;;
volume|network) cat "$FAKE_DIR/$1.txt" 2>/dev/null ;;
info)
  case "$FAKE_DOCKER:$H" in
  ok:*) cat "$FAKE_DIR/info.json"; exit 0 ;;
  explicit:unix:///var/run/docker.sock) cat "$FAKE_DIR/info_spaced.json"; exit 0 ;;
  explicit:*) echo '{"ServerVersion":"","ServerErrors":["x"]}'
    echo "Cannot connect to the Docker daemon at unix:///run/user/1000/docker.sock. Is the docker daemon running?" >&2
    exit 0 ;;
  perm:*) echo "permission denied while trying to connect to the Docker daemon socket at" \
    "unix:///var/run/docker.sock: connect: permission denied" >&2; exit 1 ;;
  *) echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?" >&2
    exit 1 ;;
  esac ;;
buildx) echo "github.com/docker/buildx v0.17.1 0000000" ;;
*) exit 1 ;;
esac
"""
STUB_CURL = r"""#!/bin/sh
for a; do u=$a; done
echo "curl $u" >> "$FAKE_DIR/log"
case $u in
http://127.0.0.1:8080/) printf '200 0.004512' ;;
http://127.0.0.1:8443/) printf '400 0.001000' ;;
https://127.0.0.1:8443/) printf '302 0.011803' ;;
*) printf '000 0.000000'; exit 7 ;;
esac
"""
STUB_UNAME = """#!/bin/sh
case $1 in -m) echo x86_64 ;; *) echo Linux ;; esac
"""


@unittest.skipUnless(_SH, "no sh on PATH")
@mock.patch.dict(os.environ, NO_NS)
class ShProbeScript(unittest.TestCase):
    """The real POSIX probe in the local sh, with docker, curl and uname replaced by stubs first on PATH."""

    def run_probe(self, mode, ns=None):
        """ns: {"psa" (docker ps -a), "volume", "network": [name<TAB>labels rows]} for the namespace listing."""
        tmp = Path(tempfile.mkdtemp(prefix="orch-discover-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for name, body in (("docker", STUB_DOCKER), ("curl", STUB_CURL), ("uname", STUB_UNAME)):
            (tmp / name).write_text(body, encoding="utf-8", newline="\n")
            os.chmod(tmp / name, 0o755)
        (tmp / "info.json").write_text(INFO + "\n", encoding="utf-8", newline="\n")
        (tmp / "info_spaced.json").write_text(json.dumps(json.loads(INFO)) + "\n", encoding="utf-8", newline="\n")
        (tmp / "ps.txt").write_text("\n".join(PS_ROWS) + "\n", encoding="utf-8", newline="\n")
        for k, rows in (ns or {}).items():
            (tmp / f"{k}.txt").write_text("".join(x + "\n" for x in rows), encoding="utf-8", newline="\n")
        env = dict(os.environ, PATH=str(tmp) + os.pathsep + os.environ.get("PATH", ""), FAKE_DIR=str(tmp),
                   FAKE_DOCKER=mode)
        env.pop("DOCKER_HOST", None)
        r = subprocess.run([_SH, "-s"], input=d.probe_script_sh().encode(), capture_output=True, env=env, timeout=120)
        log = (tmp / "log").read_text(encoding="utf-8") if (tmp / "log").exists() else ""
        self.args = (tmp / "args").read_text(encoding="utf-8") if (tmp / "args").exists() else ""
        return r, d._kv(d._dec(r.stdout)), log

    def test_another_namespace_is_listed(self):  # council 2 MF5: every container (also stopped), volume and network
        r, kv, _ = self.run_probe("ok", ns={
            "psa": ["agentx-1030-job\tio.other.agent=1,io.other.job=1030", "agentx-mine\tio.orch.agent=1",
                    "agentx-stopped\t"],
            "volume": ["agentx-cache-npm\t"], "network": ["agentx-1030-net\tio.other.agent=1"]})
        self.assertEqual(r.returncode, 0, d._dec(r.stderr))
        self.assertEqual(kv["ns"], ["container\tagentx-1030-job\tio.other.agent=1,io.other.job=1030",
                                    "container\tagentx-mine\tio.orch.agent=1", "container\tagentx-stopped\t",
                                    "volume\tagentx-cache-npm\t", "network\tagentx-1030-net\tio.other.agent=1"])
        fo = d.foreign_objects([v.split("\t", 2) for v in kv["ns"]], "agentx-", "io.orch.agent")
        self.assertEqual([o["name"] for o in fo], ["agentx-1030-job", "agentx-stopped", "agentx-cache-npm",
                                                   "agentx-1030-net"])
        for argv in ("ps -a --filter name=agentx- --format {{.Names}}\\t{{.Labels}}",
                     "volume ls --filter name=agentx- --format {{.Name}}\\t{{.Labels}}",
                     "network ls --filter name=agentx- --format {{.Name}}\\t{{.Labels}}"):
            self.assertIn(argv, self.args)
        self.assertEqual(len(d._facts(kv)["cotenants"]), 6)  # the rest of the probe is unchanged

    def test_ok(self):
        r, kv, log = self.run_probe("ok")
        self.assertEqual(r.returncode, 0, d._dec(r.stderr))
        f = d._facts(kv)
        self.assertEqual((f["os"], f["arch"], f["docker"], f["socket"], f["buildx"]),
                         ("linux", "x86_64", "27.3.1", "default", True))
        self.assertEqual(f["docker_id"], "0a1b2c3d-0000-4000-8000-0000000000aa")
        self.assertIsInstance(f["cpus"], int)
        self.assertTrue(f["disk_free_bytes"] is None or f["disk_free_bytes"] > 0)
        self.assertEqual([c["name"] for c in f["cotenants"]], ["app1-db", "app1-web", "dns", "metrics", "shop-api",
                                                               "shop-gw"])
        self.assertEqual(f["endpoints"], TWO_EPS)
        curls = set(re.findall(r"curl (\S+)", log))
        self.assertEqual(curls, {"http://127.0.0.1:8080/", "http://127.0.0.1:8443/", "https://127.0.0.1:8443/",
                                 "http://127.0.0.1:9000/", "https://127.0.0.1:9000/", "http://127.0.0.1:9100/",
                                 "https://127.0.0.1:9100/"})  # not 5432 (non-HTTP), 53 (udp), 7000 (orch label)
        self.assertNotIn("docker_err", kv)

    def test_default_socket_dead_explicit_works(self):
        r, kv, log = self.run_probe("explicit")
        f = d._facts(kv)
        self.assertEqual((f["socket"], f["docker"]), ("unix:///var/run/docker.sock", "27.3.1"))
        self.assertIn("unix:///run/user/1000/docker.sock", d._one(kv, "docker_err"))
        self.assertIn("unix:///var/run/docker.sock ps", log)  # the rest of the probe uses the socket that worked
        self.assertEqual(len(f["endpoints"]), 2)

    def test_docker_problems(self):
        for mode, cls in (("perm", "docker-permission"), ("down", "docker-down")):
            r, kv, log = self.run_probe(mode)
            self.assertEqual(d._one(kv, "docker_socket"), "none", mode)
            self.assertNotIn("ctr", kv)
            self.assertEqual(d.classify_ssh_error(1, d._one(kv, "docker_err"), "app1")["class"], cls)
            self.assertEqual(log.count(" info"), 3, mode)  # default, /var/run/docker.sock, rootless socket


PS_JSON = [{"Names": "app1-web", "Image": "app1:1", "Labels": "com.docker.compose.project=app1",
            "Ports": "0.0.0.0:8080->80/tcp"},
           {"Names": "shop-gw", "Image": "nginx:1.27", "Labels": "", "Ports": "0.0.0.0:8443->443/tcp"},
           {"Names": "cache", "Image": "redis:7", "Labels": "", "Ports": "127.0.0.1:6379->6379/tcp"},
           {"Names": "agentx-t-ok-1", "Image": "alpine:3.20", "Labels": "io.orch.agent=1", "Ports": ""},
           {"Names": "builder-x", "Image": "buildkit:0.16", "Labels": "io.orch.agent=1",
            "Ports": "0.0.0.0:7000->7000/tcp"}]
STUB_DOCKER_CMD = "\r\n".join([
    "@echo off",
    'echo %1 >> "%~dp0log.txt"',
    'if "%1"=="ps" if "%2"=="-a" (echo %* >> "%~dp0args.txt" & type "%~dp0psa.txt" 2>nul & exit /b 0)',
    'if "%1"=="volume" (echo %* >> "%~dp0args.txt" & type "%~dp0volume.txt" 2>nul & exit /b 0)',
    'if "%1"=="network" (echo %* >> "%~dp0args.txt" & type "%~dp0network.txt" 2>nul & exit /b 0)',
    'if "%1"=="ps" (type "%~dp0ps.jsonl" & exit /b 0)',
    'if "%1"=="buildx" (echo github.com/docker/buildx v0.17.1 & exit /b 0)',
    'if not "%1"=="info" exit /b 1',
    'if exist "%~dp0down.flag" (type "%~dp0down.json" & type "%~dp0down.err" 1>&2 & exit /b 1)',
    'type "%~dp0info.json"', ""])
STUB_CURL_CMD = "\r\n".join([
    "@echo off", "setlocal", 'for %%a in (%*) do set "u=%%~a"', 'echo curl %u% >> "%~dp0log.txt"',
    'if "%u%"=="http://127.0.0.1:8080/" (echo 200 0.004512& exit /b 0)',
    'if "%u%"=="http://127.0.0.1:8443/" (echo 400 0.001000& exit /b 0)',
    'if "%u%"=="https://127.0.0.1:8443/" (echo 302 0.011803& exit /b 0)',
    "echo 000 0.000000", "exit /b 7", ""])


@unittest.skipUnless(_PS, "Windows PowerShell only")
@mock.patch.dict(os.environ, NO_NS)
class PsProbeScript(unittest.TestCase):
    """The real PowerShell probe, run locally exactly as sshd would run it, with PATH = only a stub dir (docker.cmd,
    curl.cmd): no real docker or curl can be reached."""

    def run_probe(self, docker=True, down=False, ns=None):
        tmp = Path(tempfile.mkdtemp(prefix="orch-discover-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for k, rows in (ns or {}).items():  # psa (docker ps -a), volume, network: name<TAB>labels rows
            (tmp / f"{k}.txt").write_text("".join(x + "\n" for x in rows), encoding="utf-8")
        self.tmp = tmp
        if docker:
            (tmp / "docker.cmd").write_bytes(STUB_DOCKER_CMD.encode())
            (tmp / "info.json").write_text(INFO + "\n", encoding="utf-8")
            (tmp / "ps.jsonl").write_text("\n".join(json.dumps(x) for x in PS_JSON) + "\n", encoding="utf-8")
            (tmp / "down.json").write_text('{"ServerVersion":"","ServerErrors":["x"]}\n', encoding="utf-8")
            (tmp / "down.err").write_text("error during connect: this error may indicate that the docker daemon is "
                                          "not running: open //./pipe/dockerDesktopLinuxEngine: The system cannot "
                                          "find the file specified.\n", encoding="utf-8")
            if down:
                (tmp / "down.flag").write_text("x", encoding="utf-8")
        (tmp / "curl.cmd").write_bytes(STUB_CURL_CMD.encode())
        env = dict(os.environ, PATH=str(tmp))
        enc = d.probe_command_ps()[len(d.PS_PREFIX):]
        r = subprocess.run([_PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True,
                           env=env, timeout=180)
        log = (tmp / "log.txt").read_text(encoding="utf-8", errors="replace") if (tmp / "log.txt").exists() else ""
        return r, d._kv(d._dec(r.stdout)), log

    def test_ok(self):
        r, kv, log = self.run_probe()
        self.assertEqual(r.returncode, 0, d._dec(r.stderr))
        f = d.parse_probe_ps(d._dec(r.stdout))
        self.assertEqual((f["os"], f["docker"], f["socket"], f["buildx"], f["psi"]),
                         ("windows", "27.3.1", "default", True, False))
        self.assertGreater(f["cpus"], 0)
        self.assertGreater(f["mem_bytes"], 0)
        self.assertEqual([c["name"] for c in f["cotenants"]], ["app1-web", "cache", "shop-gw"])
        self.assertEqual(f["endpoints"], TWO_EPS)
        self.assertNotIn("6379", log)  # redis port skipped
        self.assertNotIn("7000", log)  # orch-labelled container skipped

    def test_another_namespace_is_listed(self):  # council 2 MF5
        r, kv, log = self.run_probe(ns={"psa": ["agentx-1030-job\tio.other.agent=1,io.other.job=1030",
                                                "agentx-mine\tio.orch.agent=1"],
                                        "volume": ["agentx-cache-npm\t"], "network": ["agentx-1030-net"]})
        self.assertEqual(r.returncode, 0, d._dec(r.stderr))
        self.assertEqual(kv["ns"], ["container\tagentx-1030-job\tio.other.agent=1,io.other.job=1030",
                                    "container\tagentx-mine\tio.orch.agent=1", "volume\tagentx-cache-npm",
                                    "network\tagentx-1030-net"])
        fo = d.foreign_objects([v.split("\t", 2) for v in kv["ns"]], "agentx-", "io.orch.agent")
        self.assertEqual([o["name"] for o in fo], ["agentx-1030-job", "agentx-cache-npm", "agentx-1030-net"])
        args = (self.tmp / "args.txt").read_text(encoding="utf-8", errors="replace")
        self.assertIn('ps -a --filter name=agentx- --format "{{.Names}}\\t{{.Labels}}"', args)
        self.assertIn('volume ls --filter name=agentx- --format "{{.Name}}\\t{{.Labels}}"', args)
        self.assertEqual(len(d.parse_probe_ps(d._dec(r.stdout))["cotenants"]), 3)  # the rest is unchanged

    def test_docker_down_and_missing(self):
        r, kv, log = self.run_probe(down=True)
        self.assertEqual(d._one(kv, "docker_socket"), "none")
        p = d.classify_ssh_error(1, d._one(kv, "docker_err"), "builder@192.0.2.13")
        self.assertEqual(p["class"], "docker-down")
        self.assertIn("start Docker Desktop on builder@192.0.2.13", p["fix"])
        r, kv, log = self.run_probe(docker=False)
        self.assertEqual(d._one(kv, "docker_cli"), "0")


if __name__ == "__main__":
    unittest.main()
