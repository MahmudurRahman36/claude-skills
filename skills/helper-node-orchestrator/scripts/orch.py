#!/usr/bin/env python3
"""orch - helper-node orchestrator. Python 3.10+, standard library only.

The master dispatches; heavy work runs only on helper nodes inside hardened, agent-owned containers.
Every object orch creates carries the label <ORCH_LABEL>=1 (default io.orch.agent) AND a name starting with
<ORCH_PREFIX> (default "agentx-");
orch never stops, removes or modifies anything that lacks either one. Objects with that name and another orch
namespace's labels (or none) refuse the node with their fix and are never touched.

Verbs: init, nodes, plan, run, ci, deploy --check, live, journal, status, logs, inventory, cordon, uncordon, hold,
reconcile, gc --session-end [--node NAME], stop-all, selftest.
Exit codes: 0 pass, 1 test-fail, 2 usage/refused/no setup, 3 oom, 4 timeout, 5 infra, 6 r1-guard, 7 deploy gate not met
(reserved for orch deploy --check), 75 nothing admitted.
Config: project mode (default) = the approved setup of the project found from the cwd (ORCH_PROJECT=DIR picks another),
made by orch init; ORCH_CONFIG = a directory of nodes.json, jobs.json, protected.json (legacy/test mode). See
config_source(). Project mode also records each finished run (state/runs.jsonl) and each invocation (journal).
ORCH_CONFIG skips a project's approval, so it is refused inside an approved project unless ORCH_TEST_MODE=1 (the test
suites and dev harnesses); its nodes never include a production host of the guard view (legacy_prod_nodes).
Test-only env, honoured only together with ORCH_CONFIG: ORCH_FAULT (fault injection), ORCH_CI_CHILD (command prefix
replacing "<python> -u orch.py run" for ci children), ORCH_REPO_<NAME> (a repo path), and with ORCH_TEST_MODE=1 too
ORCH_GC_NODE_JSON (gc --node of a node gone from the legacy nodes.json; never in project mode). ORCH_ALERT_DRYRUN=1
prints the alert command instead of running it.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import concurrent.futures as cf
import contextlib
import datetime as dt
import hashlib
import importlib
import inspect
import io
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import traceback
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("orch needs Python 3.10+")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass

SKILL = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("ORCH_HOME") or Path.home() / ".helper-orch")
OWNER = socket.gethostname().lower()
PREFIX_RX, LABEL_RX = r"[a-z][a-z0-9-]{1,14}-", r"[a-z0-9.-]{3,40}"
DEFAULT_LABEL, DEFAULT_PREFIX = "io.orch.agent", "agentx-"


def ns_check(prefix: str, label: str) -> None:
    """ORCH_PREFIX / ORCH_LABEL validation (cli-contract: bad value -> exit 2). A label in the default label's family
    (io.orch.*) other than the default itself is refused: it would see the default namespace's objects."""
    if not re.fullmatch(PREFIX_RX, prefix or ""):
        raise ValueError(f"ORCH_PREFIX {prefix!r} must match ^{PREFIX_RX}$")
    if not re.fullmatch(LABEL_RX, label or ""):
        raise ValueError(f"ORCH_LABEL {label!r} must match ^{LABEL_RX}$")
    if "." not in label.strip("."):
        raise ValueError(f"ORCH_LABEL {label!r} needs a family part before its last dot (e.g. {DEFAULT_LABEL})")
    fam = DEFAULT_LABEL.rsplit(".", 1)[0]
    if label != DEFAULT_LABEL and label.rsplit(".", 1)[0] == fam:
        raise ValueError(f"ORCH_LABEL {label!r} shares the default family {fam}. of {DEFAULT_LABEL}: "
                         f"use another family (e.g. io.orchtest.agent)")


PREFIX = os.environ.get("ORCH_PREFIX", DEFAULT_PREFIX)
L_AGENT = os.environ.get("ORCH_LABEL", DEFAULT_LABEL)
try:
    ns_check(PREFIX, L_AGENT)
except ValueError as _e:
    print(f"orch: {_e}", file=sys.stderr)
    sys.exit(2)
LF = f"label={L_AGENT}=1"  # docker --filter for agent-owned objects
LT = '{{.Label "' + L_AGENT + '"}}'  # docker --format field for the agent label
# Label family: all other ownership keys live next to the agent label (io.orch.agent -> io.orch.owner, ...), so two
# namespaces (default io.orch.*, tests io.orchtest.*) never see each other's objects, not even in the collision check.
LNS = L_AGENT.rsplit(".", 1)[0] + "."
L_OWNER, L_SESSION, L_JOB, L_LEASE, L_ROLE, L_HEAVY = (LNS + k for k in ("owner", "session", "job", "lease", "role", "heavy"))
CFG_FILES = ("nodes.json", "jobs.json", "protected.json")
EXIT = {"pass": 0, "test-fail": 1, "oom": 3, "timeout": 4, "infra": 5, "r1-guard": 6}
NOADMIT = 75
GATE = 7  # reserved: orch deploy --check, a deploy gate is not met (never used by run/ci)
NO_SETUP = "no orch setup here: run orch init"
# Host keys strict: a command-line -o wins over ~/.ssh/config, so a user's accept-new or no never applies to orch.
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
       "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
IS_WIN = os.name == "nt"
CONTEXT_PREFIX = "orch-"  # orch init's docker context names (orch_discover.CONTEXT_PREFIX): orch-<node>[-<8 hex>]


# ----------------------------------------------------------------------------- basics
def now() -> float:
    return time.time()


def ts(t: float | None = None) -> str:
    return dt.datetime.fromtimestamp(t if t is not None else now()).strftime("%Y-%m-%d %H:%M:%S")


def load_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json(p: Path, data) -> None:
    """Atomic write; unique tmp per call (threads share a pid); retries while Windows holds the target open."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    for i in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"could not replace {p}")


JOB_KEYS = {"kind", "heavy", "requires", "image", "repo", "source", "memory", "cpus", "pids", "timeout", "prep_timeout",
            "network", "prep_network", "cache", "env", "prep_cap_add", "cap_add", "prep", "cmd", "shell", "fail_patterns",
            "result_paths", "dockerfile", "targets", "results_stage", "build_args", "read_only", "test_only",
            "default_args", "note", "source_allow", "stage", "context", "allow_external"}
NODE_KEYS = {"enabled", "os", "host", "ssh", "ssh_args", "context", "transport", "docker_host", "role", "note", "tags",
             "opt_in", "io", "max_heavy"}
NODES_TOP = {"_doc", "admission", "score_weights", "watchdog", "lease", "subnet_pool", "subnet_prefix", "builder", "nodes",
             "limits", "require_digest"}
JOBS_TOP = {"_doc", "images", "repos", "source_exclude", "jobs"}
STAGES = ("lint", "unit", "build", "image", "e2e", "investigate")
DEFAULT_LIMITS = {"memory_max": "6g", "timeout_max": 3600}  # job caps; nodes.json "limits" overrides them
# ssh_args: flags allowed before the destination (last element). -o takes KEY=VALUE with a key of SSH_O_KEYS only: keys
# that change neither where ssh connects (HostName, Port, ProxyJump, HostKeyAlias, ControlPath, ...) nor what runs on
# this master (ProxyCommand, LocalCommand, ...), never pull in another file (Include) and never loosen host keys
# (they stay strict). The port goes in -p and a jump host in -J; legacy_prod_nodes resolves both with ssh -G.
SSH_FLAGS = {"-p", "-l", "-i", "-J", "-o", "-4", "-6"}
SSH_O_KEYS = ("AddressFamily", "CertificateFile", "Compression", "ConnectionAttempts", "ConnectTimeout",
              "IdentitiesOnly", "IdentityFile", "IPQoS", "LogLevel", "PreferredAuthentications", "PubkeyAuthentication",
              "ServerAliveCountMax", "ServerAliveInterval", "TCPKeepAlive", "User")
SSH_O_ALLOW = {k.lower() for k in SSH_O_KEYS}
SSH_O_HINT = {"port": "; give the port as -p PORT", "proxyjump": "; give a jump host as -J [user@]host[:port]",
              "hostname": "; put the real host in the destination (the last element)"}
_validated = {}


class ConfigError(Exception):
    pass


class NoSetup(ConfigError):
    """No usable setup: none found, not approved, no helper or a helper never probed (exit 2, next: run orch init).
    detail = an optional second stderr line (where orch looked)."""

    def __init__(self, msg: str, detail: str | None = None):
        super().__init__(msg)
        self.detail = detail


def limits_of(n: dict) -> dict:
    return dict(DEFAULT_LIMITS, **(n.get("limits") or {}))


def _relpath_ok(s) -> bool:
    """A path relative to the repo root: no absolute path, drive letter, backslash or '..' part."""
    return isinstance(s, str) and not re.match(r"([/\\]|[A-Za-z]:)", s) and "\\" not in s and ".." not in s.split("/")


def validate_jobs(j: dict, limits: dict | None = None, require_digest: bool = True) -> None:
    """jobs.json rules. limits = nodes.json "limits" (memory_max, timeout_max; defaults 6g / 3600 s); require_digest =
    nodes.json "require_digest" (default true: every image must be pinned by digest)."""
    L = dict(DEFAULT_LIMITS, **(limits or {}))
    mem_max, t_max = parse_mem(L["memory_max"]), L["timeout_max"]
    errs = [f"jobs.json: unknown top-level key {k!r}" for k in j if k not in JOBS_TOP]
    for name, c in j.get("jobs", {}).items():
        errs += [f"job {name}: unknown key {k!r}" for k in c if k not in JOB_KEYS]
        kind = c.get("kind")
        if kind not in ("container", "build", "external"):
            errs.append(f"job {name}: kind {kind!r} not container|build|external")
        if not isinstance(c.get("allow_external", False), bool):
            errs.append(f"job {name}: allow_external must be true|false")
        if kind == "external" and c.get("allow_external") is not True:
            errs.append(f'job {name}: kind external runs a command on this master; allow it with "allow_external": true')
        if c.get("stage") is not None and c["stage"] not in STAGES:
            errs.append(f"job {name}: stage {c['stage']!r} not one of {', '.join(STAGES)}")
        for k in ("cap_add", "prep_cap_add"):
            if c.get(k):
                errs.append(f"job {name}: {k} must be empty in Phase 1 (got {c[k]})")
        if kind != "external" and c.get("network", "none") != "none":
            errs.append(f"job {name}: run network must be none (got {c.get('network')!r})")
        if c.get("prep_network") not in (None, "egress-registries"):
            errs.append(f"job {name}: prep_network must be egress-registries")
        try:
            if "memory" in c and parse_mem(c["memory"]) > mem_max:
                errs.append(f"job {name}: memory {c['memory']} > limits.memory_max {L['memory_max']}")
        except ValueError as e:
            errs.append(f"job {name}: {e}")
        for k in ("timeout", "prep_timeout"):
            if k in c and not (_num(c[k]) and 0 < c[k] <= t_max):
                errs.append(f"job {name}: {k} {c[k]!r} must be 1..{t_max} s (limits.timeout_max)")
        for k in ("dockerfile", "context"):
            if c.get(k) is not None and not _relpath_ok(c[k]):
                errs.append(f"job {name}: {k} {c[k]!r} must be a path relative to the repo root (no '..', no absolute path)")
        if kind == "container" and c.get("image") not in j.get("images", {}):
            errs.append(f"job {name}: image {c.get('image')!r} not in images")
        if c.get("source") == "git-archive" and c.get("repo") not in j.get("repos", {}):
            errs.append(f"job {name}: repo {c.get('repo')!r} not in repos")
    for k, v in j.get("images", {}).items():
        if require_digest and "@sha256:" not in str(v):
            errs.append(f"image {k}: {v!r} must be pinned by digest (name@sha256:<digest>; "
                        f"docker buildx imagetools inspect {v} shows it)")
    if errs:
        raise ConfigError("; ".join(errs))


def _ssh_args_errs(name: str, sa) -> list[str]:
    """ssh_args = [flags..., destination], e.g. ["n1"] or ["-p", "2222", "deploy@192.0.2.12"]."""
    if not (isinstance(sa, list) and sa and all(isinstance(x, str) and x and not re.search(r"[\s\x00-\x1f]", x) for x in sa)):
        return [f"node {name}: ssh_args must be a non-empty list of strings without spaces"]
    errs, i = [], 0
    while i < len(sa) - 1:
        f = sa[i]
        if f not in SSH_FLAGS:
            errs.append(f"node {name}: ssh_args option {f!r} is not allowed (allowed: {' '.join(sorted(SSH_FLAGS))})")
            i += 1
            continue
        if f in ("-4", "-6"):
            i += 1
            continue
        if i + 1 >= len(sa) - 1:
            errs.append(f"node {name}: ssh_args option {f} needs a value before the destination")
            break
        v = sa[i + 1]
        k = v.split("=", 1)[0].lower()
        if f == "-o" and (not re.fullmatch(r"[A-Za-z]+=\S+", v) or k not in SSH_O_ALLOW):
            errs.append(f"node {name}: ssh_args -o {v!r} is not allowed: -o takes KEY=VALUE with one of "
                        f"{', '.join(SSH_O_KEYS)}, never a key that changes the destination, runs a command, reads "
                        f"another file or loosens host-key checks{SSH_O_HINT.get(k, '')}")
        i += 2
    if sa[-1].startswith("-"):
        errs.append(f"node {name}: ssh_args must end with the destination (alias or user@host), got {sa[-1]!r}")
    return errs


def validate_nodes(n: dict) -> None:
    errs = [f"nodes.json: unknown top-level key {k!r}" for k in n if k not in NODES_TOP]
    L = n.get("limits")
    if L is not None:
        if not isinstance(L, dict):
            errs.append("limits must be an object like {\"memory_max\": \"6g\", \"timeout_max\": 3600}")
        else:
            errs += [f"limits: unknown key {k!r} (known: memory_max, timeout_max)" for k in L if k not in DEFAULT_LIMITS]
            try:
                if parse_mem(L.get("memory_max", DEFAULT_LIMITS["memory_max"])) <= 0:
                    errs.append("limits.memory_max must be > 0")
            except ValueError as e:
                errs.append(f"limits.memory_max: {e}")
            t = L.get("timeout_max", DEFAULT_LIMITS["timeout_max"])
            if not (_num(t) and t > 0):
                errs.append(f"limits.timeout_max {t!r} must be a number of seconds > 0")
    if not isinstance(n.get("require_digest", True), bool):
        errs.append(f"require_digest {n.get('require_digest')!r} must be true|false")
    A, W = n.get("admission", {}), n.get("watchdog", {})
    if A.get("reserve_frac", 0) < 0.2:
        errs.append(f"admission.reserve_frac {A.get('reserve_frac')} < 0.2")
    if not 1 <= A.get("max_heavy", 0) <= 2:
        errs.append(f"admission.max_heavy {A.get('max_heavy')} not in 1..2")
    if A.get("psi_some_avg10_max", 99) > 25:
        errs.append(f"admission.psi_some_avg10_max {A.get('psi_some_avg10_max')} > 25")
    if W.get("psi_max", 99) > 25:
        errs.append(f"watchdog.psi_max {W.get('psi_max')} > 25")
    if not isinstance(A.get("enforce_new_checks", False), bool):
        errs.append(f"admission.enforce_new_checks {A.get('enforce_new_checks')!r} must be true|false")
    for name, c in n.get("nodes", {}).items():
        errs += [f"node {name}: unknown key {k!r}" for k in c if k not in NODE_KEYS]
        ssh = c.get("ssh")
        if not ssh and not c.get("ssh_args"):
            errs.append(f"node {name}: needs ssh (an ssh alias, user@host or local) or ssh_args")
        if ssh is not None and not (isinstance(ssh, str) and re.fullmatch(r"[^\s\x00-\x1f-][^\s\x00-\x1f]*", ssh)):
            errs.append(f"node {name}: ssh {ssh!r} must be an alias, user@host or local (no spaces, no leading '-')")
        if c.get("ssh_args") is not None:
            errs += _ssh_args_errs(name, c["ssh_args"])
            if ssh == "local":
                errs.append(f"node {name}: ssh local runs on this machine; remove ssh_args")
        if c.get("transport", "context") not in ("context", "ssh-docker"):
            errs.append(f"node {name}: transport must be context|ssh-docker")
        if c.get("transport") == "ssh-docker" and c.get("docker_host") is not None \
                and not str(c["docker_host"]).startswith("unix://"):
            errs.append(f"node {name}: ssh-docker docker_host must be null (the helper's default docker) or unix://...")
        if c.get("os") not in ("linux", "windows"):
            errs.append(f"node {name}: os must be linux|windows")
        if c.get("transport") == "ssh-docker" and c.get("ssh") == "local":
            errs.append(f"node {name}: transport ssh-docker cannot use ssh local")
        if "max_heavy" in c:
            mh = c["max_heavy"]
            if isinstance(mh, bool) or not isinstance(mh, int) or not 1 <= mh <= 2:
                errs.append(f"node {name}: max_heavy {mh!r} not in 1..2")
            elif mh > 1 and "prod" in str(c.get("role", "")):
                errs.append(f"node {name}: a helper + production node runs at most 1 heavy job (max_heavy {mh})")
    if errs:
        raise ConfigError("; ".join(errs))


def node_max_heavy(name: str, A: dict) -> int:
    """Heavy-job capacity of one helper: its own max_heavy (config hosts.<name>.max_heavy) or the policy value; a
    helper + production node runs at most 1."""
    n = cfg_nodes()["nodes"].get(name) or {}
    return 1 if "prod" in str(n.get("role", "")) else int(n.get("max_heavy") or A["max_heavy"])


def config_source():
    """THE seam between the engine and its configuration; nothing else reads config locations.
    Legacy/test mode: ORCH_CONFIG names a directory holding v1-style nodes.json, jobs.json and protected.json -> that
    Path. Project mode (ORCH_CONFIG unset): the compiled dict {"nodes", "jobs", "protected", "_label"} of the project's
    approved setup (project_source()); the validators, caches and every verb below run unchanged on either.
    Legacy mode is refused inside an approved project (legacy_gate)."""
    d = os.environ.get("ORCH_CONFIG")
    if d:
        legacy_gate()
        return Path(d)
    return project_source()


_LEGACY = {"key": None, "root": None, "refused": None}  # legacy_gate's answer per (ORCH_CONFIG, ORCH_PROJECT, cwd, HOME)
_WARNED = set()


def legacy_gate() -> None:
    """ORCH_CONFIG names its own nodes and jobs and so skips a project's approval, its production exclusion and its
    journal: orch refuses it (ConfigError, exit 2) while the cwd or ORCH_PROJECT lies in a project approved on this
    master. ORCH_TEST_MODE=1 (set by the test suites and dev harnesses) lifts only this check; legacy_prod_nodes
    holds either way."""
    if os.environ.get("ORCH_TEST_MODE") == "1":
        return
    try:
        cwd = os.getcwd()
    except OSError:  # the cwd was removed: only ORCH_PROJECT can place the call
        cwd = ""
    starts = [os.environ.get("ORCH_PROJECT") or "", cwd]
    key = (os.environ.get("ORCH_CONFIG"), *starts, str(HOME))
    with _PM_LOCK:
        if _LEGACY["key"] != key:
            _LEGACY.update(key=key, root=approved_root([s for s in starts if s]))
        root = _LEGACY["root"]
    if root:
        _LEGACY["refused"] = root  # journal_hook records the refusal in that project's journal
        raise ConfigError(f"ORCH_CONFIG is refused inside the approved orch project {root}, because it would skip that "
                          f"project's approved setup: unset ORCH_CONFIG and run the command again")


def _inside(path: str, root: str) -> bool:
    try:
        a, b = Path(path).resolve(), Path(root).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return a == b or b in a.parents


def _read_view() -> tuple[dict | None, str | None]:
    """$ORCH_HOME/guard-view.json (the union of the approved setups, written by orch init) -> (view or None when
    absent, error when it exists but cannot be used)."""
    p = HOME / "guard-view.json"
    try:
        view = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"{p.as_posix()} cannot be read ({e.strerror or e})"
    except ValueError as e:
        return None, f"{p.as_posix()} is not a valid guard view ({e})"
    if not isinstance(view, dict) or not isinstance(view.get("hosts") or [], list):
        return None, f"{p.as_posix()} is not a valid guard view (no object with a hosts list)"
    return view, None


def approved_root(starts: list[str]) -> str | None:
    """The root of a project approved on this master that holds one of starts: listed in the guard view, or found
    from the start (orch_config.find_project) with a trust record in ORCH_HOME. None when there is none."""
    gv = _read_view()[0]
    projects = gv.get("projects") if isinstance(gv, dict) else None
    for pr in projects if isinstance(projects, list) else []:
        r = pr.get("root") if isinstance(pr, dict) else None
        if isinstance(r, str) and r.strip() and any(_inside(s, r) for s in starts):
            return Path(r).as_posix()
    try:
        oc = sibling("orch_config")
    except ImportError:
        return None
    for s in starts:
        try:
            p = oc.find_project(s, env={})
            if p is not None and oc.trust_get(HOME, p):
                return Path(p.root).as_posix()
        except Exception:  # noqa: BLE001 - no project there (bad path, no git): not approved
            continue
    return None


def legacy_env(name: str) -> str:
    """A test-only env value (ORCH_FAULT, ORCH_CI_CHILD, ORCH_REPO_<NAME>): honoured only together with ORCH_CONFIG
    (which legacy_gate checks), ignored with one warning in project mode."""
    v = os.environ.get(name, "")
    if v and not os.environ.get("ORCH_CONFIG"):
        if name not in _WARNED:
            _WARNED.add(name)
            say(f"warn: {name} is test-only and is ignored without ORCH_CONFIG")
        return ""
    return v


def _host_port(v) -> tuple[str, int | None]:
    """(host, port|None) of an ssh target as typed: alias, user@host, host:port, [v6]:port, ssh:// or tcp:// URL;
    the same normalisation as the guard (lowercase, no brackets, no trailing dot); local sockets -> ("", None)."""
    v = str(v or "").strip().strip("\"'").lower()
    m = re.match(r"^([a-z][a-z0-9+.-]*)://([^/?#]*)", v)
    if m:
        if m.group(1) in ("unix", "npipe", "fd", "file"):
            return "", None
        v = m.group(2)
    v = v.rsplit("@", 1)[-1]
    m = re.match(r"^\[([^\]]*)\](?::(\d+))?", v)
    if m:
        return m.group(1), (int(m.group(2)) if m.group(2) else None)
    if v.count(":") == 1:
        h, _, p = v.partition(":")
        if p.isdigit():
            return h.strip("/").rstrip("."), int(p)
    return v.strip("/").rstrip("."), None


def _view_index(view: dict) -> dict:
    """{host key: [guard-view host records]}: each host's name, ssh spec, hostname, IPs, aliases and contexts."""
    index = {}
    for h in view.get("hosts") or []:
        if not isinstance(h, dict):
            continue
        keys = [h.get("name"), h.get("spec"), h.get("hostname")]
        keys += [x for k in ("ips", "aliases", "contexts") for x in (h.get(k) if isinstance(h.get(k), list) else [])]
        for k in keys:
            host = _host_port(k)[0] if isinstance(k, str) else ""
            if host and h not in index.setdefault(host, []):
                index[host].append(h)
    return index


def _prod_rec(index: dict, host: str, port: int | None) -> dict | None:
    """The guard-view production record behind host[:port], else None. Several hosts behind one key -> the port
    decides, else production wins (as in the guard)."""
    recs = index.get(host) or []
    if len(recs) > 1 and port:
        recs = [r for r in recs if r.get("port") == port] or recs
    if len(recs) > 1:
        recs = [r for r in recs if "prod" in (r.get("roles") or [])] or recs
    return recs[0] if recs and "prod" in (recs[0].get("roles") or []) else None


def ssh_dump(args: list[str]) -> R:
    """`ssh -G` with orch's own ssh options, then args: ssh prints the configuration it would use (command-line -o
    first, then ~/.ssh/config) and connects to nothing. The one place orch runs it (tests replace it)."""
    return cmd([SSH[0], "-G", *SSH[1:], *args], job="config", timeout=15,
               note="ssh -G (local, connects to nothing): where a node really connects")


def context_endpoint(ctx: str) -> str | None:
    """The docker endpoint (ssh://..., tcp://..., unix://...) of a local docker context, or None when docker or the
    context is missing (then no docker command of that node can run either). Local; tests replace it."""
    r = cmd(["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}", ctx], job="config",
            timeout=15, note="docker context endpoint (local)")
    try:
        v = json.loads(r.out.strip().splitlines()[-1]) if r.ok and r.out.strip() else None
    except ValueError:
        return None
    return v if isinstance(v, str) and v.strip() else None


def _ssh_settings(args: list[str]) -> tuple[dict | None, str | None]:
    """ssh_dump(args) -> ({setting: value}, None), or (None, why) when ssh cannot resolve them."""
    r = ssh_dump(list(args))
    kv = {}
    for line in r.out.splitlines():
        k, _, v = line.strip().partition(" ")
        if k and v.strip():
            kv.setdefault(k.lower(), v.strip())
    if not r.ok or not kv.get("hostname"):
        return None, f"ssh -G {' '.join(args)} failed ({r.last_err() or f'exit {r.rc}'})"
    return kv, None


def _dest_args(spec: str) -> list[str] | None:
    """[user@]host[:port] or ssh://[user@]host[:port] (a ProxyJump hop, an ssh:// endpoint) -> ssh args, or None."""
    s = spec.strip()
    m = re.match(r"^ssh://([^/?#]*)", s, re.I)
    s = m.group(1) if m else s
    user = s.rsplit("@", 1)[0] if "@" in s else ""
    host, port = _host_port(s)
    if not host or host.startswith("-") or user.startswith("-") or re.search(r"[\s\x00-\x1f]", s):
        return None
    return (["-p", str(port)] if port else []) + [f"{user}@{host}" if user else host]


def ssh_route(args: list[str], limit: int = 8) -> tuple[list, str | None]:
    """Where ssh really goes for these args: ([(host, port, what)], None) for the destination and every ProxyJump hop
    (the -J value or the ssh config's, each hop resolved again with its own config), or ([], why)."""
    kv, why = _ssh_settings(args)
    if kv is None:
        return [], why
    p = str(kv.get("port") or "22")
    host, port = _host_port(kv["hostname"])[0], int(p) if p.isdigit() else 22
    route, todo, seen = [(host, port, f"its ssh connects to {host}:{port}")], [], set()

    def jumps(v):
        return [] if not v or v.lower() == "none" else [j.strip() for j in v.split(",") if j.strip()]
    todo += jumps(kv.get("proxyjump"))
    while todo:
        hop = todo.pop(0)
        if hop in seen:
            continue
        if len(seen) >= limit:
            return [], f"more than {limit} ssh jump hosts"
        seen.add(hop)
        hargs = _dest_args(hop)
        if hargs is None:
            return [], f"ssh jump host {hop!r} cannot be read"
        hk, why = _ssh_settings(hargs)
        if hk is None:
            return [], f"ssh jump host {hop}: {why}"
        hh = _host_port(hk["hostname"])[0]
        hp = int(hk["port"]) if str(hk.get("port") or "").isdigit() else 22
        route.append((hh, hp, f"its ssh goes through jump host {hop} ({hh}:{hp})"))
        todo += jumps(hk.get("proxyjump"))
    return route, None


def node_route(c: dict) -> tuple[list, str | None]:
    """Where a legacy node really connects: its ssh (ssh_args, else the ssh alias) with every jump hop, and the
    endpoint of its docker context (transport context) -> ([(host, port, what)], None), or ([], why) when its ssh
    destination cannot be resolved. A missing context or docker CLI adds nothing: no docker command can run there."""
    route = []
    sa = [str(x) for x in (c.get("ssh_args") or ([c["ssh"]] if c.get("ssh") and c.get("ssh") != "local" else []))]
    if sa:
        hops, why = ssh_route(sa)
        if why:
            return [], why
        route += hops
    ctx = c.get("context")
    if c.get("transport", "context") == "context" and isinstance(ctx, str) and ctx:
        ep = context_endpoint(ctx) or ""
        scheme = ep.split("://", 1)[0].lower() if "://" in ep else ""
        if scheme == "ssh":
            dargs = _dest_args(ep)
            hops, why = ssh_route(dargs) if dargs else ([], f"its endpoint {ep} cannot be read")
            if why:
                return [], f"docker context {ctx}: {why}"
            route += [(h, p, f"its docker context {ctx} ({ep}): {w}") for h, p, w in hops]
        elif scheme == "tcp":
            h = _host_port(ep)[0]
            route.append((h, None, f"its docker context {ctx} connects to {ep}"))
    return route, None


def legacy_prod_nodes(nodes: dict, resolve: bool = False) -> tuple[dict, str | None]:
    """Legacy nodes that reach a production host of an approved setup on this master ($ORCH_HOME/guard-view.json, the
    union the guard enforces) -> ({node: why}, error); why reads after "node <name> ". A node matches by its ssh
    alias, host, ssh_args destination or docker context against a host's name, ssh spec, hostname, IPs, aliases and
    contexts (_prod_rec). resolve (runtime verbs, selftest): also by where it really connects, as orch will connect
    (node_route: ssh -G over orch's ssh options and its ssh_args, so ~/.ssh/config and -J hops count, and its docker
    context's endpoint); a node whose ssh cannot be resolved is listed as not checkable. error = the view exists but
    cannot be read."""
    view, err = _read_view()
    if view is None:
        return {}, err
    index = _view_index(view)

    def name_of(r):
        return f"production host {r.get('name')} ({r.get('spec') or r.get('hostname')}) of an approved setup"
    out, todo = {}, {}
    for name, c in (nodes or {}).items():
        if not isinstance(c, dict):
            continue
        sa = [str(x) for x in (c.get("ssh_args") or [])]
        port = int(sa[sa.index("-p") + 1]) if "-p" in sa[:-1] and sa[sa.index("-p") + 1].isdigit() else None
        targets = [c.get("ssh") if c.get("ssh") != "local" else None, c.get("host"), sa[-1] if sa else None,
                   c.get("context"), c.get("docker_host")]
        for t in targets:
            host, tport = _host_port(t) if isinstance(t, str) else ("", None)
            r = _prod_rec(index, host, tport or port)
            if r:
                out[name] = f"is the {name_of(r)}"
                break
        if name not in out and resolve:
            todo[name] = c
    def route_of(c):
        try:
            return node_route(c)
        except Exception as e:  # noqa: BLE001 - an odd node never crashes the check: it is just not checkable
            return [], f"orch could not resolve it ({e!r})"[:300]
    if todo:
        with cf.ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            routes = dict(zip(todo, ex.map(route_of, todo.values())))
        for name, (route, why) in routes.items():
            if why:
                out[name] = f"could not be checked against the production hosts: {why}"
                continue
            for host, port, what in route:
                r = _prod_rec(index, host, port)
                if r:
                    out[name] = f"reaches the {name_of(r)}: {what}"
                    break
    return out, None


def legacy_prod_check(doc: dict) -> None:
    """Legacy mode never runs jobs on a production host (legacy_prod_nodes, resolved in runtime verbs): run, ci, plan,
    nodes and selftest refuse the whole config (exit 2); cleanup and read-only verbs only warn, so gc can still remove
    orch's own objects."""
    strict = VERB["name"] in RUNTIME_VERBS or VERB["name"] is None
    bad, err = legacy_prod_nodes(doc.get("nodes") or {}, resolve=strict)
    if err:
        if strict:
            raise ConfigError(f"{err}, so orch cannot tell which hosts are production: legacy ORCH_CONFIG mode runs "
                              f"nothing until orch init rewrites it")
        say(f"warn: {err}")
    if bad:
        what = "; ".join(f"node {n} {why}" for n, why in sorted(bad.items()))
        if strict:
            fix = " (or fix its ssh settings so that ssh -G resolves them)" if any(
                w.startswith("could not be checked") for w in bad.values()) else ""
            raise ConfigError(f"{what}: orch never runs jobs there; remove it from the ORCH_CONFIG nodes.json{fix}")
        say(f"warn: {what}; only cleanup and read-only verbs reach it")


