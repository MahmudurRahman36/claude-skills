#!/usr/bin/env python3
"""Maintainer tool: regenerate the Codex templates from the Claude Code templates.

  python scripts/sync_codex_templates.py          # write
  python scripts/sync_codex_templates.py --check  # exit 1 if the Codex templates are stale
"""
import sys
from pathlib import Path

T = Path(__file__).resolve().parents[1] / "assets" / "templates"
CLAUDE, CODEX = T / "claude", T / "codex"

VERIFY_CALL_OLD = ("- For each item, call the `{{SLUG}}-verifier` subagent. Send it only the run folder path, "
                   "the item id, and that item's scorecard entry. Never send your reasoning.")
VERIFY_CALL_NEW = ("- Start a fresh, read-only Codex process. It must not see your reasoning:\n"
                   "  `codex exec --skip-git-repo-check --sandbox read-only '${{SLUG}}-verify <run folder>'`\n"
                   "  Use single quotes so the shell doesn't expand `$`.")
RECEIVE_OLD = ("For each item:\n"
               "- a run folder;\n"
               "- an item id;\n"
               "- that item's scorecard entry.")
RECEIVE_NEW = "You receive a run folder. Check every item in `<run folder>/scorecards.json`."


def drop_keys(text, keys):
    head, body = text.split("---\n", 2)[1:]
    kept = [ln for ln in head.splitlines() if ln.split(":", 1)[0].strip() not in keys]
    return "---\n" + "\n".join(kept) + "\n---\n" + body


def must_replace(text, old, new):
    if old not in text:
        raise SystemExit(f"sync: template text changed, update sync_codex_templates.py: {old[:60]!r}")
    return text.replace(old, new)


def yaml(name, desc, implicit):
    return (f'interface:\n  display_name: "{name}"\n  short_description: "{desc}"\n'
            f"policy:\n  allow_implicit_invocation: {str(implicit).lower()}\n")


def build():
    out = {}
    run = (CLAUDE / "skills/__SLUG__-run/SKILL.md").read_text(encoding="utf-8")
    run = drop_keys(run, {"argument-hint", "context", "agent"})
    run = must_replace(run, '--args "[$ARGUMENTS]" --run auto', '--args "[<what the user typed after the skill name, or nothing>]" --run auto --unguarded')
    run = must_replace(run, "--mark-processed --run <run folder>\n", "--mark-processed --run <run folder> --unguarded\n")
    run = must_replace(run, "- If ingest says the guard is not active, stop and report that. Don't retry another way.\n", "")
    run = must_replace(run, "The arguments are already filled in; don't change them:", "Put in the user's words after the skill name (e.g. `golden`), or leave the brackets empty:")
    run = must_replace(run, VERIFY_CALL_OLD, VERIFY_CALL_NEW)
    run = must_replace(run, "## 2. Intake (code decides what is new)",
                       "## 2. Intake (code decides what is new)\nCodex has no run guard (experimental: not yet run live). `--unguarded` "
                       "tells ingest.py so; its path checks still apply. Run with `--sandbox workspace-write` "
                       "and write only inside `{{HARNESS_DIR}}/runs/`.\n")
    out["skills/__SLUG__-run/SKILL.md"] = run.replace("/{{SLUG}}-", "${{SLUG}}-")
    cal = (CLAUDE / "skills/__SLUG__-calibrate/SKILL.md").read_text(encoding="utf-8")
    cal = drop_keys(cal, {"argument-hint", "disable-model-invocation"})
    cal = must_replace(cal, "starts with the date in `$ARGUMENTS`", "starts with the date the user gave")
    out["skills/__SLUG__-calibrate/SKILL.md"] = cal.replace("/{{SLUG}}-", "${{SLUG}}-")
    ver = (CLAUDE / "agents/__SLUG__-verifier.md").read_text(encoding="utf-8")
    body = must_replace(ver.split("---\n", 2)[2], RECEIVE_OLD, RECEIVE_NEW)
    out["skills/__SLUG__-verify/SKILL.md"] = (
        "---\nname: {{SLUG}}-verify\ndescription: Independent read-only checker for {{SLUG}} scorecards. "
        "Run it in a fresh `codex exec --sandbox read-only` process after scoring and before the report is written.\n---\n" + body)
    out["skills/__SLUG__-run/agents/openai.yaml"] = yaml("{{SLUG}} run", "Process new items, verify them, write the report", True)
    out["skills/__SLUG__-calibrate/agents/openai.yaml"] = yaml("{{SLUG}} calibrate", "Human-only: turn feedback into an approved general rule", False)
    out["skills/__SLUG__-verify/agents/openai.yaml"] = yaml("{{SLUG}} verify", "Read-only independent check of scorecards", False)
    return out


def main():
    check = "--check" in sys.argv
    stale = []
    for rel, content in build().items():
        path = CODEX / rel
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            continue
        stale.append(rel)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    print(("stale: " if check else "written: ") + (", ".join(stale) or "none"))
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
