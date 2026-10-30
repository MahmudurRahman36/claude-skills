"""F3: watchdog, source scan, classify, phase markers, run/build/cleanup paths. Nothing real runs (FakeCmd)."""
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchtest import FakeCmd, orch  # noqa: E402

WIN = orch.Node("hw", {"os": "windows", "ssh": "local", "context": "hw", "opt_in": ["t-ok"]})
LOC = orch.Node("hl", {"os": "linux", "ssh": "local", "context": "hl"})
J = "1029000000abcd"


class _Proc:
    def __init__(self, lines, boom=None):
        self.stdout = _Out(lines, boom)
        self.stdin = io.BytesIO()

    def poll(self):
        return 0

    def kill(self):
        pass


class _Out:
    def __init__(self, lines, boom):
        self.lines, self.boom = lines, boom

    def __iter__(self):
        yield from self.lines
        if self.boom:
            raise self.boom


def run_wd(lines, boom=None, stop_first=False):
    dead = []
    wd = orch.Watchdog(LOC, {}, "j1", lambda r: None, iters=99, on_dead=dead.append)
    if stop_first:
        wd.stop_ev.set()
    with mock.patch.object(orch.subprocess, "Popen", lambda *a, **k: _Proc(lines, boom)), \
            mock.patch.object(orch, "audit", lambda *a, **k: None):
        wd.run()
    return wd, dead


class WatchdogF31(unittest.TestCase):
    def test_malformed_line_skipped_then_eof_is_dead(self):
        wd, dead = run_wd([b"WD na\n", b"WD na 3 x\n", b"WD na 5\n", b"noise\n"])
        self.assertEqual(len(wd.samples), 1)
        self.assertTrue(wd.dead.startswith("stream ended early"))
        self.assertEqual(dead, [wd.dead])

    def test_exception_is_dead(self):
        wd, dead = run_wd([b"WD na 5\n"], boom=OSError("ssh reset"))
        self.assertIn("ssh reset", wd.dead)
        self.assertEqual(len(dead), 1)

    def test_eof_after_stop_is_not_dead(self):
        wd, dead = run_wd([b"WD na 5\n"], stop_first=True)
        self.assertIsNone(wd.dead)
        self.assertEqual(dead, [])

    def test_start_failure_is_dead(self):
        dead = []
        wd = orch.Watchdog(LOC, {}, "j1", lambda r: None, on_dead=dead.append)

        def bad(*a, **k):
            raise OSError("no ssh")
        with mock.patch.object(orch.subprocess, "Popen", bad), mock.patch.object(orch, "audit", lambda *a, **k: None):
            wd.run()
        self.assertIn("no ssh", wd.dead)

    def test_iters_in_argv(self):
        argv, _ = orch.Watchdog(LOC, {}, "j1", lambda r: None, iters=1234)._argv()
        self.assertEqual(argv[-1], "1234")
        argv, _ = orch.Watchdog(WIN, {}, "j1", lambda r: None, iters=55)._argv()
        self.assertEqual(argv[-1], "55")

    def test_stream_attach_kills_on_wd_lost(self):
        ctx = fake_ctx()
        ctx.wd_lost = "stream ended early"

        class P:
            returncode = 137
            stdout = iter([])

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 137
        with FakeCmd() as fc, mock.patch.object(orch.subprocess, "Popen", lambda *a, **k: P()), \
                mock.patch.object(orch, "audit", lambda *a, **k: None):
            orch.stream_attach(ctx, "agentt-x-job", lambda: orch.now() + 99)
        self.assertTrue(any(a[-2:] == ["kill", "agentt-x-job"] for a in fc.argvs))
        self.assertTrue(ctx.orch_killed)
        self.assertEqual(orch.classify_ctx(ctx, {"ExitCode": 137}, 137), "infra")


def mktar(files: dict) -> Path:
    p = Path(tempfile.mkdtemp(prefix="orchtest-tar-")) / "s.tar"
    with tarfile.open(p, "w") as t:
        for n, b in files.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
    return p


