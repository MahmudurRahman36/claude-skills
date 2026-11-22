"""Release gate: attacks on the generated guard and scripts. Every test here must pass before a release.

Each test plays a hijacked or careless runner (for example, one that obeyed an injected ticket) and checks
that code stops it.

Run: python -m unittest discover -s skills/harness-builder/tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCAFFOLD = SKILL / "scripts" / "scaffold.py"
SLUG = "doc-screen"


def run(script, *args, stdin=None, env=None):
    p = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, input=stdin, env=env)
    return p.returncode, p.stdout + p.stderr


class HostileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name)
        run(SCAFFOLD, "--project", str(self.p), "--slug", SLUG)
        self.h = self.p / "harness" / SLUG
        self.inbox = self.p / "inbox"
        self.inbox.mkdir()
        for i in (1, 2, 3):
            (self.inbox / f"T-{i}.txt").write_text(f"ticket {i}: the app crashes on login", encoding="utf-8")
        cfg = {"fields": ["priority"], "verdicts": [{"name": "Suggested"}], "live_source": str(self.inbox)}
        (self.h / "context/rubric.md").write_text("x\n```json harness-config\n" + json.dumps(cfg) + "\n```\n", encoding="utf-8")
        self.env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.p))

    def tearDown(self):
        self.tmp.cleanup()

    # helpers
    def guard(self, tool, data, raw=None):
        event = raw if raw is not None else json.dumps({"tool_name": tool, "tool_input": data, "cwd": str(self.p)})
        return run(self.h / "scripts/guard.py", stdin=event, env=self.env)

    def ingest(self, *args):
        code, out = run(self.h / "scripts/ingest.py", *args, "--unguarded", env=self.env)
        return code, out

    def processed(self):
        f = self.h / "memory/processed.md"
        return f.read_text(encoding="utf-8") if f.is_file() else ""

    def default_run(self):
        code, out = self.ingest("--args", "[]", "--run", "auto")
        self.assertEqual(code, 0, out)
        return self.h / "runs" / json.loads(out)["run"]

    def score(self, run_dir, items):
        (run_dir / "scorecards.json").write_text(json.dumps({"items": items}), encoding="utf-8")

    def card(self, iid, verdict="Suggested"):
        return {"id": iid, "source_text": f"sources/{iid}.txt", "priority": "P2", "verdict": verdict,
                "claims": [{"field": "priority", "quote": "the app crashes on login"}], "red_flags": []}

    # 1. interpreter: only a bare python name
    def test_shebang_or_path_interpreter_is_blocked(self):
        script = "harness/doc-screen/scripts/ingest.py --args [] --run auto"
        for interp in ("harness/doc-screen/runs/python", "./python", "C:/x/python.exe", "/usr/bin/python3",
                       "runs\\python", "pythonx"):
            code, out = self.guard("Bash", {"command": f"{interp} {script}"})
            self.assertEqual(code, 2, (interp, out))
        self.assertEqual(self.guard("Bash", {"command": f"python {script}"})[0], 0)

    def test_unguarded_flag_is_blocked_inside_a_run(self):
        code, out = self.guard("Bash", {"command": "python harness/doc-screen/scripts/ingest.py --args [] --run auto --unguarded"})
        self.assertEqual(code, 2, out)

    # 2. fail closed
    def test_malformed_events_block(self):
        for raw in ("[]", "null", "", "not json", json.dumps({"tool_name": "Bash", "tool_input": []}),
                    json.dumps({"tool_name": "Bash", "tool_input": {"command": 42}}),
                    json.dumps({"tool_name": "Write", "tool_input": {"file_path": 7}}),
                    json.dumps({"tool_name": None, "tool_input": {}})):
            code, out = self.guard(None, None, raw=raw)
            self.assertEqual(code, 2, (raw, out))

    def test_launcher_blocks_when_no_python_is_found(self):
        sh = shutil.which("sh")
        if not sh:
            self.skipTest("sh not available")
        event = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "harness/doc-screen/HARNESS.md"}})
        launcher = str(self.h / "scripts/guard.sh")
        empty = tempfile.mkdtemp()
        try:
            p = subprocess.run([sh, launcher], input=event, capture_output=True, text=True,
                               env=dict(self.env, PATH=empty))
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("harness guard", p.stderr)
        finally:
            os.rmdir(empty)
        # with Python on PATH the launcher passes the guard's answer through
        p = subprocess.run([sh, launcher], input=event, capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        bad = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
        p = subprocess.run([sh, launcher], input=bad, capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 2, p.stderr)

    # 3. other tools
    def test_only_the_verifier_subagent_may_start(self):
        for tool in ("Agent", "Task"):
            self.assertEqual(self.guard(tool, {"subagent_type": "general-purpose", "prompt": "x"})[0], 2)
            self.assertEqual(self.guard(tool, {"prompt": "x"})[0], 2)
            self.assertEqual(self.guard(tool, {"subagent_type": f"{SLUG}-verifier", "prompt": "x"})[0], 0)

    def test_unknown_tools_are_blocked(self):
        for tool in ("WebFetch", "WebSearch", "mcp__gmail__send_email", "NewTool"):
            self.assertEqual(self.guard(tool, {})[0], 2, tool)

    def test_reads_stay_inside_the_harness(self):
        (self.p / ".env").write_text("SECRET=1", encoding="utf-8")
        self.assertEqual(self.guard("Read", {"file_path": "harness/doc-screen/context/rubric.md"})[0], 0)
        self.assertEqual(self.guard("Grep", {"pattern": "x", "path": "harness/doc-screen/runs"})[0], 0)
        # the search lands inside the harness through the pattern's literal start (seen in a live run)
        self.assertEqual(self.guard("Glob", {"pattern": "harness/doc-screen/context/*", "path": str(self.p)})[0], 0)
        self.assertEqual(self.guard("Glob", {"pattern": "harness/doc-screen/context/*"})[0], 0)
        self.assertEqual(self.guard("Grep", {"pattern": "x", "glob": "harness/doc-screen/**", "path": str(self.p)})[0], 0)
        self.assertEqual(self.guard("Grep", {"pattern": "x", "glob": "*.env", "path": str(self.p)})[0], 2)
        self.assertEqual(self.guard("Glob", {"pattern": "{harness/doc-screen/*,.env}"})[0], 2)
        for tool, data in [("Read", {"file_path": ".env"}), ("Read", {"file_path": str(Path.home() / ".ssh/id_rsa")}),
                           ("Read", {"file_path": "harness/doc-screen/../../.env"}), ("Grep", {"pattern": "SECRET"}),
                           ("Glob", {"pattern": "**/.env"}), ("Glob", {"pattern": "../**", "path": "harness/doc-screen"}),
                           ("Glob", {"pattern": "C:/Users/**", "path": "harness/doc-screen"})]:
            self.assertEqual(self.guard(tool, data)[0], 2, (tool, data))

    # 4. scripts confine their own paths (the guard checks only which script runs)
    def test_scripts_refuse_paths_outside_runs(self):
        outside = self.p / "src"
        for args in (["--args", "[]", "--run", str(outside)], ["--args", "[]", "--run", "harness/doc-screen/runs/../../../src"],
                     ["--mode", "default", "--source", str(self.p), "--run", "auto"],
                     ["--args", f"[from {self.p}]", "--run", "auto"]):
            code, out = self.ingest(*args)
            self.assertEqual(code, 2, (args, out))
        self.assertFalse(outside.exists())
        outside.mkdir()
        (outside / "scorecards.json").write_text('{"items": []}', encoding="utf-8")
        code, out = run(self.h / "scripts/render_report.py", "--run", str(outside), env=self.env)
        self.assertEqual(code, 2, out)
        self.assertFalse((outside / "report.md").exists())

    # 5. mark-processed integrity
    def test_forged_manifest_cannot_mark_items(self):
        fake = self.h / "runs/fake"
        (fake / "sources").mkdir(parents=True)
        items = []
        for i in (1, 2, 3):
            (fake / f"sources/T-{i}.txt").write_text(f"ticket {i}: the app crashes on login", encoding="utf-8")
            items.append({"id": f"T-{i}", "text": f"sources/T-{i}.txt"})
        (fake / "manifest.json").write_text(json.dumps({"mode": "default", "all": False, "items": items}), encoding="utf-8")
        self.score(fake, [self.card(f"T-{i}") for i in (1, 2, 3)])
        run(self.h / "scripts/render_report.py", "--run", str(fake), env=self.env)
        code, out = self.ingest("--mark-processed", "--run", str(fake))
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self.processed().count("| T-"), 0)

    def test_tampered_manifest_after_ingest_is_refused(self):
        r = self.default_run()
        man = json.loads((r / "manifest.json").read_text(encoding="utf-8"))
        man["items"].append({"id": "T-9", "text": "sources/T-1.txt"})
        (r / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        self.score(r, [self.card("T-1")])
        run(self.h / "scripts/render_report.py", "--run", str(r), env=self.env)
        code, out = self.ingest("--mark-processed", "--run", str(r))
        self.assertNotEqual(code, 0, out)

    def test_marking_needs_a_rendered_report_and_passing_items(self):
        r = self.default_run()
        bad = self.card("T-2")
        bad["claims"][0]["quote"] = "invented evidence"
        self.score(r, [self.card("T-1"), bad, self.card("T-3", verdict="Needs a human look")])
        code, out = self.ingest("--mark-processed", "--run", str(r))
        self.assertNotEqual(code, 0, out)                       # no report yet
        run(self.h / "scripts/render_report.py", "--run", str(r), env=self.env)
        self.score(r, [self.card("T-1"), self.card("T-2"), self.card("T-3")])  # edited after rendering
        code, out = self.ingest("--mark-processed", "--run", str(r))
        self.assertNotEqual(code, 0, out)
        self.score(r, [self.card("T-1"), bad, self.card("T-3", verdict="Needs a human look")])
        run(self.h / "scripts/render_report.py", "--run", str(r), env=self.env)
        code, out = self.ingest("--mark-processed", "--run", str(r))
        self.assertEqual(code, 0, out)
        res = json.loads(out)
        self.assertEqual(res["marked"], ["T-1"])
        self.assertEqual(res["pending_human"], ["T-3"])
        self.assertEqual(res["not_marked"]["failed_checks"], ["T-2"])
        self.assertIn("| T-3", (self.h / "memory/pending.md").read_text(encoding="utf-8"))
        # next default run: T-1 done, T-3 waits for a human, T-2 comes back
        code, out = self.ingest("--args", "[]", "--run", "auto")
        self.assertEqual([i["id"] for i in json.loads(out)["items"]], ["T-2"])

    def test_items_without_a_scorecard_stay_unmarked(self):
        r = self.default_run()
        self.score(r, [self.card("T-1")])
        run(self.h / "scripts/render_report.py", "--run", str(r), env=self.env)
        code, out = self.ingest("--mark-processed", "--run", str(r))
        res = json.loads(out)
        self.assertEqual(res["marked"], ["T-1"])
        self.assertEqual(res["not_marked"]["no_scorecard"], ["T-2", "T-3"])

    # 6. the code canary: ingest refuses to start when the guard hook never ran
    def test_ingest_refuses_without_an_active_guard(self):
        ingest = self.h / "scripts/ingest.py"
        code, out = run(ingest, "--args", "[]", "--run", "auto", env=self.env)
        self.assertEqual(code, 2, out)
        self.assertIn("guard", out)
        cmd = "python harness/doc-screen/scripts/ingest.py --args [] --run auto"
        self.assertEqual(self.guard("Bash", {"command": cmd})[0], 0)   # the hook fires and leaves a fresh stamp
        code, out = run(ingest, "--args", "[]", "--run", "auto", env=self.env)
        self.assertEqual(code, 0, out)
        stamp = self.h / "memory/.guard-ok"
        old = time.time() - 3600
        os.utime(stamp, (old, old))
        code, out = run(ingest, "--args", "[]", "--run", "auto", env=self.env)
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
