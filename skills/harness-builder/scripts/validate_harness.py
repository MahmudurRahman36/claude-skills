#!/usr/bin/env python3
"""Check a generated harness for structure, limits and safety. Stdlib only.

Usage:
  python validate_harness.py --project PATH --slug NAME [--harness-dir DIR]
                             [--platform claude|codex|both] [--allow-fill] [--json]
Exit 0 = no errors (warnings allowed), 1 = errors found, 2 = bad input.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ENTRY_MAX_LINES = 200
SKILL_MAX_LINES = 500
DESC_MAX = 1024
READ_ONLY_TOOLS = {"Read", "Grep", "Glob"}
KNOWN_SKILL_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools",
                    "disallowed-tools", "disable-model-invocation", "user-invocable", "argument-hint",
                    "model", "effort", "context", "agent", "hooks", "when_to_use", "paths", "shell"}
KNOWN_AGENT_KEYS = {"name", "description", "tools", "disallowedTools", "model", "permissionMode",
                    "maxTurns", "skills", "mcpServers", "hooks", "memory", "background", "effort",
                    "isolation", "color", "initialPrompt", "omitClaudeMd"}
SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}", r"\bsk-[A-Za-z0-9_-]{20,}", r"\bghp_[A-Za-z0-9]{30,}", r"\bglpat-[A-Za-z0-9_-]{20,}",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]
CONFIG_RE = re.compile(r"```json harness-config\s*\n(.*?)\n```", re.S)


def frontmatter(text):
    """Tiny YAML-subset parser: scalars, inline lists, and '- item' lists."""
    m = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return None
    data, key = {}, None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and key:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(item.group(1).strip().strip("'\""))
            continue
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv:
            key, value = kv.group(1), kv.group(2).strip()
            data[key] = value.strip("'\"") if value else ""
        elif key and line.startswith((" ", "\t")) and isinstance(data.get(key), str):
            data[key] = (data[key] + " " + line.strip()).strip()  # folded continuation
    return data


def as_list(value):
    if isinstance(value, list):
        return [v.strip() for v in value if v.strip()]
    value = (value or "").strip().strip("[]")
    return [v.strip().strip("'\"") for v in re.split(r"[,\s]+", value) if v.strip()]


class Report:
    def __init__(self):
        self.errors, self.warnings, self.passed = [], [], []

    def check(self, ok, msg, warn=False):
        if ok:
            self.passed.append(msg)
        elif warn:
            self.warnings.append(msg)
        else:
            self.errors.append(msg)
        return ok


def check_skill(r, path, slug_name, expect_manual=False):
    if not r.check(path.is_file(), f"exists: {path.as_posix()}"):
        return None
    text = path.read_text(encoding="utf-8")
    fm = frontmatter(text)
    if not r.check(fm is not None, f"frontmatter present: {path.name} in {path.parent.name}"):
        return text
    r.check(fm.get("name") == path.parent.name == slug_name,
            f"skill name '{fm.get('name')}' matches folder '{path.parent.name}'")
    desc = fm.get("description", "")
    r.check(0 < len(desc) <= DESC_MAX, f"{slug_name}: description 1-{DESC_MAX} chars (got {len(desc)})")
    unknown = set(fm) - KNOWN_SKILL_KEYS
    r.check(not unknown, f"{slug_name}: no unknown frontmatter keys {sorted(unknown)}", warn=True)
    r.check(len(text.splitlines()) <= SKILL_MAX_LINES, f"{slug_name}: SKILL.md <= {SKILL_MAX_LINES} lines")
    if expect_manual:
        policy = path.parent / "agents" / "openai.yaml"
        manual = fm.get("disable-model-invocation", "").lower() == "true" or (
            policy.is_file() and re.search(r"allow_implicit_invocation:\s*false", policy.read_text(encoding="utf-8")))
        r.check(bool(manual), f"{slug_name}: manual-only (disable-model-invocation: true or openai.yaml allow_implicit_invocation: false)")
    return text


def guard_hooked(text):
    """True when an agent file's frontmatter sends every tool call through guard.sh."""
    head = text.split("---", 2)[1] if text.startswith("---") else ""
    return "PreToolUse" in head and "guard.sh" in head and re.search(r'matcher:\s*["\']\*["\']', head) is not None


