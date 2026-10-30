"""orch_deploy unit tests: every deploy gate against temp git repos with a local bare upstream, and orch live against a
local http.server thread. No ssh and no remote network: production reachability goes through a fake probe (or the
real orch_discover.probe_hosts with a fake ssh run=); git runs for real, isolated from the user's git config.
Run from the skill root:  python -B -m unittest discover -s tests -p "test_deploy.py"
"""
import base64
import contextlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import orch_config as oc  # noqa: E402
import orch_deploy as dp  # noqa: E402
import orch_discover as od  # noqa: E402

PIN = "@sha256:" + "a" * 64
IMG = "python:3.12" + PIN
GITCONFIG = "[user]\n\tname = t\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n"
TIMEOUT_20 = "ssh: connect to host 192.0.2.20 port 2222: Connection timed out"


def base_cfg(**over):
    cfg = {"schema": 1,
           "hosts": {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"], "jobs": "all"},
                     "h12": {"ssh": "app-host", "roles": ["helper", "prod"], "jobs": "tests"},
                     "h20": {"ssh": "deploy@192.0.2.20:2222", "roles": ["prod"]}},
           "production": {"answer": "set"},
           "deploy": {"command": "./deploy.sh", "rollback": "./rollback.sh", "health": ["https://shop.example/health"]},
           "alerts": {"command": None},
           "jobs": {"app-lint": {"stage": "lint", "image": IMG, "cmd": "ruff check ."},
                    "app-unit": {"stage": "unit", "image": IMG, "cmd": "pytest -q"},
                    "app-e2e": {"stage": "e2e", "image": IMG, "cmd": "pytest -q e2e"},
                    "app-image": {"kind": "build", "dockerfile": "Dockerfile"}},
           "policy": {}}
    cfg.update(over)
    return cfg


def git(cwd, *a):
    r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _rmtree(path):
    """Windows keeps git objects read-only; clear the flag and retry instead of leaving the temp tree behind."""
    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))


def gate(res, name):
    return next(g for g in res["gates"] if g["name"] == name)


