"""Review round 1 engine fixes: a watchdog trip (or a lost watchdog) kills a running image build or external job
(R2), legacy ORCH_CONFIG mode is refused inside approved projects and never reaches a production host (R3), one-node
gc for a helper leaving the setup (R5), strict ssh host keys (R9). Round 2: the real ssh destination of a legacy node
counts (Q2), one buildx instance and record set per docker engine (Q3), ORCH_GC_NODE_JSON never names an endpoint of
its own (Q4). Local python child processes only; no network, ssh or docker (ssh -G and docker context inspect are
orchtest's offline fakes)."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import orchtest  # noqa: E402
from orchtest import CFG, NODES, REAL_WATCHED_CMD, FakeCmd, orch, write_config  # noqa: E402
from test_orch_run import fake_ctx  # noqa: E402

GIT = shutil.which("git")
TRIP = "psi 31 > 25 (3 samples in a row)"
LOST = "stream ended early after 3 samples"


def tmpdir(t: unittest.TestCase) -> Path:
    d = Path(tempfile.mkdtemp(prefix="orchsafe-")).resolve()
    t.addCleanup(shutil.rmtree, d, True)
    return d


def gone(pid: int) -> bool:
    """Dead, or a zombie nobody reaped yet (a container whose pid 1 does not reap counts as dead too)."""
    if not orch.pid_alive(pid):
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def later(t: unittest.TestCase, s: float, fn) -> None:
    tm = threading.Timer(s, fn)
    tm.daemon = True
    tm.start()
    t.addCleanup(tm.cancel)


class BlockingPopen:
    """subprocess.Popen stand-in: every start is recorded; the process runs until killed (a long buildx client), or
    exits at once with exit_code when given."""

    def __init__(self, exit_code=None, on_start=None):
        self.starts, self.exit_code, self.on_start = [], exit_code, on_start

    def __call__(self, argv, **kw):
        self.starts.append((list(argv), kw))
        if self.on_start:
            self.on_start()
        return _Proc(self.exit_code)


class _Proc:
    pid = 0

    def __init__(self, exit_code):
        self.killed = threading.Event()
        self.code = exit_code
        self.stdout = io.BytesIO(b"#1 [internal] load build definition from Dockerfile\n")

    @property
    def returncode(self):
        return self.poll()

    def poll(self):
        if self.code is not None:
            return self.code
        return -9 if self.killed.is_set() else None

    def wait(self, timeout=None):
        if self.code is not None:
            return self.code
        if not self.killed.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        return -9

    def kill(self):
        self.killed.set()


class WatchedCmd(unittest.TestCase):
    """The real poll-and-kill loop against real local child processes."""

    def run_py(self, code, stop=lambda: None, timeout=60, stdin=None):
        t0 = time.monotonic()
        r = REAL_WATCHED_CMD([sys.executable, "-c", code], stop=stop, job="t", timeout=timeout, stdin=stdin, poll_s=0.1)
        return r, time.monotonic() - t0

    def test_stop_kills_the_command_and_its_children_at_once(self):
        d = tmpdir(self)
        pidf = d / "grandchild.pid"
        code = ("import os, subprocess, sys, time\n"
                "print('building', flush=True)\n"
                "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pidf)!r} + '.tmp', 'w').write(str(g.pid))\n"
                f"os.replace({str(pidf)!r} + '.tmp', {str(pidf)!r})\n"
                "time.sleep(60)\n")
        r, took = self.run_py(code, stop=lambda: TRIP if pidf.exists() else None)
        self.assertEqual((r.stopped, r.rc), (TRIP, 137))
        self.assertIn("building", r.out)
        self.assertIn("killed by orch: " + TRIP, r.err)
        self.assertLess(took, 20)
        gpid = int(pidf.read_text())
        for _ in range(50):  # the plugin-like grandchild dies with it (taskkill /T, or the process group)
            if gone(gpid):
                break
            time.sleep(0.1)
        self.assertTrue(gone(gpid), "grandchild survived the kill")

    def test_timeout_kills_with_rc_124(self):
        r, took = self.run_py("import time; time.sleep(60)", timeout=1)
        self.assertEqual((r.rc, r.stopped), (124, None))
        self.assertIn("timed out after 1s", r.err)
        self.assertLess(took, 20)

    def test_exit_code_output_and_stdin_file(self):
        d = tmpdir(self)
        (d / "in.bin").write_bytes(b"ctx-tar-bytes")
        with open(d / "in.bin", "rb") as f:
            r, _ = self.run_py("import sys; d = sys.stdin.buffer.read(); print('got', d.decode()); "
                               "print('oops', file=sys.stderr); sys.exit(3)", stdin=f)
        self.assertEqual((r.rc, r.stopped), (3, None))
        self.assertIn("got ctx-tar-bytes", r.out)
        self.assertIn("oops", r.out)  # stderr merged, as the build log and fail patterns read it

    def test_a_tripped_job_starts_nothing(self):
        with mock.patch.object(orch.subprocess, "Popen", side_effect=AssertionError("started after a trip")):
            r = REAL_WATCHED_CMD(["docker", "buildx", "build", "-"], stop=lambda: TRIP, job="t")
        self.assertEqual((r.stopped, r.rc), (TRIP, 137))


FILES = {"src/Dockerfile": b"FROM scratch\nCOPY app.py /\n", "src/app.py": b"print(1)\n"}


class BuildTrip(unittest.TestCase):
    """R2: a trip or a lost watchdog during an image build kills the buildx client at once, no later target starts,
    and the idle agent builder is stopped."""

    def ctx(self, targets=("a", "b")):
        ctx = fake_ctx(job="app-image", targets=list(targets))
        d = tmpdir(self)
        tar = d / "s.tar"
        with tarfile.open(tar, "w") as t:
            for n, b in FILES.items():
                ti = tarfile.TarInfo(n)
                ti.size = len(b)
                t.addfile(ti, io.BytesIO(b))
        ctx.opts._src = {"tar": str(tar), "ref": "abc1234", "files": list(FILES), "sha": "a" * 40, "wt": False}
        return ctx

    def build(self, ctx, popen):
        kills = []

        def kill_tree(proc):
            kills.append(proc)
            proc.kill()
        t0 = time.monotonic()
        with FakeCmd() as fc, mock.patch.object(orch, "watched_cmd", REAL_WATCHED_CMD), \
                mock.patch.object(orch.subprocess, "Popen", popen), mock.patch.object(orch, "kill_tree", kill_tree), \
                mock.patch.object(orch, "ensure_builder", lambda *a, **k: None), mock.patch.object(orch, "say"):
            cls = orch.run_build_job(ctx)
        return cls, time.monotonic() - t0, kills, fc

    @staticmethod
    def builder_stops(fc):
        return [a for a in fc.argvs if a[-4:] == ["stop", "-t", "5", "agentt-builder"]]

    def test_trip_mid_build_kills_it_and_no_later_target_starts(self):
        ctx, popen = self.ctx(), BlockingPopen()
        later(self, 0.3, lambda: setattr(ctx, "guard", TRIP))
        cls, took, kills, fc = self.build(ctx, popen)
        self.assertEqual(cls, "r1-guard")
        self.assertLess(took, 10)
        self.assertTrue(ctx.orch_killed)
        self.assertEqual(len(kills), 1)
        self.assertEqual(len(popen.starts), 1, "target b started after the trip")
        self.assertIn("--target", popen.starts[0][0])
        self.assertEqual(popen.starts[0][0][popen.starts[0][0].index("--target") + 1], "a")
        self.assertEqual(ctx.meta["failed_target"], "a")
        self.assertEqual(len(self.builder_stops(fc)), 1)
        self.assertTrue(ctx.meta["builder_stopped"])

    def test_lost_watchdog_mid_build_fails_closed(self):
        ctx, popen = self.ctx(), BlockingPopen()
        later(self, 0.3, lambda: setattr(ctx, "wd_lost", LOST))
        cls, took, kills, fc = self.build(ctx, popen)
        self.assertEqual((cls, ctx.orch_killed, len(kills), len(popen.starts)), ("infra", True, 1, 1))
        self.assertLess(took, 10)

    def test_trip_before_a_target_starts_nothing(self):
        ctx, popen = self.ctx(), BlockingPopen()
        ctx.guard = TRIP
        cls, _, kills, fc = self.build(ctx, popen)
        self.assertEqual((cls, popen.starts, kills, self.builder_stops(fc)), ("r1-guard", [], [], []))

    def test_trip_while_a_target_ends_skips_the_rest(self):
        ctx = self.ctx()
        popen = BlockingPopen(exit_code=0, on_start=lambda: setattr(ctx, "guard", TRIP))
        cls, _, kills, fc = self.build(ctx, popen)
        self.assertEqual((cls, len(popen.starts), kills, self.builder_stops(fc)), ("r1-guard", 1, [], []))

    def test_builder_kept_while_another_build_job_of_this_master_is_live(self):
        other = orch.live_dir() / "1030000000beef.json"
        orch.write_json(other, {"jobid": "1030000000beef", "pid": os.getpid(), "pstart": orch.proc_start(os.getpid()),
                                "node": "hw", "job": "app-image", "heavy": True})
        self.addCleanup(other.unlink, True)
        ctx, popen = self.ctx(), BlockingPopen()
        later(self, 0.3, lambda: setattr(ctx, "guard", TRIP))
        cls, _, kills, fc = self.build(ctx, popen)
        self.assertEqual((cls, len(kills), self.builder_stops(fc)), ("r1-guard", 1, []))
        self.assertFalse(ctx.meta["builder_stopped"])

    def test_execute_trip_during_a_build_is_exit_6_with_cordon_and_alert(self):
        """The real trip path end to end: Watchdog's on_trip -> ctx.guard -> client killed -> r1-guard, cordon, alert."""
        ctx0 = self.ctx()
        job, node = ctx0.job, ctx0.node
        cordons, alerts, test = [], [], self

        class WD:
            def __init__(self, node_, base, jobid, on_trip, lease_s, iters=0, on_dead=None):
                self.on_trip, self.samples = on_trip, []

            def start(self):
                later(test, 0.3, lambda: self.on_trip(TRIP))

            def stop(self):
                pass
        fp = {"ok": True, "containers": {}, "latency": {}, "services": [], "t": orch.now()}
        opts = SimpleNamespace(lease=300, heartbeat=60, _src=ctx0.opts._src, _sel_s=0.0)
        popen = BlockingPopen()
        empty = {"ok": True, "err": "", "objects": [], "collisions": []}
        t0 = time.monotonic()
        with FakeCmd(), mock.patch.object(orch, "watched_cmd", REAL_WATCHED_CMD), \
                mock.patch.object(orch.subprocess, "Popen", popen), \
                mock.patch.object(orch, "kill_tree", lambda p: p.kill()), \
                mock.patch.object(orch, "ensure_builder", lambda *a, **k: None), \
                mock.patch.object(orch, "list_agent", lambda *a, **k: dict(empty)), \
                mock.patch.object(orch, "reconcile_node", lambda *a, **k: []), \
                mock.patch.object(orch, "fingerprint", lambda *a, **k: dict(fp)), \
                mock.patch.object(orch, "Watchdog", WD), mock.patch.object(orch, "cleanup_job", lambda *a, **k: []), \
                mock.patch.object(orch, "cordon", lambda *a: cordons.append(a)), \
                mock.patch.object(orch, "r1_guard_alert", lambda *a: alerts.append(a)), mock.patch.object(orch, "say"):
            cls, ctx = orch.execute(job, node, {"caches": []}, {"jobid": "1030000000abcd"}, opts)
        self.assertEqual((cls, orch.EXIT[cls], len(popen.starts)), ("r1-guard", 6, 1))
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(cordons[0][:2], ("hw", "r1-guard: " + TRIP))
        self.assertEqual(alerts[0][2], TRIP)
        self.assertEqual(ctx.meta["reason"], "watchdog r1-guard: " + TRIP)