# ----------------------------------------------------------------------------- project mode
RUNTIME_VERBS = {"run", "ci", "plan", "nodes", "selftest"}  # these refuse anything but an approved, probed setup
VERB = {"name": None}  # the verb main() runs; None (a direct call) counts as a runtime verb
PROJECT = {"p": None, "key": None, "src": None, "left_out": {}}  # project mode: the project, its compiled config and
# the config jobs compile_engine left out ({job: {"why", "stage"}})
JOURNAL: dict = {}  # details of this invocation for the project journal: filled by the verbs, written by main()
_PM_LOCK = threading.RLock()


def sibling(name: str):
    """A module next to this file (orch_config, orch_init, orch_deploy, orch_journal), imported on first use; one
    already in sys.modules (a test fake) wins."""
    if name not in sys.modules:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.append(here)
    return importlib.import_module(name)


def reset_project() -> None:
    """Forget the cached project and compiled config (tests; one orch process compiles once)."""
    with _PM_LOCK:
        PROJECT.update(p=None, key=None, src=None, left_out={})
        _LEGACY.update(key=None, root=None, refused=None)
        _WARNED.clear()


def project_source() -> dict:
    """The approved setup of the project found from the cwd (ORCH_PROJECT overrides it), compiled once per process by
    orch_config.compile_engine. Runtime verbs (RUNTIME_VERBS) refuse with NoSetup (exit 2, next: run orch init) when
    there is no setup, it is not approved (its hash differs from the trusted one), it has no helper, or a helper was
    never probed. Every other verb (gc, reconcile, stop-all, status, ...) uses the last approved setup while a change
    waits for approval, so cleanup and the kill switch never depend on a pending answer."""
    key = (os.environ.get("ORCH_PROJECT") or "", os.getcwd(), str(HOME), VERB["name"])
    with _PM_LOCK:
        if PROJECT["key"] != key or PROJECT["src"] is None:
            PROJECT.update(src=None, key=None)
            src = _compile_project()
            PROJECT.update(src=src, key=key)
        return PROJECT["src"]


def _compile_project() -> dict:
    try:
        oc = sibling("orch_config")
    except ImportError as e:
        raise ConfigError(f"project mode needs scripts/orch_config.py next to orch.py ({e}): reinstall the skill") \
            from None
    try:
        p = oc.find_project()
    except oc.ConfigError as e:
        raise NoSetup(str(e)) from None
    if p is None:
        start = Path(os.environ.get("ORCH_PROJECT") or os.getcwd()).as_posix()
        raise NoSetup(NO_SETUP, f"looked for a project at {start}: run orch init there, or set ORCH_PROJECT=DIR")
    PROJECT["p"] = p
    root = Path(p.root).as_posix()
    try:
        cfg, trust = oc.load_config(p), oc.trust_get(HOME, p)
    except oc.ConfigError as e:
        raise NoSetup(str(e)) from None
    sh = oc.load_state(p, "hosts", {})
    sh = {k: v for k, v in sh.items() if isinstance(v, dict)} if isinstance(sh, dict) else {}
    strict = VERB["name"] in RUNTIME_VERBS or VERB["name"] is None
    if not (trust and trust.get("hash") == oc.config_hash(cfg)):
        why = (f"the orch setup of {root} changed since it was approved" if trust
               else f"the orch setup of {root} is not approved yet")
        prev = (trust or {}).get("config")
        if strict or not isinstance(prev, dict):
            raise NoSetup(f"{why}: run orch init to review and approve it")
        say(f"warn: {why}; orch {VERB['name']} uses the setup approved {trust.get('approved_at', '-')} until orch init "
            f"approves the change")
        cfg = prev
    hosts = cfg.get("hosts") or {}
    helpers = [n for n, h in hosts.items() if "helper" in ((h or {}).get("roles") or [])]
    if strict and not helpers:
        raise NoSetup(f"the orch setup of {root} has no helper server: run orch init to add one")
    unprobed = [f"{n} ({hosts[n].get('ssh', '?')})" for n in helpers if not sh.get(n, {}).get("probed")]
    if strict and unprobed:
        one = len(unprobed) == 1
        raise NoSetup(f"{'helper' if one else 'helpers'} {', '.join(unprobed)} {'has' if one else 'have'} not been "
                      f"probed yet: run orch init (it probes every helper)")
    try:
        src = oc.compile_engine(p, cfg, sh, **_master_prod_kw(oc))
    except (oc.ConfigError, ValueError) as e:
        raise NoSetup(f"the orch setup of {root} cannot be used: {e}") from None
    src["_label"] = f"project {root}"
    PROJECT["left_out"] = {n: {"why": f"its image {j.get('image')} is not pinned to a digest",  # stage: as compiled
                               "stage": j.get("stage") or ("image" if j.get("kind") == "build" else "unit")}
                           for n, j in (cfg.get("jobs") or {}).items() if n not in src["jobs"]["jobs"]}
    return src


def _master_prod_kw(oc) -> dict:
    """{"master_prod": orch_config.master_prod(HOME)} when orch_config has it and compile_engine takes it (hosts that
    any approved setup on this master makes production: compile_engine clamps them), else {}. Never fatal: a failure
    warns and compiles without it."""
    fn = getattr(oc, "master_prod", None)
    if not callable(fn):
        return {}
    try:
        ps = inspect.signature(oc.compile_engine).parameters
    except (TypeError, ValueError):
        return {}
    if "master_prod" not in ps and not any(x.kind == x.VAR_KEYWORD for x in ps.values()):
        return {}
    try:
        return {"master_prod": fn(HOME)}
    except Exception as e:  # noqa: BLE001
        say(f"warn: production hosts of the other approved setups on this master were not checked ({e!r})"[:300])
        return {}


_SRC_KEY = {"nodes.json": "nodes", "jobs.json": "jobs", "protected.json": "protected"}


def _load(name: str) -> tuple:
    """(data or None, cache key, where) for one config file from config_source()."""
    src = config_source()
    if isinstance(src, dict):  # compiled in memory: the key is the content, so an edit is always re-validated
        data = src.get(_SRC_KEY[name])
        blob = json.dumps(data, sort_keys=True, default=str).encode()
        return data, ("compiled", hashlib.sha256(blob).hexdigest()), f"{src.get('_label', 'compiled config')} ({name})"
    p = src / name
    data = load_json(p)
    return data, (str(p), p.stat().st_mtime_ns if data is not None else 0), str(p)


def cfg_path(name: str) -> Path:
    src = config_source()
    if isinstance(src, dict):
        raise ConfigError(f"{name} is compiled in memory ({src.get('_label', 'compiled config')}), not a file")
    return src / name


def config_sources() -> dict:
    src = config_source()
    if isinstance(src, dict):
        return {f: src.get("_label", "compiled config") for f in CFG_FILES}
    return {f: str(src / f) for f in CFG_FILES}


def config_line() -> str:
    s = config_sources()
    if len(set(s.values())) == 1:  # project mode: all three come from one setup
        return f"config: {next(iter(s.values()))}"
    return f"config: nodes={s['nodes.json']} jobs={s['jobs.json']} protected={s['protected.json']}"


def _cfg(name: str, validator, dep=""):
    """Load + validate once per (source, content/mtime, dep); dep = whatever else the validator reads."""
    data, key, where = _load(name)
    if not isinstance(data, dict):
        raise ConfigError(f"{where} is missing or not a JSON object: fix it, or point ORCH_CONFIG at a directory "
                          f"holding nodes.json, jobs.json and protected.json")
    key = key + (dep,)
    if _validated.get(name) != key:
        if validator:
            validator(data)
        _validated[name] = key
    return data


def cfg_nodes():
    if isinstance(config_source(), dict):
        return _cfg("nodes.json", validate_nodes)
    try:  # legacy mode: re-checked whenever the guard view changes
        st = (HOME / "guard-view.json").stat()
        view = (st.st_mtime_ns, st.st_size)
    except OSError:
        view = None
    strict = VERB["name"] in RUNTIME_VERBS or VERB["name"] is None
    return _cfg("nodes.json", lambda d: (validate_nodes(d), legacy_prod_check(d)), dep=f"view {view} strict {strict}")


def cfg_jobs():
    n = cfg_nodes()
    lim, dig = limits_of(n), n.get("require_digest", True) is not False
    return _cfg("jobs.json", lambda d: validate_jobs(d, lim, dig), dep=json.dumps([lim, dig], sort_keys=True))


def prot_warnings(prot: dict, nodes: dict) -> list[str]:
    """Non-fatal protected.json findings (printed once per file change, never raised)."""
    out = []
    for name, c in (prot or {}).get("nodes", {}).items():
        if c.get("endpoints") and nodes.get(name, {}).get("os") == "windows":
            out.append(f"protected.json node {name}: latency probes not supported on windows nodes (watchdog has no network)")
    return out


def cfg_prot():
    data, key, where = _load("protected.json")
    if not isinstance(data, dict):
        raise ConfigError(f"{where} is missing or not a JSON object: fix it, or point ORCH_CONFIG at a directory "
                          f"holding nodes.json, jobs.json and protected.json")
    if _validated.get("protected.json") != key:
        _validated["protected.json"] = key
        try:
            nodes = cfg_nodes().get("nodes", {})
        except ConfigError:
            nodes = {}
        for w in prot_warnings(data, nodes):
            say(f"warn: {w}")
    return data


def parse_mem(s) -> int:
    if isinstance(s, (int, float)):
        return int(s)
    m = re.fullmatch(r"\s*([0-9.]+)\s*([kmgt]?)b?\s*", str(s).lower())
    if not m:
        raise ValueError(f"bad memory size {s!r}")
    return int(float(m.group(1)) * {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}[m.group(2)])


def fmt_mem(b: int) -> str:
    return f"{b / (1 << 30):.1f}G" if b >= 1 << 30 else f"{b / (1 << 20):.0f}M"


class Logger:
    """Prints orch lines to stderr and mirrors them into the current job log."""
    lock = threading.Lock()
    fh = None
    verbose = True  # cmd_run turns this off unless -v / ORCH_VERBOSE=1: then only milestones + warnings reach stderr
    LOUD = re.compile(r"\s*(warn|refus|ALERT|WATCHDOG|no alert|DRYRUN|interrupted|emergency|skip |remove failed|"
                      r"finalise step|fingerprint worse|fingerprint after failed|infra|usage|timeout|oom|r1-guard)", re.I)

    @classmethod
    def say(cls, msg: str, m: bool = False) -> None:
        """m=True marks a milestone line (always on stderr); warnings/refusals always show too."""
        line = f"orch[{time.strftime('%H:%M:%S')}] {msg}"
        with cls.lock:
            if cls.verbose or m or cls.LOUD.match(msg):
                print(line, file=sys.stderr, flush=True)
            if cls.fh:
                cls.fh.write(line + "\n")
                cls.fh.flush()

    @classmethod
    def raw(cls, text: str) -> None:
        with cls.lock:
            if cls.fh:
                cls.fh.write(text)
                cls.fh.flush()


say = Logger.say


# ----------------------------------------------------------------------------- locking, ledger, audit, state
class FileLock:
    """Exclusive lock on ~/.helper-orch/lock (msvcrt on Windows, fcntl elsewhere). Re-entrant per thread."""
    _tl = threading.local()

    def __init__(self, timeout: float = 120):
        self.timeout = timeout

    def __enter__(self):
        depth = getattr(self._tl, "depth", 0)
        if depth:
            self._tl.depth = depth + 1
            return self
        HOME.mkdir(parents=True, exist_ok=True)
        fh = open(HOME / "lock", "a+b")
        end = now() + self.timeout
        while True:
            try:
                if IS_WIN:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if now() > end:
                    fh.close()
                    raise TimeoutError("could not take ~/.helper-orch/lock")
                time.sleep(0.1)
        self._tl.fh, self._tl.depth = fh, 1
        return self

    def __exit__(self, *a):
        self._tl.depth -= 1
        if self._tl.depth:
            return
        fh = self._tl.fh
        try:
            if IS_WIN:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def session_id() -> str:
    for k in ("ORCH_SESSION", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        if os.environ.get(k):
            return re.sub(r"[^A-Za-z0-9_.-]", "", os.environ[k])[:40]
    p = HOME / "session-id"
    try:
        s = p.read_text().strip()
        if s:
            return s
    except FileNotFoundError:
        pass
    s = "s" + time.strftime("%y%m%d%H%M") + secrets.token_hex(2)
    HOME.mkdir(parents=True, exist_ok=True)
    p.write_text(s)
    return s


def prompt_id() -> str:
    return os.environ.get("ORCH_PROMPT_ID") or os.environ.get("CLAUDE_PROMPT_ID") or ""


def _rotate(p: Path, max_bytes: int, keep_days: int) -> None:
    try:
        if p.exists() and p.stat().st_size > max_bytes:
            os.replace(p, p.with_name(f"{p.stem}-{time.strftime('%Y%m%d%H%M%S')}{p.suffix}"))
        for old in p.parent.glob(f"{p.stem}-*{p.suffix}"):
            if now() - old.stat().st_mtime > keep_days * 86400:
                old.unlink()
    except OSError:
        pass


def ledger(ev: str, **kw) -> None:
    rec = {"ts": ts(), "t": round(now(), 3), "ev": ev, "session": session_id(), "owner": OWNER, **kw}
    with FileLock():
        p = HOME / "ledger.jsonl"
        _rotate(p, 10 << 20, 30)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


_audit_lock = threading.Lock()


_ENV_FLAGS = ("-e", "--env", "--build-arg")
_SECRET_K = re.compile(r"(?i)token|pass|secret|key")
_KV = re.compile(r"([A-Za-z_][\w.\-]*)=(.*)", re.S)


def redact(argv) -> list[str]:
    """argv with secret values masked: the value after -e/--env/--build-arg (KEY=*** keeps the key), and any
    k=v whose k matches token|pass|secret|key. A multi-word arg (ssh remote command) is redacted word by word."""
    def kv(t, force):
        m = _KV.fullmatch(t.lstrip("'\""))
        if m and (force or _SECRET_K.search(m.group(1))):
            return t[:len(t) - len(t.lstrip("'\""))] + m.group(1) + "=***"
        return "***" if force else t

    def words(ws):
        out, nxt = [], False
        for w in ws:
            if nxt:
                out.append(kv(w, True))
                nxt = False
            elif w in _ENV_FLAGS:
                out.append(w)
                nxt = True
            elif "=" in w and w.split("=", 1)[0] in _ENV_FLAGS:
                f, v = w.split("=", 1)
                out.append(f + "=" + kv(v, True))
            elif " " in w:
                out.append(" ".join(words(w.split(" "))))
            else:
                out.append(kv(w, False))
        return out
    return words([str(a) for a in argv])


def audit(node, job, argv, note=None) -> None:
    rec = {"ts": ts(), "node": node, "job": job, "prompt": prompt_id(), "pid": os.getpid(),
           "cmd": [a if len(a) < 400 else a[:200] + f"...<{len(a)} chars>" for a in redact(argv)]}
    if note:
        rec["note"] = note
    with _audit_lock:
        HOME.mkdir(parents=True, exist_ok=True)
        p = HOME / "audit.jsonl"
        _rotate(p, 10 << 20, 90)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def state_get() -> dict:
    s = load_json(HOME / "state.json", {}) or {}
    for k in ("cordon", "hold", "base_images", "subnets"):
        s.setdefault(k, {})
    return s


def state_update(fn):
    with FileLock():
        s = state_get()
        r = fn(s)
        write_json(HOME / "state.json", s)
        return r


def retention() -> None:
    """results/ and node-logs/: 14 days. Ledger 10 MB/30 d and audit 90 d are rotated on write."""
    for sub in ("results", "node-logs"):
        d = HOME / sub
        if not d.exists():
            continue
        for e in d.iterdir():
            try:
                if now() - e.stat().st_mtime > 14 * 86400:
                    shutil.rmtree(e) if e.is_dir() else e.unlink()
            except OSError:
                pass
    tmp = HOME / "tmp"
    if tmp.exists():
        for e in tmp.iterdir():
            try:
                if now() - e.stat().st_mtime > 86400:
                    shutil.rmtree(e) if e.is_dir() else e.unlink()
            except OSError:
                pass


# ----------------------------------------------------------------------------- local processes
def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if IS_WIN:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def proc_start(pid: int) -> int:
    """Process creation time (Windows FILETIME / Linux clock ticks); guards against pid reuse."""
    try:
        if IS_WIN:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, int(pid))
            if not h:
                return 0
            c, e, kt, ut = (ctypes.c_ulonglong() for _ in range(4))
            ok = k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
            k.CloseHandle(h)
            return int(c.value) if ok else 0
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except Exception:
        return 0


def same_proc(d: dict) -> bool:
    pid = d.get("pid", 0)
    if not pid_alive(pid):
        return False
    st = d.get("pstart")
    return not st or proc_start(pid) == st


def live_dir() -> Path:
    d = HOME / "live"
    d.mkdir(parents=True, exist_ok=True)
    return d


def live_jobs() -> list[dict]:
    out = []
    for p in live_dir().glob("*.json"):
        d = load_json(p)
        if d and d.get("jobid"):
            d["_alive"] = same_proc(d)
            out.append(d)
    return out


def job_is_live(jobid: str) -> bool:
    """Live = the owning orch process (same pid AND same start time) still runs. A live process keeps its
    objects even if a heartbeat write failed; a dead one loses them once the label lease expires."""
    d = load_json(live_dir() / f"{jobid}.json")
    return bool(d and same_proc(d))


# ----------------------------------------------------------------------------- commands
class R:
    stopped = None  # watched_cmd: why orch killed the command (a watchdog trip, a lost watchdog), else None

    def __init__(self, rc, out="", err=""):
        self.rc, self.out, self.err = rc, out, err

    @property
    def ok(self):
        return self.rc == 0

    def last_err(self):
        lines = [l for l in (self.err or self.out or "").splitlines()
                 if l.strip() and "post-quantum" not in l and "store now, decrypt" not in l and "openssh.com/pq" not in l]
        return lines[-1].strip()[:300] if lines else f"rc={self.rc}"


def dec(b) -> str:
    if b is None:
        return ""
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else b


