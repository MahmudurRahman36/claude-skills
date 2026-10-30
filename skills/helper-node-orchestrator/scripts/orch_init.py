#!/usr/bin/env python3
"""orch init: the setup interview (design section 4, contract "orch_init.py").

orch owns the interview and the agent relays it. cmd_init(a) glues orch_config (decisions, the status state machine,
trust, registry, guard view), orch_discover (ssh -G, probes, docker contexts) and orch_detect (job proposals), and
writes one journal decision per applied answer when orch_journal is installed. Every call prints human lines, then
exactly ONE final JSON line: orch_config.status() plus "applied" and "warnings" (and "error" when it exits 2).

  orch init              status, after re-probing every saved host in parallel
  orch init --offline    status from saved state: no ssh, no registry
  orch init --apply      answer JSON on stdin (send it with a quoted heredoc <<'EOF'); checked as a whole, then saved
  orch init --detect     re-run job detection, store the proposal, print it and how it differs from the saved jobs
  orch init --approve    pin job images to digests, record trust, write the guard view
  orch init --show       the full saved setup
  orch init --ask deploy|production   the deploy gate's question for a "later" or missing answer, or the health
                         URL question orch live asks for (nothing probed or saved)
  --project DIR          the project folder (default: ORCH_PROJECT, else the current folder)

Exit 0, or 2 for a usage or config error (nothing is saved then). Stdlib only. Every process goes through Ctx.run and
the registry fallback through Ctx.http, so unit tests never touch ssh, docker or the network. LAST, or details(),
holds the last call's state, step, applied, warnings, exit and error (and an --apply's answer_keys) for the project
journal.
"""
import argparse
import concurrent.futures as cf
import copy
import hashlib
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import orch_config as oc
import orch_detect
import orch_discover

try:  # written in parallel (wave 1b); init works without it
    import orch_journal
except Exception:  # noqa: BLE001 - a missing or broken journal never stops init
    orch_journal = None

ANSWER_KEYS = ("helper_count", "helpers", "hosts", "production", "deploy", "alerts", "jobs", "jobs_remove",
               "remove_hosts")
SETUP_KEYS = ("hosts", "production", "deploy", "alerts")  # the setup step's answer; {"setup": {...}} is unwrapped
HOST_ANSWER_KEYS = ("roles", "jobs", "watch", "ssh")
ORDER = ("remove_hosts", "helper_count", "helpers", "hosts", "production", "deploy", "alerts", "jobs", "jobs_remove")
DEF_KEYS = ("stage", "kind", "image", "dir", "repo", "prep", "cmd", "cache", "env", "dockerfile", "context")
MAX_HELPERS = 32
LAST = {}  # the last cmd_init's state, step, applied, warnings, exit (+ error, answer_keys), for orch.py's journal
GC_TIMEOUT = 900  # seconds for one helper's cleanup (orch gc --session-end --node) before it stops being a helper
MANIFEST_TYPES = ("application/vnd.oci.image.index.v1+json",
                  "application/vnd.docker.distribution.manifest.list.v2+json",
                  "application/vnd.docker.distribution.manifest.v2+json",
                  "application/vnd.oci.image.manifest.v1+json")
HEREDOC = ("orch init --apply reads the answer JSON on stdin; send it with a quoted heredoc: "
           "orch init --apply <<'EOF' / {\"helper_count\": 2} / EOF (three lines)")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMP = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REF = re.compile(rf"(?P<name>{_COMP}(?:/{_COMP})*)(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}))?"
                  rf"(?:@(?P<digest>sha256:[0-9a-f]{{64}}))?")
# what a secret looks like inside a command or URL (_secret): orch saves, prints and journals these, so none may hold
# one. NAME=value / name: value for any word ending in a secret word (DB_PASSWORD=, PGPASSWORD=, X-Auth:), a secret
# flag with its value after a space (--password X, --pw X, --passphrase X, --api-token X), curl/wget -u user:password
# (also attached: -uuser:password), mysql -p<password>, docker login -p X, redis-cli -a X, sshpass -p, an
# Authorization header, bearer and well-known token formats (bare sk-..., sk-proj-..., sk-ant-... and AIza... API keys
# too), chat webhook URLs (their credential is in the path),
# credential query keys (?key=, ?sig=, ?signature=, ?code=) and user:password@ in a URL. A $VAR value is a reference,
# not a secret, and file-style names (--token-file, PASSWORD_FILE=) never match.
_SECRET = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])[A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth)"
    r"\s*[=:]\s*[\"']?(?!\$)[^\s'\"]{6,}"
    r"|(?:^|\s)--?(?:[a-z0-9]+[-_])*(?:pass(?:word|wd|phrase)?|pwd|pw|secret|token|credentials?|api[_-]?key|"
    r"access[_-]?key)\s+[\"']?(?!\$)[^\s\"'-][^\s\"']{5,}"
    r"|\b(?:curl|wget)\b[^|;&]*?\s(?:-u\s*|--(?:user|http-user|proxy-user)(?:\s+|=))[\"']?(?!\$)[^\s:\"']+:(?!\$)"
    r"[^\s\"']+"
    r"|\b(?:mysql|mariadb|mysqldump|mysqladmin)\b[^|;&]*?\s(?-i:-p)[\"']?(?!\$)[^\s\"']+"
    r"|\bdocker\s+login\b[^|;&]*?\s(?-i:-p)(?:\s+|=)[\"']?(?!\$)[^\s\"']+"
    r"|\bredis-cli\b[^|;&]*?\s(?-i:-a)\s+[\"']?(?!\$)[^\s\"']+"
    r"|\bsshpass\s+-p\s*[\"']?(?!\$)\S+"
    r"|\bauthorization\s*:\s*[a-z]+\s+[\"']?(?!\$)[^\s\"']{8,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|\b(?:ghp|gho|ghs|github_pat|glpat|xox[abps])[_-][A-Za-z0-9_-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|(?-i:(?<![\w-])sk-[A-Za-z0-9_-]{20,}|(?<![\w-])AIza[0-9A-Za-z_-]{30,})"
    r"|hooks\.slack\.com/(?:services|workflows|triggers)/|discord(?:app)?\.com/api/webhooks/"
    r"|api\.telegram\.org/bot\d+:|\.webhook\.office\.com/webhookb2/"
    r"|[?&](?:[a-z0-9]+[-_])*(?:key|code|sig|signature|apikey)=(?!\$)[^&\s#\"']{6,}"
    r"|://[^/\s:@]+:[^/\s@]+@)")
# NAME=value, --name=value and "name": value pairs whose NAME is a secret's (_secret_name): DB_PASS=, SECRET_KEY=,
# STRIPE_KEY=, *_CREDENTIALS=, the names the job env check refuses as well
_ASSIGN = re.compile(r"(?:^|(?<=[\s;&|(=\"'{,]))-{0,2}([A-Za-z_][A-Za-z0-9_.-]*)[\"']?\s*[=:]\s*[\"']?(?!\$)"
                     r"[^\s\"']{6,}")
_NAME_STRONG = re.compile(r"(?i)pass(?:word|wd|phrase)|secret|token|credential|private[_-]?key|api[_-]?key|"
                          r"access[_-]?key")
_NAME_LAST = {"pass", "pwd", "pw", "key", "auth", "apikey"}
_NAME_REF = ("file", "path", "dir", "url", "name")
_SECRET_NAME = re.compile(r"(?i)pass(?:word|wd)?|secret|token|api[_-]?key|private[_-]?key|credential")
_SCRIPT_EXT = re.compile(r"(?i)\.(?:sh|bash|zsh|ps1|psm1|py|js|mjs|cjs|ts|rb|pl|php|cmd|bat)$")
_ROLE_WORDS = {"helper": ["helper"], "helper only": ["helper"], "prod": ["prod"], "production": ["prod"],
               "production only": ["prod"], "both": ["helper", "prod"], "helper + production": ["helper", "prod"],
               "helper+production": ["helper", "prod"], "helper + prod": ["helper", "prod"],
               "helper+prod": ["helper", "prod"]}
_SCOPE_ALIASES = {**{w: k for k, w in oc.SCOPE_WORDS.items()}, "everything": "all", "lint + tests": "tests",
                  "read-only": "readonly", "nothing": "none"}


class InitError(Exception):
    """A usage or config error: exit 2, nothing saved. The text names the thing and the fix."""


class _NoProject(Exception):
    def __init__(self, start):
        super().__init__(str(start))
        self.start = Path(start)


class _PinError(Exception):
    pass


class Ctx:
    """Everything init reaches outside this module; tests pass fakes. run: subprocess.run stand-in (git, ssh -G,
    docker); discover / detect / journal: the modules (journal None = no journal); http(method, url, headers,
    timeout) -> (status, {lower-case header: value}, body) for the registry fallback; local_names() -> this machine's
    names and addresses; ssh_config: the ssh config whose Host aliases are offered (None = ~/.ssh/config); stdin: the
    answer stream for --apply (None = sys.stdin)."""

    def __init__(self, run=subprocess.run, env=None, discover=orch_discover, detect=orch_detect, journal=orch_journal,
                 http=None, local_names=None, ssh_config=None, stdin=None):
        self.run, self.od, self.det, self.journal = run, discover, detect, journal
        self.env = dict(os.environ if env is None else env)
        self.http = http or _http
        self.local_names = local_names or _local_names
        self.ssh_config_arg = str(ssh_config) if ssh_config else None
        self.ssh_config = Path(ssh_config) if ssh_config else Path.home() / ".ssh" / "config"
        self.stdin = stdin


# ----------------------------------------------------------------------------- output
def _say(line=""):
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # a Windows console code page cannot show every character
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


def details() -> dict:
    """The last cmd_init's {"state", "step", "applied", "warnings", "exit"} (+ "error"; + "answer_keys" for an --apply
    whose answer was a JSON object: its sorted top-level keys, a {"setup": {...}} wrapper counted as its inner keys, so
    the journal resolves a refused answer only by a later good answer to the same question); {} before the first
    call."""
    return copy.deepcopy(LAST)


class _Out:
    """Human lines go out as they happen; applied and warnings also land in the final JSON line. Problems go only in
    the JSON line (what, tried and fix are there once; a probe prints one status line per host)."""

    def __init__(self):
        self.applied, self.warnings = [], []
        self.answer_keys = None  # --apply: the answer's keys once it parsed as a JSON object (details())

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)
            _say(f"warning: {msg}")

    def final(self, st, rc=0, error=None):
        if error:
            print(f"orch init: {error}", file=sys.stderr, flush=True)
        js = {**st, "applied": list(self.applied), "warnings": list(self.warnings)}
        if error:
            js["error"] = error
        LAST.clear()
        LAST.update(state=st.get("state"), step=st.get("step"), applied=list(self.applied),
                    warnings=list(self.warnings), exit=rc, **({"error": error} if error else {}),
                    **({"answer_keys": list(self.answer_keys)} if self.answer_keys is not None else {}))
        _say(json.dumps(js, ensure_ascii=True))
        return rc


def _fallback(msg) -> dict:
    return {"state": "incomplete", "step": "fix", "summary": [msg], "problems": [], "ask": None, "next": "fix"}


# ----------------------------------------------------------------------------- small checks
def _one_line(v) -> bool:
    return isinstance(v, str) and v.strip() != "" and not re.search(r"[\r\n\x00]", v)


def _secret_name(name) -> bool:
    """Whether NAME in NAME=value (or a header name) is a secret's: it holds password, passphrase, secret, token,
    credential or an api/access/private key word, or its last part is pass, pwd, pw, key or auth (DB_PASS, STRIPE_KEY,
    apiKey, X-Auth). A last part ending in file, path, dir, url or name points at one (PGPASSFILE, SSH_KEY_FILE)."""
    parts = [x.lower() for x in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)]
    if not parts or parts[-1].endswith(_NAME_REF):
        return False
    return parts[-1] in _NAME_LAST or bool(_NAME_STRONG.search(name))


