# Harness spec template

Write this to `<harness dir>/SPEC.md` before scaffolding. It is the contract the user confirms and the reference you fill every template from. Keep it under 150 lines.

```markdown
# Harness spec: <slug>
Date: YYYY-MM-DD | Status: draft / confirmed by <who> on <date>
Preflight: root <path> | git: yes/no (backup plan) | platform: claude/codex/both | new / improving <existing>

## 1. Understanding
Tag each part when a row mixes facts and decisions (e.g. "K: source folder; U: volume").

| # | Item | Value | K/I/U | Evidence (file, command or interview answer) |
|---|---|---|---|---|
| 1 | Job and goal | | | |
| 2 | Decision owner | | | |
| 3 | Trigger and volume | | | |
| 4 | Inputs and access | | | |
| 5 | Output and destination | | | |
| 6 | Quality bar | | | |
| 7 | Oracle (code vs judgment) | | | |
| 8 | Failure cost and irreversible actions | | | |
| 9 | Guardrails | | | |
| 10 | Sensitive data and retention | | | |
| 11 | State | | | |
| 12 | Golden examples | | | |

## 2. Why a harness (and not a script or nothing)
Value score: freq x repeat x verifiable x (6-risk) = ... Pattern: <chaining/routing/parallel/evaluator-optimizer>.

## 3. Layer design
- Context: files and what goes in each; scale (binary or 1/3/5); criteria, weights or `fields`; verdict rules; unsure rule.
- Tools: connectors and MCP servers, the exact scope (folder, read-only?); every "never" and how it is enforced (deny rule, hook, missing tool, or prose only).
- Process: run steps; output template summary.
- Verification: code checks; runner (scoring) model; verifier model; checks list; when "Needs a human look".
- Memory: processed log key; calibration approval owner.
- Automation: trigger, schedule, local or cloud, where reports land; start date.

## 4. Risks and mitigations
Prompt injection, bias or legal, privacy and retention, cost, drift.

## 5. Open questions (blocking)
(must be empty before Phase 4)

## 5b. Follow-ups (non-blocking)
(e.g. "owner adds 10 more golden cases by <date>")

## 6. Acceptance
Phase 6:
- [ ] validate_harness.py: 0 errors
- [ ] Fresh-context golden run: <n>/<n> match; injection case flagged, not obeyed
- [ ] Second golden run gives identical results; a no-new-items run stops in one line
- [ ] Seeded error: check_run reports it and the verifier returns Corrected; the verifier also returned at least one Confirmed
- [ ] Guard exits 2 on a forbidden call; the deny rule blocks an input edit in a fresh session
- [ ] Calibrate stops for approval

Before Phase 7:
- [ ] Owner reviewed one real report
```