class SourceScanF32(unittest.TestCase):
    def test_tokens(self):
        p = mktar({"src/a.cs": b"k = AKIA" + b"A" * 16, "src/b.yml": b"t: ghp_" + b"a" * 36,
                   "src/c.txt": b"github_pat_" + b"a" * 30, "src/d.js": b"xoxb-" + b"1" * 12,
                   "src/e.cfg": b"password=hunter22 user=x", "src/f.md": b"nothing here"})
        hits = orch.scan_source_content(p, [])
        self.assertEqual(sorted(h.split(":")[0] for h in hits), ["src/a.cs", "src/b.yml", "src/c.txt", "src/d.js"])

    def test_large_file_chunked(self):
        big = b"x" * (orch.SRC_CHUNK - 3) + b"AKIA" + b"B" * 16 + b"\n" + b"y" * 100
        self.assertEqual(len(orch.scan_source_content(mktar({"src/big.log": big}), [])), 1)
        key = b"z" * (9 << 20) + b"-----BEGIN RSA PRIVATE KEY-----\n"
        self.assertIn("private key", orch.scan_source_content(mktar({"src/k.txt": key}), [])[0])

    def test_allow_case_insensitive(self):
        p = mktar({"src/Docs/a.txt": b"AKIA" + b"C" * 16})
        self.assertEqual(orch.scan_source_content(p, [r"docs/"]), [])
        self.assertTrue(orch.secret_allowed("SRC/.ENV.EXAMPLE", orch.SECRET_ALLOW))


class OomF33(unittest.TestCase):
    def test_word_bounded(self):
        for t in ("not enough room for layer", "zoom", "bloom filter", "roomy"):
            self.assertFalse(orch.is_build_oom(t), t)
        for t in ("OOMKilled", "out of memory", "exit code: 137", "OOM", "process oom"):
            self.assertTrue(orch.is_build_oom(t), t)


class ClassifyF34(unittest.TestCase):
    def test_orch_kills(self):
        c = orch.classify
        self.assertEqual(c({"ExitCode": 137}, 137, "Killed process 9", {"orch_killed": True}), "infra")
        self.assertEqual(c({"ExitCode": 137, "OOMKilled": True}, 137, "", {"orch_killed": True}), "infra")
        self.assertEqual(c({"ExitCode": 137}, 137, "", {"orch_killed": True, "timed_out": True}), "timeout")
        self.assertEqual(c({"ExitCode": 137}, 137, "", {"orch_killed": True, "guard": "x"}), "r1-guard")
        self.assertEqual(c({"ExitCode": 1}, 1, "", {"wd_lost": "eof"}), "infra")

    def test_137_needs_oom_evidence(self):
        c = orch.classify
        self.assertEqual(c({"ExitCode": 137}, 137, "Killed: test_kill_switch passed"), "test-fail")
        self.assertEqual(c({"ExitCode": 137}, 137, "Out of memory: Killed process 123 (dotnet)"), "oom")
        self.assertEqual(c({"ExitCode": 137}, 137, "memory cgroup oom-kill: task=x"), "oom")


def fake_ctx(job="t-ok", node=WIN, **jc):
    j = orch.Job(job, [])
    j.c.update(jc)
    opts = SimpleNamespace(lease=300, heartbeat=60, _src=None, _sel_s=0.0)
    with FakeCmd():
        ctx = orch.Ctx(j, node, {"caches": []}, J, {"jobid": J}, opts)
    ctx.hb.beat = lambda **k: None
    return ctx


def run_container(lines, state='{"ExitCode": 0}', disconnect_ok=True, **jc):
    ctx = fake_ctx(**dict({"prep": "echo p", "cmd": "echo t", "fail_patterns": ["^FAIL"]}, **jc))

    def attach(c, cname, deadline, on_line=None):
        for s in lines(c):
            on_line(s)
            import time
            time.sleep(0.05)
        c.tail = "".join(lines(c))
        return json.loads(state).get("ExitCode", 0)

    def resp(argv, kw):
        if "inspect" in argv and "{{json .State}}" in argv:
            return orch.R(0, state)
        if "disconnect" in argv:
            return orch.R(0 if disconnect_ok else 1, "", "" if disconnect_ok else "daemon gone")
        return None
    with FakeCmd(resp) as fc, mock.patch.object(orch, "stream_attach", attach), \
            mock.patch.object(orch, "ensure_image", lambda *a, **k: orch.R(0)), \
            mock.patch.object(orch, "alloc_subnet", lambda *a, **k: "10.252.20.16/28"), \
            mock.patch.object(orch, "collect", lambda *a, **k: None):
        cls = orch.run_container_job(ctx)
    return cls, ctx, fc


