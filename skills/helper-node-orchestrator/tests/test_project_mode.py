"""Project mode in orch.py (design sections 4, 7 and 13): the approved setup compiled into the engine config, the
refusals with their "run orch init" hint, runs.jsonl records, the journal hook, the new verbs (argparse and their
modules), ssh-docker with the helper's default docker, and the offline parts of the dev runtime harnesses.

Real orch_config on temp git repos (local git only); fakes for orch_init, orch_deploy and orch_journal through
sys.modules; no network, ssh or docker."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import orchtest
from orchtest import ROOT, TEST_ENV, FakeCmd, orch

sys.path.insert(0, str(ROOT / "scripts"))
import orch_config as oc  # noqa: E402  (imported before any sys.modules patch: one module object for every test)

GIT = shutil.which("git")
PIN = "@sha256:" + "a" * 64
IMG = "python:3.12-slim" + PIN
SA12 = ["-p", "2222", "deploy@192.0.2.12"]
EP = {"name": "app1-web :8080", "url": "http://127.0.0.1:8080/", "insecure": False}
RUN_REC_KEYS = {"job", "sha", "wt", "node", "result", "exit", "wall_s", "jobid", "ts"}


def tmpdir(t: unittest.TestCase) -> Path:
    d = Path(tempfile.mkdtemp(prefix="orchpm-")).resolve()
    t.addCleanup(shutil.rmtree, d, True)
    return d


def git_init(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
    return d


def rec(host, transport="context", ctx=None, docker_host=None, ok=True, eps=(), problem=None):
    return {"ok": ok, "probed": "2026-10-30T09:00:00", "spec": host,
            "resolved": {"hostname": host, "user": "deploy", "port": 22, "alias": False},
            "facts": {"os": "linux", "arch": "x86_64", "cpus": 8, "mem_bytes": 16500000000, "disk_free_bytes": 4e11,
                      "docker": "27.3.1", "docker_id": "id-" + host, "rootless": False, "buildx": True, "psi": True,
                      "cgroup": 2, "cotenants": [], "endpoints": list(eps)},
            "transport": transport, "context": ctx, "docker_host": docker_host, "problem": problem}


def setup_cfg() -> dict:
    return {"schema": 1,
            "hosts": {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"], "jobs": "all"},
                      "h12": {"ssh": "deploy@192.0.2.12:2222", "roles": ["helper", "prod"], "jobs": "tests",
                              "watch": ["https://127.0.0.1/app/"]}},
            "production": {"answer": "set"},
            "deploy": {"command": "./scripts/deploy.sh", "rollback": None, "health": ["https://shop.example/health"]},
            "alerts": {"command": None},
            "jobs": {"app-unit": {"stage": "unit", "image": IMG, "dir": "app", "prep": "pip install -r requirements.txt",
                                  "cmd": "python -m pytest -q", "memory": "2g", "timeout": 600,
                                  "detected": "python 3.12 (app/pyproject.toml)"},
                     "app-image": {"kind": "build", "stage": "image", "dir": "app", "dockerfile": "Dockerfile",
                                   "memory": "4g", "timeout": 1200}},
            "policy": {}}


def hosts_state() -> dict:
    return {"h11": rec("192.0.2.11", ctx="orch-h11"),
            "h12": rec("192.0.2.12", transport="ssh-docker", docker_host="unix:///run/docker.sock", eps=[EP])}


def run_main(argv, stdin=""):
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e), mock.patch.object(orch.atexit, "register"), \
            mock.patch.object(orch.signal, "signal"), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
        rc = orch.main(argv)
    return rc, o.getvalue(), e.getvalue()


def last_json(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


def meta(jobid="j1", node="h11", result="pass", exit_=0, **kw):
    return dict({"jobid": jobid, "job": "t-ok", "node": node, "result": result, "exit": exit_, "wall_s": 1.5,
                 "wait_s": 0.2, "overhead_s": 0.3, "reason": None, "failures": [], "sha": None, "wt": False}, **kw)


class ProjectCase(unittest.TestCase):
    """A temp git project with an approved two-helper setup and its probe state; env in project mode (ORCH_CONFIG
    unset, ORCH_PROJECT = the project). The journal hook is off (ORCH_JOURNAL=0) unless the class turns it on."""
    JOURNAL_ON = False

    def setUp(self):
        if not GIT:
            self.skipTest("needs git")
        self.tmp = tmpdir(self)
        self.root = git_init(self.tmp / "shop")
        self.p, _ = oc.init_project(self.root, env={})
        self.cfg = setup_cfg()
        oc.save_config(self.p, self.cfg)
        oc.save_state(self.p, "hosts", hosts_state())
        oc.trust_set(orch.HOME, self.p, self.cfg, {})
        self.env(ORCH_PROJECT=str(self.root))

    def env(self, **kw):
        env = {k: v for k, v in os.environ.items() if k not in ("ORCH_CONFIG", "ORCH_JOURNAL", "ORCH_PROJECT")}
        env.update({} if self.JOURNAL_ON else {"ORCH_JOURNAL": "0"}, **kw)
        pe = mock.patch.dict(os.environ, env, clear=True)
        pe.start()
        self.addCleanup(pe.stop)
        orch.reset_project()
        self.addCleanup(orch.reset_project)
        orch.JOURNAL.clear()
        self.addCleanup(orch.JOURNAL.clear)

    def save(self, cfg=None, hosts=None, approve=False):
        if cfg is not None:
            oc.save_config(self.p, cfg)
        if hosts is not None:
            oc.save_state(self.p, "hosts", hosts)
        if approve:
            oc.trust_set(orch.HOME, self.p, cfg, {})
        orch.reset_project()


class Loading(ProjectCase):
    def test_compiled_setup_reaches_the_engine(self):
        n = orch.cfg_nodes()
        self.assertEqual(sorted(n["nodes"]), ["h11", "h12"])
        self.assertEqual(n["admission"], oc.DEFAULT_POLICY["admission"])
        h11, h12 = n["nodes"]["h11"], n["nodes"]["h12"]
        self.assertEqual((h11["enabled"], h11["transport"], h11["context"], h11["ssh_args"]),
                         (True, "context", "orch-h11", ["deploy@192.0.2.11"]))
        self.assertEqual((h12["enabled"], h12["transport"], h12["docker_host"], h12["ssh_args"]),
                         (True, "ssh-docker", "unix:///run/docker.sock", SA12))
        self.assertIn("app-unit", h12["opt_in"])
        self.assertNotIn("app-image", h12["opt_in"])  # helper + prod: tests scope, no image builds
        j = orch.cfg_jobs()
        self.assertEqual(j["repos"], {".": self.root.as_posix()})
        self.assertEqual(j["jobs"]["app-unit"]["cmd"], "mkdir -p /out && cd /src/app && python -m pytest -q")
        self.assertEqual(j["jobs"]["app-image"]["context"], "app")
        self.assertTrue({"t-ok", "t-fail", "shell-investigate"} <= set(j["jobs"]))
        self.assertEqual((j["images"][IMG], orch.images_cfg()[IMG]), (IMG, "python" + PIN))
        prot = orch.cfg_prot()
        self.assertEqual([e["url"] for e in prot["nodes"]["h12"]["endpoints"]],
                         ["http://127.0.0.1:8080/", "https://127.0.0.1/app/"])
        self.assertEqual(prot["alert"]["command"], None)
        lab = f"project {self.root.as_posix()}"
        self.assertEqual(orch.config_line(), f"config: {lab}")
        self.assertEqual(Path(orch.PROJECT["p"].store), Path(self.p.store))
        nodes = orch.nodes_all()
        self.assertEqual(nodes["h12"].docker_argv(["ps"]), orch.SSH + SA12 + ["docker -H unix:///run/docker.sock ps"])
        self.assertEqual(nodes["h11"].docker_argv(["ps"]), ["docker", "--context", "orch-h11", "ps"])
        self.assertEqual(orch.Job("app-unit", []).get("stage"), "unit")

    def test_compiled_once_per_process(self):
        real, calls = oc.compile_engine, []

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)
        with mock.patch.object(oc, "compile_engine", counting):
            for _ in range(3):
                orch.cfg_nodes(), orch.cfg_jobs(), orch.cfg_prot()
            self.assertEqual(len(calls), 1)
            orch.reset_project()
            orch.cfg_nodes()
            self.assertEqual(len(calls), 2)

    def test_failed_probe_disables_the_helper_only(self):
        hs = hosts_state()
        hs["h12"] = rec("192.0.2.12", ok=False, problem={"what": "ssh: Permission denied (publickey)", "tried": "-",
                                                          "fix": "ssh-copy-id -p 2222 deploy@192.0.2.12"})
        self.save(hosts=hs)
        n = orch.cfg_nodes()["nodes"]
        self.assertTrue(n["h11"]["enabled"])
        self.assertFalse(n["h12"]["enabled"])
        self.assertIn("probe failed: ssh: Permission denied", n["h12"]["note"])

    def test_non_git_root_cannot_ship_source(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        p, warns = oc.init_project(plain, env={})
        self.assertFalse(p.git)
        oc.save_config(p, setup_cfg())
        oc.save_state(p, "hosts", hosts_state())
        oc.trust_set(orch.HOME, p, setup_cfg(), {})
        self.env(ORCH_PROJECT=str(plain))
        a = SimpleNamespace(job="app-unit", args=[], lease=0, heartbeat=0, ref=None, wt=False, verbose=False)
        with self.assertRaisesRegex(orch.OrchError, f"{plain.as_posix()} is not a git repository, so orch cannot ship "
                                                    f"the source of app-unit \\(git archive\\): make it one"):
            orch.cmd_run(a)
        self.assertEqual(orch.Job("t-ok", []).get("source"), "none")  # built-in jobs still run

    def test_unpinned_job_is_named_with_the_fix(self):
        cfg = setup_cfg()
        cfg["jobs"]["web-unit"] = {"stage": "unit", "image": "node:22-bookworm-slim", "cmd": "npm test"}
        self.save(cfg, approve=True)
        self.assertNotIn("web-unit", orch.cfg_jobs()["jobs"])
        with self.assertRaisesRegex(orch.OrchError, "job web-unit cannot run yet: its image node:22-bookworm-slim is "
                                                    "not pinned to a digest; run orch init --approve"):
            orch.Job("web-unit", [])
        with self.assertRaisesRegex(orch.OrchError, "unknown job nope; known: "):
            orch.Job("nope", [])
        with redirect_stderr(io.StringIO()), self.assertRaisesRegex(orch.OrchError, "job web-unit cannot run yet"):
            orch.cmd_ci(SimpleNamespace(jobs="web-unit", stages=None, ref=None, wt=False, keep_going=False, wait=1))


class Refusals(ProjectCase):
    def refused(self, pattern, detail=None):
        with self.assertRaisesRegex(orch.NoSetup, pattern) as cm:
            orch.cfg_nodes()
        if detail:
            self.assertRegex(cm.exception.detail or "", detail)
        for argv in (["nodes"], ["plan", "t-ok"], ["run", "t-ok"], ["ci"], ["selftest", "--unit-only"]):
            rc, out, err = run_main(argv)
            self.assertEqual(rc, 2, argv)
            j = last_json(out)
            self.assertEqual((j["result"], j["exit"], j["next"]), ("usage", 2, "run orch init"), argv)
            self.assertRegex(j["error"], pattern)
            self.assertIn("orch: " + j["error"], err.splitlines())

    def test_no_project(self):
        other = git_init(self.tmp / "other")
        self.env(ORCH_PROJECT=str(other))
        self.refused("^no orch setup here: run orch init$", f"looked for a project at {other.as_posix()}")

    def test_not_approved_yet(self):
        p2, _ = oc.init_project(git_init(self.tmp / "fresh"), env={})
        oc.save_config(p2, setup_cfg())
        oc.save_state(p2, "hosts", hosts_state())
        self.env(ORCH_PROJECT=str(p2.root))
        self.refused(f"the orch setup of {p2.root.as_posix()} is not approved yet: run orch init to review and "
                     f"approve it")

    def test_changed_since_approval(self):
        cfg = setup_cfg()
        cfg["hosts"]["h11"]["jobs"] = "tests"
        self.save(cfg)
        self.refused(f"the orch setup of {self.root.as_posix()} changed since it was approved: run orch init")

    def test_no_helper(self):
        cfg = setup_cfg()
        cfg["hosts"] = {"p20": {"ssh": "deploy@192.0.2.20:2222", "roles": ["prod"]}}
        self.save(cfg, approve=True)
        self.refused("has no helper server: run orch init to add one")

    def test_helper_never_probed(self):
        self.save(hosts={"h11": hosts_state()["h11"]})
        self.refused(r"^helper h12 \(deploy@192.0.2.12:2222\) has not been probed yet: run orch init")
        self.save(hosts={})
        self.refused(r"^helpers h11 \(deploy@192.0.2.11\), h12 \(deploy@192.0.2.12:2222\) have not been probed")

    def test_broken_config_or_state(self):
        (Path(self.p.store) / "state" / "hosts.json").write_text("{not json", encoding="utf-8")
        orch.reset_project()
        self.refused("have not been probed yet")  # unreadable observations = no observations
        (Path(self.p.store) / "config.json").write_text("{not json", encoding="utf-8")
        orch.reset_project()
        with self.assertRaisesRegex(orch.NoSetup, "config.json"):
            orch.cfg_nodes()

    def test_maintenance_verbs_use_the_last_approved_setup(self):
        cfg = setup_cfg()
        cfg["hosts"]["h13"] = {"ssh": "deploy@192.0.2.13", "roles": ["helper"], "jobs": "all"}  # unapproved, unprobed
        self.save(cfg)
        for verb in ("gc", "reconcile", "stop-all", "status"):
            orch.reset_project()
            with mock.patch.dict(orch.VERB, {"name": verb}), redirect_stderr(io.StringIO()) as e:
                self.assertEqual(sorted(orch.cfg_nodes()["nodes"]), ["h11", "h12"], verb)
            self.assertIn("changed since it was approved; orch " + verb + " uses the setup approved", e.getvalue())
        orch.reset_project()
        with self.assertRaisesRegex(orch.NoSetup, "changed since it was approved"):
            orch.cfg_nodes()  # outside main (VERB None) = strict
        p2, _ = oc.init_project(git_init(self.tmp / "never"), env={})
        oc.save_config(p2, setup_cfg())
        self.env(ORCH_PROJECT=str(p2.root))
        with mock.patch.dict(orch.VERB, {"name": "gc"}), self.assertRaisesRegex(orch.NoSetup, "not approved yet"):
            orch.cfg_nodes()

    def test_gc_main_runs_on_the_approved_setup(self):
        cfg = setup_cfg()
        cfg["hosts"]["h11"]["jobs"] = "tests"
        self.save(cfg)
        seen = {}

        def gc(a):
            seen["nodes"] = sorted(orch.nodes_all())
            return 0
        with mock.patch.object(orch, "cmd_gc", gc):
            rc, out, err = run_main(["gc", "--session-end"])
        self.assertEqual((rc, seen["nodes"]), (0, ["h11", "h12"]))
        self.assertIn("orch gc uses the setup approved", err)


class GcEnvNodeProjectMode(ProjectCase):
    """Review round 2 Q4: in project mode ORCH_GC_NODE_JSON never supplies a node, test mode or not: gc cleans only the
    helpers orch compiles from the approved setup, and an endpoint in the JSON is never used."""
    PROD = {"enabled": True, "os": "linux", "host": "192.0.2.20", "ssh": "deploy@192.0.2.20",
            "ssh_args": ["deploy@192.0.2.20"], "context": None, "transport": "ssh-docker", "docker_host": None,
            "tags": ["docker", "linux-host"], "opt_in": ["t-ok"], "io": None}

    def gc(self, name, node, **env):
        seen = []

        def gc_node(n):
            seen.append((n.name, n.ssh_args, n.ctx))
            return {"node": n.name, "removed": [], "kept": [], "error": None, "collisions": [], "live": []}
        la = {"ok": True, "err": "", "collisions": [], "objects": []}
        self.env(ORCH_PROJECT=str(self.root), ORCH_GC_NODE_JSON=json.dumps({"name": name, "node": node}), **env)
        with mock.patch.object(orch, "gc_node", gc_node), mock.patch.object(orch, "list_agent", lambda *a, **k: la), \
                mock.patch.object(orch, "cmd") as cmd:
            rc, out, _ = run_main(["gc", "--session-end", "--node", name])
        self.assertEqual(cmd.call_count, 0)  # no ssh, no docker
        return rc, out, seen

    def test_a_node_outside_the_approved_setup_is_refused(self):
        for env in ({}, {"ORCH_TEST_MODE": "0"}):  # the suites run with ORCH_TEST_MODE=1: it opens nothing here
            rc, out, seen = self.gc("h7", self.PROD, **env)
            self.assertEqual((rc, seen), (2, []), env)
            self.assertIn("node h7 is not in it: orch cleans only the helpers of the approved setup, as orch compiles "
                          "them, and ORCH_GC_NODE_JSON never adds or changes one", last_json(out)["error"])

    def test_a_helper_of_the_setup_comes_from_orchs_own_compile(self):
        rc, out, seen = self.gc("h11", dict(self.PROD))  # the JSON points h11 at another endpoint: never used
        self.assertEqual((rc, seen), (0, [("h11", ["deploy@192.0.2.11"], "orch-h11")]))


class CiLeftOutJobs(ProjectCase):
    """Review round 2 Q9: plain orch ci names the jobs of the chosen stages that wait for an image pin (with the fix),
    blames no job of another stage, and never reports a stage green without its job."""
    MSG = ("job {} cannot run yet: its image python:3.13-slim is not pinned to a digest; run orch init --approve (it "
           "pins images)")

    def ci(self, *argv):
        calls = []

        def group(names, a):
            calls.append(list(names))
            return [(n, 0, json.dumps({"result": "pass", "exit": 0, "job": n, "node": "h11", "wall_s": 2.0}), "")
                    for n in names]
        with mock.patch.object(orch, "ci_run_group", group):
            rc, out, _ = run_main(["ci", *argv])
        return rc, out.strip().splitlines(), calls

    def test_only_unpinned_jobs_in_the_stages_are_named_with_the_fix(self):
        cfg = setup_cfg()
        cfg["jobs"] = {"shop-test": {"stage": "unit", "image": "python:3.13-slim", "cmd": "python -m pytest -q"}}
        self.save(cfg, approve=True)
        rc, lines, calls = self.ci()
        self.assertEqual((rc, calls), (2, []))
        self.assertEqual(last_json(lines[-1])["error"], self.MSG.format("shop-test"))
        rc, lines, _ = self.ci("--stages", "lint")  # an unpinned job of another stage is not the reason
        self.assertEqual(rc, 2)
        self.assertEqual(last_json(lines[-1])["error"], "no job has a stage in lint; jobs without a stage run only when "
                                                        "named: orch ci --jobs <job,job>")

    def test_a_stage_never_passes_without_its_unpinned_job(self):
        cfg = setup_cfg()
        cfg["jobs"]["app-unit"]["image"] = "python:3.13-slim"
        self.save(cfg, approve=True)
        rc, lines, calls = self.ci()
        self.assertEqual((rc, calls), (2, []))  # app-image waits behind the red unit stage, as after any refusal
        self.assertEqual(lines[:2], [f"app-unit - usage 0s | {self.MSG.format('app-unit')} | next: run orch init",
                                     "app-image - skipped 0s | not run: unit is red (--keep-going runs it) | next: -"])
        final = last_json(lines[-1])
        self.assertEqual((final["result"], final["exit"], final["next"]), ("usage", 2, "run orch init"))
        rc, lines, calls = self.ci("--keep-going")
        self.assertEqual((rc, calls), (2, [["app-image"]]))
        final = last_json(lines[-1])
        self.assertEqual([(j["job"], j["stage"], j["result"], j["exit"]) for j in final["jobs"]],
                         [("app-unit", "unit", "usage", 2), ("app-image", "image", "pass", 0)])
        self.assertEqual((final["result"], final["next"]), ("usage", "run orch init"))
        rc, lines, calls = self.ci("--stages", "image")  # other stages: nothing held, nothing named
        self.assertEqual((rc, calls, last_json(lines[-1])["next"]), (0, [["app-image"]], "done"))
        self.assertNotIn("app-unit", "\n".join(lines))


class Runs(ProjectCase):
    def attempts(self, results):
        job = orch.Job("t-ok", [])
        node = orch.nodes_all()["h11"]
        res = list(results)
        a = SimpleNamespace(node=None, no_retry=False, wait=1, _src=None)

        def execute(job_, node_, p, live, a_):
            cls, m = res.pop(0)
            return cls, SimpleNamespace(meta=m, dir=self.tmp, log=self.tmp / "x.log")
        with mock.patch.object(orch, "select_node", lambda *x, **k: (node, {}, {"jobid": "j"}, {})), \
                mock.patch.object(orch, "execute", execute), redirect_stdout(io.StringIO()) as o, \
                redirect_stderr(io.StringIO()):
            rc = orch._run_attempts(job, a)
        return rc, o.getvalue()

    def test_append_run_after_a_faked_run(self):
        orch.cfg_nodes()
        rc, out = self.attempts([("pass", meta(sha="b" * 40))])
        self.assertEqual(rc, 0)
        runs = oc.read_runs(self.p)
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(set(r), RUN_REC_KEYS)
        self.assertEqual({k: r[k] for k in RUN_REC_KEYS - {"ts"}},
                         {"job": "t-ok", "sha": "b" * 40, "wt": False, "node": "h11", "result": "pass", "exit": 0,
                          "wall_s": 1.5, "jobid": "j1"})
        self.assertRegex(r["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual((orch.JOURNAL["result"], orch.JOURNAL["retries"], len(orch.JOURNAL["attempts"])), ("pass", 0, 1))

    def test_infra_retry_records_both_attempts(self):
        orch.cfg_nodes()
        rc, _ = self.attempts([("infra", meta("j1", result="infra", exit_=5, reason="daemon gone")),
                               ("pass", meta("j2", node="h12"))])
        self.assertEqual(rc, 0)
        self.assertEqual([(r["jobid"], r["result"]) for r in oc.read_runs(self.p)], [("j1", "infra"), ("j2", "pass")])
        self.assertEqual((orch.JOURNAL["retries"], orch.JOURNAL["node"], orch.JOURNAL["attempts"][0]["reason"]),
                         (1, "h12", "daemon gone"))

    def test_legacy_mode_never_appends(self):
        orch.cfg_nodes()  # PROJECT p is set ...
        with mock.patch.dict(os.environ, {"ORCH_CONFIG": TEST_ENV["ORCH_CONFIG"]}):  # ... but legacy mode wins
            orch.record_run(meta())
        self.assertEqual(oc.read_runs(self.p), [])

    def test_failed_append_warns_and_keeps_the_result(self):
        orch.cfg_nodes()
        with mock.patch.object(oc, "append_run", side_effect=OSError("disk full")), \
                redirect_stderr(io.StringIO()) as e:
            orch.record_run(meta())
        self.assertIn("warn: run j1 not recorded in", e.getvalue())
        self.assertIn("orch deploy --check will not count it", e.getvalue())

    def test_select_node_journals_the_pick_with_reasons_and_scores(self):
        job = orch.Job("t-ok", [])

        def admit(job_, n, p, mem=None):
            if n.name == "h11":
                return {"ok": True, "score": 0.7, "checks": [], "breakdown": {}}
            return {"ok": False, "score": 0.1, "checks": [{"check": "mem", "ok": False, "detail": "memory low"}]}
        opts = SimpleNamespace(node=None, wait=0, lease=60)
        with mock.patch.object(orch, "probe", lambda n, refs=(): {"heavy_jobs": []}), \
                mock.patch.object(orch, "admit", admit), redirect_stderr(io.StringIO()):
            node, _, live, _ = orch.select_node(job, opts)
        self.addCleanup((orch.live_dir() / f"{live['jobid']}.json").unlink, True)
        self.assertEqual(node.name, "h11")
        self.assertEqual(orch.JOURNAL["admission"], [{"pick": "h11", "nodes": {"h11": {"ok": True, "score": 0.7},
                                                                             "h12": {"ok": False, "reason": "memory low"}}}])


class ProbeProblemNodes(ProjectCase):
    """Council 2 MF5: a helper whose last probe found another orch namespace's objects is refused by the runtime verbs
    with the probe's message (not "ownership assertion failed"); the other helper keeps working and gc still reaches
    it. A helper whose probe failed without a transport (Docker down) is a disabled node, never a broken setup."""

    def hosts(self, **recs):
        sh = hosts_state()
        for n, extra in recs.items():
            sh[n] = {**sh[n], "ok": False, **extra}
        oc.save_state(self.p, "hosts", sh)
        orch.reset_project()

    def test_another_namespace_node_is_refused_with_the_fix(self):
        import orch_discover as od  # noqa: PLC0415
        pr = od.foreign_problem([{"kind": "container", "name": "agentt-9-job", "family": "io.orch"}], "agentt-")
        self.hosts(h11={"problem": pr})
        msg = f"{pr['what']}. Fix: {pr['fix']}"
        with mock.patch.dict(orch.VERB, {"name": "run"}):
            nodes = orch.nodes_all(True)
            self.assertEqual((nodes["h11"].c["enabled"], nodes["h12"].c["enabled"]), (False, True))
            self.assertIn("h11", orch.cleanable_nodes())  # its transport stays: gc reaches orch's own objects there
            opts = SimpleNamespace(node="h11", wait=0, lease=60)
            with redirect_stderr(io.StringIO()):
                node, _, _, why = orch.select_node(orch.Job("t-ok", []), opts)
        self.assertIsNone(node)
        self.assertIn("h11: disabled: roles helper; jobs all (everything incl. image builds); disabled: " + msg, why)
        self.assertNotIn("ownership assertion failed", why)

    def test_a_docker_down_helper_is_a_disabled_node(self):
        self.hosts(h11={"transport": None, "context": None,
                        "problem": {"class": "docker-down", "what": "Docker is not running on deploy@192.0.2.11",
                                    "fix": "start Docker on deploy@192.0.2.11: sudo systemctl start docker",
                                    "tried": "-"}})
        with mock.patch.dict(orch.VERB, {"name": "run"}):
            nodes = orch.cfg_nodes()["nodes"]  # validates: no "transport must be context|ssh-docker" for the setup
            self.assertEqual((nodes["h11"]["enabled"], nodes["h12"]["enabled"]), (False, True))
            self.assertNotIn("h11", orch.cleanable_nodes())


class JournalHook(ProjectCase):
    JOURNAL_ON = True

    def fake(self, fail=None, chatty=False):
        calls = []
        m = types.ModuleType("orch_journal")

        def record_invocation(p, verb, args, started, ended, exit_code, details):
            if chatty:
                print("noise from the journal")
            if fail:
                raise fail
            calls.append({"p": p, "verb": verb, "args": args, "started": started, "ended": ended, "exit": exit_code,
                          "details": details})

        def cmd_journal(a):
            calls.append({"cmd_journal": a.action})
            print(json.dumps({"ok": True}))
            return 0
        m.record_invocation, m.cmd_journal = record_invocation, cmd_journal
        pm = mock.patch.dict(sys.modules, {"orch_journal": m})
        pm.start()
        self.addCleanup(pm.stop)
        return calls

    @staticmethod
    def verb(rc, **details):
        def fn(a):
            orch.JOURNAL.update(details)
            print(json.dumps({"result": "x", "exit": rc}))
            return rc
        return fn

    def test_once_per_verb_with_the_exit_code(self):
        calls = self.fake()
        with mock.patch.object(orch, "cmd_plan", self.verb(75, pick=None, nodes={"h11": {"ok": False}})), \
                mock.patch.object(orch, "cmd_nodes", self.verb(0)), mock.patch.object(orch, "cmd_cordon", self.verb(0)):
            for argv, want in ((["plan", "t-ok"], 75), (["nodes"], 0), (["cordon", "h11", "--reason", "x"], 0)):
                n = len(calls)
                rc, out, _ = run_main(argv)
                self.assertEqual(rc, want)
                self.assertEqual(len(calls), n + 1, argv)
                c = calls[-1]
                self.assertEqual((c["verb"], c["exit"], c["args"]), (argv[0], want, argv))
                self.assertEqual(Path(c["p"].store), Path(self.p.store))
                self.assertLessEqual(c["started"], c["ended"])
                self.assertEqual(last_json(out)["exit"], want)
        self.assertEqual(calls[0]["details"], {"pick": None, "nodes": {"h11": {"ok": False}}})
        self.assertEqual(calls[1]["details"], {})

    def test_run_details_and_redacted_args(self):
        calls = self.fake()
        node = orch.nodes_all()["h11"]
        orch.reset_project()

        def select(job, a, exclude=(), mem=None):
            orch._j_pick("h11", [(0.7, "h11", {})], {"h12": "busy: agent heavy jobs 1 >= max 1"})
            return node, {}, {"jobid": "j1"}, {}

        def execute(job, node_, p, live, a):
            return "test-fail", SimpleNamespace(meta=meta(result="test-fail", exit_=1, reason="tests failed: x",
                                                          failures=["FAIL a", "FAIL b", "FAIL c", "FAIL d"]),
                                                dir=self.tmp, log=self.tmp / "x.log")
        with mock.patch.object(orch, "select_node", select), mock.patch.object(orch, "execute", execute):
            rc, out, _ = run_main(["run", "t-ok", "--no-retry", "--", "API_TOKEN=s3cret"])
        self.assertEqual(rc, 1)
        self.assertEqual(last_json(out)["result"], "test-fail")
        c = calls[-1]
        self.assertEqual((c["verb"], c["exit"]), ("run", 1))
        self.assertEqual(c["args"], ["run", "t-ok", "--no-retry", "--", "API_TOKEN=***"])
        d = c["details"]
        self.assertEqual((d["job"], d["node"], d["jobid"], d["result"], d["retries"], d["failures"]),
                         ("t-ok", "h11", "j1", "test-fail", 0, ["FAIL a", "FAIL b", "FAIL c"]))
        self.assertEqual(d["admission"], [{"pick": "h11", "nodes": {
            "h11": {"ok": True, "score": 0.7}, "h12": {"ok": False, "reason": "busy: agent heavy jobs 1 >= max 1"}}}])
        self.assertEqual((d["wall_s"], d["wait_s"], d["overhead_s"]), (1.5, 0.2, 0.3))
        self.assertEqual([r["result"] for r in oc.read_runs(self.p)], ["test-fail"])

    def test_refusal_is_recorded(self):
        calls = self.fake()
        cfg = setup_cfg()
        cfg["alerts"] = {"command": "cat >/dev/null"}
        self.save(cfg)
        rc, out, _ = run_main(["run", "t-ok"])
        self.assertEqual(rc, 2)
        self.assertEqual((calls[-1]["verb"], calls[-1]["exit"]), ("run", 2))
        self.assertIn("changed since it was approved", calls[-1]["details"]["error"])

    def test_journal_failure_never_fails_the_verb(self):
        for fail in (RuntimeError("disk full"), KeyboardInterrupt(), SystemExit(3)):
            self.fake(fail=fail)
            with mock.patch.object(orch, "cmd_plan", self.verb(75)):
                rc, out, err = run_main(["plan", "t-ok"])
            self.assertEqual(rc, 75, fail)
            self.assertEqual(last_json(out), {"result": "x", "exit": 75})
            self.assertIn("orch: warn: journal entry not written", err)

    def test_stdout_keeps_the_verbs_last_line(self):
        self.fake(chatty=True)
        with mock.patch.object(orch, "cmd_plan", self.verb(0)):
            rc, out, err = run_main(["plan", "t-ok"])
        self.assertEqual(last_json(out), {"result": "x", "exit": 0})
        self.assertNotIn("noise from the journal", out)
        self.assertIn("noise from the journal", err)

    def test_off_for_journal_itself_legacy_mode_and_orch_journal_0(self):
        calls = self.fake()
        rc, out, _ = run_main(["journal", "path"])
        self.assertEqual((rc, calls), (0, [{"cmd_journal": "path"}]))
        with mock.patch.object(orch, "cmd_nodes", self.verb(0)):
            with mock.patch.dict(os.environ, {"ORCH_JOURNAL": "0"}):
                run_main(["nodes"])
            with mock.patch.dict(os.environ, {"ORCH_CONFIG": TEST_ENV["ORCH_CONFIG"]}):
                run_main(["nodes"])
        self.assertEqual(len(calls), 1)

    def test_no_journal_module_is_silent(self):
        with mock.patch.dict(sys.modules, {"orch_journal": None}), mock.patch.object(orch, "cmd_nodes", self.verb(0)):
            rc, out, err = run_main(["nodes"])
        self.assertEqual(rc, 0)
        self.assertNotIn("journal", err)

    def test_init_is_recorded_once_its_setup_exists(self):
        calls = self.fake()
        fresh = git_init(self.tmp / "fresh")
        self.env(ORCH_PROJECT=str(fresh))
        m = types.ModuleType("orch_init")

        def cmd_init(a):
            p, _ = oc.init_project(a.project, env={})
            oc.save_config(p, {"schema": 1, "pending": {"helper_count": 1}})
            m.LAST = {"state": "incomplete", "step": "helpers", "applied": ["helper_count"]}
            print(json.dumps({"state": "incomplete", "step": "helpers", "next": "ask"}))
            return 0
        m.cmd_init = cmd_init
        with mock.patch.dict(sys.modules, {"orch_init": m}):
            rc, out, _ = run_main(["init", "--apply", "--project", str(fresh)], stdin='{"helper_count": 1}\n')
        self.assertEqual(rc, 0)
        self.assertEqual(last_json(out)["step"], "helpers")
        c = calls[-1]
        self.assertEqual((c["verb"], c["exit"], c["details"]),
                         ("init", 0, {"state": "incomplete", "step": "helpers", "applied": ["helper_count"]}))
        self.assertEqual(Path(c["p"].root), fresh)

    def test_init_details_from_a_module_journal_dict(self):  # R16: orch_init fills JOURNAL, not LAST/details()
        calls = self.fake()
        m = types.ModuleType("orch_init")
        m.JOURNAL = {}

        def cmd_init(a):
            m.JOURNAL.clear()
            m.JOURNAL.update(state="unapproved", step="approve", applied=[], warnings=[], exit=2,
                             error="not every helper probed ok (h12 (deploy@192.0.2.12:2222))")
            print(json.dumps({"state": "unapproved", "step": "approve", "next": "fix"}))
            return 2
        m.cmd_init = cmd_init
        with mock.patch.dict(sys.modules, {"orch_init": m}):
            rc, out, _ = run_main(["init", "--approve"])
        self.assertEqual(rc, 2)
        c = calls[-1]
        self.assertEqual((c["verb"], c["exit"]), ("init", 2))
        self.assertEqual((c["details"]["state"], c["details"]["step"]), ("unapproved", "approve"))
        self.assertIn("not every helper probed ok", c["details"]["error"])

    def test_a_failed_verb_names_the_difficulty_orch_recorded(self):  # R35, with the real orch_journal
        jr = orch.sibling("orch_journal")
        with mock.patch.dict(sys.modules, {"orch_journal": jr}), mock.patch.object(orch, "cmd_plan", self.verb(75)), \
                mock.patch.object(orch, "cmd_nodes", self.verb(0)):
            rc, out, err = run_main(["plan", "t-ok"])
            rc0, _, err0 = run_main(["nodes"])
        self.assertEqual((rc, last_json(out), rc0), (75, {"result": "x", "exit": 75}, 0))
        diffs = [r for r in jr.read(self.p) if r.get("type") == "difficulty" and r.get("by") == "orch"]
        self.assertEqual(len(diffs), 1)
        self.assertIn(f"journal: difficulty #{diffs[0]['seq']} recorded (orch)", err.splitlines())
        self.assertNotIn("journal: difficulty", err0)

    def test_difficulty_line_from_any_return_shape(self):
        m = types.ModuleType("orch_journal")
        m.read = lambda p, task=None, last=None: [{"type": "difficulty", "by": "orch", "seq": 8, "step": 7, "task": "t1"}]
        for got, want in (({"type": "difficulty", "seq": 9}, "#9"), ({"type": "step", "seq": 4, "difficulty": 5}, "#5"),
                          ({"type": "step", "seq": 4, "difficulty": {"seq": 6}}, "#6"),
                          ({"type": "step", "seq": 7, "task": "t1"}, "#8"), ({"type": "step", "seq": 3}, None),
                          (None, None)):
            m.record_invocation = lambda *a, _g=got, **k: _g
            with mock.patch.dict(sys.modules, {"orch_journal": m}), mock.patch.object(orch, "cmd_plan", self.verb(75)):
                rc, _, err = run_main(["plan", "t-ok"])
            self.assertEqual(rc, 75)
            lines = [l for l in err.splitlines() if l.startswith("journal: difficulty")]
            self.assertEqual(lines, [f"journal: difficulty {want} recorded (orch)"] if want else [], got)


JOURNAL_STUB = '''import json, os


def record_invocation(p, verb, args, started, ended, exit_code, details):
    with open(os.environ["ORCH_TEST_JOURNAL"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"store": str(p.store), "verb": verb, "args": args, "exit": exit_code,
                            "seconds": ended - started, "details": details}) + "\\n")


def cmd_journal(a):
    print(json.dumps({"journal": a.action}))
    return 0
'''
INIT_STUB = '''import json
LAST = {}


def cmd_init(a):
    LAST.update(state="ready", step="none")
    print(json.dumps({"state": "ready", "step": "none", "summary": ["setup ok"], "problems": [], "ask": None,
                      "next": "ready"}))
    return 0
'''


class RealProcess(ProjectCase):
    """orch.py as a real process next to stub orch_init / orch_journal modules (the real ones are other builders')."""
    JOURNAL_ON = True

    def test_hook_and_module_verbs(self):
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        shutil.copy(ROOT / "scripts" / "orch.py", bin_)
        shutil.copy(ROOT / "scripts" / "orch_config.py", bin_)
        (bin_ / "orch_journal.py").write_text(JOURNAL_STUB, encoding="utf-8")
        (bin_ / "orch_init.py").write_text(INIT_STUB, encoding="utf-8")
        out = self.tmp / "journal-out.jsonl"
        env = dict(os.environ, ORCH_TEST_JOURNAL=str(out))

        def run(*args):
            return subprocess.run([sys.executable, "-B", str(bin_ / "orch.py"), *args], env=env, capture_output=True,
                                  text=True, timeout=120)
        r = run("status", "--local")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("init", "--show")
        self.assertEqual((r.returncode, last_json(r.stdout)["state"]), (0, "ready"), r.stderr)
        r = run("journal", "path")
        self.assertEqual((r.returncode, last_json(r.stdout)), (0, {"journal": "path"}), r.stderr)
        r = run("deploy", "--check")  # no orch_deploy module next to this orch.py
        self.assertEqual(r.returncode, 2)
        self.assertIn("orch deploy needs scripts/orch_deploy.py next to orch.py", last_json(r.stdout)["error"])
        cfg = setup_cfg()
        cfg["hosts"]["h11"]["jobs"] = "tests"
        oc.save_config(self.p, cfg)
        r = run("nodes")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(last_json(r.stdout)["next"], "run orch init")
        recs = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(x["verb"], x["exit"]) for x in recs], [("status", 0), ("init", 0), ("deploy", 2), ("nodes", 2)])
        self.assertEqual({Path(x["store"]) for x in recs}, {Path(self.p.store)})
        self.assertEqual(recs[1]["details"], {"state": "ready", "step": "none"})
        self.assertIn("changed since it was approved", recs[3]["details"]["error"])
        self.assertTrue(all(x["seconds"] >= 0 for x in recs))


