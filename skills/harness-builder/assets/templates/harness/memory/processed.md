# Processed items: {{SLUG}}

One line per handled item: `YYYY-MM-DD | id`, where id is the file name without its extension. `ingest.py --mark-processed` writes these lines in code; it is append-only.
The `rerun`, `golden` and `from` modes don't write here; `ingest.py` refuses. To reprocess one item, a human removes its line.

