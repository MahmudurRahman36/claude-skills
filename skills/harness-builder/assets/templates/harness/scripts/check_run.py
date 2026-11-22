#!/usr/bin/env python3
"""Deterministic sensor for the {{SLUG}} harness. Stdlib only. Code, not an LLM, checks:
  - every item has its source text, and every quote really appears in it
    (whitespace, case and punctuation tolerant; "..." joins fragments)
  - scores are numbers on the rubric scale; weighted score maths (when weights exist)
  - the verdict follows the rubric's verdict rules (when rules exist)
  - red_flag_patterns found in the source are reported as red flags (cheap injection check)
With --golden it also compares the run to the owner-labelled golden set (regression eval):
expected_verdict, expected_scores, expected_fields and expected_red_flag.

Usage:
  python {{HARNESS_DIR}}/scripts/check_run.py --scorecards <run>/scorecards.json
         [--rubric {{HARNESS_DIR}}/context/rubric.md] [--golden {{HARNESS_DIR}}/evals/golden.json]
Exit 0 = clean, 1 = issues found, 2 = bad input.
"""
import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RUNS = HERE / "runs"
RUN_LOG = HERE / "memory" / "run-log.jsonl"
CONFIG_RE = re.compile(r"```json harness-config\s*\n(.*?)\n```", re.S)


# Shared by ingest.py and render_report.py: run folders stay inside runs/, and the files code wrote
# are logged in memory/ (which a run cannot write to), so a run can't forge them later.
def run_folder(raw):
    """Resolve a run folder; raise ValueError unless it is inside runs/."""
    run = Path(raw).resolve()
    if RUNS.resolve() not in run.parents:
        raise ValueError(f"run folder must be inside {RUNS}: {raw}")
    return run


def run_name(run):
    return run.relative_to(RUNS.resolve()).as_posix()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def log_run(entry):
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry) + "\n")


def logged(name, key):
    """The latest value of `key` logged for run `name`, or None."""
    value = None
    if RUN_LOG.is_file():
        for line in RUN_LOG.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("run") == name and key in entry:
                value = entry[key]
    return value


