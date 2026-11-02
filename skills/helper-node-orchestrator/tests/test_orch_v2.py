"""v2 engine generalisation: validators (allow_external, limits, require_digest, stage, paths, ssh, ssh_args), ssh_args at
every ssh site, the config seam (legacy dir, compiled dict, no setup), the generic label family, alerts (command, legacy,
window, no bash), the build context subdir, result sha + wt, selftest phantoms, the wrapper. Nothing touches the
network, ssh or docker; a few tests run local git or bash."""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from orchtest import IMAGES, JOBS, NODES, POLICY, ROOT, FakeCmd, orch, write_config  # noqa: E402
from test_orch_run import J, fake_ctx  # noqa: E402

PIN = IMAGES["alpine"]


def jobs_doc(jobs, images=None):
    return {"images": images or {"alpine": PIN}, "repos": {"app": "/srv/app"}, "jobs": jobs}


def ctr(**kw):
    return dict({"kind": "container", "image": "alpine", "memory": "1g", "timeout": 60, "cmd": "true"}, **kw)


def nodes_doc(nodes, **top):
    return dict(json.loads(json.dumps(POLICY)), nodes=nodes, **top)


def node(**kw):
    return dict({"enabled": True, "os": "linux", "ssh": "h11", "context": "h11"}, **kw)


def tmpdir(tc):
    d = Path(tempfile.mkdtemp(prefix="orchv2-"))
    tc.addCleanup(shutil.rmtree, d, True)
    return d


class ValidateJobs(unittest.TestCase):
    def test_external_needs_allow_external_any_name(self):
        ext = {"kind": "external", "cmd": ["true"], "timeout": 60}
        with self.assertRaisesRegex(orch.ConfigError, 'allow it with "allow_external": true'):
            orch.validate_jobs(jobs_doc({"my-suite": dict(ext)}))
        orch.validate_jobs(jobs_doc({"my-suite": dict(ext, allow_external=True), "e2e-other": dict(ext, allow_external=True)}))
        with self.assertRaisesRegex(orch.ConfigError, "allow_external must be true|false"):
            orch.validate_jobs(jobs_doc({"x": dict(ext, allow_external="yes")}))

    def test_limits_caps(self):
        with self.assertRaisesRegex(orch.ConfigError, r"memory 8g > limits.memory_max 6g"):
            orch.validate_jobs(jobs_doc({"big": ctr(memory="8g")}))
        orch.validate_jobs(jobs_doc({"big": ctr(memory="8g")}), {"memory_max": "8g"})
        with self.assertRaisesRegex(orch.ConfigError, r"timeout 5000 must be 1..3600 s \(limits.timeout_max\)"):
            orch.validate_jobs(jobs_doc({"long": ctr(timeout=5000)}))
        orch.validate_jobs(jobs_doc({"long": ctr(timeout=5000, prep_timeout=4000)}), {"timeout_max": 7200})
        with self.assertRaises(orch.ConfigError):
            orch.validate_jobs(jobs_doc({"long": ctr(timeout=True)}))

    def test_require_digest(self):
        with self.assertRaisesRegex(orch.ConfigError, "docker buildx imagetools inspect alpine:3.20"):
            orch.validate_jobs(jobs_doc({"j": ctr()}, images={"alpine": "alpine:3.20"}))
        orch.validate_jobs(jobs_doc({"j": ctr()}, images={"alpine": "alpine:3.20"}), require_digest=False)

    def test_stage_and_paths(self):
        orch.validate_jobs(jobs_doc({f"j{i}": ctr(stage=s) for i, s in enumerate(orch.STAGES)}))
        with self.assertRaisesRegex(orch.ConfigError, "stage 'deploy' not one of lint, unit"):
            orch.validate_jobs(jobs_doc({"j": ctr(stage="deploy")}))
        b = {"kind": "build", "repo": "app", "source": "git-archive", "timeout": 60}
        orch.validate_jobs(jobs_doc({"a": dict(b, dockerfile="docker/api.Dockerfile", context="api"),
                                     "b": dict(b, dockerfile="Dockerfile", context=".")}))
        for k, v in (("dockerfile", "../Dockerfile"), ("context", "/srv/app"), ("context", "C:/work/shop"),
                     ("context", "api\\sub"), ("dockerfile", "a/../../x")):
            with self.assertRaisesRegex(orch.ConfigError, "must be a path relative to the repo root", msg=(k, v)):
                orch.validate_jobs(jobs_doc({"a": dict(b, **{k: v})}))

    def test_cfg_jobs_follows_nodes_limits(self):
        d = tmpdir(self)
        write_config(d, jobs=dict(JOBS, big=dict(JOBS["t-ok"], memory="8g")))
        nj = json.loads((d / "nodes.json").read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {"ORCH_CONFIG": str(d)}):
            with self.assertRaisesRegex(orch.ConfigError, "limits.memory_max 6g"):
                orch.cfg_jobs()
            nj["limits"] = {"memory_max": "8g"}
            (d / "nodes.json").write_text(json.dumps(nj), encoding="utf-8")
            self.assertIn("big", orch.cfg_jobs()["jobs"])
            nj.pop("limits")  # tightening again re-validates jobs.json although jobs.json itself did not change
            os.utime(d / "nodes.json", ns=(time.time_ns(), time.time_ns() + 10 ** 9))
            (d / "nodes.json").write_text(json.dumps(nj), encoding="utf-8")
            with self.assertRaises(orch.ConfigError):
                orch.cfg_jobs()


