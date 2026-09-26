"""Negative fixtures exercise validation without modifying the source package."""
import importlib.util
from pathlib import Path
import shutil
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
        shutil.copytree(ROOT, self.root)

    def alter(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def test_valid_package(self):
        self.assertEqual(validator.validate(self.root), [])

    def test_missing_entry(self):
        (self.root / "SKILL.md").unlink()
        self.assertTrue(validator.validate(self.root))

    def test_bad_frontmatter(self):
        self.alter("SKILL.md", "---\nname: [broken\n---\nBody")
        self.assertTrue(validator.validate(self.root))

    def test_bad_manifest(self):
        self.alter("agents/openai.yaml", "interface: [broken # $personal-dev-skill")
        self.assertTrue(validator.validate(self.root))

    def test_manifest_wrong_types(self):
        self.alter("agents/openai.yaml", "interface: null")
        self.assertTrue(validator.validate(self.root))

    def test_empty_companion(self):
        self.alter("references/delivery.md", " ")
        self.assertTrue(validator.validate(self.root))

    def test_missing_companion(self):
        (self.root / "references/delivery.md").unlink()
        self.assertTrue(validator.validate(self.root))

    def test_bad_link(self):
        self.alter("README.md", "[missing](does-not-exist.md)")
        self.assertTrue(validator.validate(self.root))

    def test_escaping_link(self):
        self.alter("README.md", "[outside](../../outside.md)")
        self.assertTrue(validator.validate(self.root))

    def test_conflict_marker(self):
        self.alter("README.md", "<<<<<<< branch\nconflict")
        self.assertTrue(validator.validate(self.root))

    def test_core_budget(self):
        entry = self.root / "SKILL.md"
        self.alter("SKILL.md", entry.read_text(encoding="utf-8") + " word" * 1001)
        self.assertTrue(validator.validate(self.root))


if __name__ == "__main__":
    unittest.main()