class ExternalTrip(unittest.TestCase):
    """R2: an external job (an argv on this master) is killed on a trip or a lost watchdog too."""

    def run_ext(self, attr, value):
        ctx = fake_ctx(job="t-ok", kind="external", cmd=["e2e-runner", "--suite", "smoke"], timeout=600)
        later(self, 0.3, lambda: setattr(ctx, attr, value))
        popen, kills = BlockingPopen(), []
        t0 = time.monotonic()
        with FakeCmd(), mock.patch.object(orch, "watched_cmd", REAL_WATCHED_CMD), \
                mock.patch.object(orch.subprocess, "Popen", popen), \
                mock.patch.object(orch, "kill_tree", lambda p: (kills.append(p), p.kill())), mock.patch.object(orch, "say"):
            cls = orch.run_external_job(ctx)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual((popen.starts[0][0], len(kills), ctx.orch_killed), (["e2e-runner", "--suite", "smoke"], 1, True))
        return cls

    def test_trip(self):
        self.assertEqual(self.run_ext("guard", TRIP), "r1-guard")

    def test_lost_watchdog(self):
        self.assertEqual(self.run_ext("wd_lost", LOST), "infra")


class LegacyBase(unittest.TestCase):
    """A temp ORCH_HOME (orch.HOME patched) and env without ORCH_TEST_MODE."""

    def setUp(self):
        self.tmp = tmpdir(self)
        self.home = self.tmp / "home"
        self.home.mkdir()
        for p in (mock.patch.object(orch, "HOME", self.home),
                  mock.patch.dict(os.environ, {"ORCH_HOME": str(self.home), "ORCH_CONFIG": str(CFG)})):
            p.start()
            self.addCleanup(p.stop)
        for k in ("ORCH_TEST_MODE", "ORCH_PROJECT"):  # the patched dict restores both
            os.environ.pop(k, None)
        orch.reset_project()
        self.addCleanup(orch.reset_project)
        orch._validated.clear()
        self.addCleanup(orch._validated.clear)

    def verb(self, name):
        p = mock.patch.dict(orch.VERB, {"name": name})
        p.start()
        self.addCleanup(p.stop)
        orch.reset_project()

    def view(self, hosts=(), projects=()):
        (self.home / "guard-view.json").write_text(json.dumps({"schema": 1, "hosts": list(hosts),
                                                               "projects": list(projects)}), encoding="utf-8")