class Verbs(unittest.TestCase):
    def parse(self, argv):
        seen = {}

        def grab(a):
            seen["a"] = a
            return 0
        fns = {k: grab for k in ("cmd_init", "cmd_deploy", "cmd_live", "cmd_journal")}
        with mock.patch.multiple(orch, **fns), mock.patch.object(orch.atexit, "register"), \
                mock.patch.object(orch.signal, "signal"):
            self.assertEqual(orch.main(argv), 0)
        return seen["a"]

    def refused(self, argv):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            self.parse(argv)
        self.assertEqual(e.exception.code, 2, argv)

    def test_init(self):
        a = self.parse(["init"])
        self.assertEqual((a.offline, a.apply, a.detect, a.approve, a.show, a.ask, a.project),
                         (False, False, False, False, False, None, None))
        for flag in ("offline", "apply", "detect", "approve", "show"):
            self.assertTrue(getattr(self.parse(["init", f"--{flag}"]), flag))
        self.assertEqual(self.parse(["init", "--apply", "--project", "C:/work/shop"]).project, "C:/work/shop")
        for key in ("deploy", "production"):  # R15: the deploy gate's fix runs orch init --ask deploy|production
            self.assertEqual(self.parse(["init", "--ask", key, "--offline"]).ask, key)

    def test_init_refuses_abbreviations(self):  # the guard matches "orch init --approve" exactly
        for argv in (["init", "--appr"], ["init", "--approv"], ["init", "--off"], ["init", "--proj", "/srv/app"],
                     ["init", "--bogus"], ["init", "--as", "deploy"], ["init", "--ask", "alerts"], ["init", "--ask"],
                     ["init", "--ask", "deploy", "--apply"], ["init", "--apply", "--approve"]):
            self.refused(argv)

    def test_init_flags_come_from_orch_init(self):  # they cannot drift from what cmd_init reads
        oi = orch.sibling("orch_init")
        ref = __import__("argparse").ArgumentParser(prog="orch init", allow_abbrev=False)
        oi.add_arguments(ref)
        keys = sorted(vars(ref.parse_args([])))

        def theirs(argv):
            try:
                with redirect_stderr(io.StringIO()):
                    return vars(ref.parse_args(argv))
            except SystemExit:
                return "refused"

        def ours(argv):
            try:
                with redirect_stderr(io.StringIO()):
                    a = self.parse(["init", *argv])
            except SystemExit:
                return "refused"
            return {k: getattr(a, k) for k in keys}
        for argv in ([], ["--ask", "production"], ["--ask", "alerts"], ["--detect", "--offline"], ["--show", "--apply"],
                     ["--approve", "--project", "/srv/app"], ["--appr"]):
            self.assertEqual(ours(argv), theirs(argv), argv)
        o = io.StringIO()
        with redirect_stdout(o), self.assertRaises(SystemExit):
            orch.main(["init", "-h"])
        flat = " ".join(o.getvalue().split())
        for a in ref._actions:
            if a.option_strings and a.help and "%(" not in a.help:
                self.assertIn(" ".join(a.help.split()), flat, a.dest)

    def test_other_verbs_never_import_orch_init(self):  # its import would cost every orch call (ci children too)
        with mock.patch.dict(sys.modules):
            sys.modules.pop("orch_init", None)
            self.assertTrue(self.parse(["deploy", "--check"]).check)
            self.assertNotIn("orch_init", sys.modules)
            self.assertEqual(self.parse(["init", "--ask", "production"]).ask, "production")
            self.assertIn("orch_init", sys.modules)

    def test_init_parser_without_a_usable_orch_init(self):
        broken = types.ModuleType("orch_init")
        broken.add_arguments = lambda ap: 1 // 0
        for mod in (None, types.ModuleType("orch_init"), broken):
            with mock.patch.dict(sys.modules, {"orch_init": mod}):
                a = self.parse(["init", "--ask", "deploy", "--offline", "--project", "/srv/app"])
                self.assertEqual((a.ask, a.offline, a.project, a.apply), ("deploy", True, "/srv/app", False), mod)
                self.refused(["init", "--appr"])
                self.refused(["init", "--ask", "alerts"])
                self.assertTrue(self.parse(["deploy", "--check"]).check)  # the other verbs never need orch_init

    def test_deploy_live_journal(self):
        a = self.parse(["deploy", "--check"])
        self.assertEqual((a.check, a.ref, a.project), (True, "HEAD", None))
        self.assertEqual(self.parse(["deploy", "--check", "--ref", "abc123"]).ref, "abc123")
        self.assertFalse(self.parse(["deploy"]).check)
        self.assertEqual(self.parse(["live", "--project", "/srv/app"]).project, "/srv/app")
        for act in ("start", "add", "end", "show", "stats", "path"):
            self.assertEqual(self.parse(["journal", act]).action, act)
        a = self.parse(["journal", "show", "--task", "t20261030-090000-ab12", "--json"])
        self.assertEqual((a.task, a.json, a.last, a.days), ("t20261030-090000-ab12", True, None, None))
        self.assertEqual(self.parse(["journal", "show", "--last", "3"]).last, 3)
        self.assertEqual(self.parse(["journal", "stats", "--days", "7", "--project", "/srv/app"]).days, 7)
        for argv in (["journal"], ["journal", "bogus"], ["journal", "show", "--last", "x"], ["live", "--check"]):
            self.refused(argv)

    def test_help_lists_the_new_verbs(self):
        o = io.StringIO()
        with redirect_stdout(o), self.assertRaises(SystemExit):
            orch.main(["-h"])
        for v in ("init", "deploy", "live", "journal"):
            self.assertRegex(o.getvalue(), rf"\b{v}\b")

    def test_journal_flags_and_help_come_from_orch_journal(self):  # R37: they cannot drift from cmd_journal
        jr = orch.sibling("orch_journal")
        ref = __import__("argparse").ArgumentParser(prog="orch journal")
        jr.add_arguments(ref)
        keys = sorted(vars(ref.parse_args(["show"])))

        def theirs(argv):
            try:
                with redirect_stderr(io.StringIO()):
                    return vars(ref.parse_args(argv))
            except SystemExit:
                return "refused"

        def ours(argv):
            try:
                with redirect_stderr(io.StringIO()):
                    a = self.parse(["journal", *argv])
            except SystemExit:
                return "refused"
            return {k: getattr(a, k) for k in keys}
        o = io.StringIO()
        with mock.patch.dict(sys.modules, {"orch_journal": jr}):
            for argv in (["stats", "--days", "1.5"], ["show", "--last", "3"], ["show", "--last", "x"], ["bogus"],
                         ["show", "--task", "t20261030-090000-ab12", "--json"], ["path", "--project", "/srv/app"]):
                self.assertEqual(ours(argv), theirs(argv), argv)
            with redirect_stdout(o), self.assertRaises(SystemExit):
                orch.main(["journal", "-h"])
        flat = " ".join(o.getvalue().split())
        for a in ref._actions:
            if a.option_strings and a.help and "%(" not in a.help:
                self.assertIn(" ".join(a.help.split()), flat, a.dest)

    def test_journal_parser_without_a_usable_orch_journal(self):
        broken = types.ModuleType("orch_journal")
        broken.add_parser = lambda sp: 1 // 0
        only_args = types.ModuleType("orch_journal")
        only_args.add_arguments = lambda ap: (ap.add_argument("action"), ap.add_argument("--last", type=int))
        for mod in (None, types.ModuleType("orch_journal"), broken, only_args):
            with mock.patch.dict(sys.modules, {"orch_journal": mod}):
                self.assertEqual(self.parse(["journal", "show", "--last", "3"]).last, 3, mod)
                self.assertTrue(self.parse(["init", "--offline"]).offline)


