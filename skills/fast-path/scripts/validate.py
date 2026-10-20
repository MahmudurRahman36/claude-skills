"""Validate locally; requires PyYAML, never installs dependencies automatically."""
import re
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
MAX_LINES = 150
MAX_BYTES = 17000
SECTIONS = [
    "Core principle", "Triage", "Fan-out threshold", "DAG", "Parallel", "Shortcuts", "Verify",
    "Anti-patterns", "Tokens", "Levels", "Exit", "Exceptions", "Boundary", "Hand-offs",
]
COMPANIONS = [
    "agents/openai.yaml", "references/dag-template.md", "references/shortcuts.md",
    "references/subagent-brief.md", "tests/scenarios.md", "tests/make_fixture.py",
]
TELEMETRY = "nodes N | batches B | shortcuts S (eq ✓/✗) | fallbacks F | verify ✓/✗"


def validate(root=ROOT):
    errors = []
    entry = root / "SKILL.md"
    if not entry.is_file():
        return ["Missing SKILL.md"]
    text = entry.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not match:
        errors.append("Missing frontmatter")
    else:
        try:
            fields = yaml.safe_load(match[1])
            if not isinstance(fields, dict):
                raise ValueError("Expected mapping")
        except (yaml.YAMLError, ValueError) as exc:
            errors.append(f"Invalid frontmatter YAML: {exc}")
            fields = {}
        if fields.get("name") != "fast-path":
            errors.append("Unexpected skill name")
        description = fields.get("description", "")
        if not isinstance(description, str) or not 1 <= len(description) <= 1024:
            errors.append("Description missing or too long")
    if len(text.splitlines()) > MAX_LINES:
        errors.append(f"SKILL.md exceeds {MAX_LINES} lines")
    if len(text.encode("utf-8")) > MAX_BYTES:
        errors.append(f"SKILL.md exceeds {MAX_BYTES} bytes")
    headings = re.findall(r"^## (.+)$", text, re.M)
    for section in SECTIONS:
        if not any(h.startswith(section) for h in headings):
            errors.append(f"Missing section: {section}")
    if TELEMETRY not in text:
        errors.append("Telemetry format missing or changed")
    for path in root.rglob("*"):
        if path.suffix not in {".md", ".yaml", ".py"} or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        if re.search(r"^(?:<{7}|={7}|>{7})(?: |$)", content, re.M):
            errors.append(f"Merge marker: {path.relative_to(root)}")
        if path.suffix != ".md":
            continue
        for link in re.findall(r"\]\(([^)]+)\)", content):
            if "://" in link or link.startswith("#"):
                continue
            target = (path.parent / link.split("#")[0]).resolve()
            if not target.is_relative_to(root.resolve()) or not target.exists():
                errors.append(f"Invalid local link: {path.relative_to(root)}: {link}")
    for name in COMPANIONS:
        if not (root / name).is_file():
            errors.append(f"Missing companion: {name}")
        elif not (root / name).read_text(encoding="utf-8").strip():
            errors.append(f"Empty companion: {name}")
    scenarios = root / "tests/scenarios.md"
    if scenarios.is_file():
        count = len(re.findall(r"^## \d+\. ", scenarios.read_text(encoding="utf-8"), re.M))
        if count != 7:
            errors.append(f"Expected 7 scenarios, found {count}")
    manifest = root / "agents/openai.yaml"
    if manifest.exists():
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            interface = data["interface"]
            for field in ("display_name", "short_description", "default_prompt"):
                if not isinstance(interface[field], str) or not interface[field].strip():
                    raise ValueError(f"Invalid interface field: {field}")
            if not 25 <= len(interface["short_description"]) <= 64:
                raise ValueError("short_description must be 25-64 characters")
            if "$fast-path" not in interface["default_prompt"]:
                raise ValueError("Default prompt must reference the skill")
            if not isinstance(data["policy"]["allow_implicit_invocation"], bool):
                raise ValueError("Implicit invocation policy must be boolean")
        except (yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"Invalid Codex manifest: {exc}")
    return errors


if __name__ == "__main__":
    findings = validate()
    for finding in findings:
        print(f"FAIL: {finding}")
    print(f"{'FAIL' if findings else 'PASS'}: package structure, sections, links, line budget")
    raise SystemExit(bool(findings))