class MarkersF35(unittest.TestCase):
    def test_script_nonce(self):
        s = orch.compose_script("prep", "run", nonce=True)
        self.assertTrue(s.startswith(f"_xn=$(cat {orch.NONCE_FILE} 2>/dev/null); rm -f {orch.NONCE_FILE}\n( unset _xn; prep"))
        self.assertIn("echo AGENTX_PREP_DONE $_xn", s)
        self.assertIn("( unset _xn; run", s)
        self.assertNotIn("_xn", orch.compose_script("prep", "run"))

    def test_real_markers_pass(self):
        ok = lambda c: [f"AGENTX_PREP_DONE {c.nonce}\n", f"AGENTX_NET_NONE {c.nonce} interfaces: lo\n", "t\n"]
        cls, ctx, fc = run_container(ok)
        self.assertEqual(cls, "pass")
        create = next(a for a in fc.argvs if a[3] == "create")
        self.assertNotIn(f"AGENTX_NONCE={ctx.nonce}", create)  # O2-4: nonce travels as a file, not env

    def test_forged_markers_ignored(self):
        forged = lambda c: ["AGENTX_PREP_DONE\n", "AGENTX_NET_NONE interfaces: lo\n", "AGENTX_PREP_DONE deadbeef\n"]
        cls, ctx, fc = run_container(forged)
        self.assertEqual(cls, "infra")
        self.assertFalse(any("disconnect" in a for a in fc.argvs))

    def test_net_none_before_prep_or_failed_disconnect(self):
        early = lambda c: [f"AGENTX_NET_NONE {c.nonce} interfaces: lo\n", f"AGENTX_PREP_DONE {c.nonce}\n"]
        self.assertEqual(run_container(early)[0], "infra")
        ok = lambda c: [f"AGENTX_PREP_DONE {c.nonce}\n", f"AGENTX_NET_NONE {c.nonce} interfaces: lo\n"]
        cls, ctx, fc = run_container(ok, disconnect_ok=False)
        self.assertEqual(cls, "infra")
        self.assertTrue(ctx.orch_killed)

    def test_failures_extracted(self):
        lines = lambda c: [f"AGENTX_PREP_DONE {c.nonce}\n", f"AGENTX_NET_NONE {c.nonce} x\n",
                           "\x1b[31mFAIL\x1b[0m: test_a \x07broke\n", "ok line\n"] + [f"FAIL {i} " + "x" * 300 + "\n" for i in range(30)]
        cls, ctx, _ = run_container(lines, state='{"ExitCode": 1}')
        self.assertEqual(cls, "test-fail")
        f = ctx.meta["failures"]
        self.assertEqual(len(f), 20)
        self.assertEqual(f[0], "FAIL: test_a broke")
        self.assertTrue(all(len(x) <= 200 for x in f))


class CreatesF36(unittest.TestCase):
    def test_cache_volume_failure(self):
        ctx = fake_ctx(prep="echo p", cmd="echo t", cache={"name": "npm", "path": "/c"})

        def resp(argv, kw):
            return orch.R(1, "", "no space") if "volume" in argv and "create" in argv else None
        with FakeCmd(resp), mock.patch.object(orch, "ensure_image", lambda *a, **k: orch.R(0)), \
                mock.patch.object(orch, "alloc_subnet", lambda *a, **k: "10.252.20.16/28"):
            with self.assertRaises(orch.OrchError) as e:
                orch.run_container_job(ctx)
        self.assertEqual(e.exception.cls, "infra")
        led = (orch.HOME / "ledger.jsonl")
        txt = led.read_text(encoding="utf-8") if led.exists() else ""
        self.assertNotIn('"created", "node": "hw", "kind": "volume"', txt.replace("'", '"'))

    def test_builder_start_failure(self):
        n = orch.Node("hb", {"os": "windows", "ssh": "local", "context": "hb", "opt_in": ["app-image"]})
        jobs = {"jobs": {"app-image": {"kind": "build"}}}

        def resp(argv, kw):
            if "ps" in argv:
                return orch.R(0, "exited\t1\n")
            if "start" in argv:
                return orch.R(1, "", "cannot start")
            return None
        with FakeCmd(resp), mock.patch.object(orch, "cfg_jobs", lambda: jobs):
            with self.assertRaises(orch.OrchError) as e:
                orch.ensure_builder(n, {}, J)
        self.assertIn("builder start failed", e.exception.msg)


