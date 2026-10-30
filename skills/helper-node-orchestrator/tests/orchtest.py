"""Test harness: loads ../scripts/orch.py in an isolated namespace. Nothing real ever runs: orch's two local resolvers
(ssh_dump = `ssh -G`, context_endpoint = `docker context inspect`) are replaced by offline fakes (fake_ssh_dump,
fake_context_endpoint; REAL_SSH_DUMP / REAL_CONTEXT_ENDPOINT keep the originals).

Env is set BEFORE import: temp ORCH_HOME, ORCH_PREFIX=agentt-, ORCH_LABEL=io.orchtest.agent, ORCH_ALERT_DRYRUN=1,
ORCH_TEST_MODE=1 (legacy mode is allowed inside approved projects too: the project-mode tests approve temp projects)
and ORCH_CONFIG = a temp dir holding the SYNTHETIC legacy config from write_config(): generic nodes n1 (linux, context),
n2 (windows, context), n3 (linux, ssh-docker, ssh_args), policy values equal to the v1 defaults, orch's built-in t-* and
shell-investigate jobs, one project job app-test (container, stage unit) and one build job app-image (stage image), no
alert command. No test depends on any real site. `python -B tests/orchtest.py <dir>` writes the same config to <dir>.
"""
import atexit
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"

POLICY = {
    "admission": {"reserve_frac": 0.25, "psi_some_avg10_max": 10, "swap_pages_per_s_max": 50, "disk_free_frac_min": 0.15,
                  "disk_free_gb_min": 20, "inode_free_frac_min": 0.1, "load_frac_max": 0.85, "max_heavy": 1},
    "score_weights": {"mem": 0.5, "load": 0.3, "affinity": 0.2},
    "watchdog": {"interval_s": 10, "psi_max": 25, "iowait_max": 30, "lat_factor": 2.0, "lat_add_s": 0.2,
                 "consecutive": 3, "guard_cordon_min": 30},
    "lease": {"default_s": 300, "heartbeat_s": 60, "builder_idle_h": 24},
    "subnet_pool": "10.252.20.0/24",
    "subnet_prefix": 28,
    "builder": {"image": "buildkit", "memory": "4g", "cpus": 4, "pids": 4096, "gc_keepstorage_mb": "2000,20000,15000",
                "cap_gb": 15, "cache_volume_cap_mb": 6000},
}
T_JOBS = ["t-ok", "t-fail", "t-sleep", "t-oom", "t-hold"]
IO = {"read_bps": "150mb", "write_bps": "80mb", "blkio_weight": 100}
NODES = {
    "n1": {"enabled": True, "os": "linux", "host": "192.0.2.11", "ssh": "n1", "context": "n1", "transport": "context",
           "role": "helper", "tags": ["docker", "buildx", "linux-host"],
           "opt_in": ["app-test", "app-image", "shell-investigate"] + T_JOBS, "io": IO},
    "n2": {"enabled": True, "os": "windows", "host": "192.0.2.12", "ssh": "n2", "context": "n2", "transport": "context",
           "role": "helper", "tags": ["docker", "buildx", "windows-host"],
           "opt_in": ["app-test", "app-image", "shell-investigate"] + T_JOBS, "io": None},
    "n3": {"enabled": True, "os": "linux", "host": "192.0.2.13", "ssh": "n3", "ssh_args": ["-p", "2222", "deploy@192.0.2.13"],
           "context": None, "transport": "ssh-docker", "docker_host": "unix:///var/run/docker.sock", "role": "helper",
           "tags": ["docker", "linux-host"], "opt_in": ["app-test", "shell-investigate"] + T_JOBS, "io": IO},
}
APP_IMAGE = "python:3.12-slim@sha256:" + "1" * 64  # synthetic pin: tests never pull it
IMAGES = {"alpine": "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc",
          "buildkit": "moby/buildkit:v0.32.2-rootless@sha256:504731e577c20559c00f968f33219f30115e70be29ab96728d1d06e963fc494b",
          "app": APP_IMAGE}


def _t(cmd, **kw):
    """A built-in t-* test job (as v1)."""
    return dict({"kind": "container", "heavy": False, "requires": ["docker"], "test_only": True, "image": "alpine",
                 "source": "none", "memory": "64m", "cpus": 0.5, "pids": 64, "timeout": 60, "network": "none",
                 "cap_add": [], "cmd": cmd, "shell": "sh", "result_paths": ["/out"]}, **kw)


