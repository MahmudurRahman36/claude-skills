# Fast-Path

A skill for Claude Code and Codex that finishes tasks faster and with fewer tokens, without changing the result.

It combines four ideas:

- **Dependency DAG and parallel batches** (after LLMCompiler, arXiv 2312.04511): independent reads, commands and subagents run together; dependent work waits only for its own inputs.
- **Triage and a fan-out threshold**: trivial work runs with no ceremony; subagents are used only when the saved time clearly beats the spawn and merge cost.
- **Shortcuts behind equivalence checks**: reuse, targeted reads, affected-tests-first, codemods and probes are allowed only when a check proves they match the long path. Otherwise the skill falls back and says so.
- **Caveman-style output** (github.com/JuliusBrussee/caveman): `lite`, `full` or `ultra` (default) compression for chat. Code, commits, docs, security warnings and destructive-action confirmations stay in normal language.

Verification gates override every speed and token rule.

## Files

| Path | Purpose |
|---|---|
| `SKILL.md` | The rules (under 150 lines, written in ultra style) |
| `references/dag-template.md` | Node table template, a STANDARD and a LARGE example |
| `references/shortcuts.md` | Shortcut catalogue: pattern, safe when, eq check, fallback |
| `references/subagent-brief.md` | Self-contained subagent brief and return format |
| `tests/scenarios.md` | 7 pressure-test scenarios with expected behaviour and reply shape |
| `tests/make_fixture.py` | Builds the throwaway project the scenarios run against |
| `scripts/validate.py` | Package checks: frontmatter, sections, links, line budget, manifest |
| `agents/openai.yaml` | Codex skill manifest |

## Install

Claude Code: copy this folder to `~/.claude/skills/fast-path/`. Codex: copy it to `~/.codex/skills/fast-path/`. Keep the companion files.

Invoke with `/fast-path` (or `/fast-path lite|full|ultra`; in Codex `$fast-path`), or let it trigger on requests about the agent's own pace, such as "work faster", "parallelize", "split task", "save tokens" (not on making code run faster). Turn it off with "stop fast-path"; "normal mode" turns off compression only.

## Validate

```
python -B scripts/validate.py
python -B -m unittest discover -s tests
```

Behavioural checks: build a fixture with `python tests/make_fixture.py <empty-dir>` and run each scenario in `tests/scenarios.md` against a fresh copy.