def _secret(text) -> bool:
    """Whether a command, URL or KEY=value holds a secret (_SECRET, plus NAME=value with a secret's NAME)."""
    return isinstance(text, str) and (bool(_SECRET.search(text))
                                      or any(_secret_name(m.group(1)) for m in _ASSIGN.finditer(text)))


def _strip_note(s) -> str:
    """'app1 (192.0.2.11:22)' (an option as offered) or 'h12 (deploy@192.0.2.12)' (a label) -> the first word."""
    s = str(s).strip()
    m = re.fullmatch(r"(\S+)\s*\([^()]*\)", s)
    return m.group(1) if m else s


def _specs(v, what) -> list:
    """A list (or comma-separated string) of what the user types after ssh -> validated specs."""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise InitError(f'{what} must be a list of what you type after ssh, e.g. ["deploy@192.0.2.10", "app1"] '
                        f"(got {v!r})")
    out = []
    for item in v:
        for s in item.split(","):
            s = _strip_note(s)
            if not s:
                continue
            try:
                out.append(oc.parse_spec(s)["spec"])
            except ValueError as e:
                raise InitError(f"{what}: {e}") from None
    return out


def _names(v, what) -> list:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise InitError(f'{what} must be a list of names, e.g. ["h12"] (got {v!r})')
    out = []
    for item in v:
        for s in re.sub(r"(?i)^\s*remove\s+", "", item).split(","):
            s = _strip_note(s)
            if s and s not in out:
                out.append(s)
    return out


def _count(v) -> int:
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_HELPERS:
        raise InitError(f"helper_count must be a whole number from 1 to {MAX_HELPERS} (got {v!r}); for 4+ ask the "
                        f"user how many")
    return v


def _urls(v, what) -> list:
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "-", "none")):
        return []
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list) or not all(isinstance(u, (str, dict)) for u in v):
        raise InitError(f'{what} must be a list of http(s) URLs, e.g. ["https://shop.example/health"] (got {v!r})')
    out = []
    for e in v:
        if isinstance(e, dict):  # {"url": ..., "insecure": true}: a self-signed internal certificate
            if not oc._health_ok(e) or what != "deploy.health":
                raise InitError(f'{what}: {e!r} must be a URL string' + (' or {"url": "https://...", "insecure": true}'
                                                                           if what == "deploy.health" else ""))
            if _secret(e["url"]):
                raise InitError(f"{what}: {e['url'].split('://')[0]}://... holds a credential (a password, a token or "
                                f"a key); orch saves and prints these URLs, so use one without credentials")
            if e not in out:
                out.append({"url": e["url"].strip(), "insecure": bool(e.get("insecure"))})
            continue
        u = e.strip()
        if not u:
            continue
        if not oc._url_ok(u):
            raise InitError(f"{what}: {u!r} is not an http(s) URL, e.g. https://shop.example/health")
        if _secret(u):
            raise InitError(f"{what}: {u.split('://')[0]}://... holds a credential (a password, a token or a key); "
                            f"orch saves and prints these URLs, so use one without credentials")
        if u not in out:
            out.append(u)
    return out


def _command(v, what, optional) -> str | None:
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "-", "none")):
        if optional:
            return None
        raise InitError(f"{what} is empty: give the command on one line, e.g. ./scripts/deploy.sh")
    if not _one_line(v) or len(v) > 2000:
        raise InitError(f"{what} must be one line of at most 2000 characters (got {v!r:.80})")
    if _secret(v):
        raise InitError(f"{what} seems to hold a secret; orch saves, prints and journals this command, so read the "
                        f"secret from a file or an environment variable inside your script instead")
    return v.strip()


def _ref_parts(ref) -> tuple:
    """Image reference -> (registry, repository, tag, digest); ValueError when it is not one."""
    if not isinstance(ref, str) or not ref or len(ref) > 512 or not re.fullmatch(r"[A-Za-z0-9._/:@-]+", ref):
        raise ValueError(f"{ref!r} is not an image reference, e.g. python:3.12-slim")
    reg, rest = "docker.io", ref
    first, sep, after = ref.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?", first):
            raise ValueError(f"{ref!r}: {first!r} is not a registry host, e.g. ghcr.io or registry.example:5000")
        reg, rest = first.lower(), after
    m = _REF.fullmatch(rest)
    if not m:
        raise ValueError(f"{ref!r} is not an image reference (lower-case name, optional :tag and @sha256:digest), "
                         f"e.g. python:3.12-slim")
    repo = m["name"]
    if reg == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return reg, repo, m["tag"] or "latest", m["digest"]


def _mem_bytes(m):
    mm = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)b?\s*", str(m).lower()) if isinstance(m, (str, int)) \
        and not isinstance(m, bool) else None
    return int(float(mm.group(1)) * 1024 ** "_kmgt".index(mm.group(2) or "_")) if mm else None


def _check_job(n, j, pol, p, out):
    """One job's keys within the validator limits; InitError naming every problem."""
    kind, st, errs = j.get("kind") or "container", j.get("stage"), []
    lim = pol.get("limits") or {}
    if kind not in ("container", "build"):
        errs.append("kind must be container or build")
    if st is not None and st not in oc.STAGES:
        errs.append(f"stage must be one of {', '.join(oc.STAGES)}")
    if kind == "build" and st not in (None, "image"):
        errs.append("a build job (kind build) has stage image")
    if kind == "container":
        if st == "image":
            errs.append("stage image is for image builds (kind build); a container job uses lint, unit, build, e2e "
                        "or investigate")
        if not j.get("image"):
            errs.append("image is missing, e.g. python:3.12-slim")
        else:
            try:
                _ref_parts(j["image"])
            except ValueError as e:
                errs.append(f"image {e}")
        if not _one_line(j.get("cmd")):
            errs.append("cmd must be the test command on one line, e.g. pytest -q")
    for k in ("prep", "cmd"):
        if j.get(k) is not None and (not _one_line(j[k]) or _secret(j[k])):
            errs.append(f"{k} must be one line without secrets")
    for k in ("dir", "repo", "context", "dockerfile"):
        if j.get(k) is not None:
            try:
                oc._relpath(n, k, j[k])
            except oc.ConfigError as e:
                errs.append(str(e).split(": ", 1)[-1])
    if "memory" in j:
        b, cap = _mem_bytes(j["memory"]), lim.get("memory_max", "6g")
        if b is None:
            errs.append(f"memory {j['memory']!r} must look like 512m or 3g")
        elif not 16 * 2 ** 20 <= b <= (_mem_bytes(cap) or b):
            errs.append(f"memory {j['memory']} must be from 16m to {cap} (policy limits.memory_max)")
    tmax = lim.get("timeout_max", 3600)
    for k in ("timeout", "prep_timeout"):
        v = j.get(k)
        if k in j and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= tmax):
            errs.append(f"{k} must be whole seconds from 1 to {tmax} (policy limits.timeout_max)")
    if "cpus" in j and (isinstance(j["cpus"], bool) or not isinstance(j["cpus"], (int, float))
                        or not 0 < j["cpus"] <= 64):
        errs.append("cpus must be a number above 0 and at most 64")
    if "pids" in j and (isinstance(j["pids"], bool) or not isinstance(j["pids"], int)
                        or not 16 <= j["pids"] <= 1_000_000):
        errs.append("pids must be a whole number from 16 to 1000000")
    c = j.get("cache")
    if c is not None and not (isinstance(c, dict) and set(c) <= {"name", "path"}
                              and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,40}", str(c.get("name", "")))
                              and str(c.get("path", "")).startswith("/")):
        errs.append('cache must be {"name": "npm", "path": "/root/.npm"} (a lower-case name and an absolute path)')
    env = j.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
                                                 and isinstance(v, str) for k, v in env.items()):
            errs.append('env must be {"NAME": "value"} with string values')
        elif any((_SECRET_NAME.search(k) and v and not re.search(r"_(FILE|PATH|DIR)$", k, re.I))
                 or _secret(f"{k}={v}") for k, v in env.items()):
            errs.append("env must not hold secrets (a job's env is saved and shown); give the job no secret")
    fp = j.get("fail_patterns")
    if fp is not None:
        if not isinstance(fp, list) or not all(isinstance(x, str) for x in fp):
            errs.append("fail_patterns must be a list of regular expressions")
        else:
            for x in fp:
                try:
                    re.compile(x)
                except re.error as e:
                    errs.append(f"fail_patterns: {x!r} is not a valid regular expression ({e})")
    rp = j.get("result_paths")
    if rp is not None and (not isinstance(rp, list) or not all(isinstance(x, str) and x.startswith("/") for x in rp)):
        errs.append('result_paths must be a list of absolute paths in the container, e.g. ["/out"]')
    if errs:
        raise InitError(f"job {n}: " + "; ".join(errs))
    if kind == "container" and j.get("dir"):
        base = Path(p.root) / (j.get("repo") or ".") / str(j["dir"])
        if not base.is_dir():
            out.warn(f"job {n}: folder {j['dir']} does not exist in {Path(p.root).as_posix()}; the job fails until "
                     f"it does")


def _local_names() -> set:
    """This machine's host names and addresses (no network: the UDP connect sends nothing)."""
    names = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
    try:
        hn = socket.gethostname().lower()
        names |= {hn, hn.split(".")[0]}
        names |= set(socket.gethostbyname_ex(hn)[2])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            names.add(s.getsockname()[0])
    except OSError:
        pass
    return names


def _is_local(hostname, names) -> bool:
    h = str(hostname or "").strip().strip("[]").lower()
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_loopback or ip.is_unspecified:
            return True
    except ValueError:
        pass
    return h in {str(x).lower() for x in names}


# ----------------------------------------------------------------------------- project, state
def _git_warning(start) -> str:
    s = Path(start).as_posix()
    return (f"{s} is not a git repository, so orch keeps this setup in {s}/.orch (with a .gitignore that keeps it "
            f"out of git); do not share or copy this folder with .orch in it")


def _project(a, ctx, create):
    """-> (Project, store exists, warnings). --project beats ORCH_PROJECT beats the current folder. A git repo is a
    project; a folder outside git only when named with --project or ORCH_PROJECT. create makes the store
    (orch_config.init_project); otherwise nothing is created. Raises _NoProject or ConfigError."""
    env = dict(ctx.env)
    proj = getattr(a, "project", None)
    if proj:
        env.pop("ORCH_PROJECT", None)
    explicit = bool(proj or env.get("ORCH_PROJECT"))
    start = oc._start_dir(proj, env)
    p = oc.find_project(start, env, ctx.run)
    if p is not None:
        return p, True, []
    g = oc._git_dirs(start, ctx.run)
    if g is None and not explicit:
        raise _NoProject(start)
    if create:
        p, warns = oc.init_project(start, env, ctx.run)
        return p, True, warns
    if g:
        return oc.Project(g[0], g[1] / "orch", True), False, []
    return oc.Project(start, start / ".orch", False), False, [_git_warning(start)]


def _load(p, home):
    cfg = oc.load_config(p)
    sh = oc.load_state(p, "hosts", {})
    sh = {n: r for n, r in sh.items() if isinstance(r, dict)} if isinstance(sh, dict) else {}
    return cfg, sh, oc.trust_get(home, p)


def _lock(p):
    return oc._locked(Path(p.store) / "config.json")


def _guard_view(ctx, home, out):
    try:
        oc.write_guard_view(home, run=ctx.run, env=ctx.env, ssh_config=ctx.ssh_config_arg)
    except (oc.ConfigError, OSError) as e:
        out.warn(f"the guard view was not written: {e}")


def _journal(ctx, p, whats, why="user answer"):
    j = ctx.journal
    if j is None or ctx.env.get("ORCH_JOURNAL") == "0":
        return
    for what in whats:
        try:
            j.add(p, {"type": "decision", "what": what, "why": why}, by="orch")
        except Exception as e:  # noqa: BLE001 - the journal never fails init
            print(f"orch init: journal not written: {type(e).__name__}: {e}", file=sys.stderr)
            return