JOBS = {
    "app-test": {"kind": "container", "heavy": True, "stage": "unit", "requires": ["docker"], "image": "app", "repo": "app",
                 "source": "git-archive", "memory": "2g", "cpus": 2, "pids": 2048, "timeout": 1200, "prep_timeout": 900,
                 "network": "none", "prep_network": "egress-registries", "cache": {"name": "pip", "path": "/root/.cache/pip"},
                 "env": {"CI": "1"}, "prep_cap_add": [], "cap_add": [],
                 "prep": "set -e; mkdir -p /out; cd /src; pip install -r requirements.txt",
                 "cmd": "cd /src; python -m pytest -q --junitxml=/out/junit.xml", "shell": "sh",
                 "fail_patterns": ["^FAILED ", "^E "], "result_paths": ["/out"]},
    "app-image": {"kind": "build", "heavy": True, "stage": "image", "requires": ["docker", "buildx"], "repo": "app",
                  "source": "git-archive", "dockerfile": "Dockerfile", "context": ".", "targets": [""],
                  "results_stage": None, "build_args": {}, "memory": "4g", "timeout": 2400, "network": "none",
                  "fail_patterns": ["ERROR: "], "result_paths": []},
    "shell-investigate": {"kind": "container", "heavy": False, "requires": ["docker"], "image": "alpine", "source": "none",
                          "read_only": True, "memory": "128m", "cpus": 0.5, "pids": 64, "timeout": 120, "network": "none",
                          "cap_add": [], "cmd": "mkdir -p /tmp/out 2>/dev/null; {args}", "shell": "sh", "result_paths": []},
    "t-ok": _t("mkdir -p /out && echo ok-$(uname -m) > /out/ok.txt && id && cat /proc/self/status | grep -E "
               "'^(CapEff|NoNewPrivs)' && echo done"),
    "t-fail": _t("mkdir -p /out && echo 'not ok 1 - deliberate failure' | tee /out/t.log && exit 1", fail_patterns=["not ok "]),
    "t-sleep": _t("mkdir -p /out && echo sleeping > /out/s.txt && sleep 120", timeout=8),
    "t-oom": _t("mkdir -p /out && echo start > /out/o.txt && x=$(head -c 300000000 /dev/zero | tr '\\0' a) && echo ${#x}"),
    "t-hold": _t("mkdir -p /out && echo holding > /out/h.txt && sleep {args} && echo released", heavy=True, timeout=300,
                 default_args="30"),
}
PROTECTED = {
    "_doc": "synthetic test config",
    "alert": {"command": None, "window_start_h": 0, "window_end_h": 24},
    "never_touch_prefixes": ["app1", "shop-", "db-"],
    "nodes": {"n1": {"endpoints": [{"name": "app1-web :8080", "url": "http://127.0.0.1:8080/"}], "host_services": []},
              "n2": {"endpoints": [], "host_services": ["sshd", "com.docker.service", "WinRM", "TermService", "LanmanServer"]},
              "n3": {"endpoints": [], "host_services": []}},
}


