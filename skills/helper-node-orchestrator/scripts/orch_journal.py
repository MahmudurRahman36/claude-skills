#!/usr/bin/env python3
"""orch project journal: task summaries, plans, decisions, steps with outcome and time, difficulties and how they
were resolved, kept in the project's store so investigation and monitoring tools can read them later.

Spec: dev/v2-design.md section 13, dev/v2-contract.md "orch_journal.py". Standard library only, no network.

Files (the store comes from orch_config.find_project / init_project; for a git project it lives inside .git):
  <store>/journal/journal.jsonl        append-only, one JSON record per line; rotated at ROTATE_BYTES to
                                       journal-<yyyymmdd-hhmmss>.jsonl
  <store>/journal/MEMORY.md            one readable section per finished task, rendered when the task ends
  <store>/state/journal-current.json   open tasks, the current one and the per-task sequence counters
Two writers: orch itself (record_invocation: one step record per orch command, a difficulty record on a non-zero
exit, and a resolution record for each of them when the same command later exits 0) and the agent (orch journal
start|add|end with one JSON object on stdin). Every string passes the secret redaction first. Appends and state
changes serialise on one cross-process lock and a record is written in one call, so lines never interleave; a line
torn by a crash is skipped by readers and never glued to the next one.
ORCH_JOURNAL=0 turns writing off (tests); reading still works.
"""
import argparse
import collections
import datetime as dt
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import orch_config as C

V = 1
TYPES = ("task-start", "plan", "decision", "step", "difficulty", "resolution", "note", "task-end")
ADD_TYPES = TYPES[1:-1]
OUTCOMES = ("done", "partial", "failed", "blocked", "abandoned")
ACTIONS = ("start", "add", "end", "show", "stats", "path")
ROTATE_BYTES = 20 << 20  # journal.jsonl is renamed to journal-<stamp>.jsonl at this size (tests lower it)
STALE_S = 12 * 3600      # an open task with no record for this long is closed as abandoned at the next start
TEXT_MAX, LIST_MAX, STDIN_MAX, SHOW_LAST = 2000, 100, 1 << 20, 50
STATE = "journal-current"
RESERVED = {"v", "ts", "seq", "task", "type", "by", "since_prev_s"}
EXIT_NAMES = {0: "pass", 1: "test-fail", 2: "usage", 3: "oom", 4: "timeout", 5: "infra", 6: "r1-guard",
              7: "deploy-gate", 75: "not-admitted"}
EXAMPLES = {
    "task-start": {"summary": "fix the failing cart tests", "request": "make the web tests pass again",
                   "plan": ["reproduce with orch run web-unit", "fix the cause", "orch ci"]},
    "plan": {"type": "plan", "steps": ["orch ci --stages lint,unit", "fix what fails", "orch ci"]},
    "decision": {"type": "decision", "what": "run web-unit before the full ci",
                 "why": "it is the smallest job that shows the failure", "options": ["web-unit only", "full orch ci"]},
    "step": {"type": "step", "what": "fixed the date parsing in cart.ts", "outcome": "done", "duration_s": 300},
    "difficulty": {"type": "difficulty", "what": "web-unit passes but the bug stays east of UTC",
                   "detail": "the helpers run in UTC, so no test covers that case"},
    "resolution": {"type": "resolution", "for": 3, "how": "added a test that runs with TZ=Asia/Tokyo",
                   "outcome": "fixed"},
    "note": {"type": "note", "text": "the user wants the change in one commit"},
    "task-end": {"outcome": "done", "summary": "cart tests pass on every helper",
                 "lessons": ["pin the time zone in test setup"], "open": []},
}
MEMORY_HEAD = ("# Project journal: task summaries\n\nWritten by orch when a task ends (orch journal end). "
               "Full records: journal.jsonl in this folder, or orch journal show.\n")


def _heredoc(act, obj) -> str:
    """A command that runs as printed: orch journal <act> with obj on stdin through a quoted heredoc (real newlines,
    so the shell finds the closing EOF on a line of its own)."""
    return f"orch journal {act} <<'EOF'\n{json.dumps(obj, ensure_ascii=False)}\nEOF"


def _example(kind) -> str:
    return _heredoc({"task-start": "start", "task-end": "end"}.get(kind, "add"), EXAMPLES[kind])


NEXT_START = ("orch logs its commands itself (steps, failures, later passes); add your decisions, problems orch cannot "
              "see and their fixes (no secrets), then orch journal end:\n" + "\n".join(_heredoc("add", t) for t in (
                  {"type": "decision", "what": "...", "why": "..."}, {"type": "difficulty", "what": "..."},
                  {"type": "resolution", "for": "<seq>", "how": "..."})))


class JournalError(ValueError):
    pass


# ----------------------------------------------------------------------------- redaction
# Free text loses only the secret value; the name, flag or URL around it stays readable. The table covers every form
# orch init's command and URL check refuses (orch_init._SECRET; tests/test_journal.py holds the two to the same
# cases), so text that check would refuse never reaches journal.jsonl or MEMORY.md in cleartext, also when it comes
# back in one of orch's own error messages. A $VAR value is a reference and stays, and so do file-style names
# (TOKEN_FILE=, --password-file) and values masked already.
_MARK = "[REDACTED]"
_SKIP = r"(?!\$|\[REDACTED|\*\*\*)"
_VAL = r"""(?:(?<=")[^"\n]+(?=")|(?<=')[^'\n]+(?=')|[^\s"'<>]+)"""  # a quoted value runs to its closing quote
_VAL6 = r"""(?:(?<=")[^"\n]{6,}(?=")|(?<=')[^'\n]{6,}(?=')|[^\s"'<>]{6,})"""  # the same, 6 characters or more
_FVAL = r"""(?:(?<=")[^"\n]+(?=")|(?<=')[^'\n]+(?=')|[^\s"'<>-][^\s"'<>]*)"""  # a flag's value, never the next flag
# Every rule runs in linear time on the 64 KB scrub() reads: a NAME starts only where a run of name characters
# starts, and no repeat sits inside another repeat that can match the same text
_NAME = r"(?<![\w.-])[\w.-]*?"  # NAME of NAME=value, prefixes such as DB_ or --db- included
_SEP = r"""["']?\s*[=:]\s*["']?"""
_CMD = r"[^|;&\n]{0,400}?\s"  # the rest of the same shell command, up to the option
_CURL = re.compile(r"(?i)\b(?:curl|wget)\b[^|;&\n]*$")


def _user_pass(m) -> str:
    """The password of -u/--user user:password; docker's -u uid:gid and -u name:name stay, except after curl/wget."""
    user, pw = m.group(1), m.group(2)
    if (pw == user or (user.isdigit() and pw.isdigit())) and not _CURL.search(m.string, max(0, m.start() - 400),
                                                                                m.start()):
        return m.group(0)
    return m.group(0)[:m.start(2) - m.start()] + _MARK