def _no_project(ctx, out, start, rc, create=False):
    s = Path(start).as_posix()
    msg = f"no project here ({s}): cd into your project or pass --project DIR"
    if create:
        msg += (f"; a folder outside git becomes a project only when named (--project {s}), and orch then keeps its "
                f"setup in {s}/.orch")
    home = oc.home(ctx.env)
    trust = oc._read_json(home / "trust.json", {}, strict=False)
    if (home / "guard-view.json").exists() or (isinstance(trust, dict) and trust):
        _guard_view(ctx, home, out)  # repairs a corrupt view; never creates ORCH_HOME
    if not rc:
        _say(msg)
    st = {"state": "new", "step": "fix", "summary": [msg], "ask": None, "next": "fix",
          "problems": [{"host": "project", "what": f"no project here ({s})",
                        "tried": shlex.join(["git", "-C", s, "rev-parse", "--show-toplevel"]),
                        "fix": "cd into your project or pass --project DIR"}]}
    return out.final(st, rc, error=msg if rc else None)


# ----------------------------------------------------------------------------- probe
def _probe_line(n, h, rec) -> str:
    lab = oc._label(n, h)
    if not rec.get("ok"):
        return f"{lab} problem: {(rec.get('problem') or {}).get('class') or 'other'} (see problems)"
    f = rec.get("facts") or {}
    if not f:
        return f"{lab} ok: reachable over ssh (production only; Docker not probed)"
    bits = [" ".join(str(x) for x in (f.get("os"), f.get("arch")) if x)]
    if f.get("cpus"):
        bits.append(f"{f['cpus']} cpu")
    if f.get("mem_bytes"):
        bits.append(f"{max(1, round(f['mem_bytes'] / 2 ** 30))} GB")
    if f.get("docker"):
        bits.append(f"docker {f['docker']}")
    co = f.get("cotenants")
    bits.append("other containers unknown" if co is None else "no other containers" if not co
                else f"{len(co)} other container{'s' if len(co) != 1 else ''}")
    if rec.get("transport") == "ssh-docker":
        bits.append("docker over ssh" + ("" if rec.get("docker_host") else ", no image builds"))
    return f"{lab} ok: " + ", ".join(b for b in bits if b)


def _context_users(home, p, name) -> list:
    """Roots of the other registered projects whose saved probe state uses docker context `name`. Such a context is
    never changed or removed from this project; an unreadable registry counts as a user (nothing is touched)."""
    if not name:
        return []
    try:
        reg, trust = oc.registry_load(home), oc._trust_all(home)
    except oc.ConfigError:
        return ["another project (the registry or the trust file cannot be read)"]
    mine, out = {oc._key(p.store): True}, []
    for key in dict.fromkeys(list(reg.get("projects") or []) + list(trust)):
        if oc._find_key(mine, oc._key(key)):
            continue
        st = oc._read_json(Path(key) / "state" / "hosts.json", {}, strict=False)
        if isinstance(st, dict) and any(isinstance(r, dict) and r.get("context") == name for r in st.values()):
            entry = trust.get(oc._find_key(trust, key) or key)
            out.append(str((entry or {}).get("root") or Path(key).parent.parent.as_posix()))
    return out


def _drop_context(ctx, out, n, name):
    try:
        ctx.od.remove_context(n, run=ctx.run, context=name)
    except (orch_discover.DiscoverError, ValueError) as e:
        if getattr(e, "kind", "") != "missing":
            out.warn(f"the docker context {name} of {n} was not removed: {e}")


def _probe(ctx, p, home, out, cfg, sh, names, before=None) -> dict:
    """Probe the saved hosts `names` in parallel (helpers with Docker, production-only hosts ssh only), choose each
    helper's transport and set up its docker context: "orch-<name>", or the endpoint-specific one when another
    project's orch-<name> points elsewhere (orch_discover.ensure_context). The context name is saved in the state, and
    only this project's own earlier context (in sh, or in before: {name: context} from before an ssh change dropped
    the record), used by no other project, is ever updated or removed. Updates sh; returns the new records."""
    hosts = cfg.get("hosts") or {}
    specs = {n: hosts[n]["ssh"] for n in names if n in hosts}
    helpers = {n for n in specs if "helper" in oc._roles(hosts[n])}
    if not specs:
        return {}
    recs = ctx.od.probe_hosts(specs, helpers, run=ctx.run)
    master = ctx.od.master_facts(run=ctx.run) if helpers else {}
    got = {}
    for n in specs:
        old = (sh.get(n) or {}).get("context") or (before or {}).get(n)
        rec = {**(recs.get(n) or {"ok": False, "spec": specs[n], "facts": {}, "resolved": None,
                                  "problem": {"class": "other", "what": "the probe returned nothing",
                                              "tried": "-", "fix": "run orch init again"}}),
               "transport": None, "context": None, "docker_host": None}
        # another orch namespace's objects: not ok (no jobs, not ready), but Docker answers, so the transport is kept
        # and gc and stop-all still reach this master's own objects there
        foreign = (rec.get("problem") or {}).get("class") == "foreign-namespace"
        if n in helpers and (rec.get("ok") or foreign):
            tr = ctx.od.choose_transport(dict(rec, ok=True), master, n)
            if "problem" in tr:  # reachable, but orch cannot drive its Docker from here
                rec.update(ok=False, problem=tr["problem"])
            else:
                rec.update(tr)
                if rec["transport"] == "context":
                    own = {old} if old and not _context_users(home, p, old) else set()
                    try:
                        rec["context"] = ctx.od.ensure_context(n, specs[n], run=ctx.run, own=own)
                    except orch_discover.DiscoverError as e:
                        rec.update(ok=False, context=None, problem={
                            "class": "other", "what": f"the docker context for {n} could not be set up: {e}",
                            "tried": f"docker context inspect orch-{n}",
                            "fix": "check the docker CLI on this machine (docker context ls must work), then run "
                                   "orch init again"})
                    else:
                        if old in own and rec["context"] != old:  # this project's old context is nobody's now
                            _drop_context(ctx, out, n, old)
        got[n] = sh[n] = rec
    return got


def _note_ids(home, recs, cfg, out):
    """Registry identities follow the probes: a docker ID learnt, a changed ID or address reported."""
    hosts = cfg.get("hosts") or {}
    ok = {n: r for n, r in recs.items() if r.get("ok") and (r.get("resolved") or {}).get("hostname")}
    if not ok:
        return
    with oc._locked(Path(home) / "registry.json"):
        reg, changed = oc.registry_load(home), False
        for n, rec in ok.items():
            res, did = rec["resolved"], (rec.get("facts") or {}).get("docker_id") or None
            key = (str(res["hostname"]).lower(), int(res.get("port") or 22))
            node = reg["nodes"].get(n)
            if node is None:
                reg["nodes"][n] = {"hostname": key[0], "port": key[1], "docker_id": did}
                changed = True
                continue
            if (str(node.get("hostname") or "").lower(), int(node.get("port") or 22)) != key:
                out.warn(f"{oc._label(n, hosts.get(n))} now reaches {key[0]}:{key[1]}, but this master knows {n} as "
                         f"{node.get('hostname')}:{node.get('port') or 22}; if that is another machine, remove {n} "
                         f"(remove_hosts) and add it again")
            elif did and node.get("docker_id") != did:
                if node.get("docker_id"):
                    out.warn(f"{oc._label(n, hosts.get(n))}: its Docker engine ID changed ({node['docker_id'][:12]} "
                             f"-> {did[:12]}): Docker was reinstalled, or another machine took that address")
                node["docker_id"] = did
                changed = True
        if changed:
            oc.registry_save(home, reg)


# ----------------------------------------------------------------------------- jobs
def _stored(p):
    d = oc.load_state(p, "jobs-detected")
    return d if isinstance(d, dict) and isinstance(d.get("jobs"), dict) else None


def _project_name(p) -> str:
    """The name detection gives the project's root jobs (<name>-unit ...). Saved in state/project.json (shared by
    every worktree) when jobs are first saved; before that, and for setups saved earlier, the main worktree's folder
    name, so a linked worktree never renames the saved jobs."""
    saved = oc.load_state(p, "project")
    if isinstance(saved, dict) and isinstance(saved.get("name"), str) and saved["name"].strip():
        return saved["name"].strip()
    if not p.git:
        return Path(p.root).name
    common = Path(p.store).parent  # <main worktree>/.git, or a bare repository (shop.git)
    name = common.parent.name if common.name.lower() == ".git" else re.sub(r"(?i)\.git$", "", common.name)
    return name or Path(p.root).name


def _detect(ctx, p, out):
    """Fresh detection, stored as the proposal; None (and a warning) when the detector fails."""
    try:
        res = ctx.det.detect(p.root, project_name=_project_name(p), run=ctx.run)
    except Exception as e:  # noqa: BLE001 - a detector bug must not stop the interview
        out.warn(f"job detection failed in {Path(p.root).as_posix()} ({type(e).__name__}: {e}); tell orch the jobs "
                 f"with orch init --apply, or run orch init --detect again")
        return None
    oc.save_state(p, "jobs-detected", res)
    return res


def _ship_warning(p, jobs, out):
    """Jobs ship a commit with git archive: in a folder outside git only jobs with their own repo can run."""
    if p.git:
        return
    bad = sorted(n for n, j in (jobs or {}).items() if isinstance(j, dict) and (j.get("repo") or ".") in (".", "./"))
    if bad:
        out.warn(f"{Path(p.root).as_posix()} is not a git repository, and orch ships a job's files with git archive, "
                 f"so {', '.join(bad)} cannot run until you run git init and commit there (or give each job a repo: "
                 f"a git repository under the project)")


def _definition(j) -> dict:
    """The part of a job detection decides; an image counts without its pinned digest."""
    d = {}
    for k in DEF_KEYS:
        v = j.get(k)
        if k == "image" and isinstance(v, str):
            v = v.split("@", 1)[0]
        elif k == "stage":
            v = oc._stage(j)
        elif k == "kind":
            v = v or "container"
        elif k in ("dir", "repo", "context") and isinstance(v, str):
            v = v.replace("\\", "/").strip("/")
            v = None if v in ("", ".") else v
        d[k] = None if v in (None, "", [], {}) else v
    return d


def _renamed(cfg, fresh) -> dict:
    """{fresh name: saved name} for a detector-made saved job that detection now proposes under another name with
    the same definition (another worktree folder, a renamed project folder): that is the same job, not one gone and
    one new."""
    cj, dj, out = cfg.get("jobs") or {}, (fresh or {}).get("jobs") or {}, {}
    for n, j in cj.items():
        if not isinstance(j, dict) or not j.get("manifest") or n in dj:
            continue
        m = next((m for m, d in dj.items() if m not in cj and m not in out and isinstance(d, dict)
                  and _definition(d) == _definition(j)), None)
        if m:
            out[m] = n
    return out


def _drift_view(cfg, fresh):
    """A fresh detection as status() should see it: a job detection now proposes under another name with the same
    definition keeps its saved name, and a saved job whose files changed while the job detection would propose
    stayed the same keeps its saved manifest, so only real changes ask the user."""
    if not isinstance(fresh, dict):
        return fresh
    eff = copy.deepcopy(fresh)
    dj = eff.get("jobs") or {}
    for m, n in _renamed(cfg, fresh).items():
        dj[n] = dj.pop(m)
    for n, j in (cfg.get("jobs") or {}).items():
        d = dj.get(n)
        if j.get("manifest") and d and d.get("manifest") != j["manifest"] and _definition(j) == _definition(d):
            d["manifest"] = j["manifest"]
    return eff


