"""orch init (scripts/orch_init.py) tests: the whole interview in a temp git repo with fake discover, detect, run and
registry; the loosening rule; image pinning order; project location; guard view repair; the real orch_discover
against a probe fixture. No network, ssh or docker: every process goes through FakeRun, and only git runs for real
(in temp folders)."""
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import orch_config as oc  # noqa: E402
import orch_deploy as dp  # noqa: E402
import orch_discover as od  # noqa: E402
import orch_init as oi  # noqa: E402

REAL_MASTER_PROD = getattr(oc, "master_prod", None)  # orch_config's (config owner); tests patch it per case

D1, D2, D3, D4 = ("sha256:" + c * 64 for c in "1234")
M1, M2 = "sha256:" + "a" * 64, "sha256:" + "b" * 64
STATUS_KEYS = {"state", "step", "summary", "problems", "ask", "next", "applied", "warnings"}
LOCAL = {"master1", "198.51.100.7"}
AUTH_FAIL = "deploy@192.0.2.12: Permission denied (publickey)."
PY_URL = "https://registry-1.docker.io/v2/library/python/manifests/3.12-slim"


def R(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def cjob(stage, image, cmd, d, manifest=M1, **kw):
    return {"stage": stage, "kind": "container", "image": image, "dir": d, "cmd": cmd, "memory": "2g",
            "timeout": 900, "detected": f"{d} stack ({d}/manifest)", "files": [f"{d}/manifest"], "manifest": manifest,
            **kw}


JOBS = {"api-unit": cjob("unit", "mcr.microsoft.com/dotnet/sdk:8.0", "dotnet test --no-restore", "api",
                         prep="dotnet restore", cache={"name": "nuget", "path": "/root/.nuget/packages"}),
        "web-unit": cjob("unit", "node:22-bookworm", "npm test", "web", prep="npm ci"),
        "tool-lint": cjob("lint", "python:3.12-slim", "ruff check .", "tool"),
        "e2e-test": cjob("e2e", "registry.example:5000/team/e2e:1", "./run-e2e.sh", "e2e"),
        "web-image": {"stage": "image", "kind": "build", "dockerfile": "web/Dockerfile", "context": "web",
                      "memory": "4g", "timeout": 1800, "detected": "Dockerfile (web/Dockerfile)",
                      "files": ["web/Dockerfile"], "manifest": M1}}


class FakeRun:
    """subprocess.run stand-in. git runs for real (temp repos only). ssh -G answers from aliases (alias ->
    (hostname, port)). docker buildx imagetools inspect answers from buildx (ref -> digest) on this machine and from
    remote (ssh destination -> {ref: digest}) over ssh; anything else fails like an unreachable registry. docker
    context ls fails (no contexts). orch.py gc --session-end --node N (init's cleanup) is recorded in gc (argv, env)
    and exits gc_rc.get(N, 0), printing like the real child: its verdict last on stdout (gc_out[N] replaces it), a
    progress line on stderr and, unless env ORCH_JOURNAL=0, orch's journal note after a failure."""

    def __init__(self, aliases=None, buildx=None, remote=None):
        self.aliases, self.buildx, self.remote = aliases or {}, buildx or {}, remote or {}
        self.calls, self.gc, self.gc_rc, self.gc_out = [], [], {}, {}

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[0] == "git":
            kw.pop("stdin", None)
            return subprocess.run(argv, **kw)
        if len(argv) > 2 and argv[1].endswith("orch.py") and argv[2] == "gc":
            env, n = kw.get("env") or {}, argv[-1]
            self.gc.append((argv[2:], env))
            rc, again = self.gc_rc.get(n, 0), f"run orch gc --session-end --node {n} again"
            out = self.gc_out.get(n) or (
                f"{n}: removed 2: container agentx-builder-{n}, volume agentx-cache-npm\n" if rc == 0 else
                f"{n}: NOT cleaned (ssh: connect to host 192.0.2.11 port 22: Connection timed out); {again} when it "
                f"is reachable\n" if rc == 5 else
                f"{n}: live job(s) j-1 still run there; {again} after they end\n" if rc == 75 else
                f"unknown node {n} (known: none)\n")
            err = f"orch[09:00:00] gc --session-end --node {n}\n"
            if rc and env.get("ORCH_JOURNAL") != "0":
                err += "journal: difficulty #7 recorded (orch)\n"
            return R(rc, "removed 0 cache entries\n" + out, err)
        if argv[0] == "ssh" and "-G" in argv:
            hp = self.aliases.get(argv[-1])
            if hp is None:
                return R(255, "", f"ssh: Could not resolve hostname {argv[-1]}")
            return R(0, f"user deploy\nhostname {hp[0]}\nport {hp[1]}\n")
        if argv[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            return self.answer(self.buildx.get(argv[4]))
        if argv[0] == "ssh":
            m = re.fullmatch(r'docker buildx imagetools inspect (\S+) --format "\{\{json \.Manifest\}\}"', argv[-1])
            if m:
                return self.answer((self.remote.get(argv[-2]) or {}).get(m.group(1)))
        if argv[:3] == ["docker", "context", "ls"]:
            return R(1, "", "docker: not found")
        raise AssertionError(f"unexpected command {argv}")

    @staticmethod
    def answer(digest):
        if digest:
            return R(0, json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "digest": digest,
                                    "size": 1609}) + "\n")
        return R(1, "", "ERROR: failed to do request: dial tcp 203.0.113.9:443: i/o timeout")

    def seen(self, prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


class FakeDiscover:
    """orch_discover stand-in. hosts: spec -> (hostname, port) (ssh -G); cot: spec -> co-tenant names; fail: spec ->
    stderr of a failing ssh (classified by the real classify_ssh_error). Transport choice is the real one."""
    DiscoverError = od.DiscoverError
    SSH_BASE = od.SSH_BASE
    ssh_args = staticmethod(od.ssh_args)
    choose_transport = staticmethod(od.choose_transport)

    def __init__(self, hosts, cot=None, fail=None):
        self.hosts, self.cot, self.fail = dict(hosts), dict(cot or {}), dict(fail or {})
        self.probed, self.contexts, self.removed = [], {}, []
        self.foreign = {}  # spec -> another orch namespace's objects there (the real foreign_problem words them)

    def ssh_resolve(self, spec, run=None):
        if spec not in self.hosts:
            raise od.DiscoverError(f"ssh could not read its configuration for {spec}: bad line; fix ~/.ssh/config")
        hn, port = self.hosts[spec]
        return {"hostname": hn, "user": "deploy", "port": port, "alias": "@" not in spec}

    def probe_hosts(self, specs, docker_names, run=None, workers=8):
        self.probed.append(sorted(specs))
        return {n: self.rec(n, s, n in docker_names) for n, s in specs.items()}

    def rec(self, name, spec, docker):
        res = self.ssh_resolve(spec)
        r = {"name": name, "ok": True, "probed": "2026-10-30T09:00:00", "spec": spec, "resolved": res, "facts": {},
             "problem": None}
        if spec in self.fail:
            r.update(ok=False, problem=od.classify_ssh_error(255, self.fail[spec], spec))
        elif docker:
            r["facts"] = {"os": "linux", "arch": "x86_64", "cpus": 8, "mem_bytes": 16 * 2 ** 30, "disk_free_bytes": 4e11,
                          "docker": "27.3.1", "docker_id": "id-" + res["hostname"], "rootless": False, "buildx": True,
                          "psi": True, "cgroup": 2, "socket": "default", "endpoints": [],
                          "cotenants": [{"name": c, "image": c + ":1", "ports": [8080]} for c in self.cot.get(spec, [])]}
            if spec in self.foreign:  # as probe_host: Docker answers (facts kept), the helper is not ok
                r.update(ok=False, problem=od.foreign_problem(self.foreign[spec], "agentx-", od.ssh_cmdline(
                    spec, "docker ps -a --filter name=agentx-")))
        return r

    def master_facts(self, run=None):
        return {"os": "linux", "python": "3.12", "git": True, "docker_cli": "27.3.1"}

    def ensure_context(self, name, spec, run=None, env=None, own=()):
        self.contexts[name] = spec
        return "orch-" + name

    def remove_context(self, name, run=None, env=None, context=None):
        self.removed.append(name)
        self.contexts.pop(name, None)


class FakeDetect:
    def __init__(self, jobs):
        self.jobs, self.calls = copy.deepcopy(jobs), 0

    def detect(self, root, max_depth=3, project_name=None, run=None):
        self.calls += 1
        return {"jobs": copy.deepcopy(self.jobs), "notes": [], "unknown": []}


class FakeHttp:
    """Registry stand-in: a manifest HEAD answers 401 with a Bearer challenge until the anonymous token is sent, then
    200 with the digest; unknown URLs are unreachable."""

    def __init__(self, digests):
        self.digests, self.calls = dict(digests), []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, headers.get("Authorization")))
        if url.startswith("https://auth.example/token?"):
            return 200, {}, json.dumps({"token": "tok-anon-1"}).encode()
        if url in self.digests:
            if headers.get("Authorization") != "Bearer tok-anon-1":
                return 401, {"www-authenticate": 'Bearer realm="https://auth.example/token",service="reg.example",'
                                                 'scope="repository:library/python:pull"'}, b""
            return 200, {"docker-content-digest": self.digests[url]}, b""
        raise OSError("connection refused")


class FakeJournal:
    def __init__(self):
        self.records = []

    def add(self, p, data, by="agent", now=None):
        self.records.append((by, dict(data)))
        return data


def tmpdir(t) -> Path:
    d = Path(tempfile.mkdtemp(prefix="orchinit-")).resolve()
    t.addCleanup(shutil.rmtree, d, True)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.root = self.d / "shop"
        for sub in ("api", "web", "tool", "e2e", "scripts"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "scripts" / "deploy.sh").write_text("#!/bin/sh\necho deploy\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.home = self.d / "orchhome"
        self.sshcfg = self.d / "ssh_config"
        self.sshcfg.write_text("Host app-builder\n  HostName 192.0.2.11\nHost spare\n  HostName 198.51.100.30\n"
                               "Host self\n  HostName 198.51.100.7\nHost bare\nHost *.example\n  User deploy\n",
                               encoding="utf-8")
        self.disc = FakeDiscover({"app-builder": ("192.0.2.11", 22), "deploy@192.0.2.11": ("192.0.2.11", 22),
                                  "deploy@192.0.2.12": ("192.0.2.12", 22), "deploy@192.0.2.20:2222": ("192.0.2.20", 2222),
                                  "deploy@192.0.2.13": ("192.0.2.13", 22), "master1": ("master1", 22)},
                                 cot={"deploy@192.0.2.12": ["app1-web", "app1-db"]})
        self.det = FakeDetect(JOBS)
        self.run = FakeRun(aliases={"app-builder": ("192.0.2.11", 22), "spare": ("198.51.100.30", 22),
                                    "self": ("198.51.100.7", 22), "bare": ("bare", 22)},
                           buildx={"mcr.microsoft.com/dotnet/sdk:8.0": D1},
                           remote={"app-builder": {"node:22-bookworm": D2},
                                   "deploy@192.0.2.12": {"node:22-bookworm": D4}})
        self.http = FakeHttp({PY_URL: D3})
        self.journal = FakeJournal()
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)
        self.mprod = {}  # what orch_config.master_prod answers: (hostname, port) -> roots of other approved projects
        self.mprod_excl = []  # the exclude= (a store) of every call: init leaves its own project out by store (Q5)
        self.mprod_patch = mock.patch.object(oc, "master_prod", create=True, new=self.fake_mprod)
        self.mprod_patch.start()
        self.addCleanup(self.mprod_patch.stop)
        self.where = None  # the folder project="root" means (None = self.root; a linked worktree in TestWorktrees)

    def fake_mprod(self, home, run=None, exclude=None):
        self.mprod_excl.append(exclude)
        return copy.deepcopy(self.mprod)

    def init(self, *flags, ans=None, project="root", ask=None):
        a = SimpleNamespace(offline="offline" in flags, apply=ans is not None, detect="detect" in flags,
                            approve="approve" in flags, show="show" in flags, ask=ask,
                            project=None if project is None else
                            str((self.where or self.root) if project == "root" else project))
        stdin = None if ans is None else io.StringIO(ans if isinstance(ans, str) else json.dumps(ans))
        ctx = oi.Ctx(run=self.run, env={"ORCH_HOME": str(self.home)}, discover=self.disc, detect=self.det,
                     journal=self.journal, http=self.http, local_names=lambda: set(LOCAL), ssh_config=self.sshcfg,
                     stdin=stdin)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = oi.cmd_init(a, ctx)
        lines = out.getvalue().splitlines()
        js = json.loads(lines[-1])
        self.assertEqual(set(js) - {"error"}, STATUS_KEYS, js)
        self.assertEqual("error" in js, rc == 2, js)
        return rc, lines[:-1], js

    def ok(self, *flags, ans=None):
        rc, lines, js = self.init(*flags, ans=ans)
        self.assertEqual(rc, 0, js)
        return lines, js

    @property
    def store(self):
        return self.root / ".git" / "orch"

    def cfg(self):
        return json.loads((self.store / "config.json").read_text(encoding="utf-8"))

    def state(self):
        return json.loads((self.store / "state" / "hosts.json").read_text(encoding="utf-8"))

    def trust(self):
        t = json.loads((self.home / "trust.json").read_text(encoding="utf-8"))
        return next(iter(t.values()))

    def view(self):
        return json.loads((self.home / "guard-view.json").read_text(encoding="utf-8"))

    def reach_ready(self):
        """The shortest interview: two helpers (one shared), a production-only host, deploy, detected jobs minus the
        unpinnable one, approved."""
        self.ok(ans={"helper_count": 2})
        self.ok(ans={"helpers": ["app-builder", "deploy@192.0.2.12"]})
        _, js = self.ok()
        ex = js["ask"]["example"]
        self.ok(ans={**ex, "production": ["deploy@192.0.2.20:2222"],
                     "deploy": {"command": "./scripts/deploy.sh", "health": ["https://shop.example/health"]}})
        self.ok(ans={"jobs": "detected", "jobs_remove": ["e2e-test"]})
        _, js = self.ok("approve")
        self.assertEqual(js["state"], "ready", js)
        return js


