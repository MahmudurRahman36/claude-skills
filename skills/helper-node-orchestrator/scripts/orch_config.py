#!/usr/bin/env python3
"""orch v2 project config: storage, the init state machine, the engine compiler and the guard view.

Spec: dev/v2-design.md sections 2-4 and 9, dev/v2-contract.md "orch_config.py". Pure module: it never runs ssh or
docker against a server. The only processes it starts (git, `ssh -G`, `docker context ls`, all local) go through an
injectable run= callable with subprocess.run's signature, so unit tests never touch the network.

Files (every write is atomic: tmp file + os.replace; 0600 where the OS supports it):
  <store>/config.json         user decisions only (schema 1); hashed for approval
  <store>/state/<name>.json   observations: hosts.json (probe records), jobs-detected.json (last proposal)
  <store>/state/runs.jsonl    one line per finished job run (deploy gate)
  $ORCH_HOME/registry.json    node name -> identity {hostname, port, docker_id}; list of project stores
  $ORCH_HOME/trust.json       store -> approved hash, time, script hashes, co-tenant snapshot, approved config
  $ORCH_HOME/guard-view.json  union of approved projects; read only by hooks/guard.py
"""
import concurrent.futures as cf
import contextlib
import copy
import datetime as dt
import glob
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = 1
DEFAULT_LABEL, DEFAULT_PREFIX = "io.orch.agent", "agentx-"
STAGES = ("lint", "unit", "build", "image", "e2e", "investigate")
SCOPES = {"all": set(STAGES), "tests": {"lint", "unit", "build", "e2e", "investigate"},
          "readonly": {"investigate"}, "none": set()}
SCOPE_WORDS = {"all": "everything incl. image builds", "tests": "lint + tests, no image builds",
               "readonly": "read-only commands only", "none": "nothing"}
SCOPE_RANK = {"none": 0, "readonly": 1, "tests": 2, "all": 3}
ROLES = ("helper", "prod")

# Thresholds and builder settings are the v1 values (config/nodes.json); images are the v1 digest-pinned refs.
DEFAULT_POLICY = {
    "admission": {"reserve_frac": 0.25, "psi_some_avg10_max": 10, "swap_pages_per_s_max": 50,
                  "disk_free_frac_min": 0.15, "disk_free_gb_min": 20, "inode_free_frac_min": 0.1,
                  "load_frac_max": 0.85, "max_heavy": 1},
    "score_weights": {"mem": 0.5, "load": 0.3, "affinity": 0.2},
    "watchdog": {"interval_s": 10, "psi_max": 25, "iowait_max": 30, "lat_factor": 2.0, "lat_add_s": 0.2,
                 "consecutive": 3, "guard_cordon_min": 30},
    "lease": {"default_s": 300, "heartbeat_s": 60, "builder_idle_h": 24},
    "subnet_pool": "10.252.20.0/24",
    "subnet_prefix": 28,
    "builder": {"image": "buildkit", "memory": "4g", "cpus": 4, "pids": 4096, "gc_keepstorage_mb": "2000,20000,15000",
                "cap_gb": 15, "cache_volume_cap_mb": 6000,
                "_doc": "buildkit GC keeps the builder at <= 15 GB (Reserved,Free,Maximum MB); each agentx-cache-* "
                        "volume is wiped after a job when it exceeds cache_volume_cap_mb; gc --session-end or 24 h "
                        "idle wipes both"},
    "limits": {"memory_max": "6g", "timeout_max": 3600},
    "require_digest": True,
    "deploy_require": ["lint", "unit"],
    "never_touch": [],
    "images": {
        "alpine": "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc",
        "buildkit": "moby/buildkit:v0.32.2-rootless@sha256:504731e577c20559c00f968f33219f30115e70be29ab96728d1d06e963fc494b"},
    "windows_host_services": ["sshd", "com.docker.service", "WinRM", "TermService", "LanmanServer"],
    # IO caps for job and builder containers on Linux helpers (docker --device-read-bps / --device-write-bps on the
    # probed Docker root device, --blkio-weight); null = no caps. e.g. {"read_bps": "150mb", "write_bps": "80mb",
    # "blkio_weight": 100}
    "io": None,
    # git pathspecs appended to `git archive` (the engine passes them through unchanged)
    "source_exclude": [":(exclude,glob)**/env.production*", ":(exclude,glob)**/.env.production*",
                       ":(exclude,glob)**/.env", ":(exclude,glob)**/*.pfx", ":(exclude,glob)**/*.pem"],
}
NODE_POLICY_KEYS = ("admission", "score_weights", "watchdog", "lease", "subnet_pool", "subnet_prefix", "builder",
                    "limits", "require_digest")
IO_KEYS = ("read_bps", "write_bps", "blkio_weight")
_RATE_RX = re.compile(r"([0-9]+(?:\.[0-9]+)?)(?:[kmgtp])?i?b?", re.I)  # docker's byte-size syntax: 150mb, 1.5g

def _t(**kw) -> dict:
    """A v1 built-in test job (fresh lists per job, so no two jobs share a mutable value)."""
    return {"kind": "container", "heavy": False, "requires": ["docker"], "test_only": True, "image": "alpine",
            "source": "none", "memory": "64m", "cpus": 0.5, "pids": 64, "timeout": 60, "network": "none",
            "cap_add": [], "shell": "sh", "result_paths": ["/out"], **kw}


BUILTIN_JOBS = {
    "shell-investigate": {"kind": "container", "heavy": False, "requires": ["docker"], "image": "alpine",
                          "source": "none", "read_only": True, "memory": "128m", "cpus": 0.5, "pids": 64,
                          "timeout": 120, "network": "none", "cap_add": [],
                          "cmd": "mkdir -p /tmp/out 2>/dev/null; {args}", "shell": "sh", "result_paths": [],
                          "stage": "investigate"},
    "t-ok": _t(cmd="mkdir -p /out && echo ok-$(uname -m) > /out/ok.txt && id && cat /proc/self/status | "
                   "grep -E '^(CapEff|NoNewPrivs)' && echo done"),
    "t-fail": _t(cmd="mkdir -p /out && echo 'not ok 1 - deliberate failure' | tee /out/t.log && exit 1",
                 fail_patterns=["not ok "]),
    "t-sleep": _t(timeout=8, cmd="mkdir -p /out && echo sleeping > /out/s.txt && sleep 120"),
    "t-oom": _t(cmd="mkdir -p /out && echo start > /out/o.txt && x=$(head -c 300000000 /dev/zero | "
                    "tr '\\0' a) && echo ${#x}"),
    "t-hold": _t(heavy=True, timeout=300, default_args="30",
                 cmd="mkdir -p /out && echo holding > /out/h.txt && sleep {args} && echo released"),
}

CONFIG_KEYS = ("schema", "hosts", "production", "deploy", "alerts", "jobs", "policy", "pending")
HOST_KEYS = {"ssh", "roles", "jobs", "watch"}
JOB_CFG_KEYS = {"stage", "kind", "image", "dir", "repo", "prep", "cmd", "cache", "env", "memory", "cpus", "pids",
                "timeout", "prep_timeout", "fail_patterns", "result_paths", "dockerfile", "context", "detected",
                "manifest", "files"}
NAME_RX = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
JOB_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
STATE_RX = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_USER_RX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
_HOST_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")
_SPEC_HINT = "type exactly what you put after ssh, e.g. deploy@192.0.2.10 or deploy@192.0.2.10:2222"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Project:
    root: Path
    store: Path
    git: bool


# ----------------------------------------------------------------------------- files
def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _write_bytes(path: Path, data: bytes) -> None:
    """Atomic write: unique tmp per call (threads share a pid), fsync, 0600 on POSIX; retries while Windows holds
    the target open (a reader or a virus scanner)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(3)}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        for i in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05 * (i + 1))
        raise ConfigError(f"could not replace {path}: another program keeps it open; close it and run the command again")
    finally:
        tmp.unlink(missing_ok=True)


def _write_json(path: Path, data) -> None:
    _write_bytes(path, (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


def _read_json(path: Path, default=None, strict=True):
    """default when absent; ConfigError on bad JSON when strict, else default."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as e:
        if strict:
            raise ConfigError(f"cannot read {path}: {e.strerror or e}; check its permissions") from None
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if strict:
            raise ConfigError(f"{path} is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg}); "
                              f"fix the file or delete it and run orch init again") from None
        return default


@contextlib.contextmanager
def _locked(path: Path, timeout: float = 30):
    """Cross-process lock on <path>.lock (msvcrt on Windows, flock elsewhere) for read-modify-write and appends."""
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+b") as f:
        end = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > end:
                    raise ConfigError(f"{lock} has been locked by another orch process for {timeout:.0f} s; wait for "
                                      f"it to finish (or end it) and run the command again") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _key(path) -> str:
    """Canonical absolute path string used as a JSON key (trust, registry) and printed in views."""
    return Path(path).resolve().as_posix()


def _find_key(d: dict, key: str):
    if key in d:
        return key
    if os.name == "nt":  # paths are case-insensitive there
        low = key.lower()
        return next((k for k in d if k.lower() == low), None)
    return None


def _run(run, argv, timeout=30):
    """run= wrapper: None when the program is missing, hangs or cannot start."""
    try:
        return run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ----------------------------------------------------------------------------- project and store
def home(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("ORCH_HOME") or Path.home() / ".helper-orch")


def _start_dir(start, env) -> Path:
    """An explicit start (--project DIR) beats ORCH_PROJECT, which beats the current folder."""
    env = os.environ if env is None else env
    s = Path(start or env.get("ORCH_PROJECT") or os.getcwd()).expanduser()
    if not s.is_dir():
        raise ConfigError(f"project directory {s} does not exist; pass an existing directory with --project DIR "
                          f"(or ORCH_PROJECT)")
    return s.resolve()


def _native(path: str) -> str:
    """On Windows, an MSYS2 or Cygwin git prints /c/work or /cygdrive/c/work; turn that into C:/work."""
    m = re.fullmatch(r"(?:/cygdrive)?/([A-Za-z])(/.*)?", path) if os.name == "nt" else None
    return f"{m.group(1).upper()}:{m.group(2) or '/'}" if m else path


