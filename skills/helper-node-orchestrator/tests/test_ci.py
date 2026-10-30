"""orch ci: stage ordering, parallel children, worst exit, --keep-going, --jobs, child result checks, and lock
contention: real child processes, but each child is a stub (ORCH_CI_CHILD, test-only) that loads orch.py and takes
orch's FileLock on the shared ORCH_HOME. No network, ssh or docker."""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import _thread
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from orchtest import JOBS, ROOT, orch, write_config  # noqa: E402

STUB = r'''
import importlib.util, json, os, sys, time
spec = importlib.util.spec_from_file_location("orch", os.environ["CI_STUB_ORCH"])
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)
job, argv = sys.argv[1], sys.argv[1:]
plan = json.loads(os.environ.get("CI_STUB_PLAN", "{}")).get(job, {})


def mark(ev):
    with orch.FileLock(timeout=60):
        with open(os.environ["CI_STUB_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"job": job, "ev": ev, "t": time.time(), "pid": os.getpid(), "argv": argv}) + "\n")


mark("start")
for _ in range(int(plan.get("bumps", 0))):  # read-modify-write under orch's lock: a lost update = a broken lock
    with orch.FileLock(timeout=60):
        p = os.path.join(os.environ["ORCH_HOME"], "ci-counter")
        n = int(open(p).read()) if os.path.exists(p) else 0
        time.sleep(0.002)
        with open(p, "w") as f:
            f.write(str(n + 1))
time.sleep(float(plan.get("sleep", 0.8)))
mark("end")
rc = int(plan.get("exit", 0))
res = {0: "pass", 1: "test-fail", 2: "usage", 3: "oom", 4: "timeout", 5: "infra", 6: "r1-guard", 75: "not-admitted"}.get(rc, "x")
if plan.get("warn"):
    sys.stderr.write("orch[00:00:00] warn: n1: metric missing: ncpu\n")
if not plan.get("no_json"):
    print(json.dumps({"result": res, "exit": plan.get("json_exit", rc), "job": job, "jobid": "j-" + job, "node": "n1",
                      "wall_s": 1.4, "failures": plan.get("failures", []), "reason": None if rc == 0 else "reason " + job,
                      "sha": "a" * 40, "wt": "--wt" in argv}))
sys.exit(rc)
'''


def job(stage=None):
    j = dict(JOBS["t-ok"])
    if stage:
        j["stage"] = stage
    return j


CI_JOBS = {"lint1": job("lint"), "unit1": job("unit"), "unit2": job("unit"), "img1": job("image"), "bld1": job("build"),
           "e2e1": job("e2e"), "legacy1": job(), "t-ok": job()}


def args(**kw):
    return SimpleNamespace(**dict({"jobs": None, "stages": None, "ref": None, "wt": False, "keep_going": False,
                                   "wait": 600}, **kw))


class CiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orchci-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = write_config(self.tmp / "cfg", jobs=CI_JOBS)
        stub = self.tmp / "stub.py"
        stub.write_text(STUB, encoding="utf-8")
        self.log = self.tmp / "log.jsonl"
        self.env = {"ORCH_CONFIG": str(self.cfg), "ORCH_CI_CHILD": json.dumps([sys.executable, "-B", str(stub)]),
                    "CI_STUB_ORCH": str(ROOT / "scripts" / "orch.py"), "CI_STUB_LOG": str(self.log)}
        (orch.HOME / "ci-counter").unlink(missing_ok=True)

    def ci(self, plan=None, **kw):
        o, e = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, dict(self.env, CI_STUB_PLAN=json.dumps(plan or {}))), \
                redirect_stdout(o), redirect_stderr(e):
            rc = orch.cmd_ci(args(**kw))
        lines = o.getvalue().strip().splitlines()
        return rc, lines[:-1], json.loads(lines[-1])

    def events(self):
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()]

    def times(self, ev, jobs):
        return [e["t"] for e in self.events() if e["ev"] == ev and e["job"] in jobs]


