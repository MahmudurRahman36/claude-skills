#!/usr/bin/env python3
"""orch_deploy - the deploy gate (`orch deploy --check`) and the live check (`orch live`). orch never deploys.

Spec: dev/v2-design.md section 8, dev/v2-contract.md "orch_deploy.py".

deploy --check runs seven gates in order and prints a line for each failing or warning gate only,
`FAIL|warn <gate>: <detail>`, then one summary line (`NOT ready: 2 failed, 3 skipped, 2 ok (7 gates)`), then ONE JSON
line {"ready", "gates" (all seven, each with its fix), "next", "ref", "sha"}, plus "ask" (the setup question to relay,
answer_key deploy or production) when the production gate fails on a "later" or missing answer. A deploy command
without a health URL is a warning (orch live will have nothing to check). Exit 0 = ready (next = "run the deploy
command: <command>, then orch live"), 7 = a gate is not met (next = the first failing gate's fix), 2 = usage (no
--check, a bad --ref, an unreadable project folder). Git is read locally and never fetched. Production hosts get
`ssh ... true` through orch_discover.probe_hosts (docker=False), all in parallel, and only for an approved setup.

live GETs every saved health URL in parallel (10 s timeout, 3 tries, 2 s then 4 s apart); 2xx and 3xx are up and a
redirect is never followed; certificates are verified unless the entry is {"url": ..., "insecure": true}. One line per
URL, then ONE JSON line {"ok", "checks", "next"}. Exit 0 = all up, 1 = any down, 2 = no health URL saved (then the
JSON line carries the deploy question as "ask" and next "orch init --ask deploy") or no setup.

Stdlib only. Git goes through run= (subprocess.run signature) and ssh through probe= (default probe_production), so
unit tests never touch ssh or a remote network. LAST, or details(), holds the last result for the project journal: a
gate failure adds reason (the first failing gate) and fix (next); a live failure adds class "live-down", result
"down", reason (each failing URL and its error) and fix (next); a live without a health URL adds fix and ask.
"""
import base64
import concurrent.futures as cf
import copy
import hashlib
import http.client
import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import orch_config as oc
import orch_discover as od

GATES = ("setup approved", "production answered", "tree clean", "HEAD pushed", "required jobs", "scripts unchanged",
         "production reachable")
GATE_EXIT = 7  # orch.py GATE: a deploy gate is not met
NOT_DEPLOY = "orch does not deploy: run orch deploy --check, then the deploy command saved in the setup"
NO_FETCH = "last fetched state; orch does not fetch"
FIX_INIT = "orch init (finish the setup and approve it, only after the user says yes)"
FIX_PROD = ("ask the user which servers this project deploys to (relay the ask; orch init --ask production prints "
            "it again), save the answer with orch init --apply under the key production, then run orch deploy --check "
            "again")
FIX_CMD = ("ask the user for the deploy command (relay the ask; orch init --ask deploy prints it again), save it with "
           "orch init --apply under the key deploy, run orch init --approve once the user says yes (a new deploy "
           "command needs approval), then run orch deploy --check again")
FIX_HEALTH = ("ask the user for the health URL(s) to check after a deploy (orch init --ask deploy prints the "
              "question), save the answer with orch init --apply under the key deploy (the command stays)")
LIVE_TIMEOUT, LIVE_TRIES, LIVE_BACKOFF = 10, 3, (2, 4)  # seconds per try, tries per URL, pauses between tries
LIVE_READ = 65536  # body bytes read from an answer before the connection is closed
WORD = {"ok": "ok", "fail": "FAIL", "warn": "warn", "skip": "skip"}
LAST: dict = {}  # the last cmd_deploy / cmd_live result, for the project journal (see details())


def details() -> dict:
    """The last cmd_deploy / cmd_live result ({"verb", "exit", "gates" | "checks", ...}); {} before the first call."""
    return copy.deepcopy(LAST)


def _where(start=None, env=None) -> str:
    """The folder a message names: --project (start) wins over ORCH_PROJECT, then the current folder (as
    orch_config.find_project)."""
    e = os.environ if env is None else env
    return Path(start or e.get("ORCH_PROJECT") or os.getcwd()).expanduser().resolve().as_posix()


def _usage(verb, msg, nxt) -> int:
    print(f"orch: usage: {msg}", file=sys.stderr, flush=True)
    print(json.dumps({"result": "usage", "exit": 2, "error": msg, "next": nxt}), flush=True)
    LAST.clear()
    LAST.update(verb=verb, exit=2, error=msg)
    return 2


def _plural(n, word) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# ----------------------------------------------------------------------------- gates: shapes and output
def _gate(name, status, detail, fix="") -> dict:
    """status ok|fail|warn|skip; ok = the gate does not block (a warning never blocks, a skipped gate does)."""
    return {"name": name, "ok": status in ("ok", "warn"), "status": status, "detail": detail, "fix": fix}