class ValidateNodes(unittest.TestCase):
    def test_limits_and_digest_shape(self):
        orch.validate_nodes(nodes_doc({}, limits={"memory_max": "8g", "timeout_max": 7200}, require_digest=False))
        for top, want in (({"limits": []}, "limits must be an object"), ({"limits": {"cpu": 1}}, "unknown key 'cpu'"),
                          ({"limits": {"memory_max": "lots"}}, "limits.memory_max"),
                          ({"limits": {"timeout_max": 0}}, "limits.timeout_max 0"),
                          ({"require_digest": "no"}, "require_digest 'no' must be true|false")):
            with self.assertRaisesRegex(orch.ConfigError, want):
                orch.validate_nodes(nodes_doc({}, **top))

    def test_ssh_field(self):
        for bad in ("-oProxyCommand=calc", "a b", "", "x\ny"):
            with self.assertRaises(orch.ConfigError, msg=bad):
                orch.validate_nodes(nodes_doc({"h11": node(ssh=bad)}))
        with self.assertRaisesRegex(orch.ConfigError, "needs ssh"):
            orch.validate_nodes(nodes_doc({"h11": {"enabled": True, "os": "linux"}}))
        orch.validate_nodes(nodes_doc({"h11": node(ssh=None, ssh_args=["deploy@192.0.2.11"])}))

    def test_ssh_args(self):
        for ok in (["h11"], ["-p", "2222", "deploy@192.0.2.11"], ["-4", "-o", "ConnectTimeout=5", "-l", "deploy", "-i", "k", "h11"],
                   ["-o", "IdentitiesOnly=yes", "-o", "identityfile=~/.ssh/id_h11", "h11"],
                   ["-J", "jump@198.51.100.7", "deploy@192.0.2.11"]):
            orch.validate_nodes(nodes_doc({"h11": node(ssh_args=ok)}))
        for bad, want in ((["-o", "ProxyCommand=calc", "h11"], "not allowed"), (["-oProxyCommand=calc", "h11"], "not allowed"),
                          (["-o", "Include=/tmp/x", "h11"], "not allowed"), (["-o", "LocalCommand=x", "h11"], "not allowed"),
                          (["-o", "StrictHostKeyChecking=no", "h11"], "loosens host-key checks"),
                          (["-o", "UserKnownHostsFile=/dev/null", "h11"], "loosens host-key checks"),
                          (["-o", "Port=2222", "h11"], "changes the destination.*give the port as -p PORT"),
                          (["-o", "ProxyJump=deploy@192.0.2.20", "h11"], "give a jump host as -J"),
                          (["-o", "HostName=192.0.2.20", "h11"], "put the real host in the destination"),
                          (["-o", "HostKeyAlias=h12", "h11"], "not allowed"), (["-o", "ControlPath=/tmp/m", "h11"], "not allowed"),
                          (["-o", "ControlMaster=auto", "h11"], "not allowed"), (["-o", "ForwardAgent=yes", "h11"], "not allowed"),
                          (["-F", "cfg", "h11"], "not allowed"), (["-p", "2222"], "needs a value"), ([], "non-empty"),
                          (["h11", "-p"], "end with the destination"), (["two words"], "without spaces"), ("h11", "non-empty")):
            with self.assertRaisesRegex(orch.ConfigError, want, msg=bad):
                orch.validate_nodes(nodes_doc({"h11": node(ssh_args=bad)}))
        with self.assertRaisesRegex(orch.ConfigError, "remove ssh_args"):
            orch.validate_nodes(nodes_doc({"h11": node(ssh="local", ssh_args=["h11"])}))

    def test_synthetic_config_loads(self):
        self.assertEqual(orch.cfg_nodes()["nodes"]["n3"]["ssh_args"], ["-p", "2222", "deploy@192.0.2.13"])
        self.assertEqual(orch.cfg_jobs()["jobs"]["app-test"]["stage"], "unit")