class WorstExit(unittest.TestCase):
    def test_table(self):
        for codes, want in (([], 0), ([0, 0], 0), ([0, 1], 1), ([1, 4], 4), ([4, 3], 3), ([3, 75], 75), ([75, 2], 2),
                            ([2, 5], 5), ([5, 6], 6), ([1, 3, 4], 3), ([0, 7], 5), ([127], 5), ([6, 7], 6)):
            self.assertEqual(orch.worst_exit(codes), want, codes)

    def test_child_argv(self):
        with mock.patch.dict(os.environ, {"ORCH_CI_CHILD": ""}):
            a = orch.ci_child_argv("app-test", args(ref="abc"))
        self.assertEqual(a[:2], [sys.executable, "-u"])
        self.assertTrue(a[2].endswith("orch.py"))
        self.assertEqual(a[3:], ["run", "app-test", "--wait", "600", "--ref", "abc"])
        with mock.patch.dict(os.environ, {"ORCH_CI_CHILD": "stub-orch --flag"}):
            self.assertEqual(orch.ci_child_argv("j", args(wt=True, wait=5)), ["stub-orch", "--flag", "j", "--wait", "5", "--wt"])
        with mock.patch.dict(os.environ, {"ORCH_CI_CHILD": "[not json"}):
            with self.assertRaisesRegex(orch.OrchError, "ORCH_CI_CHILD must be a JSON array"):
                orch.ci_child_argv("j", args())

    def test_ci_result(self):
        line = json.dumps({"result": "pass", "exit": 0, "job": "j"})
        self.assertEqual(orch.ci_result("j", 0, "noise\n" + line + "\n", "")["result"], "pass")
        d = orch.ci_result("j", 1, "", "Traceback\nKeyError: x\n")
        self.assertEqual((d["result"], d["exit"]), ("infra", 5))
        self.assertIn("printed no result line (exit 1) KeyError: x", d["reason"])
        d = orch.ci_result("j", 1, line, "")
        self.assertEqual((d["exit"], d["reason"]), (5, "orch run exit 1 does not match its result line (pass, exit 0)"))
        d = orch.ci_result("j", 7, json.dumps({"result": "x", "exit": 7}), "")
        self.assertEqual(d["exit"], 5)

    def test_stop_falls_back_to_terminate(self):
        p = mock.Mock()
        p.send_signal.side_effect = OSError("no console")
        orch._ci_stop(p)
        p.terminate.assert_called_once_with()