def vhost(name, roles, hostname, port=22, aliases=(), contexts=(), spec=None):
    return {"name": name, "roles": list(roles), "spec": spec or f"deploy@{hostname}" + (f":{port}" if port != 22 else ""),
            "hostname": hostname, "port": port, "user": "deploy", "ips": [hostname], "aliases": list(aliases),
            "contexts": list(contexts)}


@unittest.skipUnless(GIT, "needs git")
class LegacyGate(LegacyBase):
    """R3: ORCH_CONFIG skips approval, so it is refused inside an approved project (unless ORCH_TEST_MODE=1)."""

    def setUp(self):
        super().setUp()
        oc = orch.sibling("orch_config")
        self.root = self.tmp / "shop"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        p, _ = oc.init_project(self.root, env={})
        cfg = {"schema": 1, "hosts": {}, "jobs": {}}
        oc.save_config(p, cfg)
        oc.trust_set(self.home, p, cfg, {})
        (self.root / "api").mkdir()

    def test_refused_from_the_project_or_any_subdir_of_it(self):
        for env in ({"ORCH_PROJECT": str(self.root)}, {}):
            cwd = self.root / "api" if not env else self.tmp
            with mock.patch.dict(os.environ, env), mock.patch.object(orch.os, "getcwd", lambda: str(cwd)):
                orch.reset_project()
                with self.assertRaisesRegex(orch.ConfigError, "ORCH_CONFIG is refused inside the approved orch "
                                                              r"project .*shop.*unset ORCH_CONFIG"):
                    orch.cfg_nodes()

    def test_cli_exit_2_and_the_fix(self):
        o, e = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"ORCH_PROJECT": str(self.root)}), redirect_stdout(o), redirect_stderr(e), \
                mock.patch.object(orch.atexit, "register"), mock.patch.object(orch.signal, "signal"):
            rc = orch.main(["plan", "t-ok"])
        self.assertEqual(rc, 2)
        last = json.loads(o.getvalue().strip().splitlines()[-1])
        self.assertEqual((last["result"], last["exit"]), ("usage", 2))
        self.assertIn("unset ORCH_CONFIG and run the command again", last["error"])
        self.assertNotIn("ORCH_TEST_MODE", o.getvalue() + e.getvalue())  # never hint the switch to the agent
        jr = orch.sibling("orch_journal")  # the refusal is in the project's journal (legacy runs are not)
        p = orch.sibling("orch_config").find_project(str(self.root), env={})
        diffs = [r for r in jr.read(p) if r.get("type") == "difficulty"]
        self.assertEqual(len(diffs), 1)
        self.assertIn("ORCH_CONFIG is refused inside the approved orch project", diffs[0]["detail"])
        self.assertIn(f"journal: difficulty #{diffs[0]['seq']} recorded (orch)", e.getvalue())

    def test_allowed_outside_any_approved_project_and_in_test_mode(self):
        with mock.patch.object(orch.os, "getcwd", lambda: str(self.tmp)):
            orch.reset_project()
            self.assertIn("n1", orch.cfg_nodes()["nodes"])
        with mock.patch.dict(os.environ, {"ORCH_PROJECT": str(self.root), "ORCH_TEST_MODE": "1"}):
            orch.reset_project()
            self.assertIn("n1", orch.cfg_nodes()["nodes"])

    def test_a_root_in_the_guard_view_counts_too(self):
        other = self.tmp / "other"
        (other / "web").mkdir(parents=True)
        self.view(projects=[{"root": other.as_posix(), "store": (other / ".orch").as_posix()}])
        with mock.patch.object(orch.os, "getcwd", lambda: str(other / "web")):
            orch.reset_project()
            with self.assertRaisesRegex(orch.ConfigError, "refused inside the approved orch project .*other"):
                orch.cfg_nodes()