SA = ["-p", "2222", "deploy@192.0.2.13"]


class SshArgsEverySite(unittest.TestCase):
    LX = orch.Node("n3", {"os": "linux", "ssh": "n3", "ssh_args": SA, "transport": "ssh-docker",
                          "docker_host": "unix:///var/run/docker.sock"})
    WN = orch.Node("n9", {"os": "windows", "ssh": "n9", "ssh_args": ["-p", "2200", "admin@192.0.2.19"], "context": "n9"})

    def test_docker_sh_watchdog(self):
        self.assertEqual(self.LX.docker_argv(["ps", "-a"]), orch.SSH + SA + ["docker -H unix:///var/run/docker.sock ps -a"])
        self.assertEqual(self.LX.sh_argv(["1"]), orch.SSH + SA + ["sh -s -- 1"])
        argv, _ = orch.Watchdog(self.LX, {}, "j1", lambda r: None, iters=5)._argv()
        self.assertEqual(argv, orch.SSH + SA + ["sh -s -- 5"])

    def test_shell_probe_and_windows(self):
        with FakeCmd(lambda argv, kw: orch.R(0, "")) as fc:
            orch.probe(self.LX)
            self.WN.shell("'REACH'")
        self.assertEqual(fc.argvs[0][:len(orch.SSH) + 3], orch.SSH + SA)
        w = fc.argvs[1]
        self.assertEqual(w[:-1], orch.SSH + ["-p", "2200", "admin@192.0.2.19"])
        self.assertTrue(w[-1].startswith("powershell -NoProfile"))

    def test_alias_fallback_and_no_bare_ssh_sites(self):
        n = orch.Node("h11", {"os": "linux", "ssh": "h11"})
        self.assertEqual(n.sh_argv(), orch.SSH + ["h11", "sh -s -- "])
        src = Path(orch.__file__).read_text(encoding="utf-8")
        self.assertNotIn("SSH + [self.ssh", src)
        self.assertEqual(src.count("SSH + self.ssh_args"), 3)  # docker_argv (ssh-docker), sh_argv, windows shell