def gate_line(g, fix=True) -> str:
    """`ok   setup approved: approved 2026-10-30 09:20`; with fix, a failing or warning line ends with ` | fix: <fix>`
    unless the detail already names the fix."""
    line = f"{WORD[g['status']]:<4} {g['name']}: {g['detail']}"
    if fix and g["status"] in ("fail", "warn") and g["fix"] and g["fix"] not in g["detail"]:
        line += f" | fix: {g['fix']}"
    return line


def summary_line(res) -> str:
    """`NOT ready: 2 failed, 3 skipped, 2 ok (7 gates)` / `ready: 1 warning, 6 ok (7 gates)`: the one human line after
    the failing and warning gates (their fixes, and next, are in the JSON line only)."""
    count = {s: sum(1 for g in res["gates"] if g["status"] == s) for s in ("fail", "warn", "skip", "ok")}
    words = {"fail": "failed", "warn": "warning", "skip": "skipped", "ok": "ok"}
    parts = [f"{n} {words[s]}" + ("s" if s == "warn" and n > 1 else "") for s, n in count.items() if n]
    return f"{'ready' if res['ready'] else 'NOT ready'}: {', '.join(parts)} ({len(res['gates'])} gates)"


# ----------------------------------------------------------------------------- git (local only, never fetch)
def _git(run, cwd, *args, timeout=30) -> tuple:
    """(rc, stdout, stderr) of one local git command; rc 127 when git is missing, hangs or cannot start."""
    try:
        r = run(["git", "-C", str(cwd), *args], capture_output=True, encoding="utf-8", errors="replace",
                timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", f"git did not run ({e}); install git or put it on PATH"
    return r.returncode, r.stdout or "", r.stderr or ""


def _last(text, default="no error text") -> str:
    lines = [x.strip() for x in str(text or "").splitlines() if x.strip()]
    return lines[-1] if lines else default


def _porcelain(out) -> list:
    """`git status --porcelain -z` -> paths; a rename or copy entry is followed by its old path, which is skipped."""
    files, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        e, i = parts[i], i + 1
        if len(e) > 3:
            files.append(e[3:])
            if "R" in e[:2] or "C" in e[:2]:
                i += 1
    return files


def _job_repo(j) -> str:
    r = str((j or {}).get("repo") or ".").replace("\\", "/").strip().strip("/")
    while r.startswith("./"):
        r = r[2:]
    return r or "."


def _repo_state(path, key, ref, run) -> dict:
    """What the gates need from one repository: toplevel, the commit of ref, changed files, branch, upstream and how
    far the commit is ahead of / behind it (as of the last fetch), or the remote branches holding a detached commit."""
    st = {"key": key, "path": Path(path), "top": None, "error": None, "error_fix": "", "dirty": None,
          "status_err": None, "sha": None, "remote": None, "branch": None, "upstream": None, "ahead": 0, "behind": 0,
          "on": []}
    if not Path(path).is_dir():
        st["error"] = "does not exist"
        st["error_fix"] = f"fix the repo of the jobs that use {key} (orch init), or put that repository back"
        return st
    rc, out, err = _git(run, path, "rev-parse", "--show-toplevel")
    if rc:
        nogit = rc != 127 and "not a git repository" in err.lower()
        st["error"] = "is not a git repository" if nogit else f"cannot be read by git: {_last(err)}"
        st["error_fix"] = ("put it under git, commit and push it: git init, git add -A, git commit -m <message>, "
                           "git remote add origin <url>, git push -u origin main" if nogit
                           else f"run git -C {Path(path).as_posix()} status to see the full error and fix it")
        return st
    st["top"] = out.strip()
    rc, out, err = _git(run, path, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=normal",
                        "--ignore-submodules=none")
    if rc == 0:
        st["dirty"] = _porcelain(out)
    else:
        st["status_err"] = _last(err)
    rc, out, _ = _git(run, path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    st["sha"] = out.strip() if rc == 0 and out.strip() else None
    rc, out, _ = _git(run, path, "remote")
    remotes = out.split() if rc == 0 else []
    st["remote"] = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
    if ref == "HEAD":
        rc, out, _ = _git(run, path, "symbolic-ref", "-q", "--short", "HEAD")
        st["branch"] = out.strip() if rc == 0 and out.strip() else None
    elif _git(run, path, "show-ref", "--verify", "--quiet", f"refs/heads/{ref}")[0] == 0:
        st["branch"] = ref
    if not st["sha"]:
        return st
    if st["branch"]:
        rc, out, _ = _git(run, path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{st['branch']}@{{upstream}}")
        st["upstream"] = out.strip() if rc == 0 and out.strip() else None
    if st["upstream"]:
        rc, out, _ = _git(run, path, "rev-list", "--left-right", "--count", f"{st['upstream']}...{st['sha']}")
        nums = out.split()
        if rc == 0 and len(nums) == 2 and all(x.isdigit() for x in nums):
            st["behind"], st["ahead"] = int(nums[0]), int(nums[1])
        else:
            st["upstream"] = None  # configured but not fetched yet: reported as no upstream
    elif not st["branch"]:
        rc, out, _ = _git(run, path, "branch", "-r", "--contains", st["sha"], "--format=%(refname:short)")
        st["on"] = [x for x in out.split() if not x.endswith("/HEAD") and x not in remotes] if rc == 0 else []
    return st


def _states(p, cfg, ref, run) -> dict:
    """{repo key: state} for the project root and every repo a job names; keys inside one work tree share a state.
    A root that is not a git repository is left out when every job names a nested repo (a folder of repos)."""
    jobs = (cfg or {}).get("jobs") or {}
    used = {_job_repo(j) for j in jobs.values() if isinstance(j, dict)}
    keys = ["."] + sorted(used - {"."})
    if not p.git and "." not in used and len(keys) > 1:
        keys.remove(".")
    out, seen = {}, {}
    for k in keys:
        st = _repo_state(Path(p.root) if k == "." else Path(p.root) / k, k, ref, run)
        top = os.path.normcase(os.path.abspath(st["top"])) if st["top"] else None
        if top and top in seen:
            out[k] = seen[top]
            continue
        out[k] = st
        if top:
            seen[top] = st
    return out


def _uniq(states) -> list:
    return list({id(s): s for s in states.values()}.values())


def _who(st, root) -> str:
    return Path(root).as_posix() if st["key"] == "." else st["key"]


def _gitc(st) -> str:
    return "git " if st["key"] == "." else f"git -C {shlex.quote(st['key'])} "


# ----------------------------------------------------------------------------- the seven gates
def _g_setup(cfg, cfg_err, trust, trust_err) -> dict:
    n = GATES[0]
    if cfg_err or trust_err:
        return _gate(n, "fail", cfg_err or trust_err, FIX_INIT)
    if not trust:
        return _gate(n, "fail", "this setup was never approved", FIX_INIT)
    when = str(trust.get("approved_at") or "?")[:16].replace("T", " ")
    if trust.get("hash") != oc.config_hash(cfg):
        return _gate(n, "fail", f"the setup changed after its approval on {when}", FIX_INIT)
    return _gate(n, "ok", f"approved {when}")


def _prod_hosts(cfg) -> list:
    """[(name, ssh spec)] of production hosts: production-only first, then helpers that are also production."""
    hosts = {n: h for n, h in ((cfg or {}).get("hosts") or {}).items()
             if isinstance(h, dict) and "prod" in (h.get("roles") or [])}
    only = [n for n, h in hosts.items() if "helper" not in (h.get("roles") or [])]
    return [(n, str(hosts[n].get("ssh") or "?")) for n in only + [n for n in hosts if n not in only]]


def _labels(hosts) -> str:
    return ", ".join(f"{n} ({s})" for n, s in hosts)


def _g_production(cfg, cfg_err) -> dict:
    n = GATES[1]
    if cfg_err:
        return _gate(n, "skip", "not checked: the setup cannot be read")
    # Production = every host with the prod role, helper + production hosts included; the production answer only
    # covers the extra production-only servers, so "none" there still deploys to a helper + production host.
    ans = (cfg.get("production") or {}).get("answer")
    hosts = _prod_hosts(cfg)
    if not hosts:
        if ans is None:
            return _gate(n, "fail", "production is not answered yet", FIX_PROD)
        if ans == "later":
            return _gate(n, "fail", 'production is "later": the production servers are not decided yet', FIX_PROD)
        return _gate(n, "fail", "no host has the production role, so there is nothing to deploy to", FIX_PROD)
    dep = cfg.get("deploy") if isinstance(cfg.get("deploy"), dict) else {}
    if dep.get("answer") == "later":
        return _gate(n, "fail", f'{_labels(hosts)}; the deploy command is "later": not decided yet', FIX_CMD)
    if not str(dep.get("command") or "").strip():
        return _gate(n, "fail", f"{_labels(hosts)}; no deploy command is saved", FIX_CMD)
    if not health_targets(cfg):  # a warning: the deploy may go ahead, but orch live will have nothing to check
        return _gate(n, "warn", f"{_labels(hosts)}; deploy command {dep['command']}; no health URL is saved, so orch "
                                f"live cannot check the app after the deploy", FIX_HEALTH)
    return _gate(n, "ok", f"{_labels(hosts)}; deploy command {dep['command']}")


def ask_for(cfg, key) -> dict:
    """The setup step's deploy or production lines on their own (same text and [current value] as the setup
    question), as an init-style ask with answer_key and example: what a "later" or missing answer needs before a
    deploy. With a deploy command saved but no health URL, the deploy ask is the health URL line alone (what orch
    live needs; "later" would drop the saved command). The example is exactly what the [brackets] say, so a plain
    "ok" saves no value the user did not give. orch deploy --check and orch live put it in their JSON line; orch init
    --ask deploy|production prints it."""
    cfg = cfg or {}
    if key == "deploy":
        dep = cfg.get("deploy") if isinstance(cfg.get("deploy"), dict) else {}
        dep = {} if dep.get("answer") == "later" else dep
        cmd, health, rb = str(dep.get("command") or "").strip(), list(dep.get("health") or []), dep.get("rollback")
        if cmd and not health:
            lines = ["No health URL is saved, so nothing checks the app after a deploy (defaults in [brackets]; reply "
                     "with the URL(s), or ok for none):",
                     "Health URL(s) checked after a deploy, comma separated [-]"]
            return oc._ask("text", "Health URL", "\n".join(lines), answer_key="deploy",
                           example={"deploy": {"health": []}})
        lines = ["A deploy needs the deploy command (defaults in [brackets]; reply with the command, or later):",
                 f"Deploy command - runs on THIS machine and must reach production itself, e.g. ./deploy.sh "
                 f"[{cmd or ('-' if health or rb else 'later')}]",
                 f"Health URL(s) checked after a deploy [{', '.join(oc.health_label(h) for h in health) or '-'}]",
                 f"Rollback command (optional) [{rb or '-'}]"]
        if cmd:
            ex = {"deploy": {"command": cmd, "health": health, "rollback": rb}}
        elif health or rb:  # saved without a command (a hand edit): "ok" keeps them, and orch asks for the command
            ex = {"deploy": {"health": health, "rollback": rb}}
        else:
            ex = {"deploy": "later"}
        return oc._ask("text", "Deploy", "\n".join(lines), answer_key="deploy", example=ex)
    if key == "production":
        hosts = cfg.get("hosts") or {}
        only = [str(h.get("ssh")) for h in hosts.values() if isinstance(h, dict) and h.get("ssh")
                and (h.get("roles") or []) == ["prod"]]
        ans = (cfg.get("production") or {}).get("answer")
        word = ans if ans in ("none", "later") else "none"
        lines = ["A deploy needs the production servers this project deploys to:",
                 f"Production servers (what you type after ssh, comma separated), or none / later "
                 f"[{', '.join(only) or word}]"]
        return oc._ask("text", "Production", "\n".join(lines), answer_key="production",
                       example={"production": only or word})
    raise ValueError(f"ask_for: key must be deploy or production (got {key!r})")


def _g_clean(states, root) -> dict:
    n, uniq = GATES[2], _uniq(states)
    multi = len(uniq) > 1
    bad, fixes = [], []
    for st in uniq:
        pre = f"{_who(st, root)}: " if multi else ""
        if st["error"]:
            bad.append(f"{_who(st, root)} {st['error']}")
            fixes.append(st["error_fix"])
        elif st["dirty"] is None:
            bad.append(f"{pre}git status failed: {st['status_err']}")
            fixes.append(f"run {_gitc(st)}status and fix what it reports")
        elif st["dirty"]:
            d = st["dirty"]
            more = f" and {len(d) - 5} more" if len(d) > 5 else ""
            bad.append(f"{pre}{len(d)} uncommitted or untracked {'file' if len(d) == 1 else 'files'}: "
                       f"{', '.join(d[:5])}{more}")
            fixes.append(f"commit or stash them ({_gitc(st)}status lists them), then run orch deploy --check again")
    if bad:
        return _gate(n, "fail", "; ".join(bad), fixes[0])
    return _gate(n, "ok", "nothing uncommitted or untracked"
                 + (f" in {', '.join(_who(s, root) for s in uniq)}" if multi else ""))


def _g_pushed(states, root, ref) -> dict:
    n = GATES[3]
    uniq = [s for s in _uniq(states) if not s["error"]]
    if not uniq:
        return _gate(n, "skip", "not checked: no git repository (see tree clean)")
    multi = len(_uniq(states)) > 1
    good, bad = [], []
    for st in uniq:
        pre, c = (f"{_who(st, root)}: " if multi else ""), _gitc(st)
        remote, branch = st["remote"] or "origin", st["branch"]
        short = (st["sha"] or "")[:7]
        if not st["sha"]:
            if ref == "HEAD":
                bad.append((f"{pre}no commit yet", f"commit the project ({c}commit), then push it "
                                                   f"({c}push -u {remote} {branch or 'main'})"))
            else:
                bad.append((f"{pre}{ref} is not a commit here", "pass a branch, tag or commit that exists (git log "
                                                                "--oneline -5 lists recent ones), or leave out --ref"))
        elif branch and not st["upstream"]:
            if st["remote"]:
                fx = f"{c}push -u {remote} {branch}"
                bad.append((f"{pre}no upstream branch: push first ({fx})", fx))
            else:
                fx = f"{c}remote add origin <url>, then {c}push -u origin {branch}"
                bad.append((f"{pre}no upstream branch: push first (there is no git remote yet: {fx})", fx))
        elif st["upstream"] and st["ahead"]:
            fx = f"{c}push" if ref == "HEAD" else f"{c}push {st['upstream'].split('/')[0]} {branch}"
            also = f", which also has {_plural(st['behind'], 'newer commit')}" if st["behind"] else ""
            if also and ref == "HEAD":  # diverged: a plain push is refused until the newer commits are in
                fx = f"{c}pull --rebase, then {c}push"
            bad.append((f"{pre}{_plural(st['ahead'], 'commit')} not pushed to {st['upstream']}{also} ({NO_FETCH})",
                        fx))
        elif st["upstream"]:
            newer = f", which has {_plural(st['behind'], 'newer commit')}" if st["behind"] else ""
            good.append(f"{pre}{short} is on {st['upstream']}{newer}")
        elif st["on"]:
            good.append(f"{pre}{short} is on {st['on'][0]}")
        else:
            what = f"HEAD is detached at {short}" if ref == "HEAD" else f"{ref} ({short})"
            bad.append((f"{pre}{what} and that commit is on no remote branch ({NO_FETCH})",
                        f"push it from a branch: {c}switch -c <branch>, then {c}push -u {remote} <branch>"))
    if bad:
        return _gate(n, "fail", "; ".join(b[0] for b in bad), bad[0][1])
    return _gate(n, "ok", "; ".join(good) + f" ({NO_FETCH})")


def _job_stage(j) -> str:
    """The stage the engine gives the job (orch_config compile_engine: a build job is always "image")."""
    return "image" if j.get("kind") == "build" else (j.get("stage") or "unit")


def _deploy_require(cfg) -> list:
    v = ((cfg or {}).get("policy") or {}).get("deploy_require", oc.DEFAULT_POLICY["deploy_require"])
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list):
        v = oc.DEFAULT_POLICY["deploy_require"]
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def _g_jobs(p, cfg, cfg_err, states, root, ref) -> dict:
    n = GATES[4]
    if cfg_err:
        return _gate(n, "skip", "not checked: the setup cannot be read")
    req = _deploy_require(cfg)
    if not req:
        return _gate(n, "ok", "policy.deploy_require is empty: no stage needs a pass")
    pol = cfg.get("policy") or {}
    digest = pol.get("require_digest", oc.DEFAULT_POLICY["require_digest"])
    jobs = {k: j for k, j in (cfg.get("jobs") or {}).items() if isinstance(j, dict) and _job_stage(j) in req}
    if not jobs:
        return _gate(n, "warn", f"no saved job has a stage in {', '.join(req)}, so nothing was tested for this commit",
                     "orch init --detect (proposes jobs; save them with the user)")
    shas = {}
    for k, j in jobs.items():
        st = states.get(_job_repo(j)) or {}
        if st.get("error") or not st.get("sha"):
            why = (f"{_who(st, root)} {st['error']}" if st.get("error") else f"{ref} is not a commit in "
                   f"{_who(st, root) if st else _job_repo(j)}")
            return _gate(n, "skip", f"not checked: {why}")
        shas[k] = st["sha"]
    runs = oc.read_runs(p)
    passed, missing, unpinned = [], [], []
    for k, j in jobs.items():
        sha, short = shas[k], shas[k][:7]
        mine = [r for r in runs if r.get("job") == k]
        for_sha = [r for r in mine if r.get("sha") == sha]
        clean = [r for r in for_sha if r.get("wt") is False]
        if any(r.get("result") == "pass" for r in clean):
            passed.append(k)
            continue
        img = str(j.get("image") or "")
        if img and digest and "@sha256:" not in img:
            unpinned.append(k)
            why = "cannot run until its image is pinned to a digest"
        elif clean:
            why = f"{clean[-1].get('result') or 'no result'} for {short}"
        elif for_sha:
            why = f"only --wt runs for {short}, which never count"
        elif mine:
            why = f"no run for {short} (the last run was {mine[-1].get('result') or 'unfinished'} for " \
                  f"{str(mine[-1].get('sha') or '?')[:7]})"
        else:
            why = f"never ran (needed: a pass for {short})"
        missing.append((k, why))
    if missing:
        names = [k for k, _ in missing]
        ci = f"orch ci --stages {','.join(req)}" if len(names) == len(jobs) else f"orch ci --jobs {','.join(names)}"
        ci += f" --ref {ref}" if ref != "HEAD" else ""
        fix = (f"pin the job images with orch init --approve (after the user says yes), then {ci}" if unpinned
               else ci)
        return _gate(n, "fail", "; ".join(f"{k}: {w}" for k, w in missing), fix)
    one = len(set(shas.values())) == 1
    where = f"for {next(iter(shas.values()))[:7]}" if one else "for the current commit of each repository"
    return _gate(n, "ok", f"{', '.join(passed)} passed {where} (runs without --wt)")


def script_digest(data: bytes) -> str:
    """The content hash approval records for a deploy or rollback script: sha256 hex of the file bytes."""
    return hashlib.sha256(data).hexdigest()


def _same(want, data) -> bool:
    """want: sha256 hex (an optional "sha256:" prefix is fine) of the raw or the LF-normalised content."""
    w = str(want or "").strip().lower()
    w = w[7:] if w.startswith("sha256:") else w
    return w in (script_digest(data), script_digest(data.replace(b"\r\n", b"\n")))


def _g_scripts(p, trust) -> dict:
    n = GATES[5]
    if not trust:
        return _gate(n, "skip", "not checked: the setup was never approved")
    scripts = trust.get("scripts") if isinstance(trust.get("scripts"), dict) else {}
    if not scripts:
        return _gate(n, "ok", "no deploy or rollback script file was recorded at approval")
    when = str(trust.get("approved_at") or "?")[:16].replace("T", " ")
    changed = []
    for rel, want in scripts.items():
        f = Path(rel) if Path(rel).is_absolute() else Path(p.root) / rel
        try:
            data = f.read_bytes()
        except OSError:
            changed.append(f"{rel} is missing")
            continue
        if not _same(want, data):
            changed.append(f"{rel} changed")
    if changed:
        first = next(iter(scripts))
        return _gate(n, "warn", f"{'; '.join(changed)} since the approval on {when} (a warning; it never blocks)",
                     f"review the change (git log -p -- {first}); when it is intended, record it with "
                     f"orch init --approve (after the user says yes)")
    return _gate(n, "ok", f"{', '.join(scripts)} {'matches' if len(scripts) == 1 else 'match'} the approved content")


def probe_production(specs: dict) -> dict:
    """{name: ssh spec} -> orch_discover records: `ssh -G` and `ssh ... true` only (docker=False), in parallel."""
    return od.probe_hosts(specs, set())


def _g_reach(cfg, approved, probe) -> dict:
    n = GATES[6]
    hosts = _prod_hosts(cfg)
    if not approved:
        return _gate(n, "skip", "not checked: the setup is not approved")
    if not hosts:
        return _gate(n, "skip", "not checked: no production host is saved")
    try:
        recs = (probe or probe_production)(dict(hosts)) or {}
    except Exception as e:  # noqa: BLE001 - a bug in the probe must not hide the other gates
        return _gate(n, "fail", f"the ssh check failed inside orch: {type(e).__name__}: {e}",
                     "report this as an orch bug, with the message")
    bad = []
    for name, spec in hosts:
        rec = recs.get(name) or {}
        if rec.get("ok"):
            continue
        pr = rec.get("problem") or {}
        try:
            tried = od.ssh_cmdline(spec)
        except ValueError:
            tried = f"ssh {spec} true"
        bad.append((f"{name} ({spec}): {pr.get('what') or 'no answer'}",
                    pr.get("fix") or f"check that {spec} answers ssh, then retry: {tried}"))
    if bad:
        return _gate(n, "fail", "; ".join(b[0] for b in bad), bad[0][1])
    return _gate(n, "ok", f"{_labels(hosts)} answered ssh")


# ----------------------------------------------------------------------------- deploy --check
def check(start=None, ref="HEAD", env=None, run=subprocess.run, probe=None) -> dict:
    """Every gate for the project at start (--project; it wins over ORCH_PROJECT, then the cwd) -> {"ready", "gates",
    "next", "ref", "sha"} (+ "shas" {repo: sha} when the jobs use more than one repository, + "ask" when the
    production gate fails on a "later" or missing answer). run = git, probe = the production ssh check ({name: spec}
    -> records). Raises oc.ConfigError only when the project folder itself cannot be read."""
    p = oc.find_project(start, env=env, run=run)
    if p is None:
        gates = [_gate(GATES[0], "fail", f"no orch setup for {_where(start, env)}", FIX_INIT)]
        gates += [_gate(g, "skip", "not checked: no orch setup here") for g in GATES[1:]]
        return {"ready": False, "gates": gates, "next": FIX_INIT, "ref": ref, "sha": None}
    cfg, cfg_err, trust, trust_err = {}, None, None, None
    try:
        cfg = oc.load_config(p)
    except oc.ConfigError as e:
        cfg_err = str(e)
    try:
        trust = oc.trust_get(oc.home(env), p)
    except oc.ConfigError as e:
        trust_err = str(e)
    states = _states(p, cfg, ref, run)
    gates = [_g_setup(cfg, cfg_err, trust, trust_err), _g_production(cfg, cfg_err), _g_clean(states, p.root),
             _g_pushed(states, p.root, ref), _g_jobs(p, cfg, cfg_err, states, p.root, ref), _g_scripts(p, trust)]
    gates.append(_g_reach(cfg, gates[0]["ok"], probe))
    ready = all(g["ok"] for g in gates)
    if ready:
        nxt = f"run the deploy command: {cfg['deploy']['command']}, then orch live"
    else:
        nxt = next((g["fix"] for g in gates if not g["ok"] and g["fix"]), FIX_INIT)
    uniq = _uniq(states)
    out = {"ready": ready, "gates": gates, "next": nxt, "ref": ref, "sha": uniq[0]["sha"] if uniq else None}
    if len(uniq) > 1:
        out["shas"] = {s["key"]: s["sha"] for s in uniq}
    if gates[1]["status"] == "fail":  # a "later" or missing answer: the question init will not ask again by itself
        out["ask"] = ask_for(cfg, "deploy" if _prod_hosts(cfg) else "production")
    return out


def cmd_deploy(a) -> int:
    """`orch deploy --check [--ref R] [--project DIR]`: a line per failing or warning gate, one summary line, then
    the final JSON line. Exit 0 ready, 7 a gate is not met, 2 usage. Without --check: exit 2, orch never deploys."""
    LAST.clear()
    if not getattr(a, "check", False):
        return _usage("deploy", NOT_DEPLOY, "orch deploy --check")
    ref = str(getattr(a, "ref", None) or "HEAD").strip() or "HEAD"
    if ref.startswith("-") or re.search(r"[\s\x00-\x1f\x7f]", ref):
        return _usage("deploy", f"bad ref {ref!r}: pass a branch, tag or commit (default HEAD)",
                      "orch deploy --check")
    try:
        res = check(getattr(a, "project", None), ref)
    except oc.ConfigError as e:
        return _usage("deploy", str(e), "fix what the error names, then run orch deploy --check again")
    for g in res["gates"]:  # only what needs attention; every gate, its fix and next are in the JSON line (Q19)
        if g["status"] in ("fail", "warn"):
            print(gate_line(g, fix=False), flush=True)
    print(summary_line(res), flush=True)
    code = 0 if res["ready"] else GATE_EXIT
    print(json.dumps(res), flush=True)
    LAST.update(verb="deploy", exit=code, **copy.deepcopy(res))
    bad = next((g for g in res["gates"] if not g["ok"]), None)
    if bad:  # the journal's difficulty: which gate, and the fix the agent was given
        LAST.update(reason=f"{bad['name']}: {bad['detail']}", fix=res["next"])
    return code


# ----------------------------------------------------------------------------- live
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an answer (the app is up); following it could reach another host, so it never is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def health_targets(cfg) -> list:
    """Saved health URLs -> [{"url", "insecure"}]; an entry is a URL string or {"url": ..., "insecure": true}."""
    dep = (cfg or {}).get("deploy")
    if not isinstance(dep, dict) or dep.get("answer") == "later":
        return []
    out = []
    for h in dep.get("health") or []:
        if isinstance(h, str) and h.strip():
            out.append({"url": h.strip(), "insecure": False})
        elif isinstance(h, dict) and isinstance(h.get("url"), str) and h["url"].strip():
            out.append({"url": h["url"].strip(), "insecure": h.get("insecure") is True})
    return out


def show_url(url) -> str:
    """The URL with any user or password in it masked: https://***@shop.example/health."""
    return re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/?#@]*@", r"\1***@", str(url))


def _why(e, timeout) -> str:
    """A plain sentence for one failed GET."""
    if isinstance(e, ssl.SSLCertVerificationError):
        why = getattr(e, "verify_message", None) or getattr(e, "reason", None) or e
        return (f"certificate not trusted ({why}): fix the server's certificate, or save this URL as insecure only "
                f"if you trust that server")
    if isinstance(e, TimeoutError):
        return f"no answer within {timeout:g} s"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused: nothing accepts connections on that host and port"
    if isinstance(e, socket.gaierror):
        return "the host name does not resolve from this machine"
    if isinstance(e, ssl.SSLError):
        return f"TLS failed: {getattr(e, 'reason', None) or e}"
    if isinstance(e, http.client.RemoteDisconnected):
        return "the server closed the connection without an answer"
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return str(e) or type(e).__name__


def _get(url, timeout, opener) -> tuple:
    """One GET -> (HTTP status or None, error or None); 2xx and 3xx are answers, everything else is an error."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None, "not an http or https URL; save one like https://shop.example/health (orch init)"
        headers = {"User-Agent": "orch-live", "Accept": "*/*"}
        if parts.username is not None:  # credentials in the URL become a basic auth header, never the request line
            cred = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(cred).decode("ascii")
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            url = urlunsplit(parts._replace(netloc=host + (f":{parts.port}" if parts.port else "")))
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            r.read(LIVE_READ)
            return r.status, None
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, (None if 300 <= e.code < 400 else f"HTTP {e.code} {e.reason}".strip())
    except urllib.error.URLError as e:
        return None, _why(e.reason, timeout)
    except (OSError, ValueError, http.client.HTTPException) as e:
        return None, _why(e, timeout)


def check_url(url, insecure=False, timeout=None, tries=None, backoff=None, sleep=time.sleep) -> dict:
    """GET url until it answers 2xx/3xx: `tries` attempts of `timeout` s, pausing backoff[i] s between them (defaults
    LIVE_TIMEOUT, LIVE_TRIES, LIVE_BACKOFF) -> {"url" (credentials masked), "status", "ms" (the last try), "tries",
    "error" (None = up), "insecure"}. Certificates are verified unless insecure is True."""
    timeout = LIVE_TIMEOUT if timeout is None else timeout
    tries = max(1, int(LIVE_TRIES if tries is None else tries))
    backoff = tuple(LIVE_BACKOFF if backoff is None else backoff) or (0,)
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    status, err, ms, n = None, None, 0, 0
    for n in range(1, tries + 1):
        if n > 1:
            sleep(backoff[min(n - 2, len(backoff) - 1)])
        t0 = time.monotonic()
        status, err = _get(url, timeout, opener)
        ms = round((time.monotonic() - t0) * 1000)
        if err is None:
            break
    return {"url": show_url(url), "status": status, "ms": ms, "tries": n, "error": err, "insecure": bool(insecure)}


def live(targets, timeout=None, tries=None, backoff=None, sleep=time.sleep) -> list:
    """check_url for every {"url", "insecure"} target, in parallel; results in target order."""
    if not targets:
        return []
    with cf.ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
        futs = [ex.submit(check_url, t["url"], t.get("insecure", False), timeout, tries, backoff, sleep)
                for t in targets]
        return [f.result() for f in futs]


def live_line(c) -> str:
    """`ok   https://shop.example/health 200 0.21s`; FAIL lines end with the error."""
    line = f"{'ok' if c['error'] is None else 'FAIL':<4} {c['url']} {c['status'] or '-'} {c['ms'] / 1000:.2f}s"
    if c["tries"] > 1:
        line += f" ({c['tries']} tries)"
    if c["insecure"]:
        line += " (certificate not checked)"
    return line + (f": {c['error']}" if c["error"] else "")


def cmd_live(a) -> int:
    """`orch live [--project DIR]`: one line per saved health URL, then {"ok", "checks", "next"}. Exit 0 all up,
    1 any down, 2 no health URL saved (+ "ask": the deploy question, next "orch init --ask deploy") or no setup."""
    LAST.clear()
    start = getattr(a, "project", None)
    try:
        p = oc.find_project(start)
        cfg = oc.load_config(p) if p else {}
    except oc.ConfigError as e:
        return _usage("live", str(e), "orch init")
    targets = health_targets(cfg)
    if not targets:
        if p is None:
            msg, nxt, ask = f"no orch setup for {_where(start)}: run orch init", "orch init", None
        else:  # the deploy question again: plain orch init says ready and never asks it once a command is saved
            dep = cfg.get("deploy") if isinstance(cfg.get("deploy"), dict) else {}
            has_cmd = dep.get("answer") != "later" and bool(str(dep.get("command") or "").strip())
            msg = (f"no health URL is saved for {Path(p.root).as_posix()}, so orch live has nothing to check: relay "
                   f"the ask (orch init --ask deploy prints it again) and save the answer with orch init --apply under "
                   f"the key deploy" + ("" if has_cmd else
                                        "; a new deploy command needs orch init --approve after the user says yes"))
            nxt, ask = "orch init --ask deploy", ask_for(cfg, "deploy")
        print(f"orch: {msg}", file=sys.stderr, flush=True)
        print(json.dumps({"ok": False, "checks": [], "error": msg, "next": nxt, **({"ask": ask} if ask else {})}),
              flush=True)
        LAST.update(verb="live", exit=2, ok=False, checks=[], error=msg, fix=nxt, **({"ask": ask} if ask else {}))
        return 2
    checks = live(targets)
    for c in checks:
        print(live_line(c), flush=True)
    ok = all(c["error"] is None for c in checks)
    rb = str((cfg.get("deploy") or {}).get("rollback") or "").strip()
    nxt = ("check that the change is visible (read-only GET or SELECT), then hand over for UAT" if ok
           else f"tell the user; roll back only when they ask: {rb}" if rb
           else "tell the user (no rollback command is saved), then investigate")
    print(json.dumps({"ok": ok, "checks": checks, "next": nxt}), flush=True)
    code = 0 if ok else 1
    LAST.update(verb="live", exit=code, ok=ok, checks=copy.deepcopy(checks))
    if not ok:  # production is down after a deploy: not a test failure (journal class, detail and hint)
        LAST.update({"class": "live-down", "result": "down", "fix": nxt,
                     "reason": "; ".join(f"{c['url']}: {c['error']}" for c in checks if c["error"])})
    return code