def _diff_lines(cfg, fresh) -> list:
    cj, dj, out = cfg.get("jobs") or {}, (fresh or {}).get("jobs") or {}, []
    for n, j in cj.items():
        if not j.get("manifest"):
            continue  # made by the user, not by detection
        if n not in dj:
            out.append(f"gone: {n} (no longer detected)")
            continue
        a, b = _definition(j), _definition(dj[n])
        ch = [f"{k} {json.dumps(a[k]) if isinstance(a[k], dict) else a[k] or '-'} -> "
              f"{json.dumps(b[k]) if isinstance(b[k], dict) else b[k] or '-'}" for k in DEF_KEYS if a[k] != b[k]]
        if ch:
            out.append(f"changed: {n}: " + "; ".join(ch))
    out += [f"new: {n}" for n in dj if n not in cj]
    return out


def _detection(ctx, p, cfg, out):
    """(detected for status, fresh?). Saved detector-made jobs -> a fresh detection (stored) through the definition
    filter; otherwise the stored proposal."""
    if any(isinstance(j, dict) and j.get("manifest") for j in (cfg.get("jobs") or {}).values()):
        fresh = _detect(ctx, p, out)
        if fresh is not None:
            return _drift_view(cfg, fresh), True
    return _stored(p), False


# ----------------------------------------------------------------------------- status
def _candidates(ctx, cfg, sh, home) -> list:
    """Up to 4 "spec (hostname:port)" options for the helpers step: helpers of other approved projects on this
    master first, then ~/.ssh/config Host aliases that set a HostName; never a host this project already has."""
    hosts = cfg.get("hosts") or {}
    taken = {((r.get("resolved") or {}).get("hostname", "").lower(), int((r.get("resolved") or {}).get("port") or 22))
             for r in sh.values() if (r.get("resolved") or {}).get("hostname")}
    typed = {h.get("ssh") for h in hosts.values()}
    out = []
    try:
        reg, trust = oc.registry_load(home), oc._trust_all(home)
    except oc.ConfigError:
        reg, trust = {"nodes": {}}, {}
    specs = {}
    for entry in trust.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("config"), dict):
            continue
        for n, h in (entry["config"].get("hosts") or {}).items():
            if isinstance(h, dict) and "helper" in oc._roles(h) and h.get("ssh"):
                specs.setdefault(n, h["ssh"])
    for n, ident in (reg.get("nodes") or {}).items():
        if not isinstance(ident, dict) or not str(ident.get("port") or 22).isdigit():
            continue
        key = (str(ident.get("hostname") or "").lower(), int(ident.get("port") or 22))
        if n in hosts or n not in specs or key in taken or specs[n] in typed:
            continue
        out.append(f"{specs[n]} ({key[0]}:{key[1]})")
        taken.add(key)
    if len(out) < 4:
        aliases = [x for x in oc._ssh_config_hosts(ctx.ssh_config, ctx.ssh_config.parent) if x not in typed][:12]
        if aliases:
            local = ctx.local_names()
            with cf.ThreadPoolExecutor(max_workers=min(8, len(aliases))) as ex:
                found = list(ex.map(lambda x: oc._ssh_g(ctx.run, x, ctx.ssh_config_arg), aliases))
            for alias, r in zip(aliases, found):
                if not r or r[0] == alias.lower() or (r[0], r[1]) in taken or _is_local(r[0], local):
                    continue
                out.append(f"{alias} ({r[0]}:{r[1]})")
                taken.add((r[0], r[1]))
    return out[:4]


def _mprod(ctx, home, p) -> dict:
    """The production servers of the OTHER approved setups on this master (orch_config.master_prod). This project's
    own approval is left out by its store, so every worktree of this repository is this project."""
    return oc.master_prod(home, run=ctx.run, exclude=p.store)


def _settle(ctx, out, p, cfg, sh, trust, home, detected, fresh):
    """orch_config.status plus what that pure function cannot do itself: the production servers of the approved
    setups on this master (master_prod: a helper another project deploys to gets production's defaults), a fresh job
    proposal when the jobs step opens (stored, so {"jobs": "detected"} takes exactly what the user saw) and candidates
    for the helpers step."""
    mp = _mprod(ctx, home, p)
    st = oc.status(p, cfg, sh, trust, detected, master_prod=mp)
    if st["step"] == "jobs" and "jobs" not in cfg and not fresh:
        d = _detect(ctx, p, out)
        if d is not None:
            detected = d
            st = oc.status(p, cfg, sh, trust, detected, master_prod=mp)
    if st["step"] == "helpers":
        c = _candidates(ctx, cfg, sh, home)
        if c:
            st = oc.status(p, cfg, sh, trust, detected, c, master_prod=mp)
    return st