def _git_dirs(start: Path, run):
    """One `git rev-parse --show-toplevel --git-common-dir` -> (toplevel, absolute common dir), or None when start is
    not in a git work tree (or git is not installed). The common dir comes back relative to start in the main work
    tree (".git", "../.git") and absolute in a linked worktree; either way it is the main repo's .git."""
    r = _run(run, ["git", "-C", str(start), "rev-parse", "--show-toplevel", "--git-common-dir"])
    if r is None:
        return None
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        low = err.lower()
        if "not a git repository" in low:
            return None
        if "dubious ownership" in low:
            m = re.search(r"repository at '([^']+)'", err)
            repo = m.group(1) if m else start.as_posix()
            raise ConfigError(f"git refuses to read {repo} (dubious ownership: another user owns it); "
                              f"fix: git config --global --add safe.directory {repo}")
        last = err.splitlines()[-1] if err else f"exit {r.returncode}"
        raise ConfigError(f"git rev-parse failed in {start.as_posix()}: {last}; run orch from the project's working "
                          f"tree or pass --project DIR")
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ConfigError(f"git rev-parse in {start.as_posix()} printed {len(lines)} line(s) instead of 2; update git "
                          f"(2.13 or newer) and run the command again")
    top, common = (Path(_native(x)) for x in lines[:2])
    if not common.is_absolute():
        common = start / common
    return top.resolve(), common.resolve()


def _walk_up(start: Path):
    for d in (start, *start.parents):
        if (d / ".orch" / "config.json").is_file():
            return Project(d, d / ".orch", False)
    return None


def find_project(start=None, env=None, run=subprocess.run):
    """The set-up project for start (an explicit start, i.e. --project, beats ORCH_PROJECT, which beats the cwd), else
    None. Git: <common-dir>/orch with a config.json (shared by all worktrees). Not git: the nearest
    <dir>/.orch/config.json walking up."""
    s = _start_dir(start, env)
    g = _git_dirs(s, run)
    if g:
        store = g[1] / "orch"
        return Project(g[0], store, True) if (store / "config.json").is_file() else None
    return _walk_up(s)


def _mkstore(store: Path, git: bool = True) -> None:
    """The store and its state folder (owner-only on POSIX). A store outside git (<dir>/.orch) also gets a .gitignore
    of "*", so a later git init, git add -A or a copy into a repository never commits the setup, the state or the
    journal (an existing one is left as it is)."""
    (store / "state").mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        for d in (store, store / "state"):
            os.chmod(d, 0o700)
    if not git and not (store / ".gitignore").exists():
        _write_bytes(store / ".gitignore", b"*\n")


def init_project(start=None, env=None, run=subprocess.run):
    """Like find_project but creates the store when missing. Returns (Project, warnings)."""
    s = _start_dir(start, env)
    g = _git_dirs(s, run)
    if g:
        p = Project(g[0], g[1] / "orch", True)
        _mkstore(p.store)
        return p, []
    p = _walk_up(s)
    if p:
        _mkstore(p.store, git=False)
        return p, []
    p = Project(s, s / ".orch", False)
    _mkstore(p.store, git=False)
    return p, [f"{s.as_posix()} is not a git repository, so orch keeps this setup in {p.store.as_posix()} (with a "
               f".gitignore that keeps it out of git); do not share or copy this folder with .orch in it"]