def write_config(d, nodes=None, jobs=None, protected=None, repo="/srv/app") -> Path:
    """The synthetic legacy config (nodes.json, jobs.json, protected.json) in d; overrides replace whole sections."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    files = {"nodes.json": dict(json.loads(json.dumps(POLICY)), nodes=nodes if nodes is not None else NODES),
             "jobs.json": {"images": IMAGES, "repos": {"app": repo},
                           "source_exclude": [":(exclude,glob)**/.env", ":(exclude,glob)**/*.pem"],
                           "jobs": jobs if jobs is not None else JOBS},
             "protected.json": protected if protected is not None else PROTECTED}
    for name, data in files.items():
        (d / name).write_text(json.dumps(data, indent=1), encoding="utf-8")
    return d


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -B tests/orchtest.py <dir>  (writes the synthetic test config into <dir>)")
    print(write_config(sys.argv[1]))
    sys.exit(0)

TMP = Path(tempfile.mkdtemp(prefix="orchtest-"))
atexit.register(shutil.rmtree, TMP, True)
CFG = write_config(TMP / "config")

TEST_ENV = {"ORCH_HOME": str(TMP / "home"), "ORCH_PREFIX": "agentt-", "ORCH_LABEL": "io.orchtest.agent",
            "ORCH_CONFIG": str(CFG), "ORCH_SELFTEST": "1", "ORCH_ALERT_DRYRUN": "1", "ORCH_TEST_MODE": "1"}
os.environ.update(TEST_ENV)
for _k in ("ORCH_FAULT", "ORCH_VERBOSE", "ORCH_CI_CHILD"):
    os.environ.pop(_k, None)

_spec = importlib.util.spec_from_file_location("orch", ROOT / "scripts" / "orch.py")
orch = importlib.util.module_from_spec(_spec)
sys.modules["orch"] = orch
_spec.loader.exec_module(orch)
assert orch.PREFIX == "agentt-" and orch.L_AGENT == "io.orchtest.agent"


SSH_CONFIG = {}  # alias -> {"hostname", "port", "user", "proxyjump"} or {"fail": stderr}: the ~/.ssh/config of fake_ssh_dump
CONTEXTS = {}  # docker context name -> its endpoint (ssh://..., tcp://...): what fake_context_endpoint answers


def _fmt_jump(v):
    """As a real `ssh -G` prints ProxyJump: one hop parsed again (ssh:// dropped, an IP host in brackets:
    deploy@[192.0.2.20]:2222), several hops exactly as given."""
    if not v or "," in v:
        return v
    s = v[6:] if v.lower().startswith("ssh://") else v
    user, _, hp = s.rpartition("@")
    m = re.match(r"^\[([^\]]*)\](?::(\d+))?$", hp) or re.match(r"^([^:]*)(?::(\d+))?$", hp)
    host, port = (m.group(1), m.group(2)) if m else (hp, None)
    try:
        ipaddress.ip_address(host)
        host = f"[{host}]"
    except ValueError:
        pass
    return (f"{user}@" if user else "") + host + (f":{port}" if port else "")


def fake_ssh_dump(args):
    """Offline stand-in for orch.ssh_dump (`ssh -G`; unit tests never run ssh). OpenSSH's rules, checked against a real
    `ssh -G`: on the command line the first value of a setting wins (-o HostName/Port/User/ProxyJump, -p, -l, -J), and
    beats SSH_CONFIG for the destination; a destination SSH_CONFIG does not know is its own hostname; port 22; a single
    ProxyJump hop comes back reformatted (_fmt_jump)."""
    a, i, cl, dest = list(args), 0, {}, None
    while i < len(a):
        x = a[i]
        if x in ("-G", "-4", "-6"):
            i += 1
        elif x in ("-o", "-p", "-l", "-i", "-J", "-F"):
            v = a[i + 1] if i + 1 < len(a) else ""
            i += 2
            if x == "-o":
                k, _, val = v.partition("=")
                cl.setdefault(k.lower(), val)
            elif x in ("-p", "-l", "-J"):
                cl.setdefault({"-p": "port", "-l": "user", "-J": "proxyjump"}[x], v)
        else:
            dest = x
            break
    if not dest:
        return orch.R(255, "", "usage: ssh [options] destination")
    user, _, host = dest.rpartition("@")
    conf = SSH_CONFIG.get(host.lower(), {})
    if conf.get("fail"):
        return orch.R(255, "", conf["fail"])
    s = {"user": cl.get("user") or user or conf.get("user") or "tester",
         "hostname": cl.get("hostname") or conf.get("hostname") or host.lower(),
         "port": cl.get("port") or conf.get("port") or 22,
         "proxyjump": _fmt_jump(cl.get("proxyjump") or conf.get("proxyjump"))}
    return orch.R(0, "".join(f"{k} {v}\n" for k, v in s.items() if v))


def fake_context_endpoint(ctx):
    """Offline stand-in for orch.context_endpoint (`docker context inspect`): CONTEXTS, else None (no such context)."""
    return CONTEXTS.get(ctx)


REAL_SSH_DUMP, REAL_CONTEXT_ENDPOINT = orch.ssh_dump, orch.context_endpoint
orch.ssh_dump, orch.context_endpoint = fake_ssh_dump, fake_context_endpoint


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class _NoRun:
    def __init__(self, *a, **k):
        raise AssertionError(f"real subprocess attempted: {a[:1]}")


REAL_WATCHED_CMD = orch.watched_cmd  # the real poll-and-kill loop, for tests that run it against a fake Popen


class FakeCmd:
    """Patches orch.cmd and orch.watched_cmd (and subprocess.run/Popen inside orch) to record argv and return scripted
    orch.R objects. A watched_cmd call reaches the responder like a cmd call; its kw also holds "stop".

    responder(argv, kw) -> orch.R | None; or a list of R consumed in order; default R(0, "")."""

    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder
        self._patches = []

    def _cmd(self, argv, **kw):
        self.calls.append((list(argv), kw))
        r = self.responder
        if callable(r):
            out = r(list(argv), kw)
        elif isinstance(r, list) and r:
            out = r.pop(0)
        else:
            out = None
        return out if out is not None else orch.R(0, "")

    @property
    def argvs(self):
        return [c[0] for c in self.calls]

    def __enter__(self):
        self._patches = [mock.patch.object(orch, "cmd", self._cmd), mock.patch.object(orch, "watched_cmd", self._cmd),
                         mock.patch.object(orch.subprocess, "run", _NoRun),
                         mock.patch.object(orch.subprocess, "Popen", _NoRun)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False