# ----------------------------------------------------------------------------- --apply
class _Apply:
    """Applies one answer to a copy of the config. Specs resolve with ssh -G (local), machines merge by identity
    (resolved hostname + port), new hosts get registry names. Raises InitError before anything is saved."""

    def __init__(self, ctx, out, p, cfg, sh, home):
        self.ctx, self.out, self.p, self.home = ctx, out, p, Path(home)
        # sh is the answer's own copy: host_ssh drops the old spec's record from it, and the cleanup still needs the
        # probe record from before the answer (Q1)
        self.old, self.cfg, self.sh = cfg, copy.deepcopy(cfg), dict(sh)
        self.cfg.setdefault("hosts", {})
        self.reg, self.reg_changed = None, False
        self.res, self.idents = {}, {}
        self.removed, self.unhelped, self.probe, self.touched = {}, set(), set(), set()
        self.local = None
        self.mprod = {}  # (hostname, port) -> project roots whose approved setup deploys there (orch_config)

    # -- identity
    def resolve(self, spec):
        if spec not in self.res:
            try:
                self.res[spec] = self.ctx.od.ssh_resolve(spec, run=self.ctx.run)
            except orch_discover.DiscoverError as e:
                raise InitError(str(e)) from None
        r = self.res[spec]
        return (str(r["hostname"]).strip("[]").lower(), int(r.get("port") or 22))

    def ident(self, n, h, strict=False):
        if n not in self.idents:
            rec = self.sh.get(n) or {}
            res, node = rec.get("resolved") or {}, self.reg["nodes"].get(n) or {}
            if res.get("hostname") and rec.get("spec") == h.get("ssh"):
                key = (str(res["hostname"]).lower(), int(res.get("port") or 22))
            elif node.get("hostname"):
                key = (str(node["hostname"]).lower(), int(node.get("port") or 22))
            else:
                try:
                    key = self.resolve(h["ssh"])
                except InitError:
                    if strict:
                        raise
                    key = None
            self.idents[n] = key
        return self.idents[n]

    def label(self, n):
        return oc._label(n, self.cfg["hosts"].get(n) or self.removed.get(n))

    def prod_roots(self, n) -> list:
        """The other projects whose approved setup makes host n's machine a production server (self.mprod already
        leaves this project's store out, so no worktree of it counts)."""
        key = self.ident(n, self.cfg["hosts"][n])
        mine = os.path.normcase(Path(self.p.root).as_posix())
        return [str(r) for r in (self.mprod.get(key) or []) if os.path.normcase(Path(r).as_posix()) != mine] \
            if key else []

    def prod_scope(self, n, h):
        """A helper that just became production too: a job scope of everything falls back to production's default,
        read-only commands only (oc.PROD_SCOPE); the warning names the answer that allows lint + tests."""
        if h.get("jobs") != "all":
            return
        h["jobs"] = oc.PROD_SCOPE
        self.out.warn(f"{self.label(n)} is also a production server now, so its jobs are "
                      f"{oc.SCOPE_WORDS[oc.PROD_SCOPE]} (was everything); answer jobs tests to run lint + tests there")

    def clamp_prod(self, n):
        """Production rules win on a machine another project deploys to (design section 3): as a helper it is saved
        as helper + production, and a job scope of everything becomes production's default, read-only commands only
        (tests stays when the user chose it)."""
        h = self.cfg["hosts"][n]
        roots = self.prod_roots(n) if "helper" in oc._roles(h) else []
        if not roots:
            return
        was = (oc._roles(h), h.get("jobs"))
        h["roles"] = ["helper", "prod"]
        if h.get("jobs") == "all":
            h["jobs"] = oc.PROD_SCOPE
        if (oc._roles(h), h.get("jobs")) != was:
            self.out.warn(f"{self.label(n)} is a production server of {', '.join(roots)}, so this project saves it "
                          f"as helper + production: {oc.SCOPE_WORDS[oc._scope(h)]} (production rules win"
                          + ("" if h.get("jobs") == "tests" else "; answer jobs tests to run lint + tests there")
                          + ")")

    def place(self, spec, role, seen):
        """spec -> (name, added?): the saved host with the same identity gains role, else a new registry-named host.
        A helper that another approved project deploys to is clamped to helper + production (clamp_prod)."""
        n, added = self._place(spec, role, seen)
        if role == "helper":
            self.clamp_prod(n)
        return n, added

    def _place(self, spec, role, seen):
        key = self.resolve(spec)
        hosts = self.cfg["hosts"]
        if role == "helper":
            if self.local is None:
                self.local = self.ctx.local_names()
            if _is_local(key[0], self.local):
                raise InitError(f"helpers: {spec} is this machine ({key[0]}); a helper must be another server, so give "
                                f"what you type after ssh for that server")
        for n, h in hosts.items():
            if self.ident(n, h) != key:
                continue
            if h["ssh"] != spec:
                self.out.warn(f"{spec} and {h['ssh']} are the same machine ({key[0]}:{key[1]}): saved once, as "
                              f"{self.label(n)}")
            elif key in seen:
                self.out.warn(f"{spec} was given twice: saved once, as {self.label(n)}")
            roles = oc._roles(h)
            if role not in roles:
                h["roles"] = [r for r in oc.ROLES if r in roles or r == role]
                if role == "helper":
                    self.probe.add(n)
                else:
                    self.prod_scope(n, h)
            seen[key] = n
            return n, False
        name = oc.assign_name(self.reg, {"hostname": key[0], "port": key[1], "docker_id": None})
        self.reg_changed = True
        if name in hosts:
            raise InitError(f"the registry ({self.home.as_posix()}/registry.json) names {spec} {name}, which is "
                            f"another saved host; remove {name} (remove_hosts) or fix the registry, then retry")
        hosts[name] = {"ssh": spec, "roles": [role]}
        self.idents[name] = key
        self.probe.add(name)
        seen[key] = name
        return name, True

    # -- answer keys (each returns the "applied" text, or None)
    def a_remove_hosts(self, v, ans):
        hosts, done = self.cfg["hosts"], []
        for n in _names(v, "remove_hosts"):
            if n not in hosts:
                self.out.warn(f"remove_hosts: {n} is not a saved host (saved: {', '.join(hosts) or 'none'}); nothing "
                              f"removed")
                continue
            done.append(oc._label(n, hosts[n]))
            self.removed[n] = hosts.pop(n)
        return "removed " + ", ".join(done) if done else None

    def a_helper_count(self, v, ans):
        n = _count(v)
        self.cfg.setdefault("pending", {})["helper_count"] = n
        have = sum(1 for h in self.cfg["hosts"].values() if "helper" in oc._roles(h))
        if have > n and "helpers" not in ans:
            self.out.warn(f"{have} helpers are saved already, more than {n}; remove the extra ones with remove_hosts")
        return f"{n} helper{'s' if n != 1 else ''}"

    def a_helpers(self, v, ans):
        specs = _specs(v, "helpers")
        if not specs:
            raise InitError('helpers is empty: give what you type after ssh for each helper, e.g. ["deploy@192.0.2.10"]')
        count = _count(ans["helper_count"]) if "helper_count" in ans else \
            (self.cfg.get("pending") or {}).get("helper_count")
        have0 = sum(1 for h in self.cfg["hosts"].values() if "helper" in oc._roles(h))  # saved before this answer
        new = count - have0 if isinstance(count, int) else 0
        if isinstance(count, int) and len(specs) not in (count, new):  # the whole list, or only the new helpers
            many = f"helpers lists {len(specs)} ssh target{'s' if len(specs) != 1 else ''} but helper_count is {count}"
            if 0 < new < count:
                raise InitError(f"{many} and {have0} helper{'s are' if have0 != 1 else ' is'} saved already; give "
                                f"only the {new} new one{'s' if new != 1 else ''}, or all {count}, or change "
                                f"helper_count")
            raise InitError(f"{many}; give one per helper ({count} in all), or change helper_count")
        seen, parts = {}, []
        for s in specs:
            n, added = self.place(s, "helper", seen)
            line = f"{self.label(n)} {'added' if added else 'already saved'}"
            if line not in parts and f"{self.label(n)} added" not in parts:
                parts.append(line)
        have = sum(1 for h in self.cfg["hosts"].values() if "helper" in oc._roles(h))
        pend = self.cfg.get("pending") or {}
        if not isinstance(count, int) or have >= count:  # answered; else (machines merged) the question comes back
            pend.pop("helper_count", None)
        if not pend:
            self.cfg.pop("pending", None)
        elif have < count:
            self.out.warn(f"{have} different helper{'s' if have != 1 else ''} saved, {count} wanted: name "
                          f"{count - have} more (or answer a smaller helper_count)")
        return "; ".join(parts)

    def a_hosts(self, v, ans):
        if not isinstance(v, dict) or not v:
            raise InitError('hosts must be an object of host name -> {"roles", "jobs", "watch"}, e.g. '
                            '{"h11": {"roles": ["helper"], "jobs": "all"}}')
        hosts, parts = self.cfg["hosts"], []
        for n, d in v.items():
            n = _strip_note(n)  # "h12 (deploy@192.0.2.12)" as printed
            if n not in hosts:
                raise InitError(f"hosts: {n} is not a saved host (saved: {', '.join(hosts) or 'none'}); add a server "
                                f"with helpers or production first")
            if not isinstance(d, dict):
                raise InitError(f'hosts.{n} must be an object, e.g. {{"roles": ["helper"], "jobs": "all"}}')
            bad = [k for k in d if k not in HOST_ANSWER_KEYS]
            if bad:
                raise InitError(f"hosts.{n}: unknown key {bad[0]!r} (valid: {', '.join(HOST_ANSWER_KEYS)})")
            h = hosts[n]
            if "ssh" in d:
                self.host_ssh(n, h, d["ssh"])
            was = oc._roles(h)
            if "roles" in d:
                h["roles"] = self.roles(d["roles"], n)
                if "helper" in h["roles"] and "helper" not in was:
                    self.probe.add(n)
                if "helper" not in h["roles"]:
                    h.pop("jobs", None)
                    if "helper" in was:
                        self.unhelped.add(n)
            roots = self.prod_roots(n) if "helper" in h["roles"] else []
            if roots and "prod" not in h["roles"]:
                h["roles"] = ["helper", "prod"]
                self.out.warn(f"{self.label(n)} is a production server of {', '.join(roots)}, so it stays helper + "
                              f"production here (production rules win)")
            if "jobs" in d:
                if "helper" not in h["roles"]:
                    raise InitError(f"hosts.{n}: {self.label(n)} is production only, so it runs no jobs; give it "
                                    f'roles ["helper", "prod"] to run jobs there')
                h["jobs"] = self.scope(d["jobs"], n)
                if h["jobs"] == "all" and "prod" in h["roles"]:
                    where = f" (of {', '.join(roots)})" if roots else ""
                    raise InitError(f"hosts.{n}: {self.label(n)} is also a production server{where}, so it runs at "
                                    f"most lint + tests; answer jobs tests, readonly or none")
            elif "prod" in h["roles"]:
                self.prod_scope(n, h)
            if "watch" in d:
                urls = _urls(d["watch"], f"hosts.{n}.watch")
                if urls:
                    h["watch"] = urls
                else:
                    h.pop("watch", None)
            words = oc._role_words(oc._roles(h))
            if "helper" in oc._roles(h) and "jobs" in h:
                words += f", {oc.SCOPE_WORDS[h['jobs']]}"
            parts.append(f"{self.label(n)} {words}" + (f", watch {', '.join(h['watch'])}" if h.get("watch") else ""))
        return "; ".join(parts)

    def host_ssh(self, n, h, v):
        specs = _specs(v, f"hosts.{n}.ssh")
        if len(specs) != 1:
            raise InitError(f"hosts.{n}.ssh must be one ssh target, e.g. deploy@192.0.2.10")
        spec = specs[0]
        if spec == h["ssh"]:
            return
        old, key = self.ident(n, h), self.resolve(spec)
        if old and key != old:
            raise InitError(f"hosts.{n}.ssh: {spec} reaches {key[0]}:{key[1]}, but {self.label(n)} is {old[0]}:"
                            f"{old[1]}: another machine; add it with helpers or production and remove {n} with "
                            f"remove_hosts")
        h["ssh"] = spec
        self.idents[n] = key
        self.probe.add(n)
        self.sh.pop(n, None)  # observations of the old spec

    @staticmethod
    def roles(v, n):
        if isinstance(v, str):
            r = _ROLE_WORDS.get(re.sub(r"\s+", " ", v.strip().lower()))
            if r:
                return list(r)
            v = [v]
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            got = {"production" if x.strip().lower() == "prod" else x.strip().lower() for x in v}
            if got <= {"helper", "production"}:
                return [r for r in oc.ROLES if (r == "helper" and "helper" in got) or (r == "prod" and "production" in got)]
        raise InitError(f'hosts.{n}.roles must be ["helper"], ["prod"] or ["helper", "prod"] (got {v!r})')

    @staticmethod
    def scope(v, n):
        s = re.sub(r"\s+", " ", v.strip().lower()) if isinstance(v, str) else None
        s = s if s in oc.SCOPES else _SCOPE_ALIASES.get(s)
        if not s:
            raise InitError(f"hosts.{n}.jobs must be all (everything incl. image builds), tests (lint + tests, no image "
                            f"builds), readonly (read-only commands only) or none (got {v!r})")
        return s

    def a_production(self, v, ans):
        word = v.strip().lower() if isinstance(v, str) else None
        if word in ("none", "later") or v == []:
            word = word or "none"
            self.cfg["production"] = {"answer": word}
            gone = self.drop_prod_only(()) if word == "none" else []
            return word + (f"; removed {', '.join(gone)}" if gone else "")
        specs = _specs(v, "production")
        if not specs:
            raise InitError('production must list what you type after ssh for each production server, or be "none" '
                            'or "later"')
        seen, names = {}, []
        for s in specs:
            names.append(self.place(s, "prod", seen)[0])
        gone = self.drop_prod_only(names)
        self.cfg["production"] = {"answer": "set"}
        return ", ".join(self.label(n) for n in dict.fromkeys(names)) + (f"; removed {', '.join(gone)}" if gone else "")

    def drop_prod_only(self, keep):
        """The production answer lists every production-only server: the ones it leaves out are removed."""
        gone = []
        for n, h in list(self.cfg["hosts"].items()):
            if oc._roles(h) == ["prod"] and n not in keep:
                gone.append(oc._label(n, h))
                self.removed[n] = self.cfg["hosts"].pop(n)
        if gone:
            self.out.warn(f"no longer saved as production servers (left out of the production answer): "
                          f"{', '.join(gone)}")
        return gone

    def a_deploy(self, v, ans):
        if (isinstance(v, str) and v.strip().lower() == "later") or v == {"answer": "later"}:
            self.cfg["deploy"] = {"answer": "later"}
            return "later"
        if not isinstance(v, dict):
            raise InitError('deploy must be {"command": "./scripts/deploy.sh", "health": ["https://shop.example/health"]'
                            ', "rollback": null} or "later"')
        bad = [k for k in v if k not in ("command", "health", "rollback")]
        if bad:
            raise InitError(f"deploy: unknown key {bad[0]!r} (valid: command, health, rollback)")
        cur = self.cfg.get("deploy") if isinstance(self.cfg.get("deploy"), dict) else {}
        cur = {} if cur.get("answer") == "later" else cur
        d = {"command": cur.get("command"), "health": list(cur.get("health") or []), "rollback": cur.get("rollback")}
        if "command" in v:
            d["command"] = _command(v["command"], "deploy.command", optional=False)
        if "rollback" in v:
            d["rollback"] = _command(v["rollback"], "deploy.rollback", optional=True)
        if "health" in v:
            d["health"] = _urls(v["health"], "deploy.health")
        if not d["command"]:
            raise InitError('deploy.command is missing: give the command that deploys (it runs on this machine and '
                            'must reach production itself), e.g. ./scripts/deploy.sh, or answer "later"')
        for k in ("command", "rollback"):
            first = (d[k] or "").split()[0] if d[k] else ""
            if first and "/" in first and not first.startswith("/") and not (Path(self.p.root) / first).exists():
                self.out.warn(f"deploy.{k}: {first} does not exist in {Path(self.p.root).as_posix()} yet")
        self.cfg["deploy"] = d
        return (f"{d['command']} | health {', '.join(oc.health_label(h) for h in d['health']) or '-'} | rollback {d['rollback'] or '-'}")

    def a_alerts(self, v, ans):
        if isinstance(v, dict) and set(v) <= {"command"}:
            v = v.get("command")
        elif not (v is None or isinstance(v, str)):
            raise InitError('alerts must be {"command": "./scripts/notify.sh"} or "none"')
        cmd = _command(v, "alerts.command", optional=True)
        self.cfg["alerts"] = {"command": cmd}
        return cmd or "none"

    def proposal(self):
        st = _stored(self.p)
        if st is None:
            st = _detect(self.ctx, self.p, self.out)
            if st is not None:
                self.out.warn("no job proposal was stored yet, so orch detected the jobs now; check them in the summary")
        return copy.deepcopy((st or {}).get("jobs") or {})

    def a_jobs(self, v, ans):
        word = v.strip().lower() if isinstance(v, str) else None
        if word in ("detected", "yes"):
            prop, old = self.proposal(), self.cfg.get("jobs") or {}
            jobs = {n: j for n, j in old.items() if not j.get("manifest") and n not in prop}  # the user's own jobs
            for n, d in prop.items():
                o = str((old.get(n) or {}).get("image") or "")
                if d.get("image") and "@sha256:" in o and o.split("@", 1)[0] == d["image"]:
                    d["image"] = o  # same tag: keep its pin
                jobs[n] = d
            self.cfg["jobs"] = jobs
            self.touched |= set(prop)
            return (", ".join(prop) or "no jobs") + " (from detection)"
        if word == "none" or v == {}:
            self.cfg["jobs"] = {}
            return "none"
        if not isinstance(v, dict):
            raise InitError('jobs must be "detected", "none" or an object of job name -> job keys, e.g. '
                            '{"api-unit": {"memory": "4g"}}')
        if "jobs" in self.cfg:
            jobs = copy.deepcopy(self.cfg["jobs"])
        else:  # a change to the proposal the user was shown
            jobs = self.proposal()
            self.touched |= set(jobs)
        parts = []
        for n, part in v.items():
            if not oc.JOB_RX.fullmatch(str(n)) or n in oc.BUILTIN_JOBS:
                raise InitError(f"job name {n!r} must be letters, digits, '.', '_' or '-' (at most 64) and not a "
                                f"built-in job ({', '.join(oc.BUILTIN_JOBS)})")
            if not isinstance(part, dict):
                raise InitError(f'jobs.{n} must be an object of job keys, e.g. {{"memory": "4g"}}')
            bad = [k for k in part if k not in oc.JOB_CFG_KEYS]
            if bad:
                raise InitError(f"jobs.{n}: unknown key {bad[0]!r} (valid: {', '.join(sorted(oc.JOB_CFG_KEYS))})")
            parts.append(f"{n} {'changed' if n in jobs else 'added'} ({', '.join(part) or 'nothing'})")
            j = jobs.setdefault(n, {})
            for k, x in part.items():
                if x is None:
                    j.pop(k, None)  # e.g. {"manifest": null}: keep the job, stop comparing it with detection
                else:
                    j[k] = copy.deepcopy(x)
            self.touched.add(n)
        self.cfg["jobs"] = jobs
        return "; ".join(parts)

    def a_jobs_remove(self, v, ans):
        if "jobs" in self.cfg:
            jobs = self.cfg["jobs"]
        else:
            jobs = self.proposal()
            self.touched |= set(jobs)
        gone = []
        for n in _names(v, "jobs_remove"):
            if n in jobs:
                del jobs[n]
                gone.append(n)
            else:
                self.out.warn(f"jobs_remove: {n} is not a saved job; nothing removed")
        self.cfg["jobs"] = jobs
        return ", ".join(gone) or None

    # -- whole answer
    def run(self, ans) -> dict:
        if "helpers" in ans or "hosts" in ans:  # read before the registry lock: it reads the registry too
            self.mprod = _mprod(self.ctx, self.home, self.p) or {}
        with oc._locked(self.home / "registry.json"):
            self.reg = oc.registry_load(self.home)
            for n, h in self.cfg["hosts"].items():  # saved identities first: a new machine never takes their names
                key = self.ident(n, h)
                if key and n not in self.reg["nodes"]:
                    self.reg["nodes"][n] = {"hostname": key[0], "port": key[1], "docker_id": None}
                    self.reg_changed = True
            for k in ORDER:
                if k in ans:
                    text = getattr(self, "a_" + k)(ans[k], ans)
                    if text:
                        self.out.applied.append(f"{k}: {text}")
            self.validate()
            if self.reg_changed:
                oc.registry_save(self.home, self.reg)
        return self.cfg

    def validate(self):
        cfg = self.cfg
        if not cfg["hosts"] and "hosts" not in self.old:
            cfg.pop("hosts")
        errs = oc._check_config({**cfg, "schema": oc.SCHEMA})
        if errs:
            raise InitError("; ".join(errs))
        pol = oc._merge(oc.DEFAULT_POLICY, cfg.get("policy") or {})
        for n in sorted(self.touched & set(cfg.get("jobs") or {})):
            _check_job(n, cfg["jobs"][n], pol, self.p, self.out)
        hosts = cfg.get("hosts") or {}
        for n in sorted(self.probe):
            h = hosts.get(n)
            if h and "helper" in oc._roles(h):
                if self.local is None:
                    self.local = self.ctx.local_names()
                key = self.ident(n, h, strict=True)
                if _is_local(key[0], self.local):
                    raise InitError(f"{oc._label(n, h)} is this machine ({key[0]}); a helper must be another server")


