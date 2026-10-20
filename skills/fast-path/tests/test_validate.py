"""Negative fixtures exercise validation without modifying the source package."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("validator", ROOT / "scripts/validate.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class PackageValidation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "skill"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns("__pycache__"))

    def edit(self, rel, old, new):
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_source_package_passes(self):
        self.assertEqual(validator.validate(ROOT), [])

    def test_wrong_name(self):
        self.edit("SKILL.md", "name: fast-path", "name: fastpath")
        self.assertIn("Unexpected skill name", validator.validate(self.root))

    def test_line_budget(self):
        self.edit("SKILL.md", "## Boundary", "\n" * 200 + "## Boundary")
        self.assertTrue(any("lines" in e for e in validator.validate(self.root)))

    def test_byte_budget(self):
        self.edit("SKILL.md", "## Boundary", "## Boundary\n" + "y" * 2000)
        self.assertTrue(any("bytes" in e for e in validator.validate(self.root)))

    def test_missing_section(self):
        self.edit("SKILL.md", "## Exit", "## Leave")
        self.assertIn("Missing section: Exit", validator.validate(self.root))

    def test_telemetry_format_locked(self):
        self.edit("SKILL.md", "fallbacks F", "fallback F")
        self.assertIn("Telemetry format missing or changed", validator.validate(self.root))

    def test_broken_link(self):
        self.edit("SKILL.md", "(references/shortcuts.md)", "(references/nope.md)")
        self.assertTrue(any("Invalid local link" in e for e in validator.validate(self.root)))

    def test_missing_companion(self):
        (self.root / "references/subagent-brief.md").unlink()
        errors = validator.validate(self.root)
        self.assertIn("Missing companion: references/subagent-brief.md", errors)

    def test_scenario_count(self):
        self.edit("tests/scenarios.md", "## 7. ", "### 7. ")
        self.assertIn("Expected 7 scenarios, found 6", validator.validate(self.root))

    def test_manifest_must_reference_skill(self):
        self.edit("agents/openai.yaml", "$fast-path", "$other")
        self.assertTrue(any("Codex manifest" in e for e in validator.validate(self.root)))

    def test_manifest_short_description_length(self):
        self.edit("agents/openai.yaml", "Fast, low-token DAG execution with verified shortcuts",
                  "Fast, low-token DAG execution with verified shortcuts, batches, gates and more")
        self.assertTrue(any("25-64" in e for e in validator.validate(self.root)))


class FixtureBuild(unittest.TestCase):
    def test_fixture_builds_and_baseline_suite_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "inv"
            subprocess.run([sys.executable, str(ROOT / "tests/make_fixture.py"), str(target)],
                           check=True, capture_output=True)
            run = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=target,
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            sites = sum(p.read_text(encoding="utf-8").count("render_table(")
                        for p in target.rglob("*.py"))
            self.assertEqual(sites, 21)  # 20 files; test_fmt.py has 2 (call + method name)
            self.assertTrue((target / "data/app.db").is_file())


if __name__ == "__main__":
    unittest.main()