_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED-AWS-KEY]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|\b(?:gh[pousr]|github_pat)[_-][\w-]{10,}"),
     "[REDACTED-GH-TOKEN]"),
    (re.compile(r"\bglpat[_-][\w-]{10,}"), "[REDACTED-GL-TOKEN]"),
    (re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}|\bxox[abposr]_[\w-]{10,}"), "[REDACTED-SLACK]"),
    # bare API keys: sk-... (sk-proj-..., sk-ant-..., sk-svcacct-...) and Google's AIza...
    (re.compile(r"(?<![\w-])sk-[A-Za-z0-9_-]{20,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"(?<![\w-])AIza[0-9A-Za-z_-]{30,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"(?<![\w-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED-JWT]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(rf"(?i)(\bauthorization{_SEP}[a-z][\w-]*\s+[\"']?){_SKIP}[^\s\"']{{8,}}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@"), r"\1[REDACTED]@"),
    # webhook URLs whose path is the credential
    (re.compile(r"(?i)(\bhooks\.slack\.com/(?:services|workflows|triggers)/|\bdiscord(?:app)?\.com/api/webhooks/|"
                r"\bapi\.telegram\.org/bot)(?!\[REDACTED)[^\s\"'<>]+"), r"\1[REDACTED]"),
    (re.compile(rf"(?i)([?&](?:key|code|sig|signature|apikey|api[_-]key|access[_-]?token|token|auth|password|secret|"
                rf"client[_-]secret)=){_SKIP}[^&\s\"'<>#]+"), r"\1[REDACTED]"),
    # a secret option and its value: --password X, --api-token=X, -pass X, --pw 'X'
    (re.compile(rf"(?i)((?<![\w-])--?(?:[a-z0-9]+[-_])*(?:pass(?:word|wd|phrase)?|pwd|pw|secret|token|api[_-]?key|"
                rf"access[_-]?key|credentials?)(?:\s+|=)[\"']?){_SKIP}{_FVAL}"), r"\1[REDACTED]"),
    # tool options that take a password: sshpass -p X, mysql -pX (attached), docker login -p X, redis-cli -a X
    (re.compile(rf"(\b(?i:sshpass)\s+-p\s*[\"']?){_SKIP}{_VAL}"), r"\1[REDACTED]"),
    (re.compile(rf"(\b(?i:mysql|mariadb|mysqldump|mysqladmin|mysqlsh|mysqlimport|mysqlcheck)\b{_CMD}-p[\"']?)"
                rf"{_SKIP}{_VAL}"), r"\1[REDACTED]"),
    (re.compile(rf"(\b(?i:docker|podman|nerdctl|buildah)\s+(?i:login)\b{_CMD}-p(?:\s+|=)?[\"']?){_SKIP}{_FVAL}"),
     r"\1[REDACTED]"),
    (re.compile(rf"(\b(?i:redis-cli)\b{_CMD}-a\s*[\"']?){_SKIP}{_FVAL}"), r"\1[REDACTED]"),
    (re.compile(rf"(?<![\w-])(?:-u|-U|--user|--http-user|--proxy-user)(?:\s+|=)?[\"']?(?!\$)([^\s:\"'$]+):{_SKIP}"
                r"([^\s\"']+)"), _user_pass),
    # NAME=value and NAME: value: a name that ends in a secret word (DB_PASSWORD, X-Api-Key, SECRET_KEY) masks any
    # value; a name that holds one elsewhere (or ends in _KEY) masks NAME=value of 6 or more characters, file-style
    # names excepted; a name ending in auth (X-Auth: value) masks 6 or more characters
    (re.compile(rf"(?i)({_NAME}(?:passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
                rf"secret[_-]?key|credentials?|[_-]pass){_SEP}){_SKIP}{_VAL}"), r"\1[REDACTED]"),
    (re.compile(rf"(?i)((?<![\w.-])(?:(?=[\w.-]*?(?:pass|pwd|secret|token|credential|api[_-]?key|private[_-]?key))"
                rf"[\w.-]+(?<!file)(?<!path)(?<!dir)|[\w.-]*[_-]key)[\"']?\s*=\s*[\"']?){_SKIP}{_VAL6}"),
     r"\1[REDACTED]"),
    (re.compile(rf"(?i)({_NAME}auth{_SEP}){_SKIP}{_VAL6}"), r"\1[REDACTED]"),
]
_SECRET_NAME = re.compile(r"(?i)token|pass|secret|key")
_ENV_FLAGS = ("-e", "--env", "--build-arg")
_USER_FLAGS = ("-u", "--user")
_KV = re.compile(r"([A-Za-z_][\w.\-]*)=(.*)", re.S)
_USERPASS = re.compile(r"([^\s:/\\@]+):(?![/\\])(\S+)")


def scrub(text: str) -> str:
    """Secret values masked in free text (agent records, orch's details and error messages): private key blocks,
    cloud, git and chat tokens, bare API keys (sk-..., sk-proj-..., sk-ant-..., AIza...), JWTs, Bearer and
    Authorization values, URL passwords, webhook URL paths and URL query
    credentials, the value of a secret option (--password X, --api-token=X, sshpass -p X, mysql -pX, docker login
    -p X, redis-cli -a X), the password of -u user:password, and NAME=value / NAME: value for secret names
    (DB_PASSWORD=, PGPASSWORD=, DB_PASS=, SECRET_KEY=, X-Auth:). $VAR references stay."""
    s = str(text)[:65536]
    for rx, rep in _SECRETS:
        s = rx.sub(rep, s)
    return s


def redact_args(args) -> list:
    """argv with secret values masked (same rules as orch's redact): the value after -e/--env/--build-arg keeps its
    KEY (KEY=***); the value after a flag whose name looks like token|pass|secret|key, and --flag=value forms of it;
    any KEY=value whose KEY looks like one; the password of user:password after -u/--user. A multi-word argument
    (a remote shell command) is redacted word by word; every word then passes scrub()."""
    def kv(t, force):
        m = _KV.fullmatch(t.lstrip("'\""))
        if m and (force or _SECRET_NAME.search(m.group(1))):
            return t[:len(t) - len(t.lstrip("'\""))] + m.group(1) + "=***"
        return "***" if force else t

    def words(ws):
        out, nxt = [], None
        for w in ws:
            if nxt == "env":
                out.append(kv(w, True))
            elif nxt == "flag":
                out.append("***")
            elif nxt == "user":
                out.append(_USERPASS.sub(r"\1:***", w, count=1))
            elif w in _ENV_FLAGS:
                out.append(w)
                nxt = "env"
                continue
            elif w in _USER_FLAGS:
                out.append(w)
                nxt = "user"
                continue
            elif w.startswith("-") and "=" not in w and _SECRET_NAME.search(w):
                out.append(w)
                nxt = "flag"
                continue
            elif w.startswith("-") and "=" in w:
                f, v = w.split("=", 1)
                out.append(f + "=" + (kv(v, True) if f in _ENV_FLAGS else "***" if _SECRET_NAME.search(f) else v))
            elif " " in w:
                out.append(" ".join(words(w.split(" "))))
            else:
                out.append(kv(w, False))
            nxt = None
        return out
    return [scrub(w) for w in words([str(a) for a in args])]


def _cap(s: str, n: int = TEXT_MAX) -> str:
    return s if len(s) <= n else s[:n] + f" [... {len(s) - n} more characters]"


def _clean(v, depth=0):
    """JSON-safe copy: strings redacted and capped, lists capped, non-finite floats -> null, other objects -> text."""
    if isinstance(v, str):
        return _cap(scrub(v))
    if v is None or isinstance(v, (bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if depth >= 8:
        return _cap(scrub(json.dumps(v, default=str)), 200)
    if isinstance(v, dict):
        return {str(k)[:200]: _clean(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        items = sorted(v, key=str) if isinstance(v, (set, frozenset)) else list(v)
        out = [_clean(x, depth + 1) for x in items[:LIST_MAX]]
        if len(items) > LIST_MAX:
            out.append(f"[... {len(items) - LIST_MAX} more items]")
        return out
    return _cap(scrub(str(v)))


# ----------------------------------------------------------------------------- time
def _now(t=None) -> dt.datetime:
    """Aware local time from None (now), epoch seconds, an ISO string or a datetime (naive = local)."""
    if t is None:
        return dt.datetime.now().astimezone()
    if isinstance(t, (int, float)):
        return dt.datetime.fromtimestamp(t).astimezone()
    if isinstance(t, str):
        t = dt.datetime.fromisoformat(t)
    return t.astimezone()


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="milliseconds")


def _parse(ts):
    try:
        t = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _dur(s) -> str:
    if not _isnum(s):
        return "-"
    s = max(0.0, float(s))
    if s < 10:
        return f"{s:.1f}s"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _local(ts, fmt="%Y-%m-%d %H:%M") -> str:
    t = _parse(ts)
    return t.strftime(fmt) if t else str(ts or "?")


# ----------------------------------------------------------------------------- files and state
def journal_dir(p) -> Path:
    return Path(p.store) / "journal"


def paths(p) -> dict:
    d = journal_dir(p)
    return {"journal": d / "journal.jsonl", "memory": d / "MEMORY.md",
            "current": Path(p.store) / "state" / f"{STATE}.json"}


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("ORCH_JOURNAL", "1")).strip().lower() not in ("0", "false", "off", "no")


def _lock(p):
    d = journal_dir(p)
    d.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(d, 0o700)
    return C._locked(d / "journal.jsonl")


def _load(p) -> dict:
    """The open tasks; an unreadable state file counts as none open (its tasks then never get an end record)."""
    st = C.load_state(p, STATE, None)
    st = st if isinstance(st, dict) else {}
    st["v"] = V
    if not isinstance(st.get("open"), dict):
        st["open"] = {}
    if not isinstance(st.get("untracked"), dict):
        st["untracked"] = {"seq": 0, "last": None}
    if st.get("current") not in st["open"]:
        st["current"] = None
    return st


_ROT = re.compile(r"journal-(\d{8}-\d{6})(?:-(\d+))?\.jsonl")


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < ROTATE_BYTES:
            return
    except FileNotFoundError:
        return
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest, n = path.with_name(f"journal-{stamp}.jsonl"), 1
    while dest.exists():
        n += 1
        dest = path.with_name(f"journal-{stamp}-{n}.jsonl")
    for i in range(10):
        try:
            os.replace(path, dest)
            return
        except PermissionError:  # Windows: a reader holds it open; rotate on a later append, never lose a record
            time.sleep(0.05 * (i + 1))
        except OSError:
            return


def _append(p, recs) -> None:
    """One write per call under the lock (held by the caller); 0600 on POSIX."""
    path = paths(p)["journal"]
    _rotate(path)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                data = b"\n" + data  # a crash tore the last line: start this record on a line of its own
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def _files(p, since=None) -> list:
    """Rotated files oldest first, then journal.jsonl; with since (epoch), rotated files last written before it are
    skipped (they cannot hold a record of a task that started later)."""
    d = journal_dir(p)
    try:
        names = list(d.iterdir())
    except OSError:
        return []
    rot = []
    for f in names:
        m = _ROT.fullmatch(f.name)
        if m:
            try:
                if since is None or f.stat().st_mtime >= since - 2:
                    rot.append((m.group(1), int(m.group(2) or 1), f))
            except OSError:
                continue
    cur = d / "journal.jsonl"
    return [f for _, _, f in sorted(rot)] + ([cur] if cur.exists() else [])


def _read_file(f, task=None) -> list:
    out, needle = [], task.encode("utf-8") if task else None
    try:
        with open(f, "rb") as fh:
            for raw in fh:
                if needle and needle not in raw:
                    continue
                try:
                    r = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue  # torn by a crash, or hand-edited
                if isinstance(r, dict) and (task is None or r.get("task") == task):
                    out.append(r)
    except OSError:
        pass
    return out


def _records(p, task=None, since=None) -> list:
    return [r for f in _files(p, since) for r in _read_file(f, task)]


def _rec(task, info, kind, by, now, fields) -> dict:
    """The next record of task; info (its state entry) gets the new seq and last-record time."""
    info["seq"] = int(info.get("seq") or 0) + 1
    r = {"v": V, "ts": _iso(now), "seq": info["seq"], "task": task, "type": kind, "by": by}
    last = _parse(info.get("last"))
    if last is not None:
        r["since_prev_s"] = round(max(0.0, (now - last).total_seconds()), 3)
    info["last"] = r["ts"]
    r.update({k: v for k, v in fields.items() if k not in RESERVED})
    return r


def _session():
    for k in ("ORCH_SESSION", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        v = re.sub(r"[^A-Za-z0-9_.-]", "", os.environ.get(k) or "")[:40]
        if v:
            return v
    return None


# ----------------------------------------------------------------------------- validation
def _verb(kind) -> str:
    return {"task-start": "journal start", "task-end": "journal end"}.get(kind, f"journal add {kind}")


def _missing(kind, key, hint) -> JournalError:
    return JournalError(f'{_verb(kind)} needs "{key}" ({hint}); send one JSON object, e.g.:\n{_example(kind)}')


def _jtype(v) -> str:
    return {dict: "an object", list: "a list", bool: "true/false"}.get(type(v), type(v).__name__)


def _text(kind, d, key, hint=None):
    v = d.get(key)
    if _isnum(v):
        v = str(v)
    if v is None or (isinstance(v, str) and not v.strip()):
        if hint:
            raise _missing(kind, key, hint)
        return None
    if not isinstance(v, str):
        raise JournalError(f'{_verb(kind)} "{key}" must be text, not {_jtype(v)}; send one JSON object, e.g.:\n'
                           f'{_example(kind)}')
    return v.strip()


def _texts(kind, d, key):
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or any(isinstance(x, (dict, list)) for x in v):
        raise JournalError(f'{_verb(kind)} "{key}" must be a list of short texts; send one JSON object, e.g.:\n'
                           f'{_example(kind)}')
    return [str(x).strip() for x in v if x is not None and str(x).strip()]


def _seq(kind, d, key, hint, required):
    v = d.get(key)
    if isinstance(v, str) and re.fullmatch(r"#?\d+", v.strip()):
        v = int(v.strip().lstrip("#"))
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise _missing(kind, key, hint)
    return v


def _fields(kind, d) -> dict:
    """Validated, redacted fields of one record; JournalError names the missing or malformed field."""
    f = {k: v for k, v in d.items() if k not in RESERVED}
    put = {}
    if kind == "task-start":
        put["summary"] = _text(kind, d, "summary", "one line: what this task is about")
        put["request"] = _text(kind, d, "request")
        put["plan"] = _texts(kind, d, "plan")
        for k in ("closed", "session"):  # set by orch
            f.pop(k, None)
    elif kind == "plan":
        put["steps"] = _texts(kind, d, "steps")
        if not put["steps"]:
            raise _missing(kind, "steps", "the planned steps, a list of short texts")
    elif kind == "decision":
        put["what"] = _text(kind, d, "what", "what you decided")
        put["why"] = _text(kind, d, "why", "why you chose it")
        put["options"] = _texts(kind, d, "options")
    elif kind == "step":
        put["what"] = _text(kind, d, "what", "what was done")
        put["outcome"] = _text(kind, d, "outcome")
        x = d.get("duration_s")
        if isinstance(x, str):
            try:
                x = float(x)
            except ValueError:
                pass
        if x is not None and (not _isnum(x) or x < 0):
            raise JournalError(f'{_verb(kind)} "duration_s" must be seconds, a number of 0 or more; send one JSON '
                               f'object, e.g.:\n{_example(kind)}')
        put["duration_s"] = x
    elif kind == "difficulty":
        put["what"] = _text(kind, d, "what", "what went wrong")
        put["detail"] = _text(kind, d, "detail")
        put["class"] = _text(kind, d, "class")
        put["step"] = _seq(kind, d, "step", "the seq of the step it happened in", False)
    elif kind == "resolution":
        put["for"] = _seq(kind, d, "for", "the seq number of the difficulty it resolves", True)
        put["how"] = _text(kind, d, "how", "how it was resolved")
        put["outcome"] = _text(kind, d, "outcome")
    elif kind == "note":
        put["text"] = _text(kind, d, "text", "the note")
    elif kind == "task-end":
        o = _text(kind, d, "outcome", "one of " + ", ".join(OUTCOMES))
        if o.lower() not in OUTCOMES:
            raise JournalError(f'{_verb(kind)} "outcome" is {o!r}; use one of {", ".join(OUTCOMES)}')
        put["outcome"] = o.lower()
        put["summary"] = _text(kind, d, "summary")
        put["lessons"] = _texts(kind, d, "lessons")
        put["open"] = _texts(kind, d, "open")
        for k in ("duration_s", "counts", "started", "reason"):  # computed by orch
            f.pop(k, None)
    for k, v in put.items():
        f.pop(k, None)
        if v is not None:
            f[k] = v
    return _clean(f)


def _obj(data) -> dict:
    if not isinstance(data, dict):
        raise JournalError(f"the journal takes one JSON object, not {_jtype(data)}")
    return data


def _pick(st, want, need_open) -> str:
    if want not in (None, ""):
        want = str(want).strip()
        if want in st["open"] or (want == "untracked" and not need_open):
            return want
        raise JournalError(f"task {want} is not open (it ended or never started); open tasks: "
                           f"{', '.join(st['open']) or 'none'}; leave out the task to use the current one")
    if st.get("current"):
        return st["current"]
    if need_open:
        others = ", ".join(st["open"])
        raise JournalError("no current journal task to end; " + (f"open tasks: {others}; pass --task ID" if others else
                                                                 "start one with orch journal start"))
    return "untracked"


# ----------------------------------------------------------------------------- task lifecycle
def _counts(recs) -> dict:
    steps = [r for r in recs if r.get("type") == "step"]
    diffs = [r for r in recs if r.get("type") == "difficulty"]
    fixed = _fixes(recs)
    res = sum(1 for r in diffs if r.get("seq") in fixed)
    return {"steps": len(steps), "decisions": sum(1 for r in recs if r.get("type") == "decision"),
            "difficulties": len(diffs), "resolved": res, "unresolved": len(diffs) - res,
            "orch_seconds": round(sum(r["duration_s"] for r in steps if r.get("by") == "orch" and
                                      _isnum(r.get("duration_s"))), 3)}


def _fixes(recs) -> dict:
    """difficulty seq -> resolution records that fixed it (a resolution with outcome "failed" was only an attempt)."""
    out = {}
    for r in recs:
        if r.get("type") == "resolution" and str(r.get("outcome") or "").lower() != "failed":
            out.setdefault(r.get("for"), []).append(r)
    return out


def _memory(p, recs):
    """Append the task's section to MEMORY.md (atomic rewrite); a warning string when it could not be written."""
    path = paths(p)["memory"]
    try:
        try:
            old = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            old = MEMORY_HEAD
        if old and not old.endswith("\n"):
            old += "\n"
        C._write_bytes(path, (old + "\n" + render_task_md(recs)).encode("utf-8"))
        return None
    except (OSError, C.ConfigError) as e:
        task = recs[-1].get("task") if recs else "?"
        return (f"MEMORY.md was not updated ({e}); the journal holds the full record: "
                f"orch journal show --task {task}")


def _finish(p, st, task, info, fields, now, by, stop=None):
    """Write task-end for an open task: duration, counts, then its MEMORY.md section. Returns (record, warning)."""
    started = _parse(info.get("started")) or now
    recs = _records(p, task=task, since=started.timestamp())
    fields = dict(fields, started=info.get("started"),
                  duration_s=round(max(0.0, ((stop or now) - started).total_seconds()), 3), counts=_counts(recs))
    rec = _rec(task, info, "task-end", by, now, fields)
    st["open"].pop(task, None)
    if st.get("current") == task:
        st["current"] = None
    C.save_state(p, STATE, st)  # state before the append: a crash leaves a gap in seq, never a second end
    _append(p, [rec])
    return rec, _memory(p, recs + [rec])


def _close_stale(p, st, now) -> list:
    out = []
    for task, info in list(st["open"].items()):
        last = _parse(info.get("last")) or _parse(info.get("started"))
        if last is not None and (now - last).total_seconds() <= STALE_S:
            continue
        _finish(p, st, task, info, {"outcome": "abandoned", "summary": info.get("summary") or "",
                                    "reason": f"no orch journal end within {STALE_S // 3600} h of the last record "
                                              f"({_local(info.get('last'))})"}, now, "orch", stop=last)
        out.append(task)
    return out


def start(p, data: dict, now=None) -> dict:
    """New task (closes every open task idle for over 12 h as abandoned first). Returns the task-start record plus
    "open": the other tasks still open (parallel agents in one store)."""
    data, now = _obj(data), _now(now)
    fields = _fields("task-start", data)
    if not enabled():
        return {"disabled": True, "type": "task-start", **fields}
    with _lock(p):
        st = _load(p)
        closed = _close_stale(p, st, now)
        task = f"t{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        info = {"summary": fields["summary"], "started": _iso(now), "seq": 0, "last": None, "difficulties": []}
        if closed:
            fields["closed"] = closed
        sess = _session()
        if sess:
            fields["session"] = sess
        rec = _rec(task, info, "task-start", "agent", now, fields)
        st["open"][task] = info
        st["current"] = task
        C.save_state(p, STATE, st)
        _append(p, [rec])
    return dict(rec, open=[t for t in st["open"] if t != task])


def add(p, data: dict, by="agent", now=None) -> dict:
    """One plan|decision|step|difficulty|resolution|note record on the task named by data["task"], else the current
    task, else "untracked". A resolution's "for" must be a difficulty seq of the same task."""
    data, now = _obj(data), _now(now)
    if by not in ("agent", "orch"):
        raise JournalError(f'by must be "agent" or "orch", not {by!r}')
    kind = str(data.get("type") or "").strip()
    if kind in ("task-start", "task-end"):
        raise JournalError(f'"{kind}" records come from orch journal {kind[5:]}; run that command instead')
    if kind not in ADD_TYPES:
        raise JournalError(f'journal add needs "type", one of {", ".join(ADD_TYPES)}; send one JSON object, e.g.:\n'
                           f'{_example("decision")}')
    fields = _fields(kind, data)
    if not enabled():
        return {"disabled": True, "type": kind, **fields}
    with _lock(p):
        st = _load(p)
        task = _pick(st, data.get("task"), need_open=False)
        info = st["open"].get(task) or st["untracked"]
        if kind == "resolution":
            n, top = fields["for"], int(info.get("seq") or 0)
            if n > top:
                raise JournalError(f'resolution "for" is #{n} but the newest record of {task} is #{top}; use the seq '
                                   f'printed when the difficulty was added (orch journal show --task {task})')
            if task != "untracked" and n not in info.get("difficulties", []):
                listed = ", ".join(f"#{x}" for x in info.get("difficulties", [])) or "none"
                raise JournalError(f'resolution "for" #{n} is not a difficulty of {task} (its difficulties: {listed}); '
                                   f'use the difficulty\'s seq')
        rec = _rec(task, info, kind, by, now, fields)
        if kind == "difficulty" and task != "untracked":
            info.setdefault("difficulties", []).append(rec["seq"])
        C.save_state(p, STATE, st)
        _append(p, [rec])
    return rec


def end(p, data: dict, now=None) -> dict:
    """Close the task named by data["task"], else the current one: duration, counts, MEMORY.md section. Returns the
    task-end record plus "memory" (the MEMORY.md path) and "warnings" when MEMORY.md could not be written."""
    data, now = _obj(data), _now(now)
    fields = _fields("task-end", data)
    if not enabled():
        return {"disabled": True, "type": "task-end", **fields}
    with _lock(p):
        st = _load(p)
        task = _pick(st, data.get("task"), need_open=True)
        info = st["open"][task]
        if not fields.get("summary"):
            fields["summary"] = info.get("summary") or ""
        rec, warn = _finish(p, st, task, info, fields, now, "agent")
    out = dict(rec, memory=paths(p)["memory"].as_posix())
    if warn:
        out["warnings"] = [warn]
    return out


def _fix(cls, d) -> str:
    jid, node = d.get("jobid") or "<jobid>", d.get("node") or "<node>"
    return {"test-fail": f"read the failures with orch logs {jid}, fix the code, then run the job again",
            "usage": "fix the command or the setup it names (orch init when the setup is missing), then run it again",
            "oom": f"read orch logs {jid}; ask the user before raising the job's memory limit",
            "timeout": f"read orch logs {jid} to find what hangs; ask the user before raising the timeout",
            "infra": f"check the helpers with orch nodes; if {node} stays down, report it to the user",
            "r1-guard": f"stop: the apps on {node} degraded and orch cordoned it; tell the user",
            "deploy-gate": "do the first failing gate's fix, then run orch deploy --check again",
            "live-down": "tell the user the health check failed; roll back only when they ask, then orch live again",
            "not-admitted": "stop and report the reason line: no helper can take the job safely now",
            }.get(cls, "stop and report the exit code: orch ended in an unexpected way")


def _seqs(v) -> list:
    """A pending entry as a list of difficulty seqs (a state file of an older version holds one int)."""
    v = [v] if isinstance(v, int) else v if isinstance(v, list) else []
    return [x for x in v if isinstance(x, int) and not isinstance(x, bool)]


def record_invocation(p, verb, args, started, ended, exit_code, details=None):
    """orch's own record of one command: a step (redacted args, start/end, duration, exit, result, details), plus a
    difficulty when exit != 0 (class, detail and fix from details "class", "reason" or "error", and "fix" when the
    verb sets them, else from the exit code), one resolution per earlier unresolved difficulty of the same command
    (_signature) when it exits 0, and one decision per details["decisions"] item (init --apply/--approve). Attaches to
    the current task, else "untracked". started/ended: epoch seconds (time.time()), datetimes, or monotonic clock
    readings (time.monotonic(), anchored so ended = now). Never raises: a failure prints one warning line on
    stderr. No-op for p None (legacy ORCH_CONFIG mode), verb "journal" (its records are the agent's) and
    ORCH_JOURNAL=0."""
    if p is None or verb == "journal" or not enabled():
        return None
    try:
        details = dict(details or {})
        decisions = details.pop("decisions", None) or []
        args = args.split() if isinstance(args, str) else args
        if all(_isnum(x) for x in (started, ended)) and max(started, ended) < 1e9:  # monotonic clock readings
            t1 = _now()
            t0 = t1 - dt.timedelta(seconds=max(0.0, ended - started))
        else:
            t0, t1 = _now(started), _now(ended)
        code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
        name = EXIT_NAMES.get(code, "crash" if code is None else f"exit-{code}")
        argv = redact_args(args or [])
        what = _cap("orch " + " ".join(argv if argv[:1] == [str(verb)] else [str(verb), *argv]), 300)
        step = {"what": what, "verb": str(verb), "args": argv, "started": _iso(t0), "ended": _iso(t1),
                "duration_s": round(max(0.0, (t1 - t0).total_seconds()), 3), "exit": code,
                "result": str(details.get("result") or ("ok" if code == 0 else name)), "details": details}
        step = _clean(step)
        sig = _signature(verb, argv, details)
        with _lock(p):
            st = _load(p)
            task = st["current"] or "untracked"
            info = st["open"].get(task) or st["untracked"]
            recs = [_rec(task, info, "step", "orch", t1, step)]
            pending = info.get("pending")
            if not isinstance(pending, dict):
                pending = info["pending"] = {}  # command signature -> seqs of its unresolved difficulties
            # ci's child runs record each failed job themselves; the ci line would only repeat them
            if code != 0 and not (str(verb) == "ci" and details.get("jobs")):
                cls = str(details.get("class") or name)
                recs.append(_rec(task, info, "difficulty", "orch", t1, _clean({
                    "what": f"{what} exited {code} ({cls})" if code is not None else f"{what} crashed",
                    "class": cls, "detail": str(details.get("reason") or details.get("error") or ""),
                    "fix": str(details.get("fix") or _fix(cls, details)), "step": recs[0]["seq"]})))
                if task != "untracked":
                    info.setdefault("difficulties", []).append(recs[-1]["seq"])
                if sig:
                    pending[sig] = _seqs(pending.get(sig)) + [recs[-1]["seq"]]
            elif code == 0 and sig in pending:  # the same command passing later resolves every earlier failure of it
                how = f"{what} passed later (step #{recs[0]['seq']})"
                for n in _seqs(pending.pop(sig)):
                    recs.append(_rec(task, info, "resolution", "orch", t1, {"for": n, "how": how, "outcome": "pass"}))
            for d in decisions if isinstance(decisions, list) else []:
                d = {"what": d} if isinstance(d, str) else d
                if isinstance(d, dict) and d.get("what"):
                    recs.append(_rec(task, info, "decision", "orch", t1, _clean(
                        {"what": str(d["what"]), "why": str(d.get("why") or "user answer"),
                         **({"options": d["options"]} if isinstance(d.get("options"), list) else {})})))
            C.save_state(p, STATE, st)
            _append(p, recs)
        return recs[0]
    except Exception as e:  # the journal never fails an orch verb
        print(f"orch: warn: journal not written: {e}", file=sys.stderr)
        return None


_SIG_FLAGS = {"--check", "--apply", "--approve", "--detect", "--stages", "--jobs", "--node"}


def _signature(verb, argv, details) -> str | None:
    """What makes two invocations "the same command" for auto-resolution: the verb, the job (run/plan), the flags
    that change what is done (not --wait, --ref values and the like) and, for --apply (orch init), the sorted
    top-level answer keys from details["answer_keys"], so a good answer to one question never resolves a refused
    answer to another. None for read-only verbs, and for an --apply whose answer keys are unknown."""
    verb = str(verb)
    if verb in ("journal", "logs", "status", "nodes", "inventory", "show"):
        return None
    words, keep = [w for w in argv if w != verb], []
    for i, w in enumerate(words):
        if w in _SIG_FLAGS:
            keep.append(w)
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if w in ("--stages", "--jobs", "--node") and nxt and not nxt.startswith("-"):
                keep.append(nxt)
        elif not w.startswith("-") and (i == 0 or words[i - 1] not in ("--wait", "--ref", "--project", "--lease",
                                                                        "--heartbeat", "--task", "--last", "--days")):
            keep.append(w)
    d = details if isinstance(details, dict) else {}
    if "--apply" in keep:
        keys = d.get("answer_keys")
        if not isinstance(keys, (list, tuple)):
            return None
        keep.append("answers=" + ",".join(sorted({_cap(scrub(str(k)), 64) for k in keys})))
    job = d.get("job")
    return " ".join([verb] + ([str(job)] if job and str(job) not in keep else []) + keep)


# ----------------------------------------------------------------------------- read side
def read(p, task=None, last=None) -> list:
    """Records oldest first (rotated files, then journal.jsonl); task filters one task (or "untracked"), last keeps
    the newest N. Torn or hand-edited lines are skipped."""
    if last and not task:  # newest first: older rotated files are read only when the newer ones hold too few
        out = []
        for f in reversed(_files(p)):
            out = _read_file(f) + out
            if len(out) >= last:
                break
        return out[-int(last):]
    recs = _records(p, task=task or None)
    return recs[-int(last):] if last else recs


def _pct(xs, q):
    return xs[max(0, math.ceil(q / 100 * len(xs)) - 1)]


def _summ(xs) -> dict:
    xs = sorted(float(x) for x in xs if _isnum(x))
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "mean": round(sum(xs) / len(xs), 2), "p50": round(_pct(xs, 50), 2),
            "p95": round(_pct(xs, 95), 2)}


def stats(p, days=None, now=None) -> dict:
    """Numbers for monitoring: tasks by outcome and their durations, durations per orch verb and per job (from
    run steps), failure classes, helpers used, unresolved difficulties of real tasks, the most common difficulties.
    days keeps records from the last N days (by ts); a difficulty counts as resolved, and a task as ended, by any
    later record, also one outside the window."""
    now = _now(now)
    cutoff = now - dt.timedelta(days=days) if days else None
    # a rotated file last written before the cutoff holds nothing of the window, nor a later resolution or end
    recs = _records(p, since=cutoff.timestamp() if cutoff else None)

    def inside(r):
        if cutoff is None:
            return True
        t = _parse(r.get("ts"))
        return t is not None and t >= cutoff
    win = [r for r in recs if inside(r)]
    kind = collections.defaultdict(list)
    for r in win:
        kind[r.get("type")].append(r)
    ended = {r.get("task") for r in recs if r.get("type") == "task-end"}
    opened = sorted({r.get("task") for r in kind["task-start"] if r.get("task") not in ended})
    steps = [r for r in kind["step"] if r.get("by") == "orch"]
    verbs, jobs, helpers = collections.defaultdict(list), collections.defaultdict(list), collections.Counter()
    vfail, jres = collections.Counter(), collections.defaultdict(collections.Counter)
    for r in steps:
        v = str(r.get("verb") or "?")
        verbs[v].append(r.get("duration_s"))
        if r.get("exit") != 0:
            vfail[v] += 1
        d = r.get("details") if isinstance(r.get("details"), dict) else {}
        if v == "run" and d.get("job"):
            t = d.get("timings") if isinstance(d.get("timings"), dict) else {}
            wall = next((x for x in (d.get("wall_s"), t.get("wall_s")) if _isnum(x)), r.get("duration_s"))
            jobs[str(d["job"])].append(wall)
            jres[str(d["job"])][str(r.get("result") or "?")] += 1
            if d.get("node"):
                helpers[str(d["node"])] += 1
    fixed = {(r.get("task"), r.get("for")) for r in recs if r.get("type") == "resolution" and
             str(r.get("outcome") or "").lower() != "failed"}
    diffs = kind["difficulty"]
    unresolved = [{"task": r.get("task"), "seq": r.get("seq"), "ts": r.get("ts"), "what": r.get("what"),
                   "class": r.get("class")} for r in diffs
                  if r.get("task") != "untracked" and (r.get("task"), r.get("seq")) not in fixed]
    ends = kind["task-end"]
    return {"days": days, "since": _iso(cutoff) if cutoff else None, "records": len(win),
            "tasks": {"total": len(ends) + len(opened), "open": len(opened),
                      "by_outcome": dict(collections.Counter(str(r.get("outcome") or "?") for r in ends)),
                      "seconds": _summ(r.get("duration_s") for r in ends)},
            "verbs": {v: dict(_summ(xs), failed=vfail[v]) for v, xs in sorted(verbs.items())},
            "jobs": {j: dict(_summ(xs), results=dict(jres[j])) for j, xs in sorted(jobs.items())},
            "failure_classes": dict(collections.Counter(str(r.get("class") or "other") for r in diffs).most_common()),
            "helpers": dict(helpers.most_common()),
            "unresolved": unresolved[::-1][:50],
            "top_difficulties": [{"what": w, "count": n} for w, n in
                                 collections.Counter(str(r.get("what") or "?") for r in diffs).most_common(10)]}


# ----------------------------------------------------------------------------- rendering
def _one(s, n=400) -> str:
    return _cap(" ".join(str(s).split()), n)


def _sent(s, n=400) -> str:
    """_one without a trailing full stop, for text the renderer ends with its own."""
    return _one(s, n).rstrip(". ")


def _cell(s) -> str:
    return _one(s, 200).replace("|", "\\|") or "-"


def _step_time(r) -> str:
    if _isnum(r.get("duration_s")):
        return _dur(r["duration_s"])
    return "~" + _dur(r["since_prev_s"]) if _isnum(r.get("since_prev_s")) else "-"


def _step_outcome(r) -> str:
    if r.get("by") == "orch":
        res = str(r.get("result") or "?")
        return res if r.get("exit") in (0, None) else f"{res} (exit {r.get('exit')})"
    return str(r.get("outcome") or "-")


def render_task_md(records) -> str:
    """One task's MEMORY.md section: title (task id, local start, outcome, duration), summary, request, plan,
    decisions, steps table, difficulties with their resolutions (else UNRESOLVED), notes, lessons, open items."""
    recs = sorted((r for r in records if isinstance(r, dict)),
                  key=lambda r: r.get("seq") if _isnum(r.get("seq")) else 0)
    if not recs:
        return ""
    head = next((r for r in recs if r.get("type") == "task-start"), recs[0])
    tail = next((r for r in reversed(recs) if r.get("type") == "task-end"), None)
    if tail:
        outcome, dur = tail.get("outcome") or "?", tail.get("duration_s")
    else:
        a, b = _parse(recs[0].get("ts")), _parse(recs[-1].get("ts"))
        outcome, dur = "open", (b - a).total_seconds() if a and b else None
    out = [f"## {head.get('task', '?')} - {_local(head.get('ts'))} - {outcome} - {_dur(dur)}", ""]
    summary = (tail or {}).get("summary") or head.get("summary")
    for label, v in (("Summary", summary), ("Request", head.get("request")),
                     ("Closed by orch", (tail or {}).get("reason"))):
        if v:
            out.append(f"**{label}:** {_one(v, TEXT_MAX)}")
    plans = ([("Plan", head.get("plan"))] if head.get("plan") else []) + [
        (f"Plan update (#{r.get('seq')})", r.get("steps")) for r in recs if r.get("type") == "plan"]
    for label, steps in plans:
        out += ["", f"**{label}:**"] + [f"{i}. {_one(s)}" for i, s in enumerate(steps or [], 1)]
    decisions = [r for r in recs if r.get("type") == "decision"]
    if decisions:
        out += ["", "**Decisions:**"]
        for r in decisions:
            opts = f" Options: {_sent(', '.join(map(str, r['options'])))}." if r.get("options") else ""
            by = " (orch)" if r.get("by") == "orch" else ""
            out.append(f"- #{r.get('seq')} {_sent(r.get('what', ''))}{by}. Why: {_sent(r.get('why', ''))}.{opts}")
    steps = [r for r in recs if r.get("type") == "step"]
    if steps:
        out += ["", "**Steps:**", "", "| step | by | outcome | time |", "|---|---|---|---|"]
        out += [f"| #{r.get('seq')} {_cell(r.get('what', ''))} | {r.get('by', '?')} | {_cell(_step_outcome(r))} | "
                f"{_step_time(r)} |" for r in steps]
    diffs = [r for r in recs if r.get("type") == "difficulty"]
    if diffs:
        fixed, tries = _fixes(recs), collections.defaultdict(list)
        for r in recs:
            if r.get("type") == "resolution":
                tries[r.get("for")].append(r)
        out += ["", "**Difficulties:**"]
        for r in diffs:
            text = f"- #{r.get('seq')} {_sent(r.get('what', ''))}"
            if r.get("detail"):
                text += f": {_sent(r['detail'])}"
            for x in tries.get(r.get("seq"), []):
                verb = "Resolved by" if x in fixed.get(r.get("seq"), []) else "Tried"
                text += f". {verb} #{x.get('seq')}: {_sent(x.get('how', ''))}"
                text += f" ({_sent(x['outcome'], 80)})" if x.get("outcome") else ""
            out.append(text + ("." if r.get("seq") in fixed else ". UNRESOLVED"))
    notes = [r for r in recs if r.get("type") == "note"]
    if notes:
        out += ["", "**Notes:**"] + [f"- #{r.get('seq')} {_one(r.get('text', ''))}" for r in notes]
    for label, key in (("Lessons", "lessons"), ("Open items", "open")):
        if tail and tail.get(key):
            out += ["", f"**{label}:**"] + [f"- {_one(x)}" for x in tail[key]]
    c = (tail or {}).get("counts") or _counts(recs)
    out += ["", f"Counts: {_tally(c)}, orch time {_dur(c.get('orch_seconds'))}."]
    return "\n".join(out) + "\n"


def _tally(c) -> str:
    def n(k, one, many):
        x = c.get(k) or 0
        return f"{x} {one if x == 1 else many}"
    return (f"{n('steps', 'step', 'steps')}, {n('decisions', 'decision', 'decisions')}, "
            f"{n('difficulties', 'difficulty', 'difficulties')} ({c.get('resolved') or 0} resolved, "
            f"{c.get('unresolved') or 0} unresolved)")


def _brief(r) -> str:
    k = r.get("type")
    if k == "task-start":
        return str(r.get("summary") or "")
    if k == "plan":
        return "; ".join(map(str, r.get("steps") or []))
    if k == "decision":
        return f"{r.get('what', '')} (why: {r.get('why', '')})"
    if k == "step":
        return f"{r.get('what', '')} -> {_step_outcome(r)} {_step_time(r)}"
    if k == "difficulty":
        return str(r.get("what") or "") + (f" [{r['class']}]" if r.get("class") else "")
    if k == "resolution":
        return f"for #{r.get('for')}: {r.get('how', '')}" + (f" ({r['outcome']})" if r.get("outcome") else "")
    if k == "note":
        return str(r.get("text") or "")
    if k == "task-end":
        return f"{r.get('outcome', '?')} in {_dur(r.get('duration_s'))}: {r.get('summary') or ''}"
    return ""


def show_line(r, with_task=True) -> str:
    """Compact one-line view: local time, (task,) seq, type, by, what/outcome/duration."""
    head = _local(r.get("ts"), "%Y-%m-%d %H:%M:%S") + (f" {r.get('task', '?')}" if with_task else "")
    return _one(f"{head} #{r.get('seq', '?')} {r.get('type', '?')} {r.get('by', '?')} {_brief(r)}", 240)


def _span(s) -> str:
    if not s.get("n"):
        return "no data"
    return f"{s['n']}x mean {_dur(s['mean'])}, p50 {_dur(s['p50'])}, p95 {_dur(s['p95'])}"


def stats_lines(s) -> list:
    t = s["tasks"]
    window = f"last {s['days']:g} days" if s.get("days") else "all time"
    outs = ", ".join(f"{k} {n}" for k, n in sorted(t["by_outcome"].items())) or "none ended"
    out = [f"journal stats ({window}): {s['records']} records",
           f"tasks {t['total']} ({outs}; open {t['open']}); duration {_span(t['seconds'])}"]
    out += [f"verb {v}: {_span(x)}, {x['failed']} failed" for v, x in s["verbs"].items()]
    out += [f"job {j}: {_span(x)} ({', '.join(f'{k} {n}' for k, n in sorted(x['results'].items()))})"
            for j, x in s["jobs"].items()]
    for label, d in (("failure classes", s["failure_classes"]), ("helpers used", s["helpers"])):
        out.append(f"{label}: " + (", ".join(f"{k} {n}" for k, n in d.items()) or "none"))
    u = s["unresolved"]
    out.append(f"unresolved difficulties {len(u)}" + (": " + "; ".join(
        f"{x['task']} #{x['seq']} {x['what']}" for x in u[:5]) if u else ""))
    if s["top_difficulties"]:
        out.append("top difficulties: " + "; ".join(f"{x['count']}x {x['what']}" for x in s["top_difficulties"][:5]))
    return [_one(x, 600) for x in out]


# ----------------------------------------------------------------------------- command line
def project(start=None, run=subprocess.run, create=True, warnings=None):
    """The orch project: start (--project) beats ORCH_PROJECT beats the current folder, as for orch init. Without a
    setup yet, the store goes where orch init --apply saves the setup, so a task opened before the setup interview
    gets every record of it: inside .git in a git work tree (never committed); outside git only for a folder named
    by --project or ORCH_PROJECT, in <dir>/.orch, as orch init does. create makes that store (start, add, end; the
    not-a-git warning of a new <dir>/.orch is added to warnings); without it nothing is made (show, stats, path)."""
    env = dict(os.environ)
    if start:
        env.pop("ORCH_PROJECT", None)
    p = C.find_project(start, env=env, run=run)
    if p is not None:
        return p
    s = C._start_dir(start, env)
    g = C._git_dirs(s, run)
    if g is None and not (start or env.get("ORCH_PROJECT")):
        raise JournalError(f"{s.as_posix()} is not a git repository: pass --project DIR to keep orch's setup and "
                           f"journal in DIR/.orch")
    if not create:
        return C.Project(g[0], g[1] / "orch", True) if g else C.Project(s, s / ".orch", False)
    fresh = g is None and not (s / ".orch").is_dir()
    p, warns = C.init_project(start, env=env, run=run)
    if fresh and warnings is not None:
        warnings.extend(warns)
    return p


def _stdin(act) -> dict:
    ex = EXAMPLES["task-start" if act == "start" else "task-end" if act == "end" else "decision"]
    how = f"send it with a quoted heredoc:\n{_heredoc(act, ex)}"
    s = sys.stdin
    if s is None or s.isatty():
        raise JournalError(f"orch journal {act} reads one JSON object on stdin; {how}")
    raw = s.buffer.read(STDIN_MAX + 1) if hasattr(s, "buffer") else s.read(STDIN_MAX + 1).encode("utf-8")
    if len(raw) > STDIN_MAX:
        raise JournalError(f"orch journal {act}: stdin is over {STDIN_MAX >> 20} MB; keep journal text short")
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    if not text:
        raise JournalError(f"orch journal {act} got empty stdin; {how}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise JournalError(f"orch journal {act}: stdin is not valid JSON (line {e.lineno}, column {e.colno}: "
                           f"{e.msg}); {how}") from None
    if not isinstance(data, dict):
        raise JournalError(f"orch journal {act} takes one JSON object, not {_jtype(data)}; {how}")
    return data


def _say(line, obj) -> int:
    print(line)
    print(json.dumps(obj, ensure_ascii=False))
    return 0


def cmd_journal(a) -> int:
    """a.action start|add|end (one JSON object on stdin) prints one short line then one JSON line; show prints
    compact lines (--json: one record per line); stats prints a summary (--json: one object); path prints the
    journal and MEMORY.md paths. Exit 0, or 2 with a message naming the bad input."""
    act = getattr(a, "action", None)
    task, as_json = getattr(a, "task", None), getattr(a, "json", False)
    try:
        if act not in ACTIONS:
            raise JournalError(f"unknown journal action {act!r}; use one of {', '.join(ACTIONS)}")
        warns = []
        p = project(getattr(a, "project", None), create=act in ("start", "add", "end") and enabled(), warnings=warns)
        f = paths(p)
        if act == "path":
            if as_json:
                print(json.dumps({k: v.as_posix() for k, v in f.items()}))
            else:
                print(f"journal {f['journal'].as_posix()}")
                print(f"memory {f['memory'].as_posix()}")
            return 0
        if act == "show":
            last = getattr(a, "last", None)
            if last is not None and last < 0:
                raise JournalError("--last must be 0 or more (0 = every record)")
            recs = read(p, task=task, last=(SHOW_LAST if last is None and not task else last) or None)
            if as_json:
                lines = [json.dumps(r, ensure_ascii=False) for r in recs]
            else:
                lines = [show_line(r, with_task=not task) for r in recs] or [
                    "journal: no records" + (f" for task {task}" if task else "")]
            for line in lines:
                print(line)
            return 0
        if act == "stats":
            days = getattr(a, "days", None)
            if days is not None and not days > 0:
                raise JournalError("--days must be a number above 0")
            s = stats(p, days=days)
            if as_json:
                print(json.dumps(s, ensure_ascii=False))
            else:
                print("\n".join(stats_lines(s)))
            return 0
        data = _stdin(act)
        if task:
            data["task"] = task
        rec = start(p, data) if act == "start" else add(p, data) if act == "add" else end(p, data)
        if rec.get("disabled"):
            return _say("journal: off (ORCH_JOURNAL=0), nothing recorded", {"ok": True, "disabled": True})
        res = {"ok": True, "task": rec["task"], "seq": rec["seq"], "type": rec["type"]}
        warns += rec.get("warnings") or []
        if warns:
            res["warnings"] = warns
        if act == "start":
            extra = (f"; closed {len(rec['closed'])} idle task(s) as abandoned: {', '.join(rec['closed'])}"
                     if rec.get("closed") else "") + (f"; still open: {', '.join(rec['open'])}" if rec["open"] else "")
            return _say(f"journal: task {rec['task']} started{extra}",
                        dict(res, closed=rec.get("closed", []), open=rec["open"], next=NEXT_START))
        if act == "add":
            note = " (no open task: start one with orch journal start)" if rec["task"] == "untracked" else ""
            if rec["type"] == "difficulty":
                res["next"] = "when it is fixed:\n" + _heredoc("add", {"type": "resolution", "for": rec["seq"],
                                                                         "how": "..."})
            return _say(f"journal: {rec['task']} #{rec['seq']} {rec['type']} recorded{note}", res)
        c = rec["counts"]
        res.update(outcome=rec["outcome"], duration_s=rec["duration_s"], counts=c, memory=rec["memory"])
        return _say(f"journal: task {rec['task']} ended {rec['outcome']} in {_dur(rec['duration_s'])}: {_tally(c)}; "
                    f"summary in {rec['memory']}", res)
    except (JournalError, C.ConfigError) as e:
        print(f"orch journal: {e}", file=sys.stderr)
        print(json.dumps({"ok": False, "exit": 2, "error": str(e)}, ensure_ascii=False))
        return 2


def add_arguments(ap) -> None:
    ap.add_argument("action", choices=ACTIONS, help="start|add|end read one JSON object on stdin; show|stats|path read")
    ap.add_argument("--task", help="task id (default: the current task)")
    ap.add_argument("--last", type=int,
                    help=f"show: the newest N records (default {SHOW_LAST}, all of one --task; 0 = all)")
    ap.add_argument("--days", type=float, help="stats: only records of the last N days")
    ap.add_argument("--json", action="store_true", help="JSON output (show: one record per line)")
    ap.add_argument("--project", help="project directory (beats ORCH_PROJECT; default: ORCH_PROJECT, else the "
                                      "current folder)")


def add_parser(sp):
    """Register `orch journal` on orch.py's subparsers; dispatch it to cmd_journal."""
    p = sp.add_parser("journal", help="project journal: start|add|end (JSON on stdin), show, stats, path",
                      allow_abbrev=False)
    add_arguments(p)
    return p


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="orch journal", description=__doc__.splitlines()[0], allow_abbrev=False)
    add_arguments(ap)
    return cmd_journal(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
