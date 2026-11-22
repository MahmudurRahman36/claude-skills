# Calibration notes: {{SLUG}}

General rules learned from human corrections. Only `/{{SLUG}}-calibrate` adds notes here, and only after a human approves them. Every run reads this file.

Notes change how anchors and evidence are **read**. They never change numbers. Thresholds, weights, scale and verdict rules live only in `context/rubric.md`, which the owner edits, because `check_run.py` reads them from there. If a note seems to conflict with the rubric, stop and ask the owner.

Format: `- [YYYY-MM-DD] C<n>: <general rule>. Why: <root cause>. Approved by: <who>.`

Upkeep: keep this file under 60 notes. Each month, a human folds stable notes into `context/rubric.md` and deletes them here.

## Notes
(none yet)