class LegacyProduction(LegacyBase):
    """R3: legacy nodes never include a production host of the guard view, test mode or not."""

    def setUp(self):
        super().setUp()
        os.environ["ORCH_TEST_MODE"] = "1"

    def check(self, verb="run"):
        self.verb(verb)
        orch._validated.clear()
        with mock.patch.object(orch, "say") as say:
            return orch.cfg_nodes(), say

    def test_host_alias_ssh_args_and_context_match(self):
        cases = [
            ([vhost("p20", ["prod"], "192.0.2.12")], "n2"),  # node host / IP
            ([vhost("p30", ["prod"], "198.51.100.30", aliases=["n1"])], "n1"),  # ssh alias
            ([vhost("p13", ["prod"], "192.0.2.13", port=2222)], "n3"),  # ssh_args destination
            ([vhost("p40", ["helper", "prod"], "203.0.113.40", contexts=["n2"])], "n2"),  # docker context, helper+prod
        ]
        for hosts, node in cases:
            self.view(hosts=hosts)
            with self.assertRaisesRegex(orch.ConfigError, rf"node {node} is the production host {hosts[0]['name']} .* "
                                                          "orch never runs jobs there; remove it"):
                self.check()

    def test_port_decides_between_hosts_behind_one_address(self):
        self.view(hosts=[vhost("p13", ["prod"], "192.0.2.13", port=22), vhost("h13", ["helper"], "192.0.2.13", port=2222)])
        self.assertIn("n3", self.check()[0]["nodes"])  # n3 is -p 2222: the helper
        self.view(hosts=[vhost("p13", ["prod"], "192.0.2.13", port=2222), vhost("h13", ["helper"], "192.0.2.13")])
        with self.assertRaisesRegex(orch.ConfigError, "node n3 is the production host p13"):
            self.check()

    def test_helper_only_hosts_and_no_view_pass(self):
        self.assertEqual(sorted(self.check()[0]["nodes"]), ["n1", "n2", "n3"])
        self.view(hosts=[vhost("h11", ["helper"], "192.0.2.11", contexts=["n1"])])
        self.assertEqual(sorted(self.check()[0]["nodes"]), ["n1", "n2", "n3"])

    def test_cleanup_verbs_only_warn(self):
        self.view(hosts=[vhost("p20", ["prod"], "192.0.2.12")])
        for verb in ("gc", "reconcile", "stop-all", "status"):
            nodes, say = self.check(verb)
            self.assertIn("n2", nodes["nodes"])
            self.assertIn("node n2 is the production host p20", say.call_args[0][0])

    def test_unreadable_view_refuses_runtime_verbs(self):
        (self.home / "guard-view.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(orch.ConfigError, "not a valid guard view .*cannot tell which hosts are production"):
            self.check()
        self.assertIn("n1", self.check("gc")[0]["nodes"])

    # review round 2 Q2: the real destination counts, not only the literal node fields
    PC = [vhost("pc", ["prod"], "192.0.2.20", port=2222), vhost("ha", ["helper"], "192.0.2.11", aliases=["haa"]),
          vhost("p13", ["prod"], "192.0.2.13"), vhost("h13", ["helper"], "192.0.2.13", port=2222)]

    def nodes(self, **nodes):
        d = tmpdir(self)
        write_config(d, nodes=nodes)
        p = mock.patch.dict(os.environ, {"ORCH_CONFIG": str(d)})
        p.start()
        self.addCleanup(p.stop)

    def x(self, sa, **kw):
        self.nodes(x=dict({"enabled": True, "os": "linux", "host": "192.0.2.11", "ssh": "haa", "ssh_args": sa,
                           "context": "cx", "transport": "context", "tags": ["docker", "linux-host"],
                           "opt_in": ["t-ok"]}, **kw))

    def test_ssh_options_that_change_the_destination_are_refused(self):
        self.view(hosts=self.PC)
        for sa in (["-o", "HostName=192.0.2.20", "placeholder"], ["-o", "HostName=192.0.2.20", "deploy@192.0.2.11"],
                   ["-o", "hostname=192.0.2.20", "haa"], ["-o", "Port=22", "-p", "2222", "deploy@192.0.2.13"],
                   ["-o", "ProxyJump=deploy@192.0.2.20:2222", "deploy@192.0.2.11"],
                   ["-o", "ControlPath=/tmp/master-pc", "deploy@192.0.2.11"]):
            self.x(sa)
            with self.assertRaisesRegex(orch.ConfigError, rf"node x: ssh_args -o {re.escape(repr(sa[1]))} is not allowed"):
                self.check()

    def test_where_the_node_really_connects_is_checked(self):
        self.view(hosts=self.PC)
        pc, p13 = "pc (deploy@192.0.2.20:2222)", "p13 (deploy@192.0.2.13)"
        for conf, ctxs, sa, who, how in (
                ({"n9": {"hostname": "192.0.2.20"}}, {}, ["n9"], pc, "its ssh connects to 192.0.2.20:22"),  # ~/.ssh/config
                ({}, {}, ["-J", "deploy@192.0.2.20:2222", "deploy@192.0.2.11"], pc,
                 "its ssh goes through jump host deploy@[192.0.2.20]:2222 (192.0.2.20:2222)"),  # as ssh -G prints it
                ({"jumpy": {"hostname": "192.0.2.20"}}, {}, ["-J", "jumpy", "deploy@192.0.2.11"], pc,
                 "its ssh goes through jump host jumpy (192.0.2.20:22)"),
                ({"haa": {"hostname": "192.0.2.11", "proxyjump": "hop1"}, "hop1": {"hostname": "192.0.2.11", "proxyjump":
                  "pc-jump"}, "pc-jump": {"hostname": "192.0.2.20", "port": 2222}}, {}, ["haa"], pc,
                 "its ssh goes through jump host pc-jump (192.0.2.20:2222)"),  # ProxyJump from the config, two deep
                ({}, {"cx": "ssh://deploy@192.0.2.20:2222"}, ["deploy@192.0.2.11"], pc,
                 "its docker context cx (ssh://deploy@192.0.2.20:2222): its ssh connects to 192.0.2.20:2222"),
                ({}, {"cx": "tcp://192.0.2.20:2376"}, ["deploy@192.0.2.11"], pc,
                 "its docker context cx connects to tcp://192.0.2.20:2376"),
                ({"n9": {"hostname": "192.0.2.13", "port": 22}}, {}, ["n9"], p13, "its ssh connects to 192.0.2.13:22")):
            self.x(sa)
            with mock.patch.dict(orchtest.SSH_CONFIG, conf), mock.patch.dict(orchtest.CONTEXTS, ctxs):
                with self.assertRaisesRegex(orch.ConfigError, re.escape(f"node x reaches the production host {who} of an "
                                                                        f"approved setup: {how}") + ": orch never runs"):
                    self.check()
                self.assertIn("x", self.check("gc")[0]["nodes"])  # cleanup verbs never resolve and never refuse

    def test_resolved_helpers_pass_and_nothing_resolves_without_a_view(self):
        self.view(hosts=self.PC)
        self.x(["n9"])
        with mock.patch.dict(orchtest.SSH_CONFIG, {"n9": {"hostname": "192.0.2.11"}}), \
                mock.patch.dict(orchtest.CONTEXTS, {"cx": "ssh://haa"}):
            self.assertIn("x", self.check()[0]["nodes"])
        self.x(["-p", "2222", "deploy@192.0.2.13"], context=None, transport="ssh-docker")
        self.assertIn("x", self.check()[0]["nodes"])  # 192.0.2.13:2222 is the helper h13, not p13
        (self.home / "guard-view.json").unlink()
        with mock.patch.object(orch, "ssh_dump") as sd, mock.patch.object(orch, "context_endpoint") as ce:
            self.assertIn("x", self.check()[0]["nodes"])
        self.assertEqual((sd.call_count, ce.call_count), (0, 0))

    def test_an_unresolvable_node_is_refused_in_runtime_verbs_only(self):
        self.view(hosts=self.PC)
        with mock.patch.dict(orchtest.SSH_CONFIG, {"n1": {"fail": "command-line: line 0: Bad configuration option: x"}}):
            with self.assertRaisesRegex(orch.ConfigError, r"node n1 could not be checked against the production hosts: "
                                                          r"ssh -G n1 failed \(command-line: line 0: Bad configuration "
                                                          r"option: x\).*\(or fix its ssh settings so that ssh -G "
                                                          r"resolves them\)"):
                self.check()
            self.assertIn("n1", self.check("gc")[0]["nodes"])

    def test_a_resolver_crash_makes_the_node_uncheckable_not_the_verb(self):
        self.view(hosts=self.PC)
        with mock.patch.object(orch, "node_route", side_effect=RuntimeError("boom")):
            bad, err = orch.legacy_prod_nodes({"x": dict(NODES["n3"])}, resolve=True)
        self.assertEqual((bad, err), ({"x": "could not be checked against the production hosts: orch could not resolve "
                                            "it (RuntimeError('boom'))"}, None))

    def test_the_real_resolvers_are_local_commands(self):
        with FakeCmd(lambda argv, kw: orch.R(0, "user deploy\nhostname 192.0.2.13\nport 2222\n")) as fc:
            self.assertTrue(orchtest.REAL_SSH_DUMP(["-p", "2222", "deploy@192.0.2.13"]).ok)
        self.assertEqual(fc.argvs, [["ssh", "-G"] + orch.SSH[1:] + ["-p", "2222", "deploy@192.0.2.13"]])
        with FakeCmd([orch.R(0, '"ssh://deploy@192.0.2.20"\n'), orch.R(1, "", "context not found"),
                      orch.R(0, "null\n"), orch.R(0, "{not json\n")]) as fc:
            self.assertEqual(orchtest.REAL_CONTEXT_ENDPOINT("cx"), "ssh://deploy@192.0.2.20")
            self.assertEqual([orchtest.REAL_CONTEXT_ENDPOINT(c) for c in ("gone", "odd", "bad")], [None, None, None])
        self.assertEqual(fc.argvs[0], ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}", "cx"])


class TestOnlyEnv(unittest.TestCase):
    """R3: ORCH_CI_CHILD, ORCH_FAULT and ORCH_REPO_<NAME> act only together with ORCH_CONFIG."""

    def setUp(self):
        orch.reset_project()
        self.addCleanup(orch.reset_project)

    def no_config(self, **env):
        base = {k: v for k, v in os.environ.items() if k != "ORCH_CONFIG"}
        return mock.patch.dict(os.environ, dict(base, **env), clear=True)

    def test_ci_child_ignored_in_project_mode(self):
        a = SimpleNamespace(wait=5, ref=None, wt=False)
        with self.no_config(ORCH_CI_CHILD='["ssh", "pc", "docker", "rm", "-f", "app"]'), redirect_stderr(io.StringIO()) as e:
            argv = orch.ci_child_argv("app-test", a)
        self.assertEqual(argv[:2], [sys.executable, "-u"])
        self.assertTrue(argv[2].endswith("orch.py"))
        self.assertIn("ORCH_CI_CHILD is test-only and is ignored without ORCH_CONFIG", e.getvalue())
        with mock.patch.dict(os.environ, {"ORCH_CI_CHILD": "stub-orch"}):
            self.assertEqual(orch.ci_child_argv("j", a)[:2], ["stub-orch", "j"])

    def test_fault_ignored_in_project_mode(self):
        ctx = fake_ctx(job="t-ok", node=orch.Node("n1", dict(NODES["n1"])))
        with self.no_config(ORCH_FAULT="create@n1"), FakeCmd() as fc, mock.patch.object(orch, "say"):
            orch.create_run(ctx, "job", "alpine", "none", [], "true", "64m")
        self.assertTrue(any("create" in a for a in fc.argvs))
        with mock.patch.dict(os.environ, {"ORCH_FAULT": "create@n1"}), FakeCmd():
            with self.assertRaisesRegex(orch.OrchError, "injected fault"):
                orch.create_run(ctx, "job", "alpine", "none", [], "true", "64m")

    def test_repo_override_only_in_legacy_mode(self):
        with self.no_config(ORCH_REPO_APP="/srv/other"), mock.patch.object(orch, "say"):
            self.assertEqual(orch.legacy_env("ORCH_REPO_APP"), "")
        with mock.patch.dict(os.environ, {"ORCH_REPO_APP": "/srv/other"}):
            self.assertEqual(orch.legacy_env("ORCH_REPO_APP"), "/srv/other")


class GcOneNode(unittest.TestCase):
    """R5: orch gc --session-end --node NAME cleans one node, also one that left the setup (ORCH_GC_NODE_JSON)."""
    GONE = {"enabled": True, "os": "linux", "host": "192.0.2.14", "ssh": "deploy@192.0.2.14",
            "ssh_args": ["deploy@192.0.2.14"], "context": "orch-h14", "transport": "context", "docker_host": None,
            "tags": ["docker", "buildx", "linux-host"], "opt_in": ["t-ok"], "io": None, "note": "roles helper"}

    def gc(self, argv, gc_res=None, left=(), env=None, left_ok=True):
        seen = []

        def gc_node(n):
            seen.append(n)
            return dict({"node": n.name, "removed": ["volume agentt-builder-state"], "kept": [], "error": None,
                         "collisions": [], "live": []}, **(gc_res or {}))
        la = {"ok": left_ok, "err": "" if left_ok else "ssh: connect timed out", "collisions": [],
              "objects": [{"kind": "container", "name": n} for n in left]}
        o = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), mock.patch.object(orch, "gc_node", gc_node), \
                mock.patch.object(orch, "list_agent", lambda *a, **k: la), redirect_stdout(o), \
                redirect_stderr(io.StringIO()), mock.patch.object(orch.atexit, "register"), \
                mock.patch.object(orch.signal, "signal"):
            rc = orch.main(["gc", "--session-end"] + argv)
        return rc, o.getvalue(), seen

    def test_node_in_the_setup(self):
        rc, out, seen = self.gc(["--node", "n1"])
        self.assertEqual((rc, [n.name for n in seen]), (0, ["n1"]))
        self.assertIn("n1: agent objects left: 0", out)

    def test_node_that_left_the_setup_comes_from_the_env(self):
        env = {"ORCH_GC_NODE_JSON": json.dumps({"name": "h14", "node": self.GONE})}
        rc, out, seen = self.gc(["--node", "h14"], env=env)
        self.assertEqual(rc, 0)
        n = seen[0]
        self.assertEqual((n.name, n.ctx, n.docker_argv(["ps"])), ("h14", "orch-h14", ["docker", "--context", "orch-h14", "ps"]))

    def test_exit_codes(self):
        self.assertEqual(self.gc(["--node", "n1"], gc_res={"error": "ssh: connect timed out"})[0], 5)
        rc, out, _ = self.gc(["--node", "n1"], gc_res={"live": ["1030000000abcd"]})
        self.assertEqual(rc, 75)
        self.assertIn("live job(s) 1030000000abcd still run there; run orch gc --session-end --node n1 again", out)
        rc, out, _ = self.gc(["--node", "n1"], left=["agentt-builder"])
        self.assertEqual(rc, 5)
        self.assertIn("NOT clean, still there: container agentt-builder", out)
        self.assertEqual(self.gc(["--node", "n1"], left_ok=False)[0], 5)

    def test_unknown_node_and_bad_env(self):
        rc, out, seen = self.gc(["--node", "h99"])
        self.assertEqual((rc, seen), (2, []))
        self.assertIn("unknown node h99", out)
        self.assertIn("orch init cleans a helper itself before it leaves the setup", out)
        self.assertNotIn("ORCH_GC_NODE_JSON", out)  # Q4: never steer the caller to hand-make a node
        for raw in ("{not json", json.dumps({"name": "h98", "node": self.GONE}), json.dumps({"name": "h99"}),
                    json.dumps({"name": "h99", "node": dict(self.GONE, os="plan9")})):
            rc, out, seen = self.gc(["--node", "h99"], env={"ORCH_GC_NODE_JSON": raw})
            self.assertEqual((rc, seen), (2, []), raw)

    def test_env_node_only_in_legacy_test_mode_and_never_production(self):  # review round 2 Q4
        env = {"ORCH_GC_NODE_JSON": json.dumps({"name": "h14", "node": self.GONE})}
        rc, out, seen = self.gc(["--node", "h14"], env=dict(env, ORCH_TEST_MODE=""))
        self.assertEqual((rc, seen), (2, []))
        self.assertIn("ORCH_GC_NODE_JSON is test-only: list node h14 in the ORCH_CONFIG nodes.json instead",
                      json.loads(out.strip().splitlines()[-1])["error"])
        gv = orch.HOME / "guard-view.json"
        self.addCleanup(gv.unlink, True)
        for host, ctxs in (("192.0.2.14", {}),  # the node itself
                           ("192.0.2.20", {"orch-h14": "ssh://deploy@192.0.2.20"})):  # where its docker context goes
            gv.write_text(json.dumps({"schema": 1, "projects": [], "hosts": [vhost("pc", ["prod"], host)]}), encoding="utf-8")
            with mock.patch.dict(orchtest.CONTEXTS, ctxs):
                rc, out, seen = self.gc(["--node", "h14"], env=env)
            self.assertEqual((rc, seen), (2, []), host)
            self.assertRegex(json.loads(out.strip().splitlines()[-1])["error"],
                             r"^ORCH_GC_NODE_JSON: node h14 (is|reaches) the production host pc .*orch never cleans it")

    def test_live_job_on_the_node_is_75_and_its_objects_stay(self):
        live = orch.live_dir() / "1030000000cafe.json"
        orch.write_json(live, {"jobid": "1030000000cafe", "pid": os.getpid(), "pstart": orch.proc_start(os.getpid()),
                               "node": "fake", "job": "t-ok"})
        self.addCleanup(live.unlink, True)
        from test_orch_core import FakeDockerNode
        own = orch.OWNER
        n = FakeDockerNode([{"name": "agentt-1030000000cafe-job", "labels": {
            "io.orchtest.agent": "1", "io.orchtest.owner": own, "io.orchtest.job": "1030000000cafe",
            "io.orchtest.lease": "0", "io.orchtest.role": "job"}}])
        with FakeCmd(), mock.patch.object(orch, "say"):
            r = orch.gc_node(n)
        self.assertEqual(r["live"], ["1030000000cafe"])
        self.assertFalse([c for c in n.sent if c[:1] == ["rm"]])

    def test_session_end_also_cleans_disabled_nodes_that_keep_a_transport(self):
        nodes = dict(NODES, n4=dict(NODES["n1"], enabled=False, host="192.0.2.14", ssh="n4", context="n4"),
                     n5=dict(NODES["n1"], enabled=False, host="192.0.2.15", ssh="n5", context=None))
        d = tmpdir(self)
        write_config(d, nodes=nodes)
        rc, out, seen = self.gc([], env={"ORCH_CONFIG": str(d)})
        self.assertEqual(sorted(n.name for n in seen), ["n1", "n2", "n3", "n4"])  # n5: no transport left
        self.assertEqual(rc, 0)


