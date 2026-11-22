#!/usr/bin/env python3
"""Copy the harness templates into a project. Stdlib only.

Never overwrites an existing file (use --force to back it up and replace it).
Inserts or refreshes one marked block in CLAUDE.md / AGENTS.md. With --deny it merges
permission deny rules into .claude/settings.json without dropping existing rules.

Usage:
  python scaffold.py --project PATH --slug NAME [--harness-dir DIR]
                     [--platform claude|codex|both] [--dry-run] [--force]
  python scaffold.py --project PATH --slug NAME --guardrails-only --deny RULE [--deny RULE ...]
Prints a JSON summary. Exit 0 on success, 2 on bad input.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = SKILL_ROOT / "assets" / "templates"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
# template group -> (destination root, platforms that use it)
GROUPS = {
    "harness": ("{HARNESS}", {"claude", "codex", "both"}),
    "claude": (".claude", {"claude", "both"}),
    "codex": (".agents", {"codex", "both"}),
}
ENTRY_FILES = {"claude": ["CLAUDE.md"], "codex": ["AGENTS.md"], "both": ["CLAUDE.md", "AGENTS.md"]}
TEXT_SUFFIXES = {".md", ".py", ".sh", ".json", ".yaml", ".txt"}
CMD_PREFIX = {"CLAUDE.md": "/", "AGENTS.md": "$"}
PYTHON = "python" if os.name == "nt" else "python3"


def fail(msg):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(2)


def safe_rel(project, rel):
    """Resolve rel inside project; refuse absolute paths and escapes."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        fail(f"path must be relative and inside the project: {rel}")
    full = (project / p).resolve()
    if project != full and project not in full.parents:
        fail(f"path escapes the project: {rel}")
    return full


def skill_version():
    text = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r'^\s*version:\s*"?([^"\n]+)"?', text, re.M)
    return m.group(1).strip() if m else "unknown"


def render(text, tokens):
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def write(dest, content, args, report, label):
    if dest.exists():
        if not args.force:
            report["skipped"].append(label)
            return
        backup = dest.with_name(dest.name + ".bak-" + dt.datetime.now().strftime("%Y%m%d%H%M%S"))
        if not args.dry_run:
            shutil.copy2(dest, backup)
        report["backed_up"].append(label)
    if not args.dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8", newline="\n")
    report["created"].append(label)


def upsert_block(path, block, slug, args, report):
    start, end = f"<!-- harness:{slug} start -->", f"<!-- harness:{slug} end -->"
    wrapped = f"{start}\n{block.strip()}\n{end}\n"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if start in old and end in old:
        new = re.sub(re.escape(start) + r".*?" + re.escape(end) + r"\n?", lambda _: wrapped, old, flags=re.S)
        action = "refreshed"
    else:
        new = (old.rstrip("\n") + "\n\n" if old.strip() else "") + wrapped
        action = "appended" if old else "created"
    if not args.dry_run:
        path.write_text(new, encoding="utf-8", newline="\n")
    report["entry"].append(f"{path.name}: {action}")


def merge_settings(path, rules, args, report):
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError as exc:
        report["warnings"].append(f"{path} is not valid JSON ({exc}); settings not merged")
        return
    perms = current.setdefault("permissions", {})
    added = 0
    existing = perms.setdefault("deny", [])
    for rule in rules:
        if rule not in existing:
            existing.append(rule)
            added += 1
    if not args.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8", newline="\n")
    report["settings"] = f"{path.name}: {added} deny rule(s) added"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--harness-dir")
    ap.add_argument("--platform", choices=["claude", "codex", "both"], default="claude")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="replace existing files after backing them up")
    ap.add_argument("--deny", action="append", default=[], help="permission deny rule to merge (repeatable)")
    ap.add_argument("--guardrails-only", action="store_true", help="only merge --deny rules; write no templates")
    args = ap.parse_args(argv)

    project = Path(args.project).resolve()
    if not project.is_dir():
        fail(f"project folder not found: {project}")
    if not SLUG_RE.match(args.slug):
        fail("slug must be 2-41 chars of a-z, 0-9 and '-', starting with a letter or digit")
    harness_rel = (args.harness_dir or f"harness/{args.slug}").replace("\\", "/").strip("/") or "."
    safe_rel(project, harness_rel)

    tokens = {"SLUG": args.slug, "HARNESS_DIR": harness_rel, "DATE": dt.date.today().isoformat(), "PYTHON": PYTHON,
              "VERSION": skill_version()}
    report = {"ok": True, "dry_run": args.dry_run, "harness_dir": harness_rel, "platform": args.platform,
              "created": [], "skipped": [], "backed_up": [], "entry": [], "settings": None, "warnings": []}

    if args.guardrails_only:
        if not args.deny:
            fail("--guardrails-only needs at least one --deny rule")
        merge_settings(project / ".claude" / "settings.json", args.deny, args, report)
        print(json.dumps(report, indent=2))
        return 0

    for group, (root, platforms) in GROUPS.items():
        if args.platform not in platforms:
            continue
        base = TEMPLATES / group
        dest_root = harness_rel if root == "{HARNESS}" else root
        for src in sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES
                          and "__pycache__" not in p.parts):
            rel = src.relative_to(base).as_posix().replace("__SLUG__", args.slug)
            label = f"{dest_root}/{rel}" if dest_root != "." else rel
            dest = safe_rel(project, label)
            write(dest, render(src.read_text(encoding="utf-8"), tokens), args, report, label)

    block_tpl = (TEMPLATES / "entry-block.md").read_text(encoding="utf-8")
    for name in ENTRY_FILES[args.platform]:
        block = render(block_tpl, {**tokens, "CMD": CMD_PREFIX[name]})
        upsert_block(project / name, block, args.slug, args, report)

    if args.deny:
        merge_settings(project / ".claude" / "settings.json", args.deny, args, report)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
