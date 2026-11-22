# Waiting for a human: {{SLUG}}

One line per item the run sent to a human: `YYYY-MM-DD | id | run`. `ingest.py --mark-processed` writes these lines, and default runs skip these ids so nothing is scored twice.
When you've decided an item, move its line to `processed.md` (or delete it to have the next run score it again).