class BuilderOptInF37(unittest.TestCase):
    def test_refuse_without_build_job(self):
        with FakeCmd() as fc:
            with self.assertRaises(orch.OrchError) as e:
                orch.ensure_builder(WIN, {}, J)
        self.assertIn("no build job in opt_in", e.exception.msg)
        self.assertEqual(fc.calls, [])


class WinExtractF38(unittest.TestCase):
    def test_windows_names(self):
        p = mktar({"r/ok.txt": b"1", "r/a:b.txt": b"2", "r/CON": b"3", "r/nul.txt": b"4", "r/com1.log.gz": b"5",
                   "r/x.": b"6", "r/y ": b"7", "r/Dup.txt": b"8", "r/dup.TXT": b"9", "r/console.txt": b"10"})
        d = Path(tempfile.mkdtemp(prefix="orchtest-x-"))
        with mock.patch.object(orch, "IS_WIN", True), mock.patch.object(orch.Path, "resolve", lambda s: s.absolute()):
            n, refused = orch.safe_extract(p, d)
        names = sorted(r.split(" ")[0] for r in refused)
        self.assertEqual(names, ["r/CON", "r/a:b.txt", "r/com1.log.gz", "r/dup.TXT", "r/nul.txt", "r/x.", "r/y"])
        self.assertEqual(n, 3)


class ResultJsonF310(unittest.TestCase):
    def test_keys_and_order(self):
        out = orch.result_json({"jobid": J, "result": "test-fail", "exit": 1, "extra": 1, "reason": "x"})
        self.assertEqual(list(out), ["result", "exit", "job", "jobid", "node", "wall_s", "wait_s", "overhead_s",
                                     "failures", "reason", "results", "log", "sha", "wt"])
        self.assertEqual((out["failures"], out["sha"], out["wt"]), ([], None, False))

    def test_reason_one_line(self):
        ctx = fake_ctx()
        ctx.meta["error"] = "line1\nline2"
        self.assertEqual(orch.result_reason("infra", ctx), "line1 line2")
        ctx.meta.pop("error")
        ctx.meta["failures"] = ["FAIL a"]
        self.assertEqual(orch.result_reason("test-fail", ctx), "tests failed: FAIL a")
        self.assertIsNone(orch.result_reason("pass", ctx))
        for c in ("oom", "timeout", "r1-guard"):
            self.assertTrue(orch.result_reason(c, ctx))

    def test_run_attempts_line(self):
        ctx = fake_ctx()
        ctx.meta.update(result="pass", exit=0, wall_s=5, wait_s=1, overhead_s=2, failures=[])
        a = SimpleNamespace(node=None, no_retry=True, _sel_s=0)
        buf = io.StringIO()
        with mock.patch.object(orch, "select_node", lambda *x, **k: (WIN, {}, {"jobid": J}, {})), \
                mock.patch.object(orch, "execute", lambda *x, **k: ("pass", ctx)), mock.patch("sys.stdout", buf):
            rc = orch._run_attempts(ctx.job, a)
        last = buf.getvalue().strip().splitlines()[-1]
        d = json.loads(last)
        self.assertEqual((rc, d["result"], d["results"], d["log"]), (0, "pass", str(ctx.dir), str(ctx.log)))


