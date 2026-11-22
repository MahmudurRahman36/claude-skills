---
name: {{SLUG}}-calibrate
description: Turn human feedback on a past {{SLUG}} run into a proposed general rule. After approval, save it to the calibration notes, rerun that run and the golden set, and report which results changed. Run manually as ${{SLUG}}-calibrate YYYY-MM-DD.
---

# Calibrate {{SLUG}}

Use this when the decision owner disagrees with a result. The goal is to learn a **general rule** from the correction, not to patch one item. Nothing is saved without explicit approval.

## 1. Load the run
- Find the run folder in `{{HARNESS_DIR}}/runs/` that starts with the date the user gave. Ignore `-golden` runs. If several match, list them and ask which one. If none match, list the recent runs and ask.
- Read that run's `report.md` and `scorecards.json`, plus `{{HARNESS_DIR}}/context/rubric.md` and `{{HARNESS_DIR}}/memory/calibration.md`.

## 2. Get the feedback
If the feedback isn't already in the message, ask one question: "Which results were wrong, and what should they have been?"

## 3. Find the root cause
For each correction, say which criterion, anchor or rule produced the wrong result, and why. Typical causes:
- an anchor too vague;
- a missing case;
- evidence misread;
- a threshold or weight wrong.

## 4. Propose the fix
- **How anchors are read or applied** becomes a calibration note. Use this format: `- [YYYY-MM-DD] C<n>: <general rule>. Why: <root cause>. Approved by: <who>.` Make it apply to every future item, not just this one.
- **A number** (threshold, weight, scale, verdict rule) becomes a recommended edit to `context/rubric.md` and its `harness-config` block. The owner makes that edit. A note can't change a number, because the code check reads only the rubric.
- List any conflict with existing notes. Ask clarifying questions where a rule could over-reach.

## 5. Wait for approval
Show the proposal and stop. Save nothing until the human approves. Apply any edits they make to the wording.

## 6. Save, then rerun
- Append the approved notes under `## Notes` in `{{HARNESS_DIR}}/memory/calibration.md`.
- Run `${{SLUG}}-run from <that run folder>`, which rescores the same items from their saved sources.
- Then run `${{SLUG}}-run golden`.

## 7. Report the changes
Compare the new run with the original `scorecards.json` and report three groups:
- **Expected changes:** what the feedback asked for.
- **Unexpected changes:** other items that moved.
- **Golden regressions:** golden cases that no longer match.

Treat any unexpected change or regression as a reason to reword the rule. Offer to revert the note.