class ConfigSeam(unittest.TestCase):
    def compiled(self, **over):
        nodes = nodes_doc({"n1": node(ssh="n1", opt_in=["t-ok"])})
        d = {"nodes": nodes, "jobs": jobs_doc({"t-ok": dict(JOBS["t-ok"])}), "protected": {"nodes": {}, "alert": {}},
             "_label": "project /srv/app"}
        d.update(over)
        return d

    def test_compiled_dict_flows_through_validators(self):
        src = self.compiled()
        with mock.patch.object(orch, "config_source", lambda: src):
            self.assertEqual(list(orch.cfg_nodes()["nodes"]), ["n1"])
            self.assertIn("t-ok", orch.cfg_jobs()["jobs"])
            self.assertEqual(orch.cfg_prot()["alert"], {})
            self.assertEqual(orch.config_line(), "config: project /srv/app")
            with self.assertRaisesRegex(orch.ConfigError, "compiled in memory"):
                orch.cfg_path("nodes.json")
            src["jobs"]["jobs"]["t-ok"]["memory"] = "9g"  # same dict object, new content: validated again
            with self.assertRaisesRegex(orch.ConfigError, "memory 9g"):
                orch.cfg_jobs()
        with mock.patch.object(orch, "config_source", lambda: self.compiled(jobs=None)):
            with self.assertRaisesRegex(orch.ConfigError, r"project /srv/app \(jobs.json\) is missing"):
                orch.cfg_jobs()

    def test_missing_file_names_path_and_fix(self):
        d = tmpdir(self)
        write_config(d)
        (d / "jobs.json").unlink()
        with mock.patch.dict(os.environ, {"ORCH_CONFIG": str(d)}):
            with self.assertRaisesRegex(orch.ConfigError, "jobs.json is missing or not a JSON object: fix it, or point ORCH_CONFIG"):
                orch.cfg_jobs()

    def _main(self, argv):
        o, e = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "ORCH_CONFIG"}
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(o), redirect_stderr(e), \
                mock.patch.object(orch.atexit, "register"), mock.patch.object(orch.signal, "signal"):
            rc = orch.main(argv)
        return rc, o.getvalue(), e.getvalue()

    def test_no_setup_exit2_with_next(self):
        for argv in (["nodes"], ["plan", "t-ok"], ["run", "t-ok"], ["ci"], ["reconcile"]):
            rc, out, err = self._main(argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("orch: no orch setup here: run orch init", err)
            self.assertEqual(json.loads(out.strip().splitlines()[-1]),
                             {"result": "usage", "exit": 2, "error": "no orch setup here: run orch init", "next": "run orch init"})

    def test_no_setup_real_process(self):
        env = {k: v for k, v in os.environ.items() if k != "ORCH_CONFIG"}
        p = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "orch.py"), "nodes"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("no orch setup here: run orch init", p.stderr)

    def test_exit_7_reserved(self):
        self.assertEqual(orch.GATE, 7)
        self.assertIn("7 deploy gate not met", orch.__doc__)
        self.assertNotIn(7, orch.EXIT.values())


class LabelFamily(unittest.TestCase):
    def test_generic_family_rule(self):
        self.assertEqual((orch.DEFAULT_LABEL, orch.DEFAULT_PREFIX), ("io.orch.agent", "agentx-"))
        for p, l in (("agentx-", "io.orch.agent"), ("agentt-", "io.orchtest.agent"), ("agentx-", "com.example.agent"),
                     ("agentx-", "io.orch2.agent")):
            orch.ns_check(p, l)
        for l in ("io.orch.agent2", "io.orch.owner", "io.orch.x"):
            with self.assertRaisesRegex(ValueError, "shares the default family io.orch. of io.orch.agent", msg=l):
                orch.ns_check("agentx-", l)


def alert_rec(acfg, call=True, env=None, **patches):
    """Run orch.alert with acfg; returns (alerts.log record, Popen calls)."""
    calls = []

    class In(io.BytesIO):
        def close(self):
            self.sent = self.getvalue()
            super().close()

    class P:
        def __init__(self, argv, **kw):
            self.stdin = In() if kw.get("stdin") == subprocess.PIPE else None
            calls.append((argv, kw, self))
    ps = [mock.patch.dict(os.environ, env or {}), mock.patch.object(orch.subprocess, "Popen", P),
          mock.patch.object(orch, "audit", lambda *a, **k: None), mock.patch.object(orch, "say")]
    ps += [mock.patch.object(*x) for x in patches.values()]
    for p in ps:
        p.start()
    try:
        orch.alert("n1", ["watchdog r1-guard: iowait 45% > 30%"], call=call, acfg=acfg)
    finally:
        for p in reversed(ps):
            p.stop()
    return json.loads((orch.HOME / "alerts.log").read_text(encoding="utf-8").splitlines()[-1]), calls