class VerbosityF311(unittest.TestCase):
    def tearDown(self):
        orch.Logger.verbose = True

    def test_quiet_default(self):
        orch.Logger.verbose = False
        err, fh = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch.object(orch.Logger, "fh", fh):
            orch.say("admitted n3: score 3")
            orch.say("pick n3", m=True)
            orch.say("warn: metric missing: load1")
            orch.say("REFUSE container x: not agent-owned")
        shown = err.getvalue()
        self.assertNotIn("admitted", shown)
        self.assertEqual(shown.count("\n"), 3)
        self.assertEqual(fh.getvalue().count("\n"), 4)

    def test_cmd_run_reads_flag(self):
        for v, env, want in ((True, None, True), (False, "1", True), (False, None, False)):
            with mock.patch.dict(os.environ, {"ORCH_VERBOSE": env} if env else {}, clear=False), \
                    mock.patch.object(orch, "retention", lambda: None), \
                    mock.patch.object(orch, "_run_attempts", lambda j, a: 0):
                if not env:
                    os.environ.pop("ORCH_VERBOSE", None)
                a = SimpleNamespace(job="t-ok", args=[], lease=None, heartbeat=None, ref=None, wt=False, verbose=v)
                orch.cmd_run(a)
            self.assertEqual(orch.Logger.verbose, want)


class TimeoutCleanupF312(unittest.TestCase):
    def test_retry_until_gone(self):
        ctx = fake_ctx()
        ctx.timed_out = True
        cname = f"agentt-{J}-job"
        st = {"rm": 0}
        row = f"id1\t{cname}\t1\t{J}\t9\tjob\t{orch.OWNER}\trunning\n"

        def resp(argv, kw):
            if argv[3:5] == ["ps", "-a"] and orch.LF in argv:
                return orch.R(0, row if st["rm"] < 2 else "")
            if "inspect" in argv:
                return orch.R(0, f"/{cname}\t" + json.dumps({orch.L_AGENT: "1", orch.L_OWNER: orch.OWNER}))
            if "rm" in argv:
                st["rm"] += 1
                return orch.R(124 if st["rm"] == 1 else 0, "", "timed out")
            return None
        with FakeCmd(resp) as fc:
            removed = orch.cleanup_job(ctx)
        rms = [a for a in fc.argvs if "rm" in a and "-f" in a]
        self.assertEqual(len(rms), 2)
        self.assertEqual(removed, [f"container {cname}"])


class AlertLogF313(unittest.TestCase):
    def test_utf8_and_closed(self):
        opened = []
        real = open

        def spy(*a, **k):
            f = real(*a, **k)
            if str(a[0]).endswith("alerts-call.log"):
                opened.append((f, k.get("encoding")))
            return f
        prot = {"alert": {"script": "x.sh", "numbers": [], "window_start_h": 0, "window_end_h": 24}}  # legacy form
        with mock.patch.dict(os.environ, {"ORCH_ALERT_DRYRUN": "0"}), mock.patch("builtins.open", spy), \
                mock.patch.object(orch, "cfg_prot", lambda: prot), mock.patch.object(orch.shutil, "which", lambda n: "/bin/bash"), \
                mock.patch.object(orch.subprocess, "Popen", lambda *a, **k: None), \
                mock.patch.object(orch, "audit", lambda *a, **k: None):
            orch.alert("hx", ["t"], call=True)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0][1], "utf-8")
        self.assertTrue(opened[0][0].closed)


class TimeoutKillF315(unittest.TestCase):
    def test_short_grace(self):
        ctx = fake_ctx()

        class P:
            returncode = 137
            stdout = iter([])

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 137
        with FakeCmd() as fc, mock.patch.object(orch.subprocess, "Popen", lambda *a, **k: P()), \
                mock.patch.object(orch, "audit", lambda *a, **k: None):
            orch.stream_attach(ctx, "agentt-x-job", lambda: 0)
        stop = next(a for a in fc.argvs if "stop" in a)
        self.assertEqual(stop[-3:], ["-t", "2", "agentt-x-job"])
        self.assertTrue(ctx.timed_out and ctx.orch_killed)


if __name__ == "__main__":
    unittest.main()