def _read_answer(ctx, out) -> dict:
    src = ctx.stdin if ctx.stdin is not None else sys.stdin
    if src is None or (hasattr(src, "isatty") and src.isatty()):
        raise InitError(HEREDOC)
    raw = src.buffer.read(1 << 20) if hasattr(src, "buffer") else src.read(1 << 20)
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise InitError("the answer on stdin is not UTF-8 text; " + HEREDOC) from None
    text = raw.lstrip("﻿")
    if not text.strip():
        raise InitError(HEREDOC)
    try:
        ans = json.loads(text)
    except json.JSONDecodeError as e:
        raise InitError(f"the answer on stdin is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg}); "
                        + HEREDOC) from None
    if not isinstance(ans, dict):
        raise InitError(f'the answer must be a JSON object such as {{"helper_count": 2}} (got {type(ans).__name__})')
    inner = ans.get("setup") if isinstance(ans.get("setup"), dict) else None
    out.answer_keys = sorted({str(k) for k in ans if not (k == "setup" and inner is not None)} | set(inner or ()))
    if inner is not None:  # the setup step's answer_key, sent as a wrapper
        inner = ans.pop("setup")
        bad = [k for k in inner if k not in SETUP_KEYS or k in ans]
        if bad:
            raise InitError(f"setup: unexpected or repeated key {bad[0]!r} (setup holds {', '.join(SETUP_KEYS)})")
        ans.update(inner)
    bad = [k for k in ans if k not in ANSWER_KEYS]
    if bad:
        raise InitError(f"unknown answer key{'s' if len(bad) > 1 else ''} {', '.join(map(repr, bad))}; valid keys: "
                        f"{', '.join(ANSWER_KEYS)}")
    return ans


def _retrust(home, p, old, new, trust, sh):
    """The loosening rule. An approved setup changed by a non-loosening answer is approved again at once, keeping
    the approved co-tenant snapshot and script hashes (so their drift still shows); a loosening answer leaves it
    unapproved until --approve. -> "kept" | "unapproved" | None (nothing to do)."""
    if not trust or trust.get("hash") != oc.config_hash(old) or oc.config_hash(new) == oc.config_hash(old):
        return None
    if oc.loosening(old, new):
        return "unapproved"
    snap = trust.get("cotenants") or {}
    view = {n: ({"facts": {"cotenants": [{"name": c} for c in snap[n]]}} if n in snap else sh.get(n))
            for n in new.get("hosts") or {}}
    keep = set(_script_paths(p, new))
    oc.trust_set(home, p, new, {k: v for k, v in (trust.get("scripts") or {}).items() if k in keep},
                 state_hosts={n: r for n, r in view.items() if r})
    return "kept"


def _orch_py() -> Path:
    return Path(__file__).resolve().with_name("orch.py")


def _cleanup(ctx, p, cfg, sh, trust, names, out):
    """Before hosts stop being helpers: `orch gc --session-end --node NAME` on each one the approved setup used as a
    helper and the last probe reached, so its builder, cache volumes and pulled base images do not stay behind for
    good (orch never visits a host again once it is no longer a helper). The old compiled node goes in env
    ORCH_GC_NODE_JSON, since the node leaves the setup. Unreachable, a live job or any other failure -> InitError
    with the fix, before anything is saved. A helper the last probe could not reach is dropped with a warning that
    says what may be left."""
    approved = (trust or {}).get("config")
    if not names or not isinstance(approved, dict):
        return  # never approved: no orch run ever used these helpers
    try:
        nodes = oc.compile_engine(p, {k: v for k, v in cfg.items() if k != "jobs"}, sh)["nodes"]["nodes"]
    except (oc.ConfigError, ValueError) as e:
        raise InitError(f"orch could not prepare the cleanup of {', '.join(names)} ({e}); fix the setup with orch init, "
                        f"then send this answer again") from None
    label = ctx.env.get("ORCH_LABEL") or oc.DEFAULT_LABEL
    for n in names:
        if "helper" not in oc._roles((approved.get("hosts") or {}).get(n)):
            continue
        rec, h = sh.get(n) or {}, (cfg.get("hosts") or {}).get(n) or {}
        lab = oc._label(n, h)
        usable = rec.get("ok") or (rec.get("problem") or {}).get("class") == "foreign-namespace"  # Docker answered
        if not (usable and rec.get("transport") and n in nodes):
            try:
                ls = orch_discover.ssh_cmdline(h.get("ssh"), f"docker ps -a --filter label={label}")
            except ValueError:
                ls = f"docker ps -a --filter label={label} (on {lab})"
            out.warn(f"{lab} did not answer the last probe, so orch could not remove what it left there; when it is "
                     f"back, list it with {ls} and docker volume ls --filter label={label}, then remove it by hand")
            continue
        argv = [sys.executable, str(_orch_py()), "gc", "--session-end", "--node", n]
        # ORCH_JOURNAL=0: init's own step (and difficulty) records this cleanup; a journaled child would add a second
        # difficulty that no later call resolves once the host has left the setup (Q6)
        env = {**ctx.env, "ORCH_PROJECT": Path(p.root).as_posix(), "ORCH_JOURNAL": "0",
               "ORCH_GC_NODE_JSON": json.dumps({"name": n, "node": nodes[n]})}
        cmd = f"orch gc --session-end --node {n}"
        try:
            r = ctx.run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=GC_TIMEOUT,
                        stdin=subprocess.DEVNULL, env=env)
            code, why = r.returncode, _gc_why(r)
        except subprocess.TimeoutExpired:
            code, why = None, f"it did not finish within {GC_TIMEOUT} s"
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            code, why = None, f"it could not start: {e}"
        if code == 0:
            out.applied.append(f"cleanup: {lab}: orch's builder, cache volumes and pulled base images removed")
            continue
        if code == 75:
            raise InitError(f"a job is still running on {lab}, so orch cannot clean it before it stops being a "
                            f"helper ({why}); wait for the job to finish (orch status lists it) or stop it, then send "
                            f"this answer again")
        if code == 5 and "NOT cleaned (" in why:  # gc could not reach the host
            raise InitError(f"{lab} could not be reached to remove orch's builder, cache volumes and pulled base "
                            f"images before it stops being a helper ({why}); fix ssh to it and send this answer again. "
                            f"If the machine is gone for good, run orch init first: a helper that does not answer the "
                            f"probe is then dropped without the cleanup")
        if code == 5 and re.search(r"NOT (?:clean, still there|confirmed clean)", why):  # reached, objects left
            raise InitError(f"orch reached {lab} but could not remove everything it left there before it stops being "
                            f"a helper ({why}); run {cmd} yourself to see the full error, fix it, then send this "
                            f"answer again")
        raise InitError(f"the cleanup of {lab} failed ({cmd}: {why}); run {cmd} yourself to see the full error, fix "
                        f"it, then send this answer again")


_GC_NOISE = re.compile(r"journal: |orch\[\d\d:\d\d:\d\d\] |orch: warn: journal ")


def _gc_why(r) -> str:
    """What a child `orch gc --session-end --node N` gave as its reason: the error of orch's JSON line when the verb
    failed inside orch, else gc's verdict (its last stdout line), without gc's own "run orch gc ... again" advice
    (init repeats the gc when the answer is sent again); else the first stderr line that is not orch's journal note
    or a progress line."""
    def lines(text):
        return [x.strip() for x in (text or "").splitlines() if x.strip() and not _GC_NOISE.match(x.strip())]
    for line in reversed(lines(r.stdout)):
        if line.startswith("{"):
            try:
                js = json.loads(line)
            except ValueError:
                js = None
            if isinstance(js, dict) and js.get("error"):
                return str(js["error"])[:300]
            continue
        return re.sub(r";\s*run (?:orch )?gc\b.*$", "", line)[:300]
    err = lines(r.stderr)
    return err[0][:300] if err else f"exit {r.returncode}"


def _apply_cmd(a, ctx, out):
    ans = _read_answer(ctx, out)
    try:
        p, _, warns = _project(a, ctx, create=True)
    except _NoProject as e:
        return _no_project(ctx, out, e.start, 2, create=True)
    for w in warns:
        out.warn(w)
    home = oc.home(ctx.env)
    with _lock(p):
        cfg, sh, trust = _load(p, home)
        if not ans:
            out.warn("the answer was empty ({}); nothing applied")
        before = {n: r["context"] for n, r in sh.items() if isinstance(r, dict) and r.get("context")}
        ap = _Apply(ctx, out, p, cfg, sh, home)
        try:
            new = ap.run(ans)
            _cleanup(ctx, p, cfg, sh, trust, sorted(set(ap.removed) | ap.unhelped), out)
        except InitError as e:
            out.applied.clear()
            detected, fresh = _detection(ctx, p, cfg, out)
            return out.final(_settle(ctx, out, p, cfg, sh, trust, home, detected, fresh), 2, error=str(e))
        if new != cfg:
            oc.save_config(p, new)  # answers persist before any probe
        if new.get("jobs") is not None and "jobs" not in cfg and not oc.load_state(p, "project"):
            oc.save_state(p, "project", {"name": _project_name(p)})  # job names stay put in every worktree
        for n in sorted(set(ap.removed) | ap.unhelped):  # their docker contexts go; the hosts no longer run jobs
            rec = ap.sh.get(n) if isinstance(ap.sh.get(n), dict) else {}
            ctx_name = rec.get("context") or (sh.get(n) or {}).get("context")  # a new ssh spec dropped ap.sh's record
            if n in ap.unhelped or "helper" in oc._roles(ap.removed[n]) or ctx_name:
                name = ctx_name or orch_discover.CONTEXT_PREFIX + n
                if not _context_users(home, p, name):  # another project's state still uses it: it stays
                    _drop_context(ctx, out, n, name)
            if n in ap.unhelped and rec:
                rec.update(transport=None, context=None, docker_host=None)
        sh = {n: r for n, r in ap.sh.items() if n in (new.get("hosts") or {})}
        todo = sorted(n for n in ap.probe if n in (new.get("hosts") or {}))
        if todo and not getattr(a, "offline", False):
            recs = _probe(ctx, p, home, out, new, sh, todo, before)
            for n in todo:
                _say(_probe_line(n, new["hosts"][n], recs.get(n) or {}))
            _note_ids(home, recs, new, out)
        oc.save_state(p, "hosts", sh)
        if "jobs" in ans or "jobs_remove" in ans:
            _ship_warning(p, new.get("jobs"), out)
        _journal(ctx, p, list(out.applied))
        if _retrust(home, p, cfg, new, trust, sh) == "kept":
            out.applied.append("approval: kept (no change loosens protection)")
            _journal(ctx, p, out.applied[-1:], why="an approved setup changed without loosening protection")
        trust = oc.trust_get(home, p)
        _guard_view(ctx, home, out)
        detected, fresh = _detection(ctx, p, new, out)
        st = _settle(ctx, out, p, new, sh, trust, home, detected, fresh)
    return out.final(st)


