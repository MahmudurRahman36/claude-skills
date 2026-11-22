# Golden set: {{SLUG}}

These are labelled cases with known right answers, so a rule change can't silently break past judgments.

`golden.json` is a list:

```json
[
  {"id": "case-file-name-without-extension", "expected_verdict": "Shortlist",
   "expected_scores": {"C1": 5}, "expected_fields": {"priority": "P2"},
   "expected_red_flag": false, "label_source": "owner", "why": "one line"}
]
```

- Aim for 10-20 cases. Cover clear yes, clear no, borderline items and known traps. Include one prompt-injection case: an item whose text tries to instruct the AI. It must be flagged, never obeyed.
- Every key except `id` is optional. Check only what the owner labelled.
- Labels come from the decision owner (`label_source: owner`). The one exception: the AI may label the prompt-injection case from the owner's written policy (`label_source: owner-policy`), because it only asserts that the case gets flagged.
- Put each case's original file in `cases/`, named `<id>.<ext>`. Anonymise it. Use no real personal data unless policy allows it.

Regression run, after any change to the rubric, the calibration notes or the model:

```
/{{SLUG}}-run golden        (Codex: ${{SLUG}}-run golden)
```
This ingests `cases/`, judges every case, then runs `check_run.py --golden` and reports the matches.

Treat every changed verdict as a regression until the owner accepts it.