class Ci(CiBase):
    def test_order_parallel_and_lines(self):
        rc, lines, final = self.ci()
        self.assertEqual(rc, 0)
        g1, g2, g3 = ["lint1", "unit1", "unit2"], ["img1", "bld1"], ["e2e1"]
        self.assertLess(max(self.times("start", g1)), min(self.times("end", g1)), "group 1 did not run in parallel")
        self.assertLess(max(self.times("start", g2)), min(self.times("end", g2)), "group 2 did not run in parallel")
        self.assertGreater(min(self.times("start", g2)), max(self.times("end", g1)))
        self.assertGreater(min(self.times("start", g3)), max(self.times("end", g2)))
        self.assertNotIn("legacy1", {e["job"] for e in self.events()})  # no stage: only with --jobs
        self.assertEqual(lines, ["lint1 n1 pass 1s | - | next: build+image", "unit1 n1 pass 1s | - | next: build+image",
                                 "unit2 n1 pass 1s | - | next: build+image", "img1 n1 pass 1s | - | next: e2e",
                                 "bld1 n1 pass 1s | - | next: e2e", "e2e1 n1 pass 1s | - | next: done"])
        self.assertEqual((final["result"], final["exit"], final["next"]), ("pass", 0, "done"))
        self.assertEqual([j["job"] for j in final["jobs"]], g1 + g2 + g3)
        self.assertEqual(list(final["jobs"][0]), list(orch.CI_KEYS))
        self.assertEqual((final["jobs"][0]["stage"], final["jobs"][0]["sha"], final["jobs"][0]["jobid"]), ("lint", "a" * 40, "j-lint1"))
        argvs = [e["argv"] for e in self.events() if e["ev"] == "start"]
        self.assertTrue(all(a[1:] == ["--wait", "600"] for a in argvs), argvs)

    def test_red_stage_stops_unless_keep_going(self):
        plan = {"unit1": {"exit": 1, "failures": ["FAILED test_a.py::test_x", "FAILED test_b.py::test_y"]}}
        rc, lines, final = self.ci(plan, stages="unit,image,e2e")
        self.assertEqual((rc, final["result"], final["next"]), (1, "test-fail", "Development"))
        self.assertEqual({e["job"] for e in self.events()}, {"unit1", "unit2"})
        self.assertIn("unit1 n1 test-fail 1s | reason unit1 | next: Development", lines)
        self.assertIn("failed: FAILED test_a.py::test_x, FAILED test_b.py::test_y", lines)
        self.assertIn("img1 - skipped 0s | not run: unit is red (--keep-going runs it) | next: -", lines)
        self.assertEqual([j["result"] for j in final["jobs"]], ["test-fail", "pass", "skipped", "skipped"])
        self.assertIsNone(final["jobs"][2]["exit"])
        self.log.unlink()
        rc, lines, final = self.ci(plan, stages="unit,image,e2e", keep_going=True)
        self.assertEqual(rc, 1)
        self.assertEqual({e["job"] for e in self.events()}, {"unit1", "unit2", "img1", "e2e1"})
        self.assertNotIn("skipped", " ".join(lines))

    def test_e2e_failure_goes_to_investigation(self):  # review round 2 Q17: SKILL.md "Development (e2e: Investigation)"
        rc, lines, final = self.ci({"e2e1": {"exit": 1}}, stages="e2e")
        self.assertEqual(rc, 1)
        self.assertEqual(lines, ["e2e1 n1 test-fail 1s | reason e2e1 | next: Investigation"])
        self.assertEqual((final["next"], final["jobs"][0]["next"], orch.JOURNAL["next"]), ("Investigation",) * 3)
        self.assertEqual(self.ci({"e2e1": {"exit": 3}}, stages="e2e")[2]["next"], "Development")  # oom keeps its row
        rc, lines, final = self.ci({"unit1": {"exit": 1}, "e2e1": {"exit": 1}}, stages="unit,e2e", keep_going=True)
        self.assertEqual([j["next"] for j in final["jobs"] if j["exit"] == 1], ["Development", "Investigation"])
        self.assertEqual(final["next"], "Development")  # the final next is the first worst job's line

    def test_worst_exit_of_a_group(self):
        rc, lines, final = self.ci({"unit1": {"exit": 1}, "unit2": {"exit": 5}, "lint1": {"exit": 3}}, stages="lint,unit")
        self.assertEqual((rc, final["result"], final["exit"], final["next"]), (5, "infra", 5, "stop"))
        rc, _, final = self.ci({"unit1": {"exit": 75}, "unit2": {"exit": 4}}, stages="unit")
        self.assertEqual((rc, final["result"], final["next"]), (75, "not-admitted", "stop"))

    def test_jobs_any_stage_unstaged_last(self):
        rc, lines, final = self.ci(jobs="legacy1, unit1,legacy1", wt=True)
        self.assertEqual(rc, 0)
        self.assertEqual([j["job"] for j in final["jobs"]], ["unit1", "legacy1"])
        self.assertGreater(min(self.times("start", ["legacy1"])), max(self.times("end", ["unit1"])))
        self.assertEqual(lines[0], "unit1 n1 pass 1s | - | next: legacy1")
        self.assertTrue(all(e["argv"][-1] == "--wt" for e in self.events()))
        self.assertIs(final["jobs"][0]["wt"], True)

    def test_usage_errors(self):
        for kw, want in (({"jobs": "nope,unit1"}, "unknown job nope; known: "), ({"jobs": " , "}, "needs at least one job"),
                         ({"stages": "unit,deploy"}, "unknown stage deploy; valid: lint, unit, build, image, e2e"),
                         ({"stages": "investigate"}, "unknown stage investigate")):
            with mock.patch.dict(os.environ, self.env), self.assertRaisesRegex(orch.OrchError, want):
                orch.cmd_ci(args(**kw))
        d = self.tmp / "only-legacy"
        write_config(d, jobs={"t-ok": job(), "legacy1": job()})
        with mock.patch.dict(os.environ, dict(self.env, ORCH_CONFIG=str(d))), \
                self.assertRaisesRegex(orch.OrchError, "jobs without a stage run only when named: orch ci --jobs"):
            orch.cmd_ci(args())
        for argv in (["ci", "--jobs", "unit1", "--stages", "unit"], ["ci", "--ref", "x", "--wt"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                orch.main(argv)
            self.assertEqual(e.exception.code, 2)

    def test_bad_children_are_infra(self):
        rc, lines, final = self.ci({"unit1": {"no_json": True}, "unit2": {"exit": 1, "json_exit": 0}}, stages="unit")
        self.assertEqual(rc, 5)
        r = {j["job"]: j for j in final["jobs"]}
        self.assertEqual((r["unit1"]["result"], r["unit2"]["result"]), ("infra", "infra"))
        self.assertIn("printed no result line (exit 0)", r["unit1"]["reason"])
        self.assertIn("does not match its result line", r["unit2"]["reason"])
        with mock.patch.dict(os.environ, dict(self.env, ORCH_CI_CHILD=json.dumps([str(self.tmp / "no-such-exe")]))), \
                redirect_stdout(io.StringIO()) as o, redirect_stderr(io.StringIO()):
            rc = orch.cmd_ci(args(stages="lint"))
        self.assertEqual(rc, 5)
        self.assertIn("could not start orch run", json.loads(o.getvalue().splitlines()[-1])["jobs"][0]["reason"])

    def test_warn_lines(self):
        rc, lines, _ = self.ci({"lint1": {"warn": True}}, stages="lint")
        self.assertEqual(lines, ["lint1 n1 pass 1s | - | next: done", "warn: n1: metric missing: ncpu"])

    def test_lock_contention_all_finish(self):
        names = [f"u{i}" for i in range(6)]
        write_config(self.cfg, jobs=dict(CI_JOBS, **{n: job("unit") for n in names}))
        t0 = time.time()
        rc, lines, final = self.ci({n: {"bumps": 10, "sleep": 1.5} for n in names}, jobs=",".join(names))
        self.assertEqual(rc, 0, lines)
        self.assertLess(time.time() - t0, 90)
        self.assertEqual(len(self.times("end", names)), 6)
        self.assertEqual(int((orch.HOME / "ci-counter").read_text()), 60)  # 6 children x 10 locked increments, none lost
        self.assertEqual([j["result"] for j in final["jobs"]], ["pass"] * 6)
        self.assertLess(max(self.times("start", names)), min(self.times("end", names)))  # they really overlapped

    def test_interrupt_stops_children(self):
        threading.Timer(2.0, _thread.interrupt_main).start()
        t0 = time.time()
        rc, lines, final = self.ci({"unit1": {"sleep": 60}, "unit2": {"sleep": 60}}, stages="unit")
        self.assertLess(time.time() - t0, 50)
        self.assertEqual((rc, final["exit"], final["next"]), (5, 5, "stop"))
        self.assertTrue(final["reason"].startswith("interrupted"))
        pids = [e["pid"] for e in self.events() if e["ev"] == "start"]
        self.assertEqual(len(pids), 2)
        time.sleep(0.5)
        self.assertFalse(any(orch.pid_alive(p) for p in pids), "a ci child outlived the interrupt")


if __name__ == "__main__":
    unittest.main()