def skill_version():
    m = re.search(r'^\s*version:\s*"?([^"\n]+)"?', (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else None


def check_config(r, rubric_text, project=None):
    m = CONFIG_RE.search(rubric_text)
    if not r.check(m is not None, "rubric has a ```json harness-config block", warn=True):
        return
    try:
        cfg = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        r.check(False, f"harness-config is valid JSON ({exc})")
        return
    crit = cfg.get("criteria", [])
    ids = {c.get("id") for c in crit}
    if crit:
        r.check(len(ids) == len(crit), "criteria ids unique")
        r.check(bool(cfg.get("scale")), "harness-config defines a score scale")
        weighted = [c for c in crit if "weight" in c]
        if weighted:
            r.check(len(weighted) == len(crit), "either every criterion has a weight or none does")
            total = sum(float(c.get("weight", 0)) for c in weighted)
            r.check(abs(total - 1.0) < 0.011, f"criteria weights sum to 1.0 (got {total:.3f})")
    verdicts = cfg.get("verdicts", [])
    if verdicts:
        conds = {"min_score", "min_criteria", "fail_if_at_most", "no_red_flags", "no_flags"}
        r.check(not (conds & set(verdicts[-1])), "last verdict rule has no conditions (catch-all)")
        used = {k for v in verdicts for key in ("min_criteria", "fail_if_at_most") for k in v.get(key, {})}
        r.check(used <= ids, f"verdict rules reference only defined criteria (unknown: {sorted(used - ids)})")
        if any("min_score" in v for v in verdicts):
            r.check(all("weight" in c for c in crit) and bool(crit), "min_score rules need weighted criteria")
    r.check(bool(crit or cfg.get("fields")), "harness-config defines criteria or fields")
    live = cfg.get("live_source")
    if r.check(isinstance(live, str) and bool(live.strip()), "harness-config sets live_source (the only folder runs read)"):
        if project is not None and "{{FILL" not in live:
            r.check((project / live).is_dir(), f"live_source folder exists: {live}", warn=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--harness-dir")
    ap.add_argument("--platform", choices=["claude", "codex", "both"], default="claude")
    ap.add_argument("--allow-fill", action="store_true", help="tolerate {{FILL ...}} markers (right after scaffold)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"project not found: {project}")
        return 2
    slug = args.slug
    hdir = project / (args.harness_dir or f"harness/{slug}")
    r = Report()

    # Layer 1: context
    entries = {"claude": ["CLAUDE.md"], "codex": ["AGENTS.md"], "both": ["CLAUDE.md", "AGENTS.md"]}[args.platform]
    for name in entries:
        f = project / name
        if r.check(f.is_file(), f"exists: {name}"):
            text = f.read_text(encoding="utf-8")
            r.check(len(text.splitlines()) <= ENTRY_MAX_LINES,
                    f"{name} <= {ENTRY_MAX_LINES} lines (got {len(text.splitlines())}); long entry files are followed less reliably", warn=True)
            r.check(f"<!-- harness:{slug} start -->" in text, f"{name} has the harness:{slug} block")
    for rel in ["HARNESS.md", "context/brief.md", "context/rubric.md", "context/example.md",
                "context/output-template.md", "memory/processed.md", "memory/calibration.md",
                "evals/golden.json", "scripts/check_run.py", "scripts/ingest.py", "scripts/render_report.py"]:
        r.check((hdir / rel).is_file(), f"exists: {(hdir / rel).relative_to(project).as_posix()}")
    harness_md = hdir / "HARNESS.md"
    if harness_md.is_file():
        text = harness_md.read_text(encoding="utf-8")
        r.check(len(text.splitlines()) <= ENTRY_MAX_LINES, f"HARNESS.md <= {ENTRY_MAX_LINES} lines")
        r.check(re.search(r"(?i)decision owner|human (decides|owns|approves)", text) is not None,
                "HARNESS.md names the human decision owner")
        r.check(re.search(r"(?i)untrusted|data, not instructions", text) is not None,
                "HARNESS.md marks inputs as untrusted data")
    rubric = hdir / "context" / "rubric.md"
    if rubric.is_file():
        check_config(r, rubric.read_text(encoding="utf-8"), project)
    example = hdir / "context" / "example.md"
    if example.is_file():
        r.check(re.search(r"(?i)not (to )?copy", example.read_text(encoding="utf-8")) is not None,
                "example.md has a 'what NOT to copy' section")
    golden = hdir / "evals" / "golden.json"
    if golden.is_file():
        try:
            cases = json.loads(golden.read_text(encoding="utf-8"))
            r.check(isinstance(cases, list), "evals/golden.json is a JSON list")
            r.check(isinstance(cases, list) and len(cases) >= 5,
                    f"golden set has >= 5 labelled cases (got {len(cases) if isinstance(cases, list) else 0})", warn=True)
        except json.JSONDecodeError as exc:
            r.check(False, f"evals/golden.json valid JSON ({exc})")

    # Layers 3-5: skills + verifier
    roots = {"claude": [".claude"], "codex": [".agents"], "both": [".claude", ".agents"]}[args.platform]
    for root in roots:
        run_text = check_skill(r, project / root / "skills" / f"{slug}-run" / "SKILL.md", f"{slug}-run")
        check_skill(r, project / root / "skills" / f"{slug}-calibrate" / "SKILL.md", f"{slug}-calibrate", expect_manual=True)
        if run_text is not None:
            r.check(f"{slug}-verifier" in run_text or f"{slug}-verify" in run_text, f"{slug}-run hands work to the verifier")
            r.check("check_run.py" in run_text, f"{slug}-run calls the deterministic checker")
            r.check("ingest.py" in run_text and "--mark-processed" in run_text, f"{slug}-run uses code intake and marks processed items")
            r.check("calibration.md" in run_text, f"{slug}-run reads memory/calibration.md")
            r.check("render_report.py" in run_text, f"{slug}-run renders the report in code")
            if root == ".claude":
                fm = frontmatter(run_text) or {}
                r.check(fm.get("context") == "fork" and fm.get("agent") == f"{slug}-runner",
                        f"{slug}-run forks into the guarded {slug}-runner (context: fork, agent: {slug}-runner)")
        if root == ".claude":
            runner = project / ".claude" / "agents" / f"{slug}-runner.md"
            if r.check(runner.is_file(), f"exists: .claude/agents/{slug}-runner.md"):
                r.check(guard_hooked(runner.read_text(encoding="utf-8")),
                        'runner sends every tool call through guard.sh (PreToolUse, matcher "*")')
                rfm = frontmatter(runner.read_text(encoding="utf-8")) or {}
                rtools = set(as_list(rfm.get("tools", "")))
                r.check(bool(rtools), "runner declares a tools allowlist (omitting it inherits ALL tools)")
                r.check(not (rtools & {"WebFetch", "WebSearch"}), "runner has no web tools (lethal trifecta)", warn=True)
                r.check((hdir / "scripts" / "guard.py").is_file() and (hdir / "scripts" / "guard.sh").is_file(),
                        "guard.py and guard.sh exist")
            agent = project / ".claude" / "agents" / f"{slug}-verifier.md"
            if r.check(agent.is_file(), f"exists: .claude/agents/{slug}-verifier.md"):
                fm = frontmatter(agent.read_text(encoding="utf-8")) or {}
                tools = set(as_list(fm.get("tools", "")))
                r.check(guard_hooked(agent.read_text(encoding="utf-8")),
                        'verifier sends every tool call through guard.sh (PreToolUse, matcher "*")')
                r.check(bool(tools), "verifier declares a tools allowlist (omitting it inherits ALL tools)")
                r.check(tools <= READ_ONLY_TOOLS, f"verifier tools are read-only (got {sorted(tools)})")
                r.check(fm.get("name") == f"{slug}-verifier", "verifier name matches file")
                unknown = set(fm) - KNOWN_AGENT_KEYS
                r.check(not unknown, f"verifier: no unknown frontmatter keys {sorted(unknown)}", warn=True)
            settings = project / ".claude" / "settings.json"
            if settings.is_file():
                try:
                    perms = json.loads(settings.read_text(encoding="utf-8")).get("permissions", {})
                    r.check(bool(perms.get("deny")), "settings.json has deny rules for inputs and outbound tools", warn=True)
                except json.JSONDecodeError as exc:
                    r.check(False, f".claude/settings.json valid JSON ({exc})")
            else:
                r.check(False, ".claude/settings.json exists with deny rules", warn=True)
        else:
            r.check((project / ".agents" / "skills" / f"{slug}-verify" / "SKILL.md").is_file(),
                    f"exists: .agents/skills/{slug}-verify/SKILL.md (Codex verifier)")

    # Cross-cutting: placeholders and secrets across every generated file
    scan = [p for p in [hdir, project / ".claude" / "skills", project / ".claude" / "agents",
                        project / ".agents" / "skills"] if p.is_dir()]
    def ours(d, f):
        if d == hdir:
            return "runs" not in f.relative_to(d).parts
        return f.relative_to(d).parts[0].startswith(slug)
    files = [f for d in scan for f in d.rglob("*") if f.is_file() and f.suffix in {".md", ".json", ".py", ".txt", ".yaml"}
             and ours(d, f)]
    files += [project / n for n in entries if (project / n).is_file()]
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        rel = f.relative_to(project).as_posix()
        if re.search(r"\{\{(SLUG|HARNESS_DIR|DATE|PYTHON|CMD|VERSION)\}\}", text):
            r.check(False, f"unrendered template token in {rel}")
        if not args.allow_fill and "{{FILL" in text:
            r.check(False, f"unfilled {{{{FILL}}}} marker in {rel}")
        for pat in SECRET_PATTERNS:
            if re.search(pat, text):
                r.check(False, f"possible secret ({pat}) in {rel}")

    if args.json:
        print(json.dumps({"errors": r.errors, "warnings": r.warnings, "passed": len(r.passed)}, indent=2))
    else:
        for e in r.errors:
            print("ERROR", e)
        for w in r.warnings:
            print("WARN ", w)
        print(f"{len(r.passed)} passed, {len(r.warnings)} warning(s), {len(r.errors)} error(s)")
    return 1 if r.errors else 0


if __name__ == "__main__":
    sys.exit(main())
