"""Validate locally; requires PyYAML, never installs dependencies automatically."""
import re
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


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
        if fields.get("name") != "personal-dev-skill":
            errors.append("Unexpected skill name")
        description = fields.get("description", "")
        if not isinstance(description, str) or not 1 <= len(description) <= 1024:
            errors.append("Description missing or too long")
    if len(text.split()) > 1000:
        errors.append("Core exceeds 1000-word budget")
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
    expected = [
        "agents/openai.yaml", "references/briefs.md",
        "references/plan-format.md", "references/platforms.md",
        "references/skill-map.md", "references/delivery.md",
        "tests/scenarios.md",
    ]
    for name in expected:
        if not (root / name).is_file():
            errors.append(f"Missing companion: {name}")
        elif not (root / name).read_text(encoding="utf-8").strip():
            errors.append(f"Empty companion: {name}")
    manifest = root / "agents/openai.yaml"
    if manifest.exists():
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            interface = data["interface"]
            for field in ("display_name", "short_description", "default_prompt"):
                if not isinstance(interface[field], str) or not interface[field].strip():
                    raise ValueError(f"Invalid interface field: {field}")
            if "$personal-dev-skill" not in interface["default_prompt"]:
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
    print(f"{'FAIL' if findings else 'PASS'}: package structure, links, core budget")
    raise SystemExit(bool(findings))
