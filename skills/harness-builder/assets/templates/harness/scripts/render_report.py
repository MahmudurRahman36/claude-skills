#!/usr/bin/env python3
"""Render the {{SLUG}} report from scorecards.json. Stdlib only.

Code writes the report, so its shape is identical every run, and the run never depends on a
model being allowed to write a report file.

Usage:
  python {{HARNESS_DIR}}/scripts/render_report.py --run <run folder> [--golden {{HARNESS_DIR}}/evals/golden.json]
Writes <run folder>/report.md and prints its path. The run folder must be inside runs/. The hashes
of report.md and scorecards.json go to memory/run-log.jsonl, which is how ingest.py --mark-processed
knows the report came from code. Exit 0 ok, 2 bad input.
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_run import golden_diff, load_config, log_run, run_folder, run_name, sha256_file  # noqa: E402


def cell(value):
    return str(value if value not in (None, "") else "-").replace("|", "/").replace("\n", " ")


def field_names(cfg):
    return [f["name"] if isinstance(f, dict) else f for f in cfg.get("fields", [])]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render the {{SLUG}} report")
    ap.add_argument("--run", required=True)
    ap.add_argument("--golden")
    args = ap.parse_args(argv)
    try:
        run = run_folder(args.run)
        data = json.loads((run / "scorecards.json").read_text(encoding="utf-8"))
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8")) if (run / "manifest.json").is_file() else {}
    except (OSError, ValueError) as exc:   # json.JSONDecodeError is a ValueError
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    items = data.get("items", []) if isinstance(data, dict) else data
    cfg = load_config(HERE / "context" / "rubric.md")
    fields = field_names(cfg)
    weighted = any(isinstance(c, dict) and "weight" in c for c in cfg.get("criteria", []))
    human = cfg.get("human_review_verdict", "Needs a human look")
    top = cfg["verdicts"][0]["name"] if len(cfg.get("verdicts", [])) > 1 else None
    cal = HERE / "memory" / "calibration.md"
    pend = HERE / "memory" / "pending.md"
    waiting = len(re.findall(r"^\d{4}-\d\d-\d\d \|", pend.read_text(encoding="utf-8"), re.M)) if pend.is_file() else 0
    notes = len(re.findall(r"^- \[\d{4}-\d\d-\d\d\]", cal.read_text(encoding="utf-8"), re.M)) if cal.is_file() else 0
    mode = manifest.get("mode") or (run.name.rsplit("-", 1)[-1] if run.name.endswith(("-golden", "-rerun", "-from")) else "default")
    if mode == "golden" and not args.golden and (HERE / "evals" / "golden.json").is_file():
        args.golden = str(HERE / "evals" / "golden.json")

    out = [f"# {{SLUG}} report: {run.name}", "",
           f"Run: {run.name} | Mode: {mode} | Items: {len(items)} | Skipped (already processed): "
           f"{len(manifest.get('skipped_processed', []))} | Waiting for a human (memory/pending.md): {waiting} | "
           f"Calibration notes applied: {notes} | "
           f"Rendered: {dt.datetime.now():%Y-%m-%d %H:%M}", "", "## Summary", ""]
    head = ["#", "Item"] + (["Score"] if weighted else []) + fields + ["Verdict", "Verifier", "Reason"]
    out += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for n, it in enumerate(items, 1):
        row = [n, it.get("id")] + ([it.get("weighted")] if weighted else []) + [it.get(f) for f in fields]
        row += [it.get("verdict"), (it.get("verifier") or {}).get("status"), it.get("reason")]
        out.append("| " + " | ".join(cell(v) for v in row) + " |")

    attention = [it for it in items if it.get("verdict") in (top, human) or it.get("red_flags") or it.get("uncertain")
                 or (it.get("verifier") or {}).get("status") not in (None, "Confirmed")]
    out += ["", f"## Details ({len(attention)} item(s) needing attention)", ""]
    for it in attention:
        out += [f"### {it.get('id')} | {cell(it.get('verdict'))}", ""]
        if it.get("scores"):
            out += ["| Criterion | Score | Evidence (direct quote) |", "|---|---|---|"]
            for cid, s in it["scores"].items():
                s = s if isinstance(s, dict) else {"score": s}
                out.append(f"| {cid} | {cell(s.get('score'))} | \"{cell(s.get('quote'))}\" |")
            out.append("")
        if it.get("claims"):
            out += ["| Field | Value | Evidence (direct quote) |", "|---|---|---|"]
            for c in it["claims"]:
                out.append(f"| {cell(c.get('field'))} | {cell(it.get(c.get('field')))} | \"{cell(c.get('quote'))}\" |")
            out.append("")
        v = it.get("verifier") or {}
        out += [f"- Red flags: {'; '.join(map(str, it.get('red_flags') or [])) or 'None'}",
                f"- Uncertain: {'; '.join(map(str, it.get('uncertain') or [])) or 'None'}",
                f"- Verify with a human: {'; '.join(map(str, it.get('human_checks') or [])) or 'None'}",
                f"- Verifier: {cell(v.get('status'))}{'; ' + str(v.get('notes')) if v.get('notes') else ''}",
                f"- Confidence: {cell(it.get('confidence'))}", ""]

    needs = [f"- {i['id']}: no text could be extracted ({i.get('file')}). A human must read it."
             for i in manifest.get("items", []) if not i.get("text")]
    needs += [f"- {it.get('id')}: {human}" for it in items if it.get("verdict") == human]
    out += ["## Needs a decision", ""] + (needs or ["Nothing."])
    if args.golden:
        g = golden_diff(json.loads(Path(args.golden).read_text(encoding="utf-8")), items, cfg.get("fields", []))
        out += ["", "## Golden set", "", f"Matched {g['matched']}/{g['total']}."]
        out += [f"- {d['id']}: {'; '.join(d.get('problems') or [d.get('problem', '')])}" for d in g["diffs"]]
    (run / "report.md").write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    log_run({"run": run_name(run), "report_sha256": sha256_file(run / "report.md"),
             "scorecards_sha256": sha256_file(run / "scorecards.json"), "at": dt.datetime.now().isoformat(timespec="seconds")})
    print(json.dumps({"ok": True, "report": str(run / "report.md")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