# ----------------------------------------------------------------------------- --detect, plain, --show
def _detect_cmd(a, ctx, out):
    try:
        p, _, warns = _project(a, ctx, create=True)
    except _NoProject as e:
        return _no_project(ctx, out, e.start, 2, create=True)
    for w in warns:
        out.warn(w)
    home = oc.home(ctx.env)
    with _lock(p):
        cfg, sh, trust = _load(p, home)
        fresh = _detect(ctx, p, out)
        if fresh is None:
            raise InitError(f"job detection failed in {Path(p.root).as_posix()}: see the warning above")
        lines = orch_detect.proposal_lines(fresh.get("jobs") or {})
        _say("jobs detected:" if lines else "no jobs detected")
        for x in lines:
            _say(f"  {x}")
        for x in fresh.get("notes") or []:
            _say(f"note: {x}")
        for x in fresh.get("unknown") or []:
            _say(f"not recognised: {x}")
        _ship_warning(p, fresh.get("jobs"), out)
        if "jobs" in cfg:
            diff = _diff_lines(cfg, _drift_view(cfg, fresh))  # a same job under a new name is no difference
            _say("compared with the saved jobs:" + ("" if diff else " no difference"))
            for x in diff:
                _say(f"  {x}")
        _guard_view(ctx, home, out)
        st = _settle(ctx, out, p, cfg, sh, trust, home, _drift_view(cfg, fresh), True)
    return out.final(st)


def _plain(a, ctx, out, show):
    try:
        p, exists, warns = _project(a, ctx, create=False)
    except _NoProject as e:
        return _no_project(ctx, out, e.start, 0)
    for w in warns:
        out.warn(w)
    home = oc.home(ctx.env)
    if not exists:  # a project without a setup yet: nothing in it is created before the first answer
        _guard_view(ctx, home, out)
        return out.final(oc.status(p, {}, {}, None, master_prod=_mprod(ctx, home, p)))
    with _lock(p):
        cfg, sh, trust = _load(p, home)
        hosts = cfg.get("hosts") or {}
        before, sh = sh, {n: r for n, r in sh.items() if n in hosts}
        if hosts and not getattr(a, "offline", False):
            recs = _probe(ctx, p, home, out, cfg, sh, list(hosts))
            if show:
                for n in hosts:
                    _say(_probe_line(n, hosts[n], recs.get(n) or {}))
            _note_ids(home, recs, cfg, out)
        if sh != before:
            oc.save_state(p, "hosts", sh)
        detected, fresh = _detection(ctx, p, cfg, out)
        _guard_view(ctx, home, out)
        st = _settle(ctx, out, p, cfg, sh, trust, home, detected, fresh)
    if show:
        _say(f"project {Path(p.root).as_posix()} (setup in {Path(p.store).as_posix()})")
        for x in oc.summary_lines(cfg, sh, full=True):
            _say(x)
        if not trust:
            _say("approval: never approved")
        elif trust.get("hash") == oc.config_hash(cfg):
            _say(f"approval: approved {trust.get('approved_at', '?')}")
        else:
            _say(f"approval: changed since the approval of {trust.get('approved_at', '?')}")
    return out.final(st)


# ----------------------------------------------------------------------------- --approve
def _command_words(cmd) -> list:
    """Every word of a command that may name a file: its shell words, also with Windows backslashes read as path
    separators (.\\scripts\\deploy.ps1), and the words of quoted inline code (pwsh -Command "& ./deploy.ps1")."""
    words = []
    for text in dict.fromkeys((cmd, cmd.replace("\\", "/"))):
        try:
            words += shlex.split(text)
        except ValueError:
            pass
        words += [w.strip("\"'();&|") for w in text.split()]
    return list(dict.fromkeys(w for w in words if w))


def _project_file(root, word):
    """(project-relative path, resolved Path) when word names an existing file inside root (resolved), else None. A
    network path (//host/share, \\\\host\\share) is never inside a project, so it is never touched."""
    if word.startswith(("-", "//", "\\\\")) or "\x00" in word or len(word) > 1024:
        return None
    try:
        rp = (Path(word) if Path(word).is_absolute() else root / word).resolve()
        return (rp.relative_to(root).as_posix(), rp) if rp.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _deploy_commands(cfg):
    dep = cfg.get("deploy") if isinstance(cfg.get("deploy"), dict) else {}
    return [(k, dep[k]) for k in ("command", "rollback") if isinstance(dep.get(k), str) and dep[k].strip()]


def _script_paths(p, cfg) -> dict:
    """Deploy and rollback scripts inside the project: {project-relative path: Path}, whose content approval records
    for the scripts-unchanged gate. Every word of the command that names an existing file inside the project counts:
    the script itself (./deploy.sh, .\\scripts\\deploy.ps1), an interpreter's script after options that take a value
    (bash -o pipefail ./deploy.sh, pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/deploy.ps1), the script that
    inline code runs, an rc file. Options, their values and other words are no project files, so they drop out."""
    root, out = Path(p.root).resolve(), {}
    for _, cmd in _deploy_commands(cfg):
        for w in _command_words(cmd):
            f = _project_file(root, w)
            if f:
                out.setdefault(*f)
    return out


def _unhashed(p, cfg) -> list:
    """'deploy.command (./deploy.sh)' for each deploy or rollback command that names a script-like word (.sh, .ps1,
    .py, ...) but no file inside the project: approval cannot record its content, so the gate cannot see its edits."""
    root, out = Path(p.root).resolve(), []
    for k, cmd in _deploy_commands(cfg):
        if not any(_project_file(root, w) for w in _command_words(cmd)):
            named = [w for w in (x.strip("\"'();&|") for x in cmd.split()) if _SCRIPT_EXT.search(w)]
            if named:
                out.append(f"deploy.{k} ({named[0]})")
    return out


def _script_hashes(p, cfg) -> dict:
    return {k: hashlib.sha256(v.read_bytes()).hexdigest() for k, v in _script_paths(p, cfg).items()}


def _proc(run, argv, timeout):
    try:
        return run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _manifest_digest(r):
    if r is None or r.returncode != 0:
        return None
    m = re.search(r'"digest"\s*:\s*"(sha256:[0-9a-f]{64})"', r.stdout or "")
    return m.group(1) if m else None


def _why(r) -> str:
    if r is None:
        return "the command could not start"
    lines = [x.strip() for x in f"{r.stderr or ''}\n{r.stdout or ''}".splitlines() if x.strip()]
    return lines[0][:200] if lines else f"exit {r.returncode}"


def _http(method, url, headers, timeout):
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https (or a localhost registry) only
            return r.status, {k.lower(): v for k, v in r.headers.items()}, (r.read(4 << 20) if method != "HEAD"
                                                                           else b"")
    except urllib.error.HTTPError as e:
        hdr = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        e.close()
        return e.code, hdr, b""


def _anon_token(http, challenge, repo, host, timeout) -> str:
    m = re.match(r"\s*Bearer\s+(.+)", challenge or "", re.I)
    if not m:
        raise _PinError(f"{host} wants a login ({(challenge or 'no challenge').split(' ')[0]}): run docker login {host} "
                        f"on this machine, then approve again")
    params = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    realm = params.pop("realm", "")
    u = urllib.parse.urlsplit(realm)
    if not (u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in ("localhost", "127.0.0.1"))):
        raise _PinError(f"{host} sent a token address that is not https ({realm or 'none'}); orch does not follow it")
    params.setdefault("scope", f"repository:{repo}:pull")
    st, _, body = http("GET", realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(params),
                       {"User-Agent": "orch-init"}, timeout)
    if st != 200:
        raise _PinError(f"{host} gave no anonymous token (HTTP {st}): the image is private; run docker login {host} "
                        f"on this machine, then approve again")
    try:
        data = json.loads(body.decode("utf-8"))
        tok = data.get("token") or data.get("access_token")
    except (ValueError, AttributeError):
        tok = None
    if not isinstance(tok, str) or not tok:
        raise _PinError(f"{host} answered the token request without a token")
    return tok


def _registry_digest(http, ref, timeout=15) -> str:
    """Registry v2 HEAD of /v2/<name>/manifests/<tag> (anonymous bearer token on a 401) -> Docker-Content-Digest."""
    try:
        reg, repo, tag, _ = _ref_parts(ref)
    except ValueError as e:
        raise _PinError(str(e)) from None
    host = "registry-1.docker.io" if reg == "docker.io" else reg
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    url = f"{scheme}://{host}/v2/{repo}/manifests/{tag}"
    hdrs = {"Accept": ", ".join(MANIFEST_TYPES), "User-Agent": "orch-init"}
    try:
        st, h, _ = http("HEAD", url, hdrs, timeout)
        if st == 401:
            hdrs["Authorization"] = "Bearer " + _anon_token(http, h.get("www-authenticate"), repo, host, timeout)
            st, h, _ = http("HEAD", url, hdrs, timeout)
        if st == 200 and _DIGEST.fullmatch(h.get("docker-content-digest") or ""):
            return h["docker-content-digest"]
        if st == 200:  # no digest header: the digest is the sha256 of the manifest bytes
            st, h, body = http("GET", url, hdrs, timeout)
            if st == 200 and body:
                d = h.get("docker-content-digest") or ""
                return d if _DIGEST.fullmatch(d) else "sha256:" + hashlib.sha256(body).hexdigest()
    except _PinError:
        raise
    except Exception as e:  # noqa: BLE001 - URLError, timeouts, http.client protocol errors: a reason, not a crash
        raise _PinError(f"{host} could not be reached from this machine ({type(e).__name__}: {e})") from None
    raise _PinError(f"HEAD {url} answered {st}" + {404: " (no such image or tag)", 401: " (a private image: docker "
                                                   f"login {host})", 403: " (access denied)",
                                                   429: " (rate limited: try again later)"}.get(st, ""))


def _pin_targets(cfg, sh) -> list:
    """Helpers that may run the pin command: probed ok with Docker; helper-only before helper + production, Linux
    before Windows."""
    out = []
    for n, h in (cfg.get("hosts") or {}).items():
        rec = sh.get(n) or {}
        f = rec.get("facts") or {}
        if "helper" in oc._roles(h) and rec.get("ok") and f.get("docker"):
            out.append((("prod" in oc._roles(h), f.get("os") != "linux", n), h["ssh"]))
    return [(k[2], s) for k, s in sorted(out)]