class TestInterview(Base):
    def test_full_interview(self):
        # new: nothing is created before the first answer
        _, js = self.ok()
        self.assertEqual((js["state"], js["step"], js["next"]), ("new", "count", "ask"))
        self.assertEqual(js["ask"]["answer_key"], "helper_count")
        self.assertEqual(js["ask"]["options"], ["1", "2", "3", "4+"])
        self.assertFalse(self.store.exists())
        self.assertTrue((self.home / "guard-view.json").is_file())  # the guard is on before anything is approved

        # count -> helpers, with candidates from ~/.ssh/config (HostName set, not this machine)
        _, js = self.ok(ans={"helper_count": "2"})
        self.assertEqual((js["state"], js["step"]), ("incomplete", "helpers"))
        self.assertEqual(js["ask"]["options"], ["app-builder (192.0.2.11:22)", "spare (198.51.100.30:22)"])
        self.assertTrue(js["ask"]["multi"])
        self.assertEqual(self.cfg()["pending"], {"helper_count": 2})

        # count mismatch and this master: refused, nothing saved, the same question comes back
        rc, _, js = self.init(ans={"helpers": ["app-builder"]})
        self.assertEqual(rc, 2)
        self.assertIn("helper_count is 2", js["error"])
        self.assertEqual(js["step"], "helpers")
        rc, _, js = self.init(ans={"helpers": ["app-builder", "master1"]})
        self.assertEqual(rc, 2)
        self.assertIn("master1 is this machine", js["error"])
        self.assertNotIn("hosts", self.cfg())
        self.assertEqual(js["applied"], [])

        # an offered option and the same machine typed again: saved once, and the question comes back for one more
        lines, js = self.ok(ans={"helpers": ["app-builder (192.0.2.11:22)", "deploy@192.0.2.11"]})
        cfg = self.cfg()
        self.assertEqual(cfg["hosts"], {"h11": {"ssh": "app-builder", "roles": ["helper"]}})
        self.assertEqual(cfg["pending"], {"helper_count": 2})
        self.assertTrue(any("same machine (192.0.2.11:22)" in w for w in js["warnings"]), js["warnings"])
        self.assertTrue(any("1 different helper saved, 2 wanted" in w for w in js["warnings"]), js["warnings"])
        self.assertIn("h11 (app-builder) ok: linux x86_64, 8 cpu, 16 GB, docker 27.3.1, no other containers", lines)
        self.assertEqual(js["step"], "helpers")
        # R31/R32: only the missing helper is asked for and the saved one is never offered; the one candidate left is
        # named in a text question (a choice needs 2-4 options), and one answer means no multi-select
        a = js["ask"]
        self.assertEqual((a["kind"], a["options"], a["multi"], a["answer_key"]), ("text", [], False, "helpers"))
        self.assertEqual(a["question"], "Saved: h11 (app-builder). For the new helper, what do you type after ssh? "
                                        "(alias, or user@IP[:port]); known: spare (198.51.100.30:22)")
        self.assertEqual(len(a["example"]["helpers"]), 1)

        # helpers: the full list again (additive: h11 stays), one ssh auth failure
        self.disc.fail["deploy@192.0.2.12"] = AUTH_FAIL
        lines, js = self.ok(ans={"helpers": ["app-builder", "deploy@192.0.2.12"]})
        cfg = self.cfg()
        self.assertEqual(sorted(cfg["hosts"]), ["h11", "h12"])
        self.assertNotIn("pending", cfg)
        self.assertIn("helpers: h11 (app-builder) already saved; h12 (deploy@192.0.2.12) added", js["applied"])
        self.assertIn("h12 (deploy@192.0.2.12) problem: auth (see problems)", lines)  # one status line per host
        self.assertEqual((js["state"], js["step"], js["next"]), ("incomplete", "fix", "fix"))
        self.assertEqual(js["ask"]["answer_key"], "remove_hosts")
        pr = js["problems"][0]
        self.assertEqual((pr["host"], pr["fix"]), ("h12 (deploy@192.0.2.12)", "ssh-copy-id deploy@192.0.2.12"))
        self.assertIn("BatchMode=yes", pr["tried"])
        # R33: what, tried and fix are in the JSON line's problems only, never printed again as human lines
        self.assertFalse([x for x in lines if re.match(r"\s*(problem|tried|fix):", x)], lines)
        self.assertNotIn(pr["tried"], "\n".join(lines))
        self.assertEqual(self.disc.contexts, {"h11": "app-builder"})
        st = self.state()
        self.assertEqual((st["h11"]["transport"], st["h11"]["context"]), ("context", "orch-h11"))
        self.assertFalse(st["h12"]["ok"])
        reg = json.loads((self.home / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(reg["nodes"]["h11"], {"hostname": "192.0.2.11", "port": 22, "docker_id": "id-192.0.2.11"})

        # the user fixed ssh: retry = plain orch init -> setup, shared helper defaults to helper only with lint + tests, and
        # never helper + production on a guess: production is only what the user names (owner, 2026-10-31)
        self.disc.fail.clear()
        _, js = self.ok()
        self.assertEqual((js["state"], js["step"]), ("incomplete", "setup"))
        q = js["ask"]["question"]
        self.assertIn("h11 (app-builder): 8 cpu, 16 GB, no other containers [helper only; everything]", q)
        self.assertIn("h12 (deploy@192.0.2.12): 8 cpu, 16 GB, 2 other containers [helper only; lint + tests, no image builds]",
                      q)
        ex = js["ask"]["example"]
        self.assertEqual(ex["hosts"], {"h11": {"roles": ["helper"], "jobs": "all"},
                                       "h12": {"roles": ["helper"], "jobs": "tests"}})

        # setup "ok" with production later, sent wrapped in the answer_key -> jobs (detected now, stored)
        _, js = self.ok(ans={"setup": {**ex, "production": "later"}})
        cfg = self.cfg()
        self.assertEqual((cfg["production"], cfg["deploy"], cfg["alerts"]),
                         ({"answer": "later"}, {"answer": "later"}, {"command": None}))
        self.assertEqual((js["state"], js["step"], js["ask"]["options"]), ("incomplete", "jobs", ["yes", "change"]))
        self.assertIn("api-unit  unit", js["ask"]["question"])
        self.assertEqual(self.det.calls, 1)
        self.assertTrue((self.store / "state" / "jobs-detected.json").is_file())

        # production answered later: a production-only host (ssh probe only) + the deploy command
        lines, js = self.ok(ans={"production": ["deploy@192.0.2.20:2222"],
                                 "deploy": {"command": "./scripts/deploy.sh", "health": "https://shop.example/health"}})
        cfg = self.cfg()
        self.assertEqual(cfg["hosts"]["h20"], {"ssh": "deploy@192.0.2.20:2222", "roles": ["prod"]})
        self.assertEqual(cfg["production"], {"answer": "set"})
        self.assertEqual(cfg["deploy"], {"command": "./scripts/deploy.sh", "health": ["https://shop.example/health"],
                                         "rollback": None})
        self.assertIn("h20 (deploy@192.0.2.20:2222) ok: reachable over ssh (production only; Docker not probed)", lines)
        self.assertEqual(self.disc.probed[-1], ["h20"])
        self.assertEqual(js["step"], "jobs")

        # jobs: take the detection -> approve step
        _, js = self.ok(ans={"jobs": "detected"})
        self.assertEqual(sorted(self.cfg()["jobs"]), sorted(JOBS))
        self.assertEqual((js["state"], js["step"], js["next"]), ("unapproved", "approve", "approve"))
        self.assertTrue(js["ask"]["question"].endswith("Save and use this setup?"))

        # approve: one image no way can pin -> refused with the fix, nothing approved
        rc, _, js = self.init("approve")
        self.assertEqual(rc, 2)
        self.assertIn("could not pin 1 image to a digest: registry.example:5000/team/e2e:1 (job e2e-test)", js["error"])
        self.assertIn("orch init --apply", js["error"])
        self.assertFalse((self.home / "trust.json").exists())
        self.assertNotIn("@sha256:", json.dumps(self.cfg()["jobs"]))

        self.ok(ans={"jobs_remove": ["e2e-test"]})
        lines, js = self.ok("approve")
        self.assertIn(f"pinned mcr.microsoft.com/dotnet/sdk:8.0 -> {D1[:19]}... (via this machine)", lines)
        self.assertIn(f"pinned node:22-bookworm -> {D2[:19]}... (via helper h11)", lines)  # helper-only first
        self.assertIn(f"pinned python:3.12-slim -> {D3[:19]}... (via registry)", lines)
        jobs = self.cfg()["jobs"]
        self.assertEqual(jobs["web-unit"]["image"], "node:22-bookworm@" + D2)
        self.assertEqual(jobs["tool-lint"]["image"], "python:3.12-slim@" + D3)
        self.assertNotIn("image", jobs["web-image"])
        self.assertEqual(self.http.calls[0], ("HEAD", PY_URL, None))
        self.assertEqual(self.http.calls[-1], ("HEAD", PY_URL, "Bearer tok-anon-1"))
        node = [c for c in self.run.calls if "node:22-bookworm" in " ".join(c)]
        self.assertEqual(node[0][:4], ["docker", "buildx", "imagetools", "inspect"])  # this machine first
        self.assertEqual(node[1][0], "ssh")
        t = self.trust()
        self.assertEqual(t["hash"], oc.config_hash(self.cfg()))
        self.assertEqual(list(t["scripts"]), ["scripts/deploy.sh"])
        self.assertEqual(t["cotenants"], {"h11": [], "h12": ["app1-db", "app1-web"]})
        v = self.view()
        self.assertEqual([h["name"] for h in v["hosts"]], ["h11", "h12", "h20"])
        self.assertEqual(v["projects"][0]["root"], self.root.as_posix())
        self.assertEqual(v["projects"][0]["deploy"], ["./scripts/deploy.sh"])
        whats = [d["what"] for by, d in self.journal.records]
        self.assertTrue(all(by == "orch" and d["why"] == "user answer" and d["type"] == "decision"
                            for by, d in self.journal.records))
        self.assertTrue(any(w.startswith("helpers: h11 (app-builder) added") for w in whats), whats)
        self.assertIn("helper_count: 2 helpers", whats)
        self.assertTrue(whats[-1].startswith("setup approved: setup ok:"), whats)

        # later session: ready, one line, no question
        _, js = self.ok()
        self.assertEqual((js["state"], js["step"], js["next"], js["ask"]), ("ready", "none", "ready", None))
        self.assertEqual(len(js["summary"]), 1)
        self.assertRegex(js["summary"][0], r"^setup ok: helpers h11 \(app-builder\) all, h12 \(deploy@192\.0\.2\.12\) "
                                           r"tests \| prod h20 \(deploy@192\.0\.2\.20:2222\) \| 4 jobs \| "
                                           r"approved \d{4}-\d\d-\d\d$")
        self.assertEqual(js["problems"], [])

    def test_unknown_key_and_bad_input(self):
        rc, _, js = self.init(ans={"helper_count": 1, "colour": "blue"})
        self.assertEqual(rc, 2)
        self.assertIn("unknown answer key 'colour'; valid keys: helper_count, helpers, hosts", js["error"])
        self.assertFalse((self.store / "config.json").exists())
        rc, _, js = self.init(ans="{not json")
        self.assertEqual(rc, 2)
        self.assertIn("not valid JSON (line 1", js["error"])
        rc, _, js = self.init(ans="")
        self.assertIn("quoted heredoc", js["error"])
        rc, _, js = self.init(ans={"helper_count": "4+"})
        self.assertIn("helper_count must be a whole number", js["error"])
        self.ok(ans={"helper_count": 1})
        rc, _, js = self.init(ans={"helpers": ["deploy@192.0.2.12; rm -rf /"]})
        self.assertEqual(rc, 2)
        self.assertIn("spaces and shell characters are not allowed", js["error"])
        self.ok(ans={"helpers": ["deploy@192.0.2.12"]})
        rc, _, js = self.init(ans={"hosts": {"h12": {"roles": ["helper", "prod"], "jobs": "all"}}})
        self.assertIn("runs at most lint + tests", js["error"])
        rc, _, js = self.init(ans={"alerts": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop' x"}})
        self.assertIn("seems to hold a secret", js["error"])
        rc, _, js = self.init(ans={"deploy": {"health": ["ftp://shop.example/"]}})
        self.assertIn("is not an http(s) URL", js["error"])
        rc, _, js = self.init(ans={"jobs": {"x-unit": {"image": "Bad Image", "cmd": "pytest"}}})
        self.assertIn("job x-unit: image", js["error"])
        rc, _, js = self.init(ans={"jobs": {"x-unit": {"image": "python:3.12", "cmd": "pytest", "memory": "64g"}}})
        self.assertIn("must be from 16m to 6g", js["error"])
        rc, _, js = self.init(ans={"jobs": {"x-unit": {"image": "python:3.12", "cmd": "pytest",
                                                       "env": {"API_TOKEN": "abc"}}}})
        self.assertIn("env must not hold secrets", js["error"])
        rc, _, js = self.init(ans={"jobs": {"x-unit": {"image": "python:3.12", "cmd": "pytest", "dir": "../up"}}})
        self.assertIn("must be a path inside the project", js["error"])
        self.assertEqual(self.cfg()["hosts"]["h12"], {"ssh": "deploy@192.0.2.12", "roles": ["helper"]})

    def test_show_and_word_answers(self):
        self.ok(ans={"helper_count": 1})
        self.ok(ans={"helpers": "deploy@192.0.2.12"})
        self.ok(ans={"hosts": {"h12": {"roles": "helper + production", "jobs": "lint + tests, no image builds",
                                       "watch": "http://127.0.0.1:8080/app/"}},
                     "production": "none", "deploy": "later", "alerts": "none"})
        h = self.cfg()["hosts"]["h12"]
        self.assertEqual((h["roles"], h["jobs"], h["watch"]), (["helper", "prod"], "tests", ["http://127.0.0.1:8080/app/"]))
        lines, js = self.ok("show", "offline")
        self.assertIn(f"project {self.root.as_posix()} (setup in {self.store.as_posix()})", lines)
        self.assertTrue(any(x.startswith("host h12 (deploy@192.0.2.12): helper + production; jobs tests") for x in lines))
        self.assertIn("approval: never approved", lines)
        self.assertEqual(js["step"], "jobs")

    def test_approve_waits_for_deploy_and_alerts(self):
        # R30: a setup reply without deploy and alerts brings the setup step back for exactly those lines, and
        # --approve refuses, naming them, until they are answered: approve always comes after the setup step
        self.ok(ans={"helper_count": 1})
        self.ok(ans={"helpers": ["app-builder"]})
        _, js = self.ok(ans={"hosts": {"h11": {"roles": ["helper"], "jobs": "all"}}, "production": "none"})
        self.assertEqual((js["state"], js["step"], js["ask"]["example"]),
                         ("incomplete", "setup", {"deploy": "later", "alerts": "none"}))
        self.assertNotIn("h11", js["ask"]["question"])  # an answered line is never asked again
        self.ok(ans={"jobs": "detected", "jobs_remove": ["e2e-test"]})
        rc, _, js = self.init("approve")
        self.assertEqual(rc, 2)
        self.assertIn("the setup question is not answered yet: still missing deploy, alerts; run orch init", js["error"])
        self.assertEqual((js["step"], js["ask"]["example"]), ("setup", {"deploy": "later", "alerts": "none"}))
        self.assertFalse((self.home / "trust.json").exists())
        _, js = self.ok(ans=js["ask"]["example"])
        self.assertEqual((js["state"], js["step"], js["next"]), ("unapproved", "approve", "approve"))
        _, js = self.ok("approve")
        self.assertEqual((js["state"], js["ask"]), ("ready", None))


class TestLoosening(Base):
    def make_h12_production(self):
        # a shared helper is never production by default: the user says so (helper + production, read-only)
        self.ok(ans={"hosts": {"h12": {"roles": ["helper", "prod"], "jobs": "readonly"}}})
        _, js = self.ok("approve")
        self.assertEqual(js["state"], "ready")

    def test_loosening_after_approval_waits_for_approve(self):
        self.reach_ready()
        self.make_h12_production()
        before = self.trust()["hash"]
        _, js = self.ok(ans={"hosts": {"h12": {"roles": ["helper"]}}})
        self.assertEqual((js["state"], js["step"], js["next"]), ("unapproved", "approve", "approve"))
        self.assertIn("h12 (deploy@192.0.2.12) is no longer a production server", js["ask"]["question"])
        self.assertEqual(self.trust()["hash"], before)
        h12 = next(h for h in self.view()["hosts"] if h["name"] == "h12")
        self.assertEqual(h12["roles"], ["helper", "prod"])  # the approved protection stays until --approve
        _, js = self.ok("approve")
        self.assertEqual(js["state"], "ready")
        self.assertEqual(next(h for h in self.view()["hosts"] if h["name"] == "h12")["roles"], ["helper"])

    def test_tightening_stays_approved_and_keeps_the_cotenant_snapshot(self):
        self.reach_ready()
        self.disc.cot["app-builder"] = ["intruder"]  # the dedicated helper now runs someone else's container
        _, js = self.ok()
        self.assertEqual((js["state"], js["step"]), ("drift", "approve"))
        self.assertIn("now runs 1 other container (intruder)", js["ask"]["question"])
        _, js = self.ok(ans={"hosts": {"h11 (app-builder)": {"jobs": "tests"}}})  # a label works as the key
        self.assertIn("approval: kept (no change loosens protection)", js["applied"])
        self.assertEqual(self.journal.records[-1][1]["why"], "an approved setup changed without loosening protection")
        self.assertEqual(self.trust()["hash"], oc.config_hash(self.cfg()))
        self.assertEqual(self.trust()["cotenants"]["h11"], [])
        self.assertEqual((js["state"], js["step"]), ("drift", "approve"))  # the drift is not swallowed
        _, js = self.ok("approve")  # the user said yes: re-approving an unchanged hash is fine
        self.assertEqual(js["state"], "ready")
        self.assertEqual(self.trust()["cotenants"]["h11"], ["intruder"])

    def test_remove_hosts(self):
        self.reach_ready()
        self.make_h12_production()
        _, js = self.ok(ans={"remove_hosts": ["h12 (deploy@192.0.2.12)"]})
        self.assertNotIn("h12", self.cfg()["hosts"])
        self.assertNotIn("h12", self.state())
        self.assertIn("h12", self.disc.removed)
        self.assertEqual(js["applied"][0], "remove_hosts: removed h12 (deploy@192.0.2.12)")
        self.assertEqual(js["state"], "unapproved")  # a production server left the setup
        _, js = self.ok(ans={"remove_hosts": "h99"})
        self.assertIn("remove_hosts: h99 is not a saved host", js["warnings"][0])

    def test_detect_shows_real_changes_only(self):
        self.reach_ready()
        self.det.jobs["api-unit"]["manifest"] = M2  # a manifest edit that changes nothing orch runs
        self.det.jobs["web-unit"].update(manifest=M2, cmd="npm run test:ci")
        lines, js = self.ok("detect")
        self.assertIn("  changed: web-unit: cmd npm test -> npm run test:ci", lines)
        self.assertFalse(any("api-unit:" in x for x in lines), lines)
        self.assertIn("  new: e2e-test", lines)
        self.assertEqual((js["state"], js["step"]), ("drift", "jobs"))
        self.assertIn("web-unit:", js["ask"]["question"])
        self.assertNotIn("api-unit:", js["ask"]["question"])
        _, js = self.ok(ans=js["ask"]["examples"]["keep"])
        self.assertEqual(js["state"], "ready", js)
        self.assertEqual(self.cfg()["jobs"]["web-unit"]["cmd"], "npm test")
        self.assertEqual(self.cfg()["jobs"]["web-unit"]["image"], "node:22-bookworm@" + D2)


class TestProjectAndGuardView(Base):
    def test_outside_any_project_creates_nothing(self):
        plain = self.d / "plain"
        plain.mkdir()
        os.chdir(plain)
        rc, lines, js = self.init(project=None)
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], f"no project here ({plain.as_posix()}): cd into your project or pass --project DIR")
        self.assertEqual((js["state"], js["next"]), ("new", "fix"))
        self.assertFalse((plain / ".orch").exists())
        self.assertFalse(self.home.exists())

    def test_non_git_needs_an_explicit_project(self):
        plain = self.d / "plain"
        plain.mkdir()
        os.chdir(plain)
        rc, _, js = self.init(ans={"helper_count": 1}, project=None)
        self.assertEqual(rc, 2)
        self.assertIn("a folder outside git becomes a project only when named", js["error"])
        self.assertFalse((plain / ".orch").exists())
        rc, _, js = self.init(ans={"helper_count": 1}, project=plain)
        self.assertEqual(rc, 0, js)
        self.assertIn("is not a git repository, so orch keeps this setup in", js["warnings"][0])
        self.assertEqual(json.loads((plain / ".orch" / "config.json").read_text(encoding="utf-8"))["pending"],
                         {"helper_count": 1})

    def test_corrupt_guard_view_is_rewritten(self):
        self.reach_ready()
        (self.home / "guard-view.json").write_text("{not json", encoding="utf-8")
        self.ok("offline")
        self.assertEqual(self.view()["projects"][0]["root"], self.root.as_posix())
        (self.home / "guard-view.json").write_text("{not json", encoding="utf-8")
        plain = self.d / "plain"
        plain.mkdir()
        os.chdir(plain)
        self.init(project=None)  # outside every project too
        self.assertEqual(self.view()["projects"][0]["root"], self.root.as_posix())

    def test_require_digest_off_approves_with_warnings(self):
        self.ok(ans={"helper_count": 1})
        self.ok(ans={"helpers": ["app-builder"]})
        self.ok(ans={"hosts": {"h11": {"roles": ["helper"], "jobs": "all"}}, "production": "none", "deploy": "later",
                     "alerts": "none"})
        self.ok(ans={"jobs": {"e2e-test": copy.deepcopy(JOBS["e2e-test"])}, "jobs_remove": [
            "api-unit", "web-unit", "tool-lint", "web-image"]})
        cfg = self.cfg()
        cfg["policy"] = {"require_digest": False}
        (self.store / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        _, js = self.ok("approve")
        self.assertTrue(js["warnings"][0].startswith("not pinned (policy require_digest is off): "
                                                     "registry.example:5000/team/e2e:1 (job e2e-test)"), js)
        self.assertEqual(js["state"], "ready")

    def test_approve_rechecks_hand_edited_jobs(self):
        self.ok(ans={"helper_count": 1})
        self.ok(ans={"helpers": ["app-builder"]})
        self.ok(ans={"hosts": {"h11": {"roles": ["helper"], "jobs": "all"}}, "production": "none", "deploy": "later",
                     "alerts": "none"})
        self.ok(ans={"jobs": {"py-unit": {"stage": "unit", "image": "python:3.12-slim@" + D3, "cmd": "pytest -q",
                                          "env": {"TOKEN_FILE": "/run/secrets/t"}}}, "jobs_remove": list(JOBS)})
        cfg = self.cfg()
        self.assertEqual(sorted(cfg["jobs"]), ["py-unit"])
        cfg["jobs"]["py-unit"]["memory"] = "64g"
        (self.store / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        rc, _, js = self.init("approve")
        self.assertEqual(rc, 2)
        self.assertIn("job py-unit: memory 64g must be from 16m to 6g", js["error"])
        self.assertFalse((self.home / "trust.json").exists())

    def test_non_git_jobs_warn_that_they_cannot_ship(self):
        plain = self.d / "plain"
        (plain / "api").mkdir(parents=True)
        rc, _, js = self.init(ans={"jobs": {"api-unit": {"image": "python:3.12-slim", "cmd": "pytest", "dir": "api"}}},
                              project=plain)
        self.assertEqual(rc, 0, js)
        self.assertTrue(any("cannot run until you run git init and commit there" in w for w in js["warnings"]), js)

    def test_argparse_has_no_abbreviations(self):
        ap = oi.add_arguments(__import__("argparse").ArgumentParser(prog="orch init", allow_abbrev=False))
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ap.parse_args(["--appr"])
            with self.assertRaises(SystemExit):
                ap.parse_args(["--apply", "--approve"])
        self.assertTrue(ap.parse_args(["--show", "--offline"]).offline)
        self.assertEqual(ap.parse_args(["--ask", "deploy"]).ask, "deploy")
        with redirect_stderr(io.StringIO()):
            for bad in (["--ask", "alerts"], ["--ask", "deploy", "--apply"]):
                with self.assertRaises(SystemExit, msg=bad):
                    ap.parse_args(bad)


class TestRealDiscover(Base):
    """The real orch_discover module behind a fake run: probe fixture -> state/hosts.json record and docker context."""

    def test_apply_probes_with_real_discover(self):
        fixture = (ROOT / "tests" / "fixtures" / "discover-linux.txt").read_text(encoding="utf-8")
        fake = self.run

        def run(argv, **kw):
            argv = [str(a) for a in argv]
            if argv[:3] == ["ssh", "-v", "-G"]:
                return R(0, "user deploy\nhostname 192.0.2.11\nport 22\n", "debug1: Reading configuration data x\n")
            if argv[0] == "ssh" and argv[-1] == "sh -s":
                return R(0, fixture, "")
            if argv == ["docker", "--version"]:
                return R(0, "Docker version 27.3.1, build ce12230\n")
            if argv[:3] == ["docker", "context", "inspect"]:
                return R(1, "", "context \"orch-h11\": not found")
            if argv[:3] == ["docker", "context", "create"]:
                fake.calls.append(argv)
                return R(0, "orch-h11\n")
            return fake(argv, **kw)

        self.run, self.disc = run, od
        self.ok(ans={"helper_count": 1})
        lines, js = self.ok(ans={"helpers": ["deploy@192.0.2.11"]})
        rec = self.state()["h11"]
        self.assertTrue(rec["ok"], rec)
        self.assertEqual((rec["transport"], rec["context"]), ("context", "orch-h11"))
        self.assertEqual(rec["facts"]["cpus"], 8)
        self.assertIn("app1-web", [c["name"] for c in rec["facts"]["cotenants"]])
        self.assertTrue(fake.seen(["docker", "context", "create", "orch-h11"]))
        self.assertEqual(js["step"], "setup")
        self.assertTrue(any(x.startswith("h11 (deploy@192.0.2.11) ok: linux x86_64, 8 cpu") for x in lines), lines)


class TestRegistryFallback(unittest.TestCase):
    def test_token_dance_and_errors(self):
        http = FakeHttp({PY_URL: D3})
        self.assertEqual(oi._registry_digest(http, "python:3.12-slim"), D3)
        self.assertEqual(http.calls[1][1], "https://auth.example/token?service=reg.example&"
                                           "scope=repository%3Alibrary%2Fpython%3Apull")
        with self.assertRaisesRegex(oi._PinError, "could not be reached"):
            oi._registry_digest(http, "ghcr.io/owner/tool:1")

        def basic(method, url, headers, timeout):
            return 401, {"www-authenticate": 'Basic realm="reg"'}, b""
        with self.assertRaisesRegex(oi._PinError, "wants a login .*docker login reg.example"):
            oi._registry_digest(basic, "reg.example/team/app:2")

        def missing(method, url, headers, timeout):
            return 404, {}, b""
        with self.assertRaisesRegex(oi._PinError, "no such image or tag"):
            oi._registry_digest(missing, "reg.example/team/app:2")

        def broken(method, url, headers, timeout):
            import http.client
            raise http.client.BadStatusLine("garbage")
        with self.assertRaisesRegex(oi._PinError, "could not be reached .*BadStatusLine"):
            oi._registry_digest(broken, "reg.example/team/app:2")

    def test_ref_parts(self):
        self.assertEqual(oi._ref_parts("node:22"), ("docker.io", "library/node", "22", None))
        self.assertEqual(oi._ref_parts("mcr.microsoft.com/dotnet/sdk:8.0")[:3],
                         ("mcr.microsoft.com", "dotnet/sdk", "8.0"))
        self.assertEqual(oi._ref_parts("localhost:5000/app@" + D1), ("localhost:5000", "app", "latest", D1))
        for bad in ("Node:22", "node 22", "-x", "node:22;rm"):
            with self.assertRaises(ValueError):
                oi._ref_parts(bad)


# ----------------------------------------------------------------------------- review round 1 (init owner)
def new_repo(path, commit=False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    if commit:
        subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c",
                        "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "c1"], check=True,
                       capture_output=True)
    return path


def store_cfg(root) -> dict:
    return json.loads((Path(root) / ".git" / "orch" / "config.json").read_text(encoding="utf-8"))


def store_state(root) -> dict:
    return json.loads((Path(root) / ".git" / "orch" / "state" / "hosts.json").read_text(encoding="utf-8"))


class TestProductionElsewhere(Base):
    """R1: a machine another approved project deploys to is never an all-scope (image build) helper here."""

    def test_new_helper_that_is_production_elsewhere_is_helper_and_production(self):
        other = (self.d / "billing").as_posix()
        self.mprod = {("192.0.2.20", 2222): [other, self.root.as_posix()]}  # this project's own root never counts
        self.ok(ans={"helper_count": 1})
        _, js = self.ok(ans={"helpers": ["deploy@192.0.2.20:2222"]})
        h = self.cfg()["hosts"]["h20"]
        self.assertEqual(h["roles"], ["helper", "prod"])
        self.assertNotIn("jobs", h)  # the setup step still asks, now with production's default (council 2 MF6)
        self.assertIn(f"h20 (deploy@192.0.2.20:2222) is a production server of {other}, so this project saves it as "
                      f"helper + production: read-only commands only (production rules win; answer jobs tests to run "
                      f"lint + tests there)", js["warnings"])
        _, js = self.ok()  # init passes master_prod to status(): the setup line names the project that deploys there
        self.assertIn(f"h20 (deploy@192.0.2.20:2222): 8 cpu, 16 GB, no other containers, production server in {other} "
                      f"[helper + production; read-only commands only]", js["ask"]["question"])
        self.assertNotIn(self.root.as_posix(), js["ask"]["question"])  # never this project's own root
        self.assertEqual(js["ask"]["example"]["hosts"]["h20"], {"roles": ["helper", "prod"], "jobs": "readonly"})
        # "helper only; everything" cannot drop production or widen the scope
        rc, _, js = self.init(ans={"hosts": {"h20": {"roles": ["helper"], "jobs": "all"}}})
        self.assertEqual(rc, 2)
        self.assertIn(f"h20 (deploy@192.0.2.20:2222) is also a production server (of {other}), so it runs at most "
                      f"lint + tests", js["error"])
        _, js = self.ok(ans={"hosts": {"h20": {"roles": ["helper"], "jobs": "tests"}}, "production": "none"})
        self.assertEqual(self.cfg()["hosts"]["h20"]["roles"], ["helper", "prod"])
        self.assertIn(f"h20 (deploy@192.0.2.20:2222) is a production server of {other}, so it stays helper + "
                      f"production here (production rules win)", js["warnings"])
        node = oc.compile_engine(oc.find_project(str(self.root)), self.cfg(), self.state())["nodes"]["nodes"]["h20"]
        self.assertTrue(node["note"].startswith("roles helper + prod; jobs tests"), node["note"])

    def test_saved_all_scope_helper_is_clamped_once_another_project_deploys_there(self):
        self.reach_ready()  # h11 (app-builder) is a dedicated helper with every job
        other = (self.d / "billing").as_posix()
        self.mprod = {("192.0.2.11", 22): [other]}
        _, js = self.ok(ans={"hosts": {"h11": {"watch": "http://127.0.0.1:8080/app/"}}})
        h = self.cfg()["hosts"]["h11"]  # council 2 MF6: everything falls back to production's default, readonly
        self.assertEqual((h["roles"], h["jobs"]), (["helper", "prod"], "readonly"))
        self.assertTrue(any(f"production server of {other}" in w for w in js["warnings"]), js["warnings"])
        self.assertIn("h11 (app-builder) is also a production server now, so its jobs are read-only commands only (was "
                      "everything); answer jobs tests to run lint + tests there", js["warnings"])
        self.assertIn("approval: kept (no change loosens protection)", js["applied"])  # tighter, so no new approval
        _, js = self.ok(ans={"helpers": ["app-builder", "deploy@192.0.2.12"]})  # the helpers answer clamps too
        self.assertEqual(self.cfg()["hosts"]["h11"]["jobs"], "readonly")
        _, js = self.ok(ans={"hosts": {"h11": {"jobs": "tests"}}})  # an explicit tests answer is allowed and stays
        self.assertEqual(self.cfg()["hosts"]["h11"]["jobs"], "tests")
        self.ok(ans={"helpers": ["app-builder", "deploy@192.0.2.12"]})
        self.assertEqual(self.cfg()["hosts"]["h11"]["jobs"], "tests")

    @unittest.skipUnless(REAL_MASTER_PROD, "orch_config.master_prod (config owner) is not in this orch_config yet")
    def test_with_the_real_master_prod(self):
        self.mprod_patch.stop()
        self.reach_ready()  # this project deploys to h20 (deploy@192.0.2.20:2222)
        other = new_repo(self.d / "shop2")
        self.assertEqual(self.init(ans={"helper_count": 1}, project=other)[0], 0)
        rc, _, js = self.init(ans={"helpers": ["deploy@192.0.2.20:2222"]}, project=other)
        self.assertEqual(rc, 0, js)
        self.assertEqual(store_cfg(other)["hosts"]["h20"]["roles"], ["helper", "prod"])
        self.assertTrue(any("is a production server of" in w for w in js["warnings"]), js["warnings"])


class TestWorktrees(Base):
    """Q5: a linked worktree of the same repository is the same project (one store), never "another project" that
    deploys to the host, whichever worktree approved last. The real master_prod, a real git worktree."""

    def setUp(self):
        super().setUp()
        new_repo(self.root, commit=True)
        self.wt = self.d / "wt1"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-q", "-b", "wtb", str(self.wt)], check=True,
                       capture_output=True)

    def drop_production(self, where):
        """h12 is helper + production in the approved setup; the user makes it a helper only, with every job."""
        rc, _, js = self.init(ans={"hosts": {"h12": {"roles": ["helper"], "jobs": "all"}}}, project=where)
        self.assertEqual(rc, 0, js)
        self.assertEqual(self.cfg()["hosts"]["h12"], {"ssh": "deploy@192.0.2.12", "roles": ["helper"], "jobs": "all"})
        self.assertFalse([w for w in js["warnings"] if "production server of" in w], js["warnings"])
        self.assertEqual(js["state"], "unapproved")  # dropping production loosens: it waits for the user's yes
        self.assertEqual(js["applied"], ["hosts: h12 (deploy@192.0.2.12) helper only, everything incl. image builds"])

    def test_init_leaves_its_own_store_out(self):
        self.reach_ready()
        self.assertTrue(self.mprod_excl)
        self.assertEqual({oc._key(x) for x in self.mprod_excl}, {oc._key(self.store)})

    @unittest.skipUnless(REAL_MASTER_PROD, "orch_config.master_prod (config owner) is not in this orch_config yet")
    def test_approved_in_the_main_worktree_changed_in_a_linked_one(self):
        self.mprod_patch.stop()
        self.reach_ready()
        self.drop_production(self.wt)

    @unittest.skipUnless(REAL_MASTER_PROD, "orch_config.master_prod (config owner) is not in this orch_config yet")
    def test_approved_in_a_linked_worktree_changed_in_the_main_one(self):
        self.mprod_patch.stop()
        self.where = self.wt
        self.reach_ready()
        self.assertEqual(self.trust()["root"], self.wt.as_posix())
        self.where = None
        self.drop_production(self.root)


class TestForeignNamespace(Base):
    """Council 2 MF5: a helper holding another orch namespace's objects under orch's names (an older orch, another
    master) is never reported ready: a fix step with the exact fix; orch never removes them itself."""
    OBJS = [{"kind": "container", "name": "agentx-1030-job", "family": "io.other"},
            {"kind": "volume", "name": "agentx-cache-npm", "family": None}]
    WHAT = ("2 objects named agentx-* belong to another orch namespace (label io.other.*, none): an older orch or "
            "another master")
    FIX = ("run that orch's gc --session-end on this host (removing volumes needs the user's OK); or, if no other "
           "master uses this host, remove them by hand: docker rm -f agentx-1030-job; docker volume rm "
           "agentx-cache-npm")

    def test_new_helper_is_a_fix_step_until_they_are_gone(self):
        self.disc.foreign["deploy@192.0.2.12"] = self.OBJS
        self.ok(ans={"helper_count": 1})
        lines, js = self.ok(ans={"helpers": ["deploy@192.0.2.12"]})
        self.assertIn("h12 (deploy@192.0.2.12) problem: foreign-namespace (see problems)", lines)
        self.assertEqual((js["state"], js["step"], js["next"]), ("incomplete", "fix", "fix"))
        self.assertEqual(js["problems"], [{"host": "h12 (deploy@192.0.2.12)", "what": self.WHAT, "fix": self.FIX,
                                           "tried": od.ssh_cmdline("deploy@192.0.2.12",
                                                                   "docker ps -a --filter name=agentx-")}])
        self.assertIn(f"h12 (deploy@192.0.2.12): {self.WHAT} -> {self.FIX}\n", js["ask"]["question"])
        rec = self.state()["h12"]  # not ok, but Docker answers: the transport is kept for gc and stop-all
        self.assertEqual((rec["ok"], rec["problem"]["class"], rec["transport"], rec["context"]),
                         (False, "foreign-namespace", "context", "orch-h12"))
        self.assertEqual(self.run.gc, [])  # never removed automatically
        self.disc.foreign.clear()  # the user ran that orch's gc (or removed them by hand): retry
        _, js = self.ok()
        self.assertEqual((js["step"], js["problems"]), ("setup", []))
        self.assertTrue(self.state()["h12"]["ok"])

    def test_ready_setup_is_not_ready_and_the_helper_still_gets_its_cleanup(self):
        self.reach_ready()
        self.disc.foreign["app-builder"] = self.OBJS[:1]
        _, js = self.ok()
        self.assertEqual((js["state"], js["step"]), ("drift", "fix"))
        self.assertIn("h11 (app-builder): 1 object named agentx-* belongs to another orch namespace (label io.other.*): "
                      "an older orch or another master -> run that orch's gc --session-end on this host",
                      js["ask"]["question"])
        rc, _, js = self.init("approve")
        self.assertEqual(rc, 2)  # --approve needs every helper probed ok
        _, js = self.ok(ans={"remove_hosts": ["h11"]})  # Docker answered: orch's own objects there are cleaned first
        self.assertEqual([a[-1] for a, _ in self.run.gc], ["h11"])
        self.assertFalse([w for w in js["warnings"] if "did not answer the last probe" in w], js["warnings"])


class TestHelperCleanup(Base):
    """R5: a helper that leaves the setup (removed, or production only now) is cleaned first, or the change waits."""

    def test_removed_or_unhelped_helpers_are_cleaned_before_their_context_goes(self):
        self.reach_ready()
        _, js = self.ok(ans={"hosts": {"h11": {"roles": ["prod"]}}})
        (argv, env), = self.run.gc
        self.assertEqual(argv, ["gc", "--session-end", "--node", "h11"])
        self.assertEqual(env["ORCH_PROJECT"], self.root.as_posix())
        node = json.loads(env["ORCH_GC_NODE_JSON"])
        self.assertEqual(node["name"], "h11")
        self.assertEqual((node["node"]["ssh"], node["node"]["context"], node["node"]["transport"]),
                         ("app-builder", "orch-h11", "context"))
        self.assertIn("cleanup: h11 (app-builder): orch's builder, cache volumes and pulled base images removed",
                      js["applied"])
        self.assertEqual(self.disc.removed, ["h11"])
        self.assertEqual(self.cfg()["hosts"]["h11"]["roles"], ["prod"])
        self.ok(ans={"remove_hosts": ["h12"]})
        self.assertEqual([a[-1] for a, _ in self.run.gc], ["h11", "h12"])
        self.ok(ans={"remove_hosts": ["h20"]})  # production only: nothing of orch's runs there
        self.assertEqual(len(self.run.gc), 2)

    def test_unreachable_or_busy_helper_keeps_everything(self):
        self.reach_ready()
        cfg, state, trust = self.cfg(), self.state(), self.trust()
        for rc, words in ((5, "h11 (app-builder) could not be reached to remove orch's builder, cache volumes and "
                              "pulled base images before it stops being a helper (h11: NOT cleaned (ssh: connect"),
                          (75, "a job is still running on h11 (app-builder), so orch cannot clean it"),
                          (2, "the cleanup of h11 (app-builder) failed (orch gc --session-end --node h11: ")):
            self.run.gc_rc["h11"] = rc
            for ans in ({"remove_hosts": ["h11"]}, {"hosts": {"h11": {"roles": ["prod"]}}}):
                code, _, js = self.init(ans=ans)
                self.assertEqual(code, 2, js)
                self.assertIn(words, js["error"])
                self.assertEqual((self.cfg(), self.state(), self.trust()), (cfg, state, trust))
                self.assertEqual((self.disc.removed, js["applied"]), ([], []))
        self.run.gc_rc["h11"] = 5
        code, _, js = self.init(ans={"remove_hosts": ["h11"]})
        self.assertIn("fix ssh to it and send this answer again. If the machine is gone for good, run orch init "
                      "first: a helper that does not answer the probe is then dropped without the cleanup", js["error"])

    def test_helper_down_at_the_last_probe_is_dropped_with_what_to_clean_by_hand(self):
        self.reach_ready()
        self.disc.fail["app-builder"] = "ssh: connect to host 192.0.2.11 port 22: Connection timed out"
        self.ok()  # the re-probe fails: no transport now
        _, js = self.ok(ans={"remove_hosts": ["h11"]})
        self.assertEqual(self.run.gc, [])
        self.assertNotIn("h11", self.cfg()["hosts"])
        w = [x for x in js["warnings"] if "did not answer the last probe" in x]
        self.assertEqual(len(w), 1, js["warnings"])
        self.assertIn(f"{od.ssh_cmdline('app-builder', 'docker ps -a --filter label=io.orch.agent')} and docker "
                      f"volume ls --filter label=io.orch.agent", w[0])

    def test_never_approved_helpers_need_no_cleanup(self):
        self.ok(ans={"helper_count": 1})
        self.ok(ans={"helpers": ["app-builder"]})
        _, js = self.ok(ans={"remove_hosts": ["h11"]})
        self.assertEqual((self.run.gc, js["warnings"]), ([], []))

    def test_new_ssh_spec_and_production_only_in_one_answer(self):
        # Q1: the new spec drops the answer's copy of the probe record; the cleanup still uses the one before it
        self.reach_ready()
        _, js = self.ok(ans={"hosts": {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["prod"]}}})
        (argv, env), = self.run.gc
        self.assertEqual(argv[-1], "h11")
        self.assertEqual(json.loads(env["ORCH_GC_NODE_JSON"])["node"]["ssh"], "app-builder")  # what the probe reached
        self.assertIn("cleanup: h11 (app-builder): orch's builder, cache volumes and pulled base images removed",
                      js["applied"])
        self.assertFalse([w for w in js["warnings"] if "did not answer the last probe" in w], js["warnings"])
        self.assertEqual(self.cfg()["hosts"]["h11"], {"ssh": "deploy@192.0.2.11", "roles": ["prod"]})
        self.assertEqual(self.disc.removed, ["h11"])

    def test_cleanup_reason_comes_from_the_verdict_and_is_not_journaled_twice(self):
        # Q6: the child runs unjournaled (init's own step and difficulty record it); its reason is gc's verdict, never
        # orch's journal note or a progress line, and a reached host with objects left is not called unreachable
        self.reach_ready()
        self.run.gc_rc["h11"] = 5
        rc, _, js = self.init(ans={"remove_hosts": ["h11"]})
        self.assertEqual(rc, 2)
        (_, env), = self.run.gc
        self.assertEqual(env["ORCH_JOURNAL"], "0")
        self.assertIn("before it stops being a helper (h11: NOT cleaned (ssh: connect to host 192.0.2.11 port 22: "
                      "Connection timed out)); fix ssh to it", js["error"])
        self.assertNotIn("journal:", js["error"])
        self.run.gc_out["h11"] = ("h11: NOT clean, still there: volume agentx-cache-npm; run orch gc --session-end "
                                  "--node h11 again\n")
        rc, _, js = self.init(ans={"remove_hosts": ["h11"]})
        self.assertEqual(rc, 2)
        self.assertIn("orch reached h11 (app-builder) but could not remove everything it left there before it stops "
                      "being a helper (h11: NOT clean, still there: volume agentx-cache-npm); run orch gc "
                      "--session-end --node h11 yourself", js["error"])
        self.assertNotIn("could not be reached", js["error"])
        self.assertIn("h11", self.cfg()["hosts"])  # nothing changed

    def test_gc_why(self):
        note = "orch[09:00:00] gc --session-end --node h11\njournal: difficulty #7 recorded (orch)\n"
        for out, err, want in (
                ("removed 0 cache entries\nh11: NOT cleaned (ssh: timed out); run orch gc --session-end --node h11 "
                 "again when it is reachable\n", note, "h11: NOT cleaned (ssh: timed out)"),
                ('{"result": "usage", "exit": 2, "error": "ORCH_GC_NODE_JSON is not JSON"}\n', note,
                 "ORCH_GC_NODE_JSON is not JSON"),
                ("h11: live job(s) j-1 still run there\n{\"result\": \"ok\"}\n", "", "h11: live job(s) j-1 still run "
                                                                                   "there"),
                ("", note + "orch: ssh: not found\n", "orch: ssh: not found"), ("", note, "exit 5")):
            self.assertEqual(oi._gc_why(R(5, out, err)), want, out)


class TestSharedMachineContexts(Base):
    """R8: projects reaching one machine as different ssh users each keep a docker context of their own; a context
    another project still uses is never changed or removed."""

    def setUp(self):
        super().setUp()
        fixture = (ROOT / "tests" / "fixtures" / "discover-linux.txt").read_text(encoding="utf-8")
        self.fake, self.ctxs = self.run, {}

        def run(argv, **kw):
            argv = [str(a) for a in argv]
            if argv[:3] == ["ssh", "-v", "-G"]:
                user, _, host = argv[-1].rpartition("@")
                return R(0, f"user {user or 'deploy'}\nhostname {host}\nport 22\n", "")
            if argv[0] == "ssh" and argv[-1] == "sh -s":
                return R(0, fixture, "")
            if argv == ["docker", "--version"]:
                return R(0, "Docker version 27.3.1, build ce12230\n")
            if argv[:2] == ["docker", "context"]:
                self.fake.calls.append(argv)
                verb = argv[2]
                if verb == "ls":
                    return R(0, "".join(json.dumps({"Name": n, "DockerEndpoint": h}) + "\n" for n, h in self.ctxs.items()))
                name = argv[-1] if verb == "rm" else argv[3]
                if verb == "inspect":
                    if name not in self.ctxs:
                        return R(1, "", f'context "{name}": context not found')
                    return R(0, json.dumps([{"Name": name, "Endpoints": {"docker": {"Host": self.ctxs[name]}}}]))
                if verb in ("create", "update"):
                    self.ctxs[name] = argv[-1].split("=", 1)[1]
                elif verb == "rm":
                    self.ctxs.pop(name, None)
                return R(0, name + "\n")
            return self.fake(argv, **kw)
        self.run, self.disc = run, od

    def project(self, root, spec):
        self.assertEqual(self.init(ans={"helper_count": 1}, project=root)[0], 0)
        rc, _, js = self.init(ans={"helpers": [spec]}, project=root)
        self.assertEqual(rc, 0, js)
        p = oc.find_project(str(root))
        oc.trust_set(self.home, p, oc.load_config(p), {})  # approved (registered) on this master
        return store_state(root)["h30"]["context"]

    def verbs(self, since=0):
        return [c[2] for c in self.fake.seen(["docker", "context"])[since:] if c[2] in ("create", "update", "rm")]

    def test_two_users_two_contexts(self):
        alt = od.endpoint_context("h30", "ci@192.0.2.30")
        a, b, c = self.root, new_repo(self.d / "shop2"), new_repo(self.d / "shop3")
        self.assertEqual(self.project(a, "deploy@192.0.2.30"), "orch-h30")
        self.assertEqual(self.project(b, "ci@192.0.2.30"), alt)  # A's context is someone else's: never updated
        self.assertEqual(self.ctxs, {"orch-h30": "ssh://deploy@192.0.2.30", alt: "ssh://ci@192.0.2.30"})
        self.assertNotIn("update", self.verbs())
        n = len(self.fake.seen(["docker", "context"]))
        self.ok()  # A probes again: its context already points at its endpoint
        self.assertEqual((self.verbs(n), store_state(a)["h30"]["context"]), ([], "orch-h30"))
        self.assertEqual(self.project(c, "deploy@192.0.2.30"), "orch-h30")  # same endpoint: shared with A
        self.assertEqual(self.verbs(n), [])
        _, js = self.ok(ans={"remove_hosts": ["h30"]})  # A leaves; C still uses orch-h30
        self.assertIn("orch-h30", self.ctxs)
        self.assertEqual([x[0][-1] for x in self.fake.gc], ["h30"])  # A was approved with it: cleaned first
        rc, _, js = self.init(ans={"remove_hosts": ["h30"]}, project=b)  # B's own context goes
        self.assertEqual(rc, 0, js)
        self.assertEqual(self.ctxs, {"orch-h30": "ssh://deploy@192.0.2.30"})

    def test_own_context_follows_a_new_ssh_spec(self):
        self.project(self.root, "deploy@192.0.2.30")
        n = len(self.fake.seen(["docker", "context"]))
        _, js = self.ok(ans={"hosts": {"h30": {"ssh": "ops@192.0.2.30"}}})
        self.assertEqual(self.verbs(n), ["update"])  # orch-h30 was this project's alone: updated in place
        self.assertEqual(self.ctxs, {"orch-h30": "ssh://ops@192.0.2.30"})
        self.assertEqual(self.state()["h30"]["context"], "orch-h30")


class NamedDetect(FakeDetect):
    """Names the root job after project_name, else the scanned folder, as orch_detect does."""

    def __init__(self):
        super().__init__({})
        self.names, self.rename, self.cmd = [], None, "pytest -q"

    def detect(self, root, max_depth=3, project_name=None, run=None):
        self.calls += 1
        self.names.append(project_name)
        name = self.rename or project_name or Path(root).name
        return {"jobs": {f"{name}-unit": {"stage": "unit", "kind": "container", "image": "python:3.12-slim",
                                          "cmd": self.cmd, "memory": "2g", "timeout": 900,
                                          "detected": "python (pyproject.toml)", "files": ["pyproject.toml"],
                                          "manifest": M1}}, "notes": [], "unknown": []}


class TestStableJobNames(Base):
    """R11: job names do not depend on the worktree folder, and the same job under a new name is not drift."""

    def setUp(self):
        super().setUp()
        self.det = NamedDetect()
        new_repo(self.root, commit=True)

    def approve_one(self):
        self.ok(ans={"helper_count": 1})
        self.ok(ans={"helpers": ["app-builder"]})
        self.ok(ans={"hosts": {"h11": {"roles": ["helper"], "jobs": "all"}}, "production": "none", "deploy": "later",
                     "alerts": "none"})
        self.ok(ans={"jobs": "detected"})
        _, js = self.ok("approve")
        self.assertEqual(js["state"], "ready", js)

    def test_a_linked_worktree_sees_the_saved_jobs(self):
        self.approve_one()
        self.assertEqual(list(self.cfg()["jobs"]), ["shop-unit"])
        self.assertEqual(json.loads((self.store / "state" / "project.json").read_text(encoding="utf-8")),
                         {"name": "shop"})
        wt = self.d / "wt1"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-q", "-b", "wtb", str(wt)], check=True,
                       capture_output=True)
        rc, _, js = self.init("offline", project=wt)
        self.assertEqual((rc, js["state"], js["step"]), (0, "ready", "none"), js)
        self.assertEqual(self.det.names[-1], "shop")  # detection got the project's name, not wt1
        stored = json.loads((self.store / "state" / "jobs-detected.json").read_text(encoding="utf-8"))
        self.assertEqual(list(stored["jobs"]), ["shop-unit"])

    def test_default_name_is_the_main_worktree_folder(self):
        wt = self.d / "wt2"  # a linked worktree shares the main repo's store, so the same name
        self.assertEqual(oi._project_name(oc.Project(wt, self.root / ".git" / "orch", True)), "shop")
        self.assertEqual(oi._project_name(oc.Project(wt, self.d / "srv" / "shop9.git" / "orch", True)), "shop9")
        self.assertEqual(oi._project_name(oc.Project(self.d / "plain", self.d / "plain" / ".orch", False)), "plain")

    def test_same_job_under_a_new_name_is_no_drift(self):
        self.approve_one()
        self.det.rename = "billing"  # e.g. the project folder was renamed: detection says billing-unit
        _, js = self.ok("offline")
        self.assertEqual((js["state"], js["step"]), ("ready", "none"), js)
        lines, js = self.ok("detect")
        self.assertIn("compared with the saved jobs: no difference", lines)
        self.assertEqual(js["state"], "ready")
        self.det.cmd = "pytest -q -x"  # a real change still asks
        _, js = self.ok("offline")
        self.assertEqual((js["state"], js["step"]), ("drift", "jobs"), js)
        self.assertIn("shop-unit: no longer detected", js["ask"]["question"])


class TestAskLater(Base):
    """R15: a "later" deploy or production answer is asked again through the deploy gate and orch init --ask."""

    def ready_later(self, production):
        self.ok(ans={"helper_count": 1})
        self.ok(ans={"helpers": ["app-builder"]})
        self.ok(ans={"hosts": {"h11": {"roles": ["helper"], "jobs": "all"}}, "production": production,
                     "deploy": "later", "alerts": "none"})
        self.ok(ans={"jobs": "detected", "jobs_remove": ["e2e-test"]})
        _, js = self.ok("approve")
        self.assertEqual((js["state"], js["ask"]), ("ready", None))

    def gate(self):
        res = dp.check(start=str(self.root), env={"ORCH_HOME": str(self.home)}, run=self.run,
                       probe=lambda specs: {n: {"ok": True} for n in specs})
        return res, next(g for g in res["gates"] if g["name"] == "production answered")

    def test_deploy_later_from_gate_to_answer(self):
        self.ready_later(["deploy@192.0.2.20:2222"])
        res, g = self.gate()
        self.assertEqual((g["status"], res["ask"]["answer_key"]), ("fail", "deploy"))
        rc, lines, js = self.init(ask="deploy")
        self.assertEqual((rc, js["state"], js["step"], js["next"]), (0, "ready", "setup", "ask"))
        self.assertEqual(js["ask"], res["ask"])
        self.assertEqual(oi.details()["step"], "setup")
        _, js = self.ok(ans={"deploy": {"command": "./scripts/deploy.sh", "health": ["https://shop.example/health"]}})
        self.assertEqual(js["state"], "unapproved")  # a new deploy command needs the user's yes
        self.ok("approve")
        res, g = self.gate()
        self.assertEqual(g["status"], "ok", g)
        self.assertNotIn("ask", res)

    def test_production_later_and_errors(self):
        self.ready_later("later")
        res, g = self.gate()
        self.assertEqual(res["ask"]["answer_key"], "production")
        rc, _, js = self.init(ask="production")
        self.assertEqual(js["ask"]["answer_key"], "production")
        self.assertIn("or none / later [later]", js["ask"]["question"])
        _, js = self.ok(ans={"production": ["deploy@192.0.2.20:2222"]})
        res, g = self.gate()
        self.assertEqual(res["ask"]["answer_key"], "deploy")  # production now saved; the deploy command is next
        rc, _, js = self.init(ask="alerts")
        self.assertEqual(rc, 2)
        self.assertIn("--ask takes deploy or production", js["error"])
        rc, _, js = self.init(ask="deploy", project=new_repo(self.d / "fresh"))
        self.assertEqual(rc, 2)
        self.assertIn("no orch setup in", js["error"])
        self.assertEqual((js["state"], js["step"], js["ask"]["answer_key"]), ("new", "count", "helper_count"))  # Q8

    def test_ok_on_a_gate_ask_saves_nothing_new(self):
        # Q12: "ok" sends ask.example (init.md), so the example is the [bracketed] value: no made-up host or command
        self.ready_later("later")
        before = self.cfg()
        res, _ = self.gate()
        _, js = self.ok(ans=res["ask"]["example"])
        self.assertEqual((self.cfg(), js["state"]), (before, "ready"))
        self.ok(ans={"production": ["deploy@192.0.2.20:2222"]})
        before = self.cfg()
        res, _ = self.gate()
        self.assertEqual(res["ask"]["answer_key"], "deploy")
        _, js = self.ok(ans=res["ask"]["example"])
        self.assertEqual((self.cfg(), self.cfg()["deploy"]), (before, {"answer": "later"}))

    def test_live_without_a_health_url_asks_for_one(self):
        # Q7/Q13: plain orch init says ready and asks nothing once a deploy command is saved; orch live and
        # orch init --ask deploy carry the health URL question, and the answer keeps the command and the approval
        self.ok(ans={"helper_count": 1})
        self.ok(ans={"helpers": ["app-builder"]})
        self.ok(ans={"hosts": {"h11": {"roles": ["helper"], "jobs": "all"}}, "production": ["deploy@192.0.2.20:2222"],
                     "deploy": {"command": "./scripts/deploy.sh"}, "alerts": "none"})
        self.ok(ans={"jobs": "detected", "jobs_remove": ["e2e-test"]})
        self.ok("approve")
        _, js = self.ok("offline")
        self.assertEqual((js["state"], js["ask"]), ("ready", None))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.dict(os.environ, {"ORCH_HOME": str(self.home)}):
            code = dp.cmd_live(SimpleNamespace(project=str(self.root)))
        live = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual((code, live["next"], live["ask"]["answer_key"]), (2, "orch init --ask deploy", "deploy"))
        rc, _, js = self.init(ask="deploy")
        self.assertEqual((rc, js["step"], js["next"], js["ask"]), (0, "setup", "ask", live["ask"]))
        self.assertIn("Health URL(s) checked after a deploy", js["ask"]["question"])
        _, js = self.ok(ans={"deploy": {"health": ["https://shop.example/health"]}})
        self.assertEqual(self.cfg()["deploy"], {"command": "./scripts/deploy.sh",
                                                "health": ["https://shop.example/health"], "rollback": None})
        self.assertEqual(js["state"], "ready")  # a health URL loosens nothing: the approval stays


class TestHelperCount(Base):
    """R31: raising helper_count takes only the new helpers (or the whole list again)."""

    def test_only_the_new_helper(self):
        self.reach_ready()
        self.ok(ans={"helper_count": 3})
        rc, _, js = self.init(ans={"helpers": ["deploy@192.0.2.13", "app-builder"]})
        self.assertEqual(rc, 2)
        self.assertIn("helpers lists 2 ssh targets but helper_count is 3 and 2 helpers are saved already; give only "
                      "the 1 new one, or all 3, or change helper_count", js["error"])
        _, js = self.ok(ans={"helpers": ["deploy@192.0.2.13"]})
        self.assertIn("helpers: h13 (deploy@192.0.2.13) added", js["applied"])
        cfg = self.cfg()
        self.assertEqual(sorted(n for n, h in cfg["hosts"].items() if "helper" in h["roles"]), ["h11", "h12", "h13"])
        self.assertNotIn("pending", cfg)

    def test_one_more_after_a_merge(self):
        self.ok(ans={"helper_count": 2})
        _, js = self.ok(ans={"helpers": ["app-builder", "deploy@192.0.2.11"]})  # one machine typed twice
        self.assertTrue(any("1 different helper saved, 2 wanted: name 1 more" in w for w in js["warnings"]))
        _, js = self.ok(ans={"helpers": ["deploy@192.0.2.12"]})  # exactly what that warning asked for
        self.assertEqual(sorted(self.cfg()["hosts"]), ["h11", "h12"])
        self.assertNotIn("pending", self.cfg())


class TestSecrets(Base):
    """R38: credentials in a command or URL are refused (exit 2, naming the field) before anything is saved."""
    REFUSED = ["DEPLOY_PASSWORD=Hunter2xyzQ ./scripts/deploy.sh", "DB_PASSWORD=Hunter2xyzQ ./scripts/deploy.sh",
               "PGPASSWORD=Hunter2xyzQ psql -h db.example -c 'select 1'", 'PASSWORD="Hunter2xyzQ" ./scripts/deploy.sh',
               "./scripts/rollback.sh --password Hunter2xyzQ", "./scripts/deploy.sh --token Hunter2xyzQ",
               "./scripts/deploy.sh --api-token 'Hunter2xyzQ'", "./scripts/deploy.sh --password=Hunter2xyzQ",
               "curl -fsS -u ops:Hunter2xyzQ https://hooks.example/notify",
               "curl --user=ops:Hunter2xyzQ https://hooks.example/notify",
               "sshpass -p Hunter2xyzQ ssh deploy@192.0.2.20 ./deploy.sh",
               "curl https://ops:Hunter2xyzQ@hooks.example/notify", "curl -H 'X-Auth: Hunter2xyzQ' https://hooks.example/n",
               "curl -H 'Authorization: Bearer abcdefghijklmnop' https://hooks.example/n",
               # Q24: the credential spellings R38's check let through
               "DB_PASS=Hunter2xyzQ ./scripts/deploy.sh", "SMTP_PASS=Hunter2xyzQ ./scripts/notify.sh",
               "SECRET_KEY=Hunter2xyzQ ./scripts/deploy.sh", "DJANGO_SECRET_KEY=Hunter2xyzQ ./scripts/deploy.sh",
               "PRIVATE_KEY=Hunter2xyzQ ./scripts/deploy.sh", "STRIPE_KEY=Hunter2xyzQ ./scripts/deploy.sh",
               "DB_CREDENTIALS=Hunter2xyzQ ./scripts/deploy.sh", "./scripts/deploy.sh --db-pass=Hunter2xyzQ",
               "mysql -h db.example -u app -pHunter2xyzQ shop", "curl -uops:Hunter2xyzQ https://hooks.example/notify",
               "docker login -u ops -p Hunter2xyzQ registry.example", "redis-cli -h cache.example -a Hunter2xyzQ ping",
               "./scripts/deploy.sh --pw Hunter2xyzQ", "./scripts/deploy.sh --passphrase Hunter2xyzQ",
               "curl -H 'Authorization: Basic b3BzOkh1bnRlcjJ4eXpR' https://hooks.example/n",
               "curl -H 'Authorization: token Hunter2xyzQ' https://hooks.example/n",
               "curl -H 'X-Api-Key: Hunter2xyzQ' https://hooks.example/n",
               "curl -d '{\"password\": \"Hunter2xyzQ\"}' https://hooks.example/n",
               "curl -X POST -d @- https://hooks.slack.com/services/T0000/B0000/Hunter2xyzQ",
               "curl -X POST -d @- https://discord.com/api/webhooks/1234/Hunter2xyzQ",
               "curl -s https://api.telegram.org/bot123456:Hunter2xyzQ/sendMessage",
               "curl -X POST -d @- https://shop.example/hook?key=Hunter2xyzQ",
               # council 2 MF7: bare API keys, with no secret's name or flag in front
               "./scripts/notify.sh sk-proj-" + "Ab1_cD2-eF" * 9, "LLM=sk-ant-api03-" + "Zx9_Yw8-Vu" * 9 + " ./x",
               "./scripts/deploy.sh --model-key=sk-" + "A1b2C3d4E5" * 5,
               "curl https://maps.example/geo?ll=1,2&k=AIza" + "SyA1b2C3d4" * 3 + "E5f6G"]
    ALLOWED = ["./scripts/risk-assessment-for-the-quarterly-release.sh", "./scripts/desk-booking-weekly-report.sh",
               "./scripts/task-start.sh sk-short","./scripts/deploy.sh --token-file /run/secrets/deploy", "./scripts/deploy.sh --password-file /run/secrets/p",
               "PGPASSFILE=/run/secrets/pgpass psql -h db.example", "TOKEN_FILE=/run/secrets/t ./scripts/deploy.sh",
               "docker run --rm -u 1000:1000 app:1 ./migrate", "curl -u ops https://hooks.example/notify",
               './scripts/deploy.sh --token "$DEPLOY_TOKEN"', "PGPASSWORD=$PGPW psql -h db.example",
               "curl -u ops:$OPS_PW https://hooks.example/notify", "./scripts/deploy.sh --env production",
               # Q24 controls: the wider check keeps these legitimate forms
               "docker run -p 8080:80 app:1", "ssh -p 2222 deploy@192.0.2.20 ./deploy.sh",
               "mysql -p -h db.example shop",
               'mysql -p"$DBPW" -h db.example shop', "mysqldump -P3306 -h db.example shop",
               "KEY_PATH=/run/secrets/k ./scripts/deploy.sh", "SSH_KEY_FILE=/run/secrets/id ./scripts/deploy.sh",
               "./scripts/deploy.sh --key-file /run/secrets/k", "docker login -u ops --password-stdin registry.example",
               'redis-cli -a "$REDIS_PW" ping', "DB_PASS=$DB_PASS ./scripts/deploy.sh",
               "BUILD_ENV=production ./deploy.sh",
               "BYPASS_CACHE=always ./scripts/deploy.sh", "PASSENGER_ENV=production ./scripts/deploy.sh",
               "AUTH_MODE=oidc-login ./scripts/deploy.sh", "curl --user-agent=orch:1 https://shop.example/",
               "TOKEN_URL=https://auth.example/token ./scripts/deploy.sh",
               "curl https://shop.example/health?ver=20261030",
               "curl https://shop.example/health?code=200", "curl -X POST -d @- \"$SLACK_WEBHOOK\""]
    URLS_REFUSED = ["https://shop.example/health?code=Hunter2xyzQ", "https://shop.example/health?sig=Hunter2xyzQ",
                    "https://shop.example/health?key=Hunter2xyzQ", "https://shop.example/h?X-Amz-Signature=Hunter2xyzQ",
                    "https://shop.example/h?subscription-key=Hunter2xyzQ", "https://shop.example/h?token=Hunter2xyzQ"]
    URLS_ALLOWED = ["https://shop.example/health?ver=20261030", "https://shop.example/health?code=200",
                    "https://shop.example/health", "http://127.0.0.1:8080/app/?keyword=shoes"]

    def test_url_forms(self):
        for u in self.URLS_REFUSED:
            with self.assertRaises(oi.InitError, msg=u) as cm:
                oi._urls([u], "deploy.health")
            self.assertIn("holds a credential", str(cm.exception))
            self.assertNotIn("Hunter2xyzQ", str(cm.exception))
        self.assertEqual(oi._urls(list(self.URLS_ALLOWED), "deploy.health"), self.URLS_ALLOWED)

    def test_secret_names(self):
        for n in ("DB_PASS", "dbPass", "PGPASSWORD", "SECRET_KEY_BASE", "STRIPE_KEY", "apiKey", "X-Api-Key", "X-Auth",
                  "Private-Token", "client_secret", "DB_CREDENTIALS", "MYSQL_PWD"):
            self.assertTrue(oi._secret_name(n), n)
        for n in ("PGPASSFILE", "SSH_KEY_FILE", "KEY_PATH", "TOKEN_URL", "SECRETS_DIR", "SECRET_NAME", "BYPASS_CACHE",
                  "PASSENGER_ENV", "MONKEY", "AUTH_MODE", "BUILD_ENV", "Content-Type", "username"):
            self.assertFalse(oi._secret_name(n), n)

    def test_command_forms(self):
        for cmd in self.REFUSED:
            with self.assertRaises(oi.InitError, msg=cmd) as cm:
                oi._command(cmd, "deploy.command", optional=False)
            self.assertIn("deploy.command seems to hold a secret", str(cm.exception))
        for cmd in self.ALLOWED:
            self.assertEqual(oi._command(cmd, "alerts.command", optional=True), cmd, cmd)

    def test_apply_refuses_naming_the_field(self):
        self.ok(ans={"helper_count": 1})
        for ans, field in (({"deploy": {"command": "DEPLOY_PASSWORD=Hunter2xyzQ ./scripts/deploy.sh"}}, "deploy.command"),
                           ({"deploy": {"command": "./scripts/deploy.sh",
                                        "rollback": "./scripts/rollback.sh --password Hunter2xyzQ"}}, "deploy.rollback"),
                           ({"alerts": {"command": "curl -u ops:Hunter2xyzQ https://hooks.example/n"}},
                            "alerts.command")):
            rc, _, js = self.init(ans=ans)
            self.assertEqual(rc, 2)
            self.assertIn(f"{field} seems to hold a secret", js["error"])
        self.assertNotIn("Hunter2xyzQ", (self.store / "config.json").read_text(encoding="utf-8"))

    def test_approve_rechecks_a_hand_edit(self):
        self.reach_ready()
        for rollback in ("./scripts/rollback.sh --password Hunter2xyzQ", "DJANGO_SECRET_KEY=Hunter2xyzQ ./rb.sh"):
            cfg = self.cfg()
            cfg["deploy"]["rollback"] = rollback
            (self.store / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            rc, _, js = self.init("approve")
            self.assertEqual(rc, 2)
            self.assertIn("deploy.rollback in config.json seems to hold a secret", js["error"])
            for f in ("trust.json", "guard-view.json"):
                self.assertNotIn("Hunter2xyzQ", (self.home / f).read_text(encoding="utf-8"), f)

    def test_job_env_and_commands_use_the_same_rule(self):
        self.ok(ans={"helper_count": 1})
        for job in ({"image": "python:3.12", "cmd": "pytest", "env": {"STRIPE_KEY": "Hunter2xyzQ"}},
                    {"image": "python:3.12", "cmd": "DJANGO_SECRET_KEY=Hunter2xyzQ pytest"}):
            rc, _, js = self.init(ans={"jobs": {"x-unit": job}})
            self.assertEqual(rc, 2, job)
            self.assertNotIn("Hunter2xyzQ", js["error"])


class TestJournalDetails(Base):
    """R16/R34: orch_init.LAST (details()) carries state, step, applied, warnings, exit and the error."""

    def test_last_after_ok_and_refused_calls(self):
        self.ok(ans={"helper_count": 1})
        self.assertEqual(oi.details(), {"state": "incomplete", "step": "helpers", "applied": ["helper_count: 1 helper"],
                                        "warnings": [], "exit": 0, "answer_keys": ["helper_count"]})
        self.assertIs(oi.LAST, oi.LAST)
        rc, _, js = self.init("approve")
        self.assertEqual(rc, 2)
        d = oi.details()
        self.assertEqual((d["exit"], d["error"]), (2, js["error"]))
        self.assertEqual(d["error"], "no helper is saved yet: run orch init and answer its questions, then approve")
        self.assertNotIn("answer_keys", d)  # only --apply has answer keys

    def test_answer_keys_name_the_question_answered(self):
        """The journal's init --apply signature: the sorted top-level keys, also when the answer is refused; the
        setup wrapper counts as its inner keys; an answer that is not a JSON object has none."""
        rc, _, js = self.init(ans={"helper_count": 1, "colour": "blue"})
        self.assertEqual((rc, oi.details()["answer_keys"]), (2, ["colour", "helper_count"]))
        self.ok(ans={"setup": {"production": "none", "alerts": "none"}, "helper_count": 1})
        self.assertEqual(oi.details()["answer_keys"], ["alerts", "helper_count", "production"])
        for bad in ("{not json", "[1, 2]"):
            rc, _, js = self.init(ans=bad)
            self.assertEqual(rc, 2)
            self.assertNotIn("answer_keys", oi.details(), bad)


class TestRefusedAnswer(Base):
    """Q8/Q16: an answer refused before the setup is read (bad JSON, an unknown key, empty stdin, a bad setup wrapper)
    returns the CURRENT status (state, step, next and the pending ask, as orch init --offline shows them) next to the
    error, never a made-up incomplete/fix; LAST (the journal's details) records that real state; nothing is created."""
    BAD = ("{not json", {"helpers_count": 2}, "", {"setup": {"colour": 1}}, {"helper_count": 2, "health": ["x"]})

    def same_as_plain(self):
        _, want = self.ok("offline")
        for bad in self.BAD:
            rc, _, js = self.init(ans=bad)
            self.assertEqual(rc, 2, bad)
            self.assertTrue(js["error"], bad)
            self.assertEqual([js[k] for k in ("state", "step", "next", "ask")],
                             [want[k] for k in ("state", "step", "next", "ask")], bad)
            self.assertEqual((oi.details()["state"], oi.details()["step"], oi.details()["exit"]),
                             (want["state"], want["step"], 2), bad)
        return want

    def test_fresh_project_creates_nothing(self):
        want = self.same_as_plain()
        self.assertEqual((want["state"], want["step"], want["ask"]["answer_key"]), ("new", "count", "helper_count"))
        self.assertFalse(self.store.exists())

    def test_mid_interview(self):
        self.ok(ans={"helper_count": 2})
        self.assertEqual(self.same_as_plain()["step"], "helpers")

    def test_ready_setup(self):
        self.reach_ready()
        self.assertEqual(self.same_as_plain()["state"], "ready")

    def test_approve_without_a_setup(self):
        rc, _, js = self.init("approve")
        self.assertEqual((rc, js["state"], js["step"], js["ask"]["answer_key"]), (2, "new", "count", "helper_count"))
        self.assertIn("nothing to approve in", js["error"])
        self.assertFalse(self.store.exists())

    def test_broken_config_still_falls_back(self):
        self.ok(ans={"helper_count": 1})
        (self.store / "config.json").write_text("{broken", encoding="utf-8")
        rc, _, js = self.init(ans={"colour": 1})
        self.assertEqual((rc, js["step"], js["ask"]), (2, "fix", None))


class TestScriptHashes(Base):
    """Q11: approval hashes the deploy and rollback scripts however the command names them (Windows paths, interpreter
    options that take a value, inline code, an rc file), so the scripts-unchanged gate warns when one changes."""
    FORMS = {"./scripts/deploy.sh": ["scripts/deploy.sh"], "scripts\\deploy.sh": ["scripts/deploy.sh"],
             ".\\scripts\\deploy.ps1": ["scripts/deploy.ps1"],
             "powershell -File .\\scripts\\deploy.ps1": ["scripts/deploy.ps1"],
             "pwsh -NoProfile -File scripts\\deploy.ps1": ["scripts/deploy.ps1"],
             "pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/deploy.ps1": ["scripts/deploy.ps1"],
             "powershell.exe -ExecutionPolicy Bypass -File scripts/deploy.ps1": ["scripts/deploy.ps1"],
             'pwsh -NoProfile -Command "& ./scripts/deploy.ps1 -Env prod"': ["scripts/deploy.ps1"],
             "bash -o pipefail ./scripts/deploy.sh": ["scripts/deploy.sh"],
             "bash -e ./scripts/deploy.sh": ["scripts/deploy.sh"],
             "bash --rcfile ./scripts/env.sh ./scripts/deploy.sh": ["scripts/deploy.sh", "scripts/env.sh"],
             "DEPLOY_ENV=prod ./scripts/deploy.sh --tag v1": ["scripts/deploy.sh"],
             "ssh deploy@192.0.2.20 ./remote-deploy.sh": [], "make deploy": [], "./scripts": [],
             "../shop2/deploy.sh": [],
             "\\\\server.example\\share\\deploy.sh": [], "bash //server.example/share/deploy.sh": []}

    def setUp(self):
        super().setUp()
        for rel in ("scripts/deploy.ps1", "scripts/env.sh"):
            (self.root / rel).write_text("echo x\n", encoding="utf-8")
        (self.d / "shop2").mkdir()
        (self.d / "shop2" / "deploy.sh").write_text("echo outside\n", encoding="utf-8")

    def test_command_forms(self):
        p = oc.Project(self.root, self.store, True)
        for cmd, want in self.FORMS.items():
            self.assertEqual(sorted(oi._script_paths(p, {"deploy": {"command": cmd}})), want, cmd)
            self.assertEqual(sorted(oi._script_paths(p, {"deploy": {"command": "x", "rollback": cmd}})), want, cmd)
        cfg = {"deploy": {"command": "ssh deploy@192.0.2.20 ./remote-deploy.sh", "rollback": ".\\scripts\\deploy.ps1"}}
        self.assertEqual(oi._unhashed(p, cfg), ["deploy.command (./remote-deploy.sh)"])

    def test_approved_script_change_warns_at_the_gate(self):
        self.reach_ready()
        for cmd, rel in (("pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/deploy.ps1", "scripts/deploy.ps1"),
                         (".\\scripts\\deploy.ps1", "scripts/deploy.ps1"),
                         ("bash -o pipefail ./scripts/deploy.sh", "scripts/deploy.sh")):
            with self.subTest(cmd=cmd):
                self.ok(ans={"deploy": {"command": cmd}})
                _, js = self.ok("approve")
                self.assertTrue(js["applied"][-1].endswith(", 1 deploy script hashed"), js["applied"])
                self.assertEqual(list(self.trust()["scripts"]), [rel])
                f = self.root / rel
                old = f.read_bytes()
                f.write_text("echo changed\n", encoding="utf-8")
                res = dp.check(start=str(self.root), env={"ORCH_HOME": str(self.home)}, run=self.run,
                               probe=lambda specs: {n: {"ok": True} for n in specs})
                g = next(g for g in res["gates"] if g["name"] == "scripts unchanged")
                self.assertEqual(g["status"], "warn", g)
                self.assertTrue(g["detail"].startswith(f"{rel} changed since the approval"), g["detail"])
                f.write_bytes(old)

    def test_approve_warns_when_no_script_can_be_hashed(self):
        self.reach_ready()
        self.ok(ans={"deploy": {"command": "ssh deploy@192.0.2.20 ./remote-deploy.sh"}})
        _, js = self.ok("approve")
        self.assertIn(f"deploy.command (./remote-deploy.sh) names no file inside {self.root.as_posix()}, so the "
                      f"approval records no script content for it and orch deploy --check cannot warn when that "
                      f"script changes", js["warnings"])
        self.assertEqual(self.trust()["scripts"], {})


class TestJournalThroughOrch(unittest.TestCase):
    """R16/R34 end to end: the real orch.py, orch_init and orch_journal in a process, a temp project, nothing else."""

    def test_step_and_difficulty_records(self):
        d = tmpdir(self)
        root = new_repo(d / "shop")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        env.update(ORCH_HOME=str(d / "home"), DOCKER_CONFIG=str(d / "docker"), HOME=str(d), USERPROFILE=str(d))

        def orch(*args, stdin=None):
            return subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "orch.py"), *args], cwd=root,
                                  env=env, input=stdin, capture_output=True, text=True, timeout=120)
        apply = ("init", "--apply", "--offline", "--project", str(root))
        r = orch(*apply, stdin='{"helper_count": 1}')
        self.assertEqual(r.returncode, 0, r.stderr)
        r = orch(*apply, stdin='{"helper_count": "4+"}')  # refused: the same question, answered again below
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertRegex(r.stderr, r"journal: difficulty #\d+ recorded \(orch\)")
        r = orch(*apply, stdin='{"helper_count": 1}')
        self.assertEqual(r.returncode, 0, r.stderr)
        r = orch("init", "--approve", "--project", str(root))
        self.assertEqual(r.returncode, 2, r.stdout)
        path = root / ".git" / "orch" / "journal" / "journal.jsonl"
        recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        steps = [x for x in recs if x["type"] == "step"]
        self.assertEqual(len(steps), 4, recs)
        self.assertEqual(steps[0]["details"], {"state": "incomplete", "step": "helpers",
                                               "applied": ["helper_count: 1 helper"], "warnings": [], "exit": 0,
                                               "answer_keys": ["helper_count"]})
        err = "no helper is saved yet: run orch init and answer its questions, then approve"
        self.assertEqual((steps[3]["details"]["exit"], steps[3]["details"]["error"]), (2, err))
        diff = [x for x in recs if x["type"] == "difficulty"]
        self.assertEqual([d["step"] for d in diff], [steps[1]["seq"], steps[3]["seq"]])
        self.assertIn("helper_count must be a whole number", diff[0]["detail"])
        self.assertEqual(diff[-1]["detail"], err)
        # the good helper_count answer resolves the refused one (same answer keys); the refused approve stays open
        res = [x for x in recs if x["type"] == "resolution"]
        self.assertEqual([(x["for"], x["outcome"]) for x in res], [(diff[0]["seq"], "pass")])


if __name__ == "__main__":
    unittest.main()
