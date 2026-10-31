"""orch_config unit tests: store location, config IO, init state machine, engine compiler, registry, trust, runs and
guard view. No network, ssh or docker: every process goes through FakeRun (one real-git test runs local git only)."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import orch_config as oc  # noqa: E402

PIN = "@sha256:" + "a" * 64
M1, M2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64
NOT_GIT = (128, "", "fatal: not a git repository (or any of the parent directories): .git")
STATUS_KEYS = {"state", "step", "summary", "problems", "ask", "next"}
ASK_KEYS = {"kind", "header", "question", "options", "multi", "answer_key", "example"}
PROBLEM_KEYS = {"host", "what", "tried", "fix"}


def R(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class FakeRun:
    """subprocess.run stand-in. git: start dir -> (rc, out, err); worktrees: git dir -> porcelain text; ssh: alias ->
    (hostname, port) for `ssh -G`; contexts: docker context dicts (None = docker fails); missing: programs absent."""

    def __init__(self, git=None, worktrees=None, ssh=None, contexts=None, missing=()):
        self.git, self.worktrees, self.ssh = git or {}, worktrees or {}, ssh or {}
        self.contexts, self.missing, self.calls = contexts, set(missing), []

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[0] in self.missing:
            raise FileNotFoundError(argv[0])
        if argv[:2] == ["git", "-C"]:
            return R(*self.git.get(Path(argv[2]).as_posix(), NOT_GIT))
        if argv[:2] == ["git", "--git-dir"]:
            out = self.worktrees.get(Path(argv[2]).as_posix())
            return R(0, out) if out is not None else R(128, "", "fatal: not a git repository")
        if argv[0] == "ssh" and "-G" in argv:
            hp = self.ssh.get(argv[-1])
            if hp is None:
                return R(255, "", "ssh: Could not resolve hostname")
            return R(0, f"user deploy\nhostname {hp[0]}\nport {hp[1]}\nbatchmode no\n")
        if argv[:3] == ["docker", "context", "ls"]:
            if self.contexts is None:
                return R(1, "", "Cannot connect")
            return R(0, "".join(json.dumps(c) + "\n" for c in self.contexts))
        raise AssertionError(f"unexpected command {argv}")

    def count(self, prefix):
        return sum(1 for c in self.calls if c[:len(prefix)] == prefix)


def tmpdir(t: unittest.TestCase) -> Path:
    d = Path(tempfile.mkdtemp(prefix="orchcfg-")).resolve()
    t.addCleanup(shutil.rmtree, d, True)
    return d


def rec(host, port=22, alias=False, os_="linux", cot=(), ok=True, ctx=None, buildx=True, problem=None, eps=(),
        transport="context", docker_host=None):
    return {"ok": ok, "probed": "2026-10-30T09:00:00", "spec": host,
            "resolved": {"hostname": host, "user": "deploy", "port": port, "alias": alias},
            "facts": {"os": os_, "arch": "x86_64", "cpus": 8, "mem_bytes": 16500000000, "disk_free_bytes": 4e11,
                      "docker": "27.3.1", "docker_id": "id-" + host, "rootless": False, "buildx": buildx, "psi": True,
                      "cgroup": 2, "cotenants": [{"name": n, "image": n + ":1", "ports": [8080]} for n in cot],
                      "endpoints": list(eps)},
            "transport": transport, "context": ctx, "docker_host": docker_host, "problem": problem}


EP12 = {"name": "app1-web :8080", "url": "http://127.0.0.1:8080/", "insecure": False}


def state():
    return {"h11": rec("192.0.2.11", ctx="orch-h11"),
            "h12": rec("192.0.2.12", alias=True, cot=("app1-web",), ctx="orch-h12", eps=[EP12]),
            "p20": rec("192.0.2.20", port=2222)}


def full_cfg():
    return {"schema": 1,
            "hosts": {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"], "jobs": "all"},
                      "h12": {"ssh": "app1", "roles": ["helper", "prod"], "jobs": "tests",
                              "watch": ["https://127.0.0.1/app/"]},
                      "p20": {"ssh": "deploy@192.0.2.20:2222", "roles": ["prod"]}},
            "production": {"answer": "set"},
            "deploy": {"command": "./scripts/deploy.sh", "rollback": None, "health": ["https://shop.example/health"]},
            "alerts": {"command": None},
            "jobs": {"api-test": {"stage": "unit", "image": "mcr.microsoft.com/dotnet/sdk:8.0" + PIN, "dir": "api",
                                  "prep": "dotnet restore", "cmd": "dotnet test --no-restore", "memory": "3g",
                                  "timeout": 1200, "cache": {"name": "nuget", "path": "/root/.nuget/packages"},
                                  "detected": "dotnet 8 (api/Api.sln)", "manifest": M1, "files": ["api/Api.sln"]},
                     "web-test": {"stage": "unit", "image": "node:22-bookworm-slim" + PIN, "dir": "web",
                                  "prep": "npm ci", "cmd": "npm test", "memory": "2g", "timeout": 900,
                                  "detected": "node 22 (web/package.json)"},
                     "web-lint": {"stage": "lint", "image": "node:22-bookworm-slim" + PIN, "dir": "web",
                                  "prep": "npm ci", "cmd": "npm run lint", "memory": "2g", "timeout": 600},
                     "app-image": {"kind": "build", "stage": "image", "dir": "web", "dockerfile": "Dockerfile",
                                   "memory": "4g", "timeout": 2400}},
            "policy": {}}


def trust_of(cfg, cot=None, approved="2026-10-30T09:00:00"):
    return {"hash": oc.config_hash(cfg), "approved_at": approved, "scripts": {},
            "cotenants": {"h11": [], "h12": ["app1-web"], "p20": []} if cot is None else cot,
            "config": json.loads(json.dumps(cfg))}


def check_shape(t, st):
    t.assertEqual(set(st), STATUS_KEYS)
    t.assertIn(st["state"], ("new", "incomplete", "unapproved", "drift", "ready"))
    t.assertIn(st["step"], ("count", "helpers", "setup", "jobs", "approve", "fix", "none"))
    t.assertIn(st["next"], ("ask", "fix", "approve", "ready"))
    t.assertIsInstance(st["summary"], list)
    for pr in st["problems"]:
        t.assertEqual(set(pr), PROBLEM_KEYS)
    if st["ask"] is not None:
        a = st["ask"]
        t.assertTrue(ASK_KEYS <= set(a) <= ASK_KEYS | {"examples"})
        t.assertIn(a["kind"], ("choice", "text"))
        t.assertLessEqual(len(a["header"]), 12)  # AskUserQuestion header limit, every ask
        if a["kind"] == "choice":  # AskUserQuestion takes 2-4 options
            t.assertTrue(2 <= len(a["options"]) <= 4, a)
        else:  # a plain message: no options to tick
            t.assertEqual((a["options"], a["multi"]), ([], False), a)
    json.dumps(st)  # the final stdout line must serialise


# ----------------------------------------------------------------------------- spec, hash, loosening
class TestSpec(unittest.TestCase):
    def test_valid(self):
        P = oc.parse_spec
        self.assertEqual(P("app1"), {"spec": "app1", "user": None, "host": "app1", "port": None})
        self.assertEqual(P(" deploy@192.0.2.10 "), {"spec": "deploy@192.0.2.10", "user": "deploy",
                                                    "host": "192.0.2.10", "port": None})
        self.assertEqual(P("deploy@192.0.2.20:2222")["port"], 2222)
        self.assertEqual(P("ci_user@app1.example")["host"], "app1.example")
        self.assertEqual(P("deploy@[2001:db8::10]:22"), {"spec": "deploy@[2001:db8::10]:22", "user": "deploy",
                                                         "host": "2001:db8::10", "port": 22})
        self.assertEqual(P("[2001:db8::10]")["port"], None)

    def test_invalid(self):
        for bad in ("", "  ", "deploy@192.0.2.10; rm -rf /", "$(id)@app1", "-oProxyCommand=x", "a b", "app1|x",
                    "deploy@192.0.2.300", "deploy@app1:0", "deploy@app1:70000", "deploy@app1:", "2001:db8::1",
                    "deploy@", "@app1", "app1.", "a@b@app1", "deploy@[zz::1]", "deploy@-app1", "app1`id`",
                    "x" * 300):
            with self.assertRaises(ValueError, msg=bad) as e:
                oc.parse_spec(bad)
            self.assertRegex(str(e.exception), r"ssh spec|port")

    def test_message_names_spec_and_fix(self):
        with self.assertRaises(ValueError) as e:
            oc.parse_spec("deploy@192.0.2.10 && id")
        self.assertIn("'deploy@192.0.2.10 && id'", str(e.exception))
        self.assertIn("e.g. deploy@192.0.2.10", str(e.exception))

    def test_ssh_args(self):
        self.assertEqual(oc._ssh_args("app1"), ["app1"])
        self.assertEqual(oc._ssh_args("deploy@192.0.2.11"), ["deploy@192.0.2.11"])
        self.assertEqual(oc._ssh_args("deploy@192.0.2.20:2222"), ["-p", "2222", "deploy@192.0.2.20"])
        self.assertEqual(oc._ssh_args("[2001:db8::10]:2222"), ["-p", "2222", "2001:db8::10"])


class TestHashLoosening(unittest.TestCase):
    def test_hash(self):
        a = full_cfg()
        b = json.loads(json.dumps(a))
        b["pending"] = {"helper_count": 3}
        c = {k: a[k] for k in reversed(list(a))}
        self.assertRegex(oc.config_hash(a), r"^[0-9a-f]{64}$")
        self.assertEqual(oc.config_hash(a), oc.config_hash(b))
        self.assertEqual(oc.config_hash(a), oc.config_hash(c))
        b["jobs"]["api-test"]["memory"] = "4g"
        self.assertNotEqual(oc.config_hash(a), oc.config_hash(b))
        no_schema = {k: v for k, v in a.items() if k != "schema"}
        self.assertEqual(oc.config_hash(no_schema), oc.config_hash(a))  # save_config adds schema 1 later

    def _one(self, edit, needle):
        old, new = full_cfg(), full_cfg()
        edit(old, new)
        got = oc.loosening(old, new)
        self.assertEqual(len(got), 1, got)
        self.assertIn(needle, got[0])

    def test_each_loosening(self):
        H = "hosts"
        self._one(lambda o, n: n[H]["h12"].update(roles=["helper"]), "h12 (app1) is no longer a production server")
        self._one(lambda o, n: n[H].pop("p20"), "p20 (deploy@192.0.2.20:2222) is no longer a production server")
        self._one(lambda o, n: (o[H]["h12"].update(jobs="readonly")), "job scope widened from readonly to tests")
        self._one(lambda o, n: (o[H]["h11"].update(jobs="none"), n[H]["h11"].update(jobs="tests")),
                  "widened from none to tests")
        self._one(lambda o, n: o["alerts"].update(command="notify-admin"), "alert command removed")
        self._one(lambda o, n: (o["alerts"].update(command="notify-admin"), n["alerts"].update(command="true")),
                  "alert command changed to true")
        self._one(lambda o, n: n[H]["h12"].update(watch=[]), "stops watching https://127.0.0.1/app/")
        self._one(lambda o, n: n["deploy"].update(command="./deploy2.sh"), "deploy command changed to ./deploy2.sh")
        self._one(lambda o, n: o["deploy"].update(command=None), "deploy command added: ./scripts/deploy.sh")
        self._one(lambda o, n: n["deploy"].update(rollback="./rollback.sh"), "rollback command added: ./rollback.sh")
        self._one(lambda o, n: n[H].update(h13={"ssh": "deploy@192.0.2.13", "roles": ["helper"], "jobs": "tests"}),
                  "new helper h13 (deploy@192.0.2.13)")
        self._one(lambda o, n: n[H]["p20"].update(roles=["prod", "helper"], jobs="readonly"), "new helper p20")
        self._one(lambda o, n: n[H]["h11"].update(ssh="deploy@198.51.100.11"), "ssh target changed")
        self._one(lambda o, n: n.update(policy={"require_digest": False}), "policy overrides changed")

    def test_alert_command_added_is_loosening(self):
        # orch runs the alert command itself on this machine (outside the guard): a new one needs approval (R7)
        self._one(lambda o, n: n["alerts"].update(command="./scripts/notify.sh"),
                  "alert command added: ./scripts/notify.sh")
        self._one(lambda o, n: (o.pop("alerts"), n["alerts"].update(command="notify-admin")),
                  "alert command added: notify-admin")

    def test_job_edits_that_loosen(self):
        # the deploy gate (policy deploy_require, default lint + unit) loses a job it needs (R11)
        self._one(lambda o, n: n["jobs"].pop("web-lint"),
                  "job web-lint (lint) removed, so the deploy gate stops requiring it to pass")
        self._one(lambda o, n: n["jobs"]["api-test"].update(stage="e2e"),
                  "job api-test: stage unit -> e2e, so the deploy gate stops requiring it to pass")
        # a job moves into a stage that a narrower scope runs: an image build now runs on lint + tests hosts (R10/R11)
        self._one(lambda o, n: n["jobs"]["app-image"].update(kind="container", stage="e2e", cmd="./e2e.sh",
                                                             image="python:3.12-slim" + PIN),
                  "job app-image: stage image -> e2e, so helpers limited to lint + tests, no image builds now run it")
        self._one(lambda o, n: (o["jobs"]["web-test"].update(stage="image"), n["jobs"]["web-test"].update(stage="e2e")),
                  "job web-test: stage image -> e2e, so helpers limited to lint + tests")
        both = full_cfg(), full_cfg()
        for c in both:
            c["policy"] = {"deploy_require": "e2e"}  # read like orch_deploy: a comma string works too
        both[1]["jobs"].pop("web-lint")
        self.assertEqual(oc.loosening(*both), [])
        both[0]["jobs"]["e2e-suite"] = {"stage": "e2e", "image": "python:3.12-slim" + PIN, "cmd": "./e2e.sh"}
        self.assertEqual(oc.loosening(*both), ["job e2e-suite (e2e) removed, so the deploy gate stops requiring it "
                                               "to pass"])

    def test_source_allow_added_is_loosening(self):
        # a path exempt from the outgoing secret scan ships to helpers unchecked: a new one needs approval
        self._one(lambda o, n: n["jobs"]["api-test"].update(source_allow=["^src/tests/fake[.]py$"]),
                  "job api-test: secret scan skips ^src/tests/fake[.]py$")
        both = full_cfg(), full_cfg()
        for c in both:
            c["jobs"]["api-test"]["source_allow"] = ["^src/a[.]py$"]
        self.assertEqual(oc.loosening(*both), [])
        both[1]["jobs"]["api-test"]["source_allow"] = []
        self.assertEqual(oc.loosening(*both), [])

    def test_source_allow_reaches_the_engine_job(self):
        j = {"image": "python:3.12-slim" + PIN, "cmd": "true", "source_allow": ["^src/a[.]py$"]}
        self.assertEqual(oc._translate("x", j)["source_allow"], ["^src/a[.]py$"])
        self.assertNotIn("source_allow", oc._translate("x", {**j, "source_allow": []}))

    def test_tightening_is_not_loosening(self):
        edits = [lambda n: n["hosts"]["h11"].update(roles=["helper", "prod"], jobs="tests"),
                 lambda n: n["hosts"]["h12"].update(jobs="readonly"),
                 lambda n: n["hosts"]["h11"].update(watch=["http://127.0.0.1:9000/"]),
                 lambda n: n["deploy"].update(command=None),
                 lambda n: n["hosts"].pop("h11"),
                 lambda n: n["hosts"].update(p21={"ssh": "deploy@192.0.2.21", "roles": ["prod"]}),
                 lambda n: n["jobs"]["api-test"].update(memory="4g", cmd="dotnet test"),
                 lambda n: n["jobs"]["web-lint"].update(stage="unit"),  # still a deploy_require stage
                 lambda n: n["jobs"].pop("app-image"),  # image builds are not a deploy_require stage
                 lambda n: n["jobs"].update(e2e={"stage": "e2e", "image": "python:3.12-slim" + PIN, "cmd": "./e2e.sh"}),
                 lambda n: n.update(pending={"helper_count": 2})]
        for e in edits:
            new = full_cfg()
            e(new)
            self.assertEqual(oc.loosening(full_cfg(), new), [], new)
        self.assertEqual(oc.loosening(full_cfg(), full_cfg()), [])


# ----------------------------------------------------------------------------- registry, trust, runs, state
class TestRegistry(unittest.TestCase):
    def test_names(self):
        reg = {}
        self.assertEqual(oc.assign_name(reg, {"hostname": "192.0.2.11", "port": 22}), "h11")
        self.assertEqual(oc.assign_name(reg, {"hostname": "App-1.Example", "port": 22}), "app-1")
        self.assertEqual(oc.assign_name(reg, {"hostname": "2001:db8::12", "port": 22}), "h12")
        self.assertEqual(set(reg["nodes"]), {"h11", "app-1", "h12"})
        with self.assertRaises(ValueError):
            oc.assign_name(reg, {"hostname": ""})

    def test_identity_and_clash(self):
        reg = {}
        a = oc.assign_name(reg, {"hostname": "192.0.2.11", "port": None, "docker_id": None})
        self.assertEqual(oc.assign_name(reg, {"hostname": "192.0.2.11", "port": 22, "docker_id": "D1"}), a)
        self.assertEqual(reg["nodes"][a]["docker_id"], "D1")  # learned once known
        self.assertEqual(oc.assign_name(reg, {"hostname": "192.0.2.11", "port": 22, "docker_id": "D1"}), "h11")
        self.assertEqual(oc.assign_name(reg, {"hostname": "192.0.2.11", "port": 22, "docker_id": "D2"}), "h11b")
        self.assertEqual(oc.assign_name(reg, {"hostname": "198.51.100.11", "port": 22}), "h11c")
        self.assertEqual(oc.assign_name(reg, {"hostname": "192.0.2.11", "port": 2222}), "h11d")
        self.assertEqual(oc.assign_name(reg, {"hostname": "198.51.100.11", "port": 22}), "h11c")
        self.assertEqual(oc.assign_name(reg, {"hostname": "192.0.2.11", "port": 22, "docker_id": "D2"}), "h11b")

    def test_save_load(self):
        h = tmpdir(self)
        self.assertEqual(oc.registry_load(h), {"nodes": {}, "projects": []})
        reg = oc.registry_load(h)
        oc.assign_name(reg, {"hostname": "192.0.2.11", "port": 22})
        oc.registry_save(h, reg)
        self.assertEqual(oc.registry_load(h)["nodes"]["h11"], {"hostname": "192.0.2.11", "port": 22, "docker_id": None})
        (h / "registry.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(oc.ConfigError):
            oc.registry_load(h)

    def test_home(self):
        self.assertEqual(oc.home({"ORCH_HOME": "/srv/orch-home"}), Path("/srv/orch-home"))
        self.assertEqual(oc.home({}), Path.home() / ".helper-orch")


class TestTrustRunsState(unittest.TestCase):
    def setUp(self):
        d = tmpdir(self)
        self.home = d / "home"
        self.p = oc.Project(d / "shop", d / "shop" / ".git" / "orch", True)
        (self.p.store / "state").mkdir(parents=True)

    def test_trust_roundtrip(self):
        cfg = full_cfg()
        cfg["pending"] = {"x": 1}
        self.assertIsNone(oc.trust_get(self.home, self.p))
        oc.trust_set(self.home, self.p, cfg, {"scripts/deploy.sh": "b" * 64}, state_hosts=state())
        t = oc.trust_get(self.home, self.p)
        self.assertEqual(t["hash"], oc.config_hash(cfg))
        self.assertRegex(t["approved_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual(t["scripts"], {"scripts/deploy.sh": "b" * 64})
        self.assertEqual(t["cotenants"], {"h11": [], "h12": ["app1-web"], "p20": []})
        self.assertNotIn("pending", t["config"])
        self.assertEqual(t["root"], self.p.root.as_posix())
        raw = json.loads((self.home / "trust.json").read_text(encoding="utf-8"))
        self.assertEqual(list(raw), [self.p.store.resolve().as_posix()])  # keyed by absolute store path
        oc.trust_set(self.home, self.p, cfg, {}, state_hosts=state())
        self.assertEqual(oc.registry_load(self.home)["projects"], [self.p.store.resolve().as_posix()])
        other = oc.Project(self.p.root.parent / "web", self.p.root.parent / "web" / ".orch", False)
        self.assertIsNone(oc.trust_get(self.home, other))
        self.assertEqual([x.name for x in self.home.iterdir() if x.name.endswith(".tmp")], [])

    def test_trust_reads_store_state(self):
        oc.save_state(self.p, "hosts", {"h11": rec("192.0.2.11", cot=("db",))})
        oc.trust_set(self.home, self.p, full_cfg(), {})
        self.assertEqual(oc.trust_get(self.home, self.p)["cotenants"], {"h11": ["db"]})

    def test_runs(self):
        self.assertEqual(oc.read_runs(self.p), [])
        r1 = {"job": "api-test", "sha": "c" * 40, "wt": False, "node": "h11", "result": "pass", "exit": 0,
              "wall_s": 12.5, "jobid": "j1", "ts": "2026-10-30T09:00:00"}
        oc.append_run(self.p, r1)
        with open(self.p.store / "state" / "runs.jsonl", "a", encoding="utf-8") as f:
            f.write('{"job": "torn"\n\n[1]\n')
        oc.append_run(self.p, {**r1, "jobid": "j2"})
        self.assertEqual([r["jobid"] for r in oc.read_runs(self.p)], ["j1", "j2"])

    def test_runs_parallel_and_capped(self):
        import threading
        base = {"job": "web-test", "sha": "d" * 40, "result": "pass"}

        def worker(i):
            for k in range(15):
                oc.append_run(self.p, {**base, "jobid": f"{i}-{k}"})
        th = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for x in th:
            x.start()
        for x in th:
            x.join()
        runs = oc.read_runs(self.p)
        self.assertEqual((len(runs), len({r["jobid"] for r in runs})), (90, 90))
        with mock.patch.object(oc, "RUNS_KEEP", 10):
            oc.append_run(self.p, {**base, "jobid": "last"})
        runs = oc.read_runs(self.p)
        self.assertEqual((len(runs), runs[-1]["jobid"]), (10, "last"))
        self.assertEqual(sorted(x.name for x in (self.p.store / "state").iterdir()), ["runs.jsonl", "runs.jsonl.lock"])

    def test_state(self):
        self.assertEqual(oc.load_state(self.p, "hosts", {}), {})
        oc.save_state(self.p, "jobs-detected", {"jobs": {}})
        self.assertEqual(oc.load_state(self.p, "jobs-detected"), {"jobs": {}})
        (self.p.store / "state" / "hosts.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(oc.load_state(self.p, "hosts", {"d": 1}), {"d": 1})
        for bad in ("../x", "a/b", "", "Hosts"):
            with self.assertRaises(ValueError):
                oc.save_state(self.p, bad, {})


class TestConfigIO(unittest.TestCase):
    def setUp(self):
        d = tmpdir(self)
        self.p = oc.Project(d, d / ".orch", False)
        self.p.store.mkdir()

    def test_roundtrip(self):
        self.assertEqual(oc.load_config(self.p), {})
        cfg = full_cfg()
        del cfg["schema"]
        oc.save_config(self.p, cfg)
        got = oc.load_config(self.p)
        self.assertEqual(got["schema"], 1)
        self.assertEqual(got, full_cfg())
        self.assertEqual([x.name for x in self.p.store.iterdir()], ["config.json"])  # no tmp left behind

    def test_errors(self):
        path = self.p.store / "config.json"
        path.write_text("{bad json", encoding="utf-8")
        with self.assertRaises(oc.ConfigError) as e:
            oc.load_config(self.p)
        self.assertIn("is not valid JSON", str(e.exception))
        cases = [({"schema": 2}, "schema is 2"),
                 ({**full_cfg(), "extra": 1}, "unknown key 'extra'"),
                 ({"schema": 1, "hosts": {"h11": {"ssh": "deploy@192.0.2.11; id", "roles": ["helper"]}}}, "ssh spec"),
                 ({"schema": 1, "hosts": {"h12": {"ssh": "app1", "roles": ["helper", "prod"], "jobs": "all"}}},
                  "never runs every job"),
                 ({"schema": 1, "hosts": {"H1": {"ssh": "app1", "roles": ["helper"]}}}, "host name 'H1'"),
                 ({"schema": 1, "hosts": {"h11": {"ssh": "app1", "roles": []}}}, "roles must be"),
                 ({"schema": 1, "jobs": {"t-ok": {"cmd": "true"}}}, "built-in job"),
                 ({"schema": 1, "jobs": {"x": {"cmd": "true", "nope": 1}}}, "unknown key 'nope'"),
                 ({"schema": 1, "jobs": {"x": {"cmd": "true", "source_allow": ["src/a.py"]}}}, "anchored path regexes"),
                 ({"schema": 1, "jobs": {"x": {"cmd": "true", "source_allow": ["^src/(a$"]}}}, "one exact file"),
                 ({"schema": 1, "jobs": {"x": {"cmd": "true", "source_allow": ["^.*$"]}}}, "one exact file"),
                 ({"schema": 1, "jobs": {"x": {"cmd": "true", "source_allow": ["^a$|^.*$"]}}}, "one exact file"),
                 ({"schema": 1, "deploy": {"command": "a\nb"}}, "deploy.command"),
                 ({"schema": 1, "deploy": {"health": ["ftp://x"]}}, "deploy.health"),
                 ({"schema": 1, "deploy": {"health": [{"url": "https://x.example/h", "insecure": "yes"}]}},
                  "deploy.health"),
                 ({"schema": 1, "deploy": {"health": [{"url": "https://x.example/h", "verify": False}]}},
                  "deploy.health"),
                 ({"schema": 1, "production": {"answer": "maybe"}}, "production must be"),
                 ({"schema": 1, "policy": {"bogus": 1}}, "policy: unknown key"),
                 ({"schema": 1, "policy": {"io": {"read_bps": "150mb", "write_bps": "1 gb"}}},
                  "policy io.write_bps must be a rate"),
                 ({"schema": 1, "policy": {"io": {"read_bps": True, "write_bps": 1048576}}},
                  "policy io.read_bps must be a rate")]
        for cfg, needle in cases:
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaises(oc.ConfigError, msg=needle) as e:
                oc.load_config(self.p)
            self.assertIn(needle, str(e.exception))
            self.assertIn("fix the file or run orch init", str(e.exception))
        with self.assertRaises(oc.ConfigError):
            oc.save_config(self.p, {"hosts": {"h11": {"ssh": "a b", "roles": ["helper"]}}})
        for ok in ({"read_bps": "150mb", "write_bps": "80MiB", "blkio_weight": None}, {"read_bps": 157286400,
                                                                                       "write_bps": "1.5g"}, None):
            oc.save_config(self.p, {"schema": 1, "policy": {"io": ok}})  # policy io (R6): rates in docker's syntax
            self.assertEqual(oc.load_config(self.p)["policy"]["io"], ok)


# ----------------------------------------------------------------------------- project location
class TestProject(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)

    def _git_argv(self, start):
        return ["git", "-C", str(start), "rev-parse", "--show-toplevel", "--git-common-dir"]

    def test_git_main_and_subdir(self):
        root = self.d / "shop"
        (root / "sub").mkdir(parents=True)
        run = FakeRun(git={root.as_posix(): (0, f"{root.as_posix()}\n.git\n", ""),
                           (root / "sub").as_posix(): (0, f"{root.as_posix()}\n../.git\n", "")})
        self.assertIsNone(oc.find_project(root, env={}, run=run))  # git repo, no setup yet
        self.assertEqual(run.calls, [self._git_argv(root)])
        p, warns = oc.init_project(root / "sub", env={}, run=run)
        self.assertEqual((p, warns), (oc.Project(root, root / ".git" / "orch", True), []))
        self.assertTrue((p.store / "state").is_dir())
        (p.store / "config.json").write_text("{}", encoding="utf-8")
        run.calls.clear()
        self.assertEqual(oc.find_project(root / "sub", env={}, run=run), p)
        self.assertEqual(run.count(["git"]), 1)

    def test_worktree_uses_main_repo_store(self):
        main, wt = self.d / "main", self.d / "wt"
        (main / ".git" / "orch").mkdir(parents=True)
        (main / ".git" / "orch" / "config.json").write_text("{}", encoding="utf-8")
        (wt / "deep").mkdir(parents=True)
        run = FakeRun(git={(wt / "deep").as_posix(): (0, f"{wt.as_posix()}\n{(main / '.git').as_posix()}\n", "")})
        p = oc.find_project(wt / "deep", env={}, run=run)
        self.assertEqual(p, oc.Project(wt, main / ".git" / "orch", True))
        self.assertEqual(run.calls, [self._git_argv(wt / "deep")])
        p2, _ = oc.init_project(env={"ORCH_PROJECT": str(wt / "deep")}, run=run)  # ORCH_PROJECT replaces the cwd
        self.assertEqual(p2, p)

    def test_explicit_project_beats_orch_project(self):
        # --project DIR (start) wins over ORCH_PROJECT, which wins over the cwd (R13): deploy --check, live and the
        # journal pass --project as start and must not land in the ORCH_PROJECT project
        a, b = self.d / "a", self.d / "b"
        for d in (a, b):
            (d / ".git" / "orch").mkdir(parents=True)
            (d / ".git" / "orch" / "config.json").write_text("{}", encoding="utf-8")
        run = FakeRun(git={d.as_posix(): (0, f"{d.as_posix()}\n.git\n", "") for d in (a, b)})
        env = {"ORCH_PROJECT": str(a)}
        self.assertEqual(oc.find_project(b, env=env, run=run).root, b)
        self.assertEqual(oc.init_project(b, env=env, run=run)[0].root, b)
        self.assertEqual(oc.find_project(None, env=env, run=run).root, a)
        with self.assertRaises(oc.ConfigError) as e:  # a missing --project never falls back to ORCH_PROJECT
            oc.find_project(self.d / "missing", env=env, run=run)
        self.assertIn("missing does not exist", str(e.exception))

    def test_non_git_walk_up_and_init(self):
        ws = self.d / "ws"
        (ws / "a" / "b").mkdir(parents=True)
        run = FakeRun()
        self.assertIsNone(oc.find_project(ws / "a" / "b", env={}, run=run))
        p, warns = oc.init_project(ws, env={}, run=run)
        self.assertEqual(p, oc.Project(ws, ws / ".orch", False))
        self.assertEqual(len(warns), 1)
        self.assertIn("is not a git repository", warns[0])
        self.assertIn("with a .gitignore that keeps it out of git", warns[0])
        (p.store / "config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(oc.find_project(ws / "a" / "b", env={}, run=run), p)
        self.assertEqual(oc.init_project(ws / "a", env={}, run=run), (p, []))  # reuses the workspace store
        self.assertFalse((ws / "a" / ".orch").exists())
        self.assertEqual(run.count(["git"]), 4)  # one git call per find/init

    def test_non_git_store_gets_a_gitignore(self):
        # council 2 MF7: a store outside git ignores itself, so a later git init / add -A or a copy never commits it
        ws = self.d / "ws"
        ws.mkdir()
        p, _ = oc.init_project(ws, env={}, run=FakeRun())
        self.assertEqual((p.store / ".gitignore").read_bytes(), b"*\n")
        (p.store / ".gitignore").unlink()  # a store an older orch made: init_project adds the file when it is missing
        (p.store / "config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(oc.init_project(ws, env={}, run=FakeRun())[0], p)
        self.assertEqual((p.store / ".gitignore").read_bytes(), b"*\n")
        (p.store / ".gitignore").write_text("state/\n", encoding="utf-8")  # the user's own file is left as it is
        oc.init_project(ws, env={}, run=FakeRun())
        self.assertEqual((p.store / ".gitignore").read_text(encoding="utf-8"), "state/\n")
        root = self.d / "shop"  # a git store (<common-dir>/orch inside .git) needs none
        run = FakeRun(git={root.as_posix(): (0, f"{root.as_posix()}\n.git\n", "")})
        root.mkdir()
        g, _ = oc.init_project(root, env={}, run=run)
        self.assertTrue(g.git)
        self.assertFalse((g.store / ".gitignore").exists())

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_non_git_store_stays_out_of_a_later_repository(self):
        ws = self.d / "later"
        ws.mkdir()
        p, _ = oc.init_project(ws, env={}, run=FakeRun())
        oc.save_config(p, {"schema": 1, "hosts": {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"]}}})
        (ws / "app.py").write_text("print(1)\n", encoding="utf-8")
        git = ["git", "-c", "core.autocrlf=false", "-C", str(ws)]
        for argv in (git + ["init", "-q"], git + ["add", "-A"]):
            subprocess.run(argv, check=True, capture_output=True)
        staged = subprocess.run(git + ["diff", "--cached", "--name-only"], check=True, capture_output=True,
                                text=True).stdout.split()
        self.assertEqual(staged, ["app.py"])

    @unittest.skipUnless(os.name == "nt", "MSYS paths only exist on Windows")
    def test_msys_git_paths(self):
        msys = "/" + self.d.drive[0].lower() + self.d.as_posix()[2:]
        (self.d / ".git" / "orch").mkdir(parents=True)
        (self.d / ".git" / "orch" / "config.json").write_text("{}", encoding="utf-8")
        run = FakeRun(git={self.d.as_posix(): (0, f"{msys}\n/cygdrive{msys}/.git\n", "")})
        self.assertEqual(oc.find_project(self.d, env={}, run=run), oc.Project(self.d, self.d / ".git" / "orch", True))

    def test_git_missing_is_not_git(self):
        run = FakeRun(missing={"git"})
        self.assertIsNone(oc.find_project(self.d, env={}, run=run))

    def test_git_errors(self):
        run = FakeRun(git={self.d.as_posix(): (128, "", "fatal: detected dubious ownership in repository at "
                                                         "'C:/work/shop'\nTo add an exception ...")})
        with self.assertRaises(oc.ConfigError) as e:
            oc.find_project(self.d, env={}, run=run)
        self.assertIn("fix: git config --global --add safe.directory C:/work/shop", str(e.exception))
        run = FakeRun(git={self.d.as_posix(): (128, "", "fatal: this operation must be run in a work tree")})
        with self.assertRaises(oc.ConfigError) as e:
            oc.init_project(self.d, env={}, run=run)
        self.assertIn("must be run in a work tree", str(e.exception))
        with self.assertRaises(oc.ConfigError) as e:
            oc.find_project(self.d / "missing", env={}, run=FakeRun())
        self.assertIn("does not exist", str(e.exception))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_real_git_worktree(self):
        main, wt = self.d / "main", self.d / "wt"
        (main / "sub").mkdir(parents=True)
        g = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
        for argv in (g + ["init", "-q", str(main)], g + ["-C", str(main), "commit", "-q", "--allow-empty", "-m", "i"],
                     g + ["-C", str(main), "worktree", "add", "-q", "-b", "wtb", str(wt)]):
            subprocess.run(argv, check=True, capture_output=True)
        (wt / "deep").mkdir()
        p, warns = oc.init_project(wt / "deep", env={})
        self.assertEqual(warns, [])
        self.assertEqual(p.store, (main / ".git" / "orch").resolve())
        self.assertEqual(p.root, wt.resolve())
        oc.save_config(p, {"schema": 1})
        q = oc.find_project(main / "sub", env={})
        self.assertEqual((q.root, q.store, q.git), (main.resolve(), p.store, True))
        roots = oc._worktree_roots(p.store, {}, subprocess.run)
        self.assertEqual([Path(r).resolve() for r in roots], [main.resolve(), wt.resolve()])


# ----------------------------------------------------------------------------- status state machine
class TestStatus(unittest.TestCase):
    def setUp(self):
        d = tmpdir(self)
        self.P = oc.Project(d / "shop", d / "shop" / ".git" / "orch", True)

    def st(self, cfg, sh=None, trust=None, detected=None, candidates=(), **kw):
        s = oc.status(self.P, cfg, state() if sh is None else sh, trust, detected, candidates, **kw)
        check_shape(self, s)
        return s

    def test_new_count(self):
        s = self.st({}, {})
        self.assertEqual((s["state"], s["step"], s["next"], s["problems"]), ("new", "count", "ask", []))
        self.assertEqual(s["summary"], [f"no orch setup yet for {self.P.root.as_posix()}"])
        self.assertEqual(s["ask"], {"kind": "choice", "header": "Helpers",
                                    "question": "How many helper servers will orch use?",
                                    "options": ["1", "2", "3", "4+"], "multi": False, "answer_key": "helper_count",
                                    "example": {"helper_count": 2}})
        s = self.st({"schema": 1, "production": {"answer": "later"}}, {})
        self.assertEqual((s["state"], s["step"]), ("incomplete", "count"))

    def test_helpers_step(self):
        cfg = {"schema": 1, "pending": {"helper_count": 2}}
        s = self.st(cfg, {}, candidates=("app1", "deploy@192.0.2.11", "app1", "b1", "c1", "d1"))
        self.assertEqual((s["state"], s["step"], s["next"]), ("incomplete", "helpers", "ask"))
        self.assertEqual(s["ask"], {"kind": "choice", "header": "Helper ssh",
                                    "question": "For each of the 2 helpers, what do you type after ssh? "
                                                "(alias, or user@IP[:port]; comma separated)",
                                    "options": ["app1", "deploy@192.0.2.11", "b1", "c1"], "multi": True,
                                    "answer_key": "helpers",
                                    "example": {"helpers": ["deploy@192.0.2.11", "deploy@192.0.2.12"]}})
        s = self.st({"schema": 1, "pending": {"helper_count": 1}}, {})
        self.assertEqual((s["ask"]["kind"], s["ask"]["options"], s["ask"]["multi"]), ("text", [], False))
        self.assertEqual(s["ask"]["question"], "For the helper, what do you type after ssh? (alias, or user@IP[:port])")
        cfg = {"schema": 1, "pending": {"helper_count": 2},
               "hosts": {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"]}}}
        s = self.st(cfg, state())  # the re-ask after two specs merged into one machine: only the missing helper
        self.assertEqual(s["ask"], {"kind": "text", "header": "Helper ssh",
                                    "question": "Saved: h11 (deploy@192.0.2.11). For the new helper, what do you type "
                                                "after ssh? (alias, or user@IP[:port])",
                                    "options": [], "multi": False, "answer_key": "helpers",
                                    "example": {"helpers": ["deploy@192.0.2.12"]}})

    def test_helpers_step_asks_only_for_new_helpers(self):
        # R31: raising the count on a saved setup asks for the new helpers only; saved hosts are never offered
        cfg = full_cfg()
        cfg["pending"] = {"helper_count": 3}
        cands = ("deploy@192.0.2.11 (192.0.2.11:22)", "spare (198.51.100.30:22)", "app1", "b1 (198.51.100.31:22)")
        s = self.st(cfg, candidates=cands)
        self.assertEqual((s["state"], s["step"], s["next"]), ("incomplete", "helpers", "ask"))
        self.assertEqual(s["ask"], {"kind": "choice", "header": "Helper ssh",
                                    "question": "Saved: h11 (deploy@192.0.2.11), h12 (app1). For the new helper, what do "
                                                "you type after ssh? (alias, or user@IP[:port])",
                                    "options": ["spare (198.51.100.30:22)", "b1 (198.51.100.31:22)"], "multi": False,
                                    "answer_key": "helpers", "example": {"helpers": ["deploy@192.0.2.12"]}})
        cfg["pending"] = {"helper_count": 5}
        a = self.st(cfg, candidates=cands)["ask"]
        self.assertEqual((a["multi"], a["example"]),
                         (True, {"helpers": ["deploy@192.0.2.12", "deploy@192.0.2.13", "deploy@192.0.2.14"]}))
        self.assertTrue(a["question"].endswith("For each of the 3 new helpers, what do you type after ssh? (alias, or "
                                               "user@IP[:port]; comma separated)"), a["question"])

    def test_helpers_ask_follows_ask_tool_rules(self):
        # R32: a choice needs 2-4 options, so one candidate goes into a text question; multi only for 2+ answers
        one = ("spare (198.51.100.30:22)",)
        a = self.st({"schema": 1, "pending": {"helper_count": 2}}, {}, candidates=one)["ask"]
        self.assertEqual((a["kind"], a["options"], a["multi"]), ("text", [], False))
        self.assertTrue(a["question"].endswith("comma separated); known: spare (198.51.100.30:22)"), a["question"])
        two = one + ("b1 (198.51.100.31:22)",)
        a = self.st({"schema": 1, "pending": {"helper_count": 1}}, {}, candidates=two)["ask"]
        self.assertEqual((a["kind"], a["options"], a["multi"]), ("choice", list(two), False))
        a = self.st({"schema": 1, "pending": {"helper_count": 2}}, {}, candidates=two)["ask"]
        self.assertEqual((a["kind"], a["multi"]), ("choice", True))

    def _setup_cfg(self):
        return {"schema": 1, "pending": {"helper_count": 2},
                "hosts": {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"]},
                          "h12": {"ssh": "app1", "roles": ["helper"]}}}

    def test_another_namespace_keeps_an_approved_setup_from_ready(self):
        # council 2 MF5: the probe's foreign-namespace problem is a fix step in every state, with the exact fix
        import orch_discover as od  # noqa: PLC0415 - the problem record the probe writes
        pr = od.foreign_problem([{"kind": "container", "name": "agentx-1030-job", "family": "io.other"}], "agentx-",
                                "ssh deploy@192.0.2.11 'docker ps -a --filter name=agentx-'")
        cfg = full_cfg()
        sh = {**state(), "h11": {**rec("192.0.2.11", ctx="orch-h11"), "ok": False, "problem": pr}}
        s = self.st(cfg, sh, trust=trust_of(cfg))
        self.assertEqual((s["state"], s["step"], s["next"]), ("drift", "fix", "fix"))
        self.assertEqual(s["problems"][0]["host"], "h11 (deploy@192.0.2.11)")
        self.assertIn("h11 (deploy@192.0.2.11): 1 object named agentx-* belongs to another orch namespace (label "
                      "io.other.*): an older orch or another master -> run that orch's gc --session-end on this host "
                      "(removing volumes needs the user's OK); or, if no other master uses this host, remove them by "
                      "hand: docker rm -f agentx-1030-job\n", s["ask"]["question"])
        self.assertEqual(self.st(cfg, state(), trust=trust_of(cfg))["state"], "ready")  # gone: ready again

    def test_fix_step_during_interview(self):
        pr = {"what": "ssh: Permission denied (publickey)", "tried": "ssh -o BatchMode=yes deploy@192.0.2.11 true",
              "fix": "ssh-copy-id deploy@192.0.2.11", "class": "auth"}
        sh = {**state(), "h11": {"ok": False, "spec": "deploy@192.0.2.11", "problem": pr}}
        s = self.st(self._setup_cfg(), sh)
        self.assertEqual((s["state"], s["step"], s["next"]), ("incomplete", "fix", "fix"))
        self.assertEqual(s["problems"], [{"host": "h11 (deploy@192.0.2.11)", "what": pr["what"], "tried": pr["tried"],
                                          "fix": pr["fix"]}])
        # R33: one line per problem (host: what -> fix); the exact command tried stays in problems only
        self.assertEqual(s["ask"], {"kind": "choice", "header": "Fix", "question":
                                    "h11 (deploy@192.0.2.11): ssh: Permission denied (publickey) -> "
                                    "ssh-copy-id deploy@192.0.2.11\n"
                                    "Run the fix shown, then choose retry; or remove the host.",
                                    "options": ["retry", "remove h11"], "multi": False, "answer_key": "remove_hosts",
                                    "example": {"remove_hosts": ["h11"]}})
        s = self.st(self._setup_cfg(), {})  # never probed (orch init --offline)
        self.assertEqual(s["step"], "fix")
        self.assertEqual(s["problems"][0]["what"], "not probed yet")
        self.assertEqual(s["ask"]["options"], ["retry", "remove h11, h12"])
        self.assertEqual(s["ask"]["question"].splitlines(), [
            "h11 (deploy@192.0.2.11): not probed yet -> run orch init (without --offline) to probe it",
            "h12 (app1): not probed yet -> run orch init (without --offline) to probe it",
            "Run the fixes shown, then choose retry; or remove the hosts."])
        tried = "ssh -o BatchMode=yes -o ConnectTimeout=8 deploy@192.0.2.11 true"
        net = {"what": "ssh: connect to host 192.0.2.11 port 22: Connection timed out", "tried": tried,
               "fix": f"check that 192.0.2.11 is up and port 22 is open from this machine, then retry: {tried}"}
        s = self.st(self._setup_cfg(), {**state(), "h11": {"ok": False, "problem": net}})
        self.assertEqual(s["ask"]["question"].count(tried), 0)  # the old fix text repeated the tried command
        self.assertIn("-> check that 192.0.2.11 is up and port 22 is open from this machine\n", s["ask"]["question"])
        self.assertEqual(s["problems"][0]["tried"], tried)

    def test_setup_step(self):
        sh = {"h11": rec("192.0.2.11"), "h12": rec("192.0.2.12", cot=("app1-web", "app1-db"))}
        s = self.st(self._setup_cfg(), sh)
        self.assertEqual((s["state"], s["step"], s["next"]), ("incomplete", "setup", "ask"))
        self.assertEqual(s["ask"]["question"], "\n".join([
            "Defaults are in [brackets]; reply ok to accept them all, or change any line.",
            "Helpers (role: helper only | helper + production; jobs: everything | lint + tests, no image builds | "
            "read-only commands only | nothing):",
            "  h11 (deploy@192.0.2.11): 8 cpu, 15 GB, no other containers [helper only; everything]",
            "  h12 (app1): 8 cpu, 15 GB, 2 other containers [helper only; lint + tests, no image builds]",
            "Production servers (what you type after ssh, comma separated), or none / later [none]",
            "Deploy command - runs on THIS machine and must reach production itself, e.g. ./deploy.sh [later]",
            "Health URL(s) checked after a deploy [-]",
            "Rollback command (optional) [-]",
            "Alert command when a helper's own apps degrade (optional; gets the message on stdin) [none]",
            "Extra app URLs to watch on shared helpers (optional) [-]"]))
        self.assertEqual({k: v for k, v in s["ask"].items() if k != "question"},
                         {"kind": "text", "header": "Setup", "options": [], "multi": False, "answer_key": "setup",
                          "example": {"hosts": {"h11": {"roles": ["helper"], "jobs": "all"},
                                                "h12": {"roles": ["helper"], "jobs": "tests"}},
                                      "production": "none", "deploy": "later", "alerts": "none"}})

    def test_setup_step_asks_only_missing(self):
        # R30: an approved setup that gains a helper asks for that helper alone: no production, deploy or alert line
        # again, and an example that never re-sends saved answers
        cfg = full_cfg()
        cfg["hosts"]["h13"] = {"ssh": "deploy@192.0.2.13", "roles": ["helper"]}
        sh = {**state(), "h13": {**rec("192.0.2.13", os_="windows", buildx=False), "facts": {
            **rec("192.0.2.13", os_="windows", buildx=False)["facts"], "cotenants": None}}}
        s = self.st(cfg, sh, trust=trust_of(full_cfg()))
        self.assertEqual((s["state"], s["step"], s["next"]), ("incomplete", "setup", "ask"))
        self.assertEqual(s["ask"]["question"].splitlines(), [
            "Defaults are in [brackets]; reply ok to accept them all, or change any line.",
            "Helpers (role: helper only | helper + production; jobs: everything | lint + tests, no image builds | "
            "read-only commands only | nothing):",
            "  h13 (deploy@192.0.2.13): windows, 8 cpu, 15 GB, no buildx, other containers unknown "
            "[helper only; lint + tests, no image builds]",
            "Extra app URLs to watch on shared helpers (optional) [-]"])
        self.assertEqual(s["ask"]["example"], {"hosts": {"h13": {"roles": ["helper"], "jobs": "tests"}}})
        # an unscoped saved helper is asked too, with its own watch URLs as the default
        del cfg["hosts"]["h12"]["jobs"]
        s = self.st(cfg, sh, trust=trust_of(full_cfg()))
        lines = s["ask"]["question"].splitlines()
        self.assertEqual(lines[2], "  h12 (app1): 8 cpu, 15 GB, 1 other container [helper + production; read-only "
                                   "commands only]")
        self.assertEqual(lines[-1], "Extra app URLs to watch on shared helpers (optional) [h12: https://127.0.0.1/app/]")
        self.assertEqual(list(s["ask"]["example"]), ["hosts"])
        self.assertEqual(s["ask"]["example"]["hosts"]["h12"],
                         {"roles": ["helper", "prod"], "jobs": "readonly", "watch": ["https://127.0.0.1/app/"]})

    def test_setup_step_reasks_unanswered_deploy_and_alerts(self):
        # R30: a setup reply that left out deploy and alerts brings the setup step back for exactly those lines
        cfg = full_cfg()
        del cfg["deploy"], cfg["alerts"]
        s = self.st(cfg, trust=None)
        self.assertEqual((s["state"], s["step"], s["next"]), ("incomplete", "setup", "ask"))
        self.assertEqual(s["ask"]["question"].splitlines(), [
            "Defaults are in [brackets]; reply ok to accept them all, or change any line.",
            "Deploy command - runs on THIS machine and must reach production itself, e.g. ./deploy.sh [later]",
            "Health URL(s) checked after a deploy [-]",
            "Rollback command (optional) [-]",
            "Alert command when a helper's own apps degrade (optional; gets the message on stdin) [none]"])
        self.assertEqual(s["ask"]["example"], {"deploy": "later", "alerts": "none"})
        cfg["deploy"] = {"answer": "later"}  # "later" is an answer
        self.assertEqual(self.st(cfg)["ask"]["example"], {"alerts": "none"})
        cfg["alerts"] = {"command": None}
        self.assertEqual(self.st(cfg)["step"], "approve")
        cfg["production"] = {}  # production not answered: its line alone, saved production-only hosts as the default
        s = self.st(cfg)
        self.assertEqual(s["ask"]["question"].splitlines()[1:], [
            "Production servers (what you type after ssh, comma separated), or none / later [deploy@192.0.2.20:2222]"])
        self.assertEqual(s["ask"]["example"], {"production": ["deploy@192.0.2.20:2222"]})

    def test_setup_default_for_production_elsewhere(self):
        # R1 + council 2 MF6: a helper that another approved project saves as production defaults to helper +
        # production, read-only commands only,
        # and its line names that project; this project's own root is never named
        other = (self.P.root.parent / "other").as_posix()
        prod = {("192.0.2.11", 22): [other, self.P.root.as_posix()]}
        s = self.st(self._setup_cfg(), {"h11": rec("192.0.2.11"), "h12": rec("192.0.2.12")}, master_prod=prod)
        lines = s["ask"]["question"].splitlines()
        self.assertEqual(lines[2], f"  h11 (deploy@192.0.2.11): 8 cpu, 15 GB, no other containers, production server in "
                                   f"{other} [helper + production; read-only commands only]")
        self.assertEqual(lines[3], "  h12 (app1): 8 cpu, 15 GB, no other containers [helper only; everything]")
        self.assertEqual(s["ask"]["example"]["hosts"], {"h11": {"roles": ["helper", "prod"], "jobs": "readonly"},
                                                        "h12": {"roles": ["helper"], "jobs": "all"}})
        own = {("192.0.2.11", 22): [self.P.root.as_posix()]}
        s = self.st(self._setup_cfg(), {"h11": rec("192.0.2.11"), "h12": rec("192.0.2.12")}, master_prod=own)
        self.assertIn("  h11 (deploy@192.0.2.11): 8 cpu, 15 GB, no other containers [helper only; everything]",
                      s["ask"]["question"])

    def _jobs_cfg(self):
        cfg = full_cfg()
        del cfg["jobs"]
        return cfg

    def test_jobs_step(self):
        det = {"jobs": {k: full_cfg()["jobs"][k] for k in ("api-test", "web-test")},
               "notes": ["web: the npm test script is the npm stub, so no test job"], "unknown": []}
        s = self.st(self._jobs_cfg(), detected=det)
        self.assertEqual((s["state"], s["step"], s["next"]), ("incomplete", "jobs", "ask"))
        self.assertEqual(s["ask"], {"kind": "choice", "header": "Jobs", "question": "\n".join([
            "Jobs found in this project:",
            "  api-test  unit  dotnet 8  api/  3 GB  dotnet test --no-restore",
            "  web-test  unit  node 22  web/  2 GB  npm test",
            "note: web: the npm test script is the npm stub, so no test job",
            "Use these jobs? (yes / change: tell me what to change)"]),
            "options": ["yes", "change"], "multi": False, "answer_key": "jobs", "example": {"jobs": "detected"}})
        s = self.st(self._jobs_cfg(), detected={"jobs": {}, "notes": [], "unknown": ["tools/Cargo.toml"]})
        self.assertEqual(s["ask"]["question"], "not recognised: tools/Cargo.toml\nNo known stack found (dotnet, node, "
                                               "python, Dockerfile). Which image and test command should orch run? "
                                               "e.g. python:3.12-slim and pytest -q, or none")
        self.assertEqual(s["ask"]["example"], {"jobs": {"shop-test": {"stage": "unit", "image": "python:3.12-slim",
                                                                      "cmd": "pytest -q"}}})
        s = self.st(self._jobs_cfg(), detected=None)
        self.assertEqual(s["ask"]["question"], "This project has not been scanned for jobs yet; run: orch init --detect")
        self.assertEqual(s["step"], "jobs")

    def test_approve_first_time(self):
        s = self.st(full_cfg(), trust=None)
        self.assertEqual((s["state"], s["step"], s["next"], s["problems"]), ("unapproved", "approve", "approve", []))
        self.assertEqual(s["summary"], [
            "helpers: h11 (deploy@192.0.2.11) everything incl. image builds; "
            "h12 (app1) lint + tests, no image builds (also production)",
            "production: p20 (deploy@192.0.2.20:2222), h12 (app1)",
            "deploy: ./scripts/deploy.sh | health https://shop.example/health | rollback -",
            "alerts: none",
            "watch: h12 (app1) https://127.0.0.1/app/",
            "jobs: api-test (unit), web-test (unit), web-lint (lint), app-image (image)"])
        # R33: the question does not repeat the summary lines; one self-contained line says what is approved
        self.assertEqual(s["ask"], {"kind": "choice", "header": "Approve", "question":
                                    "Setup: helpers h11 (deploy@192.0.2.11) all, h12 (app1) tests | prod p20 "
                                    "(deploy@192.0.2.20:2222), h12 | deploy ./scripts/deploy.sh | alerts none | 4 jobs\n"
                                    "Save and use this setup?",
                                    "options": ["yes", "no"], "multi": False, "answer_key": None, "example": None})
        self.assertFalse(set(s["summary"]) & set(s["ask"]["question"].splitlines()))
        cfg = full_cfg()
        cfg["deploy"].update(rollback="./scripts/rollback.sh")
        cfg["alerts"]["command"] = "./scripts/notify.sh"
        self.assertEqual(self.st(cfg)["ask"]["question"].splitlines()[0],
                         "Setup: helpers h11 (deploy@192.0.2.11) all, h12 (app1) tests | prod p20 (deploy@192.0.2.20:2222), "
                         "h12 | deploy ./scripts/deploy.sh | rollback ./scripts/rollback.sh | alerts ./scripts/notify.sh | "
                         "4 jobs")

    def test_unapproved_lists_loosening_and_changes(self):
        old = full_cfg()
        new = full_cfg()
        new["hosts"]["h12"]["roles"] = ["helper"]
        sh = state()
        sh["h11"] = rec("192.0.2.11", cot=("app2-web",))
        sh["p20"] = {"ok": False, "problem": {"what": "ssh: connect to host 192.0.2.20 port 2222: Connection timed out",
                                              "tried": "ssh -p 2222 deploy@192.0.2.20 true",
                                              "fix": "check that 192.0.2.20 is up and port 2222 is open"}}
        s = self.st(new, sh, trust=trust_of(old))
        self.assertEqual((s["state"], s["step"]), ("unapproved", "approve"))
        q = s["ask"]["question"].splitlines()
        i = q.index("Changes that loosen protection since the last approval:")
        self.assertEqual(q[i + 1], "  h12 (app1) is no longer a production server")
        self.assertIn("Changed on the servers since the last approval:", q)
        self.assertEqual(q[q.index("Problems:") + 1], "  p20 (deploy@192.0.2.20:2222): ssh: connect to host 192.0.2.20 "
                                                      "port 2222: Connection timed out -> check that 192.0.2.20 is up "
                                                      "and port 2222 is open")
        self.assertEqual(q[-1], "Save and use this setup?")

    def test_unapproved_with_a_helper_down_asks_the_fix(self):
        # R12: --approve refuses while a helper is not probed ok, so the fix step comes before the approve step
        down = {**state(), "h11": {"ok": False, "problem": {"what": "ssh: connect to host 192.0.2.11 port 22: "
                                                            "Connection timed out", "tried": "t",
                                                            "fix": "check that 192.0.2.11 is up"}}}
        s = self.st(full_cfg(), down, trust=None)
        self.assertEqual((s["state"], s["step"], s["next"]), ("unapproved", "fix", "fix"))
        self.assertEqual((s["ask"]["options"], s["ask"]["answer_key"]), (["retry", "remove h11"], "remove_hosts"))
        new = full_cfg()
        new["deploy"]["command"] = "./scripts/deploy2.sh"  # a loosening edit while the helper is down
        s = self.st(new, down, trust=trust_of(full_cfg()))
        self.assertEqual((s["state"], s["step"], s["next"]), ("unapproved", "fix", "fix"))
        self.assertEqual(self.st(new, trust=trust_of(full_cfg()))["step"], "approve")  # back up: approve

    def test_ready(self):
        cfg = full_cfg()
        s = self.st(cfg, trust=trust_of(cfg))
        self.assertEqual((s["state"], s["step"], s["next"], s["ask"], s["problems"]),
                         ("ready", "none", "ready", None, []))
        self.assertEqual(s["summary"], ["setup ok: helpers h11 (deploy@192.0.2.11) all, h12 (app1) tests | "
                                        "prod p20 (deploy@192.0.2.20:2222), h12 | 4 jobs | approved 2026-10-30"])
        cfg["pending"] = {"note": "pending answers never break approval"}
        self.assertEqual(self.st(cfg, trust=trust_of(full_cfg()))["state"], "ready")

    def test_ready_production_later_and_prod_problem(self):
        cfg = {"schema": 1, "hosts": {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"], "jobs": "all"}},
               "production": {"answer": "later"}, "deploy": {"answer": "later"}, "alerts": {"command": None},
               "jobs": {"web-test": full_cfg()["jobs"]["web-test"]}}
        s = self.st(cfg, trust=trust_of(cfg))
        self.assertEqual(s["state"], "ready")
        self.assertEqual(s["summary"], ["setup ok: helpers h11 (deploy@192.0.2.11) all | prod later | 1 job | "
                                        "approved 2026-10-30"])
        cfg["production"] = {"answer": "set"}
        self.assertIn(" | prod none | ", self.st(cfg, trust=trust_of(cfg))["summary"][0])
        self.assertIn("production: none listed", oc.summary_lines(cfg, {}))
        cfg = full_cfg()
        sh = {**state(), "p20": {"ok": False, "problem": {"what": "ssh: Connection timed out", "tried": "t", "fix": "f"}}}
        s = self.st(cfg, sh, trust=trust_of(cfg))
        self.assertEqual((s["state"], s["next"]), ("ready", "ready"))  # production problems never block tests
        self.assertEqual(s["problems"], [{"host": "p20 (deploy@192.0.2.20:2222)", "what": "ssh: Connection timed out",
                                          "tried": "t", "fix": "f"}])
        self.assertTrue(s["summary"][0].endswith(" | problems: p20 (deploy@192.0.2.20:2222)"))

    def test_unpinned_job_asks_to_pin(self):
        # R14: ci and run leave an unpinned job out, so an approved setup with one asks (not ready) before --approve
        cfg = full_cfg()
        cfg["jobs"]["web-test"]["image"] = "node:22-bookworm-slim"
        s = self.st(cfg, trust=trust_of(cfg))
        self.assertEqual((s["state"], s["step"], s["next"]), ("drift", "approve", "approve"))
        self.assertEqual(s["problems"][0]["host"], "job web-test")
        self.assertIn("not pinned to a digest", s["problems"][0]["what"])
        self.assertEqual(s["ask"], {"kind": "choice", "header": "Pin images", "question":
                                    "Not pinned to a digest, so this job cannot run: web-test (node:22-bookworm-slim). "
                                    "Pin it now? orch init --approve looks up each digest (it needs registry access "
                                    "from this machine or a helper).",
                                    "options": ["yes", "no"], "multi": False, "answer_key": None, "example": None})
        cfg["policy"] = {"require_digest": False}  # unpinned jobs run: nothing to ask, nothing wrong
        s = self.st(cfg, trust=trust_of(cfg))
        self.assertEqual((s["state"], s["problems"]), ("ready", []))

    def test_drift_host_problem(self):
        cfg = full_cfg()
        sh = {**state(), "h11": {"ok": False, "problem": {"what": "docker: permission denied", "tried": "x",
                                                          "fix": "sudo usermod -aG docker deploy"}}}
        s = self.st(cfg, sh, trust=trust_of(cfg))
        self.assertEqual((s["state"], s["step"], s["next"]), ("drift", "fix", "fix"))
        self.assertEqual(s["ask"]["example"], {"remove_hosts": ["h11"]})

    def test_drift_cotenants(self):
        cfg = full_cfg()
        sh = {**state(), "h11": rec("192.0.2.11", cot=("app2-web", "app2-db"))}
        s = self.st(cfg, sh, trust=trust_of(cfg))
        self.assertEqual((s["state"], s["step"], s["next"]), ("drift", "approve", "approve"))
        self.assertEqual(s["ask"], {"kind": "choice", "header": "Changed", "question": "\n".join([
            "Changed since approval:",
            "  h11 (deploy@192.0.2.11): now runs 2 other containers (app2-db, app2-web); it had none when approved "
            "[keep: helper only; everything]",
            "Keep this setup? (yes / change: tell me what to change)"]),
            "options": ["yes", "change"], "multi": False, "answer_key": None, "example": None})
        s = self.st(cfg, sh, trust=trust_of(cfg, cot={"h11": ["app2-db", "app2-web"]}))  # shared at approval
        self.assertEqual(s["state"], "ready")

    def test_drift_manifest(self):
        cfg = full_cfg()
        cfg["jobs"]["web-test"].update(manifest=M1, files=["web/package.json"])
        det_api = {**cfg["jobs"]["api-test"], "manifest": M2, "detected": "dotnet 9 (api/Api.sln)"}
        det = {"jobs": {"api-test": det_api}, "notes": [], "unknown": []}
        s = self.st(cfg, trust=trust_of(cfg), detected=det)
        self.assertEqual((s["state"], s["step"], s["next"]), ("drift", "jobs", "ask"))
        self.assertEqual(s["ask"]["question"], "\n".join([
            "Changed since approval:",
            "  api-test: api/Api.sln changed since approval; detected dotnet 9 (api/Api.sln), saved dotnet 8 (api/Api.sln)",
            "  web-test: no longer detected (the files it was made from moved or were removed)",
            "Take the new detection for these jobs? (yes / keep: keep the saved jobs and stop asking)"]))
        self.assertEqual(s["ask"]["options"], ["yes", "keep"])
        self.assertEqual(s["ask"]["example"], {"jobs": {"api-test": det_api}, "jobs_remove": ["web-test"]})
        self.assertEqual(s["ask"]["examples"]["keep"], {"jobs": {"api-test": {"manifest": M2},
                                                                 "web-test": {"manifest": None}}})
        same = {"jobs": {"api-test": cfg["jobs"]["api-test"], "web-test": cfg["jobs"]["web-test"]}}
        self.assertEqual(self.st(cfg, trust=trust_of(cfg), detected=same)["state"], "ready")
        s = self.st(cfg, trust=None, detected=det)  # update jobs first, approve once after
        self.assertEqual((s["state"], s["step"]), ("unapproved", "jobs"))

    def test_drift_yes_keeps_the_digest_of_an_unchanged_tag(self):
        # R14: the detector proposes bare tags; "yes" must not unpin a job whose tag did not change
        cfg = full_cfg()
        saved = cfg["jobs"]["api-test"]["image"]
        bare = {**cfg["jobs"]["api-test"], "manifest": M2, "image": saved.split("@")[0]}
        s = self.st(cfg, trust=trust_of(cfg), detected={"jobs": {"api-test": bare}})
        self.assertEqual(s["ask"]["examples"]["yes"]["jobs"]["api-test"]["image"], saved)
        self.assertEqual(s["ask"]["example"], s["ask"]["examples"]["yes"])
        newer = {**bare, "image": "mcr.microsoft.com/dotnet/sdk:9.0"}  # a new tag needs a new pin
        s = self.st(cfg, trust=trust_of(cfg), detected={"jobs": {"api-test": newer}})
        self.assertEqual(s["ask"]["examples"]["yes"]["jobs"]["api-test"]["image"], "mcr.microsoft.com/dotnet/sdk:9.0")

    def test_summary_full(self):
        lines = oc.summary_lines(full_cfg(), state(), full=True)
        self.assertEqual(lines[0], "host h11 (deploy@192.0.2.11): helper only; jobs all (everything incl. image "
                                   "builds); 8 cpu, 15 GB, no other containers; probed 2026-10-30T09:00:00 ok")
        self.assertIn("job api-test  unit  dotnet 8  api/  3 GB  dotnet test --no-restore  image "
                      "mcr.microsoft.com/dotnet/sdk:8.0" + PIN, lines)
        self.assertIn("job app-image  image  -  web/  4 GB  docker build -f Dockerfile  image -", lines)
        self.assertEqual(lines[-1], "policy overrides: none")
        self.assertLessEqual(len(oc.summary_lines(full_cfg(), state())), 8)
        self.assertEqual(oc.summary_lines({}, {}), ["helpers: none yet", "production: not answered yet",
                                                    "deploy: not answered yet", "alerts: not answered yet",
                                                    "jobs: not chosen yet"])


# ----------------------------------------------------------------------------- compile_engine
class TestCompile(unittest.TestCase):
    def setUp(self):
        d = tmpdir(self)
        self.P = oc.Project(d / "shop", d / "shop" / ".git" / "orch", True)

    def c(self, cfg=None, sh=None, policy=None):
        return oc.compile_engine(self.P, full_cfg() if cfg is None else cfg, state() if sh is None else sh, policy)

    def test_nodes(self):
        out = self.c()
        self.assertEqual(set(out), {"nodes", "jobs", "protected"})
        n = out["nodes"]
        self.assertEqual(set(n), {"_doc", "admission", "score_weights", "watchdog", "lease", "subnet_pool",
                                  "subnet_prefix", "builder", "limits", "require_digest", "nodes"})
        self.assertEqual(n["admission"], oc.DEFAULT_POLICY["admission"])
        self.assertEqual((n["limits"], n["require_digest"]), ({"memory_max": "6g", "timeout_max": 3600}, True))
        self.assertEqual(list(n["nodes"]), ["h11", "h12"])  # production-only p20 never becomes a node
        t = ["t-ok", "t-fail", "t-sleep", "t-oom", "t-hold"]
        self.assertEqual(n["nodes"]["h11"], {
            "enabled": True, "os": "linux", "host": "192.0.2.11", "ssh": "deploy@192.0.2.11",
            "ssh_args": ["deploy@192.0.2.11"], "context": "orch-h11", "transport": "context", "docker_host": None,
            "role": "helper", "tags": ["docker", "buildx", "linux-host"],
            "opt_in": ["api-test", "web-test", "web-lint", "app-image", "shell-investigate"] + t, "io": None,
            "note": "roles helper; jobs all (everything incl. image builds)"})
        h12 = n["nodes"]["h12"]
        self.assertEqual((h12["enabled"], h12["ssh_args"], h12["host"]), (True, ["app1"], "192.0.2.12"))
        self.assertEqual(h12["opt_in"], ["api-test", "web-test", "web-lint", "shell-investigate"] + t)
        self.assertEqual(h12["note"], "roles helper + prod; jobs tests (lint + tests, no image builds)")  # chosen
        self.assertEqual(h12["role"], "helper+prod")

    def test_disabled_and_transport_cases(self):
        cfg = full_cfg()
        cfg["hosts"]["h12"]["jobs"] = "none"
        cfg["hosts"]["h13"] = {"ssh": "deploy@192.0.2.13:2200", "roles": ["helper"], "jobs": "readonly"}
        cfg["hosts"]["h14"] = {"ssh": "deploy@192.0.2.14", "roles": ["helper"], "jobs": "all"}
        cfg["hosts"]["h15"] = {"ssh": "deploy@192.0.2.15", "roles": ["helper"], "jobs": "all"}
        sh = {**state(), "h11": {**rec("192.0.2.11", ctx="orch-h11"), "ok": False, "problem": {"what": "ssh: timeout"}},
              "h13": rec("192.0.2.13", port=2200, transport="ssh-docker"),
              "h14": rec("192.0.2.14", os_="windows", ctx="orch-h14")}
        n = self.c(cfg, sh)["nodes"]
        self.assertEqual((n["nodes"]["h11"]["enabled"], n["nodes"]["h11"]["note"]),
                         (False, "roles helper; jobs all (everything incl. image builds); disabled: probe failed: "
                                 "ssh: timeout"))
        self.assertEqual((n["nodes"]["h12"]["enabled"], n["nodes"]["h12"]["opt_in"]),
                         (False, ["t-ok", "t-fail", "t-sleep", "t-oom", "t-hold"]))
        h13 = n["nodes"]["h13"]
        self.assertEqual((h13["enabled"], h13["transport"], h13["docker_host"], h13["tags"], h13["ssh_args"]),
                         (True, "ssh-docker", "unix:///var/run/docker.sock", ["docker", "linux-host"],
                          ["-p", "2200", "deploy@192.0.2.13"]))
        self.assertEqual(h13["opt_in"][0], "shell-investigate")  # readonly: no project jobs
        self.assertEqual(n["nodes"]["h14"]["tags"], ["docker", "buildx", "windows-host"])
        self.assertNotIn("h15", n["nodes"])  # not probed yet
        self.assertIn("h15 left out: not probed yet (run orch init)", n["_doc"])
        sh["h11"] = rec("192.0.2.11", ctx=None)
        self.assertIn("disabled: no docker transport", self.c(cfg, sh)["nodes"]["nodes"]["h11"]["note"])

    def test_another_namespace_disables_the_node_with_the_fix(self):
        # council 2 MF5: runtime verbs refuse that node with the probe's message; its transport stays, so gc and
        # stop-all still reach this master's own objects there
        import orch_discover as od  # noqa: PLC0415
        pr = od.foreign_problem([{"kind": "volume", "name": "agentx-cache-npm", "family": None}], "agentx-")
        sh = {**state(), "h11": {**rec("192.0.2.11", ctx="orch-h11"), "ok": False, "problem": pr}}
        h11 = self.c(full_cfg(), sh)["nodes"]["nodes"]["h11"]
        self.assertEqual((h11["enabled"], h11["transport"], h11["context"]), (False, "context", "orch-h11"))
        self.assertEqual(h11["note"], "roles helper; jobs all (everything incl. image builds); disabled: 1 object "
                                      "named agentx-* belongs to another orch namespace (label none): an older orch "
                                      "or another master. Fix: run that orch's gc --session-end on this host (removing "
                                      "volumes needs the user's OK); or, if no other master uses this host, remove "
                                      "them by hand: docker volume rm agentx-cache-npm")

    def test_failed_probe_without_a_transport_is_still_a_valid_node(self):
        # a helper whose Docker is down after approval: a disabled node with transport "context" (the engine's
        # validator refuses None), so every other helper keeps working
        sh = {**state(), "h11": {**rec("192.0.2.11"), "ok": False, "transport": None,
                                 "problem": {"class": "docker-down", "what": "Docker is not running on 192.0.2.11"}}}
        h11 = self.c(full_cfg(), sh)["nodes"]["nodes"]["h11"]
        self.assertEqual((h11["enabled"], h11["transport"], h11["context"]), (False, "context", None))
        self.assertTrue(h11["note"].endswith("disabled: probe failed: Docker is not running on 192.0.2.11"))

    def test_prod_host_never_gets_all(self):
        # council 2 MF6: no heavy work on production by default: all falls back to production's default, readonly
        cfg = full_cfg()
        cfg["hosts"]["h12"]["jobs"] = "all"  # hand edit (load_config would refuse it)
        h12 = self.c(cfg)["nodes"]["nodes"]["h12"]
        self.assertEqual(h12["opt_in"], ["shell-investigate", "t-ok", "t-fail", "t-sleep", "t-oom", "t-hold"])
        self.assertIn("jobs readonly (read-only commands only)", h12["note"])
        del cfg["hosts"]["h12"]["jobs"]  # no scope saved: the same default
        self.assertIn("jobs readonly", self.c(cfg)["nodes"]["nodes"]["h12"]["note"])
        self.assertEqual(oc._scope({"roles": ["helper", "prod"]}), oc.PROD_SCOPE)
        self.assertEqual(oc._scope({"roles": ["helper", "prod"], "jobs": "tests"}), "tests")  # chosen: it stays
        self.assertEqual(oc._scope({"roles": ["helper"]}), "all")

    def test_readonly_runs_no_project_job(self):
        # R4/R10: readonly = read-only commands only; an investigate-stage project job (writable, networked prep,
        # up to the memory and time caps) never lands on a readonly host, helper-only or helper + production
        cfg = full_cfg()
        cfg["jobs"]["db-poke"] = {"stage": "investigate", "image": "python:3.12-slim" + PIN, "prep": "pip install x",
                                  "cmd": "python poke.py", "memory": "6g", "timeout": 3600}
        cfg["hosts"]["h11"]["jobs"] = cfg["hosts"]["h12"]["jobs"] = "readonly"
        n = self.c(cfg)["nodes"]["nodes"]
        t = ["t-ok", "t-fail", "t-sleep", "t-oom", "t-hold"]
        for name in ("h11", "h12"):
            self.assertEqual(n[name]["opt_in"], ["shell-investigate"] + t, name)
            self.assertIn("jobs readonly (read-only commands only)", n[name]["note"])
        cfg["hosts"]["h11"]["jobs"] = "tests"  # tests (and all) still run project investigate jobs
        self.assertIn("db-poke", self.c(cfg)["nodes"]["nodes"]["h11"]["opt_in"])

    def test_production_in_another_project_clamps_the_helper(self):
        # R1: the master-wide production set (master_prod) wins over this project's helper-only answer
        cfg = full_cfg()
        other = (self.P.root.parent / "other").as_posix()
        prod = {("192.0.2.11", 22): [other], ("192.0.2.12", 22): [other]}
        n = oc.compile_engine(self.P, cfg, state(), master_prod=prod)["nodes"]["nodes"]
        h11 = n["h11"]  # council 2 MF6: its everything answer falls back to production's default, readonly
        self.assertEqual(h11["opt_in"], ["shell-investigate", "t-ok", "t-fail", "t-sleep", "t-oom", "t-hold"])
        self.assertEqual(h11["note"], f"roles helper; jobs readonly (read-only commands only); production server in "
                                      f"{other}")
        self.assertEqual((h11["role"], n["h12"]["role"]), ("helper+prod", "helper+prod"))
        self.assertEqual(n["h12"]["note"], "roles helper + prod; jobs tests (lint + tests, no image builds)")
        cfg["hosts"]["h11"]["jobs"] = "tests"  # an explicit tests answer stays: lint + tests, still no image build
        h11 = oc.compile_engine(self.P, cfg, state(), master_prod=prod)["nodes"]["nodes"]["h11"]
        self.assertEqual(h11["opt_in"], ["api-test", "web-test", "web-lint", "shell-investigate",
                                         "t-ok", "t-fail", "t-sleep", "t-oom", "t-hold"])
        cfg["hosts"]["h11"]["jobs"] = "readonly"  # narrower answers stay
        self.assertIn("jobs readonly", oc.compile_engine(self.P, cfg, state(), master_prod=prod)["nodes"]["nodes"]
                      ["h11"]["note"])
        own = {("192.0.2.11", 22): [self.P.root.as_posix()]}
        self.assertIn("app-image", self.c()["nodes"]["nodes"]["h11"]["opt_in"])  # no master_prod: unchanged
        self.assertIn("app-image", oc.compile_engine(self.P, full_cfg(), state(), master_prod=own)["nodes"]["nodes"]
                      ["h11"]["opt_in"])

    def test_io_caps(self):
        # R6: policy io caps job and builder containers on Linux helpers (the engine's io_args reads node io)
        cfg = full_cfg()
        cfg["hosts"]["h14"] = {"ssh": "deploy@192.0.2.14", "roles": ["helper"], "jobs": "all"}
        sh = {**state(), "h14": rec("192.0.2.14", os_="windows", ctx="orch-h14")}
        n = self.c(cfg, sh)["nodes"]["nodes"]
        self.assertEqual({k: v["io"] for k, v in n.items()}, {"h11": None, "h12": None, "h14": None})
        io_ = {"read_bps": "150mb", "write_bps": "80mb", "blkio_weight": 100}
        cfg["policy"] = {"io": io_}
        n = self.c(cfg, sh)["nodes"]["nodes"]
        self.assertEqual({k: v["io"] for k, v in n.items()}, {"h11": io_, "h12": io_, "h14": None})
        self.assertIsNot(n["h11"]["io"], n["h12"]["io"])
        for bad, needle in (({"read_bps": "fast", "write_bps": "80mb"}, "policy io.read_bps must be a rate"),
                            ({"read_bps": "150mb"}, "policy io.write_bps must be a rate"),
                            ({"read_bps": "0mb", "write_bps": "80mb"}, "io.read_bps must be a rate above 0"),
                            ({**io_, "blkio_weight": 5}, "io.blkio_weight must be a whole number from 10 to 1000"),
                            ({**io_, "dev": "/dev/sda"}, "policy io: unknown key 'dev'"),
                            ("150mb", "policy io must be an object")):
            cfg["policy"] = {"io": bad}
            with self.assertRaises(oc.ConfigError, msg=needle) as e:
                self.c(cfg, sh)
            self.assertIn(needle, str(e.exception))

    def test_jobs(self):
        j = self.c()["jobs"]
        self.assertEqual(set(j), {"_doc", "images", "repos", "source_exclude", "jobs"})
        sdk, node = "mcr.microsoft.com/dotnet/sdk:8.0" + PIN, "node:22-bookworm-slim" + PIN
        self.assertEqual(j["images"], {sdk: sdk, node: node, **oc.DEFAULT_POLICY["images"]})
        self.assertEqual(j["repos"], {".": self.P.root.as_posix()})
        self.assertEqual(j["source_exclude"], oc.DEFAULT_POLICY["source_exclude"])
        self.assertEqual(list(j["jobs"]), list(oc.BUILTIN_JOBS) + ["api-test", "web-test", "web-lint", "app-image"])
        self.assertEqual(j["jobs"]["api-test"], {
            "kind": "container", "heavy": True, "repo": ".", "source": "git-archive", "timeout": 1200, "memory": "3g",
            "fail_patterns": [], "requires": ["docker"], "stage": "unit", "image": sdk, "cpus": 2, "pids": 2048,
            "prep_timeout": 900, "network": "none", "env": {}, "cmd": "mkdir -p /out && cd /src/api && dotnet test --no-restore",
            "shell": "sh", "result_paths": ["/out"], "prep": "mkdir -p /out && cd /src/api && dotnet restore",
            "prep_network": "egress-registries", "cache": {"name": "nuget", "path": "/root/.nuget/packages"},
            "note": "dotnet 8 (api/Api.sln)"})
        self.assertEqual(j["jobs"]["app-image"], {
            "kind": "build", "heavy": True, "repo": ".", "source": "git-archive", "timeout": 2400, "memory": "4g",
            "fail_patterns": [], "requires": ["docker", "buildx"], "stage": "image", "dockerfile": "Dockerfile",
            "context": "web", "targets": [""], "result_paths": []})

    def test_job_translation_edges(self):
        cfg = full_cfg()
        cfg["jobs"] = {"svc-test": {"stage": "unit", "image": "python:3.12-slim" + PIN, "dir": "my app/",
                                    "repo": "services/svc", "cmd": "pytest -q"},
                       "root-test": {"image": "python:3.12-slim" + PIN, "dir": ".", "cmd": "pytest", "memory": None,
                                     "timeout": None, "cpus": None}}
        j = self.c(cfg)["jobs"]
        self.assertEqual(j["jobs"]["svc-test"]["cmd"], "mkdir -p /out && cd '/src/my app' && pytest -q")
        self.assertNotIn("prep", j["jobs"]["svc-test"])
        self.assertNotIn("prep_network", j["jobs"]["svc-test"])
        self.assertEqual(j["jobs"]["svc-test"]["repo"], "services/svc")
        self.assertEqual(j["repos"]["services/svc"], (self.P.root / "services" / "svc").resolve().as_posix())
        rt = j["jobs"]["root-test"]
        # root jobs start in /src too (a runtime run showed commands landing in / without it)
        self.assertEqual((rt["cmd"], rt["stage"], rt["memory"], rt["timeout"], rt["cpus"]),
                         ("mkdir -p /out && cd /src && pytest", "unit", "2g", 1200, 2))
        for bad, needle in (({"repo": "../other"}, "must be a path inside the project"),
                            ({"dir": "/etc"}, "must be a path inside the project"),
                            ({"dir": "C:/work"}, "must be a path inside the project"),
                            ({"cmd": ""}, "cmd is missing"), ({"image": None}, "image is missing")):
            cfg["jobs"] = {"x": {"image": "python:3.12-slim" + PIN, "cmd": "pytest", **bad}}
            with self.assertRaises(oc.ConfigError, msg=needle) as e:
                self.c(cfg)
            self.assertIn(needle, str(e.exception))

    def test_unpinned_left_out(self):
        cfg = full_cfg()
        cfg["jobs"]["web-test"]["image"] = "node:22-bookworm-slim"
        out = self.c(cfg)
        self.assertNotIn("web-test", out["jobs"]["jobs"])
        self.assertNotIn("node:22-bookworm-slim", out["jobs"]["images"])
        self.assertNotIn("web-test", out["nodes"]["nodes"]["h11"]["opt_in"])
        self.assertIn("left out until pinned (run orch init --approve): web-test", out["jobs"]["_doc"])
        out = self.c(cfg, policy={"require_digest": False})
        self.assertIn("web-test", out["jobs"]["jobs"])
        self.assertFalse(out["nodes"]["require_digest"])

    def test_policy_layers(self):
        cfg = full_cfg()
        cfg["policy"] = {"admission": {"max_heavy": 2}, "never_touch": ["app1-"]}
        out = self.c(cfg, policy={"images": {"alpine": "alpine:3.21" + PIN}})
        self.assertEqual(out["nodes"]["admission"]["max_heavy"], 2)
        self.assertEqual(out["nodes"]["admission"]["reserve_frac"], 0.25)
        self.assertEqual(out["jobs"]["images"]["alpine"], "alpine:3.21" + PIN)
        self.assertEqual(out["protected"]["never_touch_prefixes"], ["app1-"])
        self.assertEqual(oc.DEFAULT_POLICY["admission"]["max_heavy"], 1)  # defaults never mutated

    def test_protected(self):
        cfg = full_cfg()
        cfg["alerts"] = {"command": "notify-admin"}
        cfg["hosts"]["h14"] = {"ssh": "deploy@192.0.2.14", "roles": ["helper"], "jobs": "all",
                               "watch": ["http://127.0.0.1:8080/"]}
        sh = {**state(), "h14": rec("192.0.2.14", os_="windows", ctx="orch-h14", eps=[EP12])}
        p = self.c(cfg, sh)["protected"]
        self.assertEqual(set(p), {"never_touch_prefixes", "nodes", "alert"})
        self.assertEqual(p["never_touch_prefixes"], [])
        self.assertEqual(p["alert"], {"command": "notify-admin", "window_start_h": 0, "window_end_h": 24})
        self.assertEqual(p["nodes"]["h12"], {"endpoints": [EP12, {"name": "https://127.0.0.1/app/",
                                                                  "url": "https://127.0.0.1/app/", "insecure": True}],
                                             "host_services": []})
        self.assertEqual(p["nodes"]["h11"], {"endpoints": [], "host_services": []})
        self.assertEqual(p["nodes"]["h14"], {"endpoints": [EP12], "host_services": oc.DEFAULT_POLICY["windows_host_services"]})
        self.assertNotIn("p20", p["nodes"])
        self.assertIsNone(self.c()["protected"]["alert"]["command"])


class TestDefaults(unittest.TestCase):
    """DEFAULT_POLICY / BUILTIN_JOBS are exact copies of the v1 values (skipped once the v1 files move away)."""

    @staticmethod
    def v1(name):
        for d in (ROOT / "config", ROOT / "dev" / "v1-config"):
            if (d / name).is_file():
                return json.loads((d / name).read_text(encoding="utf-8"))
        raise unittest.SkipTest(f"v1 {name} not found")

    def test_policy_matches_v1(self):
        n = self.v1("nodes.json")
        for k in ("admission", "score_weights", "watchdog", "lease", "subnet_pool", "subnet_prefix", "builder"):
            self.assertEqual(oc.DEFAULT_POLICY[k], n[k], k)
        im = self.v1("jobs.json")
        self.assertEqual(oc.DEFAULT_POLICY["images"], {k: im["images"][k] for k in ("alpine", "buildkit")})
        self.assertEqual(oc.DEFAULT_POLICY["source_exclude"],
                         [x for x in im["source_exclude"] if x != ":(exclude)ops" and "appsettings" not in x])

    def test_builtins_match_v1(self):
        v1 = self.v1("jobs.json")["jobs"]
        for k, j in oc.BUILTIN_JOBS.items():
            self.assertEqual({a: b for a, b in j.items() if a != "stage"}, v1[k], k)
        self.assertEqual(oc.BUILTIN_JOBS["shell-investigate"]["stage"], "investigate")
        self.assertIsNot(oc.BUILTIN_JOBS["t-ok"]["requires"], oc.BUILTIN_JOBS["t-fail"]["requires"])


class TestV1Compat(unittest.TestCase):
    """compile_engine output passes the v1 validators (scripts/orch.py of this tree) apart from the new v2 keys."""
    NEW = {"nodes": (r"nodes\.json: unknown top-level key '(limits|require_digest)'", r"node \w+: unknown key 'ssh_args'"),
           "jobs": (r"job [\w.-]+: unknown key '(stage|context)'",)}

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="orchcfg-v1-")
        cls.addClassCleanup(shutil.rmtree, tmp, True)
        env = {"ORCH_HOME": tmp, "ORCH_PREFIX": "agentt-", "ORCH_LABEL": "io.orchtest.agent"}
        spec = importlib.util.spec_from_file_location("orch_v1_compat", ROOT / "scripts" / "orch.py")
        cls.v1 = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, env):
            spec.loader.exec_module(cls.v1)

    def errors(self, fn, data):
        try:
            fn(data)
            return []
        except self.v1.ConfigError as e:
            return str(e).split("; ")

    def test_v1_validators(self):
        d = tmpdir(self)
        p = oc.Project(d, d / ".orch", False)
        cfg = full_cfg()
        sh = {**state(), "h13": rec("192.0.2.13", transport="ssh-docker", docker_host="unix:///run/docker.sock")}
        cfg["hosts"]["h13"] = {"ssh": "deploy@192.0.2.13:2222", "roles": ["helper"], "jobs": "tests"}
        cfg["policy"] = {"io": {"read_bps": "150mb", "write_bps": "80mb", "blkio_weight": 100}}
        out = oc.compile_engine(p, cfg, sh)
        node = self.v1.Node("h11", out["nodes"]["nodes"]["h11"])  # the engine turns node io into docker flags (R6)
        self.assertEqual(self.v1.io_args(node, {"dev": "/dev/sda"}), ["--device-read-bps", "/dev/sda:150mb",
                                                                      "--device-write-bps", "/dev/sda:80mb",
                                                                      "--blkio-weight", "100"])
        for part, fn in (("nodes", self.v1.validate_nodes), ("jobs", self.v1.validate_jobs)):
            errs = self.errors(fn, out[part])
            other = [e for e in errs if not any(re.fullmatch(rx, e) for rx in self.NEW[part])]
            self.assertEqual(other, [], f"v1 {part} validator rejects more than the new v2 keys")
        stripped = json.loads(json.dumps(out))
        for k in ("limits", "require_digest"):
            stripped["nodes"].pop(k)
        for n in stripped["nodes"]["nodes"].values():
            n.pop("ssh_args")
        for j in stripped["jobs"]["jobs"].values():
            j.pop("stage", None)
            j.pop("context", None)
        self.assertEqual(self.errors(self.v1.validate_nodes, stripped["nodes"]), [])
        self.assertEqual(self.errors(self.v1.validate_jobs, stripped["jobs"]), [])


class TestMasterProd(unittest.TestCase):
    """master_prod: every host an approved project on this master saves as production (R1), keyed by identity."""

    def setUp(self):
        d = tmpdir(self)
        self.home = d / "home"
        self.a = oc.Project(d / "shop", d / "shop" / ".git" / "orch", True)
        self.b = oc.Project(d / "web", d / "web" / ".orch", False)
        self.c = oc.Project(d / "draft", d / "draft" / ".orch", False)
        for p in (self.a, self.b, self.c):
            (p.store / "state").mkdir(parents=True)

    def approve(self, p, cfg, sh=None):
        oc.save_config(p, cfg)
        if sh is not None:
            oc.save_state(p, "hosts", sh)
        oc.trust_set(self.home, p, cfg, {})

    def test_union_of_approved_projects(self):
        self.assertEqual(oc.master_prod(self.home, run=FakeRun()), {})  # nothing approved: no ORCH_HOME files yet
        self.approve(self.a, full_cfg(), state())  # h12 helper + prod (alias app1, probed), p20 production only
        cfg_b = {"schema": 1, "hosts": {"p30": {"ssh": "deploy@db.example", "roles": ["prod"]},
                                        "app3": {"ssh": "app3", "roles": ["prod"]},
                                        "h12b": {"ssh": "deploy@192.0.2.12", "roles": ["prod"]},
                                        "h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"], "jobs": "all"}},
                 "production": {"answer": "set"}, "jobs": {}}
        self.approve(self.b, cfg_b)  # never probed: registry identity, ssh -G for an alias, else the spec
        reg = oc.registry_load(self.home)
        reg["nodes"]["p30"] = {"hostname": "198.51.100.30", "port": 22, "docker_id": None}
        reg["projects"].append(oc._key(self.c.store))
        oc.registry_save(self.home, reg)
        oc.save_config(self.c, {"schema": 1, "hosts": {"p40": {"ssh": "deploy@192.0.2.40", "roles": ["prod"]}}})
        run = FakeRun(ssh={"app3": ("192.0.2.33", 2200)})
        a, b = self.a.root.as_posix(), self.b.root.as_posix()
        want = {("192.0.2.12", 22): [a, b], ("192.0.2.20", 2222): [a], ("198.51.100.30", 22): [b],
                ("192.0.2.33", 2200): [b]}  # p40: its project was never approved
        self.assertEqual(oc.master_prod(self.home, run=run), want)
        cfg = oc.load_config(self.a)
        cfg["hosts"]["h12"]["roles"] = ["helper"]
        oc.save_config(self.a, cfg)  # waiting for approval: the approved snapshot still counts
        self.assertEqual(oc.master_prod(self.home, run=run), want)
        (self.a.store / "state" / "hosts.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(oc.master_prod(self.home, run=run)[("192.0.2.20", 2222)], [a])  # spec deploy@192.0.2.20:2222
        self.assertEqual(oc.master_prod(self.home, run=FakeRun(missing={"ssh"}))[("app3", 22)], [b])

    def test_exclude_leaves_one_store_out_whatever_its_root(self):
        # Q5: orch init leaves its own project out by store, so a linked worktree (another root, the same store) is
        # never "another project", whichever worktree approved last
        self.approve(self.a, full_cfg(), state())
        self.approve(self.b, {"schema": 1, "hosts": {"p30": {"ssh": "deploy@192.0.2.30", "roles": ["prod"]}}})
        run = FakeRun()
        every = oc.master_prod(self.home, run=run)
        b_only = {("192.0.2.30", 22): [self.b.root.as_posix()]}
        self.assertEqual({k: v for k, v in every.items() if k != ("192.0.2.30", 22)},
                         {("192.0.2.12", 22): [self.a.root.as_posix()], ("192.0.2.20", 2222): [self.a.root.as_posix()]})
        wt = oc.Project(self.a.root.parent / "wt1", self.a.store, True)  # a linked worktree shares the store
        self.assertEqual(oc.master_prod(self.home, run=run, exclude=wt.store), b_only)
        self.assertEqual(oc.master_prod(self.home, run=run, exclude=str(self.a.store)), b_only)
        self.assertEqual(oc.master_prod(self.home, run=run, exclude=self.b.store),
                         {k: v for k, v in every.items() if k != ("192.0.2.30", 22)})
        self.assertEqual(oc.master_prod(self.home, run=run, exclude=self.c.store), every)  # never approved: no change

    def test_never_raises(self):
        self.approve(self.b, {"schema": 1, "hosts": {"p30": {"ssh": "deploy@192.0.2.30", "roles": ["prod"]}}})
        (self.home / "registry.json").write_text("[1]", encoding="utf-8")
        self.assertEqual(oc.master_prod(self.home, run=FakeRun()), {("192.0.2.30", 22): [self.b.root.as_posix()]})
        (self.home / "trust.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(oc.master_prod(self.home, run=FakeRun()), {})
        (self.home / "trust.json").write_text(json.dumps({"x": "not an entry", oc._key(self.b.store): {"hash": 1}}),
                                              encoding="utf-8")
        self.assertEqual(oc.master_prod(self.home, run=FakeRun()), {})


# ----------------------------------------------------------------------------- guard view
class TestJobIndex(unittest.TestCase):
    def test_cmd_keys(self):
        cases = {"dotnet test --no-restore": ["dotnet test"], "npm test": ["npm test", "npm run test"],
                 "npm run test": ["npm run test", "npm test"], "npm run lint": ["npm run lint"],
                 "pnpm --filter web test": ["pnpm test", "pnpm run test"], "yarn lint": ["yarn lint", "yarn run lint"],
                 "yarn install --frozen-lockfile": ["yarn install"], "python -m pytest -q": ["pytest", "python -m pytest"],
                 "uv run pytest tests": ["pytest"], "CI=1 npx --yes playwright test": ["npx playwright"],
                 "set -e; cd /src/api && dotnet build -c Release": ["dotnet build"], "go test ./...": ["go test"],
                 "py.test": ["pytest"], "python3 -m unittest discover": ["python -m unittest"], "echo hi": [],
                 "npm ci && npm test": ["npm ci", "npm test", "npm run test"], "./node_modules/.bin/jest": ["jest"],
                 "time npm run build": ["npm run build"], "": []}
        for cmd, keys in cases.items():
            self.assertEqual(oc._cmd_keys(cmd), keys, cmd)

    def test_index(self):
        jobs = {"api-test": {"stage": "unit", "prep": "dotnet restore", "cmd": "dotnet test"},
                "web-lint": {"stage": "lint", "prep": "npm ci", "cmd": "npm run lint"},
                "web-test": {"stage": "unit", "prep": "npm ci", "cmd": "npm test"},
                "py-test": {"stage": "unit", "cmd": "python -m pytest -q"},
                "app-image": {"kind": "build", "dir": "web"}}
        self.assertEqual(oc.job_index(jobs), {
            "dotnet test": "api-test", "pytest": "py-test", "python -m pytest": "py-test", "npm test": "web-test",
            "npm run test": "web-test", "npm run lint": "web-lint", "docker build": "app-image",
            "docker buildx build": "app-image", "dotnet restore": "api-test", "npm ci": "web-test"})


class TestGuardView(unittest.TestCase):
    def setUp(self):
        d = tmpdir(self)
        self.home = d / "home"
        self.a = oc.Project(d / "shop", d / "shop" / ".git" / "orch", True)
        self.a_wt = d / "shop-wt"
        self.b = oc.Project(d / "draft", d / "draft" / ".orch", False)
        self.c = oc.Project(d / "web", d / "web" / ".orch", False)
        for p in (self.a, self.b, self.c):
            (p.store / "state").mkdir(parents=True)
        cfg_a = full_cfg()
        cfg_a["jobs"]["py-test"] = {"stage": "unit", "image": "python:3.12-slim" + PIN, "cmd": "pytest -q"}
        oc.save_config(self.a, cfg_a)
        oc.save_state(self.a, "hosts", state())
        oc.trust_set(self.home, self.a, cfg_a, {})
        cfg_b = {"schema": 1, "hosts": {"h40": {"ssh": "deploy@192.0.2.40", "roles": ["prod"]}}}
        oc.save_config(self.b, cfg_b)  # registered but never approved
        reg = oc.registry_load(self.home)
        reg["projects"].append(oc._key(self.b.store))
        oc.registry_save(self.home, reg)
        cfg_c = {"schema": 1, "hosts": {"h12": {"ssh": "app1", "roles": ["helper"], "jobs": "readonly"},
                                        "h31": {"ssh": "deploy@198.51.100.31", "roles": ["prod"]},
                                        "app3": {"ssh": "app3", "roles": ["prod"]}},
                 "production": {"answer": "set"}, "deploy": {"command": "./ship.sh", "rollback": "./back.sh"},
                 "alerts": {"command": None}, "jobs": {}}
        oc.trust_set(self.home, self.c, cfg_c, {}, state_hosts={})
        edited = json.loads(json.dumps(cfg_c))
        del edited["hosts"]["h31"]  # loosening edit, waiting for approval
        oc.save_config(self.c, edited)
        self.sshcfg = d / "ssh" / "config"
        (d / "ssh" / "conf.d").mkdir(parents=True)
        self.sshcfg.write_text("# test config\nHost app1\n  HostName 192.0.2.12\nHost app1-admin\n  HostName 192.0.2.12\n"
                               "  Port 2222\nHost *.example !skip\n  User deploy\nHost=bastion other\n"
                               "  HostName 203.0.113.5\nInclude conf.d/*\n", encoding="utf-8")
        (d / "ssh" / "conf.d" / "extra").write_text("Host h11alias\n  HostName 192.0.2.11\n", encoding="utf-8")
        wt_text = (f"worktree {self.a.root.as_posix()}\nHEAD 1\nbranch refs/heads/main\n\n"
                   f"worktree {self.a_wt.as_posix()}\nHEAD 2\ndetached\n\n"
                   f"worktree {(d / 'gone').as_posix()}\nHEAD 3\nprunable gitdir file points to non-existent location\n")
        self.run = FakeRun(
            worktrees={(self.a.root / ".git").as_posix(): wt_text},
            ssh={"app1": ("192.0.2.12", 22), "app1-admin": ("192.0.2.12", 2222), "bastion": ("203.0.113.5", 22),
                 "other": ("203.0.113.5", 22), "h11alias": ("192.0.2.11", 22), "app3": ("192.0.2.33", 2200)},
            contexts=[{"Name": "default", "DockerEndpoint": "unix:///var/run/docker.sock"},
                      {"Name": "orch-h11", "DockerEndpoint": "ssh://deploy@192.0.2.11"},
                      {"Name": "mine-h12", "DockerEndpoint": "ssh://app1"},
                      {"Name": "tcp12", "DockerEndpoint": "tcp://192.0.2.12:2376"},
                      {"Name": "p20x", "DockerEndpoint": "ssh://deploy@192.0.2.20:2222"},
                      {"Name": "p20-wrong-port", "DockerEndpoint": "ssh://deploy@192.0.2.20"},
                      {"Name": "c33", "DockerEndpoint": "ssh://deploy@app3"}])

    def view(self, run=None, env=None):
        return oc.build_guard_view(self.home, run=run or self.run, env=env or {}, ssh_config=self.sshcfg)

    def test_view(self):
        v = self.view()
        self.assertEqual(set(v), {"schema", "generated", "namespace", "hosts", "projects"})
        self.assertEqual((v["schema"], v["namespace"]), (1, {"label": "io.orch.agent", "prefix": "agentx-"}))
        self.assertEqual([(x["root"], x["store"]) for x in v["projects"]],
                         [(self.a.root.as_posix(), oc._key(self.a.store)), (self.a_wt.as_posix(), oc._key(self.a.store)),
                          (self.c.root.as_posix(), oc._key(self.c.store))])
        pa, pc = v["projects"][0], v["projects"][2]
        self.assertEqual(set(pa), {"root", "store", "deploy", "rollback", "jobs"})
        self.assertEqual((pa["deploy"], pa["rollback"]), (["./scripts/deploy.sh"], []))
        self.assertEqual((pc["deploy"], pc["rollback"], pc["jobs"]), (["./ship.sh"], ["./back.sh"], {}))
        for k, job in {"dotnet test": "api-test", "npm test": "web-test", "npm run test": "web-test",
                       "pytest": "py-test", "npm run lint": "web-lint", "docker build": "app-image"}.items():
            self.assertEqual(pa["jobs"][k], job, k)
        hosts = {x["name"]: x for x in v["hosts"]}
        self.assertEqual(list(hosts), ["app3", "h11", "h12", "h31", "p20"])  # h40 (unapproved project) absent
        self.assertEqual(hosts["app3"], {"name": "app3", "roles": ["prod"], "spec": "app3", "hostname": "192.0.2.33",
                                         "port": 2200, "user": "deploy", "ips": ["192.0.2.33"], "aliases": ["app3"],
                                         "contexts": ["c33"]})  # never probed: resolved with ssh -G
        self.assertEqual(hosts["h12"], {"name": "h12", "roles": ["helper", "prod"], "spec": "app1",
                                        "hostname": "192.0.2.12", "port": 22, "user": "deploy", "ips": ["192.0.2.12"],
                                        "aliases": ["app1"], "contexts": ["orch-h12", "mine-h12", "tcp12"]})
        self.assertEqual((hosts["h11"]["aliases"], hosts["h11"]["contexts"], hosts["h11"]["roles"]),
                         (["h11alias"], ["orch-h11"], ["helper"]))
        self.assertEqual((hosts["p20"]["port"], hosts["p20"]["contexts"], hosts["p20"]["aliases"]), (2222, ["p20x"], []))
        self.assertEqual((hosts["h31"]["roles"], hosts["h31"]["hostname"], hosts["h31"]["user"]),
                         (["prod"], "198.51.100.31", "deploy"))  # kept from the last approved config
        self.assertEqual(self.run.count(["docker"]), 1)
        self.assertEqual(self.run.count(["ssh"]), 6)  # 5 config Hosts (never wildcards) + the unprobed alias app3

    def test_namespace_env_and_failures(self):
        v = self.view(env={"ORCH_LABEL": "io.orchtest.agent", "ORCH_PREFIX": "agentt-"})
        self.assertEqual(v["namespace"], {"label": "io.orchtest.agent", "prefix": "agentt-"})
        run = FakeRun(missing={"git", "ssh", "docker"})
        v = self.view(run=run)
        hosts = {x["name"]: x for x in v["hosts"]}
        self.assertEqual((hosts["h12"]["aliases"], hosts["h12"]["contexts"]), (["app1"], ["orch-h12"]))
        self.assertEqual(v["projects"][0]["root"], self.a.root.as_posix())  # trust root when git is missing
        run = FakeRun(contexts=None)
        self.assertEqual({x["name"]: x["contexts"] for x in self.view(run=run)["hosts"]}["h11"], ["orch-h11"])
        run = FakeRun(missing={"ssh"}, contexts=self.run.contexts)  # no ssh: alias contexts still match by alias
        hosts = {x["name"]: x for x in self.view(run=run)["hosts"]}
        self.assertEqual(hosts["h12"]["contexts"], ["orch-h12", "mine-h12", "tcp12"])
        self.assertEqual((hosts["app3"]["hostname"], hosts["app3"]["contexts"]), ("app3", ["c33"]))

    def test_unresolved_first_sight_is_upgraded(self):
        reg = oc.registry_load(self.home)
        reg["projects"] = [oc._key(self.c.store), oc._key(self.a.store)]  # C (no probe state) merges first
        oc.registry_save(self.home, reg)
        h12 = {x["name"]: x for x in self.view()["hosts"]}["h12"]
        self.assertEqual((h12["hostname"], h12["port"], h12["user"], h12["ips"], h12["roles"]),
                         ("192.0.2.12", 22, "deploy", ["192.0.2.12"], ["helper", "prod"]))

    def test_snapshot_and_unapproved(self):
        cfg = oc.load_config(self.a)
        cfg["hosts"]["h12"]["roles"] = ["helper"]
        oc.save_config(self.a, cfg)  # now unapproved: the approved snapshot still protects h12 as production
        hosts = {x["name"]: x for x in self.view()["hosts"]}
        self.assertEqual(hosts["h12"]["roles"], ["helper", "prod"])
        t = json.loads((self.home / "trust.json").read_text(encoding="utf-8"))
        for e in t.values():
            e.pop("config")
        (self.home / "trust.json").write_text(json.dumps(t), encoding="utf-8")
        v = self.view()
        self.assertEqual([x["name"] for x in v["hosts"]], [])  # no approved content left at all
        self.assertEqual(v["projects"], [])

    def test_write(self):
        path = oc.write_guard_view(self.home, run=self.run, env={}, ssh_config=self.sshcfg)
        self.assertEqual(path, self.home / "guard-view.json")
        got = json.loads(path.read_text(encoding="utf-8"))
        want = self.view()
        got.pop("generated")
        want.pop("generated")
        self.assertEqual(got, want)
        self.assertEqual([x.name for x in self.home.iterdir() if x.name.endswith(".tmp")], [])
        empty = tmpdir(self)
        v = json.loads(oc.write_guard_view(empty, run=FakeRun()).read_text(encoding="utf-8"))
        self.assertEqual((v["hosts"], v["projects"]), ([], []))


if __name__ == "__main__":
    unittest.main()


class TestHealthEntries(unittest.TestCase):
    """deploy.health takes URL strings or {"url", "insecure"} for self-signed internal certificates."""

    def test_forms(self):
        self.assertTrue(oc._health_ok("https://shop.example/health"))
        self.assertTrue(oc._health_ok({"url": "https://198.51.100.7/health", "insecure": True}))
        self.assertFalse(oc._health_ok({"url": "ftp://x"}))
        self.assertEqual(oc.health_label({"url": "https://198.51.100.7/h", "insecure": True}), "https://198.51.100.7/h (insecure)")
        self.assertEqual(oc.health_url("https://shop.example/h"), "https://shop.example/h")