class Base(unittest.TestCase):
    """Temp ORCH_HOME and git config (no user settings, no signing, no hooks); the production probe is faked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-deploy-")).resolve()
        self.addCleanup(_rmtree, self.tmp)
        (self.tmp / "gitconfig").write_text(GITCONFIG, encoding="utf-8")
        env = {"ORCH_HOME": str(self.tmp / "home"), "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CEILING_DIRECTORIES": str(self.tmp), "no_proxy": "*",
               "NO_PROXY": "*"}
        patch_env = mock.patch.dict(os.environ, env)
        patch_env.start()
        self.addCleanup(patch_env.stop)
        os.environ.pop("ORCH_PROJECT", None)
        self.probe_calls, self.probe_bad = [], {}
        patch_probe = mock.patch.object(dp, "probe_production", self._probe)
        patch_probe.start()
        self.addCleanup(patch_probe.stop)

    def _probe(self, specs):
        self.probe_calls.append(dict(specs))
        return {n: {"name": n, "ok": n not in self.probe_bad, "spec": s, "problem": self.probe_bad.get(n)}
                for n, s in specs.items()}

    def make_repo(self, name="shop", push=True, at=None):
        work, up = at or self.tmp / name, self.tmp / f"{Path(name).name}-up.git"
        git(self.tmp, "init", "-q", "--bare", str(up))
        git(self.tmp, "init", "-q", str(work))
        for rel, text in (("deploy.sh", "echo deploy\n"), ("app.py", "print(1)\n")):
            (work / rel).write_text(text, encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "c1")
        git(work, "remote", "add", "origin", str(up))
        if push:
            git(work, "push", "-q", "-u", "origin", "main")
        return work

    def project(self, root, cfg=None, approve=True, scripts=None):
        p, _ = oc.init_project(root)
        oc.save_config(p, base_cfg() if cfg is None else cfg)
        if approve:
            oc.trust_set(oc.home(), p, oc.load_config(p), scripts or {}, state_hosts={})
        return p

    def runs(self, p, sha, jobs=("app-lint", "app-unit"), result="pass", wt=False):
        for j in jobs:
            oc.append_run(p, {"job": j, "sha": sha, "wt": wt, "node": "h11", "result": result,
                              "exit": 0 if result == "pass" else 1, "wall_s": 1.0, "jobid": "j1",
                              "ts": "2026-10-30T09:00:00"})

    def commit(self, work, text, push=False):
        (work / "app.py").write_text(text, encoding="utf-8")
        git(work, "commit", "-q", "-am", text.strip())
        if push:
            git(work, "push", "-q")
        return git(work, "rev-parse", "HEAD")

    def ready(self, cfg=None, scripts=None):
        work = self.make_repo()
        p = self.project(work, cfg, scripts=scripts)
        sha = git(work, "rev-parse", "HEAD")
        self.runs(p, sha)
        return work, p, sha

    def deploy(self, root, ref=None, check=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dp.cmd_deploy(SimpleNamespace(check=check, ref=ref, project=str(root)))
        lines = out.getvalue().splitlines()
        return code, lines, json.loads(lines[-1]), err.getvalue()


class DeployGate(Base):
    def test_without_check_refuses_with_exit_2(self):
        work, _, _ = self.ready()
        code, lines, res, err = self.deploy(work, check=False)
        self.assertEqual(code, 2)
        self.assertEqual(res["error"], "orch does not deploy: run orch deploy --check, then the deploy command saved "
                                       "in the setup")
        self.assertIn(dp.NOT_DEPLOY, err)
        self.assertEqual((len(lines), self.probe_calls), (1, []))
        self.assertEqual(dp.details()["exit"], 2)

    def test_every_gate_ok_is_ready_exit_0(self):
        work, p, sha = self.ready()
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 0, lines)
        self.assertEqual([g["name"] for g in res["gates"]], list(dp.GATES))
        self.assertEqual([g["status"] for g in res["gates"]], ["ok"] * 7, lines)
        self.assertTrue(res["ready"])
        self.assertEqual(res["next"], "run the deploy command: ./deploy.sh, then orch live")
        self.assertEqual((res["ref"], res["sha"]), ("HEAD", sha))
        self.assertEqual(lines[:-1], ["ready: 7 ok (7 gates)"])  # Q19: no gate needs attention, so no gate line
        detail = [g["detail"] for g in res["gates"]]
        self.assertRegex(detail[0], r"^approved \d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual(detail[1], "h20 (deploy@192.0.2.20:2222), h12 (app-host); deploy command ./deploy.sh")
        self.assertEqual(detail[2], "nothing uncommitted or untracked")
        self.assertEqual(detail[3], f"{sha[:7]} is on origin/main (last fetched state; orch does not fetch)")
        self.assertEqual(detail[4], f"app-lint, app-unit passed for {sha[:7]} (runs without --wt)")
        self.assertEqual(detail[6], "h20 (deploy@192.0.2.20:2222), h12 (app-host) answered ssh")
        self.assertEqual(self.probe_calls, [{"h20": "deploy@192.0.2.20:2222", "h12": "app-host"}])
        for g in res["gates"]:
            self.assertEqual(set(g), {"name", "ok", "status", "detail", "fix"})
        self.assertNotIn("ask", res)
        d = dp.details()
        self.assertEqual((d["verb"], d["exit"], d["ready"], d["sha"]), ("deploy", 0, True, sha))
        self.assertEqual([g["name"] for g in d["gates"]], list(dp.GATES))
        self.assertFalse({"reason", "fix", "class"} & set(d), d)

    def test_never_approved_fails_first_gate_and_skips_ssh(self):
        work = self.make_repo()
        self.project(work, approve=False)
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "setup approved")
        self.assertEqual((g["status"], g["ok"], g["detail"]), ("fail", False, "this setup was never approved"))
        self.assertEqual(res["next"], dp.FIX_INIT)
        self.assertEqual(lines[0], "FAIL setup approved: this setup was never approved")  # its fix is next (JSON)
        self.assertEqual(gate(res, "scripts unchanged")["status"], "skip")
        self.assertEqual(gate(res, "production reachable")["detail"], "not checked: the setup is not approved")
        self.assertFalse([x for x in lines if x.startswith(("ok ", "skip "))], lines)  # Q19: only what needs work
        self.assertEqual(self.probe_calls, [])

    def test_setup_edited_after_approval_is_not_approved(self):
        work, p, _ = self.ready()
        cfg = oc.load_config(p)
        cfg["deploy"]["command"] = "./other.sh"
        oc.save_config(p, cfg)
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        self.assertIn("the setup changed after its approval on ", gate(res, "setup approved")["detail"])
        self.assertEqual(self.probe_calls, [])

    def test_production_later_or_none_fails(self):
        # no host with the prod role: "later" and "none" both leave nothing to deploy to
        helper_only = {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"], "jobs": "all"}}
        for ans, want in (("later", 'production is "later"'), ("none", "no host has the production role")):
            with self.subTest(production=ans):
                work = self.make_repo(name=f"shop-{ans}")
                p = self.project(work, base_cfg(production={"answer": ans}, hosts=helper_only))
                self.runs(p, git(work, "rev-parse", "HEAD"))
                code, _, res, _ = self.deploy(work)
                self.assertEqual(code, 7)
                g = gate(res, "production answered")
                self.assertEqual(g["status"], "fail")
                self.assertTrue(g["detail"].startswith(want), g["detail"])
                self.assertEqual(res["next"], dp.FIX_PROD)
                # the question init never asks again by itself comes with the gate (R15)
                ask = res["ask"]
                self.assertEqual((ask["answer_key"], ask["kind"], ask["header"]), ("production", "text", "Production"))
                self.assertIn("Production servers (what you type after ssh, comma separated), or none / later "
                              f"[{ans}]", ask["question"])
                self.assertEqual(ask["example"], {"production": ans})  # Q12: "ok" keeps [ans], never a made-up host
                self.assertEqual(dp.details()["ask"], ask)
        for words in ("orch init --ask production", "orch init --apply under the key production"):
            self.assertIn(words, dp.FIX_PROD)

    def test_helper_production_host_counts_as_production(self):
        # "none" answers the extra production-only servers question; a helper + production host is still a target
        hosts = {"h12": {"ssh": "app-host", "roles": ["helper", "prod"], "jobs": "tests"}}
        work = self.make_repo(name="shop-hp")
        p = self.project(work, base_cfg(production={"answer": "none"}, hosts=hosts))
        self.runs(p, git(work, "rev-parse", "HEAD"))
        code, _, res, _ = self.deploy(work)
        g = gate(res, "production answered")
        self.assertEqual(g["status"], "ok", g)
        self.assertIn("h12 (app-host)", g["detail"])

    def test_deploy_command_later_or_missing_fails(self):
        for dep in ({"answer": "later"}, {"command": None, "health": [], "rollback": None}):
            with self.subTest(deploy=dep):
                work = self.make_repo(name=f"shop-{len(dep)}")
                p = self.project(work, base_cfg(deploy=dep))
                self.runs(p, git(work, "rev-parse", "HEAD"))
                code, _, res, _ = self.deploy(work)
                self.assertEqual(code, 7)
                g = gate(res, "production answered")
                self.assertRegex(g["detail"], r"the deploy command is \"later\"|no deploy command is saved")
                self.assertEqual(res["next"], dp.FIX_CMD)
                ask = res["ask"]
                self.assertEqual(ask["answer_key"], "deploy")
                self.assertIn("Deploy command - runs on THIS machine and must reach production itself, e.g. "
                              "./deploy.sh [later]", ask["question"])
                self.assertIn("Rollback command (optional) [-]", ask["question"])
                self.assertEqual(ask["example"], {"deploy": "later"})  # Q12: "ok" keeps [later], never ./deploy.sh
        for words in ("orch init --ask deploy", "orch init --apply under the key deploy", "orch init --approve"):
            self.assertIn(words, dp.FIX_CMD)

    def test_ask_for_shows_the_saved_values(self):
        cfg = base_cfg(deploy={"command": None, "rollback": "./rollback.sh",
                               "health": ["https://shop.example/health",
                                          {"url": "https://198.51.100.5/h", "insecure": True}]})
        ask = dp.ask_for(cfg, "deploy")
        self.assertTrue(ask["question"].splitlines()[1].endswith("[-]"))  # saved without a command (a hand edit)
        self.assertIn("Health URL(s) checked after a deploy [https://shop.example/health, https://198.51.100.5/h",
                      ask["question"])
        self.assertIn("Rollback command (optional) [./rollback.sh]", ask["question"])
        self.assertEqual(ask["example"], {"deploy": {"health": cfg["deploy"]["health"], "rollback": "./rollback.sh"}})
        ask = dp.ask_for(cfg, "production")
        self.assertIn("[deploy@192.0.2.20:2222]", ask["question"])
        self.assertEqual(ask["example"], {"production": ["deploy@192.0.2.20:2222"]})
        with self.assertRaises(ValueError):
            dp.ask_for(cfg, "alerts")

    def test_ask_example_is_what_the_brackets_say(self):
        """Q12: a plain "ok" sends ask.example unchanged (references/init.md), so the example holds exactly the
        [bracketed] values: never a placeholder host or command the user did not give."""
        h = "https://shop.example/health"
        no_deploy = {k: v for k, v in base_cfg().items() if k != "deploy"}
        helper_only = {"h11": {"ssh": "deploy@192.0.2.11", "roles": ["helper"], "jobs": "all"}}
        cfgs = [base_cfg(deploy={"answer": "later"}), no_deploy, base_cfg(),
                base_cfg(deploy={"command": None, "health": [], "rollback": None}),
                base_cfg(deploy={"command": "./deploy.sh", "health": [h, {"url": h + "2", "insecure": True}],
                                 "rollback": None}),
                base_cfg(deploy={"command": "./deploy.sh", "health": [], "rollback": "./rollback.sh"}),
                base_cfg(hosts=helper_only, production={"answer": "later"}),
                base_cfg(hosts=helper_only, production={"answer": "none"}), base_cfg(hosts=helper_only)]
        seen = set()
        for cfg in cfgs:
            for key in ("deploy", "production"):
                ask = dp.ask_for(cfg, key)
                got = dict(re.findall(r"^(Deploy command|Health URL\(s\)|Rollback command|Production servers)"
                                      r".*\[([^\]]*)\]$", ask["question"], re.M))
                ex = ask["example"][key]
                with self.subTest(cfg=cfg.get(key), key=key, brackets=got):
                    self.assertTrue(got)
                    if key == "production":
                        word = got["Production servers"]
                        self.assertEqual(ex, word if word in ("none", "later") else word.split(", "))
                        continue
                    if ex == "later":
                        self.assertEqual(got, {"Deploy command": "later", "Health URL(s)": "-",
                                               "Rollback command": "-"})
                        seen.add("later")
                        continue
                    self.assertIsInstance(ex, dict)
                    cmd = got.get("Deploy command", "-")
                    self.assertEqual(ex.get("command") or "-", cmd)
                    self.assertEqual(", ".join(oc.health_label(x) for x in ex["health"]) or "-", got["Health URL(s)"])
                    if "Rollback command" in got:
                        self.assertEqual(ex.get("rollback") or "-", got["Rollback command"])
                    else:  # the health URL question alone: "ok" saves no URL and keeps the command
                        self.assertEqual(ex, {"health": []})
                        seen.add("health")
        self.assertEqual(seen, {"later", "health"})

    def test_no_health_url_warns_and_never_blocks(self):
        # Q13: the gap shows before the deploy, not first at orch live
        work = self.make_repo()
        p = self.project(work, base_cfg(deploy={"command": "./deploy.sh", "health": [], "rollback": None}))
        self.runs(p, git(work, "rev-parse", "HEAD"))
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 0, lines)
        g = gate(res, "production answered")
        self.assertEqual((g["status"], g["ok"], g["fix"]), ("warn", True, dp.FIX_HEALTH))
        self.assertTrue(g["detail"].endswith("deploy command ./deploy.sh; no health URL is saved, so orch live cannot "
                                             "check the app after the deploy"), g["detail"])
        self.assertIn("orch init --ask deploy", dp.FIX_HEALTH)
        self.assertNotIn("ask", res)  # a warning: the deploy may go ahead; orch live asks for the URL
        self.assertEqual(res["next"], "run the deploy command: ./deploy.sh, then orch live")

    def test_human_lines_name_only_what_needs_work(self):
        # Q19: one line per failing or warning gate and one summary line; the JSON keeps every gate and its fix
        work = self.make_repo(push=False)
        self.project(work, base_cfg(jobs={}), approve=False)
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        bad = [g for g in res["gates"] if g["status"] in ("fail", "warn")]
        self.assertEqual([g["name"] for g in bad], ["setup approved", "HEAD pushed", "required jobs"])
        self.assertEqual(lines[:-2], [dp.gate_line(g, fix=False) for g in bad])
        self.assertEqual(lines[-2], "NOT ready: 2 failed, 1 warning, 2 skipped, 2 ok (7 gates)")
        self.assertEqual([g["name"] for g in res["gates"]], list(dp.GATES))
        self.assertTrue(all(g["fix"] for g in bad))
        self.assertFalse([x for x in lines[:-1] if res["next"] in x or " | fix: " in x], lines)

    def test_gate_failure_details_name_the_gate_and_the_fix(self):
        # the journal's difficulty reads reason and fix (R18)
        work = self.make_repo()
        self.project(work, approve=False)
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        d = dp.details()
        self.assertEqual(d["reason"], "setup approved: this setup was never approved")
        self.assertEqual(d["fix"], res["next"])
        self.assertNotIn("class", d)  # exit 7 is already the deploy-gate class

    def test_project_option_wins_over_orch_project_in_messages(self):
        # R13: --project beats ORCH_PROJECT (as orch init does), also in what the gate names
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir()
        b.mkdir()
        env = {**os.environ, "ORCH_PROJECT": str(a)}
        self.assertEqual(dp._where(str(b), env), b.resolve().as_posix())
        self.assertEqual(dp._where(None, env), a.resolve().as_posix())
        res = dp.check(start=str(b), env=env)
        self.assertEqual(res["gates"][0]["detail"], f"no orch setup for {b.resolve().as_posix()}")

    def test_dirty_tree_counts_tracked_and_untracked_files(self):
        work, p, _ = self.ready()
        git(work, "config", "status.showUntrackedFiles", "no")  # a user setting must not hide untracked files
        (work / "notes.txt").write_text("x\n", encoding="utf-8")
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "tree clean")
        self.assertEqual(g["detail"], "1 uncommitted or untracked file: notes.txt")
        self.assertEqual(g["fix"], "commit or stash them (git status lists them), then run orch deploy --check again")
        self.assertEqual(res["next"], g["fix"])
        self.assertIn("FAIL tree clean: 1 uncommitted or untracked file: notes.txt", lines)
        (work / "app.py").write_text("print(2)\n", encoding="utf-8")
        _, _, res, _ = self.deploy(work)
        self.assertEqual(gate(res, "tree clean")["detail"], "2 uncommitted or untracked files: app.py, notes.txt")
        self.assertTrue(gate(res, "HEAD pushed")["ok"])  # the commit itself is still pushed

    def test_no_upstream_branch(self):
        work = self.make_repo(push=False)
        p = self.project(work)
        self.runs(p, git(work, "rev-parse", "HEAD"))
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "HEAD pushed")
        self.assertEqual(g["detail"], "no upstream branch: push first (git push -u origin main)")
        self.assertEqual((g["fix"], res["next"]), ("git push -u origin main", "git push -u origin main"))
        self.assertIn("FAIL HEAD pushed: no upstream branch: push first (git push -u origin main)", lines)
        git(work, "remote", "remove", "origin")
        _, _, res, _ = self.deploy(work)
        self.assertEqual(gate(res, "HEAD pushed")["detail"],
                         "no upstream branch: push first (there is no git remote yet: git remote add origin <url>, "
                         "then git push -u origin main)")

    def test_commits_not_pushed(self):
        work, p, _ = self.ready()
        sha2 = self.commit(work, "print(2)\n")
        self.runs(p, sha2)
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "HEAD pushed")
        self.assertEqual(g["detail"], "1 commit not pushed to origin/main (last fetched state; orch does not fetch)")
        self.assertEqual((g["fix"], res["next"]), ("git push", "git push"))
        self.assertTrue(gate(res, "required jobs")["ok"])
        git(work, "push", "-q")
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 0)
        self.assertEqual(res["sha"], sha2)

    def test_behind_upstream_still_counts_as_pushed(self):
        work, _, sha = self.ready()
        self.commit(work, "print(2)\n", push=True)
        git(work, "reset", "-q", "--hard", "HEAD~1")
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 0)
        self.assertEqual(gate(res, "HEAD pushed")["detail"], f"{sha[:7]} is on origin/main, which has 1 newer commit "
                                                            f"(last fetched state; orch does not fetch)")
        self.commit(work, "print(3)\n")  # diverged: one commit each side
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "HEAD pushed")
        self.assertEqual(g["detail"], "1 commit not pushed to origin/main, which also has 1 newer commit (last fetched "
                                      "state; orch does not fetch)")
        self.assertEqual(g["fix"], "git pull --rebase, then git push")

    def test_detached_head(self):
        work, p, sha = self.ready()
        git(work, "checkout", "-q", "--detach")
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 0)
        self.assertTrue(gate(res, "HEAD pushed")["detail"].startswith(f"{sha[:7]} is on origin/main"))
        sha2 = self.commit(work, "print(3)\n")
        self.runs(p, sha2)
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "HEAD pushed")
        self.assertTrue(g["detail"].startswith(f"HEAD is detached at {sha2[:7]} and that commit is on no remote "
                                               f"branch"), g["detail"])
        self.assertEqual(g["fix"], "push it from a branch: git switch -c <branch>, then git push -u origin <branch>")

    def test_required_jobs_need_a_pass_for_this_exact_commit_without_wt(self):
        work = self.make_repo()
        p = self.project(work)
        sha = git(work, "rev-parse", "HEAD")
        code, _, res, _ = self.deploy(work)
        g = gate(res, "required jobs")
        self.assertEqual(code, 7)
        self.assertEqual(g["detail"], f"app-lint: never ran (needed: a pass for {sha[:7]}); app-unit: never ran "
                                      f"(needed: a pass for {sha[:7]})")
        self.assertEqual((g["fix"], res["next"]), ("orch ci --stages lint,unit", "orch ci --stages lint,unit"))
        self.runs(p, sha, jobs=("app-unit",), result="test-fail")
        self.runs(p, sha, jobs=("app-lint",), wt=True)
        _, _, res, _ = self.deploy(work)
        g = gate(res, "required jobs")
        self.assertEqual(g["detail"], f"app-lint: only --wt runs for {sha[:7]}, which never count; app-unit: "
                                      f"test-fail for {sha[:7]}")
        self.assertNotIn("app-e2e", g["detail"])  # stage e2e is not in deploy_require
        self.assertNotIn("app-image", g["detail"])  # a build job is stage image
        self.runs(p, sha, jobs=("app-lint",))  # lint passes now; only the missing job is rerun
        _, _, res, _ = self.deploy(work)
        self.assertEqual(gate(res, "required jobs")["detail"], f"app-unit: test-fail for {sha[:7]}")
        self.assertEqual(res["next"], "orch ci --jobs app-unit")
        self.runs(p, sha, jobs=("app-unit",))
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 0)  # a pass for the sha counts even after an earlier failure

    def test_deploy_require_policy_and_a_pass_for_another_commit(self):
        work = self.make_repo()
        p = self.project(work, base_cfg(policy={"deploy_require": ["e2e"]}))
        sha = git(work, "rev-parse", "HEAD")
        self.runs(p, "f" * 40, jobs=("app-e2e",))
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "required jobs")
        self.assertEqual(g["detail"], f"app-e2e: no run for {sha[:7]} (the last run was pass for fffffff)")
        self.assertEqual(g["fix"], "orch ci --stages e2e")
        self.runs(p, sha, jobs=("app-e2e",))
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 0)
        self.assertEqual(gate(res, "required jobs")["detail"], f"app-e2e passed for {sha[:7]} (runs without --wt)")

    def test_unpinned_image_fix_names_approve(self):
        cfg = base_cfg()
        cfg["jobs"]["app-unit"]["image"] = "python:3.12"
        work = self.make_repo()
        p = self.project(work, cfg)
        self.runs(p, git(work, "rev-parse", "HEAD"), jobs=("app-lint",))
        _, _, res, _ = self.deploy(work)
        g = gate(res, "required jobs")
        self.assertEqual(g["detail"], "app-unit: cannot run until its image is pinned to a digest")
        self.assertEqual(g["fix"], "pin the job images with orch init --approve (after the user says yes), then "
                                   "orch ci --jobs app-unit")

    def test_no_required_job_warns_and_never_blocks(self):
        work = self.make_repo()
        self.project(work, base_cfg(jobs={}))
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 0, lines)
        g = gate(res, "required jobs")
        self.assertEqual((g["status"], g["ok"]), ("warn", True))
        self.assertEqual(lines[0], "warn required jobs: no saved job has a stage in lint, unit, so nothing was "
                                   "tested for this commit")
        self.assertTrue(g["fix"].startswith("orch init --detect"), g["fix"])
        self.assertEqual(lines[1:-1], ["ready: 1 warning, 6 ok (7 gates)"])

    def test_script_change_warns_and_never_blocks(self):
        work = self.make_repo()
        digest = dp.script_digest((work / "deploy.sh").read_bytes())
        p = self.project(work, scripts={"deploy.sh": digest})
        self.runs(p, git(work, "rev-parse", "HEAD"))
        _, _, res, _ = self.deploy(work)
        self.assertEqual(gate(res, "scripts unchanged")["detail"], "deploy.sh matches the approved content")
        (work / "deploy.sh").write_text("echo deploy v2\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "deploy v2")
        git(work, "push", "-q")
        self.runs(p, git(work, "rev-parse", "HEAD"))
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 0, lines)
        g = gate(res, "scripts unchanged")
        self.assertEqual((g["status"], g["ok"]), ("warn", True))
        self.assertRegex(g["detail"], r"^deploy\.sh changed since the approval on \d{4}-\d\d-\d\d \d\d:\d\d \(a "
                                      r"warning; it never blocks\)$")
        self.assertTrue(lines[0].startswith("warn scripts unchanged: deploy.sh changed since the approval on "))
        self.assertEqual(g["fix"], "review the change (git log -p -- deploy.sh); when it is intended, record it with "
                                   "orch init --approve (after the user says yes)")
        self.assertEqual(res["next"], "run the deploy command: ./deploy.sh, then orch live")
        git(work, "rm", "-q", "deploy.sh")
        git(work, "commit", "-q", "-m", "drop deploy.sh")
        git(work, "push", "-q")
        self.runs(p, git(work, "rev-parse", "HEAD"))
        code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 0)
        self.assertTrue(gate(res, "scripts unchanged")["detail"].startswith("deploy.sh is missing since the approval"))

    def test_unreachable_production_blocks_with_the_probe_fix(self):
        work, _, _ = self.ready()
        fix = "check that 192.0.2.20 is up and that port 2222 is reachable from this machine (VPN, firewall), then retry"
        self.probe_bad = {"h20": {"class": "network", "what": TIMEOUT_20, "fix": fix, "tried": "ssh ..."}}
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "production reachable")
        self.assertEqual(g["detail"], f"h20 (deploy@192.0.2.20:2222): {TIMEOUT_20}")
        self.assertEqual((g["fix"], res["next"]), (fix, fix))
        self.assertTrue(all(gate(res, n)["ok"] for n in dp.GATES[:6]))

    def test_reachability_through_discover_probe_hosts(self):
        """The default path: orch_discover.probe_hosts with docker off runs `ssh -G` and `ssh ... true`, never docker."""
        work, _, _ = self.ready()
        calls = []

        def fake_ssh(argv, **kw):
            argv = [str(x) for x in argv]
            calls.append(argv)
            if "-G" in argv:
                return SimpleNamespace(returncode=0, stdout=f"user deploy\nhostname {argv[-1].split('@')[-1]}\n"
                                                            f"port 22\n", stderr="")
            if argv[-1] == "true":
                if argv[-2] == "deploy@192.0.2.20":
                    return SimpleNamespace(returncode=255, stdout="", stderr=TIMEOUT_20 + "\n")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command {argv}")

        with mock.patch.object(dp, "probe_production", lambda specs: od.probe_hosts(specs, set(), run=fake_ssh)):
            code, _, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        g = gate(res, "production reachable")
        self.assertEqual(g["detail"], f"h20 (deploy@192.0.2.20:2222): {TIMEOUT_20}")
        self.assertIn("port 2222 is reachable from this machine", g["fix"])
        self.assertIn(od.SSH_BASE + ["-p", "2222", "deploy@192.0.2.20", "true"], calls)
        self.assertIn(od.SSH_BASE + ["app-host", "true"], calls)
        self.assertFalse([c for c in calls if any("docker" in x for x in c)])

    def test_no_setup_is_a_gate_failure(self):
        work = self.make_repo()
        code, lines, res, _ = self.deploy(work)
        self.assertEqual(code, 7)
        self.assertEqual(res["gates"][0]["detail"], f"no orch setup for {work.as_posix()}")
        self.assertEqual([g["status"] for g in res["gates"]], ["fail"] + ["skip"] * 6)
        self.assertEqual(res["next"], dp.FIX_INIT)
        self.assertEqual(self.probe_calls, [])

    def test_ref_option(self):
        work, p, sha1 = self.ready()
        self.commit(work, "print(2)\n")  # HEAD moves on, unpushed; sha1 is pushed and tested
        code, _, res, _ = self.deploy(work, ref=sha1)
        self.assertEqual(code, 0, res)
        self.assertEqual((res["ref"], res["sha"]), (sha1, sha1))
        self.assertTrue(gate(res, "HEAD pushed")["detail"].startswith(f"{sha1[:7]} is on origin/main"))
        code, _, res, _ = self.deploy(work, ref="main")
        self.assertEqual(code, 7)
        self.assertEqual(gate(res, "HEAD pushed")["fix"], "git push origin main")
        self.assertEqual(gate(res, "required jobs")["fix"], "orch ci --stages lint,unit --ref main")
        code, _, res, _ = self.deploy(work, ref="v9")
        self.assertEqual(code, 7)
        self.assertEqual(gate(res, "HEAD pushed")["detail"], "v9 is not a commit here")
        self.assertEqual(gate(res, "required jobs")["status"], "skip")
        code, lines, res, err = self.deploy(work, ref="--all")
        self.assertEqual((code, len(lines)), (2, 1))
        self.assertIn("bad ref '--all'", res["error"])

    def test_folder_of_repos_checks_the_job_repos(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        api = self.make_repo(name="api", at=ws / "api")
        cfg = base_cfg(jobs={"api-unit": {"stage": "unit", "repo": "api", "image": IMG, "cmd": "pytest -q"}})
        p = self.project(ws, cfg)
        self.assertFalse(p.git)
        sha = git(api, "rev-parse", "HEAD")
        self.runs(p, sha, jobs=("api-unit",))
        code, lines, res, _ = self.deploy(ws)
        self.assertEqual(code, 0, lines)
        self.assertEqual(res["sha"], sha)
        cfg["jobs"]["root-lint"] = {"stage": "lint", "image": IMG, "cmd": "ruff check ."}  # repo "." is not git here
        p = self.project(ws, cfg)
        code, _, res, _ = self.deploy(ws)
        self.assertEqual(code, 7)
        self.assertEqual(gate(res, "tree clean")["detail"], f"{ws.as_posix()} is not a git repository")
        self.assertTrue(gate(res, "HEAD pushed")["detail"].startswith(f"api: {sha[:7]} is on origin/main"))
        self.assertEqual(gate(res, "required jobs")["status"], "skip")
        self.assertEqual(res["shas"], {".": None, "api": sha})


class Units(unittest.TestCase):
    def test_gate_line(self):
        g = dp._gate
        self.assertEqual(dp.gate_line(g("setup approved", "ok", "approved 2026-10-30 09:20")),
                         "ok   setup approved: approved 2026-10-30 09:20")
        self.assertEqual(dp.gate_line(g("tree clean", "fail", "1 uncommitted or untracked file: a.txt", "git stash")),
                         "FAIL tree clean: 1 uncommitted or untracked file: a.txt | fix: git stash")
        self.assertEqual(dp.gate_line(g("HEAD pushed", "fail", "no upstream branch: push first (git push -u origin "
                                                               "main)", "git push -u origin main")),
                         "FAIL HEAD pushed: no upstream branch: push first (git push -u origin main)")
        self.assertEqual(dp.gate_line(g("production reachable", "skip", "not checked: x", "f")),
                         "skip production reachable: not checked: x")
        self.assertEqual(dp.gate_line(g("tree clean", "fail", "1 file: a.txt", "git stash"), fix=False),
                         "FAIL tree clean: 1 file: a.txt")
        res = {"ready": True, "gates": [g(n, "ok", "d") for n in dp.GATES]}
        self.assertEqual(dp.summary_line(res), "ready: 7 ok (7 gates)")
        res["gates"][4:6] = [g("required jobs", "warn", "d", "f"), g("scripts unchanged", "warn", "d", "f")]
        self.assertEqual(dp.summary_line(res), "ready: 2 warnings, 5 ok (7 gates)")
        self.assertEqual([g("x", s, "d")["ok"] for s in ("ok", "warn", "fail", "skip")], [True, True, False, False])

    def test_porcelain_skips_the_old_path_of_a_rename(self):
        self.assertEqual(dp._porcelain("R  new.txt\0old.txt\0 M a.py\0?? dir/\0"), ["new.txt", "a.py", "dir/"])
        self.assertEqual(dp._porcelain(""), [])

    def test_script_hash_forms(self):
        data = b"echo a\r\necho b\r\n"
        raw, lf = dp.script_digest(data), dp.script_digest(data.replace(b"\r\n", b"\n"))
        for want in (raw, lf, "sha256:" + raw, raw.upper()):
            self.assertTrue(dp._same(want, data), want)
        for want in ("0" * 64, None, ""):
            self.assertFalse(dp._same(want, data))

    def test_health_targets(self):
        cfg = {"deploy": {"command": "x", "health": ["https://shop.example/health", " ",
                                                     {"url": "https://198.51.100.7/up", "insecure": True},
                                                     {"url": "https://198.51.100.8/up", "insecure": "yes"}, 5]}}
        self.assertEqual(dp.health_targets(cfg), [{"url": "https://shop.example/health", "insecure": False},
                                                  {"url": "https://198.51.100.7/up", "insecure": True},
                                                  {"url": "https://198.51.100.8/up", "insecure": False}])
        self.assertEqual(dp.health_targets({"deploy": {"answer": "later"}}), [])
        self.assertEqual(dp.health_targets({}), [])

    def test_show_url_masks_credentials(self):
        self.assertEqual(dp.show_url("https://ops:s3cret@shop.example/health"), "https://***@shop.example/health")
        self.assertEqual(dp.show_url("https://token@shop.example/h?a=b@c"), "https://***@shop.example/h?a=b@c")
        self.assertEqual(dp.show_url("https://shop.example/h?a=b@c"), "https://shop.example/h?a=b@c")

    def test_failure_sentences(self):
        self.assertEqual(dp._why(ConnectionRefusedError(111, "Connection refused"), 10),
                         "connection refused: nothing accepts connections on that host and port")
        self.assertEqual(dp._why(socket.gaierror(-2, "Name or service not known"), 10),
                         "the host name does not resolve from this machine")
        self.assertEqual(dp._why(TimeoutError("timed out"), 10), "no answer within 10 s")
        e = ssl.SSLCertVerificationError(1, "certificate verify failed")
        e.verify_message = "self-signed certificate"
        self.assertTrue(dp._why(e, 10).startswith("certificate not trusted (self-signed certificate): "))
        self.assertEqual(dp._why(ValueError("bad"), 10), "bad")


# ----------------------------------------------------------------------------- live: a local HTTP server
class _Handler(http.server.BaseHTTPRequestHandler):
    CODES = {"/ok": 200, "/redirect": 302, "/fail": 500, "/slow": 200}

    def do_GET(self):
        s = self.server
        with s.lock:
            s.hits[self.path] = n = s.hits.get(self.path, 0) + 1
            s.auth.append(self.headers.get("Authorization"))
        if self.path == "/slow":
            s.stop.wait(5)
        code = (503 if n == 1 else 200) if self.path == "/flaky" else self.CODES.get(self.path, 404)
        try:
            self.send_response(code)
            if code == 302:
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except OSError:
            pass  # the client gave up (timeout test)

    def log_message(self, *a):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


def serve(tls=None):
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.hits, srv.auth, srv.lock, srv.stop = {}, [], threading.Lock(), threading.Event()
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


def stop(srv):
    srv.stop.set()
    srv.shutdown()
    srv.server_close()


def self_signed(d):
    """A throwaway self-signed certificate for 127.0.0.1 (openssl, when installed), or None."""
    exe = shutil.which("openssl")
    if not exe:
        return None
    cnf, crt, key = d / "req.cnf", d / "crt.pem", d / "key.pem"
    cnf.write_text("[req]\ndistinguished_name = dn\nprompt = no\nx509_extensions = ext\n[dn]\nCN = 127.0.0.1\n"
                   "[ext]\nsubjectAltName = IP:127.0.0.1\n", encoding="utf-8")
    try:
        r = subprocess.run([exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-days", "2", "-config", str(cnf), "-keyout", str(key), "-out", str(crt)],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return (str(crt), str(key)) if r.returncode == 0 and crt.is_file() and key.is_file() else None


class Live(unittest.TestCase):
    def setUp(self):
        patch_env = mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"})  # never through a proxy here
        patch_env.start()
        self.addCleanup(patch_env.stop)
        self.srv = serve()
        self.addCleanup(stop, self.srv)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.sleeps = []

    def get(self, path, **kw):
        kw.setdefault("sleep", self.sleeps.append)
        return dp.check_url(self.base + path, **kw)

    def test_200_is_up(self):
        c = self.get("/ok")
        self.assertEqual((c["status"], c["error"], c["tries"], c["insecure"]), (200, None, 1, False))
        self.assertEqual(set(c), {"url", "status", "ms", "tries", "error", "insecure"})
        self.assertEqual(self.sleeps, [])
        self.assertRegex(dp.live_line(c), r"^ok   http://127\.0\.0\.1:\d+/ok 200 \d+\.\d\ds$")

    def test_302_is_up_and_never_followed(self):
        c = self.get("/redirect")
        self.assertEqual((c["status"], c["error"]), (302, None))
        self.assertEqual(self.srv.hits, {"/redirect": 1})

    def test_500_fails_after_three_tries_2_then_4_seconds_apart(self):
        c = self.get("/fail")
        self.assertEqual((c["status"], c["tries"], c["error"]), (500, 3, "HTTP 500 Internal Server Error"))
        self.assertEqual(self.sleeps, [2, 4])
        self.assertEqual(self.srv.hits["/fail"], 3)
        self.assertRegex(dp.live_line(c), r"^FAIL http://127\.0\.0\.1:\d+/fail 500 \d+\.\d\ds \(3 tries\): HTTP 500 "
                                          r"Internal Server Error$")

    def test_recovers_on_a_later_try(self):
        c = self.get("/flaky")
        self.assertEqual((c["status"], c["tries"], c["error"]), (200, 2, None))
        self.assertEqual(self.sleeps, [2])

    def test_timeout_on_a_slow_answer(self):
        c = self.get("/slow", timeout=0.3, tries=2, backoff=(0,))
        self.assertEqual((c["status"], c["tries"], c["error"]), (None, 2, "no answer within 0.3 s"))
        self.assertLess(c["ms"], 3000)

    def test_credentials_go_in_a_header_and_are_masked(self):
        c = dp.check_url(self.base.replace("http://", "http://ops:s3cret@") + "/ok", sleep=self.sleeps.append)
        self.assertEqual((c["status"], c["error"]), (200, None))
        self.assertEqual(c["url"], self.base.replace("http://", "http://***@") + "/ok")
        self.assertEqual(self.srv.auth[-1], "Basic " + base64.b64encode(b"ops:s3cret").decode())

    def test_only_http_and_https(self):
        c = dp.check_url("ftp://198.51.100.7/health", tries=1)
        self.assertIsNone(c["status"])
        self.assertTrue(c["error"].startswith("not an http or https URL"))

    def test_live_checks_in_parallel_in_order(self):
        res = dp.live([{"url": self.base + "/fail"}, {"url": self.base + "/ok", "insecure": False}], tries=1)
        self.assertEqual([(c["status"], c["error"] is None) for c in res], [(500, False), (200, True)])
        self.assertEqual(dp.live([]), [])


class LiveTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="orch-tls-"))
        cls.pair = self_signed(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        _rmtree(cls.tmp)

    def test_certificate_verified_unless_insecure(self):
        if not self.pair:
            self.skipTest("openssl is not installed: this case needs a throwaway self-signed certificate")
        srv = serve(tls=self.pair)
        self.addCleanup(stop, srv)
        url = f"https://127.0.0.1:{srv.server_address[1]}/ok"
        with mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
            c = dp.check_url(url, tries=1)
            self.assertIsNone(c["status"])
            self.assertTrue(c["error"].startswith("certificate not trusted ("), c["error"])
            c = dp.check_url(url, insecure=True, tries=1)
        self.assertEqual((c["status"], c["error"], c["insecure"]), (200, None, True))
        self.assertTrue(dp.live_line(c).endswith(" (certificate not checked)"))


class LiveCommand(Base):
    def setUp(self):
        super().setUp()
        self.srv = serve()
        self.addCleanup(stop, self.srv)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        patch_backoff = mock.patch.object(dp, "LIVE_BACKOFF", (0, 0))
        patch_backoff.start()
        self.addCleanup(patch_backoff.stop)

    def live(self, root):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dp.cmd_live(SimpleNamespace(project=str(root)))
        lines = out.getvalue().splitlines()
        return code, lines, json.loads(lines[-1]), err.getvalue()

    def folder(self, deploy, name="site"):
        root = self.tmp / name
        root.mkdir()
        self.project(root, base_cfg(deploy=deploy), approve=False)  # live reads the saved URLs; no approval needed
        return root

    def test_exit_0_when_every_url_answers(self):
        root = self.folder({"command": "./deploy.sh", "rollback": None,
                            "health": [self.base + "/ok", self.base + "/redirect"]})
        code, lines, res, _ = self.live(root)
        self.assertEqual(code, 0, lines)
        self.assertTrue(res["ok"])
        self.assertEqual([c["status"] for c in res["checks"]], [200, 302])
        self.assertTrue(lines[0].startswith(f"ok   {self.base}/ok 200 "))
        self.assertEqual(res["next"], "check that the change is visible (read-only GET or SELECT), then hand over "
                                      "for UAT")
        d = dp.details()
        self.assertEqual((d["verb"], d["exit"], d["ok"], len(d["checks"])), ("live", 0, True, 2))

    def test_exit_1_when_any_url_fails(self):
        root = self.folder({"command": "./deploy.sh", "rollback": "./rollback.sh",
                            "health": [self.base + "/ok", self.base + "/fail"]})
        code, lines, res, _ = self.live(root)
        self.assertEqual(code, 1)
        self.assertFalse(res["ok"])
        self.assertTrue(lines[1].startswith(f"FAIL {self.base}/fail 500 "), lines[1])
        self.assertTrue(lines[1].endswith(" (3 tries): HTTP 500 Internal Server Error"), lines[1])
        self.assertEqual(res["next"], "tell the user; roll back only when they ask: ./rollback.sh")
        d = dp.details()
        self.assertEqual(d["exit"], 1)
        # R18: a production outage, not a test failure, with the failing URL and the verb's own next step
        self.assertEqual((d["class"], d["result"], d["fix"]), ("live-down", "down", res["next"]))
        self.assertEqual(d["reason"], f"{self.base}/fail: HTTP 500 Internal Server Error")

    def test_failed_live_is_journaled_as_live_down(self):
        import orch_journal as oj
        root = self.folder({"command": "./deploy.sh", "rollback": None, "health": [self.base + "/fail"]})
        code, _, res, _ = self.live(root)
        self.assertEqual(code, 1)
        p = oc.find_project(str(root))
        with mock.patch.dict(os.environ, {"ORCH_JOURNAL": "1"}):
            oj.record_invocation(p, "live", ["live"], 1.0, 2.0, code, dp.details())
        diff = [r for r in oj.read(p) if r.get("type") == "difficulty"]
        self.assertEqual(len(diff), 1, diff)
        self.assertEqual((diff[0]["class"], diff[0]["fix"]), ("live-down", res["next"]))
        self.assertIn("/fail: HTTP 500", diff[0]["detail"])
        step = next(r for r in oj.read(p) if r.get("type") == "step")
        self.assertEqual(step["result"], "down")

    def test_exit_2_without_health_urls_or_setup(self):
        # Q7/Q13: plain orch init says ready and asks nothing once a deploy command is saved, so live carries the
        # deploy question itself and points at orch init --ask deploy
        root = self.folder({"answer": "later"})
        code, lines, res, err = self.live(root)
        self.assertEqual((code, len(lines), res["ok"], res["checks"], res["next"]),
                         (2, 1, False, [], "orch init --ask deploy"))
        self.assertEqual(res["error"], f"no health URL is saved for {root.as_posix()}, so orch live has nothing to "
                                       f"check: relay the ask (orch init --ask deploy prints it again) and save the "
                                       f"answer with orch init --apply under the key deploy; a new deploy command "
                                       f"needs orch init --approve after the user says yes")
        self.assertIn(res["error"], err)
        self.assertEqual(res["ask"], dp.ask_for(oc.load_config(oc.find_project(str(root))), "deploy"))
        self.assertEqual((res["ask"]["answer_key"], res["ask"]["example"]), ("deploy", {"deploy": "later"}))
        d = dp.details()
        self.assertEqual((d["exit"], d["ask"], d["fix"]), (2, res["ask"], "orch init --ask deploy"))
        root = self.folder({"command": "./deploy.sh", "health": [], "rollback": None}, name="site2")
        code, _, res, _ = self.live(root)
        self.assertEqual((code, res["next"]), (2, "orch init --ask deploy"))
        self.assertTrue(res["error"].endswith("under the key deploy"), res["error"])  # the command stays: no approval
        self.assertEqual((res["ask"]["header"], res["ask"]["example"]), ("Health URL", {"deploy": {"health": []}}))
        self.assertEqual(res["ask"]["question"].splitlines()[1], "Health URL(s) checked after a deploy, comma "
                                                                 "separated [-]")
        empty = self.tmp / "empty"
        empty.mkdir()
        code, _, res, _ = self.live(empty)
        self.assertEqual((code, res["next"]), (2, "orch init"))
        self.assertEqual(res["error"], f"no orch setup for {empty.as_posix()}: run orch init")
        self.assertNotIn("ask", res)


if __name__ == "__main__":
    unittest.main()