def norm(text):
    text = unicodedata.normalize("NFKC", str(text)).lower()
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                         "–": "-", "—": "-"}))
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)        # 1,000 == 1000
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)      # keep only decimal points
    text = re.sub(r"[^\w%$.+/-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_found(quote, source):
    parts = [norm(p) for p in re.split(r"\.\.\.|…", quote) if norm(p)]
    pos, src = 0, norm(source)
    for part in parts:
        idx = src.find(part, pos)
        if idx < 0:
            return False
        pos = idx + len(part)
    return bool(parts)


def load_config(path):
    m = CONFIG_RE.search(path.read_text(encoding="utf-8")) if path.is_file() else None
    return json.loads(m.group(1)) if m else {}


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def expected_verdict(cfg, scores, weighted, red_flags):
    """First matching rule wins. Rules are listed best first; the last should have no conditions."""
    for rule in cfg.get("verdicts", []):
        if "min_score" in rule and (weighted is None or weighted < rule["min_score"]):
            continue
        if any(scores.get(c, 0) < v for c, v in rule.get("min_criteria", {}).items()):
            continue
        if any(scores.get(c, 99) <= v for c, v in rule.get("fail_if_at_most", {}).items()):
            continue
        if (rule.get("no_red_flags") or rule.get("no_flags")) and red_flags:
            continue
        return rule["name"]
    return None


def read_source(item, base, issues):
    sp = item.get("source_text")
    if not sp:
        issues.append("no source_text: evidence cannot be checked")
        return None
    p = Path(sp) if Path(sp).is_absolute() else base / sp
    if not p.is_file():
        issues.append(f"source text missing: {sp}")
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def check_item(item, cfg, base):
    issues = []
    source = read_source(item, base, issues)
    crit = cfg.get("criteria", [])
    weights = {c["id"]: c["weight"] for c in crit if is_num(c.get("weight"))}
    scale = cfg.get("scale", [])
    floor = min(scale) if scale else None
    numeric = {}
    for cid, entry in (item.get("scores") or {}).items():
        entry = entry if isinstance(entry, dict) else {"score": entry}
        score, quote = entry.get("score"), str(entry.get("quote") or "").strip()
        if not is_num(score):
            issues.append(f"{cid}: score {score!r} is not a number")
            continue
        numeric[cid] = score
        if scale and score not in scale:
            issues.append(f"{cid}: score {score} not on scale {scale}")
        if quote:
            if source is not None and not quote_found(quote, source):
                issues.append(f"{cid}: quote not found in source: \"{quote[:80]}\"")
        elif floor is not None and score != floor:
            issues.append(f"{cid}: score {score} has no quote (no quote, no score)")
    for claim in item.get("claims", []):
        q = str(claim.get("quote") or "").strip()
        if not q:
            issues.append(f"claim without quote: {claim.get('field') or claim.get('text', '')[:40]}")
        elif source is not None and not quote_found(q, source):
            issues.append(f"claim quote not found in source: \"{q[:80]}\"")
    red_flags = item.get("red_flags") or []
    if source is not None:
        for pat in cfg.get("red_flag_patterns", []):
            if re.search(pat, source, re.I) and not red_flags:
                issues.append(f"source matches red_flag_pattern {pat!r} but item has no red flag")
    weighted = None
    if crit:
        missing = {c["id"] for c in crit} - set(numeric)
        if missing:
            issues.append(f"criteria not scored: {sorted(missing)}")
    if weights:
        weighted = round(sum(numeric.get(c, 0) * w for c, w in weights.items()) + 1e-9, 1)
        reported = item.get("weighted")
        if is_num(reported) and abs(reported - weighted) > 0.05:
            issues.append(f"weighted score {reported} should be {weighted}")
        elif reported is not None and not is_num(reported):
            issues.append(f"weighted score {reported!r} is not a number")
    want = expected_verdict(cfg, numeric, weighted, red_flags) if cfg.get("verdicts") else None
    human = cfg.get("human_review_verdict", "Needs a human look")
    if want and item.get("verdict") not in (want, human):
        issues.append(f"verdict '{item.get('verdict')}' should be '{want}' by the rubric rules")
    claimed = {c.get("field") for c in item.get("claims", []) if str(c.get("quote") or "").strip()}
    for spec in cfg.get("fields", []):
        spec = spec if isinstance(spec, dict) else {"name": spec}
        name = spec.get("name")
        if name not in item:
            issues.append(f"missing field '{name}'")
            continue
        allowed = spec.get("allowed")
        if allowed and item[name] not in allowed:
            issues.append(f"field '{name}' = {item[name]!r} is not one of {allowed}")
        if spec.get("evidence") and name not in claimed:
            issues.append(f"field '{name}' has no claim with a quote (no quote, no label)")
    return issues


def golden_diff(cases, items, fields):
    got = {str(i.get("id")): i for i in items}
    diffs = []
    for case in cases:
        cid = str(case.get("id"))
        run = got.get(cid)
        if run is None:
            diffs.append({"id": cid, "problem": "not in this run"})
            continue
        problems = []
        if case.get("expected_verdict") and run.get("verdict") != case["expected_verdict"]:
            problems.append(f"verdict {run.get('verdict')!r} != {case['expected_verdict']!r}")
        for c, v in (case.get("expected_scores") or {}).items():
            s = (run.get("scores") or {}).get(c)
            s = s.get("score") if isinstance(s, dict) else s
            if s != v:
                problems.append(f"{c} {s!r} != {v!r}")
        for f, v in (case.get("expected_fields") or {}).items():
            if run.get(f) != v:
                problems.append(f"{f} {run.get(f)!r} != {v!r}")
        if "expected_red_flag" in case and bool(run.get("red_flags")) != bool(case["expected_red_flag"]):
            problems.append(f"red flag expected={case['expected_red_flag']} got={bool(run.get('red_flags'))}")
        if problems:
            diffs.append({"id": cid, "problems": problems})
    return {"matched": len(cases) - len(diffs), "total": len(cases), "diffs": diffs}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic checks for {{SLUG}} runs")
    ap.add_argument("--scorecards", required=True)
    ap.add_argument("--rubric", default=str(HERE / "context" / "rubric.md"))
    ap.add_argument("--golden")
    args = ap.parse_args(argv)
    manifest = Path(args.scorecards).resolve().parent / "manifest.json"
    if not args.golden and manifest.is_file():
        try:
            if json.loads(manifest.read_text(encoding="utf-8")).get("mode") == "golden":
                args.golden = str(HERE / "evals" / "golden.json")
        except json.JSONDecodeError:
            pass
    try:
        data = json.loads(Path(args.scorecards).read_text(encoding="utf-8"))
        cfg = load_config(Path(args.rubric))
        cases = json.loads(Path(args.golden).read_text(encoding="utf-8")) if args.golden else None
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        print(json.dumps({"ok": False, "error": "scorecards must be a list or {\"items\": [...]}"}))
        return 2
    base = Path(args.scorecards).resolve().parent
    report = {"ok": True, "items": [], "golden": None}
    for item in items:
        if not isinstance(item, dict):
            report["items"].append({"id": None, "issues": ["item is not an object"]})
            report["ok"] = False
            continue
        issues = check_item(item, cfg, base)
        report["items"].append({"id": item.get("id"), "issues": issues})
        report["ok"] &= not issues
    if cases is not None:
        report["golden"] = golden_diff(cases, items, cfg.get("fields", []))
        report["ok"] &= not report["golden"]["diffs"]
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