# ----------------------------------------------------------------------------- spec and config validation
def parse_spec(spec: str) -> dict:
    """[user@]host[:port] exactly as typed after ssh -> {"spec","user","host","port"}; ValueError on bad syntax.
    host = alias, DNS name, IPv4 or [IPv6]. A leading '-' is impossible, so a spec can never become an ssh option."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"ssh spec is empty; {_SPEC_HINT}")
    s = spec.strip()
    if len(s) > 255:
        raise ValueError(f"ssh spec {s[:40]!r}... is longer than 255 characters; {_SPEC_HINT}")
    bad = re.search(r"[^A-Za-z0-9@:.\[\]_-]", s)
    if bad:
        raise ValueError(f"ssh spec {s!r} contains {bad.group()!r}; spaces and shell characters are not allowed; "
                         f"{_SPEC_HINT}")
    user, rest = (s.rsplit("@", 1) if "@" in s else (None, s))
    if user is not None and not _USER_RX.fullmatch(user):
        raise ValueError(f"ssh spec {s!r}: user {user!r} is not a valid user name; {_SPEC_HINT}")
    port = None
    if rest.startswith("["):
        m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::([0-9]+))?", rest)
        try:
            host = str(ipaddress.IPv6Address(m.group(1))) if m else None
        except ValueError:
            host = None
        if host is None:
            raise ValueError(f"ssh spec {s!r}: {rest!r} is not a valid [IPv6] address, e.g. deploy@[2001:db8::10]:22")
        port = m.group(2)
    else:
        if rest.count(":") > 1:
            raise ValueError(f"ssh spec {s!r}: put an IPv6 address in brackets, e.g. deploy@[2001:db8::10]:22")
        host, _, port = rest.partition(":")
        port = port if ":" in rest else None
        if not _HOST_RX.fullmatch(host or "") or host.endswith(".") or "[" in host or "]" in host:
            raise ValueError(f"ssh spec {s!r}: {host!r} is not a valid host name or address; {_SPEC_HINT}")
        if re.fullmatch(r"[0-9.]+", host):
            try:
                ipaddress.IPv4Address(host)
            except ValueError:
                raise ValueError(f"ssh spec {s!r}: {host} is not a valid IPv4 address, e.g. 192.0.2.10") from None
    if port is not None:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError(f"ssh spec {s!r}: port {port!r} must be a number from 1 to 65535")
        port = int(port)
    return {"spec": s, "user": user, "host": host, "port": port}


def _ssh_args(spec: str) -> list:
    """alias -> [alias]; user@host:port -> ["-p", port, "user@host"] (orch_discover.ssh_args must agree)."""
    d = parse_spec(spec)
    dest = f"{d['user']}@{d['host']}" if d["user"] else d["host"]
    return ["-p", str(d["port"]), dest] if d["port"] else [dest]


def _url_ok(u) -> bool:
    return isinstance(u, str) and re.fullmatch(r"https?://[^\s]+", u) is not None


def health_url(e) -> str:
    """A deploy.health entry is a URL string or {"url": ..., "insecure": true} (self-signed internal certificate)."""
    return e.get("url", "") if isinstance(e, dict) else str(e)


def health_label(e) -> str:
    return health_url(e) + (" (insecure)" if isinstance(e, dict) and e.get("insecure") else "")


def _health_ok(e) -> bool:
    if isinstance(e, dict):
        return set(e) <= {"url", "insecure"} and _url_ok(e.get("url")) and isinstance(e.get("insecure", False), bool)
    return _url_ok(e)


def _one_line(v) -> bool:
    return isinstance(v, str) and v.strip() != "" and "\n" not in v and "\r" not in v


def _io_errors(v) -> list:
    """Shape errors of policy io: null, or {"read_bps", "write_bps"} rates such as "150mb" (or bytes per second as a
    whole number) plus an optional blkio_weight from 10 to 1000."""
    if v is None:
        return []
    ex = 'e.g. {"read_bps": "150mb", "write_bps": "80mb", "blkio_weight": 100}, or null for no IO caps'
    if not isinstance(v, dict):
        return [f"policy io must be an object, {ex}"]
    errs = [f"policy io: unknown key {k!r} (valid: {', '.join(IO_KEYS)})" for k in v if k not in IO_KEYS]
    for k in ("read_bps", "write_bps"):
        r = v.get(k)
        m = _RATE_RX.fullmatch(r) if isinstance(r, str) else None
        if not ((m and float(m.group(1)) > 0) or (type(r) is int and r > 0)):
            errs.append(f"policy io.{k} must be a rate above 0 such as 150mb (a number with kb, mb or gb; got {r!r})")
    w = v.get("blkio_weight")
    if w is not None and not (type(w) is int and 10 <= w <= 1000):
        errs.append(f"policy io.blkio_weight must be a whole number from 10 to 1000, or null (got {w!r})")
    return errs


def _check_config(cfg) -> list:
    """Shape errors of a config dict (decisions only; limits are the engine validators' job)."""
    if not isinstance(cfg, dict):
        return ["the top level must be a JSON object"]
    errs = [f"unknown key {k!r} (valid: {', '.join(CONFIG_KEYS)})" for k in cfg if k not in CONFIG_KEYS]
    if cfg and cfg.get("schema") != SCHEMA:
        errs.append(f"schema is {cfg.get('schema')!r}; this orch reads schema {SCHEMA}")
    hosts = cfg.get("hosts", {})
    if not isinstance(hosts, dict):
        errs.append("hosts must be an object of name -> host")
        hosts = {}
    for n, h in hosts.items():
        if not NAME_RX.fullmatch(str(n)):
            errs.append(f"host name {n!r} must be lowercase letters, digits and '-' (at most 32)")
        if not isinstance(h, dict):
            errs.append(f"host {n}: must be an object")
            continue
        errs += [f"host {n}: unknown key {k!r} (valid: {', '.join(sorted(HOST_KEYS))})" for k in h if k not in HOST_KEYS]
        try:
            parse_spec(h.get("ssh"))
        except ValueError as e:
            errs.append(f"host {n}: {e}")
        roles = h.get("roles")
        if not isinstance(roles, list) or not roles or any(r not in ROLES for r in roles):
            errs.append(f"host {n}: roles must be a non-empty list of helper/prod (got {roles!r})")
        elif "jobs" in h:
            if h["jobs"] not in SCOPES:
                errs.append(f"host {n}: jobs must be one of {', '.join(SCOPES)} (got {h['jobs']!r})")
            elif h["jobs"] == "all" and "prod" in roles:
                errs.append(f"host {n}: a production host never runs every job; use jobs tests, readonly or none")
        if "watch" in h and (not isinstance(h["watch"], list) or not all(_url_ok(u) for u in h["watch"])):
            errs.append(f"host {n}: watch must be a list of http(s) URLs")
    prod = cfg.get("production")
    if prod is not None and (not isinstance(prod, dict) or prod.get("answer") not in ("set", "none", "later")):
        errs.append('production must be {"answer": "set" | "none" | "later"}')
    dep = cfg.get("deploy")
    if dep is not None:
        if not isinstance(dep, dict):
            errs.append('deploy must be {"command", "health", "rollback"} or {"answer": "later"}')
        elif dep.get("answer") != "later":
            for k in dep:
                if k not in ("command", "health", "rollback", "answer"):
                    errs.append(f"deploy: unknown key {k!r} (valid: command, health, rollback)")
            for k in ("command", "rollback"):
                if dep.get(k) is not None and not _one_line(dep[k]):
                    errs.append(f"deploy.{k} must be one non-empty line or null")
            if not isinstance(dep.get("health", []), list) or not all(_health_ok(u) for u in dep.get("health", [])):
                errs.append('deploy.health must be a list of http(s) URLs or {"url": ..., "insecure": true}')
    al = cfg.get("alerts")
    if al is not None and (not isinstance(al, dict) or set(al) - {"command"}
                           or (al.get("command") is not None and not _one_line(al["command"]))):
        errs.append('alerts must be {"command": "<one line>" | null}')
    jobs = cfg.get("jobs", {})
    if not isinstance(jobs, dict):
        errs.append("jobs must be an object of name -> job")
        jobs = {}
    for n, j in jobs.items():
        if not JOB_RX.fullmatch(str(n)):
            errs.append(f"job name {n!r} must be letters, digits, '.', '_' or '-' (at most 64)")
        if n in BUILTIN_JOBS:
            errs.append(f"job name {n!r} is a built-in job; pick another name")
        if not isinstance(j, dict):
            errs.append(f"job {n}: must be an object")
            continue
        errs += [f"job {n}: unknown key {k!r}" for k in j if k not in JOB_CFG_KEYS]
        if j.get("kind", "container") not in ("container", "build"):
            errs.append(f"job {n}: kind must be container or build")
        if "stage" in j and j["stage"] not in STAGES:
            errs.append(f"job {n}: stage must be one of {', '.join(STAGES)}")
    pol = cfg.get("policy", {})
    if not isinstance(pol, dict):
        errs.append("policy must be an object")
    else:
        errs += [f"policy: unknown key {k!r}" for k in pol if k not in DEFAULT_POLICY]
        errs += _io_errors(pol.get("io"))
    if not isinstance(cfg.get("pending", {}), dict):
        errs.append("pending must be an object")
    return errs


def load_config(p) -> dict:
    path = Path(p.store) / "config.json"
    cfg = _read_json(path, {})
    errs = _check_config(cfg)
    if errs:
        raise ConfigError(f"{path.as_posix()}: " + "; ".join(errs) + "; fix the file or run orch init")
    return cfg


def save_config(p, cfg) -> None:
    cfg = {"schema": SCHEMA, **{k: v for k, v in cfg.items() if k != "schema"}}
    errs = _check_config(cfg)
    if errs:
        raise ConfigError("config not saved: " + "; ".join(errs))
    _write_json(Path(p.store) / "config.json", cfg)


def _state_path(p, name) -> Path:
    if not STATE_RX.fullmatch(str(name)):
        raise ValueError(f"state name {name!r} must be lowercase letters, digits, '_' or '-'")
    return Path(p.store) / "state" / f"{name}.json"


def load_state(p, name, default=None):
    """Observations are re-made by every probe, so an unreadable file is the same as no file."""
    return _read_json(_state_path(p, name), default, strict=False)


def save_state(p, name, data) -> None:
    _write_json(_state_path(p, name), data)


RUNS_KEEP = 10000  # newest run records kept; the deploy gate only asks about recent shas


def append_run(p, rec: dict) -> None:
    """Adds one JSON line. Parallel `orch ci` children serialise on a lock; the file is rewritten atomically (tmp +
    replace), so a crash never leaves a torn line, and only the newest RUNS_KEEP records are kept."""
    path = Path(p.store) / "state" / "runs.jsonl"
    line = (json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    with _locked(path):
        try:
            old = path.read_bytes().splitlines(keepends=True)
        except FileNotFoundError:
            old = []
        old = [x if x.endswith(b"\n") else x + b"\n" for x in old[-(RUNS_KEEP - 1):]]
        _write_bytes(path, b"".join(old) + line)


def read_runs(p) -> list:
    """Every run record; a torn or hand-edited line is skipped, never fatal."""
    try:
        text = (Path(p.store) / "state" / "runs.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


# ----------------------------------------------------------------------------- hash, loosening
def _decisions(cfg) -> dict:
    """The hashed part of a config: everything except pending answers; a missing schema counts as SCHEMA."""
    return {"schema": SCHEMA, **{k: v for k, v in (cfg or {}).items() if k != "pending"}}


def config_hash(cfg: dict) -> str:
    body = json.dumps(_decisions(cfg), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _roles(h) -> list:
    return [r for r in ROLES if r in ((h or {}).get("roles") or [])]


PROD_SCOPE = "readonly"  # the default job scope of a helper that is also production: no heavy work there by default


def _scope(h) -> str:
    """Saved job scope; on a production host the default is readonly (all is never allowed there: it falls back to
    the default too; tests stays when the user chose it), else all."""
    prod = "prod" in _roles(h)
    s = (h or {}).get("jobs")
    if s not in SCOPES or (s == "all" and prod):
        s = PROD_SCOPE if prod else "all"
    return s


def _label(name, h) -> str:
    return f"{name} ({(h or {}).get('ssh', '?')})"


def _run_stage(j) -> str:
    """The stage the engine runs a job under: a build job always builds an image (as _translate and the deploy gate)."""
    j = j if isinstance(j, dict) else {}
    return "image" if j.get("kind") == "build" else (j.get("stage") or "unit")


def _narrowest(stage):
    """The narrowest helper scope that runs project jobs of this stage (readonly runs none), or None."""
    return next((s for s in ("tests", "all") if stage in SCOPES[s]), None)


def _deploy_require(cfg) -> list:
    """policy.deploy_require read the way orch_deploy reads it (a list or a comma string; default lint, unit)."""
    v = ((cfg or {}).get("policy") or {}).get("deploy_require", DEFAULT_POLICY["deploy_require"])
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list):
        v = DEFAULT_POLICY["deploy_require"]
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def _job_loosening(old, new) -> list:
    """A job the deploy gate requires (stage in deploy_require) that is removed or leaves those stages, and a job moved
    into a stage that a narrower helper scope runs (an image build turned into a test runs on lint + tests hosts)."""
    oj, nj = old.get("jobs") or {}, new.get("jobs") or {}
    req, out = _deploy_require(old), []
    for n, j in oj.items():
        if not isinstance(j, dict):
            continue
        g, was = nj.get(n), _run_stage(j)
        if not isinstance(g, dict):
            if was in req:
                out.append(f"job {n} ({was}) removed, so the deploy gate stops requiring it to pass")
            continue
        now = _run_stage(g)
        if was in req and now not in req:
            out.append(f"job {n}: stage {was} -> {now}, so the deploy gate stops requiring it to pass")
        a, b = _narrowest(was), _narrowest(now)
        if b and (a is None or SCOPE_RANK[b] < SCOPE_RANK[a]):
            out.append(f"job {n}: stage {was} -> {now}, so helpers limited to {SCOPE_WORDS[b]} now run it")
    return out


def loosening(old: dict, new: dict) -> list:
    """Reasons new protects less than old (guard asks before such a save); [] = same or tighter."""
    old, new = old or {}, new or {}
    oh, nh = old.get("hosts") or {}, new.get("hosts") or {}
    out = []
    for n, h in oh.items():
        g = nh.get(n)
        if "prod" in _roles(h) and (g is None or "prod" not in _roles(g)):
            out.append(f"{_label(n, h)} is no longer a production server")
        if g is None:
            continue
        if g.get("ssh") != h.get("ssh"):
            out.append(f"{n}: ssh target changed from {h.get('ssh')} to {g.get('ssh')}")
        if "helper" in _roles(g) and "helper" in _roles(h):
            was = h.get("jobs") if h.get("jobs") in SCOPES else "none"
            if SCOPE_RANK[_scope(g)] > SCOPE_RANK[was]:
                out.append(f"{_label(n, g)}: job scope widened from {was} to {_scope(g)}")
        gone = [u for u in h.get("watch") or [] if u not in (g.get("watch") or [])]
        if gone:
            out.append(f"{_label(n, g)}: stops watching {', '.join(gone)}")
    for n, g in nh.items():
        if "helper" in _roles(g) and "helper" not in _roles(oh.get(n)):
            out.append(f"new helper {_label(n, g)} ({SCOPE_WORDS[_scope(g)]})")
    oa, na = (old.get("alerts") or {}).get("command"), (new.get("alerts") or {}).get("command")
    if na != oa and (oa or na):  # orch runs this command itself on the master, so adding one needs approval too
        out.append(f"alert command added: {na}" if not oa else
                   f"alert command {'removed' if not na else 'changed to ' + na} (was {oa})")
    od, nd = old.get("deploy") or {}, new.get("deploy") or {}
    for k in ("command", "rollback"):
        if nd.get(k) and nd.get(k) != od.get(k):
            out.append(f"{'deploy' if k == 'command' else 'rollback'} command "
                       f"{'changed to' if od.get(k) else 'added:'} {nd[k]}")
    out += _job_loosening(old, new)
    if (old.get("policy") or {}) != (new.get("policy") or {}):
        out.append("policy overrides changed")
    return out


# ----------------------------------------------------------------------------- registry and trust
def registry_load(home) -> dict:
    reg = _read_json(Path(home) / "registry.json", {})
    if not isinstance(reg, dict):
        raise ConfigError(f"{Path(home) / 'registry.json'} must hold a JSON object; fix or delete it")
    reg.setdefault("nodes", {})
    reg.setdefault("projects", [])
    return reg


def registry_save(home, reg) -> None:
    _write_json(Path(home) / "registry.json", reg)


def _base_name(hostname: str) -> str:
    h = (hostname or "").strip().strip("[]").lower()
    try:
        ip = ipaddress.ip_address(h)
        return "h" + (h.rsplit(".", 1)[1] if ip.version == 4 else format(int(ip) & 0xFFFF, "x"))
    except ValueError:
        pass
    return re.sub(r"[^a-z0-9-]+", "-", h.split(".", 1)[0]).strip("-")[:24] or "host"


def assign_name(reg: dict, identity: dict) -> str:
    """Node name for a resolved identity {hostname, port, docker_id}: h<last IPv4 octet>, else the first DNS label.
    Same hostname+port (and docker_id when both are known) -> the same name; another machine on a taken name gets a
    suffix (h11b, h11c ...). Records the identity in reg; the caller saves it with registry_save."""
    hn = str(identity.get("hostname") or "").strip().lower()
    if not hn:
        raise ValueError("identity has no hostname; probe the host (ssh -G) before naming it")
    port, did = int(identity.get("port") or 22), identity.get("docker_id") or None
    nodes = reg.setdefault("nodes", {})
    for name, ident in nodes.items():
        if str(ident.get("hostname") or "").lower() == hn and int(ident.get("port") or 22) == port:
            od = ident.get("docker_id")
            if not (od and did and od != did):
                if did and not od:
                    ident["docker_id"] = did
                return name
    base = _base_name(hn)
    name = next((base + s for s in ["", *"bcdefghijklmnopqrstuvwxyz"] if base + s not in nodes), None)
    if name is None:
        name = next(f"{base}-{i}" for i in range(2, 10 ** 6) if f"{base}-{i}" not in nodes)
    nodes[name] = {"hostname": hn, "port": port, "docker_id": did}
    return name


def _trust_all(home) -> dict:
    t = _read_json(Path(home) / "trust.json", {})
    if not isinstance(t, dict):
        raise ConfigError(f"{Path(home) / 'trust.json'} must hold a JSON object; delete it and approve again "
                          f"(orch init --approve)")
    return t


def trust_get(home, p):
    t = _trust_all(home)
    k = _find_key(t, _key(p.store))
    return t[k] if k else None


def _cotenant_names(rec) -> list | None:
    co = ((rec or {}).get("facts") or {}).get("cotenants")
    return None if co is None else sorted(str(c.get("name", "?")) if isinstance(c, dict) else str(c) for c in co)


def trust_set(home, p, cfg, scripts: dict, state_hosts=None) -> None:
    """Record approval of cfg for p's store: hash, time, deploy/rollback script hashes, the co-tenant snapshot per
    host (state_hosts or the store's state/hosts.json) and the approved config itself, so the guard view keeps
    protecting with the last approved decisions while a later edit waits for approval. Registers the store too."""
    sh = load_state(p, "hosts", {}) if state_hosts is None else state_hosts
    sh = sh if isinstance(sh, dict) else {}
    cot = {n: c for n in (cfg.get("hosts") or {}) if (c := _cotenant_names(sh.get(n))) is not None}
    entry = {"hash": config_hash(cfg), "approved_at": _now(), "scripts": dict(scripts or {}), "cotenants": cot,
             "root": Path(p.root).as_posix(), "git": bool(p.git), "config": copy.deepcopy(_decisions(cfg))}
    h, key = Path(home), _key(p.store)
    with _locked(h / "trust.json"):
        t = _trust_all(h)
        t.pop(_find_key(t, key) or key, None)
        t[key] = entry
        _write_json(h / "trust.json", t)
    with _locked(h / "registry.json"):
        reg = registry_load(h)
        if not _find_key(dict.fromkeys(reg["projects"]), key):
            reg["projects"].append(key)
            registry_save(h, reg)


def _approved_cfg(store: Path, entry):
    """The config a trust entry approves: the store's config.json while its hash matches, else the approved snapshot
    (an edit waiting for approval never drops protection early), else None."""
    if not isinstance(entry, dict):
        return None
    try:
        now = _read_json(Path(store) / "config.json", None)
    except ConfigError:
        now = None
    snap = entry.get("config")
    if isinstance(now, dict) and config_hash(now) == entry.get("hash"):
        return now
    if isinstance(snap, dict) and config_hash(snap) == entry.get("hash"):
        return snap
    return None


def _ident(spec, rec=None, node=None, run=None):
    """(hostname, port) a saved host reaches: its probe resolution (state/hosts.json), else its registry identity, else
    the spec itself (an alias through the local `ssh -G` when run is given). None when the spec is unusable."""
    res, node = (rec or {}).get("resolved") or {}, node or {}
    try:
        ps = parse_spec(spec)
    except ValueError:
        ps = None
    host = res.get("hostname") or node.get("hostname")
    port = res.get("port") or node.get("port") or (ps or {}).get("port")
    if not host and ps:
        host = ps["host"]
        if run is not None and not ps["user"] and not ps["port"] and not _is_ip(host):
            g = _ssh_g(run, host, None)
            if g:
                host, port = g[0], port or g[1]
    if not host:
        return None
    try:
        port = int(port or 22)
    except (TypeError, ValueError):
        port = 22
    return str(host).strip().strip("[]").lower(), port


def master_prod(home, run=subprocess.run, exclude=None) -> dict:
    """(hostname, port) -> [project roots] for every host that an APPROVED setup on this master saves as production
    (role prod, alone or with helper). Built like build_guard_view: the stores of the registry and trust.json, each
    one's approved config (or its approved snapshot while an edit waits), identities from the store's
    state/hosts.json, else the registry, else the spec. exclude = a store (the caller's own project) left out: a
    project is matched by its store, so a linked worktree of the same repository (another root, the same store) is
    never "another project", whichever worktree approved last. Never raises; {} when nothing is approved."""
    h = Path(home)
    try:
        skip = _key(exclude) if exclude is not None else None
    except (OSError, RuntimeError, TypeError, ValueError):
        skip = None
    try:
        trust = _trust_all(h)
    except (ConfigError, OSError):
        return {}
    try:
        reg = registry_load(h)
    except (ConfigError, OSError):
        reg = {"nodes": {}, "projects": []}
    nodes = reg.get("nodes") if isinstance(reg.get("nodes"), dict) else {}
    projects = [x for x in reg.get("projects") or [] if isinstance(x, str)] if isinstance(reg.get("projects"), list) else []
    out = {}
    for key in dict.fromkeys(projects + list(trust)):
        if skip is not None and _same_path(key, skip):
            continue
        try:
            tk = _find_key(trust, key)
            entry = trust.get(tk) if tk else None
            store = Path(key)
            cfg = _approved_cfg(store, entry)
            if cfg is None:
                continue
            sh = _read_json(store / "state" / "hosts.json", {}, strict=False)
            sh = sh if isinstance(sh, dict) else {}
            root = str(entry.get("root") or (store.parent.parent if store.name == "orch" else store.parent).as_posix())
            for n, hc in (cfg.get("hosts") or {}).items():
                if not isinstance(hc, dict) or "prod" not in _roles(hc):
                    continue
                rec, node = sh.get(n), nodes.get(n)
                ident = _ident(hc.get("ssh"), rec if isinstance(rec, dict) else None,
                               node if isinstance(node, dict) else None, run)
                if ident and root not in out.setdefault(ident, []):
                    out[ident].append(root)
        except Exception:  # noqa: BLE001 - one unreadable store never hides the others (contract: never raises)
            continue
    return out


def _same_path(a, b) -> bool:
    a, b = Path(a).as_posix(), Path(b).as_posix()
    return a.lower() == b.lower() if os.name == "nt" else a == b


def _elsewhere(p, h, rec, prod) -> list:
    """Roots of the OTHER approved projects that save this host as production (prod = master_prod output)."""
    if not prod:
        return []
    st = Path(p.store)
    mine = (Path(p.root), st.parent.parent if st.name == "orch" else st.parent)
    return [r for r in prod.get(_ident((h or {}).get("ssh"), rec)) or []
            if not any(_same_path(r, m) for m in mine)]


# ----------------------------------------------------------------------------- summaries
def _mem_words(m) -> str:
    mm = re.fullmatch(r"\s*([0-9.]+)\s*([kmgt]?)b?\s*", str(m or "").lower())
    if not mm:
        return "-"
    unit = {"": "B", "k": "KB", "m": "MB", "g": "GB", "t": "TB"}[mm.group(2)]
    return f"{mm.group(1)} {unit}"


def _stage(j) -> str:
    return j.get("stage") or ("image" if j.get("kind") == "build" else "unit")


def job_line(name, j) -> str:
    """`api-test  unit  dotnet 8  api/  3 GB  dotnet test --no-restore` (same format as orch_detect proposals)."""
    d = (j.get("dir") or j.get("context") or "").strip("/")
    cmd = j.get("cmd") or (f"docker build -f {j.get('dockerfile') or 'Dockerfile'}" if j.get("kind") == "build" else "-")
    stack = (j.get("detected") or "").split(" (")[0] or (j.get("image") or "-").split("@")[0]
    return "  ".join([name, _stage(j), stack, (d + "/") if d and d != "." else "./", _mem_words(j.get("memory")), cmd])


def _facts_text(facts) -> str:
    if not facts:
        return "facts unknown"
    out = ["windows"] if facts.get("os") == "windows" else []
    if facts.get("cpus"):
        out.append(f"{facts['cpus']} cpu")
    if facts.get("mem_bytes"):
        out.append(f"{max(1, round(facts['mem_bytes'] / 2 ** 30))} GB")
    if facts.get("buildx") is False:
        out.append("no buildx")
    co = facts.get("cotenants")
    out.append("other containers unknown" if co is None else "no other containers" if not co
               else f"{len(co)} other container{'s' if len(co) != 1 else ''}")
    return ", ".join(out)


def _role_words(roles) -> str:
    return "helper + production" if "helper" in roles and "prod" in roles else "helper only" if "helper" in roles \
        else "production only"


def _prod_hosts(cfg) -> list:
    """Production hosts: prod-only first, then helpers that are also production (config order)."""
    hosts = cfg.get("hosts") or {}
    only = [n for n, h in hosts.items() if _roles(h) == ["prod"]]
    return only + [n for n, h in hosts.items() if "prod" in _roles(h) and n not in only]


def summary_lines(cfg, state_hosts, full=False) -> list:
    """Readable setup: <= 7 lines (approve step), or every detail with full=True (orch init --show)."""
    cfg, st = cfg or {}, state_hosts or {}
    hosts = cfg.get("hosts") or {}
    out = []
    if full:
        for n, h in hosts.items():
            rec = st.get(n) or {}
            line = f"host {_label(n, h)}: {_role_words(_roles(h))}"
            if "helper" in _roles(h):
                line += f"; jobs {_scope(h)} ({SCOPE_WORDS[_scope(h)]})" if "jobs" in h else "; jobs not chosen yet"
            line += f"; {_facts_text(rec.get('facts'))}"
            line += (f"; probed {rec.get('probed', '?')} " + ("ok" if rec.get("ok") else "with a problem")) if rec \
                else "; not probed yet"
            out.append(line + (f"; watch {', '.join(h['watch'])}" if h.get("watch") else ""))
    else:
        hl = [f"{_label(n, h)} " + (SCOPE_WORDS[_scope(h)] if "jobs" in h else "jobs not chosen yet")
              + (" (also production)" if "prod" in _roles(h) else "")
              for n, h in hosts.items() if "helper" in _roles(h)]
        out.append("helpers: " + ("; ".join(hl) or "none yet"))
    ans = (cfg.get("production") or {}).get("answer")
    pl = [_label(n, hosts[n]) for n in _prod_hosts(cfg)]
    out.append("production: " + (", ".join(pl) if pl else ans if ans in ("none", "later")
                                 else "none listed" if ans == "set" else "not answered yet"))
    dep = cfg.get("deploy")
    if not dep:
        out.append("deploy: not answered yet")
    elif dep.get("answer") == "later":
        out.append("deploy: later")
    else:
        out.append(f"deploy: {dep.get('command') or '-'} | health {', '.join(health_label(h) for h in dep.get('health') or []) or '-'} | "
                   f"rollback {dep.get('rollback') or '-'}")
    al = cfg.get("alerts")
    out.append("alerts: " + ("not answered yet" if al is None else al.get("command") or "none"))
    if not full:
        wl = [f"{_label(n, h)} {', '.join(h['watch'])}" for n, h in hosts.items() if h.get("watch")]
        if wl:
            out.append("watch: " + "; ".join(wl))
    jobs = cfg.get("jobs")
    if jobs is None:
        out.append("jobs: not chosen yet")
    elif not jobs:
        out.append("jobs: none")
    elif full:
        out += [f"job {job_line(n, j)}  image {j.get('image') or '-'}" for n, j in jobs.items()]
    else:
        out.append(f"jobs: {', '.join(f'{n} ({_stage(j)})' for n, j in jobs.items())}")
    if full:
        out.append("policy overrides: " + (json.dumps(cfg["policy"], sort_keys=True) if cfg.get("policy") else "none"))
    return out


def _short(cfg, extra=()) -> str:
    """One line: `helpers h11 (deploy@192.0.2.11) all | prod p20 (deploy@192.0.2.20:2222) | <extra> | 4 jobs`."""
    hosts = cfg.get("hosts") or {}
    hl = ", ".join(f"{_label(n, h)} {_scope(h)}" for n, h in hosts.items() if "helper" in _roles(h)) or "none"
    pl = [n if "helper" in _roles(hosts[n]) else _label(n, hosts[n]) for n in _prod_hosts(cfg)]
    ans = (cfg.get("production") or {}).get("answer")
    nj = len(cfg.get("jobs") or {})
    pw = ", ".join(pl) if pl else ans if ans in ("none", "later") else "none"
    return " | ".join([f"helpers {hl}", f"prod {pw}", *extra, f"{nj} job{'s' if nj != 1 else ''}"])


def _ready_line(cfg, trust) -> str:
    return f"setup ok: {_short(cfg)} | approved {str(trust.get('approved_at', '?'))[:10]}"


def _approve_line(cfg) -> str:
    """The approve question's one-line digest: who runs what, and every command orch runs or lets through."""
    dep, al = cfg.get("deploy"), cfg.get("alerts")
    d = "not answered" if not dep else "later" if dep.get("answer") == "later" else dep.get("command") or "-"
    extra = [f"deploy {d}"] + ([f"rollback {dep['rollback']}"] if dep and dep.get("rollback") else []) + \
        [f"alerts {'not answered' if al is None else al.get('command') or 'none'}"]
    return "Setup: " + _short(cfg, extra)


# ----------------------------------------------------------------------------- status (design section 4)
def _ask(kind, header, question, options=(), multi=False, answer_key=None, example=None) -> dict:
    return {"kind": kind, "header": header, "question": question, "options": list(options), "multi": multi,
            "answer_key": answer_key, "example": example}


def _problems(cfg, state_hosts) -> list:
    """[(name, is_helper, problem dict)] for every configured host that is not probed ok."""
    out = []
    for n, h in (cfg.get("hosts") or {}).items():
        rec = (state_hosts or {}).get(n)
        if rec and rec.get("ok"):
            continue
        pr = (rec or {}).get("problem") or {}
        out.append((n, "helper" in _roles(h), {
            "host": _label(n, h),
            "what": pr.get("what") or ("not probed yet" if not rec else "probe failed"),
            "tried": pr.get("tried") or "-",
            "fix": pr.get("fix") or ("run orch init (without --offline) to probe it" if not rec
                                     else "run orch init again to re-probe it")}))
    return out


def _host_default(h, facts, prod_elsewhere=False):
    """(roles, scope) shown in brackets: the saved answer, else production only where the user said so (here, or in
    another approved project) -> helper + production with read-only commands only (PROD_SCOPE), shared or unknown ->
    helper only with lint + tests (no image builds next to other apps), dedicated -> helper only with everything.
    A helper is never proposed as production on a guess: roles come from the user's answers."""
    if "jobs" in h and _roles(h):
        return _roles(h), _scope(h)
    if prod_elsewhere or "prod" in _roles(h):
        return ["helper", "prod"], PROD_SCOPE
    co = (facts or {}).get("cotenants")
    if co is None or co:
        return ["helper"], "tests"
    return ["helper"], "all"


def _bracket(roles, scope) -> str:
    return f"[{_role_words(roles)}; {'everything' if scope == 'all' else SCOPE_WORDS[scope]}]"


def _setup_open(cfg) -> dict:
    """What the setup step still has to ask: helpers without a job scope, and whether production, deploy (with health
    and rollback) and alerts are unanswered. Empty values everywhere = the setup step is done."""
    hosts = cfg.get("hosts") or {}
    return {"helpers": [n for n, h in hosts.items() if "helper" in _roles(h) and "jobs" not in h],
            "production": (cfg.get("production") or {}).get("answer") not in ("set", "none", "later"),
            "deploy": not cfg.get("deploy"), "alerts": cfg.get("alerts") is None}


def _setup_ask(p, cfg, state_hosts, prod=None) -> dict:
    """Only what is still unanswered (design: later sessions ask only what is missing); the example holds the same
    keys, so "ok" never re-sends a saved answer. A first run asks the whole block."""
    hosts, sh, todo = cfg.get("hosts") or {}, state_hosts or {}, _setup_open(cfg)
    lines, example = ["Defaults are in [brackets]; reply ok to accept them all, or change any line."], {}
    if todo["helpers"]:
        lines.append("Helpers (role: helper only | helper + production; jobs: everything | lint + tests, no image "
                     "builds | read-only commands only | nothing):")
        ex_hosts = {}
        for n in todo["helpers"]:
            h, rec = hosts[n], sh.get(n) or {}
            facts, other = rec.get("facts"), _elsewhere(p, hosts[n], rec, prod)
            roles, scope = _host_default(h, facts, bool(other))
            where = f", production server in {', '.join(other)}" if other else ""
            lines.append(f"  {_label(n, h)}: {_facts_text(facts)}{where} {_bracket(roles, scope)}")
            ex_hosts[n] = {"roles": roles, "jobs": scope, **({"watch": list(h["watch"])} if h.get("watch") else {})}
        example["hosts"] = ex_hosts
    if todo["production"]:
        only = [hosts[n]["ssh"] for n in _prod_hosts(cfg) if _roles(hosts[n]) == ["prod"]]
        lines.append(f"Production servers (what you type after ssh, comma separated), or none / later "
                     f"[{', '.join(only) or 'none'}]")
        example["production"] = only or "none"
    if todo["deploy"]:
        lines += ["Deploy command - runs on THIS machine and must reach production itself, e.g. ./deploy.sh [later]",
                  "Health URL(s) checked after a deploy [-]", "Rollback command (optional) [-]"]
        example["deploy"] = "later"
    if todo["alerts"]:
        lines.append("Alert command when a helper's own apps degrade (optional; gets the message on stdin) [none]")
        example["alerts"] = "none"
    if todo["helpers"]:
        watch = [f"{n}: {', '.join(hosts[n]['watch'])}" for n in todo["helpers"] if hosts[n].get("watch")]
        lines.append(f"Extra app URLs to watch on shared helpers (optional) [{'; '.join(watch) or '-'}]")
    return _ask("text", "Setup", "\n".join(lines), answer_key="setup", example=example)


def _jobs_ask(p, detected) -> dict:
    if detected is None:
        return _ask("text", "Jobs", "This project has not been scanned for jobs yet; run: orch init --detect",
                    answer_key=None, example=None)
    dj = detected.get("jobs") or {}
    extra = [f"note: {x}" for x in detected.get("notes") or []] + \
            [f"not recognised: {x}" for x in detected.get("unknown") or []]
    if not dj:
        name = (re.sub(r"[^a-z0-9-]+", "-", Path(p.root).name.lower()).strip("-") or "app") + "-test"
        q = "\n".join(extra + ["No known stack found (dotnet, node, python, Dockerfile). Which image and test command "
                               "should orch run? e.g. python:3.12-slim and pytest -q, or none"])
        return _ask("text", "Jobs", q, answer_key="jobs",
                    example={"jobs": {name: {"stage": "unit", "image": "python:3.12-slim", "cmd": "pytest -q"}}})
    q = "\n".join(["Jobs found in this project:"] + [f"  {job_line(n, j)}" for n, j in dj.items()] + extra
                  + ["Use these jobs? (yes / change: tell me what to change)"])
    return _ask("choice", "Jobs", q, ["yes", "change"], answer_key="jobs", example={"jobs": "detected"})


def _fix_line(pr) -> str:
    """`host: what -> fix`; the exact command tried stays in problems[].tried (never repeated here)."""
    fix, tried = str(pr.get("fix") or "-"), str(pr.get("tried") or "-")
    if tried != "-":
        fix = fix.replace(f", then retry: {tried}", "")
    return f"{pr['host']}: {pr['what']} -> {fix}"


def _fix_ask(blocking) -> dict:
    names = [n for n, _, _ in blocking]
    parts = [_fix_line(pr) for _, _, pr in blocking]
    one = len(names) == 1
    parts.append(f"Run the fix{'' if one else 'es'} shown, then choose retry; or remove the host{'' if one else 's'}.")
    return _ask("choice", "Fix", "\n".join(parts), ["retry", "remove " + ", ".join(names)],
                answer_key="remove_hosts", example={"remove_hosts": names})


def _cotenant_changes(cfg, state_hosts, trust) -> list:
    """Dedicated helpers (no other containers when approved) that now run other containers."""
    snap = (trust or {}).get("cotenants") or {}
    out = []
    for n, h in (cfg.get("hosts") or {}).items():
        if "helper" not in _roles(h) or snap.get(n) != []:
            continue
        rec = (state_hosts or {}).get(n) or {}
        now = _cotenant_names(rec) if rec.get("ok") else None
        if now:
            more = ", ..." if len(now) > 4 else ""
            out.append(f"{_label(n, h)}: now runs {len(now)} other container{'s' if len(now) != 1 else ''} "
                       f"({', '.join(now[:4])}{more}); it had none when approved "
                       f"[keep: {_role_words(_roles(h))}; {'everything' if _scope(h) == 'all' else SCOPE_WORDS[_scope(h)]}]")
    return out


def _manifest_changes(cfg, detected) -> list:
    """[(name, line, detected job | None)] for detector-made jobs whose manifest files changed or vanished."""
    if not isinstance(detected, dict):
        return []
    dj = detected.get("jobs") or {}
    out = []
    for n, j in (cfg.get("jobs") or {}).items():
        if not j.get("manifest"):
            continue
        d = dj.get(n)
        if d is None:
            out.append((n, f"{n}: no longer detected (the files it was made from moved or were removed)", None))
        elif d.get("manifest") and d["manifest"] != j["manifest"]:
            saved = str(j.get("image") or "")
            if d.get("image") and "@sha256:" in saved and saved.split("@", 1)[0] == d["image"]:
                d = {**d, "image": saved}  # same tag: taking the new detection keeps the approved digest pin
            files = ", ".join((j.get("files") or d.get("files") or [])[:3]) or "its manifest files"
            diff = f"; detected {d['detected']}, saved {j['detected']}" \
                if d.get("detected") and j.get("detected") and d["detected"] != j["detected"] else ""
            out.append((n, f"{n}: {files} changed since approval{diff}", d))
    return out


def _manifest_ask(changes) -> dict:
    upd = {n: d for n, _, d in changes if d is not None}
    rm = [n for n, _, d in changes if d is None]
    yes = {**({"jobs": upd} if upd else {}), **({"jobs_remove": rm} if rm else {})}
    keep = {"jobs": {n: {"manifest": d["manifest"] if d else None} for n, _, d in changes}}
    q = "\n".join(["Changed since approval:"] + [f"  {ln}" for _, ln, _ in changes]
                  + ["Take the new detection for these jobs? (yes / keep: keep the saved jobs and stop asking)"])
    a = _ask("choice", "Jobs", q, ["yes", "keep"], answer_key="jobs", example=yes)
    a["examples"] = {"yes": yes, "keep": keep}
    return a


def _helpers_ask(hosts, helpers, count, candidates) -> dict:
    """The helpers step. With helpers saved it asks only for the new ones and never offers a saved host. A choice
    needs 2-4 options (AskUserQuestion): one candidate is named in a text question instead; multi only when more
    than one answer is wanted."""
    known = [hosts[n].get("ssh") for n in helpers if hosts[n].get("ssh")]
    need = count - len(helpers)
    opts = [c for c in dict.fromkeys(x for x in candidates if isinstance(x, str) and x)
            if c.split(" (", 1)[0] not in known][:4]
    many = "; comma separated" if need > 1 else ""
    if not helpers:
        q = (f"For each of the {count} helpers, what do you type after ssh? (alias, or user@IP[:port]{many})"
             if need > 1 else "For the helper, what do you type after ssh? (alias, or user@IP[:port])")
    else:
        q = (f"Saved: {', '.join(_label(n, hosts[n]) for n in helpers)}. "
             + (f"For each of the {need} new helpers" if need > 1 else "For the new helper")
             + f", what do you type after ssh? (alias, or user@IP[:port]{many})")
    if len(opts) == 1:
        q += f"; known: {opts[0]}"
    ex = [x for x in dict.fromkeys(f"deploy@192.0.2.{11 + i % 200}" for i in range(min(need + len(known), 200)))
          if x not in known][:need]
    choice = len(opts) >= 2
    return _ask("choice" if choice else "text", "Helper ssh", q, opts if choice else [], choice and need > 1,
                "helpers", {"helpers": ex})


def _unpinned(cfg) -> list:
    """Jobs the engine leaves out while policy require_digest is on: their image is not pinned to a digest."""
    pol = cfg.get("policy") if isinstance(cfg.get("policy"), dict) else {}
    if not pol.get("require_digest", DEFAULT_POLICY["require_digest"]):
        return []
    return [n for n, j in (cfg.get("jobs") or {}).items()
            if isinstance(j, dict) and j.get("image") and "@sha256:" not in str(j["image"])]


def status(p, cfg, state_hosts: dict, trust, detected=None, candidates=(), master_prod=None) -> dict:
    """PURE init state machine (design section 4) -> {"state","step","summary","problems","ask","next"}.
    state: new|incomplete|unapproved|drift|ready; step: count|helpers|setup|jobs|approve|fix|none;
    next: ask|fix|approve|ready. candidates = ssh specs offered as options in the helpers step (max 4).
    detected = orch_detect.detect() output (the last proposal / current manifests) or None.
    master_prod = orch_config.master_prod(home) output: a helper that another approved project saves as production
    defaults to helper + production, read-only commands only (PROD_SCOPE), and its setup line names that project. A
    helper whose probe found another orch namespace's objects (problem class foreign-namespace) is a fix step."""
    cfg, sh = cfg or {}, state_hosts or {}
    hosts = cfg.get("hosts") or {}
    helpers = [n for n, h in hosts.items() if "helper" in _roles(h)]
    count = (cfg.get("pending") or {}).get("helper_count")
    probs = _problems(cfg, sh)
    blocking = [x for x in probs if x[1]]
    problems = [x[2] for x in probs]

    def out(state, step, nxt, ask=None, summary=None):
        return {"state": state, "step": step, "summary": summary if summary is not None else summary_lines(cfg, sh),
                "problems": problems, "ask": ask, "next": nxt}

    if not helpers and count is None:
        new = not hosts and not _decisions(cfg).keys() - {"schema"}
        return out("new" if new else "incomplete", "count", "ask",
                   _ask("choice", "Helpers", "How many helper servers will orch use?", ["1", "2", "3", "4+"],
                        answer_key="helper_count", example={"helper_count": 2}),
                   [f"no orch setup yet for {Path(p.root).as_posix()}"] if new else None)
    if isinstance(count, int) and len(helpers) < count:
        return out("incomplete", "helpers", "ask", _helpers_ask(hosts, helpers, count, candidates))
    setup_done = not any(_setup_open(cfg).values())
    if not setup_done or "jobs" not in cfg:
        if blocking:
            return out("incomplete", "fix", "fix", _fix_ask(blocking))
        if not setup_done:
            return out("incomplete", "setup", "ask", _setup_ask(p, cfg, sh, master_prod))
        return out("incomplete", "jobs", "ask", _jobs_ask(p, detected))
    approved = bool(trust) and trust.get("hash") == config_hash(cfg)
    if blocking:  # --approve needs every helper probed ok, so the fix comes first in every state
        return out("drift" if approved else "unapproved", "fix", "fix", _fix_ask(blocking))
    jobs_changed = _manifest_changes(cfg, detected)
    hosts_changed = _cotenant_changes(cfg, sh, trust) if trust else []
    if not approved:
        if jobs_changed:
            return out("unapproved", "jobs", "ask", _manifest_ask(jobs_changed))
        q = [_approve_line(cfg)]  # the full summary is in "summary"; the question stays short but self-contained
        prev = (trust or {}).get("config")
        loose = loosening(prev, cfg) if isinstance(prev, dict) else []
        if loose:
            q += ["Changes that loosen protection since the last approval:"] + [f"  {x}" for x in loose]
        if hosts_changed:
            q += ["Changed on the servers since the last approval:"] + [f"  {x}" for x in hosts_changed]
        if problems:
            q += ["Problems:"] + [f"  {_fix_line(x)}" for x in problems]
        q.append("Save and use this setup?")
        return out("unapproved", "approve", "approve", _ask("choice", "Approve", "\n".join(q), ["yes", "no"]))
    if jobs_changed:
        return out("drift", "jobs", "ask", _manifest_ask(jobs_changed))
    if hosts_changed:
        q = "\n".join(["Changed since approval:"] + [f"  {x}" for x in hosts_changed]
                      + ["Keep this setup? (yes / change: tell me what to change)"])
        return out("drift", "approve", "approve", _ask("choice", "Changed", q, ["yes", "change"]))
    unpinned = _unpinned(cfg)
    if unpinned:  # ci and run leave these jobs out: ask before the --approve that pins them
        problems += [{"host": f"job {n}", "what": f"image {cfg['jobs'][n]['image']} is not pinned to a digest, so the "
                      f"job cannot run", "tried": "-", "fix": "orch init --approve (pins images; needs registry "
                      "access from this machine or a helper)"} for n in unpinned]
        one = len(unpinned) == 1
        q = (f"Not pinned to a digest, so {'this job cannot' if one else 'these jobs cannot'} run: "
             + ", ".join(f"{n} ({cfg['jobs'][n]['image']})" for n in unpinned)
             + f". Pin {'it' if one else 'them'} now? orch init --approve looks up each digest (it needs registry "
               f"access from this machine or a helper).")
        return out("drift", "approve", "approve", _ask("choice", "Pin images", q, ["yes", "no"]))
    line = _ready_line(cfg, trust)
    if problems:
        line += " | problems: " + ", ".join(x["host"] for x in problems)
    return out("ready", "none", "ready", None, [line])


# ----------------------------------------------------------------------------- engine config (v1 shapes)
def _merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge(a.get(k), v) if k in a else copy.deepcopy(v)
        return out
    return copy.deepcopy(b)


def _relpath(name, what, v) -> str:
    """Project-relative directory -> normalised posix form ('' = root); refuses absolute paths and '..'."""
    s = str(v or "").replace("\\", "/").strip()
    parts = [x for x in s.split("/") if x not in ("", ".")]
    if s.startswith("/") or re.match(r"[A-Za-z]:", s) or ".." in parts:
        raise ConfigError(f"job {name}: {what} {v!r} must be a path inside the project, e.g. api or services/web")
    return "/".join(parts)


def _translate(name, j) -> dict:
    kind = j.get("kind") or "container"
    repo = _relpath(name, "repo", j.get("repo")) or "."
    d = _relpath(name, "dir", j.get("dir"))
    common = {"heavy": True, "repo": repo, "source": "git-archive", "timeout": j.get("timeout") or 1200,
              "memory": j.get("memory") or "2g", "fail_patterns": list(j.get("fail_patterns") or [])}
    if kind == "build":
        ctx = _relpath(name, "context", j.get("context")) or d
        t = {"kind": "build", **common, "requires": ["docker", "buildx"], "stage": "image",
             "dockerfile": j.get("dockerfile") or "Dockerfile", "context": ctx or ".", "targets": [""],
             "result_paths": []}
    else:
        # source lands in /src (the repo root); every command starts in its job dir with /out ready for results
        pre = f"mkdir -p /out && cd {shlex.quote(('/src/' + d) if d else '/src')} && "
        for k, what in (("image", "the image"), ("cmd", "the test command")):
            if not j.get(k):
                raise ConfigError(f"job {name}: {k} is missing; add {what} (orch init --apply)")
        t = {"kind": "container", **common, "requires": ["docker"], "stage": _stage(j), "image": j.get("image"),
             "cpus": j.get("cpus") or 2, "pids": j.get("pids") or 2048, "prep_timeout": j.get("prep_timeout") or 900,
             "network": "none", "env": dict(j.get("env") or {}), "cmd": pre + j["cmd"], "shell": "sh",
             "result_paths": list(j.get("result_paths") or ["/out"])}
        if j.get("prep"):
            t.update(prep=pre + j["prep"], prep_network="egress-registries")
        if j.get("cache"):
            t["cache"] = dict(j["cache"])
    if j.get("detected"):
        t["note"] = j["detected"]
    return t


def compile_engine(p, cfg, state_hosts, policy=None, master_prod=None) -> dict:
    """config + probe state + policy -> {"nodes","jobs","protected"} in v1 shapes (v1 engine and validators run on
    them; new keys: node ssh_args, job stage/context, nodes limits/require_digest). Policy = DEFAULT_POLICY, then
    cfg.policy, then the policy argument. Production-only hosts never become nodes. Jobs whose image is not pinned
    to a digest are left out while require_digest is on (named in jobs["_doc"]). Scopes: readonly runs only
    shell-investigate and the t-* jobs (never a project job); a helper that is also production, here or in another
    approved project (master_prod: the master_prod(home) output), runs readonly unless the user chose tests or a
    narrower scope (never all), and its node role is "helper+prod" (selftest never runs failure-path jobs there). A
    helper whose last probe failed stays a disabled node; another orch namespace's objects there (problem class
    foreign-namespace) put what they are and the fix in its note. policy io caps every Linux node."""
    cfg, sh = cfg or {}, state_hosts or {}
    pol = _merge(_merge(DEFAULT_POLICY, cfg.get("policy") or {}), policy or {})
    bad = _io_errors(pol.get("io"))
    if bad:
        raise ConfigError("; ".join(bad) + "; fix policy io in the setup and approve it again")
    store = Path(p.store).as_posix()
    jobs, images, repos, skipped = copy.deepcopy(BUILTIN_JOBS), {}, {".": Path(p.root).as_posix()}, []
    for name, j in (cfg.get("jobs") or {}).items():
        t = _translate(name, j)
        img = t.get("image")
        if img and pol.get("require_digest", True) and "@sha256:" not in img:
            skipped.append(name)
            continue
        if img:
            images[img] = img
        if t["repo"] != ".":
            repos[t["repo"]] = (Path(p.root) / t["repo"]).resolve().as_posix()
        jobs[name] = t
    images.update(pol.get("images") or {})
    nodes, prot_nodes, notes = {}, {}, []
    for n, h in (cfg.get("hosts") or {}).items():
        roles = _roles(h)
        if "helper" not in roles:
            continue
        rec = sh.get(n) or {}
        facts, res = rec.get("facts") or {}, rec.get("resolved") or {}
        if facts.get("os") not in ("linux", "windows"):
            notes.append(f"{n} left out: not probed yet (run orch init)")
            continue
        scope = _scope(h)
        other = _elsewhere(p, h, rec, master_prod) if "prod" not in roles else []
        if other and scope == "all":
            scope = PROD_SCOPE  # production in another approved project: production rules and defaults win
        transport, ctx, dh = rec.get("transport"), rec.get("context"), rec.get("docker_host")
        why = None
        if not rec.get("ok"):
            pr = rec.get("problem") if isinstance(rec.get("problem"), dict) else {}
            why = (f"{pr.get('what')}. Fix: {pr.get('fix')}" if pr.get("class") == "foreign-namespace"
                   else "probe failed: " + str(pr.get("what") or "run orch init"))
            transport = transport or "context"  # no transport chosen: the node stays (disabled) and still validates
        elif scope == "none":
            why = "job scope none"
        elif transport == "ssh-docker":
            dh = dh or "unix:///var/run/docker.sock"
            if not str(dh).startswith("unix://"):
                why, transport = f"docker_host {dh} is not a unix:// socket", "context"
        elif transport != "context" or not ctx:
            why, transport = "no docker transport (run orch init)", "context"
        # project jobs need scope tests or all; readonly means read-only commands only (shell-investigate)
        stages = SCOPES[scope] if SCOPE_RANK[scope] >= SCOPE_RANK["tests"] else set()
        tags = ["docker"] + (["buildx"] if facts.get("buildx") and transport == "context" else []) + \
            ["linux-host" if facts["os"] == "linux" else "windows-host"]
        opt = [k for k, t in jobs.items() if k not in BUILTIN_JOBS and t.get("stage") in stages]
        opt += (["shell-investigate"] if scope != "none" else []) + [k for k in BUILTIN_JOBS if k.startswith("t-")]
        nodes[n] = {"enabled": why is None, "os": facts["os"], "host": res.get("hostname") or parse_spec(h["ssh"])["host"],
                    "ssh": h["ssh"], "ssh_args": _ssh_args(h["ssh"]), "context": ctx, "transport": transport,
                    "docker_host": dh, "role": "helper+prod" if "prod" in roles or other else "helper",
                    "tags": tags, "opt_in": opt,
                    "io": copy.deepcopy(pol["io"]) if pol.get("io") and facts["os"] == "linux" else None,
                    "note": f"roles {' + '.join(roles)}; jobs {scope} ({SCOPE_WORDS[scope]})"
                            + (f"; production server in {', '.join(other)}" if other else "")
                            + (f"; disabled: {why}" if why else "")}
        eps = [{"name": e.get("name") or e.get("url"), "url": e["url"], "insecure": bool(e.get("insecure"))}
               for e in facts.get("endpoints") or [] if isinstance(e, dict) and e.get("url")]
        eps += [{"name": u, "url": u, "insecure": True} for u in h.get("watch") or [] if u not in {e["url"] for e in eps}]
        prot_nodes[n] = {"endpoints": eps,
                         "host_services": list(pol["windows_host_services"]) if facts["os"] == "windows" else []}
    doc = f"compiled by orch from {store}/config.json"
    out_nodes = {"_doc": doc + ("; " + "; ".join(notes) if notes else ""),
                 **{k: copy.deepcopy(pol[k]) for k in NODE_POLICY_KEYS}, "nodes": nodes}
    out_jobs = {"_doc": doc + (f"; left out until pinned (run orch init --approve): {', '.join(skipped)}"
                               if skipped else ""),
                "images": images, "repos": repos, "source_exclude": list(pol["source_exclude"]), "jobs": jobs}
    prot = {"never_touch_prefixes": list(pol["never_touch"]), "nodes": prot_nodes,
            "alert": {"command": (cfg.get("alerts") or {}).get("command") or None, "window_start_h": 0,
                      "window_end_h": 24}}
    return {"nodes": out_nodes, "jobs": out_jobs, "protected": prot}


# ----------------------------------------------------------------------------- guard view (design section 9)
_PKG = {"npm", "pnpm", "yarn"}
_PKG_VAL = {"--prefix", "-C", "--dir", "--cwd", "--workspace", "-w", "--filter", "-F"}
_YARN_OWN = {"add", "install", "remove", "upgrade", "up", "init", "info", "config", "cache", "workspace", "workspaces",
             "dlx", "exec", "why", "set", "link", "unlink", "pack", "publish", "npm", "plugin", "node", "bin",
             "global", "version", "constraints"}
_SUB = {"dotnet", "go", "cargo", "mvn", "mvnw", "gradle", "gradlew", "make", "deno", "bun"}
_ONE = {"pytest", "py.test", "tox", "nox", "jest", "vitest", "eslint", "tsc", "mypy", "ruff", "flake8", "pylint",
        "playwright"}
_RUNNERS = {"uv", "poetry", "pipenv", "hatch", "pdm", "rye"}
_WRAP = {"exec", "time", "env", "command", "nice"}


def _args(words, takes_value=()) -> list:
    """Non-option words (an option in takes_value also swallows the next word)."""
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w.startswith("-"):
            skip = w in takes_value
        else:
            out.append(w)
    return out


def _tool_keys(w) -> list:
    if not w:
        return []
    t = re.sub(r"\.(exe|cmd|bat)$", "", w[0].replace("\\", "/").rsplit("/", 1)[-1].lower())
    if t in _RUNNERS and "run" in w[1:]:
        return _tool_keys(w[w.index("run") + 1:])
    if t in ("python", "python3", "py") and "-m" in w[1:-1]:
        mod = w[w.index("-m") + 1]
        return ["pytest", "python -m pytest"] if mod == "pytest" else [f"python -m {mod}"]
    if t in _ONE:
        return ["pytest" if t == "py.test" else t]
    a = _args(w[1:], _PKG_VAL if t in _PKG else ())
    if t == "npx":
        return [f"npx {a[0]}"] if a else []
    if t in _PKG:
        if not a:
            return ["yarn install"] if t == "yarn" else []
        if a[0] in ("run", "run-script"):
            return ([f"{t} run {a[1]}"] + ([f"{t} test"] if a[1] == "test" else [])) if len(a) > 1 else []
        if a[0] in ("test", "t", "tst"):
            return [f"{t} test", f"{t} run test"]
        return [f"yarn {a[0]}", f"yarn run {a[0]}"] if t == "yarn" and a[0] not in _YARN_OWN else [f"{t} {a[0]}"]
    if t in _SUB:
        return [f"{t} {a[0]}"] if a else [t]
    if t == "docker" and (a[:1] == ["build"] or a[:2] == ["buildx", "build"]):
        return ["docker build", "docker buildx build"]
    return []


def _cmd_keys(cmd) -> list:
    keys = []
    for seg in re.split(r"&&|\|\||[;|\n]", cmd or ""):
        try:
            w = shlex.split(seg)
        except ValueError:
            w = seg.split()
        while w and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w[0]) or w[0] in _WRAP):
            w = w[1:]
        keys += [k for k in _tool_keys(w) if k not in keys]
    return keys


def job_index(jobs: dict) -> dict:
    """Heavy tool prefix -> job (guard rule d names `orch run <job>`), from each job's cmd text: "dotnet test" ->
    api-test, "npm test"/"npm run test" -> web-test, "pytest" -> py-test. Test commands win over prep commands;
    unit jobs over lint, build, e2e, image; then name order. Build jobs claim docker build."""
    order = {s: i for i, s in enumerate(("unit", "lint", "build", "e2e", "image", "investigate"))}
    ranked = sorted((jobs or {}).items(), key=lambda kv: (order.get(_stage(kv[1]), 9), kv[0]))
    idx = {}
    for field in ("cmd", "prep"):
        for name, j in ranked:
            keys = ["docker build", "docker buildx build"] if field == "cmd" and j.get("kind") == "build" \
                else _cmd_keys(j.get(field))
            for k in keys:
                idx.setdefault(k, name)
    return idx


def _ssh_config_hosts(path: Path, base: Path, depth=0) -> list:
    """Non-wildcard Host names in an ssh config, following Include (relative to ~/.ssh, globs allowed)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    names = []
    for raw in text.splitlines():
        m = re.match(r"\s*(\w+)\s*(?:=\s*|\s+)(.*?)\s*$", raw)
        if not m or raw.lstrip().startswith("#"):
            continue
        key, val = m.group(1).lower(), m.group(2)
        try:
            words = shlex.split(val)
        except ValueError:
            words = val.split()
        if key == "host":
            names += [w for w in words if w not in names and not w.startswith("-") and not re.search(r"[*?!]", w)]
        elif key == "include" and depth < 5:
            for inc in words:
                ip = Path(os.path.expanduser(inc))
                for f in sorted(glob.glob(str(ip if ip.is_absolute() else base / ip))):
                    names += [w for w in _ssh_config_hosts(Path(f), base, depth + 1) if w not in names]
    return names


def _ssh_g(run, alias, cfg_path):
    """`ssh -G alias` (local, no network) -> (hostname, port, user) or None."""
    argv = ["ssh"] + (["-F", str(cfg_path)] if cfg_path else []) + ["-G", alias]
    r = _run(run, argv, timeout=10)
    if r is None or r.returncode != 0:
        return None
    d = {}
    for ln in (r.stdout or "").splitlines():
        k, _, v = ln.strip().partition(" ")
        d.setdefault(k.lower(), v.strip())
    if not d.get("hostname"):
        return None
    try:
        port = int(d.get("port") or 22)
    except ValueError:
        port = 22
    return d["hostname"].lower(), port, d.get("user") or None


def _docker_contexts(run) -> list:
    """[(name, endpoint)] from `docker context ls --format {{json .}}`; [] when docker is missing or fails."""
    r = _run(run, ["docker", "context", "ls", "--format", "{{json .}}"], timeout=15)
    if r is None or r.returncode != 0:
        return []
    out = []
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        for c in d if isinstance(d, list) else [d]:
            if isinstance(c, dict) and c.get("Name"):
                out.append((c["Name"], c.get("DockerEndpoint") or ""))
    return out


def _is_ip(s) -> bool:
    try:
        ipaddress.ip_address(str(s).strip("[]"))
        return True
    except ValueError:
        return False


def _worktree_roots(store: Path, entry: dict, run) -> list:
    """Every work tree of a project (main first), so rule d covers sibling worktrees too."""
    if store.name == ".orch":
        return [store.parent.as_posix()]
    r = _run(run, ["git", "--git-dir", str(store.parent), "worktree", "list", "--porcelain"])
    roots = []
    if r is not None and r.returncode == 0:
        for block in re.split(r"\n\s*\n", (r.stdout or "").strip()):
            lines = block.splitlines()
            if lines and lines[0].startswith("worktree ") and not {"bare", "prunable"} & {x.split(" ")[0] for x in lines}:
                roots.append(Path(lines[0][9:].strip()).as_posix())
    return roots or [entry.get("root") or store.parent.parent.as_posix()]


def build_guard_view(home, run=subprocess.run, env=None, ssh_config=None) -> dict:
    """Union of approved projects for hooks/guard.py. A project counts when its config hash equals its trust hash;
    when it was edited since (waiting for approval) its last approved config keeps contributing, so a pending
    loosening never drops protection early. Hosts merge by name (roles union). aliases = non-wildcard Host names in
    ssh_config (default ~/.ssh/config) whose `ssh -G` hostname+port match; contexts = the host's orch context plus
    docker contexts whose endpoint points at the host."""
    env = os.environ if env is None else env
    h = Path(home)
    reg, trust = registry_load(h), _trust_all(h)
    stores = list(dict.fromkeys(list(reg.get("projects") or []) + list(trust)))
    projects, merged = [], {}
    for key in stores:
        tk = _find_key(trust, key)
        entry = trust.get(tk) if tk else None
        if not isinstance(entry, dict):
            continue
        store = Path(key)
        cfg = _approved_cfg(store, entry)
        if cfg is None:
            continue
        sh = _read_json(store / "state" / "hosts.json", {}, strict=False) or {}
        dep = cfg.get("deploy") or {}
        for root in _worktree_roots(store, entry, run):
            projects.append({"root": root, "store": store.as_posix(),
                             "deploy": [dep["command"]] if dep.get("command") else [],
                             "rollback": [dep["rollback"]] if dep.get("rollback") else [],
                             "jobs": job_index(cfg.get("jobs") or {})})
        for n, hc in (cfg.get("hosts") or {}).items():
            rec = sh.get(n) or {}
            res, ident = rec.get("resolved") or {}, (reg.get("nodes") or {}).get(n) or {}
            try:
                ps = parse_spec(hc.get("ssh"))
            except ValueError:
                continue
            known = res.get("hostname") or ident.get("hostname")
            m = merged.get(n)
            if m is None or (known and not m["_res"]):  # first sight, or the first one with a resolved address
                m = merged[n] = {**(m or {"name": n, "roles": [], "spec": hc["ssh"], "ips": [], "aliases": [],
                                            "contexts": []}),
                                 "hostname": str(known or ps["host"]).lower(),
                                 "port": int(res.get("port") or ident.get("port") or ps["port"] or 22),
                                 "user": res.get("user") or ps["user"] or (m or {}).get("user"), "_res": bool(known)}
            m["roles"] = [r for r in ROLES if r in m["roles"] or r in _roles(hc)]
            if res.get("alias") or (not ps["user"] and not ps["port"] and not _is_ip(ps["host"])):
                m["aliases"].append(ps["host"])  # the spec itself is an alias of this host
            if rec.get("context"):
                m["contexts"].append(rec["context"])
    if merged:
        cfg_path = Path(ssh_config) if ssh_config else Path.home() / ".ssh" / "config"
        names = _ssh_config_hosts(cfg_path, cfg_path.parent)
        names += [m["hostname"] for m in merged.values() if not m["_res"] and m["hostname"] in m["aliases"]
                  and m["hostname"] not in names]
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            resolved = dict(zip(names, ex.map(lambda a: _ssh_g(run, a, ssh_config), names)))
        by_addr = {}
        for m in merged.values():
            r = resolved.get(m["hostname"]) if not m["_res"] else None
            if r:  # an alias spec never probed: take HostName/Port/User from ssh -G
                m["hostname"], m["port"], m["user"] = r[0], r[1], m["user"] or r[2]
            if _is_ip(m["hostname"]):
                m["ips"].append(m["hostname"].strip("[]"))
            by_addr.setdefault((m["hostname"], m["port"]), m)
        for a, r in resolved.items():
            if r and (r[0], r[1]) in by_addr:
                by_addr[(r[0], r[1])]["aliases"].append(a)
        for cname, ep in _docker_contexts(run):
            try:
                u = urlsplit(ep)
                host, port = (u.hostname or "").lower(), u.port
            except ValueError:
                continue
            for m in merged.values():
                if u.scheme == "ssh":
                    r = resolved.get(host)
                    hit = (r is not None and port is None and (r[0], r[1]) == (m["hostname"], m["port"])) or \
                        (host in (m["hostname"], *m["ips"]) and (port or 22) == m["port"]) or \
                        (r is None and port is None and host in m["aliases"])
                elif u.scheme == "tcp":
                    hit = host in (m["hostname"], *m["ips"])
                else:
                    hit = False
                if hit:
                    m["contexts"].append(cname)
    for m in merged.values():
        m.pop("_res", None)
        for k in ("ips", "aliases", "contexts"):
            m[k] = list(dict.fromkeys(m[k]))
    return {"schema": SCHEMA, "generated": _now(),
            "namespace": {"label": env.get("ORCH_LABEL") or DEFAULT_LABEL, "prefix": env.get("ORCH_PREFIX") or DEFAULT_PREFIX},
            "hosts": [merged[n] for n in sorted(merged)], "projects": projects}


def write_guard_view(home, run=subprocess.run, **kw) -> Path:
    """Rewrites $ORCH_HOME/guard-view.json (also when nothing is approved, so stale views never linger)."""
    path = Path(home) / "guard-view.json"
    _write_json(path, build_guard_view(home, run=run, **kw))
    return path