def cmd(argv, *, node=None, job=None, input=None, stdin=None, stdout=None, timeout=120, note=None, env=None) -> R:
    """Run one command (audited). MSYS_NO_PATHCONV=1 as in the proven node-run.sh recipe."""
    audit(node, job, argv, note)
    env = dict(os.environ, MSYS_NO_PATHCONV="1", **(env or {}))
    try:
        p = subprocess.run(argv, input=input, stdin=stdin, stdout=stdout if stdout is not None else subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return R(124, "", f"timed out after {timeout}s: {argv[0]}")
    except FileNotFoundError as e:
        return R(127, "", str(e))
    return R(p.returncode, dec(p.stdout), dec(p.stderr))


def kill_tree(proc) -> None:
    """Kill a watched command together with its children (docker runs buildx as a child plugin process): Windows
    taskkill /T, POSIX its own process group (watched_cmd starts it in a new session); then proc.kill()."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def watched_cmd(argv, *, stop, node=None, job=None, stdin=None, timeout=120, note=None, poll_s=0.5) -> R:
    """cmd() for a long job step (a buildx client, an external job) that must end the moment the job must: Popen, then
    every poll_s check the exit, stop() and the timeout. stop() returns why the job has to end now (a watchdog trip,
    a lost watchdog: fail closed) or None; it is checked before the start too. On stop the command and its children
    are killed (closing the buildx client cancels its BuildKit solve) and R.stopped says why; on the timeout they are
    killed and rc is 124, as with cmd(). stdout and stderr come back merged in R.out."""
    why = stop()
    if why:
        r = R(137, "", f"not started: {why}")
        r.stopped = why
        return r
    audit(node, job, argv, note)
    try:
        proc = subprocess.Popen(argv, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=dict(os.environ, MSYS_NO_PATHCONV="1"), start_new_session=not IS_WIN)
    except OSError as e:
        return R(127, "", str(e))
    buf = []

    def reader():  # in chunks, so what arrived is kept even if a survivor holds the pipe open
        with contextlib.suppress(Exception):
            for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                buf.append(chunk)
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    end, why, killed = now() + timeout, None, False
    try:
        while proc.poll() is None:
            why = stop()
            if why or now() > end:
                break
            time.sleep(poll_s)
    finally:  # stop, timeout, or an interrupt while it runs: never leave it running
        if proc.poll() is None:
            killed = True
            kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    th.join(timeout=5)
    if not th.is_alive():  # a reader still blocked (a survivor holds the pipe) keeps it; closing would wait on it
        with contextlib.suppress(Exception):
            proc.stdout.close()
    out = dec(b"".join(b for b in buf if b))
    if killed and why:
        r = R(137, out, f"killed by orch: {why}")
        r.stopped = why
        return r
    if killed:
        return R(124, out, f"timed out after {timeout}s: {argv[0]}")
    return R(proc.returncode, out, "")


# ----------------------------------------------------------------------------- node access
METRICS_SH = r'''
D=${1:-/}
m=$(awk '/^MemTotal:/{t=$2}/^MemAvailable:/{a=$2}END{print t" "a}' /proc/meminfo)
p=na; [ -r /proc/pressure/memory ] && p=$(awk '/^some/{for(i=2;i<=NF;i++) if($i ~ /^avg10=/){sub("avg10=","",$i);print $i}}' /proc/pressure/memory)
s1=$(awk '/^pswpin |^pswpout /{s+=$2}END{print s+0}' /proc/vmstat); c1=$(head -1 /proc/stat)
sleep 1
s2=$(awk '/^pswpin |^pswpout /{s+=$2}END{print s+0}' /proc/vmstat); c2=$(head -1 /proc/stat)
w=$(printf '%s\n%s\n' "$c1" "$c2" | awk '{t=0; for(i=2;i<=NF;i++) t+=$i; if(NR==1){t1=t;w1=$6}else{d=t-t1; print (d>0)?int(100*($6-w1)/d):0}}')
echo "M $m"; echo "P $p"; echo "S $((s2-s1))"; echo "W $w"
echo "L $(cut -d' ' -f1 /proc/loadavg) $(grep -c ^processor /proc/cpuinfo)"
df -Pk "$D" | awk 'NR==2{print "D "$2" "$4}'
df -Pi "$D" 2>/dev/null | awk 'NR==2{print "I "$2" "$4}'
'''

WATCH_SH = r'''
n=0; max=${1:-360}
while [ $n -lt $max ]; do
  n=$((n+1))
  p=na; [ -r /proc/pressure/memory ] && p=$(awk '/^some/{for(i=2;i<=NF;i++) if($i ~ /^avg10=/){sub("avg10=","",$i);print $i}}' /proc/pressure/memory)
  c1=$(head -1 /proc/stat); sleep 1; c2=$(head -1 /proc/stat)
  w=$(printf '%s\n%s\n' "$c1" "$c2" | awk '{t=0; for(i=2;i<=NF;i++) t+=$i; if(NR==1){t1=t;w1=$6}else{d=t-t1; print (d>0)?int(100*($6-w1)/d):0}}')
  lat=""
  __LAT__
  echo "WD $p $w$lat" || exit 0
  sleep __SLEEP__
done
'''


def curl_cmd(ep) -> str:
    k = "-k " if ep.get("insecure") else ""
    # prints exactly one number: time_total on an answer, 5 when curl failed or got no HTTP status (000)
    return (f"curl -s {k}-o /dev/null -m 5 -w '%{{http_code}} %{{time_total}}' {shlex.quote(ep['url'])} 2>/dev/null"
            " | awk 'NF==2&&$1!=\"000\"{print $2;ok=1}END{if(!ok)print 5}'")


def parse_curl_latency(s: str) -> float:
    """Seconds from one curl_cmd output (raises ValueError when unparsable); 'code time' with code 000 -> 5."""
    f = s.split()
    if len(f) == 2:
        return 5.0 if f[0] == "000" else float(f[1])
    return float(s.strip())


class Node:
    def __init__(self, name: str, c: dict):
        self.name, self.c = name, c
        self.os, self.ssh, self.ctx = c["os"], c.get("ssh"), c.get("context")
        # ssh destination at every ssh site: compiled ssh_args (["-p", "2222", "deploy@192.0.2.12"]) else the ssh alias
        self.ssh_args = [str(x) for x in (c.get("ssh_args") or [self.ssh])]
        self.transport = c.get("transport", "context")
        self.docker_host = c.get("docker_host")

    @property
    def linux(self):
        return self.os == "linux"

    def docker(self, args, **kw) -> R:
        return cmd(self.docker_argv(args), node=self.name, **kw)

    def docker_argv(self, args) -> list[str]:
        """context: docker --context <ctx> ...; ssh-docker: ssh <dest> docker [-H <docker_host>] ... (a set docker_host
        is always explicit, so a DOCKER_HOST exported by the helper's login shell, e.g. a dead rootless socket, cannot
        win; docker_host null = plain docker, the helper's default socket)."""
        args = [str(a) for a in args]
        if self.transport == "ssh-docker":
            dh = ["-H", shlex.quote(self.docker_host)] if self.docker_host else []
            return SSH + self.ssh_args + [" ".join(["docker", *dh] + [shlex.quote(a) for a in args])]
        return ["docker", "--context", self.ctx] + args

    @property
    def local(self):
        return self.ssh == "local"

    def sh_argv(self, args=()) -> list[str]:
        """Linux: argv that reads a sh script on stdin (ssh, or this machine when ssh is "local")."""
        if self.local:
            return ["sh", "-s", "--", *[str(a) for a in args]]
        return SSH + self.ssh_args + ["sh -s -- " + " ".join(shlex.quote(str(a)) for a in args)]

    def shell(self, script: str, args=(), timeout=60, job=None, note=None) -> R:
        if self.linux:
            argv = self.sh_argv(args)
            if self.docker_host:
                script = f"export DOCKER_HOST={shlex.quote(self.docker_host)}\n" + script
            return cmd(argv, node=self.name, job=job, input=script.encode(), timeout=timeout,
                       note=note or f"sh script {len(script)}b")
        enc = base64.b64encode(("$ProgressPreference='SilentlyContinue'\n" + script).encode("utf-16-le")).decode()
        if self.local:
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]
        else:
            argv = SSH + self.ssh_args + [f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"]
        return cmd(argv, node=self.name, job=job, timeout=timeout, note=note or "powershell script")

    def dbatch(self, cmds: list[list[str]], job=None, timeout=90) -> list[R]:
        """Several docker commands in one round trip (Linux: one ssh shell; Windows: parallel context calls)."""
        if not cmds:
            return []
        if self.linux:
            tag = secrets.token_hex(4)
            lines = ["E=$(mktemp)", "trap 'rm -f \"$E\"' EXIT"]
            for i, c in enumerate(cmds):
                lines.append(f"docker {' '.join(shlex.quote(str(a)) for a in c)} 2>\"$E\"; rc=$?; "
                             f"printf '\\n@@ERR{tag} {i}\\n'; cat \"$E\"; printf '\\n@@END{tag} {i} %d\\n' $rc")
            lines.append("rm -f \"$E\"")
            r = self.shell("\n".join(lines) + "\n", timeout=timeout, job=job,
                           note="docker batch: " + " | ".join(" ".join(map(str, c[:3])) for c in cmds)[:300])
            res = [R(255, "", r.last_err()) for _ in cmds]
            out, err, cur = [], [], None
            for line in r.out.splitlines():
                me = re.fullmatch(rf"@@ERR{tag} (\d+)", line.strip())
                mn = re.fullmatch(rf"@@END{tag} (\d+) (\d+)", line.strip())
                if me:
                    cur = err
                elif mn:
                    o = "\n".join(out[:-1] if out and out[-1] == "" else out)
                    e = "\n".join(err[:-1] if err and err[-1] == "" else err)
                    res[int(mn.group(1))] = R(int(mn.group(2)), o + ("\n" if o else ""), e)
                    out, err, cur = [], [], None
                else:
                    (cur if cur is not None else out).append(line)
            return res
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            return list(ex.map(lambda c: self.docker(c, job=job, timeout=timeout), cmds))


def nodes_all(include_disabled=False) -> dict[str, Node]:
    c = cfg_nodes()["nodes"]
    return {k: Node(k, v) for k, v in c.items() if include_disabled or v.get("enabled")}


def canon_ref(ref: str) -> str:
    """name:tag@sha256:x -> name@sha256:x (some Docker Engine versions cannot inspect the tag+digest form)."""
    if "@" in ref:
        name, dg = ref.split("@", 1)
        if ":" in name.rsplit("/", 1)[-1]:
            name = name.rsplit(":", 1)[0]
        return f"{name}@{dg}"
    return ref


def images_cfg() -> dict:
    return {k: canon_ref(v) for k, v in cfg_jobs()["images"].items()}


# ----------------------------------------------------------------------------- probe, admission, score
def _parse_metrics(text: str, d: dict) -> None:
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        try:
            if f[0] == "M":
                d["mem_total"], d["mem_avail"] = int(f[1]) * 1024, int(f[2]) * 1024
            elif f[0] == "P":
                d["psi"] = None if f[1] == "na" else float(f[1])
            elif f[0] == "S":
                d["swap_pages_s"] = int(f[1])
            elif f[0] == "W":
                d["iowait"] = int(f[1])
            elif f[0] == "L":
                d["load1"], d["cores"] = float(f[1]), int(f[2])
            elif f[0] == "D":
                d["disk_total"], d["disk_free"] = int(f[1]) * 1024, int(f[2]) * 1024
            elif f[0] == "I":
                d["inodes_total"], d["inodes_free"] = int(f[1]), int(f[2])
        except (IndexError, ValueError):
            pass


def parse_probe(text: str, os_: str = "linux") -> dict:
    """Probe output -> fields. linux: the whole probe script output; windows: VM metrics + Cim host lines."""
    d = {}
    if os_ == "linux":
        d.update(images={}, heavy_jobs=[], builder=False, caches=[])
        for line in text.splitlines():
            f = line.split(" ", 1)
            if f[0] == "DOCKER":
                v = f[1].split()
                d.update(docker=True, engine=v[0], ncpu=int(v[1]), vm_mem=int(v[2]), root=v[3])
            elif f[0] == "DEV":
                d["dev"] = f[1].strip() if len(f) > 1 else ""
            elif f[0] == "HEAVY":
                d["heavy_jobs"] = f[1].split() if len(f) > 1 else []
            elif f[0] == "BUILDER":
                d["builder"] = f[1].strip() != "0"
            elif f[0] == "CACHE":
                d["caches"] = f[1].split() if len(f) > 1 else []
            elif f[0] == "IMG":
                ref, v = f[1].rsplit(" ", 1)
                d["images"][ref] = v == "1"
        _parse_metrics(text, d)
        return d
    _parse_metrics(text, d)
    for line in text.splitlines():
        f = line.split()
        try:
            if f[0] == "WMEM":
                d["host_mem_total"], d["host_mem_free"] = int(f[1]) * 1024, int(f[2]) * 1024
            elif f[0] == "WDISK":
                d["host_c_total"], d["host_c_free"] = int(f[1]), int(f[2])
            elif f[0] == "WCPU":
                d["host_cpu_pct"] = float(f[1])
        except (IndexError, ValueError):
            pass
    return d


def probe(node: Node, refs=(), job=None) -> dict:
    """Live probe. Linux: one ssh round trip. Windows: Cim via powershell + docker info + VM /proc via a
    throwaway labelled alpine container (agent-owned, --rm)."""
    d = {"node": node.name, "os": node.os, "reach": False, "docker": False, "reason": "", "images": {},
         "heavy_jobs": [], "builder": False, "caches": [], "t": now()}
    t0 = now()
    if node.linux:
        img = "\n".join(
            f"docker image inspect --format x {shlex.quote(r)} >/dev/null 2>&1 && echo 'IMG {r} 1' || echo 'IMG {r} 0'"
            for r in refs)
        script = ("echo REACH\n"
                  "T=$(mktemp /tmp/.PFXdinfo.XXXXXX) || T=; trap '[ -n \"$T\" ] && rm -f \"$T\"' EXIT\n"
                  "if ! docker info --format 'DOCKER {{.ServerVersion}} {{.NCPU}} {{.MemTotal}} {{.DockerRootDir}}' 2>\"${T:-/dev/null}\"; then "
                  "echo \"DOCKERERR $(tail -1 \"${T:-/dev/null}\")\"; exit 0; fi\n"
                  "R=$(docker info --format '{{.DockerRootDir}}')\n"
                  # io.max needs a whole disk (or dm) device, not a partition: /dev/sda1 -> /dev/sda
                  "dv=$(findmnt -no SOURCE -T \"$R\" 2>/dev/null | head -1)\n"
                  "[ \"$(lsblk -dno TYPE \"$dv\" 2>/dev/null)\" = part ] && dv=/dev/$(lsblk -no PKNAME \"$dv\" | head -1)\n"
                  "echo \"DEV $dv\"\n"
                  "echo \"HEAVY $(docker ps --filter LF --filter label=@NS@heavy=1 --format '{{.Label \"@NS@job\"}}' | tr '\\n' ' ')\"\n"
                  "echo \"BUILDER $(docker ps -q --filter name=^PFXbuilder$ --filter LF | wc -l)\"\n"
                  "echo \"CACHE $(docker volume ls -q --filter LF --filter label=@NS@role=cache | tr '\\n' ' ')\"\n"
                  ).replace("@NS@", LNS).replace("PFX", PREFIX).replace("--filter LF", "--filter " + LF) + img + "\n" + METRICS_SH.replace("D=${1:-/}", "D=$R")
        r = node.shell(script, timeout=45, job=job, note="probe")
        if "REACH" not in r.out:
            d["reason"] = f"unreachable: {r.last_err()}"
            return d
        d["reach"] = True
        if "DOCKERERR" in r.out:
            d["reason"] = "docker down: " + r.out.split("DOCKERERR", 1)[1].strip().splitlines()[0][:200]
            return d
        try:
            d.update(parse_probe(r.out, "linux"))
        except (ValueError, IndexError) as e:
            d["docker"], d["reason"] = False, f"probe parse error: {sanitise(str(e), 200)}"
            return d
        if not d.get("docker"):
            d["reason"] = "probe parse error: no DOCKER line"
            return d
    else:
        r = node.shell("'REACH'", timeout=30, job=job, note="reach")
        if "REACH" not in r.out:
            d["reason"] = f"unreachable: {r.last_err()}"
            return d
        d["reach"] = True
        r = node.docker(["info", "--format", "{{.ServerVersion}} {{.NCPU}} {{.MemTotal}} {{.DockerRootDir}}"],
                        timeout=40, job=job)
        if not r.ok or not r.out.strip():
            d["reason"] = "docker down: " + r.last_err()[:200]
            return d
        v = r.out.split()
        try:
            d.update(engine=v[0], ncpu=int(v[1]), vm_mem=int(v[2]), root=v[3], docker=True)
        except (ValueError, IndexError):
            d["reason"] = f"probe parse error: bad docker info line {sanitise(r.out.strip(), 200)!r}"
            return d
        alpine = images_cfg()["alpine"]

        def vm():
            ensure_image(node, alpine, job=job, quiet=True)
            return node.docker(["run", "--rm", "--name", f"{PREFIX}probe-{secrets.token_hex(3)}",
                                *label_args(job or "probe", "probe", 120, heavy=False),
                                "--network", "none", "--memory", "32m", "--cpus", "0.5", "--pids-limit", "32",
                                "--security-opt", "no-new-privileges", "--cap-drop", "ALL", alpine,
                                "sh", "-c", METRICS_SH, "probe", "/"], timeout=60, job=job)

        def host():
            return node.shell(
                "$o=Get-CimInstance Win32_OperatingSystem; \"WMEM $($o.TotalVisibleMemorySize) $($o.FreePhysicalMemory)\"\n"
                "$c=Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='$env:SystemDrive'\"; \"WDISK $($c.Size) $($c.FreeSpace)\"\n"
                "\"WCPU $((Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average)\"\n",
                timeout=40, job=job, note="cim probe")

        def misc():
            cmds = [["ps", "--filter", LF, "--filter", f"label={L_HEAVY}=1",
                     "--format", '{{.Label "' + L_JOB + '"}}'],
                    ["ps", "-q", "--filter", f"name=^{PREFIX}builder$", "--filter", LF],
                    ["volume", "ls", "-q", "--filter", LF, "--filter", f"label={L_ROLE}=cache"]]
            cmds += [["image", "inspect", "--format", "x", rf] for rf in refs]
            return node.dbatch(cmds, job=job)

        with cf.ThreadPoolExecutor(3) as ex:
            fv, fh, fm = ex.submit(vm), ex.submit(host), ex.submit(misc)
            rv, rh, rm = fv.result(), fh.result(), fm.result()
        if not rv.ok:
            d["reason"] = f"vm probe failed: {rv.last_err()}"
            d["docker"] = False
            return d
        d.update(parse_probe(rv.out + "\n" + rh.out, "windows"))
        d["heavy_jobs"] = rm[0].out.split()
        d["builder"] = bool(rm[1].out.strip())
        d["caches"] = rm[2].out.split()
        for rf, rr in zip(refs, rm[3:]):
            d["images"][rf] = rr.ok
        d["dev"] = None
    d["probe_s"] = round(now() - t0, 1)
    return d


def hard_filter(job_name: str, job: dict, node: Node) -> list[str]:
    reasons = []
    if not node.c.get("enabled"):
        reasons.append("disabled: " + node.c.get("note", ""))
    if job_name not in node.c.get("opt_in", []):
        reasons.append(f"job class {job_name} not in opt-in list")
    missing = [t for t in job.get("requires", []) if t not in node.c.get("tags", [])]
    if missing:
        reasons.append(f"missing tags {missing}")
    cd = state_get()["cordon"].get(node.name)
    if cd and (cd.get("until") is None or cd["until"] > now()):
        until = "until uncordon" if cd.get("until") is None else f"until {ts(cd['until'])}"
        reasons.append(f"cordoned ({cd.get('reason', '')}) {until}")
    return reasons


REQUIRED_METRICS = ("mem_total", "mem_avail", "disk_total", "disk_free", "load1", "ncpu")
ALWAYS_METRICS = ("mem_total", "mem_avail", "disk_total", "disk_free")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def admit(job: dict, node: Node, p: dict, mem: int | None = None, *, A=None, W=None, images=None, WD=None) -> dict:
    """Admission checks + score. Returns {ok, checks[], score, breakdown}. A/W/WD/images default to the config.
    Checks new in the revamp (missing metric, Windows host reserve) only warn unless admission.enforce_new_checks."""
    A = cfg_nodes()["admission"] if A is None else A
    W = cfg_nodes()["score_weights"] if W is None else W
    WD = cfg_nodes()["watchdog"] if WD is None else WD
    mem = parse_mem(mem if mem is not None else job.get("memory", "1g"))
    checks = []

    def chk(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    if not p.get("reach"):
        chk("reachable", False, p.get("reason", "unreachable"))
        return {"ok": False, "checks": checks, "score": None}
    if not p.get("docker"):
        chk("docker", False, p.get("reason", "docker down"))
        return {"ok": False, "checks": checks, "score": None}

    # gates added by the revamp leave a check row only when they reject, so a healthy node's check list is unchanged
    warnings = []

    def chk_new(name, ok, detail):
        if ok:
            return
        if A.get("enforce_new_checks") is True:
            chk(name, False, detail)
        else:
            warnings.append(detail)
            say(f"warn: {node.name}: {detail}")

    # missing mem/disk inputs always reject (fail closed, truthful reason); missing load1/ncpu only warn unless enforced
    missing = [k for k in REQUIRED_METRICS if not _num(p.get(k))]
    for k in missing:
        (chk if k in ALWAYS_METRICS else chk_new)("metric", False, f"metric missing: {k}")
    tot, av = p.get("mem_total") or 0, p.get("mem_avail") or 0
    reserve = int(A["reserve_frac"] * tot)
    if not {"mem_total", "mem_avail"} & set(missing):
        chk("memory", av - mem >= reserve,
            f"MemAvailable {fmt_mem(av)} - job {fmt_mem(mem)} = {fmt_mem(av - mem)} vs reserve {fmt_mem(reserve)} ({A['reserve_frac']:.0%} of {'VM' if node.os == 'windows' else 'node'} {fmt_mem(tot)})")
    if node.os == "windows":
        ht, hf = p.get("host_mem_total"), p.get("host_mem_free")
        if not (_num(ht) and _num(hf)):
            chk_new("host_memory", False, "metric missing: host_mem_total/host_mem_free")
        else:
            hres = int(A["reserve_frac"] * ht)
            chk_new("host_memory", hf - mem >= hres,
                    f"host free {fmt_mem(hf)} - job {fmt_mem(mem)} = {fmt_mem(hf - mem)} vs reserve {fmt_mem(hres)} ({A['reserve_frac']:.0%} of host {fmt_mem(ht)})")
    iw = p.get("iowait")
    if _num(iw):
        if iw >= WD["iowait_max"]:  # always enforcing
            chk("iowait", False, f"iowait {iw}% >= watchdog.iowait_max {WD['iowait_max']}%")
    else:
        chk_new("metric", False, "metric missing: iowait")
    psi = p.get("psi")
    chk("psi", psi is None or psi < A["psi_some_avg10_max"],
        "PSI not available on this kernel (check skipped)" if psi is None else f"memory PSI some avg10 {psi} (need < {A['psi_some_avg10_max']})")
    sw = p.get("swap_pages_s", 0)
    chk("swap", sw < A["swap_pages_per_s_max"], f"si+so {sw} pages/s (need < {A['swap_pages_per_s_max']})")
    dt_, df_ = p.get("disk_total") or 0, p.get("disk_free") or 0
    need = max(A["disk_free_frac_min"] * dt_, A["disk_free_gb_min"] * (1 << 30))
    if not {"disk_total", "disk_free"} & set(missing):
        chk("disk", df_ >= need, f"free {fmt_mem(df_)} (need >= {fmt_mem(int(need))} = max({A['disk_free_frac_min']:.0%}, {A['disk_free_gb_min']}G))")
    it, ifree = p.get("inodes_total", 0), p.get("inodes_free", 0)
    chk("inodes", it == 0 or ifree >= A["inode_free_frac_min"] * it,
        f"free inodes {ifree}/{it}" if it else "inode count not reported (overlay); skipped")
    l1 = p["load1"] if _num(p.get("load1")) else 0.0
    cores = max(p["cores"] if _num(p.get("cores")) else 1, 1)
    chk("load", l1 < A["load_frac_max"] * cores, f"load1 {l1} (need < {A['load_frac_max'] * cores:.2f} = {A['load_frac_max']} x {cores} cpus)")
    ok = all(c["ok"] for c in checks)
    headroom = max(0.0, min(1.0, (av - mem) / tot)) if tot else 0
    loadf = max(0.0, 1 - l1 / cores)
    img = job.get("image")
    ref = (images_cfg() if images is None else images).get(img) if img else None
    aff_img = 1.0 if (ref is None or p["images"].get(ref)) else 0.0
    cache = (job.get("cache") or {}).get("name")
    aff_cache = 1.0 if (job.get("kind") == "build" and p.get("builder")) or (cache and f"{PREFIX}cache-{cache}" in p.get("caches", [])) else 0.0
    aff = 0.5 * aff_img + 0.5 * aff_cache
    score = W["mem"] * headroom + W["load"] * loadf + W["affinity"] * aff
    return {"ok": ok, "checks": checks, "score": round(score, 4), "warnings": warnings,
            "breakdown": {"mem_headroom_ratio": round(headroom, 3), "w_mem": W["mem"], "load_free": round(loadf, 3),
                          "w_load": W["load"], "affinity": aff, "image_present": aff_img, "warm_cache": aff_cache,
                          "w_aff": W["affinity"]}}


# ----------------------------------------------------------------------------- ownership
def label_args(jobid: str, role: str, lease_s: int, heavy: bool, extra: dict | None = None) -> list[str]:
    lab = {L_AGENT: "1", L_OWNER: OWNER, L_SESSION: session_id(), L_JOB: jobid,
           L_LEASE: str(int(now() + lease_s)), L_ROLE: role, L_HEAVY: "1" if heavy else "0"}
    lab.update(extra or {})
    out = []
    for k, v in lab.items():
        out += ["--label", f"{k}={v}"]
    return out


def protected_name(name: str) -> bool:
    n = name.lstrip("/")
    return any(n.startswith(p) for p in cfg_prot().get("never_touch_prefixes", []))


def ours(name: str, labels_agent: str) -> bool:
    n = name.lstrip("/")
    return labels_agent == "1" and n.startswith(PREFIX) and not protected_name(n)


_LCOLS = '\t' + LT + "".join('\t{{.Label "' + k + '"}}' for k in (L_JOB, L_LEASE, L_ROLE, L_OWNER)) + '\t'
LIST_FMT = {
    "container": ["ps", "-a", "--filter", LF, "--format", "{{.ID}}\t{{.Names}}" + _LCOLS + "{{.State}}"],
    "volume": ["volume", "ls", "--filter", LF, "--format", "{{.Name}}\t{{.Name}}" + _LCOLS + "-"],
    "network": ["network", "ls", "--filter", LF, "--format", "{{.ID}}\t{{.Name}}" + _LCOLS + "-"],
    "image": ["images", "--filter", LF, "--format", "{{.ID}}\t{{.Repository}}:{{.Tag}}"],
}
COLLIDE = [  # name, agent label, every label (another orch namespace's objects are told apart by their labels)
    ("container", ["ps", "-a", "--filter", f"name={PREFIX}", "--format", "{{.Names}}\t" + LT + "\t{{.Labels}}"]),
    ("volume", ["volume", "ls", "--filter", f"name={PREFIX}", "--format", "{{.Name}}\t" + LT + "\t{{.Labels}}"]),
    ("network", ["network", "ls", "--filter", f"name={PREFIX}", "--format", "{{.Name}}\t" + LT + "\t{{.Labels}}"]),
    ("image", ["images", "--filter", f"reference={PREFIX}*", "--format", "{{.Repository}}:{{.Tag}}\t-"]),
    ("container-owner", ["ps", "-a", "--filter", f"label={L_OWNER}", "--format", "{{.Names}}\t" + LT]),
]


def node_label(n: Node) -> str:
    """`name (ssh spec)` as every user-facing line prints a host; the name alone when the spec is the name."""
    return n.name if not n.ssh or n.ssh == n.name else f"{n.name} ({n.ssh})"


def _discover():
    """orch_discover (the foreign-namespace rules and words orch init uses), or None when it cannot be imported: a
    broken install never stops gc or stop-all; such objects then stay plain collisions."""
    try:
        return sibling("orch_discover")
    except Exception:  # noqa: BLE001
        return None


def foreign_text(objs: list[dict]) -> str:
    """Objects named <PREFIX>* that another orch namespace made (orch_discover.foreign_objects) -> what they are and
    the exact fix, as orch init reports them (orch_discover.foreign_problem). orch never removes them."""
    pr = sibling("orch_discover").foreign_problem(objs, PREFIX)
    return f"{pr['what']}. Fix: {pr['fix']}"


def list_agent(node: Node, job=None) -> dict:
    """Ask the NODE for every agent-labelled object, plus collision candidates. One round trip on Linux. "foreign" =
    objects named <PREFIX>* that another orch namespace made (an older orch or another master): never touched, and
    the first collision line says what they are and the fix."""
    kinds = list(LIST_FMT)
    rs = node.dbatch([LIST_FMT[k] for k in kinds] + [c for _, c in COLLIDE], job=job)
    out = {"ok": all(r.rc == 0 for r in rs), "err": next((r.last_err() for r in rs if r.rc), ""),
           "objects": [], "collisions": [], "foreign": []}
    for k, r in zip(kinds, rs[:len(kinds)]):
        for line in r.out.splitlines():
            f = line.split("\t")
            if k == "image":
                if len(f) < 2:
                    continue
                out["objects"].append({"kind": "image", "id": f[0], "name": f[1], "agent": "1"})
                continue
            if len(f) < 8:
                continue
            out["objects"].append({"kind": k, "id": f[0], "name": f[1].lstrip("/"), "agent": f[2], "job": f[3],
                                   "lease": int(f[4]) if f[4].isdigit() else 0, "role": f[5], "owner": f[6],
                                   "state": f[7]})
    imgs = [o for o in out["objects"] if o["kind"] == "image"]
    if imgs:
        r = node.dbatch([["image", "inspect", "--format",
                          "\t".join('{{index .Config.Labels "' + k + '"}}' for k in (L_JOB, L_LEASE, L_ROLE, L_OWNER)),
                          o["name"]] for o in imgs], job=job)
        for o, rr in zip(imgs, r):
            f = (rr.out.strip().splitlines() or [""])[0].split("\t") + ["", "", "", ""]
            o.update(job=f[0], lease=int(f[1]) if f[1].isdigit() else 0, role=f[2], owner=f[3], state="-")
    for (k, _), r in zip(COLLIDE, rs[len(kinds):]):
        for line in r.out.splitlines():
            f = line.split("\t", 2) + ["", ""]
            name, ag = f[0].strip(), f[1].strip()
            if not name:
                continue
            if k == "image":
                if name.startswith(PREFIX) and not any(o["kind"] == "image" and o["name"] == name for o in out["objects"]):
                    out["collisions"].append(f"image {name} uses the {PREFIX} prefix without {L_AGENT}=1")
            elif k == "container-owner":
                if ag != "1" or not name.startswith(PREFIX):
                    out["collisions"].append(f"container {name} carries {LNS}* labels but is not agent-owned")
            elif name.startswith(PREFIX) and ag != "1":
                od = _discover()
                fo = od.foreign_objects([(k, name, f[2])], PREFIX, L_AGENT) if od else []
                if fo:
                    out["foreign"] += [x for x in fo if x not in out["foreign"]]
                else:  # our label family without the agent label, or a name docker never prints
                    out["collisions"].append(f"{k} {name} uses the {PREFIX} prefix without {L_AGENT}=1")
    for o in out["objects"]:
        if not o["name"].startswith(PREFIX) or protected_name(o["name"]):
            out["collisions"].append(f"{o['kind']} {o['name']} has {L_AGENT}=1 but not the {PREFIX} name: never touched")
    if out["foreign"]:
        out["collisions"].insert(0, foreign_text(out["foreign"]))
    return out


def remove_objects(node: Node, objs: list[dict], why: str, job=None) -> list[str]:
    """Remove agent objects. Every object is re-checked: label <L_AGENT>=1 AND <PREFIX> name AND owner."""
    done = []
    order = {"container": 0, "network": 1, "volume": 2, "image": 3}
    batch = []
    objs = sorted([o for o in objs if o.get("name")], key=lambda o: order[o["kind"]])
    # Re-inspect every candidate right before removal (JSON labels, so no parsing tricks from list output).
    insp = {"container": ["inspect", "--type", "container", "--format", "{{.Name}}\t{{json .Config.Labels}}"],
            "volume": ["volume", "inspect", "--format", "{{.Name}}\t{{json .Labels}}"],
            "network": ["network", "inspect", "--format", "{{.Name}}\t{{json .Labels}}"],
            "image": ["image", "inspect", "--format", "{{json .RepoTags}}\t{{json .Config.Labels}}"]}
    rs0 = node.dbatch([insp[o["kind"]] + [o["name"]] for o in objs], job=job) if objs else []
    for o, ri in zip(objs, rs0):
        if ri.rc != 0:
            if re.search(r"no such|not found", ri.out + ri.err, re.I):
                done.append(f"{o['kind']} {o['name']} (already gone)")
            else:
                say(f"skip {o['kind']} {o['name']}: verify inspect failed: {ri.last_err()}")
            continue
        try:
            first, labj = (ri.out.strip().splitlines() or [""])[0].split("\t", 1)
            labs = json.loads(labj) or {}
            names = json.loads(first) if o["kind"] == "image" else [first.lstrip("/")]
        except (ValueError, json.JSONDecodeError):
            say(f"REFUSE {o['kind']} {o['name']}: could not verify labels")
            continue
        if o["name"] not in (names or []) or not ours(o["name"], labs.get(L_AGENT, "")):
            say(f"REFUSE {o['kind']} {o['name']}: not agent-owned (label {L_AGENT}=1 + {PREFIX} name required)")
            continue
        if labs.get(L_OWNER) != OWNER:
            say(f"skip {o['kind']} {o['name']}: owner {labs.get(L_OWNER)!r} is not this master")
            continue
        ledger("intent-remove", node=node.name, kind=o["kind"], name=o["name"], job=o.get("job"), why=why)
        c = {"container": ["rm", "-f", "-v", o["name"]], "network": ["network", "rm", o["name"]],
             "volume": ["volume", "rm", o["name"]], "image": ["rmi", o["name"]]}[o["kind"]]
        batch.append((o, c))
    if not batch:
        return done
    if node.linux:  # one sequential shell script, already in dependency order
        rs = node.dbatch([c for _, c in batch], job=job, timeout=180)
    else:  # parallel calls within a kind, kinds strictly in order (containers before networks/volumes/images)
        rs = []
        for k in sorted({o["kind"] for o, _ in batch}, key=order.get):
            rs += node.dbatch([c for o, c in batch if o["kind"] == k], job=job, timeout=180)
    for (o, _), r in zip(batch, rs):
        if r.ok or "No such" in r.out + r.err or "not found" in (r.out + r.err).lower():
            ledger("removed", node=node.name, kind=o["kind"], name=o["name"], job=o.get("job"), why=why)
            done.append(f"{o['kind']} {o['name']}")
        else:
            say(f"remove failed {o['kind']} {o['name']}: {r.last_err()}")
    return done


# ----------------------------------------------------------------------------- images
def ensure_image(node: Node, ref: str, job=None, quiet=False) -> R:
    """Pull if absent. Records existed_before once per node+ref (removed at session end only if we pulled it)."""
    r = node.docker(["image", "inspect", "--format", "{{.Id}}", ref], timeout=60, job=job)
    ek = engine_key(node)  # per engine: the same machine reached as another user may have its own engine (Q3)
    known = state_get()["base_images"].get(ek, {}).get(ref)
    if r.ok:
        if known is None:
            state_update(lambda s: s["base_images"].setdefault(ek, {}).setdefault(
                ref, {"existed_before": True, "first_seen": ts()}))
            ledger("base-image", node=node.name, ref=ref, existed_before=True)
        return r
    if not re.search(r"no such image|not found", r.out + r.err, re.I):
        return R(r.rc, r.out, f"image inspect failed (not a 'No such image' answer): {r.last_err()}")
    new = known is None
    if new:
        state_update(lambda s: s["base_images"].setdefault(ek, {}).__setitem__(
            ref, {"existed_before": False, "first_seen": ts()}))
    ledger("intent-pull", node=node.name, ref=ref, existed_before=False if new else known.get("existed_before"))
    if not quiet:
        say(f"{node.name}: pulling {ref} (did not exist before; removed at session end)")
    p = node.docker(["pull", "-q", ref], timeout=900, job=job)
    if p.ok:
        ledger("pulled", node=node.name, ref=ref, existed_before=False if new else known.get("existed_before"))
    elif new:
        state_update(lambda s: s["base_images"].get(ek, {}).pop(ref, None))
    return p


# ----------------------------------------------------------------------------- fingerprint
def fingerprint(node: Node, job=None, lat_samples=3) -> dict:
    prot = cfg_prot()["nodes"].get(node.name, {})
    eps = prot.get("endpoints", [])
    fp = {"t": now(), "containers": {}, "services": {}, "latency": {}, "ok": True}
    fmt = "{{.Id}}\t{{.Name}}\t{{.RestartCount}}\t{{json .State}}\t{{json .Config.Labels}}"
    if node.linux:
        lat = "\n".join(f"echo \"LAT {i} $( {curl_cmd(e)} ) $( {curl_cmd(e)} ) $( {curl_cmd(e)} )\""
                        for i, e in enumerate(eps)) if lat_samples else ""
        # only NON-agent containers are fingerprinted (agent probe/watchdog containers come and go)
        r = node.shell("ids=$(docker ps -a --no-trunc --format '{{.ID}} {{.Label \"" + L_AGENT + "\"}}' | awk '$2!=\"1\"{print $1}')\n"
                       "echo \"FPN $(echo \"$ids\" | grep -c .)\"\n"
                       f"echo \"$ids\" | xargs -r docker inspect --format {shlex.quote(fmt)} | sed 's/^/FP /'\n"
                       f"echo FPEND\n{lat}\n", timeout=90, job=job, note="fingerprint")
        text = r.out
        m = re.search(r"^FPN (\d+)", text, re.M)
        fp["ok"] = "FPEND" in text and bool(m)
        fp["expected"] = int(m.group(1)) if m else -1
    else:
        r1 = node.docker(["ps", "-a", "--no-trunc", "--format", "{{.ID}} " + LT], timeout=60, job=job)
        ids = [l.split()[0] for l in r1.out.splitlines() if l.strip() and (len(l.split()) < 2 or l.split()[1] != "1")]
        text = ""
        if ids:
            r2 = node.docker(["inspect", "--format", fmt] + ids, timeout=90, job=job)
            text = "\n".join("FP " + l for l in r2.out.splitlines())
        fp["ok"] = r1.ok
        fp["expected"] = len(ids)
        svcs = prot.get("host_services", [])
        if svcs:
            names = ",".join(f"'{s}'" for s in svcs)
            rs = node.shell(f"foreach($n in @({names})){{ $s=Get-Service -Name $n -EA 0; if($s){{\"SVC $n $($s.Status)\"}}else{{\"SVC $n Missing\"}} }}\n"
                            "\"PROC DockerDesktop $(@(Get-Process 'Docker Desktop' -EA 0).Count)\"", timeout=40, job=job,
                            note="service fingerprint")
            for line in rs.out.splitlines():
                f = line.split()
                if len(f) >= 3 and f[0] in ("SVC", "PROC"):
                    fp["services"][f[1]] = f[2]
    for line in text.splitlines():
        if line.startswith("FP "):
            f = line[3:].split("	")
            if len(f) < 5:
                continue
            try:
                stj, lab = json.loads(f[3]), json.loads(f[4]) or {}
            except json.JSONDecodeError:
                continue
            fp.setdefault("seen", 0)
            fp["seen"] += 1
            if lab.get(L_AGENT) != "1":
                fp["containers"][f[0]] = {"name": f[1].lstrip("/"), "status": stj.get("Status", ""),
                                          "health": (stj.get("Health") or {}).get("Status", ""),
                                          "restarts": int(f[2] or 0), "started": stj.get("StartedAt", "")}
        elif line.startswith("LAT "):
            f = line.split()
            try:
                vals = [parse_curl_latency(x) for x in f[2:]]
                fp["latency"][eps[int(f[1])]["name"]] = round(statistics.median(vals), 4)
            except (ValueError, IndexError):
                pass
    # median == the curl failure value: the endpoint was down at baseline, so its latency guard cannot trip
    down = [k for k, v in fp["latency"].items() if v >= 5.0]
    if down:
        fp["unreachable_at_baseline"] = down
        say(f"warn: {node.name}: endpoint(s) unreachable at baseline (latency guard ineffective): {', '.join(down)}")
    seen = fp.get("seen", 0)
    errs = []
    if not fp["ok"]:
        errs.append("container list incomplete (no FPN/FPEND marker or docker ps failed)")
    if fp.get("expected", 0) != seen:
        errs.append(f"inspected {seen} of {fp.get('expected')} containers")
    if node.linux and lat_samples:
        miss = [e["name"] for e in eps if e["name"] not in fp["latency"]]
        if miss:
            errs.append(f"latency not parsed for {', '.join(miss)}")
    if not node.linux:
        want = prot.get("host_services", [])
        miss = [s_ for s_ in want + (["DockerDesktop"] if want else []) if s_ not in fp["services"]]
        if miss:
            errs.append(f"service state not parsed for {', '.join(miss)}")
    if errs:
        fp["ok"] = False
        fp["error"] = "; ".join(errs)
    return fp


HEALTH_RANK = {"": 0, "healthy": 0, "starting": 1, "unhealthy": 2}


def docker_time(s: str) -> float:
    """Docker RFC3339Nano (2026-10-29T09:25:31.123456789Z) -> epoch seconds; 0 if unknown."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", s or "")
    if not m or m.group(1) == "0001":
        return 0.0
    return dt.datetime(*map(int, m.groups()), tzinfo=dt.timezone.utc).timestamp()


STABLE_S = 600


def fp_diff(a: dict, b: dict, t_now: float | None = None) -> tuple[list[str], list[str]]:
    """'worse' only for containers that were running AND stable (started > 10 min before the job's
    fingerprint): gone, stopped, restarted, restart count up, or health worse. Everything else is info."""
    worse, info = [], []
    t_job = a.get("t", now() if t_now is None else t_now)
    for cid, c in a["containers"].items():
        d = b["containers"].get(cid)
        stable = c["status"] == "running" and 0 < docker_time(c["started"]) < t_job - STABLE_S
        bad = []
        if d is None:
            bad.append(f"{c['name']}: container gone")
        else:
            if d["restarts"] > c["restarts"]:
                bad.append(f"{c['name']}: restart count {c['restarts']} -> {d['restarts']}")
            if c["status"] == "running" and d["status"] != "running":
                bad.append(f"{c['name']}: {c['status']} -> {d['status']}")
            elif c["status"] == "running" and d["started"] != c["started"]:
                bad.append(f"{c['name']}: restarted (StartedAt {c['started']} -> {d['started']})")
            if HEALTH_RANK.get(d["health"], 2) > HEALTH_RANK.get(c["health"], 0):
                bad.append(f"{c['name']}: health {c['health'] or 'none'} -> {d['health']}")
            if c["status"] != d["status"] and c["status"] != "running":
                info.append(f"{c['name']}: {c['status']} -> {d['status']}")
        if stable:
            worse += bad
        else:
            info += [x + " (not stable before the job: info only)" for x in bad]
    for cid, d in b["containers"].items():
        if cid not in a["containers"]:
            info.append(f"{d['name']}: new non-agent container ({d['status']})")
    for s_, st in a["services"].items():
        nst = b["services"].get(s_)
        if s_ == "DockerDesktop":
            if st not in ("0", None) and nst == "0":
                worse.append("Docker Desktop process gone")
        elif st == "Running" and nst != "Running":
            worse.append(f"service {s_}: Running -> {nst}")
    return worse, info


def hold_active(node: str) -> bool:
    return state_get()["hold"].get(node, 0) > now()


def cordon(node: str, reason: str, minutes: float | None, by: str) -> None:
    until = None if minutes is None else now() + minutes * 60
    state_update(lambda s: s["cordon"].__setitem__(node, {"reason": reason, "until": until, "by": by, "at": ts()}))
    ledger("cordon", node=node, reason=reason, until=until, by=by)
    JOURNAL.setdefault("cordons", []).append({"node": node, "reason": reason[:200], "by": by, "minutes": minutes})


def sanitise(text: str, limit: int = 300) -> str:
    return re.sub(r"[^A-Za-z0-9 _.:;,/-]", " ", text)[:limit].strip()


def r1_guard_alert(node: str, job, reason: str, acfg: dict | None = None) -> None:
    """Watchdog trip: alert line always; the alert command only for real jobs (not t-* test jobs, not selftest)."""
    skip = None
    if job.get("test_only") or str(getattr(job, "name", "")).startswith("t-"):
        skip = f"test job {job.name}"
    elif os.environ.get("ORCH_SELFTEST") == "1":
        skip = "selftest run"
    alert(node, [f"watchdog r1-guard ({job.name}): {reason}"], call=skip is None, skip_reason=skip, acfg=acfg)


def _in_window(h: int, start, end) -> bool:
    return start <= h < end if start <= end else (h >= start or h < end)  # start > end: the window spans midnight


def alert(node: str, lines: list[str], call: bool = False, skip_reason: str | None = None, acfg: dict | None = None) -> None:
    """Alert line always (alerts.log + stderr; text sanitised). The alert command runs only when call=True (a watchdog
    r1-guard trip). protected.json "alert": {"command": "<shell command>" | null, "window_start_h", "window_end_h"}: the
    command runs detached as `bash -c <command>` with the message on stdin and in $MSG, only inside the window (default
    0-24 = always). Legacy form {"script", "numbers", window}: `bash <script> <numbers...>` with $MSG (that script waits
    for its own window). ORCH_ALERT_DRYRUN=1 prints instead of running. No bash on PATH -> skipped, reason logged.
    acfg replaces the configured alert block (selftest)."""
    a = ((cfg_prot().get("alert") or {}) if acfg is None else acfg)
    msg = sanitise(f"Helper {node}: protected workload degraded during an agent job. {'; '.join(lines)}. Node cordoned.", 240)
    rec = {"ts": ts(), "node": node, "lines": [sanitise(l) for l in lines], "msg": msg}
    HOME.mkdir(parents=True, exist_ok=True)
    say(f"ALERT {node}: {msg}")
    ws, we = a.get("window_start_h", 0), a.get("window_end_h", 24)
    legacy = "command" not in a and bool(a.get("script"))
    command = a.get("command")
    if not call:
        rec["call"] = (f"none: {skip_reason} (alert line only)" if skip_reason
                       else "none (fingerprint diff: cordon until the user uncordons, alert line only)")
    elif not legacy and command is not None and not (isinstance(command, str) and command.strip()):
        rec["call"] = "none: protected.json alert.command must be a non-empty string or null (alert line only)"
    elif not legacy and not command:
        rec["call"] = "none: no alert command configured (alert line only)"
    elif not legacy and not _in_window(dt.datetime.now().hour, ws, we):
        rec["call"] = f"none: outside the alert window {ws:02d}:00-{we:02d}:00 (alert line only)"
    elif os.environ.get("ORCH_ALERT_DRYRUN") == "1":
        rec["call"] = (f"DRYRUN would run: MSG='{msg}' bash {a['script']} {' '.join(map(str, a.get('numbers') or []))}"
                       if legacy else f"DRYRUN would run: bash -c {shlex.quote(command)} (message on stdin and in $MSG)")
    elif not shutil.which("bash"):
        rec["call"] = "none: bash not found on PATH, so the alert command was skipped (put bash on PATH)"
    else:
        argv = ([shutil.which("bash"), str(a["script"]), *map(str, a.get("numbers") or [])] if legacy
                else [shutil.which("bash"), "-c", command])
        audit(node, None, argv, "alert command")
        flags = 0x00000008 | 0x00000200 if IS_WIN else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        with open(HOME / "alerts-call.log", "a", encoding="utf-8") as logf:  # the child keeps its own handle
            p = subprocess.Popen(argv, env=dict(os.environ, MSG=msg), stdout=logf, stderr=logf,
                                 stdin=subprocess.DEVNULL if legacy else subprocess.PIPE,
                                 creationflags=flags, start_new_session=not IS_WIN)
        if not legacy and p is not None and p.stdin:
            try:
                p.stdin.write((msg + "\n").encode())
                p.stdin.close()
            except OSError:  # the command exited without reading stdin: $MSG still had the text
                pass
        rec["call"] = ("placed via the alert script" + ("" if _in_window(dt.datetime.now().hour, ws, we)
                                                        else f" (it waits for {ws:02d}:00)")) if legacy \
            else "alert command started (bash -c, detached)"
    c = rec["call"]
    say(c if c.startswith("DRYRUN") else (f"no alert command run: {c}" if c.startswith("none") else f"alert: {c}"))
    with open(HOME / "alerts.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ----------------------------------------------------------------------------- watchdog + heartbeat
def parse_wd_line(line: str, W: dict, base: dict, eps: list) -> dict | None:
    """One 'WD <psi> <iowait> <lat>...' line -> {psi, iowait, lat, bad}; None for other lines."""
    f = line.split()
    if not f or f[0] != "WD":
        return None
    psi = None if f[1] == "na" else float(f[1])
    iow = int(f[2])
    lats = [float(x) for x in f[3:]]
    bad = []
    if psi is not None and psi > W["psi_max"]:
        bad.append(f"PSI {psi} > {W['psi_max']}")
    if iow > W["iowait_max"]:
        bad.append(f"iowait {iow}% > {W['iowait_max']}%")
    for e, v in zip(eps, lats):
        b = base.get(e["name"])
        if b is not None and v > W["lat_factor"] * b + W["lat_add_s"]:
            bad.append(f"{e['name']} {v:.3f}s > 2x{b:.3f}+0.2")
    return {"psi": psi, "iowait": iow, "lat": lats, "bad": bad}


class Watchdog(threading.Thread):
    def __init__(self, node: Node, baseline: dict, jobid: str, on_trip, lease_s: int = 300, iters: int = 720,
                 on_dead=None):
        super().__init__(daemon=True)
        self.lease_s, self.iters, self.on_dead = lease_s, int(iters), on_dead
        self.dead = None
        self.node, self.base, self.jobid, self.on_trip = node, baseline, jobid, on_trip
        self.W = cfg_nodes()["watchdog"]
        self.proc = None
        self.stop_ev = threading.Event()
        self.samples = []
        self.tripped = None
        self.eps = cfg_prot()["nodes"].get(node.name, {}).get("endpoints", [])

    def _argv(self):
        iv = self.W["interval_s"]
        lat = "\n  ".join(f"lat=\"$lat $( {curl_cmd(e)} )\"" for e in self.eps)
        script = WATCH_SH.replace("__LAT__", lat or ":").replace("__SLEEP__", str(max(iv - 1, 1)))
        if self.node.linux:
            if self.node.docker_host:
                script = f"export DOCKER_HOST={shlex.quote(self.node.docker_host)}\n" + script
            return self.node.sh_argv([str(self.iters)]), script.encode()
        return (self.node.docker_argv(["run", "--rm", "--name", f"{PREFIX}{self.jobid}-wd",
                 *label_args(self.jobid, "watchdog", self.lease_s, heavy=False), "--network", "none", "--memory", "32m",
                 "--cpus", "0.2", "--pids-limit", "32", "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
                 "--log-driver", "json-file", "--log-opt", "max-size=1m", images_cfg()["alpine"], "sh", "-c", script,
                 "wd", str(self.iters)]), None)

    def run(self):
        argv, inp = self._argv()
        audit(self.node.name, self.jobid, argv, "watchdog")
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE if inp else subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         env=dict(os.environ, MSYS_NO_PATHCONV="1"))
            if inp:
                self.proc.stdin.write(inp)
                self.proc.stdin.close()
        except OSError as e:
            self._lost(f"could not start: {e}")
            return
        try:
            self._loop()
        except Exception as e:  # noqa: BLE001 - a dead watchdog must be reported, never silent
            self._lost(f"thread error: {e!r}"[:200])
            return
        if not self.stop_ev.is_set():
            self._lost(f"stream ended early after {len(self.samples)} samples")

    def _lost(self, why: str):
        if self.stop_ev.is_set() or self.dead:
            return
        self.dead = why
        say(f"WATCHDOG lost on {self.node.name}: {why}")
        if self.on_dead:
            self.on_dead(why)

    def _loop(self):
        streak = 0
        for raw in self.proc.stdout:
            if self.stop_ev.is_set():
                break
            try:
                s = parse_wd_line(dec(raw), self.W, self.base, self.eps)
            except (ValueError, IndexError):
                continue  # one malformed sample is skipped; it never kills the watchdog
            if s is None:
                continue
            bad = s["bad"]
            self.samples.append({"t": round(now(), 1), **s})
            streak = streak + 1 if bad else 0
            if streak >= self.W["consecutive"] and not self.tripped:
                self.tripped = "; ".join(bad)
                say(f"WATCHDOG trip on {self.node.name}: {self.tripped} ({streak} samples in a row)")
                self.on_trip(self.tripped)

    def stop(self):
        self.stop_ev.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        if not self.node.linux:
            self.node.docker(["rm", "-f", f"{PREFIX}{self.jobid}-wd"], timeout=60, job=self.jobid)


class Heartbeat(threading.Thread):
    """Renews the lease every heartbeat_s by rewriting live/<jobid>.json (labels are immutable on Docker objects,
    so the effective lease is max(label lease, heartbeat lease); reconcile checks both)."""

    def __init__(self, live: dict, lease_s: int, hb_s: int):
        super().__init__(daemon=True)
        self.live, self.lease_s, self.hb_s = live, lease_s, hb_s
        self.ev = threading.Event()

    _lock = threading.Lock()

    def beat(self, **kw):
        with self._lock:
            self.live.update(kw, hb=now(), lease_until=now() + self.lease_s)
            try:
                write_json(live_dir() / f"{self.live['jobid']}.json", dict(self.live))
            except OSError as e:  # never let the heartbeat thread die
                say(f"heartbeat write failed (will retry): {e}")

    def run(self):
        while not self.ev.wait(self.hb_s):
            self.beat()

    def stop(self):
        self.ev.set()


# ----------------------------------------------------------------------------- source + results
SECRET_NAME = [r"(^|/)\.env(\.[^/]*)?$", r"\.(pem|key|pfx|p12|jks|kdbx|keystore)$", r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$",
               r"(^|/)\.npmrc$", r"(^|/)\.netrc$", r"(^|/)credentials(\.[a-z]+)?$", r"(^|/)secrets?\.(json|ya?ml|env|txt)$",
               r"appsettings\.Production\.json$", r"(^|/)twilio\.env$", r"(^|/)\.git-credentials$", r"\.ovpn$"]
SECRET_ALLOW = [r"\.env\.example$", r"\.env\.sample$", r"\.env\.template$"]
SECRET_CONTENT = [
    (re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), b"[REDACTED-PRIVATE-KEY]"),
    (re.compile(rb"AKIA[0-9A-Z]{16}"), b"[REDACTED-AWS-KEY]"),
    (re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,}"), b"[REDACTED-GH-TOKEN]"),
    (re.compile(rb"github_pat_[A-Za-z0-9_]{22,}"), b"[REDACTED-GH-TOKEN]"),
    (re.compile(rb"xox[abposr]-[A-Za-z0-9-]{10,}"), b"[REDACTED-SLACK]"),
    (re.compile(rb"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|authtoken)[\"']?\s*[=:]\s*[\"']?)([^\s\"'<>;,&]{6,})"), rb"\1[REDACTED]"),
]


SRC_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
SRC_CONN = re.compile(rb"(?i)(?:server|host|data source|addr|address)\s*=\s*([^;'\"\s,]+)[^\n]{0,300}?(?:password|pwd)\s*=\s*[^;'\"\s]+"
                      rb"|(?:password|pwd)\s*=\s*[^;'\"\s]+[^\n]{0,300}?(?:server|host|data source|addr|address)\s*=\s*([^;'\"\s,]+)")
LOCAL_HOSTS = {b"localhost", b"127.0.0.1", b"::1", b"(local)", b".", b"(localdb)", b"0.0.0.0"}
# Outgoing scan: the token formats of SECRET_CONTENT (key block is SRC_KEY; not the generic password= pattern).
SRC_TOKENS = [(rx, rep.decode().strip("[]").lower()) for rx, rep in SECRET_CONTENT[1:] if not rep.startswith(b"\\1")]
SRC_CHUNK, SRC_OVERLAP = 4 << 20, 8192


def secret_allowed(name: str, allow) -> bool:
    """Matched with re.I like the name scan, but entries from the project config arrive case-exact and anchored at the
    true end ((?-i:^...\\Z), orch_config.source_allow_engine); only v1-config patterns still ignore case."""
    return any(re.search(a, name, re.I) for a in allow)


def scan_source_content(tar_path: Path, allow: list[str]) -> list[str]:
    """Private-key blocks, token formats (AWS, GitHub, Slack) and connection strings with Password= pointing at a
    non-localhost host. Large text files are scanned in overlapping chunks, never skipped."""
    hits = []
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            if not m.isfile() or secret_allowed(m.name, allow):
                continue
            f, prev, first = t.extractfile(m), b"", True
            while True:
                chunk = f.read(SRC_CHUNK)
                if not chunk or (first and b"\0" in chunk[:2048]):
                    break
                first = False
                hit = _scan_chunk(m.name, prev + chunk)
                if hit:
                    hits.append(hit)
                    break
                prev = chunk[-SRC_OVERLAP:]
    return hits


def _scan_chunk(name: str, b: bytes) -> str | None:
    if SRC_KEY.search(b):
        return f"{name}: private key block"
    for rx, what in SRC_TOKENS:
        if rx.search(b):
            return f"{name}: {what}"
    for mm in SRC_CONN.finditer(b):
        host = (mm.group(1) or mm.group(2) or b"").split(b",")[0].split(b":")[0].strip().lower()
        host = host.split(b"\\")[0]
        if host and host not in LOCAL_HOSTS and not host.startswith(b"127."):
            return f"{name}: connection string with a password for host {host.decode(errors='replace')}"
    return None


def git_source(repo: str, ref: str | None, wt: bool, dest: Path, jobid: str, allow=()) -> dict:
    """git archive into dest (prefix src/). --wt: archive the working tree INCLUDING untracked, non-ignored
    files via a throwaway index (GIT_INDEX_FILE + read-tree HEAD + add -A + write-tree); the real index and the
    working tree are not touched. Then the name scan and the content scan; refuse on any hit."""
    gx = ["git", "-C", repo]
    if ref and ref.startswith("-"):
        raise OrchError("usage", f"bad ref {ref}: a ref cannot start with '-'")
    r = cmd(gx + ["rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}"], job=jobid, timeout=30)
    sha = r.out.strip()
    if not r.ok or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise OrchError("usage", f"bad ref {ref or 'HEAD'} in {repo}: {r.last_err()}")
    short = sha[:7]
    target, info = sha, {"ref": short, "sha": sha}
    if wt:
        if ref:
            raise OrchError("usage", "--wt archives the working tree; do not combine it with --ref")
        idx = HOME / "tmp" / f"idx-{os.getpid()}-{secrets.token_hex(3)}"
        idx.parent.mkdir(parents=True, exist_ok=True)
        e = {"GIT_INDEX_FILE": str(idx)}
        try:
            for step in (["read-tree", "HEAD"], ["add", "-A", "--", "."]):
                r = cmd(gx + step, job=jobid, timeout=300, env=e)
                if not r.ok:
                    raise OrchError("infra", f"git {step[0]} (temp index) failed: {r.last_err()}")
            r = cmd(gx + ["write-tree"], job=jobid, timeout=120, env=e)
            if not r.ok:
                raise OrchError("infra", f"git write-tree failed: {r.last_err()}")
            target = r.out.strip()
        finally:
            idx.unlink(missing_ok=True)
        d = cmd(gx + ["diff-tree", "-r", "--name-status", "HEAD", target], job=jobid, timeout=60)
        st = [l.split("\t", 1) for l in d.out.splitlines() if "\t" in l]
        info.update(ref=f"{short}+wt.{target[:7]}", wt_added=[p for s_, p in st if s_ == "A"],
                    wt_modified=sum(1 for s_, _ in st if s_ == "M"), wt_deleted=sum(1 for s_, _ in st if s_ == "D"))
    with open(dest, "wb") as f:
        r = cmd(gx + ["archive", "--format=tar", "--prefix=src/", target, "--", "."] + cfg_jobs().get("source_exclude", []),
                job=jobid, stdout=f, timeout=300)
    if not r.ok:
        raise OrchError("infra", f"git archive failed: {r.last_err()}")
    with tarfile.open(dest) as t:
        names = [m.name for m in t.getmembers() if m.isfile()]
    hits = [n for n in names if any(re.search(p, n, re.I) for p in SECRET_NAME) and not secret_allowed(n, SECRET_ALLOW)
            and not secret_allowed(n, allow)]
    if hits:
        raise OrchError("refused", f"secret-pattern scan of the archive file list matched {hits[:10]}; refusing to send")
    chits = scan_source_content(dest, list(allow))
    if chits:
        raise OrchError("refused", f"secret content scan matched {chits[:10]}; refusing to send (per-job source_allow can exempt a path)")
    info.update(files=names, bytes=dest.stat().st_size, wt=bool(wt))
    return info


WIN_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.I)


def safe_extract(tar_path: Path, dest: Path, strip: int = 1, max_total=2 << 30, max_members=50000,
                 max_file=500 << 20) -> tuple[int, list[str]]:
    """Extract only regular files and dirs; no absolute paths, no '..', no links, no devices.
    Caps: 2 GB total, 50k members, 500 MB per file, and enough free disk on the master first."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    n, refused, total, seen = 0, [], 0, {}
    with tarfile.open(tar_path) as t:
        members = []
        for m in t:
            members.append(m)
            if len(members) > max_members:
                raise OrchError("refused", f"result tar has more than {max_members} members (tar bomb?)")
        declared = sum(m.size for m in members if m.isfile())
        big = [m.name for m in members if m.isfile() and m.size > max_file]
        if big:
            raise OrchError("refused", f"result file over {max_file >> 20} MB: {big[:3]}")
        if declared > max_total:
            raise OrchError("refused", f"result tar declares {declared >> 20} MB > {max_total >> 20} MB")
        free = shutil.disk_usage(dest).free
        if free < declared + (1 << 30):
            raise OrchError("infra", f"master disk too full for results: {free >> 20} MB free, need {declared >> 20} MB + 1 GB")
        for m in members:
            parts = [p for p in m.name.replace("\\", "/").split("/") if p not in ("", ".")]
            if m.name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", m.name) or ".." in parts:
                refused.append(m.name)
                continue
            parts = parts[strip:]
            if not parts:
                continue
            if IS_WIN:  # NTFS: no alternate data streams, reserved device names, trailing dot/space, case collisions
                low = "/".join(parts).lower()
                if any(":" in q or q[-1] in ". " or WIN_RESERVED.match(q) for q in parts) or seen.get(low, m.name) != m.name:
                    refused.append(f"{m.name} (not a safe Windows path)")
                    continue
                seen[low] = m.name
            target = dest.joinpath(*parts).resolve()
            if dest != target and dest not in target.parents:
                refused.append(m.name)
                continue
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = t.extractfile(m)
                with open(target, "wb") as f:
                    shutil.copyfileobj(src, f)
                n += 1
            else:
                refused.append(f"{m.name} (link/device)")
    return n, refused


def scan_results(d: Path) -> int:
    hits = 0
    for p in d.rglob("*"):
        if not p.is_file() or p.stat().st_size > 20 << 20:
            continue
        b = p.read_bytes()
        if b"\0" in b[:4096]:
            continue
        nb = b
        for rx, rep in SECRET_CONTENT:
            nb, k = rx.subn(rep, nb)
            hits += k
        if nb != b:
            p.write_bytes(nb)
    return hits


# ----------------------------------------------------------------------------- subnets
def alloc_subnet(node: Node, owner: str, job=None, slot: int | None = None) -> str:
    pool = ipaddress.ip_network(cfg_nodes()["subnet_pool"])
    subs = list(pool.subnets(new_prefix=cfg_nodes()["subnet_prefix"]))
    used = []
    r = node.dbatch([["network", "ls", "-q"]], job=job)[0]
    ids = r.out.split()
    if ids:
        r2 = node.dbatch([["network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}"] + ids], job=job)[0]
        used += r2.out.split()
    if node.linux:
        rr = node.shell("ip -4 route show | awk '{print $1}'", timeout=30, job=job, note="routes")
        used += [x for x in rr.out.split() if "/" in x or re.fullmatch(r"[0-9.]+", x)]
    used_nets = []
    for u in used:
        try:
            used_nets.append(ipaddress.ip_network(u if "/" in u else u + "/32", strict=False))
        except ValueError:
            pass

    def pick(s):
        alloc = s["subnets"].setdefault(node.name, {})
        cands = [slot] if slot is not None else range(1, len(subs))
        for i in cands:
            if str(i) in alloc and alloc[str(i)] != owner:
                continue
            net = subs[i]
            if any(net.overlaps(u) for u in used_nets if u.prefixlen > 0):
                if str(i) in alloc and alloc[str(i)] == owner:
                    return str(net)
                continue
            alloc[str(i)] = owner
            return str(net)
        return None

    net = state_update(pick)
    if not net:
        raise OrchError("infra", f"no free /28 slot in {pool} on {node.name}")
    return net


def free_subnet(node: str, owner: str) -> None:
    def f(s):
        a = s["subnets"].get(node, {})
        for k in [k for k, v in a.items() if v == owner]:
            del a[k]
    state_update(f)


# ----------------------------------------------------------------------------- builder
def engine_key(node: Node) -> str:
    """Key of the docker engine a node's docker commands reach, for orch's per-engine records: its buildx instance
    on this master, the builder/cache last-use file and the base images orch pulled there. The node name for its usual
    endpoint (no docker context, one named like the node, or orch's own CONTEXT_PREFIX + name), else its docker
    context, buildx-safe: one machine reached as two ssh users with their own engines (another project's
    "orch-<name>-<8 hex>", R8) keeps one instance and one record set per engine, never a shared one (review round 2 Q3).
    Machine-wide accounting (live jobs, max_heavy, cordon, hold, subnets) stays by node name."""
    ctx = node.ctx
    if node.transport != "context" or not ctx or ctx in (node.name, CONTEXT_PREFIX + node.name):
        return node.name
    safe = re.sub(r"[^a-z0-9._-]", "-", ctx.lower())
    return safe if safe == ctx else f"{safe}-{hashlib.sha256(ctx.encode('utf-8')).hexdigest()[:8]}"


def builder_ref(node: Node) -> str:
    """The buildx instance (master-wide docker config) that builds on this node's engine."""
    return f"{PREFIX}builder-{engine_key(node)}"


def builder_file(node: Node) -> Path:
    """Last use of the builder and cache volumes on this node's engine (reconcile removes them after builder_idle_h)."""
    return live_dir() / f"builder-{engine_key(node)}.json"


def ensure_builder(node: Node, p: dict, jobid: str) -> None:
    B = cfg_nodes()["builder"]
    name = f"{PREFIX}builder"
    jobs = cfg_jobs()["jobs"]
    if not any((jobs.get(k) or {}).get("kind") == "build" for k in node.c.get("opt_in", [])):
        raise OrchError("infra", f"refused: {node.name} has no build job in opt_in, so no builder may run there")
    ex = node.docker(["ps", "-a", "--filter", f"name=^{name}$", "--format",
                      "{{.State}}\t" + LT + '\t{{.Label "' + L_OWNER + '"}}'], timeout=60, job=jobid)
    f_ = ex.out.strip("\r\n").split("\t") if ex.out.strip() else []
    state = f_[0].strip() if f_ else ""
    if f_ and (f_[1:2] or [""])[0].strip() != "1":
        raise OrchError("infra", f"{name} exists on {node.name} without the agent label: refusing")
    if len(f_) > 2 and f_[2].strip() != OWNER:  # another master's builder: its gc would remove it under our build
        raise OrchError("infra", f"builder {name} owned by {f_[2].strip() or '?'}")
    lease = 86400 * 2
    if state and state != "running":
        r = node.docker(["start", name], timeout=60, job=jobid)
        if not r.ok:
            raise OrchError("infra", f"builder start failed: {r.last_err()}")
    elif not state:
        ref = images_cfg()[B["image"]]
        r = ensure_image(node, ref, job=jobid)
        if not r.ok:
            raise OrchError("infra", f"cannot get builder image: {r.last_err()}")
        subnet = alloc_subnet(node, f"{PREFIX}builder", job=jobid, slot=0)
        ledger("intent", node=node.name, kind="network", name=f"{name}-net", job=jobid, subnet=subnet)
        r = node.docker(["network", "create", "--driver", "bridge", "--subnet", subnet,
                         *label_args("builder", "builder", lease, False), f"{name}-net"], timeout=60, job=jobid)
        if not r.ok and "already exists" not in r.err:
            raise OrchError("infra", f"builder network: {r.last_err()}")
        ledger("created", node=node.name, kind="network", name=f"{name}-net", job=jobid)
        ledger("intent", node=node.name, kind="volume", name=f"{name}-state", job=jobid)
        r = node.docker(["volume", "create", *label_args("builder", "builder", lease, False), f"{name}-state"],
                        timeout=60, job=jobid)
        if not r.ok:
            raise OrchError("infra", f"builder volume: {r.last_err()}")
        ledger("created", node=node.name, kind="volume", name=f"{name}-state", job=jobid)
        io = io_args(node, p)
        ledger("intent", node=node.name, kind="container", name=name, job=jobid)
        r = node.docker(["run", "-d", "--name", name, *label_args("builder", "builder", lease, False),
                         "--memory", B["memory"], "--memory-swap", B["memory"], "--cpus", str(B["cpus"]),
                         "--pids-limit", str(B["pids"]), "--oom-score-adj", "800",
                         "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=2", *io,
                         "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
                         "--security-opt", "systempaths=unconfined",
                         "--network", f"{name}-net", "-v", f"{name}-state:/home/user/.local/share/buildkit", ref,
                         "--oci-worker-gc", "--oci-worker-gc-keepstorage", B["gc_keepstorage_mb"]], timeout=120, job=jobid)
        if not r.ok:
            raise OrchError("infra", f"builder start failed: {r.last_err()}")
        ledger("created", node=node.name, kind="container", name=name, job=jobid)
        for _ in range(30):
            if node.docker(["exec", name, "buildctl", "debug", "workers"], timeout=30, job=jobid).ok:
                break
            time.sleep(1)
    if node.transport != "context":
        raise OrchError("infra", f"{node.name}: the buildx builder needs a docker context transport")
    bref, want = builder_ref(node), f"docker-container://{name}?context={node.ctx}"
    li = cmd(["docker", "buildx", "inspect", bref], job=jobid, timeout=30)
    eps = re.findall(r"(?m)^\s*Endpoint:\s*(\S+)", li.out) if li.ok else []
    if li.ok and eps != [want]:  # made for another endpoint (or edited by hand): never build through it
        say(f"buildx instance {bref} points at {', '.join(eps) or 'no endpoint'}, not {want}: re-creating it")
        r = cmd(["docker", "buildx", "rm", bref], job=jobid, timeout=60)
        if not r.ok:
            raise OrchError("infra", f"buildx rm {bref} failed: {r.last_err()}")
        ledger("removed", node="master", kind="buildx-instance", name=bref, job=jobid, why=f"endpoint {eps} != {want}")
    if not li.ok or eps != [want]:
        r = cmd(["docker", "buildx", "create", "--name", bref, "--driver", "remote", want], job=jobid, timeout=60)
        if not r.ok:
            raise OrchError("infra", f"buildx create failed: {r.last_err()}")
        ledger("created", node="master", kind="buildx-instance", name=bref, job=jobid)
    write_json(builder_file(node), {"node": node.name, "last_used": now()})


def io_args(node: Node, p: dict) -> list[str]:
    io = node.c.get("io")
    if not (node.linux and io and p.get("dev")):
        return []
    dev = p["dev"]
    out = ["--device-read-bps", f"{dev}:{io['read_bps']}", "--device-write-bps", f"{dev}:{io['write_bps']}"]
    if io.get("blkio_weight"):
        out += ["--blkio-weight", str(io["blkio_weight"])]
    return out


# ----------------------------------------------------------------------------- job execution
class OrchError(Exception):
    def __init__(self, cls: str, msg: str):
        super().__init__(msg)
        self.cls, self.msg = cls, msg


class Job:
    def __init__(self, name: str, args: list[str], mem_override=None):
        jobs = cfg_jobs()["jobs"]
        if name not in jobs:
            raise OrchError("usage", left_out_msg(name) or f"unknown job {name}; known: {', '.join(sorted(jobs))}")
        self.name, self.c, self.args = name, dict(jobs[name]), args
        if mem_override:
            self.c["memory"] = mem_override

    def __getitem__(self, k):
        return self.c[k]

    def get(self, k, d=None):
        return self.c.get(k, d)

    def argstr(self) -> str:
        return " ".join(shlex.quote(a) for a in self.args) if self.args else str(self.c.get("default_args", ""))


def left_out_msg(name: str) -> str | None:
    """Why a job of the project setup is missing from the compiled config (project mode), else None."""
    lo = (PROJECT.get("left_out") or {}).get(name)
    why = lo.get("why") if isinstance(lo, dict) else lo
    return f"job {name} cannot run yet: {why}; run orch init --approve (it pins images)" if why else None


def left_out_stage(name: str) -> str | None:
    """The stage of a job left_out_msg names (as compile_engine gives it), else None."""
    lo = (PROJECT.get("left_out") or {}).get(name)
    return lo.get("stage") if isinstance(lo, dict) else None


def new_jobid() -> str:
    return time.strftime("%m%d%H%M%S") + secrets.token_hex(2)


ACTIVE = {"ctx": None}


class Ctx:
    def __init__(self, job: Job, node: Node, p: dict, jobid: str, live: dict, opts):
        self.job, self.node, self.p, self.jobid, self.opts = job, node, p, jobid, opts
        self.lease_s = opts.lease
        self.dir = HOME / "results" / jobid
        self.dir.mkdir(parents=True, exist_ok=True)
        (HOME / "node-logs").mkdir(parents=True, exist_ok=True)
        self.log = HOME / "node-logs" / f"{jobid}.log"
        self.t = {}
        self.timed_out = False
        self.guard = None
        self.wd_lost = None  # watchdog died before stop(): fail closed
        self.orch_killed = False  # orch itself stopped/killed the job container (never oom/test-fail)
        self.nonce = secrets.token_hex(8)  # phase-marker nonce, known only to the wrapper script
        self.meta = {"jobid": jobid, "job": job.name, "node": node.name, "session": session_id(), "owner": OWNER,
                     "args": job.args, "memory": job.get("memory"), "started": ts(), "managed": True,
                     **src_ids(getattr(opts, "_src", None))}
        self.hb = Heartbeat(live, self.lease_s, opts.heartbeat)
        self.cleaned = False

    def name(self, role):
        return f"{PREFIX}{self.jobid}-{role}"

    def labels(self, role):
        return label_args(self.jobid, role, self.lease_s, bool(self.job.get("heavy")))

    def phase(self, k):
        ctx = self

        class _P:
            def __enter__(s):
                s.t0 = now()
                ctx.hb.beat(stage=k)

            def __exit__(s, *a):
                ctx.t[k] = round(ctx.t.get(k, 0) + now() - s.t0, 2)
        return _P()


def hardening(ctx: Ctx, mem: str, network: str, caps: list[str], read_only=False) -> list[str]:
    j = ctx.job
    a = ["--memory", mem, "--memory-swap", mem, "--cpus", str(j.get("cpus", 1)), "--pids-limit", str(j.get("pids", 256)),
         "--oom-score-adj", "1000", "--security-opt", "no-new-privileges", "--cap-drop", "ALL"]
    for c in caps:
        a += ["--cap-add", c]
    a += ["--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=2"]
    a += io_args(ctx.node, ctx.p)
    a += ["--network", network]
    if read_only:
        a += ["--read-only", "--tmpfs", "/tmp:size=16m"]
    for k, v in (j.get("env") or {}).items():
        a += ["-e", f"{k}={v}"]
    return a


def stream_attach(ctx: Ctx, cname: str, deadline, on_line=None) -> int:
    """docker start -a with an outer timeout; deadline() may move (prep budget, then run budget).
    On timeout: docker stop -t 10, then kill by name."""
    argv = ctx.node.docker_argv(["start", "-a", cname])
    audit(ctx.node.name, ctx.jobid, argv)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    tail = []

    def reader():
        for raw in proc.stdout:
            s = dec(raw)
            Logger.raw(s)
            tail.append(s)
            if len(tail) > 4000:
                del tail[:1000]
            if on_line:
                try:
                    on_line(s)
                except Exception as e:  # noqa: BLE001
                    say(f"line hook failed: {e!r}")
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    while proc.poll() is None:
        if ctx.guard or ctx.wd_lost:
            break
        if now() > deadline():
            ctx.timed_out = ctx.orch_killed = True
            say(f"timeout reached: docker stop -t 2 {cname}")  # short grace: the result is timeout either way
            ctx.node.docker(["stop", "-t", "2", cname], timeout=30, job=ctx.jobid)
            ctx.node.docker(["kill", cname], timeout=30, job=ctx.jobid)
            break
        time.sleep(0.5)
    if (ctx.guard or ctx.wd_lost) and not ctx.timed_out:
        ctx.orch_killed = True
        ctx.node.docker(["kill", cname], timeout=30, job=ctx.jobid)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    th.join(timeout=5)
    ctx.tail = "".join(tail)
    return proc.returncode


def inspect_state(ctx: Ctx, cname: str) -> dict:
    r = ctx.node.docker(["inspect", "--format", "{{json .State}}", cname], timeout=60, job=ctx.jobid)
    try:
        return json.loads(r.out.strip())
    except json.JSONDecodeError:
        return {}


def classify(st: dict, rc: int, tail: str = "", flags: dict | None = None) -> str:
    """Container state -> result class. flags: guard (watchdog tripped), timed_out, orch_killed (orch issued the
    stop/kill itself: never oom/test-fail), wd_lost (watchdog died: infra)."""
    flags = flags or {}
    if flags.get("guard"):
        return "r1-guard"
    if flags.get("timed_out"):
        return "timeout"
    if flags.get("orch_killed") or flags.get("wd_lost"):
        return "infra"
    if st.get("Error") and not st.get("OOMKilled"):
        return "infra"
    if st.get("OOMKilled"):
        return "oom"
    if flags.get("timed_out"):
        return "timeout"
    code = st.get("ExitCode", rc)
    if code == 0:
        return "pass"
    if code == 137 and OOM_EVIDENCE.search(tail or ""):
        # SIGKILL without OOMKilled (cgroup v2 can report it late): only kernel/cgroup OOM phrasing counts
        return "oom"
    return "test-fail"


OOM_EVIDENCE = re.compile(r"\bKilled process \d+|\boom[-_ ]?kill|\bOOMKilled\b|\bout of memory\b", re.I)


def classify_ctx(ctx: Ctx, st: dict, rc: int) -> str:
    if not ctx.guard and st.get("Error") and not st.get("OOMKilled"):
        ctx.meta.setdefault("notes", []).append("container start error: " + st["Error"][:300])
    return classify(st, rc, getattr(ctx, "tail", ""), {"guard": ctx.guard, "timed_out": ctx.timed_out,
                    "orch_killed": getattr(ctx, "orch_killed", False) is True,
                    "wd_lost": isinstance(getattr(ctx, "wd_lost", None), str)})


def is_build_oom(text: str) -> bool:
    return bool(re.search(r"\bOOM(Killed)?\b|\bout of memory\b|signal: killed|exit code: 137\b", text, re.I))


ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")


def failure_lines(text: str, patterns) -> list[str]:
    """Up to 20 log lines matching the job's fail_patterns; each <=200 chars, ANSI and control chars stripped."""
    out = []
    for line in (text or "").splitlines():
        clean = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", ANSI.sub("", line)).replace("\t", " ").strip()
        if clean and any(re.search(p, clean) for p in patterns or []):
            out.append(clean[:200])
            if len(out) >= 20:
                break
    return out


def create_run(ctx: Ctx, role: str, image: str, network: str, caps, script: str, mem: str, extra=(), read_only=False):
    cname = ctx.name(role)
    shell = ctx.job.get("shell", "sh")
    fault = legacy_env("ORCH_FAULT")  # test-only fault injection, e.g. ORCH_FAULT=create@n1
    if fault == f"create@{ctx.node.name}":
        raise OrchError("infra", f"injected fault (ORCH_FAULT={fault}): simulated ssh/daemon failure on create")
    ledger("intent", node=ctx.node.name, kind="container", name=cname, job=ctx.jobid)
    r = ctx.node.docker(["create", "--name", cname, *ctx.labels(role), *hardening(ctx, mem, network, caps, read_only),
                         "-e", f"AGENTX_REF={ctx.meta.get('ref', '')}", "-e", f"AGENTX_JOB={ctx.jobid}", *extra,
                         "--entrypoint", shell, image, "-c", script],
                        timeout=120, job=ctx.jobid)
    if not r.ok:
        raise OrchError("infra", f"create {cname} failed: {r.last_err()}")
    warn = [l for l in r.err.splitlines() if "WARNING" in l.upper()]
    if warn:
        say(f"docker create warnings: {' | '.join(warn)[:300]}")
        ctx.meta.setdefault("notes", []).extend(warn[:3])
    ledger("created", node=ctx.node.name, kind="container", name=cname, job=ctx.jobid)
    return cname


def collect(ctx: Ctx, cname: str, failed: bool, st: dict) -> None:
    for pth in ctx.job.get("result_paths", []) or []:
        tarp = HOME / "tmp" / f"{ctx.jobid}-out.tar"
        tarp.parent.mkdir(parents=True, exist_ok=True)
        with open(tarp, "wb") as f:
            r = ctx.node.docker(["cp", f"{cname}:{pth}", "-"], stdout=f, timeout=300, job=ctx.jobid)
        if r.ok:
            n, refused = safe_extract(tarp, ctx.dir / "out")
            ctx.meta["result_files"] = n
            if refused:
                ctx.meta["result_refused"] = refused[:20]
        else:
            ctx.meta.setdefault("notes", []).append(f"no {pth}: {r.last_err()}")
        tarp.unlink(missing_ok=True)
    if failed:
        r = ctx.node.docker(["inspect", cname], timeout=60, job=ctx.jobid)
        (ctx.dir / "inspect.json").write_text(r.out, encoding="utf-8")
        r = ctx.node.docker(["logs", "--tail", "2000", cname], timeout=60, job=ctx.jobid)
        (ctx.dir / "container.log").write_text(r.out + r.err, encoding="utf-8")


NL = chr(10)
NONCE_FILE = "/.agentx/nonce"


def nonce_tar(nonce: str) -> bytes:
    """In-memory tar for docker cp: a world-writable /.agentx dir (any container user can delete the file) holding
    the nonce, mode 0444."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        d = tarfile.TarInfo(NONCE_FILE.lstrip("/").split("/")[0])
        d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o777, int(now())
        t.addfile(d)
        data = nonce.encode()
        f = tarfile.TarInfo(NONCE_FILE.lstrip("/"))
        f.size, f.mode, f.mtime = len(data), 0o444, int(now())
        t.addfile(f, io.BytesIO(data))
    return buf.getvalue()


def compose_script(prep, cmd_, nonce=False):
    """One container, two phases. Prep runs with the per-job egress network; then orch disconnects that
    network (docker network disconnect) and the script waits until only loopback is left before the tests
    start, so test code runs with no network at all (equivalent to --network none). No commit, no image.
    nonce=True: phase markers carry a nonce that orch docker-cp's into NONCE_FILE before start (never env or
    argv, so /proc/1/environ and /proc/1/cmdline do not hold it). The wrapper reads it into a non-exported shell
    variable and deletes the file before prep starts; prep/cmd subshells also unset the copy, so their output
    cannot forge a marker. Residual: code running as root with ptrace (CAP_SYS_PTRACE; dropped by --cap-drop ALL)
    or reading /proc/1/mem could still recover the variable from the wrapper's memory."""
    peak = 'echo "AGENTX_MEM_PEAK=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null)"'
    hide = "unset _xn; " if nonce else ""
    run = "( " + hide + cmd_ + NL + "); rc=$?; " + peak + "; exit $rc"
    if not prep:
        return run
    n = " $_xn" if nonce else ""
    return ((f"_xn=$(cat {NONCE_FILE} 2>/dev/null); rm -f {NONCE_FILE}" + NL if nonce else "")
            + "( " + hide + prep + NL + "); rc=$?; if [ $rc -ne 0 ]; then echo \"AGENTX_PREP_FAILED" + n + " rc=$rc\"; " + peak + "; exit 97; fi" + NL
            + "echo AGENTX_PREP_DONE" + n + NL
            + "i=0; while ls /sys/class/net | grep -qv '^lo$'; do i=$((i+1)); if [ $i -gt 900 ]; then echo AGENTX_NET_STILL_UP" + n + "; exit 98; fi; sleep 0.2; done" + NL
            + "echo \"AGENTX_NET_NONE" + n + " interfaces: $(ls /sys/class/net | tr '\n' ' ')\"" + NL + run)


def run_container_job(ctx: Ctx) -> str:
    j, node = ctx.job, ctx.node
    ref = images_cfg()[j["image"]]
    with ctx.phase("image"):
        r = ensure_image(node, ref, job=ctx.jobid)
        if not r.ok:
            raise OrchError("infra", f"base image {ref} unavailable on {node.name}: {r.last_err()}")
    src = ctx.opts._src
    if src:
        ctx.meta.update(ref=src["ref"], src_files=len(src["files"]), src_bytes=src["bytes"])
    cache, extra, netname = j.get("cache"), [], None
    with ctx.phase("setup"):
        if j.get("prep"):
            netname = ctx.name("net")
            subnet = alloc_subnet(node, netname, job=ctx.jobid)
            ledger("intent", node=node.name, kind="network", name=netname, job=ctx.jobid, subnet=subnet)
            r = node.docker(["network", "create", "--driver", "bridge", "--subnet", subnet, *ctx.labels("net"), netname],
                            timeout=60, job=ctx.jobid)
            if not r.ok:
                raise OrchError("infra", f"network create failed: {r.last_err()}")
            ledger("created", node=node.name, kind="network", name=netname, job=ctx.jobid, subnet=subnet)
            if cache:
                vname = f"{PREFIX}cache-{cache['name']}"
                if vname not in ctx.p.get("caches", []):
                    ledger("intent", node=node.name, kind="volume", name=vname, job=ctx.jobid)
                    r = node.docker(["volume", "create", *label_args("cache", "cache", 86400 * 2, False), vname],
                                    timeout=60, job=ctx.jobid)
                    if not r.ok:
                        raise OrchError("infra", f"cache volume create failed: {r.last_err()}")
                    ledger("created", node=node.name, kind="volume", name=vname, job=ctx.jobid)
                extra = ["-v", f"{vname}:{cache['path']}"]
                write_json(builder_file(node), {"node": node.name, "last_used": now()})
            say(f"prep on {netname} ({subnet}); network disconnected before the tests start")
        script = compose_script(j.get("prep"), j["cmd"].replace("{args}", ctx.job.argstr()), nonce=bool(netname))
        cname = create_run(ctx, "job", ref, netname or "none", [], script, j["memory"], extra,
                           read_only=j.get("read_only", False))
        if netname:  # the marker nonce goes in as a file, never env/argv (see compose_script)
            r = node.docker(["cp", "-", f"{cname}:/"], input=nonce_tar(ctx.nonce), timeout=60, job=ctx.jobid)
            if not r.ok:
                raise OrchError("infra", f"copy phase nonce failed: {r.last_err()}")
        if src:
            with open(src["tar"], "rb") as f:
                r = node.docker(["cp", "-", f"{cname}:/"], stdin=f, timeout=300, job=ctx.jobid)
            if not r.ok:
                raise OrchError("infra", f"copy source failed: {r.last_err()}")
    marks = {"t0": now()}

    def deadline():
        if netname and "prep" not in marks:
            return marks["t0"] + j.get("prep_timeout", 900)
        return marks.get("prep", marks["t0"]) + j["timeout"]

    def disconnect():
        r = node.docker(["network", "disconnect", netname, cname], timeout=60, job=ctx.jobid)
        marks["disconnected"] = r.ok
        if r.ok:
            say(f"network {netname} disconnected from {cname}: tests start", m=True)
        else:
            say(f"warn: network {netname} disconnect from {cname} failed: {r.last_err()}; killing the job")
            ctx.orch_killed = True
            node.docker(["kill", cname], timeout=30, job=ctx.jobid)

    mk = {k: f"AGENTX_{k} {ctx.nonce}" for k in ("PREP_DONE", "NET_NONE", "PREP_FAILED", "NET_STILL_UP")}

    def on_line(s):
        s = s.strip()
        if netname and s == mk["PREP_DONE"] and "prep" not in marks:
            marks["prep"] = now()
            say("prep done", m=True)
            ctx.hb.beat(stage="run")
            marks["dth"] = threading.Thread(target=disconnect, daemon=True)
            marks["dth"].start()
        if netname and s.startswith(mk["NET_NONE"] + " ") and "prep" in marks and "net_none" not in marks:
            marks["net_none"] = now()  # counted at the end only if the disconnect succeeded
    ctx.hb.beat(stage="prep" if netname else "run")
    if not netname:
        say("tests start", m=True)
    rc = stream_attach(ctx, cname, deadline, on_line)
    tend = now()
    if marks.get("dth"):
        marks["dth"].join(timeout=70)  # the disconnect result decides whether net_none counts
    ctx.t["prep"] = round(marks.get("prep", tend if netname else marks["t0"]) - marks["t0"], 2)
    ctx.t["run"] = round(tend - marks.get("prep", marks["t0"]), 2) if (marks.get("prep") or not netname) else 0.0
    st = inspect_state(ctx, cname)
    ctx.meta["state"] = {k: st.get(k) for k in ("ExitCode", "OOMKilled", "Error", "StartedAt", "FinishedAt")}
    tail = getattr(ctx, "tail", "")
    m = re.findall(r"AGENTX_CACHE_MB=(\d+)", tail)
    if m:
        ctx.meta["cache_mb"] = int(m[-1])
    m = re.findall(r"AGENTX_MEM_PEAK=(\d+)", tail)
    if m:
        ctx.meta["mem_peak"] = int(m[-1])
    cls = classify_ctx(ctx, st, rc)
    net_ok = bool(marks.get("net_none") and marks.get("disconnected"))
    if netname and not net_ok and cls == "pass":
        cls = "infra"  # never report a pass whose tests did not provably run without network
        ctx.meta.setdefault("error", "no proof the tests ran without network (nonce marker after a successful disconnect)")
    lines = [l.strip() for l in tail.splitlines()]
    if netname and mk["NET_STILL_UP"] in lines:
        cls = "infra"
        ctx.meta["error"] = "network still attached after disconnect"
    if netname and any(l.startswith(mk["PREP_FAILED"] + " ") for l in lines) and cls == "test-fail":
        ctx.meta["failed_phase"] = "prep"
        cls = "test-fail" if any(re.search(p, tail, re.M) for p in j.get("fail_patterns", [])) else "infra"
    if cls != "pass":
        ctx.meta["failures"] = failure_lines(tail, j.get("fail_patterns", []))
    with ctx.phase("collect"):
        collect(ctx, cname, cls != "pass", st)
    return cls


def run_build_job(ctx: Ctx) -> str:
    j, node = ctx.job, ctx.node
    with ctx.phase("builder"):
        ensure_builder(node, ctx.p, ctx.jobid)
    with ctx.phase("source"):
        src = ctx.opts._src
        tar, short = Path(src["tar"]), src["ref"]
        ctx.meta.update(ref=short, src_files=len(src["files"]))
        # Build context = the job's "context" subdir of the repo (default: the repo root), re-packed without the
        # src/<context>/ prefix; the Dockerfile (path relative to the repo root, may sit outside the context) is
        # injected as .agentx/Dockerfile (+ a results stage).
        cdir = str(j.get("context") or "").strip("/")
        cdir = "" if cdir in ("", ".") else cdir
        pre = "src/" + (cdir + "/" if cdir else "")
        dfp = str(j.get("dockerfile") or "Dockerfile")
        while dfp.startswith("./"):
            dfp = dfp[2:]
        dfp = "src/" + dfp
        ctxtar = HOME / "tmp" / f"{ctx.jobid}-ctx.tar"
        ctxtar.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar) as ti, tarfile.open(ctxtar, "w") as to:
            df_text, n_ctx = None, 0
            for m in ti.getmembers():
                inside = m.name.startswith(pre) and m.name.rstrip("/") + "/" != pre
                if not (inside or m.name == dfp):
                    continue
                raw = ti.extractfile(m).read() if m.isfile() else None
                if m.name == dfp and raw is not None:
                    df_text = raw.decode("utf-8")
                if not inside:
                    continue
                m2 = tarfile.TarInfo(m.name[len(pre):])
                m2.size, m2.mode, m2.type, m2.mtime, m2.linkname = m.size, m.mode, m.type, m.mtime, m.linkname
                m2.uid = m2.gid = 0
                m2.uname = m2.gname = "root"
                to.addfile(m2, io.BytesIO(raw) if raw is not None else None)
                n_ctx += 1
            if df_text is None:
                raise OrchError("usage", f"Dockerfile {dfp[4:]} is not in the source archive of {short}: "
                                         f"fix the job's dockerfile path (relative to the repo root)")
            if cdir and not n_ctx:
                raise OrchError("usage", f"build context {cdir}/ has no files in {short}: fix the job's context subdir")
            rs = j.get("results_stage")
            extra = "\nARG AGENTX_REF\n"
            if rs:
                extra += f"\nFROM scratch AS agentx-out\nCOPY --from={rs['from']} {rs['path']} /\n"
            body = (df_text + "\n" + extra).encode()
            ti2 = tarfile.TarInfo(".agentx/Dockerfile")
            ti2.size, ti2.mtime, ti2.mode = len(body), int(now()), 0o644
            to.addfile(ti2, io.BytesIO(body))
    bref = builder_ref(node)
    base = ["docker", "buildx", "build", "--builder", bref, "-f", ".agentx/Dockerfile", "--progress", "plain",
            "--build-arg", f"AGENTX_REF={short}"]
    for k, v in (j.get("build_args") or {}).items():
        base += ["--build-arg", f"{k}={v}"]
    for a in ctx.labels("image")[1::2]:
        base += ["--label", a]
    end = now() + j["timeout"]
    out_text = []
    cls = "pass"
    try:
        with ctx.phase("run"):
            steps = [(t, ["--output", "type=cacheonly"]) for t in j.get("targets", [""])]
            outtar = HOME / "tmp" / f"{ctx.jobid}-out.tar"
            if j.get("results_stage"):
                steps.append(("agentx-out", ["--output", f"type=tar,dest={outtar}"]))
            for target, out in steps:
                if ctx.guard or ctx.wd_lost:  # a trip or a lost watchdog: no later target or results stage starts
                    ctx.orch_killed = True
                    cls = "r1-guard" if ctx.guard else "infra"
                    say(f"not starting build target {target or '(default)'} on {node.name}: "
                        f"{'watchdog r1-guard: ' + ctx.guard if ctx.guard else 'watchdog lost: ' + ctx.wd_lost}")
                    break
                argv = base + (["--target", target] if target else []) + out + ["-"]
                remaining = max(30, end - now())
                say(f"buildx build {'--target ' + target if target else '(default target)'} on {node.name} via {bref}")
                with open(ctxtar, "rb") as f:
                    r = watched_cmd(argv, stop=lambda: ctx.guard or ctx.wd_lost, node=node.name, job=ctx.jobid,
                                    stdin=f, timeout=remaining)
                Logger.raw(r.out + r.err)
                out_text.append(r.out + r.err)
                if r.stopped:
                    ctx.orch_killed = True
                    cls = "r1-guard" if ctx.guard else "infra"
                    ctx.meta["failed_target"] = target
                    say(f"build killed on {node.name} ({r.stopped}): later targets are not started")
                    stop_idle_builder(ctx)
                    break
                if r.rc == 124:
                    ctx.timed_out = True
                    cls = "timeout"
                    break
                if not r.ok:
                    txt = r.out + r.err
                    if any(re.search(p, txt, re.M) for p in j.get("fail_patterns", [])):
                        cls = "test-fail"
                    elif is_build_oom(txt):
                        cls = "oom"
                    else:
                        cls = "infra"
                    ctx.meta["failed_target"] = target
                    ctx.meta["failures"] = failure_lines(txt, j.get("fail_patterns", []))
                    break
        with ctx.phase("collect"):
            if cls == "pass" and j.get("results_stage") and outtar.exists():
                n, refused = safe_extract(outtar, ctx.dir / "out", strip=0)
                ctx.meta["result_files"] = n
                if refused:
                    ctx.meta["result_refused"] = refused[:20]
            (ctx.dir / "build.log").write_text("".join(out_text), encoding="utf-8")
    finally:
        ctxtar.unlink(missing_ok=True)
        (HOME / "tmp" / f"{ctx.jobid}-out.tar").unlink(missing_ok=True)
        write_json(builder_file(node), {"node": node.name, "last_used": now()})
    return cls


def stop_idle_builder(ctx: Ctx) -> None:
    """After orch killed a build client on a trip or a lost watchdog. Closing the client cancels its BuildKit solve;
    for a solve that outlives it, orch also stops its agent builder, but only while no other live build job of this
    master is on the node (decided under the master lock, which admission takes; ensure_builder starts it again)."""
    node, name = ctx.node, f"{PREFIX}builder"
    jobs = cfg_jobs()["jobs"]
    try:
        with FileLock(timeout=30):
            others = sorted(l["jobid"] for l in live_jobs()
                            if l.get("_alive") and l.get("node") == node.name and l["jobid"] != ctx.jobid
                            and (jobs.get(l.get("job")) or {"kind": "build"}).get("kind") == "build")
            if others:
                ctx.meta["builder_stopped"] = False
                say(f"builder {name} on {node.name} kept running: live build job(s) {', '.join(others)} use it")
                return
            r = node.docker(["stop", "-t", "5", name], timeout=60, job=ctx.jobid)
    except TimeoutError as e:
        ctx.meta["builder_stopped"] = False
        say(f"warn: builder {name} on {node.name} not stopped ({e}): orch gc --session-end removes it once no job "
            f"runs there")
        return
    ctx.meta["builder_stopped"] = r.ok
    say(f"builder {name} on {node.name} stopped" if r.ok else
        f"warn: could not stop builder {name} on {node.name}: {r.last_err()}")


def run_external_job(ctx: Ctx) -> str:
    """kind external (allowed only with "allow_external": true): an argv run on this master, e.g. an existing e2e
    script. Counts toward max_heavy (heavy=true) and runs under the fingerprint + watchdog like any job, but orch does
    not own the objects it touches: managed=false."""
    ctx.meta["managed"] = False
    argv = [a.replace("{args}", ctx.job.argstr()) for a in ctx.job["cmd"]]
    with ctx.phase("run"):
        r = watched_cmd(argv, stop=lambda: ctx.guard or ctx.wd_lost, node=ctx.node.name, job=ctx.jobid,
                        timeout=ctx.job["timeout"])
    Logger.raw(r.out + r.err)
    if r.stopped:  # a trip or a lost watchdog: killed with its children (execute cordons, alerts or reports infra)
        ctx.orch_killed = True
        say(f"external job killed ({r.stopped})")
        return "r1-guard" if ctx.guard else "infra"
    if r.rc == 124:
        ctx.timed_out = True
        return "timeout"
    if not r.ok:
        ctx.meta["failures"] = failure_lines(r.out + r.err, ctx.job.get("fail_patterns", []))
    return "pass" if r.ok else "test-fail"


def cleanup_job(ctx: Ctx, why="job end") -> list[str]:
    if ctx.cleaned:
        return []
    ctx.cleaned = True
    t0 = now()
    la = list_agent(ctx.node, job=ctx.jobid)
    mine = [o for o in la["objects"] if o.get("job") == ctx.jobid and o.get("role") not in ("builder", "cache")]
    cap = cfg_nodes()["builder"].get("cache_volume_cap_mb", 6000)
    cname = (ctx.job.get("cache") or {}).get("name")
    if cname and ctx.meta.get("cache_mb", 0) > cap:
        say(f"cache volume {PREFIX}cache-{cname} is {ctx.meta['cache_mb']} MB > cap {cap} MB: wiping it")
        mine += [o for o in la["objects"] if o["name"] == f"{PREFIX}cache-{cname}"]
    removed = remove_objects(ctx.node, mine, why, job=ctx.jobid)
    for _ in range(2 if ctx.timed_out else 0):  # a timed-out client call may have left the container: re-list, rm -f
        la = list_agent(ctx.node, job=ctx.jobid)
        left = [o for o in la["objects"] if o.get("job") == ctx.jobid and o.get("role") not in ("builder", "cache")]
        if not left:
            break
        say(f"warn: timeout cleanup: {len(left)} object(s) of {ctx.jobid} still present, removing again")
        removed += remove_objects(ctx.node, left, why + " (timeout retry)", job=ctx.jobid)
    for role in ("net",):
        free_subnet(ctx.node.name, ctx.name(role))
    ctx.t["cleanup"] = round(now() - t0, 2)
    say(f"cleanup: removed {len(removed)} object(s)", m=True)
    return removed


def emergency_cleanup():
    ctx = ACTIVE.get("ctx")
    if ctx and not ctx.cleaned:
        try:
            say(f"emergency cleanup of {ctx.jobid} on {ctx.node.name}")
            cleanup_job(ctx, "signal/atexit")
        except Exception as e:  # reconcile catches the rest
            say(f"emergency cleanup incomplete ({e}); reconcile will finish after the lease expires")
        try:
            (live_dir() / f"{ctx.jobid}.json").unlink(missing_ok=True)
        except OSError:
            pass


def _sig(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def _j_pick(pick, scored, reasons) -> None:
    """Journal one admission decision: the pick and every candidate's score or skip reason."""
    nodes = {k: {"ok": True, "score": sc} for sc, k, _ in scored}
    for k, r in reasons.items():
        nodes[k] = dict(nodes.get(k, {}), ok=False, reason=" ".join(str(r).split())[:200])
    JOURNAL.setdefault("admission", []).append({"pick": pick, "nodes": nodes})


def select_node(job: Job, opts, exclude=(), mem=None) -> tuple:
    """Hard filter, probe, admission, score; reserve the slot under the master lock. Returns (node, probe, live)
    or (None, reasons)."""
    allnodes = nodes_all(include_disabled=True)
    if opts.node:
        if opts.node not in allnodes:
            raise OrchError("usage", f"unknown node {opts.node}")
        cands = {opts.node: allnodes[opts.node]}
    else:
        cands = {k: v for k, v in allnodes.items() if k not in exclude}
    ref = images_cfg().get(job.get("image")) if job.get("image") else None
    deadline = now() + opts.wait
    backoff = 5
    A = cfg_nodes()["admission"]
    while True:
        reasons = {}
        elig = {}
        for k, n in cands.items():
            hf = hard_filter(job.name, job.c, n)
            if hf:
                reasons[k] = "; ".join(hf)
            else:
                elig[k] = n
        probes = {}
        if elig:
            with cf.ThreadPoolExecutor(len(elig)) as ex:
                fut = {k: ex.submit(probe, n, [ref] if ref else []) for k, n in elig.items()}
                probes = {k: f.result() for k, f in fut.items()}
        scored = []
        for k, n in elig.items():
            a = admit(job.c, n, probes[k], mem)
            if a["ok"]:
                scored.append((a["score"], k, a))
            else:
                reasons[k] = "; ".join(c["detail"] for c in a["checks"] if not c["ok"])
        scored.sort(reverse=True)
        for score, k, a in scored:
            say(f"admitted {k}: score {score} {json.dumps(a['breakdown'])}")
        with FileLock():
            for score, k, a in scored:
                if job.get("heavy"):
                    local = {l["jobid"] for l in live_jobs() if l["node"] == k and l.get("heavy") and l["_alive"]}
                    busy = local | set(probes[k].get("heavy_jobs", []))
                    cap = node_max_heavy(k, A)
                    if len(busy) >= cap:
                        reasons[k] = f"busy: agent heavy jobs {len(busy)} >= max {cap} ({', '.join(sorted(busy))})"
                        continue
                jobid = new_jobid()
                live = {"jobid": jobid, "pid": os.getpid(), "pstart": proc_start(os.getpid()), "node": k, "job": job.name, "heavy": bool(job.get("heavy")),
                        "started": now(), "hb": now(), "lease_until": now() + opts.lease, "stage": "reserved",
                        "score": score, "session": session_id()}
                write_json(live_dir() / f"{jobid}.json", live)
                _j_pick(k, scored, reasons)
                return allnodes[k], probes[k], live, a
        left = deadline - now()
        msg = "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "no candidate nodes"
        if left <= 0:
            _j_pick(None, scored, reasons)
            return None, None, None, msg
        say(f"nothing admitted ({msg}); waiting up to {int(left)}s more")
        time.sleep(min(backoff, left))
        backoff = min(backoff * 2, 60)


def result_reason(cls: str, ctx) -> str | None:
    """One line for every non-pass result (None for pass)."""
    if cls == "pass":
        return None
    m = ctx.meta
    why = {"r1-guard": lambda: f"watchdog r1-guard: {ctx.guard}",
           "timeout": lambda: f"timeout after {ctx.job.get('timeout')}s",
           "oom": lambda: f"out of memory at {ctx.job.get('memory')}"}.get(cls)
    txt = m.get("error") or (why() if why else None) or ((m.get("failures") or [None])[0]) or cls
    if cls == "test-fail" and not m.get("error"):
        f = m.get("failures") or []
        txt = ("tests failed" + (f" in {m['failed_phase']}" if m.get("failed_phase") else "")
               + (f": {f[0]}" if f else f": exit {(m.get('state') or {}).get('ExitCode')}"))
    return " ".join(str(txt).split())[:300]


def execute(job: Job, node: Node, p: dict, live: dict, opts) -> tuple[str, Ctx]:
    ctx = Ctx(job, node, p, live["jobid"], live, opts)
    ACTIVE["ctx"] = ctx
    Logger.fh = open(ctx.log, "a", encoding="utf-8")
    t_start = now()
    ctx.t["select"] = round(opts._sel_s, 2)
    say(f"start job {ctx.jobid} {job.name} on {node.name} (lease {opts.lease}s, heartbeat {opts.heartbeat}s)", m=True)
    ctx.hb.start()
    wd = None
    cls = "infra"
    fp0 = None
    try:
        with ctx.phase("preflight"):
            la = list_agent(node, job=ctx.jobid)
            if la.get("foreign"):  # another orch namespace's leftovers: refuse the node with what they are + the fix
                raise OrchError("infra", f"{node_label(node)}: {foreign_text(la['foreign'])}")
            if la["collisions"]:
                raise OrchError("infra", "ownership assertion failed: " + "; ".join(la["collisions"][:5]))
            rec = reconcile_node(node, la, quiet=True)
            if rec:
                say(f"reconciled before dispatch: {', '.join(rec)}")
        with ctx.phase("fingerprint_before"):
            fp0 = fingerprint(node, job=ctx.jobid)
            if not fp0["ok"]:
                raise OrchError("infra", f"fingerprint failed (node/daemon): {fp0.get('error', '')}")
            say(f"fingerprint before: {len(fp0['containers'])} non-agent containers, latency baseline {fp0['latency']}")
            if fp0.get("unreachable_at_baseline"):
                ctx.meta["unreachable_at_baseline"] = fp0["unreachable_at_baseline"]

        def trip(reason):
            ctx.guard = reason

        def lost(why):
            ctx.wd_lost = why
        W = cfg_nodes()["watchdog"]
        span = int(job.get("timeout", 3600)) + int(job.get("prep_timeout", 900)) + 900  # + image/setup/collect margin
        wd = Watchdog(node, fp0["latency"], ctx.jobid, trip, ctx.lease_s,
                      iters=-(-span // max(int(W["interval_s"]) - 1, 1)), on_dead=lost)  # script sleeps interval-1
        wd.start()
        kind = job["kind"]
        cls = {"container": run_container_job, "build": run_build_job, "external": run_external_job}[kind](ctx)
        if ctx.wd_lost and cls != "r1-guard" and not ctx.guard:
            raise OrchError("infra", f"watchdog lost: {ctx.wd_lost}")
    except OrchError as e:
        cls = "infra" if e.cls == "infra" else e.cls
        ctx.meta["error"] = e.msg
        say(f"{cls}: {e.msg}")
    except KeyboardInterrupt as e:
        cls = "infra"
        ctx.meta["error"] = f"interrupted ({e})"
        say(f"interrupted: cleaning up ({e})")
    finally:
        st = {"cls": cls, "removed": []}

        def step(name, fn):  # every finalisation step runs even if an earlier one fails
            try:
                fn()
            except BaseException as e:  # noqa: BLE001 - must not abort cleanup
                ctx.meta.setdefault("finalise_errors", []).append(f"{name}: {e!r}"[:300])
                say(f"finalise step {name} failed: {e!r}")

        def s_wd():
            if wd:
                wd.stop()
                ctx.meta["watchdog_samples"] = len(wd.samples)
                ctx.meta["watchdog"] = wd.samples[-5:]

        def s_clean():
            st["removed"] = cleanup_job(ctx)

        def s_guard():
            if ctx.guard:
                st["cls"] = "r1-guard"
                cordon(node.name, f"r1-guard: {ctx.guard}", cfg_nodes()["watchdog"]["guard_cordon_min"], "watchdog")
                r1_guard_alert(node.name, job, ctx.guard)

        def s_fp():
            if not (fp0 and fp0["ok"]):
                return
            t0 = now()
            fp1 = fingerprint(node, job=ctx.jobid, lat_samples=0)
            ctx.t["fingerprint_after"] = round(now() - t0, 2)
            if not fp1["ok"]:
                ctx.meta["fingerprint_diff"] = {"error": fp1.get("error", "fingerprint after failed")}
                say(f"fingerprint after failed: {fp1.get('error')}")
                return
            worse, info = fp_diff(fp0, fp1)
            ctx.meta["fingerprint_diff"] = {"worse": worse, "info": info, "before": len(fp0["containers"]),
                                            "after": len(fp1["containers"]), "services": fp0["services"]}
            say(f"fingerprint diff: worse={worse or 'none'} info={info or 'none'}")
            if worse:
                if hold_active(node.name):
                    say(f"fingerprint worse but hold active on {node.name}: {worse}")
                    ctx.meta["fingerprint_diff"]["held"] = True
                else:
                    cordon(node.name, "fingerprint worse after job: " + "; ".join(worse)[:200], None, "fingerprint")
                    alert(node.name, worse)

        def s_left():
            left = list_agent(node, job=ctx.jobid)
            ctx.meta["leftover_agent_objects"] = [f"{o['kind']} {o['name']}" for o in left["objects"]]

        def s_live():
            ctx.hb.stop()
            (live_dir() / f"{ctx.jobid}.json").unlink(missing_ok=True)

        def s_scan():
            if (ctx.dir / "out").exists():
                ctx.meta["redactions"] = scan_results(ctx.dir / "out")

        for nm, fn in (("watchdog", s_wd), ("cleanup", s_clean), ("guard", s_guard), ("fingerprint", s_fp),
                       ("leftover", s_left), ("live", s_live), ("scan", s_scan)):
            step(nm, fn)
        cls = st["cls"]
        ACTIVE["ctx"] = None
        ctx.t["total"] = round(now() - t_start + opts._sel_s, 2)
        work = ctx.t.get("prep", 0) + ctx.t.get("run", 0)
        ctx.meta.setdefault("failures", [])
        ctx.meta.update(removed=st["removed"], timings=ctx.t, work_s=round(work, 2), wall_s=ctx.t["total"],
                        wait_s=round(opts._sel_s, 2), overhead_s=round(max(ctx.t["total"] - work - opts._sel_s, 0), 2),
                        result=cls, finished=ts(), exit=EXIT.get(cls, 5), reason=result_reason(cls, ctx))
        ctx.meta.setdefault("leftover_agent_objects", ["unknown: listing failed"])
        step("meta", lambda: write_json(ctx.dir / "meta.json", ctx.meta))
        step("ledger", lambda: ledger("job", node=node.name, job=ctx.jobid, name=job.name, result=cls, timings=ctx.t))
        say(f"RESULT {ctx.jobid} {job.name} on {node.name}: {cls} | total {ctx.t['total']}s work {work:.1f}s "
            f"overhead {ctx.meta['overhead_s']}s | leftover agent objects: {ctx.meta['leftover_agent_objects'] or 'none'}",
            m=True)
        if Logger.fh:
            Logger.fh.close()
        Logger.fh = None
    return cls, ctx


def cmd_run(a) -> int:
    Logger.verbose = bool(getattr(a, "verbose", False) or os.environ.get("ORCH_VERBOSE") == "1")
    retention()
    JOURNAL["job"] = a.job
    job = Job(a.job, a.args)
    a.lease = a.lease or cfg_nodes()["lease"]["default_s"]
    a.heartbeat = a.heartbeat or min(cfg_nodes()["lease"]["heartbeat_s"], max(2, a.lease // 4))
    a._src = None
    if job.get("source") == "git-archive":  # master-local, before any node is reserved; shared by retries
        p = PROJECT["p"]
        if p is not None and not p.git and job["repo"] == "." and not os.environ.get("ORCH_CONFIG"):
            raise OrchError("usage", f"{Path(p.root).as_posix()} is not a git repository, so orch cannot ship the source "
                                     f"of {job.name} (git archive): make it one (git init, git add -A, git commit -m "
                                     f"init), then run orch init")
        repo = legacy_env(f"ORCH_REPO_{job['repo'].upper()}") or cfg_jobs()["repos"][job["repo"]]
        tarp = HOME / "tmp" / f"src-{os.getpid()}-{secrets.token_hex(3)}.tar"
        tarp.parent.mkdir(parents=True, exist_ok=True)
        try:
            a._src = git_source(repo, a.ref, a.wt, tarp, f"src-{job.name}", allow=job.get("source_allow", []))
        except OrchError:
            tarp.unlink(missing_ok=True)
            raise
        a._src["tar"] = str(tarp)
        extra = ""
        if a.wt:
            extra = (f", --wt: {len(a._src['wt_added'])} added {a._src['wt_added'][:5]}, {a._src['wt_modified']} modified, "
                     f"{a._src['wt_deleted']} deleted vs HEAD")
        say(f"source {job['repo']}@{a._src['ref']}: {len(a._src['files'])} files, {a._src['bytes'] // 1024} KiB, "
            f"name scan + content scan clean{extra}")
    try:
        return _run_attempts(job, a)
    finally:
        if a._src:
            Path(a._src["tar"]).unlink(missing_ok=True)


RESULT_KEYS = ("result", "exit", "job", "jobid", "node", "wall_s", "wait_s", "overhead_s", "failures", "reason",
               "results", "log", "sha", "wt")


def src_ids(src: dict | None) -> dict:
    """sha = full commit of the source tree (None for source none); wt = the tree included the working tree (--wt)."""
    return {"sha": (src or {}).get("sha"), "wt": bool((src or {}).get("wt"))}


def result_json(d: dict) -> dict:
    """The contract's run result line: exactly these keys, in this order."""
    out = {k: d.get(k) for k in RESULT_KEYS}
    out["failures"] = out["failures"] or []
    out["wt"] = bool(out["wt"])
    return out


RUN_KEYS = ("node", "jobid", "result", "exit", "wall_s", "wait_s", "overhead_s", "reason")


def record_run(meta: dict) -> None:
    """One finished attempt -> JOURNAL (attempts, the last one's fields, retries) and, in project mode, one line in the
    project's state/runs.jsonl (the deploy gate's evidence). A failed write warns; it never changes the result."""
    att = {k: meta.get(k) for k in RUN_KEYS}
    JOURNAL.setdefault("attempts", []).append(att)
    JOURNAL.update(att, sha=meta.get("sha"), wt=bool(meta.get("wt")), failures=list(meta.get("failures") or [])[:3],
                   retries=len(JOURNAL["attempts"]) - 1)
    p = PROJECT["p"]
    if p is None or os.environ.get("ORCH_CONFIG"):
        return
    rec = {"job": meta.get("job"), "sha": meta.get("sha"), "wt": bool(meta.get("wt")), "node": meta.get("node"),
           "result": meta.get("result"), "exit": meta.get("exit"), "wall_s": meta.get("wall_s"),
           "jobid": meta.get("jobid"), "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        sibling("orch_config").append_run(p, rec)
    except Exception as e:  # noqa: BLE001 - the run result stands; only the deploy gate's evidence is missing
        say(f"warn: run {rec['jobid']} not recorded in {Path(p.store).as_posix()}/state/runs.jsonl ({e}); "
            f"orch deploy --check will not count it")


def _run_attempts(job: Job, a) -> int:
    tried, mem, attempt = [], None, 0
    results = []
    while True:
        attempt += 1
        t0 = now()
        node, p, live, info = select_node(job, a, exclude=tried, mem=mem)
        a._sel_s = now() - t0
        if node is None:
            say(f"no node admitted for {job.name}: {info}", m=True)
            if results:  # a retry found no node: the attempt that did run decides the exit code and the last line
                print(json.dumps(result_json(dict(last, reason="retry not admitted: " + " ".join(str(info).split())))))
                JOURNAL["retry_not_admitted"] = " ".join(str(info).split())[:300]
                return last_rc
            na = {"result": "not-admitted", "exit": NOADMIT, "job": job.name, "wait_s": round(a._sel_s, 2),
                  "reason": " ".join(str(info).split()), **src_ids(getattr(a, "_src", None))}
            print(json.dumps(result_json(na)))
            JOURNAL.update(na, reason=na["reason"][:300])
            return NOADMIT
        say(f"pick {node.name} for {job.name} (attempt {attempt})", m=True)
        if mem:
            job.c["memory"] = mem
        cls, ctx = execute(job, node, p, live, a)
        results.append(ctx.meta)
        record_run(ctx.meta)
        last, last_rc = dict(ctx.meta, results=str(ctx.dir), log=str(ctx.log)), EXIT.get(cls, 5)
        print(json.dumps(result_json(last)))
        if cls == "infra" and attempt == 1 and not a.node and not a.no_retry:
            tried.append(node.name)
            say("infra failure: retrying once on another admitted node")
            continue
        if cls == "oom" and attempt == 1 and not a.no_retry:
            mem = f"{int(parse_mem(job['memory']) * 1.5) >> 20}m"
            say(f"oom: retrying once with 1.5x memory ({mem}) if admitted")
            continue
        return EXIT.get(cls, 5)


# ----------------------------------------------------------------------------- reconcile / gc / stop-all
def reconcile_node(node: Node, la: dict | None = None, quiet=False, force_all=False) -> list[str]:
    la = la or list_agent(node)
    if not la["ok"]:
        if not quiet:
            say(f"{node.name}: listing failed: {la['err']}")
        return []
    idle_h = cfg_nodes()["lease"]["builder_idle_h"]
    bfile = load_json(builder_file(node), {}) or {}
    any_live = any(l["_alive"] and l["node"] == node.name for l in live_jobs())
    builder_idle = False
    rm, skip = [], []
    for o in la["objects"]:
        if not ours(o["name"], o.get("agent", "")):
            continue
        role = o.get("role", "")
        if role in ("builder", "cache"):
            # last use = touch file, else creation time (label lease minus the 2-day builder/cache lease)
            last = max(bfile.get("last_used", 0), o.get("lease", 0) - 2 * 86400)
            if now() - last > idle_h * 3600 and not any_live:
                rm.append(o)
                builder_idle = builder_idle or role == "builder"
            continue
        if role == "probe":
            if o.get("lease", 0) < now() or force_all:
                rm.append(o)
            continue
        live = job_is_live(o.get("job", ""))
        lf = load_json(live_dir() / f"{o.get('job', '')}.json", {}) or {}
        expired = max(o.get("lease", 0), lf.get("lease_until", 0)) < now()  # label lease renewed by heartbeat
        if force_all or (expired and not live):
            rm.append(o)
        else:
            skip.append(f"{o['kind']} {o['name']} ({'live job' if live else 'lease until ' + ts(o.get('lease', 0))})")
    if skip and not quiet:
        for s in skip:
            say(f"{node.name}: keep {s}")
    shared = [o for o in rm if o.get("role") in ("builder", "cache")]
    done = remove_objects(node, [o for o in rm if o not in shared], "reconcile", job="reconcile")
    if shared:
        with FileLock():  # admission writes live files under the same lock: no job can start using them meanwhile
            if not any(l["_alive"] and l["node"] == node.name for l in live_jobs()):
                done += remove_objects(node, shared, "reconcile: idle > 24 h", job="reconcile")
                if builder_idle:
                    cmd(["docker", "buildx", "rm", builder_ref(node)], job="reconcile", timeout=60)
    return done


def cmd_reconcile(a) -> int:
    nodes = nodes_all()
    if a.node:
        if a.node not in nodes_all(True):
            print(f"unknown node {a.node}")
            return 2
        nodes = {a.node: nodes_all(True)[a.node]}
    for n in nodes.values():
        la = list_agent(n, job="reconcile")
        if not la["ok"]:
            print(f"{n.name}: unreachable/listing failed: {la['err']}")
            continue
        for c in la["collisions"]:
            print(f"{n.name}: COLLISION (never touched): {c}")
        done = reconcile_node(n, la)
        print(f"{n.name}: removed {len(done)}: {', '.join(done) or '-'}")
    # Stale local live files (dead pid, lease expired)
    for l in live_jobs():
        if not l["_alive"] and l.get("lease_until", 0) < now():
            (live_dir() / f"{l['jobid']}.json").unlink(missing_ok=True)
    return 0


def kill_local_jobs() -> tuple[list[str], list[str]]:
    """Kill every other live orch run. Its live file is unlinked only once the process is confirmed gone.
    Returns (killed, failed)."""
    out, failed = [], []
    for l in live_jobs():
        if l["_alive"]:
            if l["pid"] == os.getpid():
                continue
            err = ""
            if IS_WIN:
                r = cmd(["taskkill", "/PID", str(l["pid"]), "/T", "/F"], job=l["jobid"], timeout=30)
                err = "" if r.ok else r.last_err()
            else:
                try:
                    os.kill(l["pid"], signal.SIGTERM)
                except OSError as e:
                    err = str(e)
            for _ in range(40):
                if not same_proc(l):
                    break
                time.sleep(0.25)
            else:
                failed.append(f"{l['jobid']} (pid {l['pid']}): {err or 'still alive after 10s'}")
                continue
            out.append(f"{l['jobid']} (pid {l['pid']})")
        (live_dir() / f"{l['jobid']}.json").unlink(missing_ok=True)
    return out, failed


def cmd_stop_all(a) -> int:
    killed, failed = kill_local_jobs()
    print(f"killed local orch jobs: {', '.join(killed) or 'none'}")
    if failed:
        print(f"kill FAILED (live file kept; retry stop-all or kill by hand): {', '.join(failed)}")
    for n in cleanable_nodes().values():
        la = list_agent(n, job="stop-all")
        if not la["ok"]:
            print(f"{n.name}: listing failed: {la['err']} (reconcile will finish later)")
            continue
        keep = {f.split()[0] for f in failed}  # jobs whose kill failed may still run: leave their objects alone
        objs = [o for o in la["objects"] if o.get("role") not in ("builder", "cache") and o.get("job") not in keep]
        done = remove_objects(n, objs, "stop-all", job="stop-all")
        print(f"{n.name}: removed {len(done)}: {', '.join(done) or '-'} (builder/cache kept; gc --session-end wipes them)")
    return 5 if failed else 0


def gc_node(n: Node) -> dict:
    """Remove orch's objects on one node: {"node", "removed", "kept", "error", "collisions", "live"}; live = the
    live jobs (any session) whose objects, and then the shared builder/caches/base images, were kept."""
    res = {"node": n.name, "removed": [], "kept": [], "error": None, "live": []}
    la = list_agent(n, job="gc")
    if not la["ok"]:
        res["error"] = la["err"]
        return res
    res["collisions"] = la["collisions"]
    # Liveness-safe: never touch a live job's objects (any session); shared builder/caches/base images only
    # when no job is live on this node, decided and done under the master lock (admission takes the same lock).
    jobobjs = [o for o in la["objects"] if o.get("role") not in ("builder", "cache")]
    live_objs = [o for o in jobobjs if job_is_live(o.get("job", ""))]
    for o in live_objs:
        res["kept"].append(f"{o['kind']} {o['name']} (job {o.get('job')} is live)")
    res["live"] = sorted({str(o.get("job")) for o in live_objs})
    res["removed"] += remove_objects(n, [o for o in jobobjs if o not in live_objs], "gc --session-end", job="gc")
    with FileLock(timeout=300):
        busy = [l["jobid"] for l in live_jobs() if l["_alive"] and l["node"] == n.name]
        if busy:
            res["live"] = sorted(set(res["live"]) | set(busy))
            res["kept"].append(f"builder/caches/base images (live jobs on {n.name}: {', '.join(busy)})")
            return res
        shared = [o for o in la["objects"] if o.get("role") in ("builder", "cache")]
        res["removed"] += remove_objects(n, shared, "gc --session-end", job="gc")
        cmd(["docker", "buildx", "rm", builder_ref(n)], job="gc", timeout=60)
        builder_file(n).unlink(missing_ok=True)
        _gc_base_images(n, res)
    return res


def _gc_base_images(n: Node, res: dict) -> None:
    """Base images we pulled that did not exist before, carry no other tag, and no non-agent container uses."""
    ek = engine_key(n)
    bi = state_get()["base_images"].get(ek, {})
    for ref, info in list(bi.items()):
        if info.get("existed_before"):
            continue
        u = n.docker(["ps", "-a", "--filter", f"ancestor={ref}", "--format", "{{.Names}}\t" + LT],
                     timeout=60, job="gc")
        users = [l for l in u.out.splitlines() if l.strip() and l.split("\t")[-1] != "1"]
        if not u.ok:
            res["kept"].append(f"{ref} (could not list its users: {u.last_err()})")
            continue
        if users:
            res["kept"].append(f"{ref} (used by {users[0].split()[0]})")
            continue
        ex = n.docker(["image", "inspect", "--format", "{{json .RepoTags}}", ref], timeout=60, job="gc")
        tags = []
        try:
            tags = json.loads(ex.out.strip() or "[]") or []
        except json.JSONDecodeError:
            pass
        named = [t for t in tags if "@" not in t]
        if ex.ok and named:
            res["kept"].append(f"{ref} (also tagged {named[0]}: not ours alone)")
            continue
        if ex.ok:
            ledger("intent-remove", node=n.name, kind="base-image", name=ref, why="gc: pulled by agent")
            r = n.docker(["rmi", ref], timeout=120, job="gc")
            if not r.ok:
                res["kept"].append(f"{ref} ({r.last_err()})")
                continue
            ledger("removed", node=n.name, kind="base-image", name=ref, why="gc")
            res["removed"].append(f"base-image {ref}")
        state_update(lambda s: s["base_images"].get(ek, {}).pop(ref, None))
    state_update(lambda s: s["subnets"].pop(n.name, None))
    return res


def select_gc_tmp(entries: list[dict], live: list[dict], t_now: float) -> list[str]:
    """Names under HOME/tmp that gc --session-end removes, only when no job is live: entries older than 30 min,
    or whose creating pid (src-<pid>-*, idx-<pid>-*) is dead. A fresh tar of an in-flight run is never touched."""
    if any(l.get("_alive") for l in live):
        return []
    out = []
    for e in entries:
        m = re.match(r"(?:src|idx)-(\d+)-", e["name"])
        if t_now - e.get("mtime", t_now) > 1800 or (m and not pid_alive(int(m.group(1)))):
            out.append(e["name"])
    return out


def has_transport(n: Node) -> bool:
    """The node still names a docker transport: a docker context, or ssh for ssh-docker (Linux)."""
    if n.transport == "ssh-docker":
        return n.linux and n.ssh != "local" and bool(n.c.get("ssh_args") or n.ssh)
    return bool(n.ctx)


def cleanable_nodes() -> dict[str, Node]:
    """gc and stop-all: every node orch may have left objects on, also a disabled one (a helper whose job scope is
    none, or whose last probe failed) as long as it still has a docker transport."""
    return {k: n for k, n in nodes_all(True).items() if n.c.get("enabled") or has_transport(n)}


def _gc_lines(n: Node, r: dict):
    """Print what gc did on one node; returns the agent objects still there (None when listing them failed)."""
    print(f"{n.name}: removed {len(r['removed'])}: {', '.join(r['removed']) or '-'}")
    if r["kept"]:
        print(f"{n.name}: kept {', '.join(r['kept'])}")
    for c in r.get("collisions", []):
        print(f"{n.name}: COLLISION (never touched): {c}")
    left = list_agent(n, job="gc")
    if not left["ok"]:
        print(f"{n.name}: agent objects left: unknown (listing failed: {left['err']})")
        return None
    print(f"{n.name}: agent objects left: {len(left['objects'])}")
    return [f"{o['kind']} {o['name']}" for o in left["objects"]]


GC_NODE_ENV = "ORCH_GC_NODE_JSON"


def _gc_env_node(name: str, known=None):
    """A node of a legacy (ORCH_CONFIG) test setup that is gone from its nodes.json, from ORCH_GC_NODE_JSON =
    {"name": name, "node": <its node>}; None when unset. Honoured only in legacy mode with ORCH_TEST_MODE=1 (the test
    suites and dev harnesses), validated like a nodes.json entry, and never a production host of the guard view
    (resolved as for runtime verbs). In project mode it never supplies a node (review round 2 Q4): orch init cleans a
    helper while it is still in the approved setup, so gc takes it from orch's own compile of that setup, and a name
    outside it stays unknown. Set anywhere else -> usage error (exit 2); known = that compiled node, if any."""
    raw = os.environ.get(GC_NODE_ENV, "").strip()
    if not raw:
        return None
    if not os.environ.get("ORCH_CONFIG"):
        where = "has no docker transport there" if known is not None else "is not in it"
        raise OrchError("usage", f"node {name} {where}: orch cleans only the helpers of the approved setup, as orch "
                                 f"compiles them, and {GC_NODE_ENV} never adds or changes one (orch init cleans a "
                                 f"helper itself before it leaves the setup)")
    if os.environ.get("ORCH_TEST_MODE") != "1":
        raise OrchError("usage", f"{GC_NODE_ENV} is test-only: list node {name} in the ORCH_CONFIG nodes.json instead")
    shape = f'{GC_NODE_ENV}={{"name": "{name}", "node": {{...its compiled node...}}}}'
    try:
        d = json.loads(raw)
    except ValueError as e:
        raise OrchError("usage", f"{GC_NODE_ENV} is not JSON ({e}): pass {shape}") from None
    if not (isinstance(d, dict) and d.get("name") == name and isinstance(d.get("node"), dict)):
        raise OrchError("usage", f"{GC_NODE_ENV} does not describe node {name}: pass {shape}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
        raise OrchError("usage", f"bad node name {name!r}: letters, digits, '-', '_' and '.' only")
    validate_nodes(dict(cfg_nodes(), nodes={name: d["node"]}))
    bad, err = legacy_prod_nodes({name: d["node"]}, resolve=True)
    if err or bad:
        raise OrchError("usage", f"{GC_NODE_ENV}: node {name} {bad.get(name) or err}: orch never cleans it from here")
    return Node(name, d["node"])


def gc_one(name: str) -> int:
    """orch gc --session-end --node NAME: clean that one node now (orch init runs it before a helper leaves the setup
    or loses its helper role, while it is still in the approved setup). Only a node of the setup (_gc_env_node: a
    legacy test setup may name a gone one). Exit 0 = clean, 5 = not reachable or orch objects still there, 75 = a live
    job is on it (its objects and the shared builder/caches are kept), 2 = unknown node."""
    known = nodes_all(True)
    n = known.get(name)
    if n is None or not has_transport(n):  # gone from the setup, or kept there without a transport
        n = _gc_env_node(name, n) or n
    if n is None:
        print(f"unknown node {name} (known: {', '.join(sorted(known)) or 'none'}): orch gc --node cleans a node of the "
              f"setup; orch init cleans a helper itself before it leaves the setup")
        return 2
    again = f"run orch gc --session-end --node {n.name} again"
    r = gc_node(n)
    if r["error"]:
        print(f"{n.name}: NOT cleaned ({r['error']}); {again} when it is reachable")
        return 5
    left = _gc_lines(n, r)
    if r["live"]:
        print(f"{n.name}: live job(s) {', '.join(r['live'])} still run there; {again} after they end")
        return NOADMIT
    if left is None:
        print(f"{n.name}: NOT confirmed clean; {again}")
        return 5
    if left:
        print(f"{n.name}: NOT clean, still there: {', '.join(left[:10])}; {again}")
        return 5
    return 0


def cmd_gc(a) -> int:
    if not a.session_end:
        print("usage: orch gc --session-end [--node NAME]")
        return 2
    if getattr(a, "node", None):
        return gc_one(a.node)
    rc = 0  # gc never kills anything: stop-all is the only kill switch
    for n in cleanable_nodes().values():
        r = gc_node(n)
        if r["error"]:
            rc = 5
            print(f"{n.name}: NOT cleaned ({r['error']}); run gc again when it is reachable")
            continue
        _gc_lines(n, r)
    lj = live_jobs()
    tmpd = HOME / "tmp"
    ents = []
    if tmpd.is_dir():
        for e in tmpd.iterdir():
            try:
                ents.append({"name": e.name, "mtime": e.stat().st_mtime})
            except OSError:
                pass
    for nm in select_gc_tmp(ents, lj, now()):
        e = tmpd / nm
        if e.is_dir():
            shutil.rmtree(e, ignore_errors=True)
        else:
            e.unlink(missing_ok=True)
    if any(l["_alive"] for l in lj):
        rc = rc or 0
        print("live jobs remain (other sessions): their objects, source tars and the shared builder/caches were kept")
    if rc == 0:
        (HOME / "session-id").unlink(missing_ok=True)
        ledger("session-end")
    retention()
    return rc


# ----------------------------------------------------------------------------- read-only verbs
def cmd_nodes(a) -> int:
    print(config_line(), file=sys.stderr if a.json else sys.stdout)
    ns = nodes_all(include_disabled=True)
    if not any(n.c.get("enabled") for n in ns.values()):
        print(f"no enabled nodes in {config_sources()['nodes.json']}: nothing can run until one is enabled", file=sys.stderr)
        return 2
    full = bool(getattr(a, "all", False))
    with cf.ThreadPoolExecutor(max(len(ns), 1)) as ex:
        fut = {k: ex.submit(probe, n) if n.c.get("enabled") else None for k, n in ns.items()}
    st = state_get()
    rows, brief = [], []
    for k, n in ns.items():
        f = fut[k]
        p = f.result() if f else {"reach": False, "reason": "disabled: " + n.c.get("note", "")}
        cd = st["cordon"].get(k)
        cds = ""
        if cd and (cd.get("until") is None or cd["until"] > now()):
            cds = f"CORDON({cd.get('reason', '')[:40]})"
        hold = "HOLD" if st["hold"].get(k, 0) > now() else ""
        if p.get("docker"):
            adm = admit({"memory": "1g"}, n, p)
            fm = lambda k_: fmt_mem(p[k_]) if _num(p.get(k_)) else "?"
            rows.append([k, n.os, "up", p.get("engine", ""), f"{fm('mem_avail')}/{fm('mem_total')}",
                         "na" if p.get("psi") is None else str(p["psi"]), str(p.get("swap_pages_s")), f"{p.get('iowait')}%",
                         f"{p.get('load1')}/{p.get('cores')}", fm("disk_free"),
                         str(len(p.get("heavy_jobs", []))), "yes" if p.get("builder") else "no",
                         ("ok" if adm["ok"] else "NO") + (" " + cds if cds else "") + (" " + hold if hold else ""),
                         f"{p.get('probe_s')}s"])
            psi = f"psi={p['psi']}" if p.get("psi") is not None else f"iow={p.get('iowait')}%"
            brief.append(f"{k} ok docker {fm('mem_avail')}/{fm('mem_total')} {psi} "
                         f"load={p.get('load1')}/{p.get('cores')} {' '.join(x for x in (cds, hold) if x) or '-'}")
            if a.json:
                p["admission_1g"] = adm
        else:
            rows.append([k, n.os, "DOWN", "", "", "", "", "", "", "", "", "", (p.get("reason") or "")[:70] + (" " + cds if cds else ""), ""])
            brief.append(f"{k} down {(p.get('reason') or '-')[:70]} {' '.join(x for x in (cds, hold) if x) or '-'}")
        if a.json:
            print(json.dumps(p, default=str))
    if not a.json and not full:
        print("\n".join(brief))
    elif not a.json:
        hdr = ["node", "os", "state", "engine", "memavail/total", "psi", "swap/s", "iowait", "load/cores", "disk free",
               "agent heavy", "builder", "admission(1G job)", "probe"]
        w = [max(len(str(r[i])) for r in rows + [hdr]) for i in range(len(hdr))]
        for r in [hdr] + rows:
            print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip())
        print("tags: " + "; ".join(f"{k}={','.join(n.c.get('tags', []))}" for k, n in ns.items()))
    return 0


def cmd_plan(a) -> int:
    print(config_line())
    job = Job(a.job, [])
    ns = nodes_all(include_disabled=True)
    ref = images_cfg().get(job.get("image")) if job.get("image") else None
    full = bool(getattr(a, "all", False))
    say_ = print if full else (lambda *x, **k: None)
    say_(f"plan {job.name}: kind={job['kind']} heavy={job.get('heavy')} memory={job.get('memory')} "
         f"cpus={job.get('cpus', '-')} timeout={job.get('timeout')}s requires={job.get('requires')} image={ref}")
    elig, skips, jn = {}, [], {}
    JOURNAL.update(job=job.name, nodes=jn)
    for k, n in ns.items():
        hf = hard_filter(job.name, job.c, n)
        if hf:
            jn[k] = {"ok": False, "reason": "; ".join(hf)[:200]}
            say_(f"  {k}: INELIGIBLE: {'; '.join(hf)}")
            if not full:
                print(f"{k} skip - {'; '.join(hf)}")
            skips.append(f"{k}: {hf[0]}")
        else:
            elig[k] = n
    with cf.ThreadPoolExecutor(max(len(elig), 1)) as ex:
        fut = {k: ex.submit(probe, n, [ref] if ref else []) for k, n in elig.items()}
    best = None
    A = cfg_nodes()["admission"]
    for k, n in elig.items():
        p = fut[k].result()
        ad = admit(job.c, n, p)
        busy = set(p.get("heavy_jobs", [])) | {l["jobid"] for l in live_jobs() if l["node"] == k and l.get("heavy") and l["_alive"]}
        cap = node_max_heavy(k, A)
        if job.get("heavy") and len(busy) >= cap:
            ad["ok"] = False
            ad["checks"].append({"check": "capacity", "ok": False, "detail": f"agent heavy jobs {len(busy)} >= max {cap}"})
        elif job.get("heavy"):
            ad["checks"].append({"check": "capacity", "ok": True, "detail": f"agent heavy jobs {len(busy)} < max {cap}"})
        say_(f"  {k}: {'ADMITTED' if ad['ok'] else 'NOT ADMITTED'} score={'-' if ad['score'] is None else ad['score']}")
        for c in ad["checks"]:
            say_(f"      [{'ok' if c['ok'] else 'NO'}] {c['check']}: {c['detail']}")
        if ad.get("breakdown"):
            b = ad["breakdown"]
            say_(f"      score = {b['w_mem']} x mem_headroom {b['mem_headroom_ratio']} + {b['w_load']} x load_free "
                 f"{b['load_free']} + {b['w_aff']} x affinity {b['affinity']} (image_present {b['image_present']}, "
                 f"warm_cache {b['warm_cache']}) = {ad['score']}")
        bad = [f"{c['check']}: {c['detail']}" for c in ad["checks"] if not c["ok"]]
        jn[k] = {"ok": ad["ok"], "score": ad["score"]}
        if not ad["ok"]:
            jn[k]["reason"] = ("; ".join(bad) or "not admitted")[:200]
        if not full:
            sc = "-" if ad["score"] is None else ad["score"]  # no score when the node could not be probed
            print(f"{k} {'admit' if ad['ok'] else 'skip'} {sc} {'ok' if ad['ok'] else '; '.join(bad) or 'not admitted'}")
        if not ad["ok"]:
            skips.append(f"{k}: {bad[0] if bad else 'not admitted'}")
        if ad["ok"] and (best is None or ad["score"] > best[1]):
            best = (k, ad["score"])
    say_(f"  => {'dispatch to ' + best[0] if best else 'nothing admitted: orch run would wait (--wait) then exit 75'}")
    print(f"pick {best[0]}" if best else f"pick none: {'; '.join(skips) or 'no nodes configured'}")
    JOURNAL.update(pick=best[0] if best else None, result="pick" if best else "not-admitted")
    return 0 if best else NOADMIT


def cmd_status(a) -> int:
    st = state_get()
    print(f"session {session_id()} owner {OWNER} home {HOME}")
    lj = live_jobs()
    print("live jobs:" + ("" if lj else " none"))
    for l in lj:
        print(f"  {l['jobid']} {l.get('job')} on {l.get('node')} stage={l.get('stage')} pid={l.get('pid')} "
              f"{'alive' if l['_alive'] else 'DEAD'} lease_until={ts(l.get('lease_until', 0))}")
    for k, v in st["cordon"].items():
        if v.get("until") is None or v["until"] > now():
            print(f"cordon {k}: {v.get('reason')} ({'until uncordon' if v.get('until') is None else 'until ' + ts(v['until'])}) by {v.get('by')}")
    for k, v in st["hold"].items():
        if v > now():
            print(f"hold {k}: until {ts(v)}")
    res = sorted((HOME / "results").glob("*/meta.json"), key=lambda p: p.stat().st_mtime)[-a.last:] if (HOME / "results").exists() else []
    print("recent jobs:")
    for p in res:
        m = load_json(p, {})
        m = m if isinstance(m, dict) else {}
        tm = m.get("timings")
        tot = tm.get("total") if isinstance(tm, dict) else None
        print(f"  {m.get('jobid') or p.parent.name} {str(m.get('job') or '?'):<16} {str(m.get('node') or '?'):<4} "
              f"{str(m.get('result') or '?'):<9} total={tot}s "
              f"work={m.get('work_s')}s overhead={m.get('overhead_s')}s ref={m.get('ref', '-')}")
    if not a.local:
        ns = list(nodes_all().values())
        with cf.ThreadPoolExecutor(max(len(ns), 1)) as ex:  # read-only listings only
            las = list(ex.map(lambda n: list_agent(n, job="status"), ns))
        for n, la in zip(ns, las):
            if not la["ok"]:
                print(f"{n.name}: listing failed: {la['err']}")
                continue
            objs = ", ".join(f"{o['kind']}:{o['name']}" for o in la["objects"]) or "none"
            print(f"{n.name}: agent objects: {objs}")
            for c in la["collisions"]:
                print(f"{n.name}: COLLISION: {c}")
    return 0


def _human(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for u in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or u == "TB":
            return f"{n:.0f}{u}" if u in ("B", "kB") else f"{n:.1f}{u}"
        n /= 1000


def _size_bytes(s) -> float:
    """Docker size strings ('1.3GB', '415MB', '0B', '12.5kB') -> bytes (decimal units, as docker prints them)."""
    m = re.match(r"\s*([0-9.]+)\s*([kKMGT]?i?B)?", str(s or ""))
    if not m:
        return 0.0
    mult = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KiB": 1024, "MiB": 1 << 20,
            "GiB": 1 << 30, "TiB": 1 << 40}.get(m.group(2) or "B", 1)
    return float(m.group(1)) * mult


def cmd_inventory(a) -> int:
    """Read-only: what is taking space on a node, with agent ownership marked. Default brief (docker system df
    totals + agent-owned items); --all adds the per-item docker system df -v tables. Removes nothing. Takes the
    foreign fingerprint before and after (after always, even on error); exit 5 if it got worse or failed."""
    ns = nodes_all(True)
    if a.node not in ns:
        print(f"unknown node {a.node}")
        return 2
    n = ns[a.node]
    fp0 = fingerprint(n, job="inventory", lat_samples=0)
    try:
        rc = _inv_brief(n, a) if not a.all else _inv_full(n, a)
    finally:
        fp1 = fingerprint(n, job="inventory", lat_samples=0)
    ok0, ok1 = bool(fp0.get("ok")), bool(fp1.get("ok"))
    worse, info = fp_diff(fp0, fp1) if ok0 and ok1 else (
        [f"fingerprint failed: {(fp1 if ok0 else fp0).get('error') or 'unknown error'}"], [])
    c0, c1 = len(fp0.get("containers") or {}), len(fp1.get("containers") or {})
    if a.json:  # stdout stays one JSON object; the fingerprint verdict is a key
        print(json.dumps(dict(getattr(a, "_inv", None) or {"node": n.name, "error": "inventory failed"},
                              fingerprint={"before": c0, "after": c1, "worse": worse, "info": info}), default=str))
    else:
        print(f"\nfingerprint before/after inventory ({c0} -> {c1} foreign containers): "
              f"worse={worse or 'none'} info={info or 'none'}")
    return rc or (5 if worse else 0)


def _inv_brief(n: Node, a) -> int:
    r0 = n.docker(["system", "df"], timeout=300, job="inventory")
    if not r0.ok:
        print(f"{n.name}: docker system df failed: {r0.last_err()}", file=sys.stderr if a.json else sys.stdout)
        return 5
    la = list_agent(n, job="inventory")
    ag = [o for o in la["objects"] if ours(o["name"], o.get("agent", ""))]
    if a.json:
        a._inv = {"node": n.name, "df": r0.out.rstrip().splitlines(), "listing_ok": la["ok"],
                  "agent_objects": [{"kind": o["kind"], "name": o["name"]} for o in ag]}
    else:
        print(f"== {n.name}: docker system df (read-only; --all adds the per-item -v tables)\n" + r0.out.rstrip())
        if not la["ok"]:
            print(f"agent-owned items on {n.name}: listing failed: {la['err']}")
        else:
            print(f"agent-owned items on {n.name}: {len(ag)} (orch removes only these, via reconcile / gc --session-end; "
                  f"everything else needs the user's decision)")
            for o in ag:
                print(f"  {o['kind']:<10} {o['name']}")
    return 0 if la["ok"] else 5


def _inv_full(n: Node, a) -> int:
    r = n.docker(["system", "df", "-v", "--format", "{{json .}}"], timeout=600, job="inventory")
    if not r.ok:
        print(f"{n.name}: docker system df failed: {r.last_err()}", file=sys.stderr if a.json else sys.stdout)
        return 5
    try:
        df = json.loads(r.out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        print(f"{n.name}: could not parse docker system df output", file=sys.stderr if a.json else sys.stdout)
        return 5
    base = state_get()["base_images"].get(engine_key(n), {})
    pulled = {canon_ref(k).split("@")[-1] for k, v in base.items() if not v.get("existed_before")}

    def lab(s, k=L_AGENT):
        for kv in str(s or "").split(","):
            if kv.startswith(k + "="):
                return kv.split("=", 1)[1]
        return ""

    def owner_c(name, labels):
        return "AGENT" if ours(name, lab(labels)) else ""

    imgs = []
    for i in df.get("Images") or []:
        ref = f"{i.get('Repository')}:{i.get('Tag')}"
        mark = "AGENT" if ref.startswith(PREFIX) else ""
        if not mark and any(str(i.get("Digest", "")).endswith(d.replace("sha256:", "")) or d in str(i.get("Digest", "")) for d in pulled):
            mark = "AGENT-PULLED"
        imgs.append((_size_bytes(i.get("Size")), ref, i.get("ID", "")[:19], i.get("Containers", ""), i.get("CreatedSince", ""),
                     i.get("SharedSize", ""), i.get("UniqueSize", ""), mark))
    ctrs = [(_size_bytes(c.get("Size")), c.get("Names", ""), c.get("Image", "")[:40], c.get("Status", ""),
             c.get("Size", ""), owner_c(c.get("Names", ""), c.get("Labels", ""))) for c in df.get("Containers") or []]
    vols = [(_size_bytes(v.get("Size")), v.get("Name", "")[:64], v.get("Links", ""), v.get("Size", ""),
             owner_c(v.get("Name", ""), v.get("Labels", ""))) for v in df.get("Volumes") or []]
    bc = df.get("BuildCache") or []
    bc_total = sum(_size_bytes(b.get("Size")) for b in bc)
    bc_shared = sum(_size_bytes(b.get("Size")) for b in bc if b.get("Shared") in (True, "true"))
    if a.json:
        a._inv = {"node": n.name, "images": imgs, "containers": ctrs, "volumes": vols,
                  "build_cache": {"entries": len(bc), "bytes": bc_total}}
    else:
        top = a.top
        print(f"== {n.name}: docker system df -v (read-only; AGENT = {L_AGENT}=1 label + {PREFIX} name; "
              f"AGENT-PULLED = base image orch pulled, removed at gc --session-end)")
        print(f"sums of the -v rows: images {len(imgs)} = {_human(sum(x[0] for x in imgs))} (shared layers counted per image; docker's own total counts them once), "
              f"containers {len(ctrs)} = {_human(sum(x[0] for x in ctrs))}, volumes {len(vols)} = {_human(sum(x[0] for x in vols))}, "
              f"build cache {len(bc)} entries = {_human(bc_total)} (default builder; never touched by orch)")
        print(f"\nIMAGES by size{'' if top is None else f' (top {top})'}:")
        print(f"  {'SIZE':>9}  {'UNIQUE':>9}  {'CTRS':>4}  {'CREATED':<14} {'OWNER':<12} IMAGE")
        for s_, ref, iid, nc, cs, sh, un, mark in sorted(imgs, reverse=True)[:top]:
            print(f"  {_human(s_):>9}  {str(un):>9}  {str(nc):>4}  {str(cs)[:14]:<14} {mark or '-':<12} {ref} ({iid})")
        print(f"\nCONTAINERS by writable-layer size{'' if top is None else f' (top {top})'}:")
        for s_, nm, im, stt, sz, mark in sorted(ctrs, reverse=True)[:top]:
            print(f"  {sz[:30]:>30}  {mark or '-':<6} {nm:<40} {stt[:28]:<28} {im}")
        print(f"\nVOLUMES by size{'' if top is None else f' (top {top})'}:")
        for s_, nm, links, sz, mark in sorted(vols, reverse=True)[:top]:
            print(f"  {sz:>9}  links={links!s:<3} {mark or '-':<6} {nm}")
        ag = [x for x in imgs if x[7]] + [x for x in ctrs if x[5]] + [x for x in vols if x[4]]
        print(f"\nagent-owned items on {n.name}: {len(ag)} (orch removes only these, via reconcile / gc --session-end; "
              f"everything else needs the user's decision)")
        for x in imgs:
            if x[7]:
                print(f"  image      {_human(x[0]):>9}  {x[7]:<12} {x[1]}")
        for x in ctrs:
            if x[5]:
                print(f"  container  {x[4][:20]:>9}  {x[5]:<12} {x[1]} ({x[3][:30]})")
        for x in vols:
            if x[4]:
                print(f"  volume     {x[3]:>9}  {x[4]:<12} {x[1]}")
    return 0


def cmd_logs(a) -> int:
    p = HOME / "node-logs" / f"{a.jobid}.log"
    if not p.exists():
        print(f"no log for {a.jobid}")
        return 2
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    m = load_json(HOME / "results" / a.jobid / "meta.json", {})
    m = m if isinstance(m, dict) else {}
    print(f"{a.jobid} job={m.get('job', '?')} node={m.get('node', '?')} result={m.get('result', '?')} "
          f"exit={m.get('exit', '?')} wall={m.get('wall_s', '?')}s lines={len(lines)} log={p}")
    if getattr(a, "all", False):
        print("\n".join(lines))
        return 0
    fails = m.get("failures") if isinstance(m.get("failures"), list) else []
    if not fails:
        pats = (cfg_jobs()["jobs"].get(m.get("job")) or {}).get("fail_patterns", [])
        fails = [l for l in lines if any(re.search(pt, l) for pt in pats)]
    fails = [str(f)[:200] for f in fails[:20]]
    if fails:
        print("failures:\n" + "\n".join("  " + f for f in fails))
    n = a.tail if a.tail and a.tail > 0 else 80
    print(f"--- last {min(n, len(lines))} of {len(lines)} lines (--all for the full log)")
    print("\n".join(lines[-n:]))
    return 0


def cmd_cordon(a) -> int:
    if a.node not in nodes_all(True):
        print(f"unknown node {a.node}")
        return 2
    cordon(a.node, a.reason or "manual", a.ttl / 60 if a.ttl else None, "cli")
    print(f"{a.node} cordoned: {a.reason or 'manual'}" + (f" for {a.ttl}s" if a.ttl else " until uncordon"))
    return 0


def cmd_uncordon(a) -> int:
    if a.node not in nodes_all(True):
        print(f"unknown node {a.node}; known: {', '.join(nodes_all(True))}")
        return 2

    def f(s):
        return s["cordon"].pop(a.node, None)
    old = state_update(f)
    ledger("uncordon", node=a.node, previous=old)
    print(f"{a.node} uncordoned (was: {old.get('reason') if old else 'not cordoned'})")
    return 0


def cmd_hold(a) -> int:
    if a.node not in nodes_all(True):
        print(f"unknown node {a.node}; known: {', '.join(nodes_all(True))}")
        return 2
    if not (0 < a.minutes < float("inf")):
        print(f"hold: minutes must be a number > 0 (got {a.minutes})")
        return 2
    until = now() + a.minutes * 60
    state_update(lambda s: s["hold"].__setitem__(a.node, until))
    ledger("hold", node=a.node, until=until)
    print(f"{a.node}: fingerprint diff suspended until {ts(until)}")
    return 0


# ----------------------------------------------------------------------------- ci
CI_GROUPS = (("lint", "unit"), ("build", "image"), ("e2e",))  # in this order; the jobs of one group run in parallel
CI_STAGES = tuple(s_ for g in CI_GROUPS for s_ in g)
SEVERITY = (6, 5, 2, NOADMIT, 3, 4, 1, 0)  # worst first: the ci exit is the worst child exit
NEXT_ON = {1: "Development", 3: "Development", 4: "Investigation", 2: "fix the call", 5: "stop", 6: "stop", NOADMIT: "stop"}


def ci_next(code: int, stage: str | None) -> str:
    """The phase after a red job (SKILL.md Results table): NEXT_ON by exit, except a failing e2e suite (exit 1), which
    goes to Investigation (reproduce, root cause), not straight back to Development (review round 2 Q17)."""
    return "Investigation" if code == 1 and stage == "e2e" else NEXT_ON.get(code, "stop")


def worst_exit(codes) -> int:
    """Worst exit by SEVERITY (6 > 5 > 2 > 75 > 3 > 4 > 1 > 0); a code orch never uses counts as infra (5)."""
    codes = [c if c in SEVERITY else 5 for c in codes]
    return min(codes, key=SEVERITY.index) if codes else 0


def ci_child_argv(job: str, a) -> list[str]:
    """One child `orch run <job>`: same interpreter, -u. ORCH_CI_CHILD (test-only) replaces the "<python> -u orch.py
    run" prefix: a JSON array of argv strings, or a POSIX shell-quoted string; honoured only with ORCH_CONFIG."""
    pre = legacy_env("ORCH_CI_CHILD").strip()
    try:
        base = (json.loads(pre) if pre.startswith("[") else shlex.split(pre)) if pre else \
            [sys.executable, "-u", str(Path(__file__).resolve()), "run"]
    except ValueError as e:
        raise OrchError("usage", f"ORCH_CI_CHILD must be a JSON array of strings or a shell-quoted command: {e}")
    argv = [str(x) for x in base] + [job, "--wait", str(a.wait)]
    if a.ref:
        argv += ["--ref", a.ref]
    if a.wt:
        argv.append("--wt")
    return argv


def ci_result(job: str, rc: int, out: str, err: str) -> dict:
    """A child's final JSON line -> its result. No line, or a line that disagrees with the process exit code (a crash
    after printing, an unknown code) -> infra (exit 5) with the reason."""
    d = _last_json(out)
    if not d:
        tail = (err.strip().splitlines() or [""])[-1].strip()[:200]
        return {"job": job, "result": "infra", "exit": 5, "reason": f"orch run printed no result line (exit {rc}) {tail}".strip()}
    if d.get("exit") != rc or rc not in SEVERITY:
        return dict(d, job=job, result="infra", exit=5,
                    reason=f"orch run exit {rc} does not match its result line ({d.get('result')}, exit {d.get('exit')})")
    return dict(d, job=job)


def _ci_stop(p) -> None:
    """Forward one stop to a child (its own process group): SIGTERM / CTRL_BREAK -> it cleans up and exits."""
    try:
        p.send_signal(signal.CTRL_BREAK_EVENT if IS_WIN else signal.SIGTERM)
    except (OSError, ValueError):
        p.terminate()


def ci_run_group(names: list[str], a) -> list[tuple]:
    """Start one child per job at once, wait for all. Returns [(job, rc, stdout, stderr)] in input order."""
    procs, outs, ths = [], {}, []
    flags = 0x00000200 if IS_WIN else 0  # CREATE_NEW_PROCESS_GROUP: a console Ctrl-C reaches only ci, which forwards it

    def wait(n, p):
        o, e = p.communicate()
        outs[n] = (p.returncode, dec(o), dec(e))
    try:
        for n in names:
            argv = ci_child_argv(n, a)
            audit(None, n, argv, "ci child")
            try:
                p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=dict(os.environ, MSYS_NO_PATHCONV="1"), creationflags=flags,
                                     start_new_session=not IS_WIN)
            except OSError as e:
                outs[n] = (5, "", f"could not start orch run: {e}")
                continue
            procs.append((n, p))
            ths.append(threading.Thread(target=wait, args=(n, p), daemon=True))
            ths[-1].start()
        for t in ths:
            while t.is_alive():
                t.join(0.5)  # a timed join keeps Ctrl-C / SIGTERM deliverable (Windows)
    except KeyboardInterrupt:
        say(f"interrupted: stopping {len(procs)} ci child run(s); each cleans up its own job")
        for _, p in procs:
            if p.poll() is None:
                _ci_stop(p)
        for t in ths:
            t.join(90)
        for _, p in procs:
            if p.poll() is None:
                p.kill()
        raise
    return [(n, *outs.get(n, (5, "", "child result lost"))) for n in names]


def _ci_line(d: dict) -> list[str]:
    """Reply-ready lines: `<job> <node> <result> <wall>s | <reason or -> | next: <phase>` (+ failed:/warn: lines)."""
    w = d.get("wall_s") if _num(d.get("wall_s")) else d.get("wait_s")  # not admitted: the time spent waiting
    out = [f"{d['job']} {d.get('node') or '-'} {d['result']} {round(w) if _num(w) else 0}s | "
           f"{d.get('reason') or '-'} | next: {d.get('next') or '-'}"]
    if d["result"] == "test-fail" and d.get("failures"):
        out.append("failed: " + ", ".join(str(f)[:100] for f in d["failures"][:3]))
    out += [f"warn: {w_}" for w_ in d.get("warns", [])]
    return out


CI_KEYS = ("job", "stage", "result", "exit", "node", "wall_s", "reason", "failures", "jobid", "sha", "wt", "next")


def cmd_ci(a) -> int:
    """Jobs by stage: lint+unit, then build+image, then e2e (or exactly --jobs). The jobs of one group run as parallel
    child `orch run` processes; a red group stops the rest unless --keep-going. stdout: one reply-ready line per job,
    then one JSON line {result, exit, jobs, next}; exit = the worst child exit."""
    jobs, held = cfg_jobs()["jobs"], []
    if a.jobs is not None:
        names = list(dict.fromkeys(x.strip() for x in a.jobs.split(",") if x.strip()))
        bad = [x for x in names if x not in jobs]
        lo = [m for m in map(left_out_msg, bad) if m]
        if bad and len(lo) == len(bad):
            raise OrchError("usage", "; ".join(lo))
        if bad or not names:
            raise OrchError("usage", f"unknown job {', '.join(bad)}; known: {', '.join(sorted(jobs))}" if bad
                            else "--jobs needs at least one job name, e.g. orch ci --jobs app-test")
    else:
        stages = [x.strip() for x in (a.stages or ",".join(CI_STAGES)).split(",") if x.strip()]
        bad = [x for x in stages if x not in CI_STAGES]
        if bad or not stages:
            raise OrchError("usage", f"unknown stage {', '.join(bad) or '(none)'}; valid: {', '.join(CI_STAGES)}")
        names = [k for k, c in jobs.items() if c.get("stage") in stages]
        # project jobs of these stages left out until their image is pinned (review round 2 Q9): named, never dropped
        held = [n for n in PROJECT.get("left_out") or {} if left_out_stage(n) in stages]
        if not names:
            if held:
                raise OrchError("usage", "; ".join(left_out_msg(n) for n in held))
            raise OrchError("usage", f"no job has a stage in {', '.join(stages)}; jobs without a stage run only when "
                                     f"named: orch ci --jobs <job,job>")
    plan = []
    for g in CI_GROUPS:
        grp = [n for n in names if jobs[n].get("stage") in g]
        hold = [n for n in held if left_out_stage(n) in g]
        if grp or hold:
            st = {jobs[n].get("stage") for n in grp} | {left_out_stage(n) for n in hold}
            plan.append(("+".join(s_ for s_ in g if s_ in st), grp, hold))
    rest = [n for n in names if not any(jobs[n].get("stage") in g for g in CI_GROUPS)]
    if rest:
        plan.append((",".join(rest), rest, []))
    results, red = [], None
    try:
        for gi, (label, grp, hold) in enumerate(plan):
            res = []
            if red and not a.keep_going:
                res = [{"job": n, "result": "skipped", "exit": None, "reason": f"not run: {red} is red (--keep-going runs it)"}
                       for n in grp]
            elif grp:
                say(f"ci {label}: {', '.join(grp)}", m=True)
                for n, rc, out, err in ci_run_group(grp, a):
                    d = ci_result(n, rc, out, err)
                    d["warns"] = list(dict.fromkeys(l.split("] warn: ", 1)[1].strip() for l in err.splitlines()
                                                    if "] warn: " in l))[:3]
                    d["next"] = (plan[gi + 1][0] if gi + 1 < len(plan) else "done") if d["exit"] == 0 \
                        else ci_next(d["exit"], jobs[n].get("stage"))
                    res.append(d)
            # refused before any run, as `orch run <job>` would be (exit 2): a stage never passes without its job
            res += [{"job": n, "stage": left_out_stage(n), "result": "usage", "exit": 2, "reason": left_out_msg(n),
                     "next": "run orch init"} for n in hold]
            if any(d["exit"] not in (0, None) for d in res):
                red = red or label
            for d in res:
                d["stage"] = jobs[d["job"]].get("stage") if d["job"] in jobs else d.get("stage")
                print("\n".join(_ci_line(d)), flush=True)
            results += res
    except KeyboardInterrupt as e:
        say(f"interrupted ({e}): ci stopped; finished jobs are listed, the running ones cleaned up themselves")
        return _ci_final(results, 5, reason=f"interrupted ({e})")
    return _ci_final(results, worst_exit([d["exit"] for d in results if d["exit"] is not None]))


def _ci_final(results: list[dict], worst: int, reason: str | None = None) -> int:
    first = next((d for d in results if d["exit"] == worst), {})
    out = {"result": "pass" if worst == 0 else first.get("result", "infra"), "exit": worst,
           "jobs": [{k: (d.get(k)[:3] if k == "failures" and d.get(k) else d.get(k)) for k in CI_KEYS} for d in results],
           "next": "done" if worst == 0 else (first.get("next") or NEXT_ON.get(worst, "stop"))}  # = that job's line
    if reason:
        out["reason"] = reason
    JOURNAL.update(result=out["result"], exit=worst, next=out["next"], reason=reason,
                   jobs=[{k: d.get(k) for k in ("job", "stage", "result", "exit", "node", "wall_s", "reason", "jobid")}
                         for d in results])
    print(json.dumps(out), flush=True)
    return worst


# ----------------------------------------------------------------------------- selftest
def _self(args, env=None, timeout=900) -> tuple[int, str, float]:
    t0 = now()
    p = subprocess.run([sys.executable, "-u", str(Path(__file__).resolve())] + args, capture_output=True,
                       timeout=timeout, env=dict(os.environ, **(env or {})))
    return p.returncode, dec(p.stdout) + dec(p.stderr), round(now() - t0, 1)


def _last_json(text: str) -> dict:
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {}


DEAD_SOCK = "unix:///run/user/0/orch-selftest-dead.sock"  # phantom-only docker socket path: never a real daemon


def project_jobs() -> list[str]:
    """Config jobs that are not orch's own built-ins (t-* test jobs, shell-investigate)."""
    return [k for k in cfg_jobs()["jobs"] if not k.startswith("t-") and k != "shell-investigate"]


def phantom_nodes(nodes: dict) -> tuple[dict, list, str | None]:
    """Selftest phantoms: hx = unreachable (fully synthetic, a TEST-NET address); hd = docker down (a copy of the first
    enabled Linux node whose docker calls go to a dead socket). Returns ({name: node}, [(name, expected word)], the
    reason hd was skipped or None)."""
    out = {"hx": {"enabled": True, "os": "linux", "ssh": "hx-unreachable.invalid", "host": "192.0.2.1",
                  "transport": "ssh-docker", "docker_host": "unix:///var/run/docker.sock", "context": None,
                  "tags": ["docker"], "opt_in": ["t-ok"], "io": None, "note": "phantom unreachable"}}
    phantoms = [("hx", "unreachable")]
    base = next((c for c in nodes.values() if c.get("os") == "linux" and c.get("enabled")), None)
    if base is None:
        return out, phantoms, "skipped: needs an enabled Linux node in config"
    hd = dict(base, docker_host=DEAD_SOCK, tags=["docker"], opt_in=["t-ok"], note="phantom: docker via a dead socket")
    if hd.get("ssh") != "local":  # remote: docker -H <dead socket> over ssh; local: the probe shell exports it
        hd.update(transport="ssh-docker", context=None)
    out["hd"] = hd
    phantoms.append(("hd", "docker down"))
    return out, phantoms, None


def _role_prod(c: dict) -> bool:
    """A node compiled from a host that is also production, here or in another approved setup (role helper+prod)."""
    return "prod" in str(c.get("role") or "").split("+")


def selftest_prod(nodes: dict) -> dict:
    """{node: why} for the selftest nodes whose host is also a production server: the compiled role (helper+prod) or
    a production host of the guard view (as legacy_prod_nodes matches it, without resolving)."""
    out = {k: "is also a production server (role helper+prod)" for k, n in nodes.items() if _role_prod(n.c)}
    try:
        view, _ = legacy_prod_nodes({k: n.c for k, n in nodes.items()})
    except Exception as e:  # noqa: BLE001 - an unreadable view never lets a failure-path job reach production
        view = {k: f"could not be checked against the production hosts ({e!r})"[:200] for k in nodes}
    for k, why in view.items():
        out.setdefault(k, why)
    return out


def selftest_plan(up: list[str], prod: dict, named=None) -> tuple[list[str], str | None, list[tuple[str, str]]]:
    """Where selftest's live jobs run: (the t-ok nodes, the failure-path node or None, [(check, skip detail)]). A node
    whose host is also production (prod: {node: why}) never runs the failure-path jobs (t-fail, t-sleep, t-oom, the
    cordon, and the kill and concurrency checks with t-hold), and runs t-ok only when --node names it."""
    named = set(named or [])
    tok = [n for n in up if n not in prod or n in named]
    safe = [n for n in up if n not in prod]
    skips = [(f"{n}: t-ok", f"skipped: {n} {prod[n]}; selftest runs t-ok there only when you name it with --node {n}")
             for n in up if n not in tok]
    if up and not safe:
        skips.append(("failure-path jobs", f"skipped: {', '.join(f'{n} {prod[n]}' for n in up)}; orch never runs "
                                           f"t-fail, t-sleep, t-oom or t-hold on a production server: add a helper that "
                                           f"is not production to test the failure paths"))
    return tok, (safe[-1] if safe else None), skips


def phantom_config(d: Path) -> tuple[list, str | None]:
    """selftest's temp legacy config in d: the current policy, jobs and protected settings with ONLY the phantom
    nodes, hd copying a node that is no production host of the guard view (legacy mode refuses those) and not a
    helper that is also production (role helper+prod). Returns (phantoms, why hd was skipped or None)."""
    nj = json.loads(json.dumps(cfg_nodes()))
    prod, _ = legacy_prod_nodes(nj.get("nodes", {}), resolve=True)
    for k, c in (nj.get("nodes") or {}).items():
        if isinstance(c, dict) and _role_prod(c):
            prod.setdefault(k, "is also a production server (role helper+prod)")
    extra, phantoms, skip = phantom_nodes({k: v for k, v in nj.get("nodes", {}).items() if k not in prod})
    if skip and prod:
        pn = sorted(n for n, w in prod.items() if not w.startswith("could not be checked"))
        un = sorted(n for n in prod if n not in pn)
        if pn:
            skip += f" that is not also production ({', '.join(pn)} {'is' if len(pn) == 1 else 'are'})"
        if un:
            skip += f"{';' if pn else ''} whose ssh destination ssh -G can resolve ({', '.join(un)}: it cannot)"
    nj["nodes"] = extra
    write_json(d / "nodes.json", nj)
    write_json(d / "jobs.json", cfg_jobs())
    write_json(d / "protected.json", cfg_prot())
    return phantoms, skip


def phantom_env(d: Path) -> dict:
    """Env of selftest's phantom runs: orch's own children on the config phantom_config wrote, so legacy mode is
    allowed inside the approved project they test (ORCH_TEST_MODE=1); the production-host check still applies."""
    return {"ORCH_CONFIG": str(d), "ORCH_TEST_MODE": "1"}


def cmd_selftest(a) -> int:
    """Short end-to-end proof: unit checks, then real dispatches on every reachable node, failure paths (never on a
    node whose host is also production: selftest_plan), cleanup proof. Writes
    $ORCH_HOME/selftest/selftest-run-<stamp>.md (or --out <path>)."""
    import io
    import tempfile
    os.environ["ORCH_SELFTEST"] = "1"  # inherited by every orch child run: r1-guard trips never phone during selftest
    rows, md = [], [f"# orch selftest {ts()}", "", f"owner {OWNER}, session {session_id()}", ""]

    def rec(name, ok, detail, out=""):
        rows.append((name, ok, detail))
        tag = "SKIP" if ok and detail.startswith("skipped:") else "PASS" if ok else "FAIL"  # a skip never fails a run
        print(f"[{tag}] {name}: {detail}", flush=True)
        md.extend([f"## {tag} - {name}", "", detail, ""])
        if out:
            md.extend(["```", out.strip()[-3500:], "```", ""])

    # --- unit checks (no node access)
    d = Path(tempfile.mkdtemp(prefix="orch-st-"))
    tp = d / "evil.tar"
    with tarfile.open(tp, "w") as t:
        for nm, data in (("out/good.txt", b"ok"), ("out/../../evil.txt", b"x"), ("/abs.txt", b"x")):
            ti = tarfile.TarInfo(nm)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        ln = tarfile.TarInfo("out/link")
        ln.type, ln.linkname = tarfile.SYMTYPE, "../../../../etc/passwd"
        t.addfile(ln)
    n, refused = safe_extract(tp, d / "x")
    files = sorted(str(p.relative_to(d / "x")) for p in (d / "x").rglob("*") if p.is_file())
    rec("safe tar extraction", n == 1 and files == ["good.txt"] and len(refused) == 3,
        f"extracted {files}, refused {refused}")
    names = ["src/.env", "src/a/id_rsa", "src/.env.example", "src/app/page.tsx", "src/appsettings.Production.json"]
    hits = [x for x in names if any(re.search(p, x, re.I) for p in SECRET_NAME) and not any(re.search(q, x, re.I) for q in SECRET_ALLOW)]
    rec("secret-pattern scan of file list", hits == ["src/.env", "src/a/id_rsa", "src/appsettings.Production.json"], f"flagged {hits}")
    (d / "r").mkdir()
    (d / "r" / "log.txt").write_text("db Password=Hunter2Secret ok\nAKIAABCDEFGHIJKLMNOP\n")
    k = scan_results(d / "r")
    txt = (d / "r" / "log.txt").read_text()
    rec("result secret redaction", k == 2 and "Hunter2" not in txt and "AKIA" not in txt, f"{k} redactions -> {txt!r}")
    checks = [ours(PREFIX + "1-run", "1"), not ours(PREFIX + "1-run", ""), not ours("app1-web", "1"),
              not ours("shop-api-1", "1"), not ours(PREFIX.upper() + "1-run", "1")]
    rec("ownership predicate (label AND name prefix)", all(checks), f"{checks}")

    class FakeNode(Node):  # remove_objects against canned inspect answers: records what it would run
        def __init__(self):
            super().__init__("fake", {"os": "linux", "ssh": "none", "context": "none"})
            self.sent = []

        def dbatch(self, cmds, job=None, timeout=90):
            out = []
            for c in cmds:
                if "inspect" in c:
                    nm = c[-1]
                    lab = {"app1-web": {}, PREFIX + "nolabel": {"x": "1"}, PREFIX + "other": {L_AGENT: "1", L_OWNER: "someone"},
                           PREFIX + "good": {L_AGENT: "1", L_OWNER: OWNER}}.get(nm, {L_AGENT: "1", L_OWNER: OWNER})
                    out.append(R(0, f"/{nm}\t{json.dumps(lab)}\n"))
                else:
                    self.sent.append(c)
                    out.append(R(0, ""))
            return out
    fn = FakeNode()
    victims = [{"kind": "container", "name": n_, "agent": "1", "owner": OWNER} for n_ in
               ("app1-web", PREFIX + "nolabel", PREFIX + "other", PREFIX + "good", "shop-gateway")]
    remove_objects(fn, victims, "selftest")
    rec("removal re-verifies labels on the node (foreign/unlabelled/other-owner objects refused even if the caller says agent=1)",
        fn.sent == [["rm", "-f", "-v", PREFIX + "good"]], f"commands issued: {fn.sent}")
    old = "2026-10-01T08:00:00.000000000Z"
    fresh = dt.datetime.fromtimestamp(now() - 60, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0Z")
    a0 = {"t": now(), "containers": {
        "c1": {"name": "app1-api-1", "status": "running", "health": "healthy", "restarts": 0, "started": old},
        "c2": {"name": "app1-web", "status": "running", "health": "healthy", "restarts": 0, "started": fresh}},
        "services": {}, "latency": {}}
    a1 = {"containers": {
        "c1": {"name": "app1-api-1", "status": "running", "health": "unhealthy", "restarts": 1, "started": "2026-10-29T10:00:00Z"},
        "c2": {"name": "app1-web", "status": "running", "health": "unhealthy", "restarts": 1, "started": "2026-10-29T10:00:00Z"}},
        "services": {}, "latency": {}}
    worse, info = fp_diff(a0, a1)
    rec("fingerprint diff: worse only for running containers stable > 10 min; recently started ones are info",
        len(worse) == 3 and all(w.startswith("app1-api-1") for w in worse) and any("app1-web" in i for i in info),
        f"worse={worse}; info={info}")
    # alert checks use a synthetic alert command under ORCH_ALERT_DRYRUN (never run), whatever the config holds
    acfg = {"command": "cat >/dev/null", "window_start_h": 0, "window_end_h": 24}
    keep = {k: os.environ.get(k) for k in ("ORCH_ALERT_DRYRUN", "ORCH_SELFTEST")}

    def env_restore():
        for k_, v_ in keep.items():
            if v_ is None:
                os.environ.pop(k_, None)
            else:
                os.environ[k_] = v_
    try:
        alert("selftest-node", worse + ["evil$(rm -rf /)`x`<b>"], call=False, acfg=acfg)
        l1 = (HOME / "alerts.log").read_text(encoding="utf-8").splitlines()[-1]
        os.environ["ORCH_ALERT_DRYRUN"] = "1"
        alert("selftest-node", ["watchdog r1-guard: iowait 45% > 30%; $(id)"], call=True, acfg=acfg)
        l2 = (HOME / "alerts.log").read_text(encoding="utf-8").splitlines()[-1]
        m2 = json.loads(l2)["msg"]
        rec("alert: fingerprint diff = line only (no command); r1-guard = command (dry run); text sanitised to [A-Za-z0-9 _.:;,/-]",
            '"call": "none' in l1 and "DRYRUN" in l2 and re.fullmatch(r"[A-Za-z0-9 _.:;,/-]*", m2) is not None
            and "$(" not in l1 and "`" not in l1, f"diff alert: {l1[:260]} | r1 alert msg: {m2}")

        class _StubJob(dict):  # r1_guard_alert reads .name and .get("test_only") only
            def __init__(self, name, **c):
                super().__init__(c)
                self.name = name
        # r1-guard routing (dry run): real job -> command; t-* job or selftest run -> alert line only
        calls = {}
        for label, jb, st_ in (("real job app-test", _StubJob("app-test"), None),
                               ("test job t-hold", _StubJob("t-hold", test_only=True), None),
                               ("real job inside a selftest run", _StubJob("app-test"), "1")):
            if st_:
                os.environ["ORCH_SELFTEST"] = st_
            else:
                os.environ.pop("ORCH_SELFTEST", None)
            r1_guard_alert("selftest-node", jb, "iowait 45% > 30% (simulated)", acfg=acfg)
            calls[label] = json.loads((HOME / "alerts.log").read_text(encoding="utf-8").splitlines()[-1])["call"]
    finally:
        env_restore()
    rec("r1-guard alert command only for real jobs (dry run): t-* and selftest runs log the alert line only",
        calls["real job app-test"].startswith("DRYRUN would run") and calls["test job t-hold"].startswith("none: test job")
        and calls["real job inside a selftest run"].startswith("none: selftest run"),
        "; ".join(f"{k} -> {v[:120]}" for k, v in calls.items()))
    # tar bomb caps (scaled-down limits so the test stays small; same code path as the 2 GB / 50k / 500 MB caps)
    bomb = d / "bomb.tar"
    with tarfile.open(bomb, "w") as t:
        for k in range(30):
            ti = tarfile.TarInfo(f"out/f{k}.txt")
            ti.size = 2048
            t.addfile(ti, io.BytesIO(b"x" * 2048))
    outs = []
    for kw in ({"max_members": 20}, {"max_file": 1024}, {"max_total": 40000}):
        try:
            safe_extract(bomb, d / "bomb", **kw)
            outs.append(f"{kw}: NOT refused")
        except OrchError as e:
            outs.append(f"{kw}: refused ({e.msg})")
    rec("tar bomb caps (members / per-file / total)", all("refused (" in o for o in outs), "; ".join(outs))
    shutil.rmtree(d, ignore_errors=True)

    # --- live checks
    if getattr(a, "unit_only", False):
        up = []
        return _selftest_finish(rows, md, up, rec, getattr(a, "out", None))
    rc, out, s = _self(["nodes"])
    rec("orch nodes", rc == 0, f"rc {rc} in {s}s", out)
    project = project_jobs()
    pj = project[0] if project else "t-ok"
    rc, out, s = _self(["plan", pj])
    rec(f"orch plan {pj}", rc in (0, NOADMIT) and "\npick " in "\n" + out, f"rc {rc} in {s}s", out)
    allowed = {n: v for n, v in nodes_all().items() if not a.node or n in a.node}
    up = []
    for name, n in allowed.items():
        p = probe(n)
        if p.get("docker"):
            up.append(name)
        else:
            rec(f"{name} reachable", True, f"skipped (not up: {p.get('reason')})")
    # production servers: no failure-path job ever, t-ok only on a node named with --node (selftest_plan)
    tok, tn, skips = selftest_plan(up, selftest_prod(allowed), a.node)
    for check, why in skips:
        rec(check, True, why)
    for name in tok:
        rc, out, s = _self(["run", "t-ok", "--node", name, "--wait", "0"])
        j = _last_json(out)
        lg = (HOME / "node-logs" / f"{j.get('jobid')}.log")
        lt = lg.read_text(encoding="utf-8", errors="replace") if lg.exists() else ""
        left = [x for x in j.get("leftover_agent_objects") or [] if not x.split()[1].startswith((f"{PREFIX}builder", f"{PREFIX}cache"))]
        good = (rc == 0 and "CapEff:	0000000000000000" in lt and "NoNewPrivs:	1" in lt and not left
                and not (j.get("fingerprint_diff") or {}).get("worse"))
        rec(f"{name}: t-ok pass, CapEff 0 + NoNewPrivs, cleaned, empty diff", good,
            f"rc {rc}, {s}s, diff {j.get('fingerprint_diff')}, leftover {j.get('leftover_agent_objects')}", out)
    if tn:  # the last reachable node that is not production
        for job, want in (("t-fail", 1), ("t-sleep", 4), ("t-oom", 3)):
            rc, out, s = _self(["run", job, "--node", tn, "--wait", "0"] + (["--no-retry"] if job == "t-oom" else []))
            j = _last_json(out)
            rec(f"{tn}: {job} -> {list(EXIT)[list(EXIT.values()).index(want)]} (exit {want}) and cleaned", rc == want and not [
                x for x in j.get("leftover_agent_objects", []) if not x.split()[1].startswith((f"{PREFIX}builder", f"{PREFIX}cache"))],
                f"rc {rc}, {s}s, result {j.get('result')}, diff {j.get('fingerprint_diff')}", out)
        # cordon with ttl -> 75
        _self(["cordon", tn, "--reason", "selftest", "--ttl", "8"])
        rc, out, s = _self(["run", "t-ok", "--node", tn, "--wait", "0"])
        rec(f"{tn}: cordoned node -> exit 75 with reason", rc == NOADMIT and "cordoned" in out, f"rc {rc}", out)
        time.sleep(9)
        # kill mid-job, reconcile after a short lease
        live0 = {l["jobid"] for l in live_jobs()}
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        pr = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "run", "t-hold", "--node", tn, "--wait", "60",
                               "--lease", "15", "--heartbeat", "3", "--", "300"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env)
        jid = None
        for _ in range(90):
            for l in live_jobs():
                if l["jobid"] not in live0 and l.get("stage") == "run":
                    jid = l["jobid"]
            if jid:
                break
            time.sleep(1)
        time.sleep(3)
        pr.kill()  # hard kill: no atexit, no signal handler
        rc1, out1, _ = _self(["reconcile", "--node", tn])
        time.sleep(17)
        rc2, out2, _ = _self(["reconcile", "--node", tn])
        rec(f"{tn}: killed orch mid-job -> reconcile keeps it during the lease, removes it after",
            bool(jid) and "keep container" in out1 and f"{PREFIX}{jid}-job" in out2.split("removed")[-1],
            f"job {jid}", out1 + "\n---\n" + out2)
        # concurrency: two heavy jobs on one node (capacity 1; a node with max_heavy 2 skips this check)
        cap = node_max_heavy(tn, cfg_nodes()["admission"])
        p1 = None if cap > 1 else subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "run", "t-hold", "--node", tn, "--wait", "0",
                               "--", "15"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        went_live = False
        for _ in range(60 if p1 else 0):
            if any(l.get("node") == tn and l.get("job") == "t-hold" for l in live_jobs()):
                went_live = True
                break
            time.sleep(0.5)
        if p1 is None:
            rec(f"{tn}: two concurrent heavy jobs -> one runs, the other exit 75 (capacity 1, master lock)", True,
                f"skipped: {tn} runs up to {cap} heavy jobs at once (max_heavy {cap})")
        elif not went_live:
            p1.kill()
            o1 = dec(p1.communicate(timeout=60)[0])
            rec(f"{tn}: two concurrent heavy jobs -> one runs, the other exit 75 (capacity 1, master lock)", False,
                "first job never went live within 30s: concurrency not tested", o1[-800:])
        else:
            rc, out, s = _self(["run", "t-hold", "--node", tn, "--wait", "0", "--", "15"])
            o1 = dec(p1.communicate(timeout=300)[0])
            why = (_last_json(out) or {}).get("reason") or ""
            if rc == NOADMIT and "busy" not in out and p1.returncode == 0:  # refused, but for the node's load
                rec(f"{tn}: two concurrent heavy jobs -> one runs, the other exit 75 (capacity 1, master lock)", True,
                    f"skipped: the second job was refused for another reason ({why or 'see log'}), so capacity was "
                    f"not tested", out + "\n---\n" + o1[-800:])
            else:
                rec(f"{tn}: two concurrent heavy jobs -> one runs, the other exit 75 (capacity 1, master lock)",
                    rc == NOADMIT and "busy" in out and p1.returncode == 0, f"second rc {rc}, first rc {p1.returncode}", out + "\n---\n" + o1[-800:])
    # unreachable + docker-down phantom nodes -> 75 (phantom_config: a temp legacy config, test-mode children)
    tmp = Path(tempfile.mkdtemp(prefix="orch-cfg-"))
    phantoms, skip = phantom_config(tmp)
    if skip:
        rec("hd: docker down node -> exit 75 with reason", True, skip, "")
    for k, word in phantoms:
        rc, out, s = _self(["run", "t-ok", "--node", k, "--wait", "0"], env=phantom_env(tmp))
        rec(f"{k}: {word} node -> exit 75 with reason", rc == NOADMIT and word in out, f"rc {rc}", out)
    shutil.rmtree(tmp, ignore_errors=True)
    if not a.quick:  # the project's own jobs through the scheduler at HEAD; external jobs (master commands) never
        jobs = cfg_jobs()["jobs"]
        for job in project:
            if jobs[job].get("kind") == "external":
                rec(f"{job} (scheduler)", True, "skipped: an external job runs a command on this master; run it by hand")
                continue
            rc, out, s = _self(["run", job, "--wait", "0"], timeout=3000)
            j = _last_json(out)
            rec(f"{job} (scheduler) pass, diff empty", rc in (0, NOADMIT) and (rc == NOADMIT or not j.get("fingerprint_diff", {}).get("worse")),
                f"rc {rc} on {j.get('node')} in {s}s, timings {j.get('timings')}", out[-2500:])
    return _selftest_finish(rows, md, up, rec, getattr(a, "out", None))


def _selftest_finish(rows, md, up, rec, out=None) -> int:
    # final: only builder/cache may remain
    for name in up:
        la = list_agent(nodes_all()[name], job="selftest")
        extra = [o["name"] for o in la["objects"] if o.get("role") not in ("builder", "cache")]
        rec(f"{name}: no job objects left (builder/cache allowed until gc)", not extra and not la["collisions"],
            f"objects {[o['kind'] + ':' + o['name'] for o in la['objects']]} collisions {la['collisions']}")
    ok = all(r[1] for r in rows)
    skip = sum(1 for r in rows if r[1] and r[2].startswith("skipped:"))
    sk = f", {skip} skipped" if skip else ""
    md.insert(4, f"**{sum(1 for r in rows if r[1]) - skip}/{len(rows)} checks passed{sk}**\n")
    outp = Path(out) if out else HOME / "selftest" / f"selftest-run-{time.strftime('%Y%m%d-%H%M%S')}.md"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text("\n".join(md), encoding="utf-8")
    print(f"\nselftest: {sum(1 for r in rows if r[1]) - skip}/{len(rows)} passed{sk}; log {outp}")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- project verbs (own modules)
def _ext(name: str, verb: str):
    try:
        return sibling(name)
    except ImportError as e:
        raise OrchError("usage", f"orch {verb} needs scripts/{name}.py next to orch.py ({e}): reinstall the skill") \
            from None


def _ext_details(mod) -> None:
    """What a verb module exposes about its last call (a LAST dict, else details(), else a module JOURNAL dict)
    -> JOURNAL; never raises."""
    try:
        d = getattr(mod, "LAST", None)
        if not (isinstance(d, dict) and d) and callable(getattr(mod, "details", None)):
            d = mod.details()
        if not (isinstance(d, dict) and d):
            d = getattr(mod, "JOURNAL", None)
        if isinstance(d, dict):
            JOURNAL.update(d)
    except Exception:  # noqa: BLE001 - journal details are optional
        pass


def _ext_call(name: str, fn: str, a) -> int:
    mod = _ext(name, a.verb)
    call = getattr(mod, fn, None)
    if not callable(call):
        raise OrchError("usage", f"scripts/{name}.py has no {fn}(), so orch {a.verb} cannot run: reinstall the skill")
    try:
        cerr = sibling("orch_config").ConfigError
    except (ImportError, AttributeError):
        cerr = ConfigError
    try:
        return call(a)
    except cerr as e:
        raise ConfigError(str(e)) from None
    finally:
        _ext_details(mod)


def cmd_init(a) -> int:
    return _ext_call("orch_init", "cmd_init", a)


def cmd_deploy(a) -> int:
    return _ext_call("orch_deploy", "cmd_deploy", a)


def cmd_live(a) -> int:
    return _ext_call("orch_deploy", "cmd_live", a)


def cmd_journal(a) -> int:
    return _ext_call("orch_journal", "cmd_journal", a)


def journal_hook(a, argv: list[str], started: float, rc: int) -> None:
    """Project mode: one journal step record per orch invocation (orch_journal.record_invocation), details = JOURNAL.
    Never raises, never prints on stdout (the final line stays the verb's) and never changes the exit code. After a
    non-zero exit one stderr line names the difficulty orch recorded ("journal: difficulty #N recorded (orch)"), so
    the agent can resolve it instead of adding its own copy. Off in legacy ORCH_CONFIG mode (but a legacy call refused
    inside an approved project is recorded there), with ORCH_JOURNAL=0 and for orch journal itself."""
    try:
        legacy = os.environ.get("ORCH_CONFIG") and not _LEGACY.get("refused")
        if a.verb == "journal" or legacy or os.environ.get("ORCH_JOURNAL") == "0":
            return
        oc = sibling("orch_config")
        p = PROJECT["p"]
        if p is None or a.verb == "init":  # init may just have created the setup, or works on --project
            try:
                p = oc.find_project(getattr(a, "project", None))
            except oc.ConfigError:
                return
        if p is None:
            return
        try:
            jr = sibling("orch_journal")
        except ModuleNotFoundError as e:
            if e.name == "orch_journal":
                return  # no journal module installed: nothing to record
            raise
        details = json.loads(json.dumps(JOURNAL, default=str))
        with contextlib.redirect_stdout(sys.stderr):
            got = jr.record_invocation(p, a.verb, redact(argv), started, now(), rc, details)
            seq = _difficulty_seq(jr, p, got) if rc != 0 else None
        if seq:
            print(f"journal: difficulty #{seq} recorded (orch)", file=sys.stderr)
    except BaseException as e:  # noqa: BLE001 - the journal never fails a verb
        try:
            print(f"orch: warn: journal entry not written ({e!r})"[:300], file=sys.stderr)
        except BaseException:  # noqa: BLE001
            pass


def _seq_of(v) -> int | None:
    v = v.get("seq") if isinstance(v, dict) else v
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _difficulty_seq(jr, p, got) -> int | None:
    """The seq of the difficulty record orch's journal wrote for this invocation, from record_invocation's return:
    the difficulty record itself, a "difficulty" seq (or record) in it, or the step record, whose difficulty links
    back to it by "step" among the newest records. None when no difficulty was written (ci leaves its failed jobs to
    their own runs)."""
    if not isinstance(got, dict):
        return None
    if got.get("type") == "difficulty":
        return _seq_of(got)
    if _seq_of(got.get("difficulty")):
        return _seq_of(got.get("difficulty"))
    step = _seq_of(got) if got.get("type") == "step" else None
    if step is None or not callable(getattr(jr, "read", None)):
        return None
    try:
        recs = jr.read(p, last=20) or []
    except Exception:  # noqa: BLE001 - the record is written; only its number is unknown
        return None
    for r in reversed(recs):
        if isinstance(r, dict) and r.get("type") == "difficulty" and r.get("by") == "orch" and r.get("step") == step \
                and r.get("task") == got.get("task"):
            return _seq_of(r)
    return None


def _dispatch(fn, a) -> int:
    try:
        return fn(a)
    except NoSetup as e:
        print(f"orch: {e}", file=sys.stderr)
        if e.detail:
            print(f"orch: {e.detail}", file=sys.stderr)
        JOURNAL.setdefault("error", str(e))
        print(json.dumps({"result": "usage", "exit": 2, "error": str(e), "next": "run orch init"}))
        return 2
    except ConfigError as e:
        print(f"orch: config rejected: {e}", file=sys.stderr)
        JOURNAL.setdefault("error", f"config rejected: {e}"[:500])
        print(json.dumps({"result": "usage", "exit": 2, "error": f"config rejected: {e}"}))
        return 2
    except OrchError as e:
        print(f"orch: {e.cls}: {e.msg}", file=sys.stderr)
        JOURNAL.setdefault("error", f"{e.cls}: {e.msg}"[:500])
        code = 2 if e.cls in ("usage", "refused") else EXIT.get(e.cls, 5)
        print(json.dumps({"result": e.cls, "exit": code, "error": e.msg}))
        return code
    except Exception as e:  # noqa: BLE001 - a crash is infra (5), never test-fail (1)
        traceback.print_exc()
        msg = f"internal error in orch {a.verb}: {e!r}"[:300]
        print(f"orch: {msg}", file=sys.stderr)
        JOURNAL.setdefault("error", msg)
        print(json.dumps({"result": "infra", "exit": 5, "error": msg}))
        return 5


def journal_parser(sp, proj: dict) -> None:
    """Register orch journal through orch_journal itself (add_parser, else add_arguments), so its flags and help
    cannot drift from what cmd_journal reads. Each is tried on a scratch parser first, so a broken module never
    leaves a half-registered verb; a missing or broken module gets this minimal copy (orch journal then reports it)."""
    try:
        jr = sibling("orch_journal")
    except Exception:  # noqa: BLE001 - not installed, or does not import: orch's other verbs still parse
        jr = None
    for how in ("add_parser", "add_arguments"):
        fn = getattr(jr, how, None)
        if not callable(fn):
            continue
        try:
            scratch = argparse.ArgumentParser(add_help=False)
            fn(scratch.add_subparsers() if how == "add_parser" else scratch)
        except Exception:  # noqa: BLE001
            continue
        if how == "add_parser":
            fn(sp)
        else:
            fn(sp.add_parser("journal", help="project journal: start|add|end (JSON on stdin), show, stats, path",
                             allow_abbrev=False))
        return
    p = sp.add_parser("journal", help="project journal: start|add|end (JSON on stdin), show, stats, path",
                      allow_abbrev=False)
    p.add_argument("action", choices=("start", "add", "end", "show", "stats", "path"),
                   help="start, add and end read one JSON object on stdin")
    p.add_argument("--task", help="task id (default: the current task)")
    p.add_argument("--last", type=int, help="show: the newest N records")
    p.add_argument("--days", type=float, help="stats: only records of the last N days")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--project", **proj)


def init_parser(sp, proj: dict, verb: str | None) -> None:
    """Register orch init with allow_abbrev=False (--appr never means --approve behind the guard's back). When the verb
    is init, its flags come from orch_init.add_arguments, so they cannot drift from what cmd_init reads (tried on a
    scratch parser first); every other verb skips that import (orch_init is the heaviest module), and a missing or
    broken orch_init gets this minimal copy (orch init then reports it)."""
    fn = None
    if verb == "init":
        try:
            fn = getattr(sibling("orch_init"), "add_arguments", None)
            if callable(fn):
                fn(argparse.ArgumentParser(add_help=False, allow_abbrev=False))
        except Exception:  # noqa: BLE001 - not installed, or broken: orch init still parses
            fn = None
    p = sp.add_parser("init", allow_abbrev=False, help="set up or check this project's helpers, production and jobs "
                      "(final line JSON)")
    if callable(fn):
        fn(p)
        return
    g = p.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="apply the JSON answers on stdin")
    g.add_argument("--detect", action="store_true", help="re-run job detection and show the proposal")
    g.add_argument("--approve", action="store_true", help="pin images and approve the setup (after the user said yes)")
    g.add_argument("--show", action="store_true", help="print the full saved setup")
    g.add_argument("--ask", choices=("deploy", "production"), help="print the deploy or production question again")
    p.add_argument("--offline", action="store_true", help="no probe: status from the saved state")
    p.add_argument("--project", **proj)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="orch", description=__doc__.splitlines()[0], allow_abbrev=False)
    sp = ap.add_subparsers(dest="verb", required=True)
    proj = {"metavar": "DIR", "help": "project directory (default: the current one, or ORCH_PROJECT)"}
    init_parser(sp, proj, argv[0] if argv else None)
    p = sp.add_parser("nodes", help="live probe: one line per node (--all full table + tags)")
    p.add_argument("--json", action="store_true", help="one JSON object per node on stdout (config line on stderr)")
    p.add_argument("--all", action="store_true", help="full table plus tags")
    p = sp.add_parser("plan", help="one line per node (admit|skip score reason), then pick <node> (--all full breakdown)")
    p.add_argument("job")
    p.add_argument("--all", action="store_true", help="every check and the score breakdown")
    p = sp.add_parser("run", help="run a job on a helper node")
    p.add_argument("job")
    p.add_argument("--node")
    p.add_argument("--ref")
    p.add_argument("--wt", action="store_true", help="archive the working tree incl. untracked files (still secret-scanned)")
    p.add_argument("--wait", type=int, default=600, help="seconds to wait for admission (default 600)")
    p.add_argument("--lease", type=int, default=0, help="lease seconds (default from nodes.json)")
    p.add_argument("--heartbeat", type=int, default=0)
    p.add_argument("--no-retry", action="store_true")
    p.add_argument("-v", "--verbose", dest="verbose", action="store_true", help="full progress on stderr (or ORCH_VERBOSE=1)")
    p = sp.add_parser("ci", help="jobs stage by stage: lint+unit, then build+image, then e2e; parallel within a stage")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stages", help=f"comma list of stages (default {','.join(CI_STAGES)})")
    g.add_argument("--jobs", help="comma list of jobs, any stage (jobs without a stage run last)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ref", help="test this commit (default HEAD of each job's repo)")
    g.add_argument("--wt", action="store_true", help="archive the working tree incl. untracked files")
    p.add_argument("--keep-going", action="store_true", help="run later stages even after a red one")
    p.add_argument("--wait", type=int, default=600, help="admission wait per job in seconds (default 600)")
    p = sp.add_parser("deploy", help="deploy gate: --check whether this commit may be deployed (orch never deploys)")
    p.add_argument("--check", action="store_true", help="check every gate; exit 7 when one is not met")
    p.add_argument("--ref", default="HEAD", help="commit to check (default HEAD)")
    p.add_argument("--project", **proj)
    p = sp.add_parser("live", help="after a deploy: check every saved health URL")
    p.add_argument("--project", **proj)
    journal_parser(sp, proj)
    p = sp.add_parser("status")
    p.add_argument("--local", action="store_true")
    p.add_argument("--last", type=int, default=10)
    p = sp.add_parser("inventory", help="read-only: df totals + agent-owned items on a node (--all adds docker system df -v)")
    p.add_argument("node")
    p.add_argument("--top", type=int, default=None, help="with --all: top N rows per table (default all)")
    p.add_argument("--all", action="store_true", help="add the per-item docker system df -v tables")
    p.add_argument("--json", action="store_true")
    p = sp.add_parser("logs", help="meta summary, failure lines, last 80 log lines")
    p.add_argument("jobid")
    p.add_argument("--tail", type=int, default=None, help="last N log lines (default 80)")
    p.add_argument("--all", action="store_true", help="print the full log")
    p = sp.add_parser("cordon")
    p.add_argument("node")
    p.add_argument("--reason", default="")
    p.add_argument("--ttl", type=int, default=0, help="auto-expire after N seconds (default: until uncordon)")
    p = sp.add_parser("uncordon", help="only when the user asks")
    p.add_argument("node")
    p = sp.add_parser("hold", help="suspend the fingerprint diff for <minutes> (> 0) during a planned deploy")
    p.add_argument("node")
    p.add_argument("minutes", type=float)
    p = sp.add_parser("reconcile")
    p.add_argument("--node")
    p = sp.add_parser("gc", help="--session-end: remove orch's objects on every node (or one --node)")
    p.add_argument("--session-end", action="store_true")
    p.add_argument("--node", metavar="NAME", help="only this node of the setup (orch init runs it before a helper "
                   "leaves); exit 0 clean, 5 unreachable or objects left, 75 a live job on it, 2 unknown node")
    sp.add_parser("stop-all", help="kill switch")
    p = sp.add_parser("selftest")
    p.add_argument("--quick", action="store_true", help="skip the project jobs (t-* checks only)")
    p.add_argument("--unit-only", action="store_true", help="only the offline checks")
    p.add_argument("--node", action="append", help="limit to node(s)")
    p.add_argument("--out", help="evidence file path (default $ORCH_HOME/selftest/selftest-run-<stamp>.md)")
    full_argv = list(argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    a.args = extra
    HOME.mkdir(parents=True, exist_ok=True)
    atexit.register(emergency_cleanup)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _sig)
    fn = {"nodes": cmd_nodes, "plan": cmd_plan, "run": cmd_run, "status": cmd_status, "logs": cmd_logs,
          "cordon": cmd_cordon, "uncordon": cmd_uncordon, "inventory": cmd_inventory, "hold": cmd_hold, "reconcile": cmd_reconcile,
          "gc": cmd_gc, "stop-all": cmd_stop_all, "selftest": cmd_selftest, "ci": cmd_ci, "init": cmd_init,
          "deploy": cmd_deploy, "live": cmd_live, "journal": cmd_journal}[a.verb]
    JOURNAL.clear()
    reset_project()
    VERB["name"] = a.verb
    started, rc = now(), 5
    try:
        rc = _dispatch(fn, a)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        raise
    except BaseException as e:
        JOURNAL.setdefault("error", f"{e!r}"[:300])
        raise
    finally:
        journal_hook(a, full_argv, started, rc)
        VERB["name"] = None
    return rc


if __name__ == "__main__":
    sys.exit(main())