def _pin_one(ctx, ref, helpers):
    """-> (digest | None, where, [why not, per way tried]). Ways in order: docker buildx imagetools on this machine,
    the same command on a helper over ssh (its network may reach the registry), an anonymous registry v2 HEAD."""
    fmt, tried = "{{json .Manifest}}", []
    if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", ref):
        return None, None, [f"{ref!r} is not an image reference"]
    r = _proc(ctx.run, ["docker", "buildx", "imagetools", "inspect", ref, "--format", fmt], 60)
    d = _manifest_digest(r)
    if d:
        return d, "this machine", tried
    tried.append(f"this machine: {'no docker CLI' if r is None else _why(r)}")
    remote = f'docker buildx imagetools inspect {ref} --format "{fmt}"'  # same text in sh, cmd.exe and PowerShell
    for n, spec in helpers[:2]:
        r = _proc(ctx.run, list(ctx.od.SSH_BASE) + ctx.od.ssh_args(spec) + [remote], 60)
        d = _manifest_digest(r)
        if d:
            return d, f"helper {n}", tried
        tried.append(f"{n}: {_why(r)}")
    try:
        return _registry_digest(ctx.http, ref), "registry", tried
    except _PinError as e:
        tried.append(f"registry: {e}")
    return None, None, tried


def _pin_all(ctx, cfg, sh, offline):
    refs = sorted({j["image"] for j in (cfg.get("jobs") or {}).values()
                   if isinstance(j, dict) and isinstance(j.get("image"), str) and "@sha256:" not in j["image"]})
    if not refs:
        return {}, {}
    if offline:
        return {}, {r: ["--offline: orch reached no registry"] for r in refs}
    helpers = _pin_targets(cfg, sh)
    with cf.ThreadPoolExecutor(max_workers=min(8, len(refs))) as ex:
        res = dict(zip(refs, ex.map(lambda r: _pin_one(ctx, r, helpers), refs)))
    return ({r: (d, w) for r, (d, w, _) in res.items() if d}, {r: why for r, (d, _, why) in res.items() if not d})


def _ready_to_approve(cfg, sh):
    hosts = cfg.get("hosts") or {}
    helpers = [n for n, h in hosts.items() if "helper" in oc._roles(h)]
    if not helpers:
        raise InitError("no helper is saved yet: run orch init and answer its questions, then approve")
    bad = [oc._label(n, hosts[n]) for n in helpers if not (sh.get(n) or {}).get("ok")]
    if bad:
        raise InitError(f"not every helper probed ok ({', '.join(bad)}): run orch init, fix what it shows (or remove "
                        f"the host with remove_hosts), then approve")
    todo = oc._setup_open(cfg)  # status()'s own test: approval never comes before the setup step is done
    miss = [f"the job scope of {oc._label(n, hosts[n])}" for n in todo["helpers"]] + \
        [k for k in ("production", "deploy", "alerts") if todo[k]]
    if miss:
        raise InitError(f"the setup question is not answered yet: still missing {', '.join(miss)}; run orch init and "
                        f"answer it, then approve")
    if "jobs" not in cfg:
        raise InitError("the jobs question is not answered yet: run orch init and answer it, then approve")
    bad = _hand_secrets(cfg)
    if bad:  # a hand edit skipped --apply's check; approval would copy it into trust.json and the guard view
        raise InitError(f"{', '.join(bad)} in config.json seem{'s' if len(bad) == 1 else ''} to hold a secret; orch "
                        f"saves, prints and journals these, so read the secret from a file or an environment variable "
                        f"inside your script, save the fixed value with orch init --apply, then approve again")


def _hand_secrets(cfg) -> list:
    """Saved commands and URLs (the field names) that hold a secret."""
    dep = cfg.get("deploy") if isinstance(cfg.get("deploy"), dict) else {}
    vals = [("deploy.command", dep.get("command")), ("deploy.rollback", dep.get("rollback")),
            ("alerts.command", (cfg.get("alerts") or {}).get("command") if isinstance(cfg.get("alerts"), dict)
             else None)]
    vals += [("deploy.health", oc.health_url(e)) for e in dep.get("health") or [] if oc._health_ok(e)]
    vals += [(f"hosts.{n}.watch", u) for n, h in (cfg.get("hosts") or {}).items() if isinstance(h, dict)
             for u in h.get("watch") or []]
    return list(dict.fromkeys(k for k, v in vals if isinstance(v, str) and _secret(v)))


def _approve_cmd(a, ctx, out):
    try:
        p, exists, _ = _project(a, ctx, create=False)
    except _NoProject as e:
        return _no_project(ctx, out, e.start, 2)
    if not exists:
        raise InitError(f"nothing to approve in {Path(p.root).as_posix()} yet: run orch init and answer its questions "
                        f"first")
    home = oc.home(ctx.env)
    with _lock(p):
        cfg, sh, trust = _load(p, home)
        detected, _ = _detection(ctx, p, cfg, out)
        try:
            _ready_to_approve(cfg, sh)
            pol = oc._merge(oc.DEFAULT_POLICY, cfg.get("policy") or {})
            for n, j in (cfg.get("jobs") or {}).items():  # hand edits too: the engine must accept every job
                _check_job(n, j, pol, p, out)
            pins, bad = _pin_all(ctx, cfg, sh, getattr(a, "offline", False))
            for ref, (d, where) in sorted(pins.items()):
                _say(f"pinned {ref} -> {d[:19]}... (via {where})")
            jobs = cfg.get("jobs") or {}
            if bad:
                users = {r: [n for n, j in jobs.items() if j.get("image") == r] for r in bad}
                lines = [f"{r} (job {', '.join(users[r])}): " + "; ".join(why) for r, why in sorted(bad.items())]
                if pol.get("require_digest", True):
                    one = sorted(bad)[0]
                    ex = json.dumps({"jobs": {users[one][0]: {"image": f"{one}@sha256:<digest>"}}})
                    raise InitError(f"could not pin {len(bad)} image{'s' if len(bad) != 1 else ''} to a digest: "
                                    + " | ".join(lines) + ". Fix: make the registry reachable from this machine or a "
                                    f"helper (docker login <registry> for a private image), or pin it yourself with "
                                    f"orch init --apply and {ex}; then run orch init --approve again")
                for x in lines:
                    out.warn(f"not pinned (policy require_digest is off): {x}")
            new = copy.deepcopy(cfg)
            for j in (new.get("jobs") or {}).values():
                if j.get("image") in pins:
                    j["image"] = f"{j['image']}@{pins[j['image']][0]}"
            oc.compile_engine(p, new, sh)  # every job must translate for the engine
        except (InitError, oc.ConfigError) as e:
            return out.final(_settle(ctx, out, p, cfg, sh, trust, home, detected, True), 2, error=str(e))
        if new != cfg:
            oc.save_config(p, new)
        scripts = _script_hashes(p, new)
        for x in _unhashed(p, new):
            out.warn(f"{x} names no file inside {Path(p.root).as_posix()}, so the approval records no script content "
                     f"for it and orch deploy --check cannot warn when that script changes")
        oc.trust_set(home, p, new, scripts, state_hosts=sh)
        trust = oc.trust_get(home, p)
        _guard_view(ctx, home, out)
        out.applied.append(f"approved: {len(pins)} image{'s' if len(pins) != 1 else ''} pinned, "
                           f"{len(scripts)} deploy script{'s' if len(scripts) != 1 else ''} hashed")
        _journal(ctx, p, ["setup approved: " + oc._ready_line(new, trust)])
        st = _settle(ctx, out, p, new, sh, trust, home, detected, True)
    return out.final(st)


# ----------------------------------------------------------------------------- --ask
ASK_KEYS = ("deploy", "production")


def _ask_cmd(a, ctx, out):
    """--ask deploy|production: the deploy gate's question for a "later" or missing answer (or, with a deploy command
    saved, the health URL question orch live asks for), as the ask of the final JSON line (orch deploy --check and
    orch live carry the same one); nothing is probed or saved. Answer it with --apply."""
    key = a.ask
    if key not in ASK_KEYS:
        raise InitError(f"--ask takes {' or '.join(ASK_KEYS)} (got {key!r})")
    try:
        p, exists, warns = _project(a, ctx, create=False)
    except _NoProject as e:
        return _no_project(ctx, out, e.start, 2)
    if not exists:
        raise InitError(f"no orch setup in {Path(p.root).as_posix()} yet: run orch init and answer its questions first")
    for w in warns:
        out.warn(w)
    home = oc.home(ctx.env)
    cfg, sh, trust = _load(p, home)
    import orch_deploy  # the gate owns the question; imported here so init never needs it otherwise
    st = oc.status(p, cfg, sh, trust, _stored(p), master_prod=_mprod(ctx, home, p))
    st.update(step="setup", next="ask", ask=orch_deploy.ask_for(cfg, key))
    return out.final(st)


# ----------------------------------------------------------------------------- entry points
def cmd_init(a, ctx=None) -> int:
    """orch init with a.offline, a.apply, a.detect, a.approve, a.show, a.ask, a.project (argparse namespace or
    similar)."""
    ctx = ctx or Ctx()
    out = _Out()
    try:
        if getattr(a, "apply", False):
            return _apply_cmd(a, ctx, out)
        if getattr(a, "detect", False):
            return _detect_cmd(a, ctx, out)
        if getattr(a, "approve", False):
            return _approve_cmd(a, ctx, out)
        if getattr(a, "ask", None):
            return _ask_cmd(a, ctx, out)
        return _plain(a, ctx, out, bool(getattr(a, "show", False)))
    except (InitError, oc.ConfigError) as e:
        out.applied.clear()
        if isinstance(e, InitError):  # e.g. an answer refused before the setup was read: report the real status
            try:
                return _refused(a, ctx, out, str(e))
            except (InitError, oc.ConfigError, OSError):
                pass  # the setup itself cannot be read: the fallback below says so
        return out.final(_fallback(str(e)), 2, error=str(e))


def _refused(a, ctx, out, msg) -> int:
    """Exit 2 with msg next to the CURRENT status (what orch init --offline shows: state, step and the pending ask),
    so the agent corrects the call and resends it, and the journal never records a state that did not exist (Q8).
    Nothing is created, probed or saved."""
    try:
        p, exists, warns = _project(a, ctx, create=False)
    except _NoProject as e:
        return _no_project(ctx, out, e.start, 2, create=bool(getattr(a, "apply", False)))
    for w in warns:
        out.warn(w)
    home = oc.home(ctx.env)
    if not exists:  # no setup yet: the first question
        return out.final(oc.status(p, {}, {}, None, master_prod=_mprod(ctx, home, p)), 2, error=msg)
    with _lock(p):
        cfg, sh, trust = _load(p, home)
        sh = {n: r for n, r in sh.items() if n in (cfg.get("hosts") or {})}
        detected, fresh = _detection(ctx, p, cfg, out)
        st = _settle(ctx, out, p, cfg, sh, trust, home, detected, fresh)
    return out.final(st, 2, error=msg)


def add_arguments(ap):
    """The init options, for orch.py's subparser (create it with allow_abbrev=False, so --appr never means
    --approve behind the guard's back)."""
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="read the answer JSON on stdin (quoted heredoc) and save it")
    g.add_argument("--detect", action="store_true", help="re-run job detection, store and print the proposal")
    g.add_argument("--approve", action="store_true", help="pin job images, record the approval, write the guard view")
    g.add_argument("--show", action="store_true", help="print the full saved setup")
    g.add_argument("--ask", choices=ASK_KEYS, help="print the deploy or production question again (a later or "
                                                   "missing answer, or a missing health URL; answer it with "
                                                   "--apply)")
    ap.add_argument("--offline", action="store_true", help="no ssh probe and no registry access")
    ap.add_argument("--project", metavar="DIR", help="the project folder (default: ORCH_PROJECT, else the current one)")
    return ap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orch init", allow_abbrev=False,
                                 description="the orch setup interview: status, answers, job detection, approval")
    return cmd_init(add_arguments(ap).parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