class MasterProd(ProjectCase):
    """R37/R1 wiring: orch_config.master_prod (hosts that another approved setup makes production) reaches
    compile_engine when both have it; never fatal."""
    MP = {("192.0.2.12", 2222): ["C:/work/other"]}

    def compiled(self, master_prod, takes=True):
        got, real = {}, oc.compile_engine

        if takes:
            def ce(p, cfg, sh, policy=None, master_prod=None):
                got["mp"] = master_prod
                return real(p, cfg, sh, policy)
        else:
            def ce(p, cfg, sh, policy=None):
                got["mp"] = "not passed"
                return real(p, cfg, sh, policy)
        e = io.StringIO()
        with mock.patch.object(oc, "compile_engine", ce), mock.patch.object(oc, "master_prod", master_prod, create=True), \
                redirect_stderr(e):
            orch.reset_project()
            self.assertEqual(sorted(orch.cfg_nodes()["nodes"]), ["h11", "h12"])
        return got["mp"], e.getvalue()

    def test_passed_when_available(self):
        seen = []
        self.assertEqual(self.compiled(lambda home, run=None: seen.append(home) or self.MP)[0], self.MP)
        self.assertEqual(seen, [orch.HOME])

    def test_missing_failing_or_not_taken(self):
        self.assertIsNone(self.compiled(None)[0])
        mp, err = self.compiled(lambda home, run=None: 1 // 0)
        self.assertIsNone(mp)
        self.assertIn("warn: production hosts of the other approved setups on this master were not checked", err)
        self.assertEqual(self.compiled(lambda home, run=None: self.MP, takes=False)[0], "not passed")


class SelftestPhantoms(ProjectCase):
    """R3: selftest's phantom runs use legacy mode inside the approved project; orch's own children pass the gate
    (ORCH_TEST_MODE=1) and the phantom config holds no production host."""

    def view(self, *names):
        hosts = {"h11": ("192.0.2.11", 22), "h12": ("192.0.2.12", 2222)}
        gv = orch.HOME / "guard-view.json"
        gv.write_text(json.dumps({"schema": 1, "projects": [], "hosts": [
            {"name": n, "roles": ["helper", "prod"], "spec": f"deploy@{hosts[n][0]}:{hosts[n][1]}",
             "hostname": hosts[n][0], "port": hosts[n][1], "user": "deploy", "ips": [hosts[n][0]], "aliases": [],
             "contexts": []} for n in names]}), encoding="utf-8")
        self.addCleanup(gv.unlink, True)

    def phantoms(self):
        ph = tmpdir(self)
        with redirect_stderr(io.StringIO()):
            phantoms, skip = orch.phantom_config(ph)
        return ph, json.loads((ph / "nodes.json").read_text(encoding="utf-8"))["nodes"], phantoms, skip

    def test_every_linux_helper_production_skips_hd(self):
        self.view("h11", "h12")
        _, nodes, phantoms, skip = self.phantoms()
        self.assertEqual((sorted(nodes), [k for k, _ in phantoms]), (["hx"], ["hx"]))
        self.assertIn("that is not also production (h11, h12 are)", skip)

    def test_a_node_ssh_cannot_resolve_is_no_phantom_base(self):  # review round 2 Q2: resolved like a legacy node
        self.view("h11")
        with mock.patch.dict(orchtest.SSH_CONFIG, {"192.0.2.12": {"fail": "command-line: line 0: Bad configuration"}}):
            _, nodes, phantoms, skip = self.phantoms()
        self.assertEqual((sorted(nodes), [k for k, _ in phantoms]), (["hx"], ["hx"]))
        self.assertIn("that is not also production (h11 is); whose ssh destination ssh -G can resolve (h12: it cannot)",
                      skip)

    def helper_only(self, name):
        self.cfg["hosts"][name]["roles"] = ["helper"]
        oc.save_config(self.p, self.cfg)
        oc.trust_set(orch.HOME, self.p, self.cfg, {})
        orch.reset_project()

    def test_a_helper_that_is_also_production_is_no_phantom_base(self):  # council 2 MF6
        _, nodes, phantoms, skip = self.phantoms()  # no view: h12's compiled role (helper+prod) alone keeps it out
        self.assertEqual([k for k, _ in phantoms], ["hx", "hd"])
        self.assertEqual(nodes["hd"]["ssh_args"], ["deploy@192.0.2.11"])  # a copy of h11, never of h12
        self.view("h11")
        _, nodes, phantoms, skip = self.phantoms()
        self.assertEqual((sorted(nodes), [k for k, _ in phantoms]), (["hx"], ["hx"]))
        self.assertIn("that is not also production (h11, h12 are)", skip)

    def test_phantoms_skip_production_nodes_and_pass_the_gate(self):
        self.helper_only("h12")
        self.view("h11")  # another approved setup made h11's host production
        ph, nodes, phantoms, skip = self.phantoms()
        self.assertEqual((sorted(nodes), [k for k, _ in phantoms], skip), (["hd", "hx"], ["hx", "hd"], None))
        self.assertEqual(nodes["hd"]["ssh_args"], ["-p", "2222", "deploy@192.0.2.12"])  # never a copy of h11
        with mock.patch.dict(os.environ, orch.phantom_env(ph)), mock.patch.dict(orch.VERB, {"name": "run"}):
            orch.reset_project()
            self.assertEqual(sorted(orch.cfg_nodes()["nodes"]), ["hd", "hx"])
            with mock.patch.dict(os.environ, {"ORCH_TEST_MODE": "0"}):
                orch.reset_project()
                with self.assertRaisesRegex(orch.ConfigError, "ORCH_CONFIG is refused inside the approved orch project"):
                    orch.cfg_nodes()


class VerbModules(unittest.TestCase):
    """Legacy env (orchtest's ORCH_CONFIG): the hook is off; only the module wiring is under test."""

    def setUp(self):
        orch.JOURNAL.clear()
        self.addCleanup(orch.JOURNAL.clear)

    def test_modules_called_and_details_kept(self):
        mi, md = types.ModuleType("orch_init"), types.ModuleType("orch_deploy")
        got = []

        def cmd_init(a):
            got.append(("init", a.show))
            mi.LAST = {"state": "ready"}
            return 0
        mi.cmd_init = cmd_init
        md.cmd_deploy = lambda a: got.append(("deploy", a.check)) or orch.GATE
        md.cmd_live = lambda a: got.append(("live", a.project)) or 0
        md.details = lambda: {"gates": [{"name": "tree clean", "ok": False}]}
        with mock.patch.dict(sys.modules, {"orch_init": mi, "orch_deploy": md}):
            self.assertEqual(run_main(["init", "--show"])[0], 0)
            self.assertEqual(orch.JOURNAL, {"state": "ready"})
            self.assertEqual(run_main(["deploy", "--check"])[0], 7)
            self.assertEqual(orch.JOURNAL, {"gates": [{"name": "tree clean", "ok": False}]})
            self.assertEqual(run_main(["live"])[0], 0)
        self.assertEqual(got, [("init", True), ("deploy", True), ("live", None)])

    def test_missing_module_is_exit_2_with_the_fix(self):
        with mock.patch.dict(sys.modules, {"orch_init": None}):
            rc, out, err = run_main(["init"])
        self.assertEqual(rc, 2)
        j = last_json(out)
        self.assertEqual((j["result"], j["exit"]), ("usage", 2))
        self.assertIn("orch init needs scripts/orch_init.py next to orch.py", j["error"])
        self.assertIn("reinstall the skill", j["error"])
        with mock.patch.dict(sys.modules, {"orch_deploy": types.ModuleType("orch_deploy")}):
            rc, out, err = run_main(["live"])
        self.assertEqual((rc, last_json(out)["error"]),
                         (2, "scripts/orch_deploy.py has no cmd_live(), so orch live cannot run: reinstall the skill"))

    def test_module_config_error_and_crash(self):
        m = types.ModuleType("orch_deploy")

        def bad_config(a):
            raise oc.ConfigError("config.json: hosts must be an object; fix the file or run orch init")
        m.cmd_deploy, m.cmd_live = bad_config, lambda a: 1 // 0
        with mock.patch.dict(sys.modules, {"orch_deploy": m}):
            rc, out, err = run_main(["deploy", "--check"])
            self.assertEqual(rc, 2)
            self.assertEqual(last_json(out), {"result": "usage", "exit": 2, "error": "config rejected: config.json: "
                                              "hosts must be an object; fix the file or run orch init"})
            rc, out, err = run_main(["live"])
        self.assertEqual(rc, 5)  # a crash is infra, never test-fail
        self.assertEqual(last_json(out)["result"], "infra")
        self.assertIn("internal error in orch live: ZeroDivisionError", last_json(out)["error"])
        self.assertIn("Traceback", err)


class SshDockerDefaultDocker(unittest.TestCase):
    N = {"os": "linux", "ssh": "h13", "ssh_args": ["-p", "2222", "deploy@192.0.2.13"], "transport": "ssh-docker",
         "docker_host": None}

    def test_plain_docker_everywhere(self):
        n = orch.Node("h13", dict(self.N))
        sa = self.N["ssh_args"]
        self.assertEqual(n.docker_argv(["ps", "-a"]), orch.SSH + sa + ["docker ps -a"])
        with FakeCmd(lambda argv, kw: orch.R(0, "")) as fc:
            n.shell("echo hi")
            n.dbatch([["ps", "-q"]])
        for argv, kw in fc.calls:
            self.assertEqual(argv[:len(orch.SSH) + 3], orch.SSH + sa)
            self.assertNotIn(b"DOCKER_HOST", kw["input"])
        argv, script = orch.Watchdog(n, {}, "j1", lambda r: None, iters=5)._argv()
        self.assertEqual(argv, orch.SSH + sa + ["sh -s -- 5"])
        self.assertNotIn(b"DOCKER_HOST", script)
        n2 = orch.Node("h13", dict(self.N, docker_host="unix:///run/user/1000/docker.sock"))
        self.assertEqual(n2.docker_argv(["ps"]), orch.SSH + sa + ["docker -H unix:///run/user/1000/docker.sock ps"])

    def test_validator(self):
        def nodes(dh):
            d = json.loads(json.dumps(orch.cfg_nodes()))
            d["nodes"] = {"h13": dict(self.N, enabled=True, docker_host=dh, tags=["docker"], opt_in=["t-ok"])}
            return d
        orch.validate_nodes(nodes(None))
        orch.validate_nodes(nodes("unix:///var/run/docker.sock"))
        with self.assertRaisesRegex(orch.ConfigError, r"ssh-docker docker_host must be null \(the helper's default "
                                                      r"docker\) or unix://"):
            orch.validate_nodes(nodes("tcp://192.0.2.13:2375"))


class ConfigErrorLine(unittest.TestCase):
    def test_config_error_prints_a_result_line(self):
        def bad(a):
            raise orch.ConfigError("node n9: os must be linux|windows")
        with mock.patch.object(orch, "cmd_nodes", bad):
            rc, out, err = run_main(["nodes"])
        self.assertEqual(rc, 2)
        self.assertEqual(last_json(out), {"result": "usage", "exit": 2,
                                          "error": "config rejected: node n9: os must be linux|windows"})
        self.assertIn("orch: config rejected: node n9", err)


def load_dev(name):
    path = ROOT / "dev" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"dev_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless((ROOT / "dev" / "runtime_init.py").is_file(), "dev harnesses are not part of the package")
class DevHarnesses(unittest.TestCase):
    def bad(self, parse, argv):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            parse(argv)
        self.assertEqual(e.exception.code, 2, argv)

    def test_runtime_init_args(self):
        ri = load_dev("runtime_init")
        a = ri.parse_args(["--helper", "deploy@192.0.2.11"])
        self.assertEqual((a.helper, a.prod, a.docker_host, a.keep), ("deploy@192.0.2.11", "", "", False))
        a = ri.parse_args(["--helper", "h11", "--prod", "deploy@192.0.2.20:2222", "--docker-host",
                           "unix:///var/run/docker.sock", "--keep"])
        self.assertEqual((a.prod, a.keep), ("deploy@192.0.2.20:2222", True))
        for argv in ([], ["--helper", "deploy@192.0.2.11 rm"], ["--helper", "-oProxyCommand=x"],
                     ["--helper", "h11", "--prod", "h11"], ["--helper", "h11", "--docker-host", "tcp://192.0.2.11:2375"],
                     ["--helper", "h11", "--prod", "deploy@192.0.2.20:99999"]):
            self.bad(ri.parse_args, argv)
        self.assertEqual((ri.NS_PREFIX, ri.NS_LABEL), ("agentt-", "io.orchtest.agent"))

    def test_runtime_init_answers_each_init_step(self):
        ri = load_dev("runtime_init")
        h, pr = "deploy@192.0.2.11", "deploy@192.0.2.20:2222"
        act = lambda **st: ri.next_action(st, h, pr)  # noqa: E731
        self.assertEqual(act(step="count", next="ask"), ("apply", {"helper_count": 1}))
        self.assertEqual(act(step="helpers", next="ask"), ("apply", {"helpers": [h]}))
        kind, ans = act(step="setup", next="ask", ask={"example": {"hosts": {"h11": {"roles": ["helper"]}}}})
        self.assertEqual((kind, ans["hosts"], ans["production"], ans["alerts"], ans["deploy"]["command"]),
                         ("apply", {"h11": {"roles": ["helper"], "jobs": "tests"}}, [pr], "none", "./deploy.sh"))
        self.assertEqual(ri.next_action({"step": "setup", "next": "ask"}, h)[1]["production"], "none")
        self.assertEqual(act(step="jobs", next="ask", ask={"example": {"jobs": "detected"}}),
                         ("apply", {"jobs": "detected"}))
        self.assertEqual(act(step="approve", next="approve"), ("approve", None))
        self.assertEqual(act(step="none", next="ready"), ("done", None))
        kind, why = act(step="fix", next="fix", problems=[{"host": "h11 (deploy@192.0.2.11)", "what": "ssh: refused",
                                                           "tried": "ssh -o BatchMode=yes deploy@192.0.2.11 true",
                                                           "fix": "check the network"}])
        self.assertEqual(kind, "fail")
        self.assertIn("h11 (deploy@192.0.2.11): ssh: refused", why)
        self.assertEqual(act(step="odd", next="ask")[0], "fail")

    def test_runtime_local_is_generic(self):
        if not GIT:
            self.skipTest("needs git")
        rl = load_dev("runtime_local")
        a = rl.parse_args([])
        self.assertEqual((a.node, a.context, a.ssh, a.docker_host), ("local", "default", "", ""))
        for argv in (["--docker-host", "unix:///var/run/docker.sock"], ["--watch", "http://127.0.0.1:8080/"],
                     ["--node", "Bad_Name"], ["--ssh", "deploy@192.0.2.11 x"]):
            self.bad(rl.parse_args, argv)
        self.assertEqual((rl.NS_PREFIX, rl.NS_LABEL), ("agentt-", "io.orchtest.agent"))
        for kw in ({"node": "local", "ctx": "default"},
                   {"node": "h11", "ctx": "", "ssh": "deploy@192.0.2.11:2222", "watch": ["http://127.0.0.1:8080/"]}):
            d = tmpdir(self)
            cfg = rl.make_config(d, **kw)
            n = json.loads((cfg / "nodes.json").read_text(encoding="utf-8"))
            j = json.loads((cfg / "jobs.json").read_text(encoding="utf-8"))
            prot = json.loads((cfg / "protected.json").read_text(encoding="utf-8"))
            orch.validate_nodes(n)
            orch.validate_jobs(j, orch.limits_of(n), n.get("require_digest", True) is not False)
            node = n["nodes"][kw["node"]]
            self.assertIn("t-prep", node["opt_in"])
            self.assertIn("t-prep", j["jobs"])
            self.assertEqual(n["admission"], oc.DEFAULT_POLICY["admission"])
            if kw.get("ssh"):
                self.assertEqual((node["ssh_args"], node["transport"], node["docker_host"]),
                                 (["-p", "2222", "deploy@192.0.2.11"], "ssh-docker", None))
                self.assertEqual(prot["nodes"]["h11"]["endpoints"][0]["url"], "http://127.0.0.1:8080/")
                self.assertEqual((node["os"], node["tags"]), ("linux", ["docker", "linux-host"]))
            else:
                self.assertEqual((node["ssh"], node["context"], node["transport"]), ("local", "default", "context"))
                want = "windows" if os.name == "nt" else "linux"
                self.assertEqual((node["os"], node["tags"]), (want, ["docker", f"{want}-host"]))
        # this machine's node follows its os (the Linux probe script cannot run on Windows); --ssh is always Linux
        for host_os in ("windows", "linux"):
            with mock.patch.object(rl, "local_os", return_value=host_os):
                for kw, want in (({"node": "local", "ctx": "default"}, host_os),
                                 ({"node": "h11", "ctx": "", "ssh": "deploy@192.0.2.11"}, "linux")):
                    n = json.loads((rl.make_config(tmpdir(self), **kw) / "nodes.json").read_text(encoding="utf-8"))
                    orch.validate_nodes(n)
                    node = n["nodes"][kw["node"]]
                    self.assertEqual((node["os"], node["tags"]), (want, ["docker", f"{want}-host"]), (host_os, kw))

    def test_harness_envs(self):  # R3: legacy runs are test mode; the init harness runs project mode like a user
        rl = load_dev("runtime_local")
        d = tmpdir(self)
        base = {k: v for k, v in os.environ.items() if k != "ORCH_TEST_MODE"}
        with mock.patch.dict(os.environ, dict(base, ORCH_PROJECT="/srv/app"), clear=True):
            env = rl.orch_env(d, d / "cfg")
        self.assertEqual((env["ORCH_CONFIG"], env["ORCH_TEST_MODE"], "ORCH_PROJECT" in env), (str(d / "cfg"), "1", False))
        if not GIT:
            self.skipTest("needs git")
        ri = load_dev("runtime_init")
        with mock.patch.dict(os.environ, {"ORCH_TEST_MODE": "1", "ORCH_CONFIG": "/tmp/x", "ORCH_GC_NODE_JSON": "{}"}):
            h = ri.Harness(ri.parse_args(["--helper", "deploy@192.0.2.11"]), d)
        self.assertFalse({"ORCH_TEST_MODE", "ORCH_CONFIG", "ORCH_GC_NODE_JSON"} & set(h.env))


if __name__ == "__main__":
    unittest.main()
