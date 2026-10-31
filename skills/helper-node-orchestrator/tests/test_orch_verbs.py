"""F4 verb tests: gc tmp, inventory, status, logs, nodes, plan, stop-all, uncordon/hold, selftest, argparse."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from orchtest import FakeCmd, orch  # noqa: E402

A = mock.Mock


def run(fn, a):
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        rc = fn(a)
    return rc, o.getvalue(), e.getvalue()


def ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


class GcTmp(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_dead_pid_fresh_removed_live_pid_fresh_kept(self):
        ents = [{"name": "src-999999-a.tar", "mtime": self.NOW - 5}, {"name": f"idx-{os.getpid()}-b", "mtime": self.NOW - 5},
                {"name": "j1-out.tar", "mtime": self.NOW - 60}, {"name": "j2-out.tar", "mtime": self.NOW - 3600}]
        with mock.patch.object(orch, "pid_alive", lambda p: p == os.getpid()):
            self.assertEqual(orch.select_gc_tmp(ents, [], self.NOW), ["src-999999-a.tar", "j2-out.tar"])


class Inventory(unittest.TestCase):
    def setUp(self):
        self.n = orch.Node("hz", {"os": "linux", "ssh": "hz", "context": "hz", "enabled": True})
        self.p = [mock.patch.object(orch, "nodes_all", lambda *a, **k: {"hz": self.n}),
                  mock.patch.object(orch, "list_agent", lambda *a, **k: {"ok": True, "err": "", "objects": [], "collisions": []})]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def _go(self, fps, all_=False, df_ok=True):
        calls = []

        def dk(args, **kw):
            calls.append(args)
            return orch.R(0 if df_ok else 1, "TYPE TOTAL\nImages 1\n", "boom")
        with mock.patch.object(orch, "fingerprint", side_effect=fps), mock.patch.object(self.n, "docker", dk):
            rc, out, _ = run(orch.cmd_inventory, ns(node="hz", all=all_, json=False, top=None))
        return rc, out, calls

    def test_brief_no_dash_v_and_failed_fp_no_keyerror_exit5(self):
        rc, out, calls = self._go([{"ok": False, "error": "ssh down"}, {"ok": False}])
        self.assertEqual(rc, 5)
        self.assertIn("fingerprint failed: ssh down", out)
        self.assertEqual(calls, [["system", "df"]])

    def test_brief_clean_exit0(self):
        fp = {"ok": True, "containers": {}, "services": {}, "latency": {}, "t": orch.now()}
        rc, out, calls = self._go([fp, dict(fp)])
        self.assertEqual(rc, 0)
        self.assertNotIn("-v", sum(calls, []))

    def test_after_fp_taken_even_when_df_fails(self):
        fp = {"ok": True, "containers": {}, "services": {}, "latency": {}, "t": orch.now()}
        m = mock.Mock(side_effect=[fp, dict(fp)])
        with mock.patch.object(orch, "fingerprint", m), \
                mock.patch.object(self.n, "docker", lambda *a, **k: orch.R(1, "", "x")):
            rc, _, _ = run(orch.cmd_inventory, ns(node="hz", all=True, json=False, top=None))
        self.assertEqual((rc, m.call_count), (5, 2))

    def test_worse_exit5(self):
        fp = {"ok": True, "containers": {}, "services": {}, "latency": {}, "t": orch.now()}
        with mock.patch.object(orch, "fp_diff", lambda a, b: (["c1 restarted"], [])):
            rc, out, _ = self._go([fp, dict(fp)])
        self.assertEqual(rc, 5)

    def test_all_single_df_call_with_v(self):
        fp = {"ok": True, "containers": {}, "services": {}, "latency": {}, "t": orch.now()}
        calls = []

        def dk(args, **kw):
            calls.append(args)
            return orch.R(0, json.dumps({"Images": [], "Containers": [], "Volumes": [], "BuildCache": []}))
        with mock.patch.object(orch, "fingerprint", side_effect=[fp, dict(fp)]), mock.patch.object(self.n, "docker", dk):
            rc, _, _ = run(orch.cmd_inventory, ns(node="hz", all=True, json=False, top=None))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("-v", calls[0])


class Status(unittest.TestCase):
    def test_corrupt_meta(self):
        for i, body in enumerate(['{"jobid": "x1", "job": null, "timings": null}', "[1,2]", "{broken"]):
            d = orch.HOME / "results" / f"st-{i}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(body, encoding="utf-8")
        rc, out, _ = run(orch.cmd_status, ns(local=True, last=10))
        self.assertEqual(rc, 0)
        self.assertIn("st-1", out)


class Logs(unittest.TestCase):
    def _log(self, jid, lines, meta=None):
        p = orch.HOME / "node-logs" / f"{jid}.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
        if meta is not None:
            d = orch.HOME / "results" / jid
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_default_summary_failures_tail80(self):
        self._log("lg1", [f"l{i}" for i in range(200)], {"job": "t-ok", "result": "test-fail", "failures": ["boom one"]})
        rc, out, _ = run(orch.cmd_logs, ns(jobid="lg1", tail=None, all=False))
        ol = out.splitlines()
        self.assertTrue(ol[0].startswith("lg1 job=t-ok"))
        self.assertIn("  boom one", ol)
        self.assertEqual(ol[-80:], [f"l{i}" for i in range(120, 200)])
        self.assertNotIn("l119", ol)

    def test_fallback_patterns_tail_and_all(self):
        job = "t-fake"
        self._log("lg2", ["ok"] * 5 + ["zzz"] * 5, {"job": job})
        with mock.patch.object(orch, "cfg_jobs", lambda: {"jobs": {job: {"fail_patterns": ["^zzz$"]}}}):
            rc, out, _ = run(orch.cmd_logs, ns(jobid="lg2", tail=3, all=False))
        self.assertEqual(out.count("  zzz"), 5)
        self.assertEqual(out.splitlines()[-3:], ["zzz"] * 3)
        rc, out, _ = run(orch.cmd_logs, ns(jobid="lg2", tail=None, all=True))
        self.assertEqual(len(out.splitlines()), 11)


class Nodes(unittest.TestCase):
    P = {"reach": True, "docker": True, "engine": "27", "mem_avail": 4 << 30, "mem_total": 8 << 30, "psi": None,
         "iowait": 2, "load1": 0.5, "cores": 4, "disk_free": 1 << 34, "probe_s": 0.1}

    def _ns(self):
        return {"h1": orch.Node("h1", {"os": "linux", "ssh": "h1", "enabled": True}),
                "h2": orch.Node("h2", {"os": "linux", "ssh": "h2", "enabled": False, "note": "off"})}

    def test_brief_one_line_per_node(self):
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: self._ns()), mock.patch.object(orch, "probe", lambda n: dict(self.P)), \
                mock.patch.object(orch, "admit", lambda *a, **k: {"ok": True}):
            rc, out, _ = run(orch.cmd_nodes, ns(json=False, all=False))
        ol = out.splitlines()
        self.assertEqual(rc, 0)
        self.assertTrue(ol[0].startswith("config: "))
        self.assertEqual(len(ol), 3)
        self.assertTrue(ol[1].startswith("h1 ok docker ") and "iow=2%" in ol[1])
        self.assertTrue(ol[2].startswith("h2 down disabled"))

    def test_json_pure_stdout(self):
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: self._ns()), mock.patch.object(orch, "probe", lambda n: dict(self.P)), \
                mock.patch.object(orch, "admit", lambda *a, **k: {"ok": True}):
            rc, out, err = run(orch.cmd_nodes, ns(json=True, all=False))
        for line in out.splitlines():
            json.loads(line)
        self.assertIn("config: ", err)

    def test_zero_enabled_exit2(self):
        off = {"h2": self._ns()["h2"]}
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: off):
            rc, _, err = run(orch.cmd_nodes, ns(json=False, all=False))
        self.assertEqual(rc, 2)
        self.assertIn("no enabled nodes", err)
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: {}):
            self.assertEqual(run(orch.cmd_nodes, ns(json=False, all=False))[0], 2)


class Plan(unittest.TestCase):
    def test_brief_lines_and_pick(self):
        job = next(iter(orch.cfg_jobs()["jobs"]))
        n1, n2 = orch.Node("p1", {"os": "linux", "ssh": "p1"}), orch.Node("p2", {"os": "linux", "ssh": "p2"})
        ads = {"p1": {"ok": True, "score": 0.7, "checks": []},
               "p2": {"ok": False, "score": 0, "checks": [{"check": "mem", "ok": False, "detail": "low"}]}}
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: {"p1": n1, "p2": n2}), \
                mock.patch.object(orch, "hard_filter", lambda *a: []), mock.patch.object(orch, "probe", lambda n, r: {"n": n.name}), \
                mock.patch.object(orch, "admit", lambda j, n, p: dict(ads[n.name], checks=list(ads[n.name]["checks"]))), \
                mock.patch.dict(orch.cfg_jobs()["jobs"][job], {"heavy": False}):
            rc, out, _ = run(orch.cmd_plan, ns(job=job, all=False))
            ol = out.splitlines()
            self.assertEqual(ol[1:], ["p1 admit 0.7 ok", "p2 skip 0 mem: low", "pick p1"])
            self.assertEqual(rc, 0)
            rc, out, _ = run(orch.cmd_plan, ns(job=job, all=True))
            self.assertIn("ADMITTED", out)
            self.assertEqual(out.splitlines()[-1], "pick p1")

    def test_unprobed_node_shows_a_dash_not_none(self):
        job = next(iter(orch.cfg_jobs()["jobs"]))
        n1 = orch.Node("p1", {"os": "linux", "ssh": "p1"})
        ad = {"ok": False, "score": None, "checks": [{"check": "reachable", "ok": False, "detail": "unreachable"}]}
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: {"p1": n1}), \
                mock.patch.object(orch, "hard_filter", lambda *a: []), mock.patch.object(orch, "probe", lambda n, r: {}), \
                mock.patch.object(orch, "admit", lambda j, n, p: dict(ad, checks=list(ad["checks"]))), \
                mock.patch.dict(orch.cfg_jobs()["jobs"][job], {"heavy": False}):
            rc, out, _ = run(orch.cmd_plan, ns(job=job, all=False))
        self.assertEqual(out.splitlines()[1], "p1 skip - reachable: unreachable")
        self.assertEqual(rc, orch.NOADMIT)

    def test_pick_none(self):
        job = next(iter(orch.cfg_jobs()["jobs"]))
        n1 = orch.Node("p1", {"os": "linux", "ssh": "p1"})
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: {"p1": n1}), \
                mock.patch.object(orch, "hard_filter", lambda *a: ["os mismatch"]):
            rc, out, _ = run(orch.cmd_plan, ns(job=job, all=False))
        self.assertEqual(rc, orch.NOADMIT)
        self.assertEqual(out.splitlines()[-1], "pick none: p1: os mismatch")


class StopAll(unittest.TestCase):
    def test_live_file_kept_when_kill_fails(self):
        lf = orch.live_dir() / "sa1.json"
        lf.write_text(json.dumps({"jobid": "sa1", "pid": 4242424, "node": "x"}), encoding="utf-8")
        try:
            with mock.patch.object(orch, "same_proc", lambda d: True), mock.patch.object(orch.time, "sleep", lambda s: None), \
                    mock.patch.object(orch.os, "kill", side_effect=PermissionError("denied")), \
                    FakeCmd(lambda argv, kw: orch.R(1, "", "Access denied")):
                killed, failed = orch.kill_local_jobs()
            self.assertEqual(killed, [])
            self.assertEqual(len(failed), 1)
            self.assertTrue(lf.exists())
            with mock.patch.object(orch, "same_proc", mock.Mock(side_effect=[True, False])), \
                    mock.patch.object(orch.os, "kill", lambda *a: None), FakeCmd():
                killed, failed = orch.kill_local_jobs()
            self.assertEqual((len(killed), failed), (1, []))
            self.assertFalse(lf.exists())
        finally:
            lf.unlink(missing_ok=True)


class UncordonHold(unittest.TestCase):
    def test_validation(self):
        nn = {"h1": orch.Node("h1", {"os": "linux", "ssh": "h1", "enabled": False})}
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: nn):
            self.assertEqual(run(orch.cmd_uncordon, ns(node="nope"))[0], 2)
            self.assertEqual(run(orch.cmd_hold, ns(node="nope", minutes=5))[0], 2)
            for m in (0, -1, float("nan")):
                self.assertEqual(run(orch.cmd_hold, ns(node="h1", minutes=m))[0], 2)
            self.assertEqual(run(orch.cmd_hold, ns(node="h1", minutes=1))[0], 0)
            self.assertEqual(run(orch.cmd_uncordon, ns(node="h1"))[0], 0)


class Selftest(unittest.TestCase):
    def test_finish_writes_to_home_or_out(self):
        rec = lambda *a: None  # noqa: E731
        rc, out, _ = run(lambda a: orch._selftest_finish([("x", True, "")], ["#", "", "", "", ""], [], rec), None)
        self.assertEqual(rc, 0)
        self.assertIn(str(orch.HOME / "selftest"), out)
        tgt = orch.HOME / "custom" / "ev.md"
        run(lambda a: orch._selftest_finish([("x", False, "")], ["#", "", "", "", ""], [], rec, str(tgt)), None)
        self.assertTrue(tgt.exists())


class SelftestProduction(unittest.TestCase):
    """Council 2 MF6: selftest never runs a failure-path job on a server that is also production (t-fail, t-sleep,
    t-oom, the cordon, kill and concurrency checks with t-hold), and t-ok there only when --node names it."""
    PROD = "is also a production server (role helper+prod)"

    @staticmethod
    def nodes():
        return {"h11": orch.Node("h11", {"os": "linux", "ssh": "deploy@192.0.2.11", "host": "192.0.2.11",
                                         "context": "orch-h11", "enabled": True, "role": "helper"}),
                "h12": orch.Node("h12", {"os": "linux", "ssh": "app-host", "host": "192.0.2.12", "context": "orch-h12",
                                         "enabled": True, "role": "helper+prod"})}

    def test_plan(self):
        prod = {"h12": self.PROD}
        self.assertEqual(orch.selftest_plan(["h11", "h12"], prod), (["h11"], "h11", [(
            "h12: t-ok", f"skipped: h12 {self.PROD}; selftest runs t-ok there only when you name it with --node h12")]))
        self.assertEqual(orch.selftest_plan(["h13", "h12"], prod)[1], "h13")  # the last node that is not production
        self.assertEqual(orch.selftest_plan(["h11", "h13"], {}), (["h11", "h13"], "h13", []))  # as before: up[-1]
        tok, tn, skips = orch.selftest_plan(["h12"], prod, ["h12"])
        self.assertEqual((tok, tn), (["h12"], None))  # named: t-ok yes, failure paths still never
        self.assertEqual(skips, [("failure-path jobs", f"skipped: h12 {self.PROD}; orch never runs t-fail, t-sleep, "
                                                       f"t-oom or t-hold on a production server: add a helper that is "
                                                       f"not production to test the failure paths")])
        self.assertEqual(orch.selftest_plan([], prod), ([], None, []))

    def test_production_nodes(self):
        ns_ = dict(self.nodes(), h13=orch.Node("h13", {"os": "linux", "ssh": "deploy@192.0.2.13", "host": "192.0.2.13",
                                                       "context": "orch-h13", "role": "helper"}))
        gv = orch.HOME / "guard-view.json"  # another approved setup deploys to h13's host
        gv.write_text(json.dumps({"schema": 1, "projects": [], "hosts": [
            {"name": "h13", "roles": ["prod"], "spec": "deploy@192.0.2.13", "hostname": "192.0.2.13", "port": 22,
             "ips": ["192.0.2.13"], "aliases": [], "contexts": []}]}), encoding="utf-8")
        self.addCleanup(gv.unlink, True)
        prod = orch.selftest_prod(ns_)
        self.assertEqual(sorted(prod), ["h12", "h13"])
        self.assertEqual(prod["h12"], self.PROD)
        self.assertIn("production host h13", prod["h13"])

    def selftest(self, node=None):
        calls, popens = [], []

        def fake_self(args, env=None, timeout=900):
            calls.append([str(x) for x in args])
            return 0, "{}", 0.1

        class FakePopen:
            def __init__(self, argv, **kw):
                popens.append([str(x) for x in argv])
                self.returncode = 0

            def kill(self):
                pass

            def communicate(self, timeout=None):
                return b"", None
        nn, out = self.nodes(), Path(orch.HOME) / "st" / "selftest.md"
        empty = {"ok": True, "err": "", "objects": [], "collisions": [], "foreign": []}
        with mock.patch.dict(os.environ, {}), mock.patch.object(orch, "nodes_all", lambda *a, **k: dict(nn)), \
                mock.patch.object(orch, "probe", lambda *a, **k: {"docker": True}), \
                mock.patch.object(orch, "_self", fake_self), mock.patch.object(orch.subprocess, "Popen", FakePopen), \
                mock.patch.object(orch.time, "sleep", lambda s: None), mock.patch.object(orch, "live_jobs", list), \
                mock.patch.object(orch, "phantom_config", lambda d: ([], None)), \
                mock.patch.object(orch, "list_agent", lambda *a, **k: dict(empty)):
            _, o, _ = run(orch.cmd_selftest, ns(unit_only=False, quick=True, node=node, out=str(out)))
        return calls, popens, o

    def test_failure_paths_never_reach_a_production_node(self):
        calls, popens, out = self.selftest()
        runs = [c for c in calls if c[0] == "run"]
        self.assertIn(["run", "t-ok", "--node", "h11", "--wait", "0"], runs)
        self.assertFalse([c for c in calls if "h12" in c])  # not even t-ok: h12 was not named
        fail = [c for c in runs if c[1] in ("t-fail", "t-sleep", "t-oom", "t-hold")]
        self.assertEqual(sorted(c[1] for c in fail), ["t-fail", "t-oom", "t-sleep"])
        self.assertEqual({c[c.index("--node") + 1] for c in fail}, {"h11"})
        self.assertEqual([c[:2] for c in calls if c[0] in ("cordon", "reconcile")],
                         [["cordon", "h11"], ["reconcile", "--node"], ["reconcile", "--node"]])
        self.assertTrue(popens)  # the kill and concurrency checks (t-hold) went to h11 only
        self.assertTrue(all("t-hold" in p and "h11" in p and "h12" not in p for p in popens), popens)
        self.assertIn(f"[SKIP] h12: t-ok: skipped: h12 {self.PROD}; selftest runs t-ok there only when you name it "
                      f"with --node h12", out)

    def test_a_named_production_node_runs_t_ok_only(self):
        calls, popens, out = self.selftest(node=["h12"])
        self.assertEqual([c for c in calls if c[0] == "run"], [["run", "t-ok", "--node", "h12", "--wait", "0"]])
        self.assertEqual((popens, [c for c in calls if c[0] in ("cordon", "reconcile")]), ([], []))
        self.assertIn(f"[SKIP] failure-path jobs: skipped: h12 {self.PROD}; orch never runs t-fail", out)


class Argparse(unittest.TestCase):
    def test_new_flags(self):
        seen = {}

        def grab(a):
            seen[a.verb] = a
            return 0
        fns = {k: grab for k in ("cmd_run", "cmd_nodes", "cmd_plan", "cmd_logs", "cmd_selftest")}
        with mock.patch.multiple(orch, **fns), mock.patch.object(orch.atexit, "register"), \
                mock.patch.object(orch.signal, "signal"):
            for argv in (["run", "t-ok", "-v"], ["nodes", "--all"], ["plan", "t-ok", "--all"], ["logs", "j", "--all"],
                         ["selftest", "--out", "x.md"]):
                orch.main(argv)
        self.assertTrue(seen["run"].verbose and seen["nodes"].all and seen["plan"].all and seen["logs"].all)
        self.assertEqual(seen["selftest"].out, "x.md")
        self.assertIsNone(seen["logs"].tail)


if __name__ == "__main__":
    unittest.main()
