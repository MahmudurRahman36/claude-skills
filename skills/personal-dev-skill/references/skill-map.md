# Skill map

Invoke only skills that change the outcome; each load costs tokens. Check the session's available-skills list — names below are the usual ones.

| Need | Skill / tool |
|---|---|
| Library/API docs | context7 MCP (`resolve-library-id` then `query-docs`) |
| Broad code search | `Explore` agent |
| Hard bug | `engineering:debug` |
| Design / architecture choice | `engineering:architecture`, `Plan` agent |
| Test strategy | `engineering:testing-strategy` |
| Run app / smoke | `run` skill; Playwright MCP for UI; curl for API |
| Code review | `code-review` (use `--fix` only after triage) |
| Security-relevant diff | `security-review` |
| Cleanup of large diff | `simplify` |
| UI work | `frontend-design:frontend-design` |
| Claude/LLM code | `claude-api` |
| Charts | `dataviz` |
| Office files | `xlsx`, `docx`, `pptx`, `pdf` |
| Pre-deploy | `engineering:deploy-checklist` |
| Repeated permission prompts | `fewer-permission-prompts` / `update-config` — allow specific read-only commands only |