BASH = (orch.shutil, "which", lambda n: "/bin/bash")


class Alerts(unittest.TestCase):
    def test_dryrun_new_and_legacy(self):
        rec, calls = alert_rec({"command": "notify --to ops"}, env={"ORCH_ALERT_DRYRUN": "1"})
        self.assertEqual(rec["call"], "DRYRUN would run: bash -c 'notify --to ops' (message on stdin and in $MSG)")
        rec, _ = alert_rec({"script": "page.sh", "numbers": ["ops-1"], "window_start_h": 6, "window_end_h": 21},
                           env={"ORCH_ALERT_DRYRUN": "1"})
        self.assertTrue(rec["call"].startswith("DRYRUN would run: MSG='Helper n1: ") and rec["call"].endswith("bash page.sh ops-1"))
        self.assertEqual(calls, [])

    def test_no_command_and_bad_command(self):
        for acfg in ({"command": None}, {}, {"script": ""}):
            self.assertEqual(alert_rec(acfg, env={"ORCH_ALERT_DRYRUN": "0"})[0]["call"],
                             "none: no alert command configured (alert line only)")
        for bad in ("", "  ", 5, ["x"]):
            self.assertIn("must be a non-empty string or null", alert_rec({"command": bad})[0]["call"])
        self.assertEqual(alert_rec({"command": "x"}, call=False)[0]["call"],
                         "none (fingerprint diff: cordon until the user uncordons, alert line only)")

    def test_window(self):
        for h, s, e, want in ((6, 6, 21, True), (21, 6, 21, False), (23, 22, 6, True), (5, 22, 6, True),
                              (12, 22, 6, False), (0, 0, 24, True), (23, 0, 24, True)):
            self.assertEqual(orch._in_window(h, s, e), want, (h, s, e))
        rec, calls = alert_rec({"command": "notify", "window_start_h": 6, "window_end_h": 21}, env={"ORCH_ALERT_DRYRUN": "0"},
                               w=(orch, "_in_window", lambda *a: False), b=BASH)
        self.assertEqual(rec["call"], "none: outside the alert window 06:00-21:00 (alert line only)")
        self.assertEqual(calls, [])

    def test_no_bash_skipped_with_reason(self):
        rec, calls = alert_rec({"command": "notify"}, env={"ORCH_ALERT_DRYRUN": "0"}, b=(orch.shutil, "which", lambda n: None))
        self.assertEqual(rec["call"], "none: bash not found on PATH, so the alert command was skipped (put bash on PATH)")
        self.assertEqual(calls, [])

    def test_command_detached_with_stdin_and_msg(self):
        rec, calls = alert_rec({"command": "notify"}, env={"ORCH_ALERT_DRYRUN": "0"}, b=BASH)
        (argv, kw, p), = calls
        self.assertEqual(argv, ["/bin/bash", "-c", "notify"])
        self.assertEqual(kw["env"]["MSG"], rec["msg"])
        self.assertEqual(p.stdin.sent.decode(), rec["msg"] + "\n")
        self.assertEqual(rec["call"], "alert command started (bash -c, detached)")
        rec, calls = alert_rec({"script": "page.sh", "numbers": ["ops-1"]}, env={"ORCH_ALERT_DRYRUN": "0"}, b=BASH)
        self.assertEqual(calls[0][0], ["/bin/bash", "page.sh", "ops-1"])
        self.assertEqual(calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertTrue(rec["call"].startswith("placed via the alert script"))

    def test_r1_guard_passes_acfg(self):
        job = SimpleNamespace(name="app-test", get=lambda k, d=None: d)
        with mock.patch.dict(os.environ, {"ORCH_ALERT_DRYRUN": "1"}), mock.patch.object(orch, "say"):
            os.environ.pop("ORCH_SELFTEST", None)
            try:
                orch.r1_guard_alert("n1", job, "x", acfg={"command": "notify"})
            finally:
                os.environ["ORCH_SELFTEST"] = "1"
        rec = json.loads((orch.HOME / "alerts.log").read_text(encoding="utf-8").splitlines()[-1])
        self.assertTrue(rec["call"].startswith("DRYRUN would run: bash -c notify"))

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_real_bash_gets_stdin_and_msg(self):
        d = tmpdir(self)
        outs = {"ALERT_T_IN": (d / "in.txt").as_posix(), "ALERT_T_ENV": (d / "env.txt").as_posix()}
        cmd = 'cat > "$ALERT_T_IN"; printf %s "$MSG" > "$ALERT_T_ENV.tmp" && mv "$ALERT_T_ENV.tmp" "$ALERT_T_ENV"'
        procs, real = [], subprocess.Popen

        def keep(*a, **k):
            procs.append(real(*a, **k))
            return procs[-1]
        with mock.patch.dict(os.environ, dict(outs, ORCH_ALERT_DRYRUN="0")), mock.patch.object(orch, "say"), \
                mock.patch.object(orch.subprocess, "Popen", keep):
            orch.alert("n1", ["watchdog r1-guard: psi 30 > 25"], call=True, acfg={"command": cmd})
        self.assertEqual(procs[0].wait(timeout=30), 0)
        rec = json.loads((orch.HOME / "alerts.log").read_text(encoding="utf-8").splitlines()[-1])
        for _ in range(100):
            if (d / "env.txt").exists():
                break
            time.sleep(0.1)
        self.assertEqual((d / "env.txt").read_text(encoding="utf-8"), rec["msg"])
        self.assertEqual((d / "in.txt").read_text(encoding="utf-8").strip(), rec["msg"])


def src_tar(files: dict) -> Path:
    p = Path(tempfile.mkdtemp(prefix="orchv2-src-")) / "s.tar"
    with tarfile.open(p, "w") as t:
        for n, b in files.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
    return p


class BuildContext(unittest.TestCase):
    FILES = {"src/api/Dockerfile": b"FROM scratch\nCOPY app.py /\n", "src/api/app.py": b"print(1)\n",
             "src/docker/api.Dockerfile": b"FROM scratch AS out\n", "src/web/index.html": b"<p>",
             "src/README.md": b"readme"}

    def build(self, **jc):
        ctx = fake_ctx(job="app-image", **jc)
        tar = src_tar(self.FILES)
        self.addCleanup(shutil.rmtree, tar.parent, True)
        ctx.opts._src = {"tar": str(tar), "ref": "abc1234", "files": list(self.FILES), "sha": "a" * 40, "wt": False}
        sent = {}

        def resp(argv, kw):
            if argv[:3] == ["docker", "buildx", "build"]:
                sent["argv"] = argv
                with tarfile.open(fileobj=io.BytesIO(kw["stdin"].read())) as t:
                    sent["files"] = {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
            return None
        with FakeCmd(resp), mock.patch.object(orch, "ensure_builder", lambda *a, **k: None):
            cls = orch.run_build_job(ctx)
        return cls, sent

    def test_context_subdir(self):
        cls, sent = self.build(dockerfile="api/Dockerfile", context="api")
        self.assertEqual(cls, "pass")
        self.assertEqual(sorted(sent["files"]), [".agentx/Dockerfile", "Dockerfile", "app.py"])
        self.assertTrue(sent["files"][".agentx/Dockerfile"].startswith(self.FILES["src/api/Dockerfile"]))
        self.assertIn(b"ARG AGENTX_REF", sent["files"][".agentx/Dockerfile"])
        self.assertIn("AGENTX_REF=abc1234", sent["argv"])

    def test_dockerfile_outside_context(self):
        cls, sent = self.build(dockerfile="./docker/api.Dockerfile", context="api/")
        self.assertEqual(sorted(sent["files"]), [".agentx/Dockerfile", "Dockerfile", "app.py"])
        self.assertTrue(sent["files"][".agentx/Dockerfile"].startswith(b"FROM scratch AS out"))

    def test_default_context_is_repo_root(self):
        for c in (None, ".", ""):
            _, sent = self.build(dockerfile="api/Dockerfile", context=c)
            self.assertEqual(sorted(sent["files"]), [".agentx/Dockerfile", "README.md", "api/Dockerfile", "api/app.py",
                                                     "docker/api.Dockerfile", "web/index.html"])

    def test_bad_dockerfile_or_context(self):
        with self.assertRaisesRegex(orch.OrchError, "Dockerfile nope/Dockerfile is not in the source archive of abc1234"):
            self.build(dockerfile="nope/Dockerfile", context="api")
        with self.assertRaisesRegex(orch.OrchError, "build context nope/ has no files in abc1234"):
            self.build(dockerfile="api/Dockerfile", context="nope")


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *a],
                          capture_output=True, text=True, timeout=60, check=True).stdout.strip()


