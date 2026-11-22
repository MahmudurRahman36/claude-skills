#!/usr/bin/env python3
"""Deterministic intake for the {{SLUG}} harness. Stdlib only.

Code, not the model, decides which items are new and writes the source text that quotes are
checked against, so the scorer never transcribes its own evidence.

  from the skill:  python {{HARNESS_DIR}}/scripts/ingest.py --args "[<raw skill arguments>]" --run auto
  explicit:        python {{HARNESS_DIR}}/scripts/ingest.py --mode default|rerun|golden|from --source DIR --run auto
  mark processed:  python {{HARNESS_DIR}}/scripts/ingest.py --mark-processed --run RUN_DIR
  outside Claude Code (no guard hook, e.g. Codex or by hand): add --unguarded

--run auto creates {{HARNESS_DIR}}/runs/<YYYY-MM-DD-HHMMSS>[-mode]/ from the real clock and prints it
as "run_dir". Every mode except default reprocesses all items and never marks them processed.
Safety, in code: run folders must be inside runs/; sources must be the configured `live_source`
(harness-config in context/rubric.md), evals/cases, or a folder inside runs/. Unless --unguarded is
given, ingest refuses to start when the guard hook has not just stamped memory/.guard-ok.

--mark-processed marks an item done only when all of these hold: the manifest is the one ingest wrote
(hash in memory/run-log.jsonl), report.md was rendered by render_report.py from the current
scorecards.json, the item has a scorecard, and that scorecard passes check_run. Items with the
human-review verdict go to memory/pending.md instead. Anything else is left for the next run.

Item id = file name without extension. Ids in memory/processed.md and memory/pending.md are skipped
in default mode, read by code (no size limit). Text extraction:
.txt/.md/.csv/.json/.eml/.html copied as text; .pdf via `pdftotext` (poppler) or the
`pypdf` package if installed, otherwise reported as `needs_text` for a human to fix.
Writes RUN_DIR/sources/<id>.txt and RUN_DIR/manifest.json; prints the manifest.
Exit 0 ok, 2 bad input.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_run import RUNS, check_item, load_config, log_run, logged, run_folder, run_name, sha256_file  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
PROCESSED = HERE / "memory" / "processed.md"
PENDING = HERE / "memory" / "pending.md"
RUBRIC = HERE / "context" / "rubric.md"
STAMP = HERE / "memory" / ".guard-ok"
STAMP_MAX_AGE = 120  # seconds between the guard hook firing and ingest starting
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".eml", ".html", ".htm", ".xml", ".log", ".diff", ".patch"}


def ids_in(path):
    if not path.is_file():
        return set()
    return {line.split("|")[1].strip() for line in path.read_text(encoding="utf-8").splitlines()
            if "|" in line and line[:4].isdigit()}


def handled_ids():
    return ids_in(PROCESSED) | ids_in(PENDING)


def refuse(message):
    print(json.dumps({"ok": False, "error": message}))
    return 2


def guard_active():
    try:
        return time.time() - STAMP.stat().st_mtime < STAMP_MAX_AGE
    except OSError:
        return False


def source_allowed(src, live):
    s = Path(src).resolve()
    if s == (HERE / "evals" / "cases").resolve() or RUNS.resolve() in s.parents:
        return True
    return bool(live) and s == Path(live).resolve()


def mark_processed(run):
    name = run_name(run)
    manifest_path, cards, report = run / "manifest.json", run / "scorecards.json", run / "report.md"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("mode", "default") != "default" or manifest.get("all") or run.name.endswith(("-golden", "-rerun", "-from")):
        print(json.dumps({"marked": [], "refused": "rerun, golden and from runs never mark items processed"}))
        return 0
    if logged(name, "manifest_sha256") != sha256_file(manifest_path):
        return refuse("manifest.json is not the one ingest.py wrote for this run; nothing was marked")
    if (not report.is_file() or logged(name, "report_sha256") != sha256_file(report)
            or logged(name, "scorecards_sha256") != sha256_file(cards)):
        return refuse("render report.md with render_report.py from the current scorecards.json first; nothing was marked")
    cfg = load_config(RUBRIC)
    human = cfg.get("human_review_verdict", "Needs a human look")
    data = json.loads(cards.read_text(encoding="utf-8"))
    items = [i for i in (data.get("items", []) if isinstance(data, dict) else data) if isinstance(i, dict)]
    with_text = {i["id"] for i in manifest.get("items", []) if i.get("text")}
    done = handled_ids()
    marked, pending, failed = [], [], []
    for it in items:
        item_id = it.get("id")
        if item_id not in with_text or item_id in done:
            continue
        done.add(item_id)
        if check_item(it, cfg, run):
            failed.append(item_id)
        else:
            (pending if it.get("verdict") == human else marked).append(item_id)
    scored = {it.get("id") for it in items}
    today = dt.date.today().isoformat()
    for path, ids, extra in ((PROCESSED, marked, ""), (PENDING, pending, f" | {name}")):
        if ids:
            with path.open("a", encoding="utf-8", newline="\n") as f:
                for item_id in ids:
                    f.write(f"{today} | {item_id}{extra}\n")
    print(json.dumps({"marked": marked, "pending_human": pending,
                      "not_marked": {"failed_checks": failed,
                                     "no_scorecard": sorted(with_text - scored - handled_ids())}}))
    return 0


def extract(path):
    ext = path.suffix.lower()
    if ext in TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="replace"), "copy"
    if ext == ".pdf":
        if shutil.which("pdftotext"):
            out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout, "pdftotext"
        try:
            from pypdf import PdfReader  # optional dependency
            text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
            if text.strip():
                return text, "pypdf"
        except Exception:  # noqa: BLE001 - any failure means "no extractor"
            pass
    return None, "needs_text"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic intake for {{SLUG}}")
    ap.add_argument("--source")
    ap.add_argument("--run", required=True)
    ap.add_argument("--all", action="store_true", help="ignore processed.md (implied by every mode except default)")
    ap.add_argument("--mode", choices=["default", "rerun", "golden", "from"], default="default")
    ap.add_argument("--args", help="raw skill arguments; code picks the mode from them")
    ap.add_argument("--mark-processed", action="store_true")
    ap.add_argument("--unguarded", action="store_true", help="run without the Claude Code guard hook (Codex, by hand)")
    args = ap.parse_args(argv)
    if not args.unguarded and not guard_active():
        return refuse("the run guard is not active, so ingest will not start. Run this through /{{SLUG}}-run in "
                      "Claude Code, in a folder Claude Code trusts: open it once interactively and accept the trust "
                      "prompt, because Claude Code skips project agents' hooks in untrusted folders. Outside Claude "
                      "Code add --unguarded; then only your own sandbox limits the run.")
    try:
        if args.mark_processed:
            return mark_processed(run_folder(args.run))
        live = load_config(RUBRIC).get("live_source")
        if args.args is not None:
            raw = args.args.strip().strip("[]").strip()
            words = raw.lower().split()
            if "golden" in words:
                args.mode, args.source = "golden", None
            elif words[:1] == ["from"] and len(raw.split(None, 1)) == 2:
                args.mode = "from"
                args.source = str(run_folder(raw.split(None, 1)[1].strip().strip("\"'")) / "sources")
            else:
                args.mode, args.source = ("rerun" if "rerun" in words else "default"), live
                if not live:
                    return refuse("set live_source in harness-config (context/rubric.md)")
        if args.mode != "default":
            args.all = True
        if args.mode == "golden" and not args.source:
            args.source = str(HERE / "evals" / "cases")
        if not args.source or not Path(args.source).is_dir():
            return refuse(f"source folder not found: {args.source}")
        if not source_allowed(args.source, live):
            return refuse(f"source must be live_source ({live}), evals/cases or a folder inside runs/; got: {args.source}")
        if args.run == "auto":
            stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
            base = RUNS / stamp
            suffix = "" if args.mode == "default" else "-" + args.mode
            n = 1
            while True:  # never reuse an existing run folder, even within the same second
                run = Path(f"{base}{'' if n == 1 else '-' + str(n)}{suffix}")
                try:
                    run.mkdir(parents=True)
                    break
                except FileExistsError:
                    n += 1
            run = run.resolve()
        else:
            run = run_folder(args.run)
    except ValueError as exc:
        return refuse(str(exc))
    skip = set() if args.all else handled_ids()
    (run / "sources").mkdir(parents=True, exist_ok=True)
    items, skipped = [], []
    for path in sorted((p for p in Path(args.source).iterdir() if p.is_file() and not p.name.startswith(".")), key=lambda p: p.name):
        item_id = path.stem
        if item_id in skip:
            skipped.append(item_id)
            continue
        text, how = extract(path)
        entry = {"id": item_id, "file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                 "extractor": how, "text": None}
        if text is not None:
            out = run / "sources" / f"{item_id}.txt"
            out.write_text(text, encoding="utf-8", newline="\n")
            entry["text"] = f"sources/{item_id}.txt"
            entry["chars"] = len(text)
        items.append(entry)
    try:
        shown = Path(os.path.relpath(run, Path.cwd())).as_posix()
        shown = shown if not shown.startswith("..") else str(run)
    except ValueError:  # different drive on Windows
        shown = str(run)
    manifest = {"run": run_name(run), "run_dir": shown, "mode": args.mode, "source": str(args.source), "all": args.all,
                "items": items, "skipped_processed": skipped}
    if args.run == "auto" and not any(i["text"] for i in items):
        shutil.rmtree(run, ignore_errors=True)   # nothing new: leave no empty run folder behind
        manifest["run_dir"] = None
        print(json.dumps(manifest, indent=2))
        return 0
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log_run({"run": run_name(run), "manifest_sha256": sha256_file(run / "manifest.json"),
             "at": dt.datetime.now().isoformat(timespec="seconds")})
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
