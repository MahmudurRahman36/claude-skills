# Report: {{SLUG}}

Code renders the report (`scripts/render_report.py`) from `scorecards.json`, so its shape is identical every run. The model never writes `report.md` itself. It supplies these fields per item in `scorecards.json`:

| Field | What to write |
|---|---|
| `reason` | One line: why this verdict or label, in the owner's language |
| `human_checks` | List of questions or checks the decision owner should do (may be empty) |
| `verifier` | `{"status": "Confirmed" \| "Corrected" \| "Needs a human look", "notes": "<corrections, one line>"}` from the verifier |
| `confidence` | High / Medium / Low, with a short why |

Report sections, in order:
1. **Header:** run folder, mode, item counts, how many calibration notes were applied.
2. **Summary table:** item, score (if weighted), each label in `fields`, verdict, verifier status, reason.
3. **Details,** only for items that need attention: the top verdict, the human-review verdict, red flags, `uncertain`, or anything the verifier didn't confirm. Shows evidence quotes, red flags, human checks, verifier notes and confidence.
4. **Needs a decision:** items with no extractable text, and items sent to a human.
5. **Golden set:** in golden mode only, matches and diffs.

{{FILL: owner-specific wording for `reason` and `human_checks`, e.g. "reason: lead with the customer impact; human_checks: what to ask the customer on the call". Delete this line if there are none. To change the report's shape, a human edits render_report.py.}}