class BuilderPerEndpoint(unittest.TestCase):
    """Review round 2 Q3: one buildx instance and one set of builder/base-image records per docker engine, so a second
    endpoint of one machine (another project's orch-<name>-<8 hex>, R8) never builds through the first one's."""
    A = {"os": "linux", "ssh": "deploy@192.0.2.30", "context": "orch-h30", "transport": "context", "enabled": True,
         "opt_in": ["app-image"]}
    B = dict(A, ssh="ci@192.0.2.30", context="orch-h30-370fcd77")
    BREF, WANT = "agentt-builder-orch-h30-370fcd77", "docker-container://agentt-builder?context=orch-h30-370fcd77"

    def test_keys_follow_the_endpoint(self):
        a, b = orch.Node("h30", dict(self.A)), orch.Node("h30", dict(self.B))
        self.assertEqual((orch.builder_ref(a), orch.builder_ref(b)), ("agentt-builder-h30", self.BREF))
        self.assertEqual((orch.engine_key(a), orch.engine_key(b)), ("h30", "orch-h30-370fcd77"))
        self.assertNotEqual(orch.builder_file(a), orch.builder_file(b))
        for name, c in (("n1", NODES["n1"]), ("n3", NODES["n3"])):  # a context named like the node; ssh-docker
            self.assertEqual(orch.engine_key(orch.Node(name, dict(c))), name)
        self.assertRegex(orch.engine_key(orch.Node("h30", dict(self.A, context="Desk+Top"))), r"^desk-top-[0-9a-f]{8}$")

    def ensure(self, node, inspect):
        def resp(argv, kw):
            if "ps" in argv:
                return orch.R(0, f"running\t1\t{orch.OWNER}\n")
            return inspect if argv[:3] == ["docker", "buildx", "inspect"] else None
        self.addCleanup(lambda: orch.builder_file(node).unlink(missing_ok=True))
        jobs = {"jobs": {"app-image": {"kind": "build"}}}
        with FakeCmd(resp) as fc, mock.patch.object(orch, "cfg_jobs", lambda: jobs), \
                mock.patch.object(orch, "cfg_nodes", lambda: {"builder": {}}), mock.patch.object(orch, "say"):
            orch.ensure_builder(node, {}, "1030000000a3b3")
        return [a for a in fc.argvs if a[:2] == ["docker", "buildx"]]

    def test_an_instance_made_for_another_endpoint_is_recreated(self):
        b = orch.Node("h30", dict(self.B))
        other = orch.R(0, f"Name:          {self.BREF}\nDriver:        remote\n\nNodes:\nName:     {self.BREF}0\n"
                          "Endpoint: docker-container://agentt-builder?context=orch-h30\nStatus:   running\n")
        inspect, rm = ["docker", "buildx", "inspect", self.BREF], ["docker", "buildx", "rm", self.BREF]
        create = ["docker", "buildx", "create", "--name", self.BREF, "--driver", "remote", self.WANT]
        self.assertEqual(self.ensure(b, other), [inspect, rm, create])
        same = orch.R(0, other.out.replace("context=orch-h30\n", "context=orch-h30-370fcd77\n"))
        self.assertEqual(self.ensure(b, same), [inspect])
        self.assertEqual(self.ensure(b, orch.R(1, "", "ERROR: no builder found")), [inspect, create])
        self.assertTrue(orch.builder_file(b).name.endswith("builder-orch-h30-370fcd77.json"))

    def test_gc_removes_only_this_endpoints_instance_and_base_images(self):
        from test_orch_core import FakeDockerNode
        n = FakeDockerNode([])
        n.name, n.ctx, n.c = "h30", "orch-h30-370fcd77", dict(self.B)
        ra, rb = "python:3.12-slim@sha256:" + "a" * 64, "python:3.12-slim@sha256:" + "b" * 64
        rec = {"existed_before": False, "first_seen": "2026-10-30 09:00:00"}
        orch.state_update(lambda s: s["base_images"].update({"h30": {ra: dict(rec)}, "orch-h30-370fcd77": {rb: dict(rec)}}))
        self.addCleanup(orch.state_update, lambda s: [s["base_images"].pop(k, None) for k in ("h30", "orch-h30-370fcd77")])
        with FakeCmd() as fc, mock.patch.object(orch, "say"):
            orch.gc_node(n)
        self.assertIn(["docker", "buildx", "rm", self.BREF], fc.argvs)
        self.assertNotIn(["docker", "buildx", "rm", "agentt-builder-h30"], fc.argvs)
        self.assertIn(["rmi", rb], n.sent)
        self.assertNotIn(["rmi", ra], n.sent)
        bi = orch.state_get()["base_images"]
        self.assertEqual((bi.get("h30"), bi.get("orch-h30-370fcd77")), ({ra: rec}, {}))  # the first engine's records stay


class StrictHostKeys(unittest.TestCase):
    """R9: every ssh orch runs pins StrictHostKeyChecking=yes, so ~/.ssh/config cannot loosen it."""

    def test_every_ssh_site(self):
        self.assertIn("StrictHostKeyChecking=yes", orch.SSH)
        i = orch.SSH.index("StrictHostKeyChecking=yes")
        self.assertEqual(orch.SSH[i - 1], "-o")
        lx = orch.Node("h13", dict(NODES["n3"]))
        win = orch.Node("h12", {"os": "windows", "ssh": "deploy@192.0.2.12", "context": "c12"})
        for argv in (lx.sh_argv(["1"]), lx.docker_argv(["ps"])):
            self.assertEqual(argv[:len(orch.SSH)], orch.SSH)
        with FakeCmd() as fc:
            win.shell("Get-Date")
        self.assertEqual(fc.argvs[0][:len(orch.SSH)], orch.SSH)


if __name__ == "__main__":
    unittest.main()
