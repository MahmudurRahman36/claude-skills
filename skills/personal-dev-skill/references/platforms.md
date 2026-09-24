# Platform mapping (Claude Code ↔ Codex)

SKILL.md names Claude Code tools. In Codex use the right-hand column. If a capability is missing, do the step inline and sequentially — never skip the step itself.

| Claude Code | Codex |
|---|---|
| `$ARGUMENTS` | the user's request / message that invoked the skill |
| `CLAUDE.md` | `AGENTS.md` (read both if present) |
| `TodoWrite` | `update_plan` |
| `Read` / `Grep` / `Glob` | shell: `cat`/`sed -n`, `rg`, `rg --files` |
| `Edit` / `Write` | `apply_patch` |
| `Bash` | shell tool |
| `Agent` (subagent, parallel calls) | spawn a sub-agent if enabled in this Codex build; otherwise run the brief yourself, one after another, and keep the ≤15-line return discipline |
| `Explore` agent | `rg` sweep done inline |
| `isolation: "worktree"` | `git worktree add ../wt-<task> -b <branch>` and work there |
| `model: haiku / sonnet / opus` | lower / default / highest reasoning effort for that sub-task |
| Skill tool (`code-review`, `run`, …) | open the matching `SKILL.md` under `~/.codex/skills` or `~/.agents/skills` and follow it; if none exists, do the step by hand (e.g. review = read `git diff` against the reviewer brief) |
| MCP (context7, Playwright) | same MCP server if configured in `~/.codex/config.toml`; else official docs / `npx playwright` |
| "Stop and ask" | ask in chat and wait |

Everything else (sizing, root-cause rule, evidence rule, fix-loop cap, final report) is identical on both.