class ShaWt(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_git_source_full_sha_and_wt(self):
        d = tmpdir(self)
        repo = d / "shop"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-q", "-m", "c1")
        head = git(repo, "rev-parse", "HEAD")
        with mock.patch.object(orch, "say"):
            info = orch.git_source(str(repo), None, False, d / "s1.tar", "j1")
            self.assertEqual((info["sha"], info["ref"], info["wt"]), (head, head[:7], False))
            self.assertEqual(orch.git_source(str(repo), head[:9], False, d / "s2.tar", "j1")["sha"], head)
            (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
            info = orch.git_source(str(repo), None, True, d / "s3.tar", "j1")
            self.assertEqual((info["sha"], info["wt"], info["wt_added"]), (head, True, ["new.txt"]))
            self.assertTrue(info["ref"].startswith(head[:7] + "+wt."))
            with self.assertRaisesRegex(orch.OrchError, "bad ref nope in"):
                orch.git_source(str(repo), "nope", False, d / "s4.tar", "j1")
            with self.assertRaisesRegex(orch.OrchError, "cannot start with '-'"):
                orch.git_source(str(repo), "--all", False, d / "s5.tar", "j1")

    def test_meta_and_result_json(self):
        ctx = fake_ctx()
        self.assertEqual((ctx.meta["sha"], ctx.meta["wt"]), (None, False))
        opts = SimpleNamespace(lease=300, heartbeat=60, _src={"sha": "b" * 40, "wt": True}, _sel_s=0.0)
        with FakeCmd():
            c2 = orch.Ctx(ctx.job, ctx.node, {}, J, {"jobid": J}, opts)
        out = orch.result_json(dict(c2.meta, result="pass", exit=0))
        self.assertEqual((out["sha"], out["wt"]), ("b" * 40, True))

    def test_not_admitted_line_carries_sha(self):
        ctx = fake_ctx()
        a = SimpleNamespace(node=None, no_retry=True, _sel_s=0, _src={"sha": "c" * 40, "wt": False})
        buf = io.StringIO()
        with mock.patch.object(orch, "select_node", lambda *x, **k: (None, None, None, "n1: busy")), \
                mock.patch.object(orch, "say"), mock.patch("sys.stdout", buf):
            rc = orch._run_attempts(ctx.job, a)
        d = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual((rc, d["result"], d["sha"], d["wt"]), (orch.NOADMIT, "not-admitted", "c" * 40, False))


class Selftest(unittest.TestCase):
    def test_phantoms_from_any_linux_node(self):
        extra, ph, skip = orch.phantom_nodes(NODES)
        self.assertIsNone(skip)
        self.assertEqual(ph, [("hx", "unreachable"), ("hd", "docker down")])
        hd = extra["hd"]
        self.assertEqual((hd["ssh"], hd["transport"], hd["docker_host"], hd["context"], hd["opt_in"]),
                         ("n1", "ssh-docker", orch.DEAD_SOCK, None, ["t-ok"]))
        self.assertTrue(orch.DEAD_SOCK.startswith("unix:///run/user/0/"))
        self.assertEqual(extra["hx"]["host"], "192.0.2.1")
        orch.validate_nodes(nodes_doc(dict(NODES, **extra)))
        extra, ph, skip = orch.phantom_nodes({"w": dict(NODES["n2"])})
        self.assertEqual((list(extra), skip), (["hx"], "skipped: needs an enabled Linux node in config"))
        extra, _, _ = orch.phantom_nodes({"l": {"enabled": True, "os": "linux", "ssh": "local", "context": "c1"}})
        self.assertEqual((extra["hd"]["transport"] if "transport" in extra["hd"] else "context", extra["hd"]["context"]),
                         ("context", "c1"))
        extra, _, _ = orch.phantom_nodes({"a": dict(NODES["n3"], enabled=False), "b": dict(NODES["n3"])})
        self.assertEqual(extra["hd"]["ssh_args"], NODES["n3"]["ssh_args"])

    def test_project_jobs(self):
        self.assertEqual(orch.project_jobs(), ["app-test", "app-image"])

    def test_unit_checks_leave_no_temp_dir_when_a_check_raises(self):  # v2.1.2: one orch-st-* dir leaked per run
        import gc
        tmp = Path(tempfile.mkdtemp(prefix="orchv2-tmp-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(tempfile, "tempdir", str(tmp)), mock.patch.object(orch, "scan_results",
                                                                                 side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()), mock.patch.object(orch, "say"):
            try:
                orch.cmd_selftest(SimpleNamespace(unit_only=True, quick=False, node=None, out=None))
            except RuntimeError:
                pass
        gc.collect()
        self.assertEqual([p.name for p in tmp.iterdir()], [])

    def test_unit_only_passes_on_synthetic_config(self):
        out = orch.HOME / "st-unit.md"
        o = io.StringIO()
        before = {k: os.environ.get(k) for k in ("ORCH_ALERT_DRYRUN", "ORCH_SELFTEST")}
        with FakeCmd(), redirect_stdout(o), mock.patch.object(orch, "say"):
            rc = orch.cmd_selftest(SimpleNamespace(unit_only=True, quick=False, node=None, out=str(out)))
        self.assertEqual(rc, 0, o.getvalue())
        self.assertNotIn("[FAIL]", o.getvalue())
        self.assertIn("passed", out.read_text(encoding="utf-8"))
        self.assertEqual({k: os.environ.get(k) for k in before}, before)


class Wrapper(unittest.TestCase):
    W = ROOT / "scripts" / "orch"

    def test_no_hardcoded_interpreter(self):
        s = self.W.read_text(encoding="utf-8")
        self.assertIn('for py in "${ORCH_PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10 '
                      'python py; do', s)
        self.assertNotRegex(s, r"(?i)/c/|[a-z]:[/\\]|\.exe")
        self.assertIn("sys.version_info >= (3, 10)", s)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_runs_and_falls_through(self):
        env = dict(os.environ, ORCH_PYTHON=(Path(tempfile.gettempdir()) / "no-such-python").as_posix())
        env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
        p = subprocess.run([shutil.which("bash"), self.W.as_posix(), "status", "--local"], env=env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("live jobs:", p.stdout)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_versioned_python_after_old_python3(self):  # EL9: python3 is 3.9, python3.12 sits beside it
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d) / "python3", Path(d) / "python3.14"
            old.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8", newline="\n")
            new.write_text(f'#!/usr/bin/env bash\necho picked-3.14 >&2\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
                           encoding="utf-8", newline="\n")
            for f in (old, new):
                f.chmod(0o755)
            env = dict(os.environ, PATH=os.pathsep.join([d, os.environ.get("PATH", "")]))
            env.pop("ORCH_PYTHON", None)
            p = subprocess.run([shutil.which("bash"), self.W.as_posix(), "status", "--local"], env=env,
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("picked-3.14", p.stderr)
        self.assertIn("live jobs:", p.stdout)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_no_python_exit2(self):  # empty PATH: no interpreter can be found (dirname is missing too: noise only)
        p = subprocess.run([shutil.which("bash"), self.W.as_posix(), "status"], env={"PATH": "/nonexistent"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2)
        self.assertIn("no Python 3.10+ found", p.stderr)


if __name__ == "__main__":
    unittest.main()
