#!/usr/bin/env python3
"""Optional Claude Code SessionStart hook: one context line when a session starts inside an approved orch project.

Agents do not always load the helper-node-orchestrator skill for a deploy or release question, or before running
tests in a project that has an approved setup. Registered for SessionStart (README.md, Session-start hook), this
prints LINE when the session's folder is inside an approved project root, and nothing anywhere else.

Reads only the payload's cwd (stdin JSON; else the process cwd) and $ORCH_HOME/guard-view.json (ORCH_HOME
defaults to ~/.helper-orch), which orch writes from approved projects only. Paths compare like the guard's:
case-folded, either slash, /c/x = c:/x. No subprocess, no network, no import from the package (safe under
python -I), and nothing from the view or the payload is echoed. Any error -> no output; the exit code is always 0.
"""
import json
import os
import posixpath
import re
import sys

LINE = ("orch: this project has an approved helper-node-orchestrator setup. Load that skill before any test, build "
        "or lint run and for every deploy, release or production question (checks too); journal every task "
        "(orch journal start/end).")
MAX_VIEW = 4 << 20      # bytes; a bigger file is not a guard view
MAX_PAYLOAD = 1 << 20   # bytes read from stdin; a SessionStart payload is a few hundred
DRIVE = re.compile(r"[a-z]:")
MSYS = re.compile(r"/(?:mnt/|cygdrive/)?([a-z])(?=/|$)")  # Git Bash, WSL and Cygwin spellings of a drive


def norm(path):
    """Case-folded absolute path with forward slashes and no trailing slash; '' when relative or not a path."""
    if not isinstance(path, str):
        return ""
    p = path.strip().replace("\\", "/")
    if not p or "\x00" in p:
        return ""
    if p == "~" or p.startswith("~/"):
        p = os.path.expanduser("~").replace("\\", "/") + p[1:]
    p = p.lower()
    drive = p[:2] if DRIVE.match(p) else ""
    rest = p[len(drive):]
    if not rest.startswith("/"):
        return ""
    rest = posixpath.normpath(rest).rstrip("/")
    return drive + rest if (drive or rest) else "/"


def spellings(p):
    """A normalised path plus its drive form when it is spelled /c/x, /mnt/c/x or /cygdrive/c/x (-> c:/x)."""
    m = MSYS.match(p)
    return (p, m.group(1) + ":" + p[m.end():]) if m else (p,)


def inside(path, root):
    """True when the normalised path is root itself or below it, in any of their spellings (as the guard sees it)."""
    if not path or not root:
        return False
    return any(a == r or a.startswith(r if r.endswith("/") else r + "/")
               for a in spellings(path) for r in spellings(root))


def approved_roots(view):
    """Normalised roots of the projects a guard view lists (orch lists approved ones only); [] when malformed."""
    if not isinstance(view, dict) or view.get("schema") != 1 or not isinstance(view.get("projects"), list):
        return []
    roots = (norm(p.get("root")) if isinstance(p, dict) else "" for p in view["projects"])
    return [r for r in roots if r]


def load_view(home):
    """The parsed guard view; None when absent, unreadable, oversized or not JSON."""
    try:
        with open(os.path.join(home, "guard-view.json"), "rb") as f:
            raw = f.read(MAX_VIEW + 1)
        return json.loads(raw.decode("utf-8-sig")) if len(raw) <= MAX_VIEW else None
    except (OSError, ValueError, RecursionError):
        return None


def payload_cwd(raw):
    """The payload's cwd string, or None (no payload, bad JSON, no cwd)."""
    try:
        data = json.loads(raw.decode("utf-8-sig")) if raw else None
    except (ValueError, RecursionError):
        return None
    cwd = data.get("cwd") if isinstance(data, dict) else None
    return cwd if isinstance(cwd, str) and cwd.strip() else None


def message(raw=b"", env=None, getcwd=os.getcwd):
    """LINE when the payload's cwd (else getcwd()) is inside an approved project root of the guard view, else None."""
    env = os.environ if env is None else env
    home = env.get("ORCH_HOME") or os.path.join(os.path.expanduser("~"), ".helper-orch")
    roots = approved_roots(load_view(home))
    if not roots:
        return None
    cwd = payload_cwd(raw)
    here = norm(cwd)
    if not here:  # no cwd in the payload, or a relative one: the hook runs in the session's folder
        base = getcwd()
        here = norm(os.path.join(base, cwd) if cwd else base)
    return LINE if any(inside(here, r) for r in roots) else None


def read_stdin():
    """The payload bytes; b"" for a terminal (never wait for typing), no stdin or a read error."""
    try:
        s = sys.stdin
        return b"" if s is None or s.isatty() else s.buffer.read(MAX_PAYLOAD)
    except Exception:
        return b""


def main():
    """Prints LINE or nothing. Never raises."""
    try:
        line = message(read_stdin())
        if line:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except BaseException:
        pass


if __name__ == "__main__":
    main()
    os._exit(0)  # exit 0 even when stdout is gone (a normal exit would fail flushing it)
