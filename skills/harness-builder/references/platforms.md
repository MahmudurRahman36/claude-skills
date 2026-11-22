# Platform mapping

| Concept | Claude Code | Codex |
|---|---|---|
| Always-loaded entry file | `CLAUDE.md` (falls back to `AGENTS.md`); keep under 200 lines | `AGENTS.md`, root to cwd, 32 KiB default cap |
| Project skills | `.claude/skills/<name>/SKILL.md`, run with `/<name>` | `.agents/skills/<name>/SKILL.md`, run with `$<name>` |
| Skill arguments | `$ARGUMENTS`, `$0`, `$1` | none; read from the user message |
| Manual-only skill | `disable-model-invocation: true` | `agents/openai.yaml`, `policy.allow_implicit_invocation: false` |
| Independent verifier | subagent `.claude/agents/<slug>-verifier.md` with `tools: Read, Grep, Glob` | fresh `codex exec --sandbox read-only '$<slug>-verify ...'` |
| Hard guardrails | `.claude/settings.json` `permissions.deny`, PreToolUse hooks | sandbox mode, approval policy |
| Connectors | `.mcp.json` or Desktop connectors | MCP in `~/.codex/config.toml` |
| Schedule | Desktop task, cloud task (`/schedule`), GitHub Action, `claude -p` + cron | ChatGPT app scheduled tasks, `codex exec` + cron, `openai/codex-action@v1` |

Verified in a real headless run (Claude Code 2.1.330, 2026-11-22):
- `claude -p "/<slug>-run golden"` expands the skill.
- `context: fork` runs it in `<slug>-runner` (depth 1), which can call `<slug>-verifier` (depth 2), each on its own model.
- `$ARGUMENTS` is substituted **inline**, so "Pick the mode from `$ARGUMENTS`" read as "from `golden`" and the run took the wrong mode. An explicit `Arguments: [$ARGUMENTS]` line plus rules still went wrong in 1 of 3 runs: the runner saw `[golden]` and chose default. So the template now passes the arguments to `ingest.py --args "[$ARGUMENTS]"` and **code** picks the mode. `check_run.py` and `render_report.py` read the mode from the manifest. Code (`ingest.py`) also refuses to mark golden, rerun and from runs as processed.
- In `--permission-mode dontAsk`, any tool call outside `--allowedTools` is denied, and so are deny-rule matches.
- A subagent's frontmatter hook (`matcher: "*"`) fired on every tool call of that subagent, including Read, Glob and Agent, and in the verifier too. `sh` was available to hooks on Windows through Git Bash.
- Claude Code skips the frontmatter hooks of project agents in a folder whose trust prompt was never accepted (debug log: "Skipping frontmatter hooks for agent ... not trusted"). The guard then never runs. Before this was known, an earlier version's guard silently did nothing in headless test runs. Now `ingest.py` refuses to start without the guard's fresh stamp, so the run stops instead. Fix: open the project in Claude Code once and accept the prompt, or for CI set `projects["<absolute project path>"].hasTrustDialogAccepted: true` in `~/.claude.json`.
- With `--permission-mode bypassPermissions`, the hook still blocked every forbidden call, and deny rules still applied.
- Claude Code may refuse a subagent's Write of a report file ("Subagents should return findings as text, not write report files"). It happened in 1 of 2 runs. So `render_report.py` writes `report.md` in code.

Codex has no subagent hooks, so a Codex harness has no `guard.py`. Its run skill passes `--unguarded` to `ingest.py`, whose path checks still apply. Codex's sandbox (`--sandbox workspace-write` for the run, `read-only` for the verifier) and approval policy are the only other limits. Treat Codex support as experimental: it hasn't been run live.

Run `scaffold.py --platform both` when a team uses both tools. The `harness/` folder is shared; only the entry blocks and the skill folders differ.

Tool names used in this skill's instructions (Claude Code / Codex):
- Read file: Read / `cat` via shell.
- Search: Grep, Glob / `rg`.
- Subagent: Agent tool / a separate `codex exec` process.
- Ask the user: AskUserQuestion or a plain message / a plain message.
- Invoke another skill: Skill tool / `$name`.
