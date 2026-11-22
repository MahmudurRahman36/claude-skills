#!/usr/bin/env python3
"""Self-check the harness-builder package itself. Stdlib only. Exit 1 on errors."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_LINES = 300
REQUIRED = [
    "SKILL.md", "README.md", "agents/openai.yaml",
    "references/discovery.md", "references/interview.md", "references/harness-spec.md",
    "references/layers.md", "references/security.md", "references/automation.md", "references/platforms.md",
    "scripts/scaffold.py", "scripts/validate_harness.py",
    "assets/templates/entry-block.md", "scripts/sync_codex_templates.py",
    "assets/templates/harness/scripts/ingest.py", "assets/templates/harness/scripts/guard.py",
    "assets/templates/harness/scripts/render_report.py",
    "assets/templates/claude/agents/__SLUG__-runner.md",
    "assets/templates/harness/HARNESS.md", "assets/templates/harness/scripts/check_run.py",
    "assets/templates/claude/skills/__SLUG__-run/SKILL.md",
    "assets/templates/claude/skills/__SLUG__-calibrate/SKILL.md",
    "assets/templates/claude/agents/__SLUG__-verifier.md",
    "assets/templates/codex/skills/__SLUG__-run/SKILL.md",
    "assets/templates/codex/skills/__SLUG__-verify/SKILL.md",
]
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
PHASES = ["Phase 0", "Phase 1", "Phase 2", "Phase 3", "Phase 4", "Phase 5", "Phase 6", "Phase 7", "Phase 8"]


def main():
    errors = []
    for rel in REQUIRED:
        if not (ROOT / rel).is_file():
            errors.append(f"missing {rel}")
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not m:
        errors.append("SKILL.md frontmatter missing")
    else:
        keys = set(re.findall(r"^([A-Za-z_-]+):", m.group(1), re.M))
        if keys - SPEC_KEYS:
            errors.append(f"non-spec frontmatter keys (breaks Codex/agentskills portability): {sorted(keys - SPEC_KEYS)}")
        name = re.search(r"^name:\s*(\S+)", m.group(1), re.M)
        if not name or name.group(1) != ROOT.name:
            errors.append("name must match the folder name")
        desc = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
        if not desc or not 1 <= len(desc.group(1)) <= 1024:
            errors.append(f"description must be 1-1024 chars (got {len(desc.group(1)) if desc else 0})")
    if len(text.splitlines()) > MAX_LINES:
        errors.append(f"SKILL.md over {MAX_LINES} lines")
    for phase in PHASES:
        if f"### {phase}" not in text:
            errors.append(f"missing section {phase}")
    # scripts/ paths in SKILL.md may name the generated harness's own scripts, so only check these two
    for link in re.findall(r"`((?:references|assets)/[^`\s]+)`", text):
        if not (ROOT / link).exists():
            errors.append(f"broken reference {link}")
    if list((ROOT / "assets").rglob("__pycache__")):
        errors.append("__pycache__ inside assets/ (delete it)")
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".py", ".json", ".yaml"}:
            content = path.read_text(encoding="utf-8")
            if re.search(r"^(?:<{7}|={7}|>{7})(?: |$)", content, re.M):
                errors.append(f"merge marker in {path.relative_to(ROOT)}")
    for e in errors:
        print("ERROR", e)
    print("package OK" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
