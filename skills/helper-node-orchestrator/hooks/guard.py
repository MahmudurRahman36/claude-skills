#!/usr/bin/env python3
"""helper-node-orchestrator PreToolUse guard.

Claude Code runs this before every Bash, PowerShell, Write, Edit, MultiEdit and NotebookEdit call and every MCP
tool call (matcher mcp__.*). It reads the hook JSON on stdin and prints one documented decision, or exits 0 with no
output (no decision; the normal permission flow applies):

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "deny" | "ask",
                            "permissionDecisionReason": "..."}}

What it knows about this master comes only from $ORCH_HOME/guard-view.json (ORCH_HOME defaults to ~/.helper-orch),
written by `orch init` from every approved project: hosts with roles (helper, prod) and every name they can be
typed as, project roots and stores, registered deploy/rollback commands, job index. No view = nothing approved on
this master = no decisions at all, unless $ORCH_HOME/trust.json exists (an approved setup did): then a missing view
counts as unreadable (below). Preferences: $ORCH_HOME/guard.json (user-only) {"new_heavy": "warn"|"deny",
"heavy_scope": "project"|"everywhere"}. Nothing is cached between calls.

Rules (see ../RULES.md):
  a  raw docker aimed at a helper-only host (--context/-H, DOCKER_HOST/DOCKER_CONTEXT, docker context use/create);
     docker context use/update/rm/create/import of an orch-* context anywhere (orch manages those)
  b  ssh to a helper-only host running docker, rm, kill, systemctl, wsl, shutdown, reboot, heavy work or a
     stdin-fed shell (read-only ssh such as uptime/free/df/cat/ls/ps/journalctl is allowed); scp/rsync/sftp to it;
     ssh to it whose remote command holds a substitution the master's shell runs first (MF2: $(...) or backticks
     outside single quotes, an unquoted-delimiter heredoc's, PowerShell $(...), (...), @(...), {...}, @"..."@);
     from an MCP terminal also an interactive ssh or shell session to it (later input would reach it unchecked)
  p  a production host (role prod, also helper+prod: prod wins) over ssh, docker -H/--context/DOCKER_HOST or
     scp/rsync/sftp: read-only commands -> allow; destructive -> deny; a registered deploy/rollback command of an
     approved project -> allow; any other change -> ask, ssh with such a local substitution included (MF2)
  c  destructive docker anywhere (system/volume/network prune, image prune -a, builder/buildx prune without
     --builder <prefix>builder, image/container prune without --filter label=<label>=1) and rule 4: docker or
     containerd restarts, daemon.json writes, wsl --shutdown/--terminate, compose --rmi all, published host ports,
     docker login with a password
  d  heavy work on the master (docker build, compose up/build, docker run of sdk/node/playwright images, dotnet
     build/test/..., npm/pnpm/yarn install/test/build/lint, npx playwright; new toolchains only when guard.json
     new_heavy is deny) inside an approved project root (payload cwd, a cd path, or a path argument), anywhere
     when guard.json heavy_scope is everywhere. Whatever new_heavy says, a tool prefix in an approved project's
     job index (pytest, python -m pytest, ruff, npx jest, npm run <script>, ...) is denied inside that project,
     matched with its launcher dropped (python -m X == X; npx/pnpm exec/yarn dlx X == X; uv/poetry run X == X;
     npm/pnpm/yarn/bun run X == run X). Waived while the user-created flag $ORCH_HOME/ALLOW_LOCAL exists
  e  creating/writing/copying/moving ALLOW_LOCAL, writing or deleting guard.json, setting HELPER_ORCH_LOCAL
  s  writing or deleting orch setup files: an approved project's store (config.json, state/) and ORCH_HOME
     trust.json, registry.json, guard-view.json (use orch init)
  i  `orch init --approve` -> ask: saving the setup is the user's call
  v  a command name held in a variable: $NAME / ${NAME} given a value earlier in the same command (NAME=...,
     export/declare/typeset/local/readonly, for NAME in ...; PowerShell $NAME = ..., Set-Variable) is replaced by
     every value it was given and checked by all the rules above. A name the guard cannot resolve (set outside the
     command, by command substitution, read, eval or as an array; a $(...) command name) asks the user when the
     rest names an orch approval, a helper or production host, prune/--rmi/wsl --shutdown or (rule d scope) heavy
     work, or it runs on a helper or production host, so the agent spells the command out; otherwise no decision
  x  code the guard cannot read (MF4): python -c / - / a bare python fed by stdin, node/bun -e/-p/--eval, deno eval,
     ruby -e, perl -e, php -r, osascript -e, pwsh/powershell -c/-Command/-EncodedCommand text (still analysed as
     PowerShell), and input an MCP terminal sends to a running process: asks when the text names a helper or
     production host (any view identifier) or a protected path (ALLOW_LOCAL, guard.json, ORCH_HOME, its setup
     files or variable, a store)
  m  MCP tools (MF3): a command tool (name with process/command/shell/exec/terminal, a command/cmd/input string)
     meets every rule above, read as the shell another input names, else as both sh and PowerShell; a file tool
     (write_file, edit_block, edit_file, move_file, create_directory, delete, write) gets the Write/Edit protection
     on its path inputs (path, file_path, source, destination, paths[], *path); any other input string naming a
     helper or production host or a protected path asks; harmless calls get no decision

This is a guard rail, not a sandbox: it parses the command text only. Unreadable view (or a missing one while
trust.json exists): commands that mention docker/ssh/build tools are denied with the `orch init` fix (orch init
itself passes, so it can write the view again), everything else gets only the rules that need no view. Any other
internal error fails open (exit 0, note on stderr), except for those same commands, which are denied.

RESIDUAL = '''
- Script files: `bash x.sh` / `./x.sh` / `python x.py` that run docker or ssh inside are not read.
- Dynamic names: aliases and functions; a variable or $(...) command name the guard cannot resolve passes when the
  rest of the command does not look relevant (rule v). Values are tracked by command text, not control flow (every
  value a name gets earlier in the command counts), and PowerShell `$x = tool` with no arguments is not checked.
  Only command names are expanded: a variable holding a host or path argument (ssh $H ..., scp f $H:/p,
  docker -H $X, python $SCRIPT --approve), `set -- ...; "$@"` and IFS word splitting are not followed.
- Inline interpreter code is not read: it asks only when its text names a host or protected path (rule x), so
  python -c "subprocess.run(['docker','build'])" passes, as do names built at run time ('p'+'c') and code that
  comes from a substitution or a file (python -c "$(cat x.py)", exec(open(...))).
- A variable holding a whole remote command passes only when this command gives it a fixed value (C=uptime); a
  local substitution or an unknown value asks (rule v) and $(...) itself asks or denies (MF2).
- MCP: a tool not recognised as a command runner is read only for host and protected-path names; input an MCP
  terminal sends to an earlier session is judged alone (a production session the user approved is not re-checked,
  nor an interpreter session such as `ssh <helper> python3` that it opened).
- `cat x.sh | bash` locally (stdin text from a file is not read; over ssh to a helper it is denied, to prod asked).
- Host names the view does not know: ssh config aliases added after the last `orch init`, `ssh -F other_config`,
  ProxyCommand, DNS names or search domains that resolve to a view IP.
- Docker API over a raw socket/HTTP (curl --unix-socket), WSL distro docker via scripts.
- Production reached any other way: HTTP calls to its apps, git push to a named remote (an explicit
  user@host:path or ssh:// URL of a production host asks), kubectl, ansible, Invoke-Command / PowerShell remoting.
- Archive tools or interpreters writing into a store through a subfolder or a path built at run time.
- Obfuscated ALLOW_LOCAL or setup paths (string concatenation inside code, variables holding the path).
- Hook not wired / python missing: Claude Code treats a non-0/2 exit as non-blocking, so the call proceeds.
- Heavy work in a folder outside every approved project root is allowed unless heavy_scope is everywhere.
- New toolchains (pytest, pip, uv, bun, deno, make, cmake, cargo, go, mvn, gradle) only warn unless
  $ORCH_HOME/guard.json has {"new_heavy": "deny"}, except inside an approved project whose own jobs use them.
- Wrappers: the listed pass-through wrappers (winpty, flock, strace, ltrace, runuser, sg, script, su, parallel,
  systemd-run, pkexec, chronic, caffeinate, and the PREFIX/VALOPTS ones) are unwrapped; a wrapper not on the list
  only asks when its tail clearly names an orch approval / a known host / a prune / a daemon.json write, and a
  command passed as one opaque string (parallel ::: 'ssh ...', an unknown wrapper's quoted argument) is not read.
- Variable host/path args are substituted only for ssh/scp/rsync/sftp/docker/cp/mv/rm/tee and redirects, from
  same-script values (rule v); a host in `-o HostName=$VAR`, a value from command substitution/read/an array, or
  a target of another command is not expanded.
'''
"""
import json
import os
import posixpath
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _orch_cmd():
    """`bash <skill>/scripts/orch`, derived from this file's location (~ for the home folder)."""
    p = os.path.join(os.path.dirname(HERE), "scripts", "orch").replace("\\", "/")
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if home and p.lower().startswith(home.lower() + "/"):
        p = "~" + p[len(home):]
    if " " in p:
        p = '"%s"' % (("$HOME" + p[1:]) if p.startswith("~") else p)
    return "bash " + p


ORCH = _orch_cmd()
INIT = ORCH + " init"
RUNJOB = ORCH + " run <job>"
MAX_DEPTH = 8
USER = "ask the user (only the user may set it)"
PROD_FIX = "ask the user to run it themselves"
APPROVE_REASON = "saving the orch setup needs your OK"
LIGHT = {"-h", "--help", "-?", "/?", "--version", "--list-tests", "--info"}
XARGS_KEY = "\x00xargs"
ENV_CWD = "\x00cwd"
DEFAULT_LABEL, DEFAULT_PREFIX = "io.orch.agent", "agentx-"
LABEL_RE = re.compile(r"^[a-z0-9.-]{3,40}$")
PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,14}-$")
SETUP_FILES = ("trust.json", "registry.json", "guard-view.json")


def orch_home():
    return os.environ.get("ORCH_HOME") or os.path.join(os.path.expanduser("~"), ".helper-orch")


def orch_label():
    v = os.environ.get("ORCH_LABEL", "")
    return v if LABEL_RE.match(v) else ((CTX.view.label if CTX else None) or DEFAULT_LABEL)


def orch_prefix():
    v = os.environ.get("ORCH_PREFIX", "")
    return v if PREFIX_RE.match(v) else ((CTX.view.prefix if CTX else None) or DEFAULT_PREFIX)


def _log(name, line):
    try:
        d = orch_home()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), line.replace("\n", " ")[:500]))
    except Exception:
        pass


def guard_prefs():
    """$ORCH_HOME/guard.json (user-only); missing or broken -> defaults (warn, project)."""
    try:
        with open(os.path.join(orch_home(), "guard.json"), encoding="utf-8") as f:
            d = json.load(f)
        d = d if isinstance(d, dict) else {}
    except Exception:
        d = {}
    return {"new_heavy": "deny" if d.get("new_heavy") == "deny" else "warn",
            "heavy_scope": "everywhere" if d.get("heavy_scope") == "everywhere" else "project"}


# ---------------------------------------------------------------- guard view
class ViewError(Exception):
    pass


URL_RE = re.compile(r"^([a-z][a-z0-9+.-]*)://([^/?#]*)")


def _host_port(v):
    """(host, port|None) of a target as typed: name, alias, IP, user@host, host:port, [v6]:port, ssh:// or tcp://
    URL, docker context name. Local docker endpoints (unix://, npipe://) -> ("", None)."""
    v = (v or "").strip().strip("\"'").lower()
    m = URL_RE.match(v)
    if m:
        if m.group(1) in ("unix", "npipe", "fd", "file"):
            return "", None
        v = m.group(2)
    v = v.rsplit("@", 1)[-1]
    m = re.match(r"^\[([^\]]*)\](?::(\d+))?", v)
    if m:
        return m.group(1), (int(m.group(2)) if m.group(2) else None)
    if v.count(":") == 1:
        v, _, p = v.partition(":")
        if p.isdigit():
            return v.strip("/").rstrip("."), int(p)
    return v.strip("/").rstrip("."), None


def _strs(v, what):
    """List of non-empty strings from a str / list / null field; anything else is a broken view."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return [x for x in v if x.strip()]
    raise ViewError("%s must be a list of strings" % what)


class View(object):
    """Parsed guard-view.json (schema 1, shape in dev/v2-contract.md)."""

    def __init__(self, data):
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ViewError("schema must be 1")
        hosts, projects, ns = (data.get(k) for k in ("hosts", "projects", "namespace"))
        hosts = [] if hosts is None else hosts
        projects = [] if projects is None else projects
        ns = {} if ns is None else ns
        if not isinstance(hosts, list) or not isinstance(projects, list) or not isinstance(ns, dict):
            raise ViewError("hosts and projects must be lists, namespace an object")
        lab, pre = ns.get("label"), ns.get("prefix")
        self.label = lab if isinstance(lab, str) and LABEL_RE.match(lab) else None
        self.prefix = pre if isinstance(pre, str) and PREFIX_RE.match(pre) else None
        self.keys = {}      # every lowercase name a host can be typed as -> [host record]
        for h in hosts:
            if not isinstance(h, dict) or not isinstance(h.get("name"), str) or not h["name"].strip():
                raise ViewError("every host needs a name")
            roles = set(_strs(h.get("roles"), "roles"))
            port = h.get("port")
            if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
                raise ViewError("host %s: port must be a number" % h["name"])
            names = [h["name"]] + _strs(h.get("aliases"), "aliases") + _strs(h.get("ips"), "ips") + \
                _strs(h.get("contexts"), "contexts") + _strs(h.get("hostname"), "hostname")
            spec = _strs(h.get("spec"), "spec")
            if not roles & {"helper", "prod"}:
                continue
            rec = {"name": h["name"].strip(), "kind": "prod" if "prod" in roles else "helper",
                   "both": {"helper", "prod"} <= roles, "port": port or 22, "spec": spec[0].strip() if spec else ""}
            for n in names + [_host_port(s)[0] for s in spec]:
                k = _host_port(n)[0] if "@" in n or "://" in n else n.strip().lower().strip("[]").rstrip(".")
                if k and rec not in self.keys.setdefault(k, []):
                    self.keys[k].append(rec)
        self.projects = []
        for p in projects:
            if not isinstance(p, dict) or not isinstance(p.get("root"), str) or not p["root"].strip():
                raise ViewError("every project needs a root")
            jobs, store = p.get("jobs"), p.get("store")
            jobs = jobs if jobs is not None else {}
            if not isinstance(jobs, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                     for k, v in jobs.items()):
                raise ViewError("project %s: jobs must map tool prefixes to job names" % p["root"])
            if store is not None and not isinstance(store, str):
                raise ViewError("project %s: store must be a path" % p["root"])
            self.projects.append({
                "root": _norm_path(p["root"]), "store": _norm_path(store) if store else "",
                "commands": _strs(p.get("deploy"), "deploy") + _strs(p.get("rollback"), "rollback"),
                "jobs": {" ".join(k.lower().split()): v.strip() for k, v in jobs.items() if k.split() and v.strip()}})

    def resolve(self, host, port=None):
        """Host record for a name/alias/hostname/IP/context, or None. Several records (one IP, several ports):
        the matching port wins, else production."""
        recs = self.keys.get((host or "").strip().lower().strip("[]").rstrip(".")) or []
        if len(recs) > 1 and port:
            recs = [r for r in recs if r["port"] == port] or recs
        if len(recs) > 1:
            recs = [r for r in recs if r["kind"] == "prod"] or recs
        return recs[0] if recs else None


def load_view(path=None):
    """The guard view; None when the file is absent (nothing approved on this master); ViewError when unreadable."""
    path = path or os.path.join(orch_home(), "guard-view.json")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ViewError("cannot read %s (%s)" % (path, e.strerror or e))
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise ViewError("%s is not valid JSON" % path)
    return View(data)


EMPTY = {"schema": 1}


def classify(target, view=None):
    """"helper" | "prod" | "both" | None for one target as typed (name, alias, hostname, IP, user@host, host:port,
    ssh:// or tcp:// URL, docker context name)."""
    if view is None:
        view = CTX.view if CTX else load_view()
    if view is None:
        return None
    host, port = _host_port(target)
    rec = view.resolve(host, port) if host else None
    if rec is None:
        return None
    return "both" if rec["both"] else rec["kind"]


# ---------------------------------------------------------------- decision state (one check per process call)
class Ctx(object):
    def __init__(self, view, cwd, prefs, raw=""):
        self.view = view if view is not None else View(EMPTY)
        self.cwd = _norm_path(cwd) if cwd else ""
        self.prefs = prefs
        self.raw = raw
        self.asks = []        # (rule, reason)
        self.pending = []     # rule d candidates (what, tool words, path hints); decided once the scope is known
        self.warns = []       # (log line, path hints or None = always log)
        self.dirs = []        # cd targets seen so far, normalised
        self.cmd_hint = None  # one command's working dir from env -C / sudo -D (rule d hint, not a cd)
        self.dir_hints = []   # Start-Process/pwsh -WorkingDirectory stack (rule d hint around a recursive check)
        self.setup = None     # orch setup paths, computed on first use
        self.docker_default = None  # production engine picked by DOCKER_HOST/DOCKER_CONTEXT or `docker context use`
        self.vars = {}        # rule v: variable -> every value this command gave it (None = cannot be told)
        self.dyn = []         # rule v heavy-work asks (reason, path hints); decided with rule d's scope
        self.budget = 64      # rule v: expansions left for this check (bounds self-referencing values)
        self.mode = "sh"      # lexer mode of this check (sh | ps | cmd), for expanding redirect-target vars
        self.session = False  # an MCP terminal runs it: a session it opens can be typed into later (MF3)


CTX = None


class Deny(Exception):
    def __init__(self, rule, msg):
        Exception.__init__(self, msg)
        self.rule = rule
        self.msg = msg


def _msg(what, use):
    room = 160 - len("blocked: ; use: ") - len(use)
    if len(what) > room and ORCH in use:  # a long install path must not eat the reason: use orch's short name
        use = use.replace(ORCH, "orch")    # (SKILL.md defines orch = bash <skill dir>/scripts/orch)
        room = 160 - len("blocked: ; use: ") - len(use)
    if len(what) > room:
        what = what[:max(room - 3, 0)] + "..."
    return "blocked: %s; use: %s" % (what, use)


def blk(rule, what, use):
    raise Deny(rule, _msg(what, use))


def ask(rule, reason):
    if (rule, reason) not in CTX.asks:
        CTX.asks.append((rule, reason))


def si(node):
    return "%s run shell-investigate --node %s -- <cmd>" % (ORCH, node)


class Remote(str):
    """Where a command runs. The str value is the node name for messages; kind is helper | prod | other."""

    def __new__(cls, name, kind, label=None):
        r = str.__new__(cls, name)
        r.kind = kind
        r.label = label or name
        return r


def _remote(host, port=None):
    rec = CTX.view.resolve(host, port) if host else None
    if rec is None:
        return Remote(host or "?", "other")
    label = "%s (%s)" % (rec["name"], rec["spec"]) if rec["spec"] and rec["spec"] != rec["name"] else rec["name"]
    return Remote(rec["name"], rec["kind"], label)


def _kind(remote):
    return remote.kind if remote is not None else None


def ask_prod(what, remote):
    ask("p", "%s on production %s needs your OK" % (what, remote.label))


def deny_prod(what, remote):
    blk("p", "%s on production %s" % (what, remote.label), PROD_FIX)


ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "mksh", "busybox", "fish"}
PWSH = {"powershell", "pwsh", "powershell_ise"}
DOCKERS = {"docker", "docker-compose", "podman", "nerdctl"}
PREFIX_WORDS = {"!", "if", "then", "else", "elif", "do", "while", "until", "time",
                "exec", "command", "builtin", "noglob", "nohup", "setsid", "unbuffer",
                "&", ".", "call", "start", "{", "}"}
# wrappers that take options (some with a value) before the real command
WRAPPER_VALOPTS = {
    "sudo": {"-u", "-g", "-p", "-c", "-d", "-h", "-r", "-t", "-t", "-u", "-C", "-D", "-R", "-T", "-U",
             "--user", "--group", "--prompt", "--chdir"},
    "doas": {"-u", "-C"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "watch": {"-n", "--interval", "-d"},
    "xargs": {"-I", "-i", "-n", "-P", "-L", "-l", "-d", "-a", "-E", "-e", "-s",
              "--max-args", "--max-procs", "--delimiter", "--arg-file", "--replace"},
    "taskset": set(),
    "chrt": set(),
}
WRAPPER_POSITIONAL = {"timeout": 1, "taskset": 1, "chrt": 1}
# wrappers that run a shell-command string passed to -c/--command (su is the classic); their value is analysed
CMD_STR_WRAPPERS = {"su", "runuser", "sg", "flock", "script"}
# other pass-through wrappers to unwrap like env/sudo (their positional inner command is rechecked by every rule).
# time/nohup/setsid/unbuffer are PREFIX_WORDS; sudo/doas/nice/ionice/stdbuf/timeout/watch/xargs/taskset/chrt are
# WRAPPER_VALOPTS; su/runuser/sg/flock/script are CMD_STR_WRAPPERS. These are the rest the review named (R-Q23).
WRAPPERS = {"winpty", "strace", "ltrace", "parallel", "systemd-run", "pkexec", "chronic", "caffeinate"}
# command names that head something the guard must re-check when an UNKNOWN wrapper (winpty, flock, strace,
# runuser, systemd-run, parallel, pkexec, ...) is placed in front of them (R-Q23 fail-closed catch-all)
WRAP_HEADS = (SHELLS | PWSH | DOCKERS |
              {"ssh", "scp", "rsync", "sftp", "cmd", "wsl", "eval", "iex", "invoke-expression", "su",
               "dotnet", "npm", "npx", "bunx", "pnpx", "pnpm", "yarn", "bun", "git",
               "orch", "orch.py", "orch_init", "orch_init.py"})

REMOTE_BAD = {"rm", "rmdir", "kill", "pkill", "killall", "taskkill", "stop-process", "spps",
              "systemctl", "service", "wsl", "shutdown", "reboot", "poweroff", "halt",
              "init", "telinit", "stop-computer", "restart-computer", "stop-service",
              "restart-service", "remove-item", "ri", "del", "erase", "rd", "dd", "mkfs",
              "crictl", "ctr", "docker-compose", "podman", "nerdctl"}
E_SAFE = {"ls", "dir", "cat", "type", "test", "[", "[[", "stat", "file", "get-item", "gi",
          "get-childitem", "gci", "test-path", "get-content", "gc", "head", "tail", "wc",
          "echo", "printf", "write-output", "write-host", "grep", "egrep", "rg", "findstr",
          "select-string", "sls", "less", "more", "rm", "del", "erase", "remove-item", "ri",
          "unlink", "orch"}
GIT_SAFE = {"commit", "log", "diff", "show", "status", "grep", "add", "blame"}

# docker_host / docker_context value, including the PowerShell braced form ${env:docker_host} and a named
# -Value/-Path parameter (Set-Item -Path env:DOCKER_HOST -Value ...).
# the operator tolerates the append/default/null-coalescing spellings the shells honour: `+=` (bash/pwsh append),
# `:=` (${VAR:=default}), `??=` (pwsh 7 null-coalescing); the `+`/`?`/`:` go after any whitespace so the spaced
# PowerShell form `$env:DOCKER_HOST += v` matches too (R-Q21).
_OP = r"\s*:?(?:\+|\?\?)?[=,]\s*"
# the value stops at a closing brace too, so ${DOCKER_HOST:=ssh://ha} captures `ssh://ha`, not `ssh://ha}`.
_VAL = r"[^\s\"';|&)}]+"
RAW_DOCKER_HOST = re.compile(r"docker_host\}?[\"']?(?:" + _OP + r"|\s+)(?:-(?:value|path)\s+)?[\"']?(" + _VAL + r")")
RAW_DOCKER_CTX = re.compile(r"docker_context\}?[\"']?(?:" + _OP + r"|\s+)(?:-(?:value|path)\s+)?[\"']?(" + _VAL + r")")
# Variables only the user may set (R3/R40): the guard view, the approval gate and the test switches all key on
# these, so an agent-set value is a bypass. Matched as an inline VAR= prefix, 'VAR'/VAR} forms (PowerShell
# $env:/${env:}), or after a setter keyword (export/declare/typeset/readonly/local/setx/Set-Item/New-Item/
# [Environment]::SetEnvironmentVariable).
USERONLY_VARS = ("helper_orch_local", "orch_home", "orch_config", "orch_ci_child", "orch_fault",
                 "orch_selftest", "orch_test_mode", "orch_gc_node_json")
_UO = "|".join(USERONLY_VARS)
RAW_USERONLY = re.compile(
    r"\b(?:" + _UO + r")\}?[\"']?\s*:?(?:\+|\?\?)?[=,]|"
    r"\b(?:setx|set-item|new-item|si|ni|export|declare|typeset|readonly|local|setenvironmentvariable)\b"
    r"[^;\n|&]*\b(?:" + _UO + r")\b")


# ---------------------------------------------------------------- lexer
class Cmd(object):
    __slots__ = ("words", "redirs", "fed", "stdin_redir", "heredocs", "word_spans", "heredoc_spans", "end")

    def __init__(self, fed):
        self.words = []
        self.redirs = []      # (op, target)
        self.fed = fed        # stdin comes from a pipe
        self.stdin_redir = False
        self.heredocs = []    # heredoc bodies (an unquoted delimiter's $(...) / backticks already made NUL markers)
        self.word_spans = []  # raw [start, end) of each word in words (quotes included); for span blanking (R-Q20)
        self.heredoc_spans = []  # raw [start, end) of each heredoc body, parallel to heredocs
        self.end = ""         # the separator that ended it; PowerShell '(' / '{' = its arguments run on into a
        #                       local expression or script block (ssh pc (Get-Content x), MF2)


SH_SPECIAL = set(" \t\r\n'\"\\$`;&|<>(){}#*?[]!~")
SEPARATORS = set(";\n(){}")
REDIR_RE = re.compile(r"&>>?|<<<|<<-|<<|<>|<&|>>|>\||>&|<|>")
FD_RE = re.compile(r"\s*(\d+|-)")


def _capture_paren(s, i, quotes=True):
    """s[i] is just after '$(' ; return (content, index after matching ')')."""
    depth, q, n, start = 1, None, len(s), i
    while i < n:
        ch = s[i]
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"':
                i += 1
        elif ch in "'\"" and quotes:
            q = ch
        elif ch == "\\":
            i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
        i += 1
    if quotes:  # unbalanced quote (e.g. an apostrophe in a heredoc): count parens only
        return _capture_paren(s, start, False)
    return s[start:], n


def _local_subs(text, mode, subs):
    """Text the shell expands before a command reads it: an unquoted-delimiter heredoc body (sh) or a PowerShell
    @"..."@ here-string. Quotes are plain text there; each $(...) (and, in sh, `...`) is run by the master's shell
    first, so it goes to subs and becomes the NUL marker, exactly as in a double-quoted word (MF2). The escape
    character keeps a following $ literal (sh: only before $, ` and \\)."""
    if "$(" not in text and "`" not in text:
        return text                       # nothing the shell runs (a large heredoc stays a fast path)
    esc = "`" if mode == "ps" else "\\"
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == esc and i + 1 < n and (mode == "ps" or text[i + 1] in "$`\\"):
            out.append(text[i + 1])
            i += 2
            continue
        if ch == "$" and text[i + 1:i + 2] == "(":
            content, i = _capture_paren(text, i + 2)
            subs.append((content, mode))
            out.append("\x00")
            continue
        if ch == "`" and mode == "sh":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            subs.append((text[i + 1:j], mode))
            out.append("\x00")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def lex(s, mode):
    """Split a shell command string into simple commands.

    mode: 'sh' (bash/sh), 'ps' (PowerShell), 'cmd' (cmd.exe).
    Returns (cmds, subs) where subs are (script, mode) pairs from $(...) / backticks.
    """
    if mode == "sh":
        s = re.sub(r"\\\r?\n", "", s)              # bash joins backslash-newline
    s = re.sub(r"(\\|`|\^)\r?\n", " ", s)          # line continuations (all dialects)
    esc = "`" if mode == "ps" else ("^" if mode == "cmd" else "\\")
    cmds, subs = [], []
    st = {"cmd": Cmd(False), "cur": None, "redir": None, "heredoc": [], "wstart": 0, "i": 0}

    def add(chars):
        if st["cur"] is None:
            st["cur"] = []
        st["cur"].append(chars)

    def finish_word():
        cur = st["cur"]
        if cur is None:
            return
        w = "".join(cur)
        span = (st["wstart"], st["i"])
        st["cur"] = None
        if st["redir"] is not None:
            op = st["redir"]
            st["redir"] = None
            st["cmd"].redirs.append((op, w))
            if op in ("<<", "<<-"):
                # a quoted delimiter ('EOF', "EOF", \EOF) keeps the body literal; an unquoted one lets the shell
                # expand it first. The delimiter itself is the word after quote removal.
                raw = s[span[0]:span[1]]
                quoted = any(q in raw for q in "'\"\\")
                bare = "'" not in raw and '"' not in raw          # \EOF: the backslash is removed, 'E\OF' keeps it
                st["heredoc"].append((w.replace("\\", "") if bare else w, op == "<<-", st["cmd"], quoted))
        else:
            st["cmd"].words.append(w)
            st["cmd"].word_spans.append(span)

    def finish_cmd(next_fed=False):
        finish_word()
        c = st["cmd"]
        if c.words or c.redirs:
            cmds.append(c)
        st["cmd"] = Cmd(next_fed)

    i, n = 0, len(s)
    while i < n:
        st["i"] = i
        if st["cur"] is None:
            st["wstart"] = i          # the current (or next) word begins here (R-Q20 raw spans)
        ch = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        # escapes
        if ch == esc and mode != "sh":
            if nxt:
                add(nxt)
            i += 2
            continue
        if ch == "\\" and mode == "sh":
            if nxt in SH_SPECIAL and nxt:
                add(nxt)
                i += 2
            else:
                add("\\")
                i += 1
            continue
        # comments
        if ch == "#" and st["cur"] is None and mode != "cmd":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        # PowerShell here-strings @' ... '@
        if mode == "ps" and ch == "@" and nxt in "'\"" and nxt and s[i + 2:i + 3] in ("\n", "\r"):
            end = s.find("\n" + nxt + "@", i + 2)
            if end < 0:
                end = n
            add(_local_subs(s[i + 2:end], mode, subs) if nxt == '"' else s[i + 2:end])   # @"..."@ expands $(...)
            i = end + 3
            continue
        # quotes
        if ch == "$" and nxt == "'" and mode == "sh":
            i += 1
            continue
        if ch == "'":
            j = s.find("'", i + 1)
            if j < 0:
                j = n
            add(s[i + 1:j])
            if st["cur"] is None:
                add("")
            i = j + 1
            continue
        if ch == '"':
            i += 1
            buf = []
            while i < n and s[i] != '"':
                c2 = s[i]
                if c2 == esc and mode != "cmd" and i + 1 < n:
                    if mode == "ps" or s[i + 1] in '"\\$`':
                        buf.append(s[i + 1])
                        i += 2
                        continue
                if c2 == "$" and s[i + 1:i + 2] == "(" and mode != "cmd":
                    content, i = _capture_paren(s, i + 2)
                    subs.append((content, mode))
                    buf.append("\x00")
                    continue
                if c2 == "`" and mode == "sh":
                    j = s.find("`", i + 1)
                    if j < 0:
                        j = n
                    subs.append((s[i + 1:j], mode))
                    buf.append("\x00")
                    i = j + 1
                    continue
                buf.append(c2)
                i += 1
            add("".join(buf))
            i += 1
            continue
        # substitutions
        if ch in "$<>" and nxt == "(" and mode != "cmd":
            content, i = _capture_paren(s, i + 2)
            subs.append((content, mode))
            add("\x00")
            continue
        if ch == "`" and mode == "sh":
            j = s.find("`", i + 1)
            if j < 0:
                j = n
            subs.append((s[i + 1:j], mode))
            add("\x00")
            i = j + 1
            continue
        # redirections
        if ch in "<>" or (ch == "&" and nxt == ">"):
            if st["cur"] is not None and "".join(st["cur"]).isdigit():
                st["cur"] = None
            finish_word()
            m = REDIR_RE.match(s, i)
            op = m.group(0)
            i += len(op)
            if op.endswith("&"):
                m2 = FD_RE.match(s, i)
                if m2:
                    i += len(m2.group(0))
                    continue
            if op.startswith("<"):
                st["cmd"].stdin_redir = True
            st["redir"] = op
            while i < n and s[i] in " \t":
                i += 1
            continue
        # control operators
        if ch == "|":
            if nxt == "|":
                finish_cmd()
                i += 2
            else:
                finish_cmd(next_fed=True)
                i += 2 if nxt == "&" else 1
            continue
        if ch == "&":
            finish_cmd()
            i += 2 if nxt == "&" else 1
            continue
        if ch in "{}":
            if mode == "sh":
                # bash reserved words: `{`/`}` are a command group only as a standalone word in command position
                # (`{ cmd; }`). A go-template `{{.Names}}`, brace expansion `a{b,c}` or `{}` in find are ordinary
                # word characters, so they stay attached and never split the command (R28).
                at_cmd = st["cur"] is None and not st["cmd"].words and not st["cmd"].redirs
                nxt_ws = (i + 1 >= n) or s[i + 1] in " \t\r\n;&|"
                if at_cmd and (ch == "}" or nxt_ws):
                    finish_cmd()
                else:
                    add(ch)
                i += 1
                continue
            if st["cur"] is not None:  # ps/cmd: split on braces (PowerShell script blocks)
                add(ch)
                i += 1
                continue
        if ch == "(" and mode == "sh" and st["cur"] is not None and st["redir"] is None:
            w = "".join(st["cur"])
            if w.endswith("=") and ASSIGN_RE.match(w):
                add("\x00")            # NAME=(...) is an array: its value cannot be told (rule v)
        if ch in SEPARATORS:
            st["cmd"].end = ch
            finish_cmd()
            i += 1
            if ch == "\n" and st["heredoc"]:
                for delim, strip_tabs, owner, quoted in st["heredoc"]:
                    body, body_start, body_end = [], i, i
                    while i < n:
                        line_start = i
                        j = s.find("\n", i)
                        line = s[i:] if j < 0 else s[i:j]
                        i = n if j < 0 else j + 1
                        chk = line.rstrip("\r")
                        if (chk.lstrip("\t") if strip_tabs else chk) == delim:
                            body_end = line_start
                            break
                        body.append(line)
                        body_end = i
                    text = "\n".join(body)
                    owner.heredocs.append(text if quoted or mode != "sh" else _local_subs(text, mode, subs))
                    owner.heredoc_spans.append((body_start, body_end))
                st["heredoc"] = []
            continue
        if ch in " \t\r":
            finish_word()
            i += 1
            continue
        add(ch)
        i += 1
    st["i"] = n
    finish_cmd()
    return cmds, subs


# ---------------------------------------------------------------- helpers
def cmdname(w):
    w = w.lower().strip().strip("\x00")
    b = re.split(r"[\\/]", w)[-1]
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if b.endswith(ext):
            b = b[:-len(ext)]
    return b


def known_name(w):
    b = cmdname(w)
    if b in KNOWN:
        return b
    alt = cmdname(w.replace("\\", ""))
    return alt if alt in KNOWN else b

def first_nonopt(args, valopts=()):
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("-") and len(a) > 1:
            i += 2 if (a in valopts and "=" not in a) else 1
            continue
        return a
    return ""


# ---------------------------------------------------------------- paths (drive letters work on any OS)
DRIVE_RE = re.compile(r"^[a-z]:(?=/|$)")
MSYS_RE = re.compile(r"^/(?:mnt/|cygdrive/)?([a-z])(?=/|$)")
HOME_VARS = ("${home}", "$home", "${env:userprofile}", "$env:userprofile", "%userprofile%", "$env:home")
DYNAMIC_RE = re.compile(r"[$*?\[{\x00%]")
PREFS_JSON_RE = re.compile(r"(^|[\\/])guard\.json$")
PS_DYN = re.compile(r"(?:^|[\s;|{(])[&.]\s*(?:\(|\$(?:\{([^}]*)\}|((?:[A-Za-z]+:)?\w+))?)")


def _ps_dynamic(script):
    """A PowerShell & / . call of an expression, or of a variable this script has not given only fixed text."""
    for m in PS_DYN.finditer(script):
        vals = CTX.vars.get(_var_key(m.group(1) if m.group(1) is not None else m.group(2), "ps"))
        if not vals or None in vals:
            return True
    return False


def _home():
    return os.path.expanduser("~").replace("\\", "/").rstrip("/")


def _norm_path(p, base=""):
    """Case-folded absolute path with forward slashes; '' when it cannot be told (relative without a base)."""
    p = (p or "").strip().strip("\"'").replace("\\", "/")
    if not p or "\x00" in p:
        return ""
    low = p.lower()
    for var in HOME_VARS:
        if low.startswith(var) and (len(p) == len(var) or p[len(var)] == "/"):
            p = _home() + p[len(var):]
            break
    if p == "~" or p.startswith("~/"):
        p = _home() + p[1:]
    p = p.lower()
    if not (p.startswith("/") or DRIVE_RE.match(p)):
        if not base:
            return ""
        p = base + "/" + p
    drive = ""
    if DRIVE_RE.match(p):
        drive, p = p[:2], (p[2:] or "/")
    return (drive + _strip_win_tails(posixpath.normpath(p))).rstrip("/") or "/"


def _strip_win_tails(p):
    """Trailing dots and spaces are dropped from every path component when Windows writes the file, so
    `config.json.` and `config.json ` both land on `config.json`. Normalise them away (except '.' and '..')
    before any setup-file comparison. Harmless on Linux: it only fails closed on odd names."""
    out = []
    for s in p.split("/"):
        out.append(s if s in (".", "..") else (s.rstrip(". ") or s))
    return "/".join(out)


def _alts(p):
    """A normalised path plus its drive spelling for Git Bash / WSL / Cygwin forms (/c/x, /mnt/c/x -> c:/x)."""
    out = {p} if p else set()
    m = MSYS_RE.match(p or "")
    if m:
        out.add(m.group(1) + ":" + p[m.end():])
    return out


def _under(p, root):
    """Path p is root or inside it (normalised paths, any spelling)."""
    for a in _alts(p):
        for r in _alts(root):
            r = r.rstrip("/")
            if a == r or a.startswith(r + "/"):
                return True
    return False


def _base():
    return CTX.dirs[-1] if CTX.dirs else CTX.cwd


CD_CMDS = {"cd", "chdir", "pushd", "set-location", "sl", "push-location"}


def _note_cd(args):
    k = 0
    while k < len(args):
        a = args[k].lower()
        if a in ("-path", "-literalpath", "-lp"):
            k += 1
            break
        if a == "/d" or (a.startswith("-") and a != "-"):
            k += 1
            continue
        break
    if k < len(args) and args[k] != "-":
        n = _norm_path(args[k], _base())
        if n:
            CTX.dirs.append(n)


# ---------------------------------------------------------------- rules e and s: user-only and orch setup files
DELETERS = {"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "unlink"}
MOVERS = {"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni"}
COPIERS = {"cp", "copy", "copy-item", "cpi", "install", "ln", "xcopy", "robocopy", "rsync", "scp"}
SAFE_EXTRA = {"mkdir", "du", "tree", "diff", "cmp", "sha256sum", "md5sum", "jq"}


def _setup():
    """(files, dirs, holders): orch setup files, store state folders, and the folders that hold them."""
    if CTX.setup is None:
        homes = [h for h in {_norm_path(orch_home()), _norm_path("~/.helper-orch")} if h]
        stores = [p["store"] for p in CTX.view.projects if p["store"]]
        # the journal is evidence too (orch journal add/end is the only writer)
        CTX.setup = ([h + "/" + f for h in homes for f in SETUP_FILES] + [s + "/config.json" for s in stores],
                     [s + "/" + d for s in stores for d in ("state", "journal")], homes + stores)
    return CTX.setup


def _protected(t, local):
    """('e' ALLOW_LOCAL | 'g' guard.json | 's' orch setup file, label) for a word naming one; else None.
    Remote commands get only the name checks: their paths are not this master's files."""
    lt = t.lower().replace("\\", "/")
    if "allow_local" in lt:
        return "e", "ALLOW_LOCAL"
    if PREFS_JSON_RE.search(lt):
        return "g", "guard.json"
    if ".helper-orch" in lt:
        tail = _strip_win_tails(lt.split(".helper-orch", 1)[1])  # Win32 drops a trailing dot on write
        if DYNAMIC_RE.search(tail):
            return "e", "a dynamic .helper-orch path"
        for f in SETUP_FILES:
            if tail.endswith("/" + f):
                return "s", f
    if not local:
        return None
    files, dirs, holders = _setup()
    flat = re.sub(r"/+", "/", lt)  # a setup path quoted inside code, e.g. python -c "open('C:/...')"
    for f in files + dirs:
        for a in _alts(f):
            if a in flat and re.search(r"(?<![\w./-])" + re.escape(a) + r"(?=[. ]*(?:$|[^\w.-]))", flat):
                return "s", f.rsplit("/", 1)[-1] + ("/" if f in dirs else "")
    n = _norm_path(t, _base())
    if not n:
        return None
    for f in files + dirs:
        if _under(n, f):
            return "s", f.rsplit("/", 1)[-1] + ("/" if f in dirs else "")
    if any(x == y for p in CTX.view.projects if p["store"] for x in _alts(n) for y in _alts(p["store"])):
        return "s", "store folder"
    if DYNAMIC_RE.search(n) and any(_under(n, h) for h in holders):
        return "s", "orch setup files"
    return None


def _covers(a):
    """A path that is (or holds) an orch setup file: deleting or moving it removes the setup. Glob-aware: a
    wildcard/brace path is cut at its first dynamic character and the static directory prefix is tested, so
    `~/.helper-orch/*` and `{guard-view,trust}.json` are covered too."""
    n = _norm_path(a, _base())
    if not n:
        return False
    files, dirs, _ = _setup()
    if any(_under(f, n) for f in files + dirs):
        return True
    m = DYNAMIC_RE.search(n)
    if m:
        prefix = n[:m.start()].rsplit("/", 1)[0]
        if prefix and any(_under(f, prefix) for f in files + dirs):
            return True
    return False


def _into_setup(a):
    """A copy/move destination that is ORCH_HOME or a store folder itself, or inside a store."""
    n = _norm_path(a, _base())
    _, _, holders = _setup()
    stores = [p["store"] for p in CTX.view.projects if p["store"]]
    return bool(n) and (any(x == y for h in holders for x in _alts(n) for y in _alts(h)) or
                        any(_under(n, st) for st in stores))


def _sed_inplace(largs):
    return any(re.match(r"^-[a-z]*i", a) or a.startswith("--in-place") for a in largs)


def copy_ends(name, args):
    """(sources, dest) for a copy/move command, so an option-given destination is seen, not only the last
    word. Honours cp/mv/install/ln -t/-tDIR/--target-directory[=], PowerShell -Destination/-Path/-LiteralPath,
    robocopy (2nd positional), and xcopy (2nd positional)/copy/move (last positional). dest '' when unknown."""
    la = [a.lower() for a in args]
    if name in ("cp", "mv", "install", "ln"):
        dest, pos, i = None, [], 0
        while i < len(args):
            a, al = args[i], la[i]
            if al in ("-t", "--target-directory"):
                dest, i = (args[i + 1] if i + 1 < len(args) else ""), i + 2
            elif al.startswith("-t") and not al.startswith("--") and len(al) > 2:
                dest, i = args[i][2:], i + 1
            elif al.startswith("--target-directory=") or al.startswith("--target="):
                dest, i = args[i].split("=", 1)[1], i + 1
            elif al == "--":
                pos += args[i + 1:]
                break
            elif al.startswith("-") and al != "-":
                i += 1
            else:
                pos.append(a)
                i += 1
        if dest is None:
            dest, pos = (pos[-1] if pos else ""), pos[:-1]
        return pos, dest
    if name in ("copy-item", "move-item", "cpi", "mi"):
        dest, srcs, pos, i = None, [], [], 0
        while i < len(args):
            a, al = args[i], la[i]
            if al.startswith("-") and al != "-":
                o, _, inline = al.lstrip("-").partition(":")
                if "=" in o:
                    o, inline = o.split("=", 1)
                v = inline or (args[i + 1] if i + 1 < len(args) and not la[i + 1].startswith("-") else "")
                took = 1 if (inline or not v) else 2
                if len(o) >= 2 and "destination".startswith(o):
                    dest = v
                elif len(o) >= 1 and ("path".startswith(o) or "literalpath".startswith(o)):
                    srcs.append(v)
                i += took
            else:
                pos.append(a)
                i += 1
        if dest is None:
            dest = pos[1] if len(pos) > 1 else (pos[0] if pos else "")
            srcs += pos[:1] if len(pos) > 1 else []
        else:
            srcs += pos
        return srcs, dest
    pos = [a for a in args if not a.startswith("/")]
    if name in ("robocopy", "xcopy"):
        return pos[:1], (pos[1] if len(pos) > 1 else "")
    if name in ("copy", "move"):
        return pos[:-1], (pos[-1] if pos else "")
    pos = [a for a in args if not a.startswith("-")]
    return pos[:-1], (pos[-1] if pos else "")


def check_protected(c, words, remote):
    local = remote is None
    hits = [(t, _protected(t, local)) for t in list(words) + [t for _, t in c.redirs if t]]
    hits = [(t, k) for t, k in hits if k]
    for op, t in c.redirs:
        if ">" not in op or not t:
            continue
        for tt in (_expand(t, CTX.mode, CTX.vars) if _word_has_var(t, CTX.mode) else [t]):  # D=~/.orch; > $D/..
            if tt is None:
                continue
            k = _protected(tt, local)
            if k and k[0] == "s":
                blk("s", "writing orch setup file %s" % k[1], INIT)
            if k:
                blk("e", "writing a user-only file (%s)" % tt, USER)
    w = list(words)
    while w and (ASSIGN_RE.match(w[0]) or cmdname(w[0]) in PREFIX_WORDS or cmdname(w[0]) in ("sudo", "env")):
        w = w[1:]
    if not w:
        return
    name = known_name(w[0])
    if name in SHELLS or name in PWSH or name in ("cmd", "eval", "iex", "invoke-expression", "ssh", "wsl",
                                                 "orch", "orch.py"):
        return  # the inner command is checked on recursion; orch writes its own files
    src_words, dest_word = (copy_ends(name, w[1:]) if name in COPIERS | MOVERS else ([], ""))
    if local and name in DELETERS | MOVERS:
        for a in w[1:]:
            if not a.startswith("-") and _covers(a):
                blk("s", "'%s' on orch setup files (%s)" % (name, a), INIT)
    if local and name in COPIERS | MOVERS and dest_word and _into_setup(dest_word):
        blk("s", "'%s' into orch setup folder %s" % (name, dest_word), INIT)
    if local and name == "find":
        la = [x.lower() for x in w[1:]]
        bad = "-delete" in la or any(la[k] in ("-exec", "-execdir", "-ok", "-okdir") and
                                     k + 1 < len(la) and cmdname(la[k + 1]) in DELETERS | MOVERS
                                     for k in range(len(la)))
        if bad:
            starts = []
            for a in w[1:]:
                if a.startswith("-") or a in ("(", ")", "!"):
                    break
                starts.append(a)
            for a in starts:
                if _covers(a):
                    blk("s", "'find %s' on orch setup files (%s)" %
                        ("-delete" if "-delete" in la else "-exec", a), INIT)
    if not hits:
        return
    largs = [x.lower() for x in w[1:]]
    if name == "git" and first_nonopt(largs, {"-c", "-C"}) in GIT_SAFE:
        return
    for t, (k, label) in hits:
        if k == "e" and name in E_SAFE:
            continue
        if k != "e" and (name in (E_SAFE - DELETERS) | CD_CMDS | SAFE_EXTRA or (name == "sed" and not
                         _sed_inplace(largs)) or (name == "find" and not FIND_ACT & set(largs)) or
                         (name in COPIERS and t in src_words)):
            continue
        if k == "s":
            blk("s", "'%s' on orch setup file %s" % (name, label), INIT)
        blk("e", "'%s' on %s" % (name, label), USER)


# ---------------------------------------------------------------- rule d scope and the job index
PATH_OPTS = {"-C", "--prefix", "--project", "--cwd", "--dir", "--project-directory", "--chdir"}


def flag_exists():
    try:
        return os.path.isfile(os.path.join(orch_home(), "ALLOW_LOCAL"))
    except Exception:
        return False


def _pathy(a):
    return "/" in a or "\\" in a or a[:1] in ("~", ".")


def _hints(args):
    """Path-like arguments of one command, plus the values of -C/--prefix/--project/--cwd/--dir."""
    out = []
    for k, a in enumerate(args):
        if a in PATH_OPTS:
            out += args[k + 1:k + 2]
        elif a.startswith("--") and "=" in a:
            o, v = a.split("=", 1)
            if o in PATH_OPTS or _pathy(v):
                out.append(v)
        elif not a.startswith("-") and _pathy(a):
            out.append(a)
    if CTX.cmd_hint:                 # env -C / sudo -D: this command's working dir (R29)
        out.append(CTX.cmd_hint)
    out += CTX.dir_hints             # Start-Process / pwsh -WorkingDirectory (R29)
    return out


def _ps_val(args, largs, names):
    """Value of a PowerShell named parameter (exact name or an unambiguous >=2-char prefix of one of `names`),
    inline (-name:val / -name=val) or the next word; '' if absent."""
    i = 0
    while i < len(args):
        tok = largs[i]
        o = tok.lstrip("-/").split(":")[0].split("=")[0]
        if o in names or (len(o) >= 2 and any(nm.startswith(o) for nm in names)):
            if ":" in tok:
                return args[i].split(":", 1)[1]
            if "=" in tok:
                return args[i].split("=", 1)[1]
            return args[i + 1] if i + 1 < len(args) else ""
        i += 1
    return ""


def _projects_at(hints=()):
    """Approved projects whose root holds the payload cwd, a cd path or one of the hints."""
    projs = [p for p in CTX.view.projects if p["root"]]
    if not projs:
        return []
    base = _base()
    cands = [x for x in [CTX.cwd] + CTX.dirs + [_norm_path(h, base) for h in hints] if x]
    return [p for p in projs if any(_under(x, p["root"]) for x in cands)]


def _d_scope(hints):
    return CTX.prefs.get("heavy_scope") == "everywhere" or bool(_projects_at(hints))


PM = {"npm", "pnpm", "yarn", "bun"}
PM_VAL = {"--prefix", "-c", "--dir", "--cwd", "--filter", "-f", "--workspace", "-w", "--cache", "--registry",
          "--userconfig"}
PM_OWN = {"add", "install", "i", "remove", "rm", "uninstall", "update", "up", "upgrade", "init", "info", "config",
          "cache", "workspace", "workspaces", "dlx", "exec", "why", "set", "link", "ln", "unlink", "pack", "publish",
          "npm", "plugin", "node", "bin", "global", "version", "constraints", "import", "rebuild", "prune", "fetch",
          "patch", "audit", "list", "ls", "outdated", "licenses", "create", "store", "env", "setup", "root",
          "deploy", "doctor", "help"}
NPX = {"npx", "bunx", "pnpx"}
NPX_VAL = {"-p", "--package", "-c", "--call"}
RUNNERS = {"uv", "poetry", "pipenv", "hatch", "pdm", "rye"}
RUNNER_VAL = {"--with", "--python", "-p", "--project", "--directory", "--extra", "--group", "--env-file",
              "--package", "--from", "--index", "-e", "--env"}


def _opts_off(w, valopts):
    """w without its leading options (an option in valopts also takes the next word)."""
    i = 0
    while i < len(w) and w[i].startswith("-") and len(w[i]) > 1:
        if w[i] == "--":
            return w[i + 1:]
        i += 2 if w[i] in valopts else 1
    return w[i:]


def _canon(w, depth=0, key=False):
    """The word lists one command (or, key=True, job-index key) can be matched by: as typed, without options, and
    with its launcher dropped: python -m X == X; npx/bunx/pnpm exec/yarn dlx/npm exec X == X; uv/poetry/pipenv/
    hatch/pdm/rye run X == X; npm/pnpm/yarn/bun run X == run X; npm/pnpm/yarn test == run test; pnpm/yarn X ==
    run X. A command's `<pm> run X` also matches the tool X (a package manager runs a binary of that name); a
    key's does not, so a script name is never taken for a tool."""
    if not w or depth > 3:
        return []
    t = cmdname(w[0])
    t = "pytest" if t == "py.test" else t
    a = [x.lower() for x in w[1:]]
    out = [[t] + a, [t] + [x for x in a if not x.startswith("-")]]
    if PY_RE.match(t):
        k = 0
        while k < len(a) and a[k].startswith("-") and a[k] not in ("-c", "-") and not a[k].startswith("-m"):
            k += 2 if a[k] in ("-w", "-x") else 1
        if k < len(a) and a[k].startswith("-m"):
            mod, rest = (a[k][2:], a[k + 1:]) if len(a[k]) > 2 else (a[k + 1] if k + 1 < len(a) else "", a[k + 2:])
            if mod:
                out.append([t, "-m", mod] + rest)
                out += _canon([mod] + rest, depth + 1, key)
    elif t in NPX:
        out += _canon(_opts_off(a, NPX_VAL), depth + 1, key)
    elif t in RUNNERS:
        if "run" in a:
            out += _canon(_opts_off(a[a.index("run") + 1:], RUNNER_VAL), depth + 1, key)
    elif t in PM:
        r = _opts_off(a, PM_VAL)
        out.append([t] + r)
        sub = r[0] if r else ""
        if sub in ("run", "run-script", "rum", "urn"):
            s = _opts_off(r[1:], ())
            if s:
                out += [["run"] + s] + ([] if key else _canon(s, depth + 1))
        elif sub in ("test", "t", "tst") and t != "bun":
            out.append(["run", "test"] + r[1:])
        elif sub in ("exec", "x", "dlx"):
            out += _canon(_opts_off(r[1:], ()), depth + 1, key)
        elif sub and t in ("pnpm", "yarn") and sub not in PM_OWN:
            out.append(["run"] + r)       # pnpm/yarn run a package script for a name that is not their own
    return out


def _best_job(tool, projects):
    """The job whose job-index tool prefix (canonical forms) is the longest that starts this command."""
    forms = _canon(tool)
    best, n = None, 0
    for p in projects:
        for k, job in p["jobs"].items():
            for kf in _canon(k.split(), key=True):
                if len(kf) > n and any(f[:len(kf)] == kf for f in forms):
                    best, n = job, len(kf)
    return best


def _use_run(tool, node=None, hints=()):
    """`orch run <job>`, naming the job when a job-index tool prefix matches the command."""
    best = _best_job([w for w in tool if w], _projects_at(hints) or CTX.view.projects)
    tail = " --node %s" % node if node else ""
    return "%s run %s%s" % (ORCH, best, tail) if best else "%s run <job>%s (see orch init --show)" % (ORCH, tail)


def heavy(what, tool, remote, args=()):
    """Rule b on a helper now; rule d on the master once the scope is known (end of check)."""
    if remote:
        blk("b", "%s on helper %s" % (what, remote), _use_run(tool, remote))
    if not flag_exists():
        CTX.pending.append((what, list(tool), _hints(args)))


def new_heavy(what, remote, tool, args=()):
    if CTX.prefs.get("new_heavy") == "deny":
        heavy(what, tool, remote, args)
    elif not flag_exists():
        CTX.warns.append(("warn %s%s :: %s" % (what, " on " + remote if remote else "", CTX.raw[:300]),
                          None if remote else _hints(args)))


# ---------------------------------------------------------------- analysis
INLINE_DOCKER = re.compile(
    r"""["']?[ \t]+(?:[a-z_][a-z0-9_]*=[^\s;|&]*[ \t]+)*(?:(?:sudo|env|command|exec|nice|nohup|time)[ \t]+"""
    r"""(?:-\S*[ \t]+)*)*["']?(?:[^\s"';|&]*[\\/])?(?:docker|docker-compose|podman|nerdctl)(?:\.exe)?["']?(?:\s|$)""")


def _raw_target(m, low, var):
    """DOCKER_HOST / DOCKER_CONTEXT text: a helper -> rule a; production -> fine only as the prefix of one docker
    command (that command is then checked), else ask (later docker commands would reach it unseen)."""
    th, tp = _host_port(m.group(1))
    rec = CTX.view.resolve(th, tp) if th else None
    if rec is None:
        return
    if rec["kind"] == "helper":
        blk("a", "%s to helper %s" % (var, rec["name"]), si(rec["name"]))
    if not INLINE_DOCKER.match(low, m.end()):
        r = _remote(th, tp)
        CTX.docker_default = CTX.docker_default or r
        ask("p", "pointing %s at production %s needs your OK (later docker commands would reach it unseen)"
            % (var, r.label))


TOKENS = ("docker_host", "docker_context") + USERONLY_VARS


def _strip_prefix(words):
    """Drop leading assignments and PREFIX/sudo/env wrapper words to reach the real command name."""
    w = list(words)
    while w and (ASSIGN_RE.match(w[0]) or cmdname(w[0]) in PREFIX_WORDS or cmdname(w[0]) in ("sudo", "env")):
        w = w[1:]
    return w


def _msg_value_spans(args, spans, opts):
    """Raw spans of the given message-option values (-m/--message/-F/--body ...). An inline =value blanks the
    whole option word (`-m=...`, `--body=...`); a next-word value blanks that word. spans parallel args; a None
    span (a synthetic word) is skipped. Blanking the whole inline word is safe: the option name holds no token."""
    out, i = [], 0
    while i < len(args):
        o = args[i].lower().split("=", 1)[0]
        if o in opts:
            if "=" in args[i]:
                if spans[i] is not None:
                    out.append(spans[i])
            elif i + 1 < len(args):
                if spans[i + 1] is not None:
                    out.append(spans[i + 1])
                i += 1
        i += 1
    return out


def _inert_spans(words, spans, heredoc_spans):
    """Raw spans of the inert data of one simple command (R27), blanked so the DOCKER_HOST/CONTEXT and user-only
    scans never fire on text that does not run: an orch call's args and stdin, a git/gh commit/tag/PR message.
    words carry their raw spans (spans[i] is None for a synthetic word, e.g. an expanded command name, which is
    never blanked -> fail closed). Prefix assignments and wrapper words are dropped with their spans, so a real
    `DOCKER_HOST=... orch ...` prefix and a non-orch heredoc are still scanned."""
    w, sp = list(words), list(spans)
    while w and (ASSIGN_RE.match(w[0]) or cmdname(w[0]) in PREFIX_WORDS or cmdname(w[0]) in ("sudo", "env")):
        w, sp = w[1:], sp[1:]
    if not w:
        return []
    name, rest, rsp = known_name(w[0]), w[1:], sp[1:]
    if name in SHELLS:                     # ORCH is `bash <path>/scripts/orch ...`: unwrap one shell layer
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 1
        if j < len(rest):
            name, rest, rsp = known_name(rest[j]), rest[j + 1:], rsp[j + 1:]
    if _orch_entry(name, rest)[0]:
        return [s for s in rsp if s is not None] + list(heredoc_spans)   # orch never runs its args/stdin
    if name == "git" and first_nonopt([a.lower() for a in rest], {"-c", "-C"}) in ("commit", "tag"):
        return _msg_value_spans(rest, rsp, ("-m", "--message", "-f", "--file"))
    if name == "gh":
        return _msg_value_spans(rest, rsp, ("-b", "--body", "--body-file", "-f", "--file"))
    return []


def _scan_text(script, cmds, mode="sh"):
    """The script lowercased, with inert data blanked IN PLACE (by raw span, never by string replace) so the
    DOCKER_HOST/CONTEXT and user-only-variable scans do not fire on text that does not run (R27): an orch
    invocation never runs its own args or stdin, and a git/gh commit/tag/PR message is text. Blanking by position
    means a data word can no longer wipe an identical real `export DOCKER_HOST=...` elsewhere in the command
    (R-Q20). Setter keywords, env prefixes and non-orch heredocs keep their text, so setx/$env:/
    SetEnvironmentVariable and `cat >> profile <<EOF export DOCKER_HOST=... EOF` are still caught. A command whose
    name is a variable (O="bash .../orch"; $O journal add <<'EOF') is judged by every value the script gave it."""
    low = script.lower()
    spans = []
    table = {k: list(v) for k, v in CTX.vars.items()}
    for c in cmds:
        dyn = None if mode == "ps" and _ps_assign(c.words) else _dyn_head(c.words, mode)
        if dyn is None:
            spans += _inert_spans(list(c.words), list(c.word_spans), c.heredoc_spans)
        else:
            k, rest = dyn
            tail_spans = c.word_spans[k + 1:] if (k is not None and k + 1 <= len(c.word_spans)) else \
                [None] * len(rest[1:])
            for e in _expand(dyn[1][0], mode, table):
                if e is not None:
                    ew = e.split()
                    spans += _inert_spans(ew + list(rest[1:]), [None] * len(ew) + list(tail_spans),
                                          c.heredoc_spans)
        _note_vars(c.words, mode, table)
    buf = list(low)
    nbuf = len(buf)
    for a, b in spans:
        if a is None or b is None or not (0 <= a <= b <= nbuf):
            continue
        if any(t in low[a:b] for t in TOKENS):   # only blank data that could trip a scan
            for pos in range(a, b):
                if buf[pos] != "\n":
                    buf[pos] = " "
    return "".join(buf)


def _uo_name(seg):
    for v in USERONLY_VARS:
        if v in seg:
            return v.upper()
    return "a user-only variable"


# ---------------------------------------------------------------- rule v: command names held in variables
SH_REF = re.compile(r"\$(?:\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*)|[0-9@*#?$!-])")
PS_REF = re.compile(r"\$(?:\{([^}]*)\}|((?:[A-Za-z]+:)?[A-Za-z_][A-Za-z0-9_]*)|[?^$])")
SH_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PS_NAME = re.compile(r"^(?:(global|script|local|private|using|env):)?([a-z_][a-z0-9_]*)$")
PS_ASSIGN = re.compile(r"^(?:\[[^\]]*\])*\$(?:\{([^}]*)\}|((?:[A-Za-z]+:)?\w+))((?:[-+*/%]|\?\?)?=(?!=))?(.*)$", re.S)
PS_OP = re.compile(r"^((?:[-+*/%]|\?\?)?=)(?!=)(.*)$", re.S)
VAR_SET = {"export", "declare", "typeset", "local", "readonly"}
VAR_READ = {"read", "mapfile", "readarray", "getopts", "unset", "let", "printf", "wait"}
PS_SETVAR = {"set-variable", "sv", "new-variable", "nv"}
PS_UNVAR = {"remove-variable", "rv", "clear-variable", "clv"}
PS_SETVAR_VAL = ("scope", "option", "description", "visibility", "include", "exclude")
MAX_VALS = 8
SPELL = "'%s' needs your OK (its command name comes from %s: spell the command out)"
DYN_ANY = {"prune", "--rmi", "--rmi=all", "--shutdown", "--terminate"}   # rule c / rule 4 shapes: anywhere
DYN_WORDS = {"build", "test", "install", "publish", "restore", "pack", "ci", "up", "msbuild", "vstest", "buildx",
             "compose", "playwright", "pytest", "py.test", "pip", "pip3", "dotnet", "npm", "npx", "pnpm", "yarn",
             "bun", "deno", "uv", "make", "cmake", "cargo", "go", "mvn", "mvnw", "gradle", "gradlew"}
STDIN_RUNNERS = SHELLS | PWSH | {"python", "python3", "perl", "cmd"}


def _var_key(name, mode):
    """Table key of a variable as typed; None for anything but a plain name (positional, special, ${x:-y},
    ${x[0]}, ${!x}). sh names keep their case; PowerShell names fold case and drop a scope other than env:."""
    if name is None:
        return None
    if mode == "ps":
        m = PS_NAME.match(name.lower())
        return ("env:" if m.group(1) == "env" else "") + m.group(2) if m else None
    return name if SH_NAME.match(name) else None


def _dyn_word(w, mode):
    """A command word whose name is known only at run time: a $NAME/${...} or $(...) in its last path part."""
    if mode == "cmd":
        return False
    b = re.split(r"[\\/]", w or "")[-1]
    return "\x00" in b or bool((PS_REF if mode == "ps" else SH_REF).search(b))


def _word_has_var(w, mode):
    """A $NAME / ${...} / $(...) anywhere in the word (not only the last path part): used to substitute host and
    path arguments (ssh $H, scp $P:/x, docker -H ssh://$H, > $D/trust.json), where the variable is rarely last."""
    if mode == "cmd":
        return False
    return "\x00" in (w or "") or bool((PS_REF if mode == "ps" else SH_REF).search(w or ""))


def _expand(word, mode, table, keep=False):
    """Every text the word can take once each $NAME / ${NAME} is replaced by a value this command gave NAME (at
    most MAX_VALS). None stands for one that cannot be told: command substitution, a name never set here or set
    by read/eval/an array, a special or positional parameter, an operator expansion. keep=True (assignment
    values) leaves such a reference as typed instead."""
    if "\x00" in word:
        return [None]
    outs, pos = [""], 0
    for m in (PS_REF if mode == "ps" else SH_REF).finditer(word):
        lit, pos = word[pos:m.start()], m.end()
        name = m.group(1) if m.group(1) is not None else m.group(2)
        vals = table.get(_var_key(name, mode))
        if not vals:
            if not keep:
                return [None]
            vals = [m.group(0)]
        outs = list(dict.fromkeys(None if o is None or v is None else o + lit + v for o in outs for v in vals))
        if len(outs) > MAX_VALS:
            outs = outs[:MAX_VALS - 1] + [None]
    return [None if o is None else o + word[pos:] for o in outs]


def _set_var(table, key, vals):
    """Add values a variable may hold. Every value counts: the guard does not follow control flow."""
    if key is None:
        return
    cur = table.setdefault(key, [])
    for v in vals:
        if v not in cur:
            cur.append(v)
    if len(cur) > MAX_VALS:
        cur[MAX_VALS - 1:] = [None]


def _ps_assign(words):
    """(key, op, right-side words) when a PowerShell statement assigns a variable ($x = ..., $x=..., [int]$x += ...),
    else None."""
    m = PS_ASSIGN.match(words[0]) if words else None
    if not m:
        return None
    op, tail, rest = m.group(3), m.group(4), list(words[1:])
    if op:
        rest = ([tail] if tail else []) + rest
    elif tail or not rest or not PS_OP.match(rest[0]):
        return None                       # $x.y(), $x[0], $x -eq 1: not an assignment
    else:
        mm = PS_OP.match(rest[0])
        op, rest = mm.group(1), ([mm.group(2)] if mm.group(2) else []) + rest[1:]
    return _var_key(m.group(1) if m.group(1) is not None else m.group(2), "ps"), op, rest


def _ps_rhs(rhs, table):
    """(values, command words) of a PowerShell assignment's right side. One word holding a space is a quoted string;
    $y copies y; a lone bare word may be a string or a command (output unknown); a bare word with arguments is a
    command, which runs (its output, unknown here, is the value)."""
    h = rhs[0] if rhs else "\x00"
    if "\x00" in h:
        return [None], None
    if len(rhs) == 1:
        if h.startswith("$"):
            return _expand(h, "ps", table), None
        return ([h] if re.search(r"\s", h) else [h, None]), None
    if h[:1] in "$@([{,-0123456789" or re.search(r"\s", h):
        return [None], None               # an expression
    return [None], rhs


def _ps_setvar(name, args, table):
    """Set-Variable/New-Variable -Name N -Value V (or positional N V); Remove-/Clear-Variable N."""
    nm, val, pos, i = None, None, [], 0
    while i < len(args):
        a = args[i]
        o = a[1:].lower() if a.startswith("-") and len(a) > 1 else None
        if o is None:
            pos.append(a)
            i += 1
        elif "name".startswith(o):
            nm, i = (args[i + 1] if i + 1 < len(args) else None), i + 2
        elif len(o) >= 2 and "value".startswith(o):
            val, i = (args[i + 1] if i + 1 < len(args) else None), i + 2
        else:
            i += 2 if len(o) >= 2 and any(p.startswith(o) for p in PS_SETVAR_VAL) else 1
    if nm is None and pos:
        nm, pos = pos[0], pos[1:]
    if val is None and pos:
        val = pos[0]
    vals = [None] if name in PS_UNVAR or val is None or "\x00" in val else \
        (_expand(val, "ps", table) if val.startswith("$") else [val])
    for n in (nm or "").split(","):
        _set_var(table, _var_key(n.strip().lstrip("$"), "ps"), vals)


def _note_vars(words, mode, table):
    """Record what one simple command does to variables later command names may use: NAME=value alone or after
    export/declare/typeset/local/readonly, for NAME in ..., PowerShell $NAME = ... and Set-Variable. read, mapfile,
    getopts, printf -v, unset, let and Remove-Variable make the names they touch unknown; source, . and eval of
    text not spelled out make every name unknown."""
    w = list(words)
    while w and cmdname(w[0]) in PREFIX_WORDS and w[0] != ".":
        w = w[1:]
    if not w or mode == "cmd":
        return
    if mode == "ps":
        pa = _ps_assign(w)
        if pa is not None:
            _set_var(table, pa[0], _ps_rhs(pa[2], table)[0] if pa[1] == "=" else [None])
            return
    elif all(ASSIGN_RE.match(x) for x in w):
        for x in w:
            k, v = x.split("=", 1)
            _set_var(table, k.rstrip("+"), [None] if k.endswith("+") else _expand(v, mode, table, True))
        return
    while w and ASSIGN_RE.match(w[0]):    # VAR=x cmd: that prefix lasts for the one command
        w = w[1:]
    if not w:
        return
    name, args = cmdname(w[0]), w[1:]
    if name in ("source", ".") or (name in ("eval", "iex", "invoke-expression") and
                                   any("\x00" in a or _dyn_word(a, mode) for a in args)):
        for k in list(table):
            _set_var(table, k, [None])
    elif mode == "ps":
        if name in PS_SETVAR | PS_UNVAR:
            _ps_setvar(name, args, table)
    elif name in VAR_SET:
        arr = any(a[:1] in "-+" and set(a[1:]) & set("aAn") for a in args)
        for a in args:
            if ASSIGN_RE.match(a):
                k, v = a.split("=", 1)
                _set_var(table, k.rstrip("+"), [None] if arr or k.endswith("+") else _expand(v, mode, table, True))
    elif name in VAR_READ:
        extra = {"read": ["REPLY"], "mapfile": ["MAPFILE"], "readarray": ["MAPFILE"]}.get(name, [])
        for a in args + extra:
            k = a.split("=", 1)[0]
            if SH_NAME.match(k):
                _set_var(table, k, [None])
    elif name in ("for", "select") and args and SH_NAME.match(args[0]):
        vals = []
        for v in (args[2:] if args[1:2] == ["in"] else ["\x00"]):
            if "\x00" in v or re.search(r"[*?\[]", v):
                vals.append(None)
            elif _dyn_word(v, mode):
                vals += [t for e in _expand(v, mode, table) for t in ([None] if e is None else e.split())]
            else:
                vals.append(v)
        _set_var(table, args[0], vals or [None])


def _dyn_head(words, mode):
    """(index or None, words from the command word on) when the command word, past assignments, prefix words and
    env/sudo-style wrappers, is a variable or $(...) expansion; else None."""
    rest = unwrap(list(words), {}, mode)
    if not rest or not _dyn_word(rest[0], mode):
        return None
    k = len(words) - len(rest)
    return (k if k >= 0 and words[k:] == rest else None), rest


def _spell(words, head):
    shown = " ".join(words).replace("\x00", "$(...)")
    return SPELL % (shown if len(shown) <= 60 else shown[:57] + "...", head.replace("\x00", "$(...)"))


def _token_host(t):
    """The view's host record one word names: name, alias, IP, context, user@host, host:path, ssh:// URL, or an
    option's value (-Hssh://x, --context=x, -oHostName=x); else None."""
    vals = {t, t.split("=", 1)[-1]}
    if t[:1] == "-" and t[1:2] != "-":
        vals.add(t[2:])
    for v in vals:
        for hp in (_host_port(v) if v[:1] != "-" else None, _copy_host(v, True)):
            rec = CTX.view.resolve(hp[0], hp[1]) if hp and hp[0] else None
            if rec is not None:
                return rec
    return None


def _names_host(t):
    """One word names a helper or production host of the view (_token_host)."""
    return _token_host(t) is not None


TEXT_SPLIT = re.compile(r"[\s\"'`,;()\[\]{}<>|&]+")
PROT_TEXT = re.compile(r"allow_local|\.helper-orch|orch_home|guard-view|(?<![\w.-])guard\.json")
TEXT_MAX = 1 << 18           # characters of one code text or MCP input that are read for host and path names


def _spellings(p):
    """A normalised path as code may spell it: as is, and a drive path's Git Bash / WSL / Cygwin forms."""
    out = set(_alts(p))
    if DRIVE_RE.match(p or ""):
        out |= {pre + p[0] + p[2:] for pre in ("/", "/mnt/", "/cygdrive/")}
    return out


def _text_names(text, local=True):
    """What text the guard cannot parse (inline code, an MCP input) names that matters, else None: 'helper X' /
    'production X' for a host of the view (name, alias, hostname, IP, context, user@host, URL), 'a protected path
    (...)' for ALLOW_LOCAL, guard.json, ORCH_HOME (its folder, its setup files, the variable) and, for text run on
    the master, an approved project's store (absolute, or relative inside that project)."""
    text = text[:TEXT_MAX]
    view = CTX.view
    if getattr(view, "key_re", None) is None:   # one pass finds the words that can name a host at all
        keys = sorted((k for k in view.keys if k), key=len, reverse=True)
        view.key_re = re.compile("|".join(re.escape(k) for k in keys)) if keys else False
    if view.key_re and view.key_re.search(text.lower()):
        for t in dict.fromkeys(TEXT_SPLIT.split(text)):
            rec = _token_host(t) if t and view.key_re.search(t.lower()) else None
            if rec is not None:
                return ("production %s" if rec["kind"] == "prod" else "helper %s") % rec["name"]
    low = text.lower().replace("\\", "/")
    m = PROT_TEXT.search(low)
    if m:
        return "a protected path (%s)" % m.group(0)
    if not local:
        return None
    flat = re.sub(r"/+", "/", low)
    _, _, holders = _setup()
    cands = [a for h in holders for a in _spellings(h)]
    cands += [p["store"][len(p["root"]) + 1:] for p in CTX.view.projects
              if p["store"] and p["store"] != p["root"] and _under(p["store"], p["root"]) and _under(_base(), p["root"])]
    for a in cands:
        if a and re.search(r"(?<![\w.-])" + re.escape(a) + r"(?![\w-])", flat):
            return "a protected path (%s)" % a
    return None


def _job_words():
    """The tool names the approved projects' jobs use (job index keys, launcher dropped)."""
    out = set()
    for p in CTX.view.projects:
        for k in p["jobs"]:
            for f in _canon(k.split(), key=True):
                if f and f[0] != "run" and not PY_RE.match(f[0]) and f[0] not in NPX | PM | RUNNERS | DOCKERS:
                    out.add(f[0])
    return out


def _unresolved(rest, remote):
    """Rule v for a command word the guard cannot resolve: ask when the other words (split on spaces, so `-c '...'`
    text counts) look like an orch approval, name a helper or production host, prune/--rmi/wsl --shutdown, or name
    heavy work (decided with rule d's scope; on a helper always); otherwise nothing, as before. On a helper or
    production host (ssh named it) it always asks: C=$(cat f); ssh ha "$C" or ssh ha '$(cat f)' (MF2)."""
    toks = [t for a in rest[1:] for t in a.lower().split()]
    reason = _spell(rest, rest[0])
    if _kind(remote) in ("helper", "prod"):
        ask("v", reason)
        return
    if not toks:
        return
    names = {cmdname(t) for t in toks}
    flags = {t.split("=", 1)[0] for t in toks}
    approve = any(f.startswith("--appr") and "--approve".startswith(f) for f in flags) and \
        bool(names & {"init", "orch_init", "orch_init.py"})
    if approve or names & DYN_ANY or any(DAEMON_JSON_RE.search(t.replace("\\", "/")) for t in toks) or \
            any(_names_host(t) for t in toks):
        ask("v", reason)
    elif names & (DYN_WORDS | _job_words()) or any(_script_heavy(n) for n in names):
        if remote is None:
            if not flag_exists():
                CTX.dyn.append((reason, _hints(rest[1:])))
        elif remote.kind == "helper":
            ask("v", reason)


# commands whose host/path argument words (and redirect targets) are substituted from tracked same-script
# variables before analysis, so `H=hbx; ssh $H docker rm` is seen as `ssh hbx docker rm` (rule v, R-open-item)
# ssh is handled in check_ssh (its host must be resolved apart from the remote command, which has its own rule v)
SUBST_CMDS = {"scp", "rsync", "sftp", "cp", "mv", "rm", "tee"} | DOCKERS
UNRES = "__unres__"          # placeholder for an unresolvable arg: not a dyn word, names no host/path


def _subst_targets(c, name, words, args, mode, remote, depth, stdin_text):
    """Substitute every tracked same-script variable in a target command's argument words, then re-check each
    resolved variant by every rule (rule v extended to host/path arguments). An unresolvable host position of
    ssh/scp/rsync/sftp toward a mutating command asks the user to spell the host out. Returns True when it
    handled the command (the caller then returns)."""
    choices, unresolved = [], False
    for a in args:
        if _word_has_var(a, mode):
            exps = _expand(a, mode, CTX.vars)
            res = [e for e in exps if e is not None]
            if None in exps:
                unresolved = True
            choices.append(res or [UNRES])
        else:
            choices.append([a])
    variants, capped = [[]], False
    for ch in choices:
        nxt = [v + [x] for v in variants for x in ch]
        if len(nxt) > MAX_VALS:
            variants, capped, unresolved = nxt[:MAX_VALS], True, True
            break
        variants = nxt
    if CTX.budget <= 0:
        return False
    CTX.budget -= len(variants)
    for v in variants:
        check_cmd(c, [words[0]] + v, mode, remote, depth + 1, stdin_text)
    if unresolved and remote is None and name in ("ssh", "scp", "rsync", "sftp"):
        _unresolved_host(name, args)
    return True


def _unresolved_host(name, args):
    """Ask when a host that ssh/scp/rsync/sftp will act on comes from a variable the guard cannot resolve and the
    command would change something (an upload, or a destructive remote command): spell the host out."""
    if name in ("scp", "rsync", "sftp"):
        mut = True                    # a copy/session to an unknown host could be an upload to a helper/prod
    else:
        toks = [t for a in args for t in a.lower().split()]
        names = {cmdname(t) for t in toks}
        mut = bool(names & (REMOTE_BAD | DELETERS |
                            {"sudo", "chmod", "chown", "chgrp", "dd", "truncate", "shred", "mkfs", "tee"})) or \
            "prune" in toks or any("--rmi" in t for t in toks) or \
            bool(names & DOCKERS and names & {"rm", "rmi", "stop", "kill", "restart", "down", "prune", "up"})
    if mut:
        shown = (name + " " + " ".join(args)).replace("\x00", "$(...)")
        ask("v", "'%s' needs your OK (its host comes from a variable: spell the host out)"
            % (shown if len(shown) <= 60 else shown[:57] + "..."))


def analyze(script, mode, remote, depth):
    if depth > MAX_DEPTH or not script or not script.strip():
        return
    cmds, subs = lex(script, mode)
    low = _scan_text(script, cmds, mode)
    for m in RAW_DOCKER_HOST.finditer(low):
        _raw_target(m, low, "DOCKER_HOST")
    for m in RAW_DOCKER_CTX.finditer(low):
        _raw_target(m, low, "DOCKER_CONTEXT")
    m = RAW_USERONLY.search(low)
    if m:
        blk("e", "setting %s" % _uo_name(m.group(0)), USER)
    for sub, smode in subs:
        analyze(sub, smode, remote, depth + 1)
    for idx, c in enumerate(cmds):
        stdin_text = None
        if c.heredocs:
            stdin_text = "\n".join(c.heredocs)
        elif c.fed and idx > 0:
            prev = cmds[idx - 1]
            if prev.heredocs:
                stdin_text = "\n".join(prev.heredocs)
            elif prev.words and cmdname(prev.words[0]) in ("echo", "printf", "write-output"):
                stdin_text = " ".join(w for w in prev.words[1:] if not w.startswith("-"))
        check_cmd(c, list(c.words), mode, remote, depth, stdin_text)
        CTX.cmd_hint = None           # the env -C hint belongs to that one command only
        _note_vars(c.words, mode, CTX.vars)
    if mode == "ps" and _ps_dynamic(script) and DYN_HEAVY.search(script):
        if _kind(remote) == "prod":
            ask_prod("a dynamic & / . call", remote)
        elif _kind(remote) != "other":
            heavy("dynamic & / . call with build tokens", [], remote)


PY_VALOPT = {"--check-hash-based-pycs"}   # a long interpreter option that consumes the next word before the script


def _py_target(args):
    """Parse a python interpreter's leading options the way CPython does and return (kind, name, rest):
    'm' a module run (name = the module, `-m runpy <mod>` unwrapped to <mod>), 'script' a path (name = basename),
    'c' inline code (name = the code), or (None, None, None) if no target is reached. A short-option cluster is
    walked char by char: 'm' takes the rest of the cluster or the next word (so -morch, -Bmorch, -Im orch match),
    'c' stops, 'W'/'X' take a value (rest of cluster or next word); --check-hash-based-pycs takes the next word.
    This is why `python -morch init --approve` and `python -X utf8 -m orch ...` reach rule i (R-Q22)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-" or not a.startswith("-"):
            return "script", cmdname(a), args[i + 1:]
        if a == "--":
            return ("script", cmdname(args[i + 1]), args[i + 2:]) if i + 1 < len(args) else (None, None, None)
        if a in PY_VALOPT:
            i += 2
            continue
        if a.startswith("--"):            # any other long option takes no value before the script/module
            i += 1
            continue
        cl = a[1:]
        j, consumed_next = 0, False
        while j < len(cl):
            ch = cl[j]
            if ch == "m":
                mod = cl[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                rest = args[i + 1:] if cl[j + 1:] else args[i + 2:]
                if cmdname(mod) == "runpy":
                    return "m", cmdname(rest[0] if rest else ""), rest[1:]
                return "m", cmdname(mod), rest
            if ch == "c":
                return "c", (cl[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")), []
            if ch in ("W", "X"):          # option with a value: rest of the cluster, else the next word
                if not cl[j + 1:]:
                    consumed_next = True
                break
            j += 1
        i += 2 if consumed_next else 1
    return None, None, None


def _orch_entry(name, args):
    """(is_orch, args_after_the_entry, is_init_module) if these words invoke orch: directly (orch/orch.py), via
    orch_init.py, `python orch.py|orch_init.py`, `python -m orch|orch_init` (attached -m and -m runpy too). The
    init flag is True for the orch_init entry point, which approves with no `init` subcommand of its own (R39)."""
    if name in ("orch", "orch.py"):
        return True, args, False
    if name in ("orch_init", "orch_init.py"):
        return True, args, True
    if PY_RE.match(name):
        kind, nm, rest = _py_target(args)
        if kind == "m":
            if nm in ("orch", "orch_init"):
                return True, rest, nm == "orch_init"
            return False, None, False
        if kind == "script" and nm in ("orch", "orch.py", "orch_init", "orch_init.py"):
            return True, rest, nm in ("orch_init", "orch_init.py")
    return False, None, False


RUNPY_RE = re.compile(r"runpy\s*\.\s*run_(?:path|module)\s*\(")


def _py_c_orch(name, args):
    """`python -c "...runpy.run_path/run_module(... orch ...)"`: an inline runpy run of orch is an orch entry
    point too. Ask when the code also carries an approval token; otherwise swallow it (an orch run is allowed).
    Returns True when the code is such an orch runpy invocation (R-Q22)."""
    if not PY_RE.match(name):
        return False
    kind, code, _ = _py_target(args)
    if kind != "c" or not code:
        return False
    low = code.lower()
    if RUNPY_RE.search(low) and ("orch" in low) and ("orch_init" in low or ".py" in low or "run_path" in low):
        if "appr" in low or "approve" in low:
            ask("i", APPROVE_REASON)
        return True
    return False


CODE_OPT = {"node": re.compile(r"^(?:-e|-p|-pe|-ep|--eval|--print)$"), "ruby": re.compile(r"^-[a-zA-Z]*e$"),
            "perl": re.compile(r"^-[a-zA-Z]*[eE]$"), "php": re.compile(r"^-[rRBE]$"), "osascript": re.compile(r"^-e$")}
CODE_OPT["nodejs"] = CODE_OPT["bun"] = CODE_OPT["node"]
# options that take the next word as their value, so that word is not taken for the script
CODE_VAL = {"node": {"-r", "--require", "--import", "-C", "--conditions", "--loader", "--experimental-loader",
                     "--title", "--env-file"},
            "bun": {"-r", "--preload", "--cwd", "-c", "--config", "--env-file"},
            "ruby": {"-I", "-r", "-E", "-C", "-F"}, "perl": {"-I", "-M", "-m"}, "php": {"-c", "-d", "-z", "-t"},
            "osascript": {"-l", "-s"}}
CODE_VAL["nodejs"] = CODE_VAL["node"]


def _inline_code(name, args, stdin_text):
    """The code an interpreter runs from its command line or stdin, which the guard cannot read: python -c, python -
    or a bare python fed by a heredoc/echo, node/bun -e/-p/--eval, deno eval, ruby -e, perl -e/-E (also -ne/-pe),
    php -r, osascript -e, and the stdin of those with no script argument. None otherwise. PowerShell -c/-Command
    text is read in its own branch (check_cmd)."""
    if PY_RE.match(name):
        kind, code, _ = _py_target(args)
        if kind == "c":
            return code
        return stdin_text if kind is None or (kind == "script" and code == "-") else None
    if name == "deno":
        la = [a.lower() for a in args]
        if first_nonopt(la) != "eval":
            return None
        return " ".join(a for a in args[la.index("eval") + 1:] if not a.startswith("-")) or None
    rx = CODE_OPT.get(name)
    if rx is None:
        return None
    code, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a == "-" or not a.startswith("-"):
            break                         # the script (or - for stdin): the words after it are its own arguments
        if rx.match(a):
            code += args[i + 1:i + 2]
            i += 2
            continue
        if rx is CODE_OPT["node"] and a.lower().startswith(("--eval=", "--print=")):
            code.append(a.split("=", 1)[1])
        i += 2 if a in CODE_VAL.get(name, ()) else 1
    if code:
        return "\n".join(code)
    return stdin_text if args[i:i + 1] in ([], ["-"]) else None


def _unreadable_code(name, code, remote):
    """MF4: code the guard cannot read that names a helper or production host of the view, or a protected path,
    asks; the user decides once they have read it (a deny by another rule still wins)."""
    what = _text_names(code, remote is None)
    if what:
        ask("x", "inline %s code naming %s needs your OK (the guard cannot read this code; spell the command out)"
            % (name, what))


def _dash_c_value(args, largs):
    """The shell-command string a CMD_STR_WRAPPERS wrapper runs via -c/--command: inline (-c=cmd / --command=cmd /
    -ccmd), the next word, or the value of a combined short cluster ending in c (su/script -qc cmd). None if
    there is no -c (a positional command, left to the unknown-wrapper catch-all)."""
    i = 0
    while i < len(args):
        al = largs[i]
        if al in ("-c", "--command"):
            return args[i + 1] if i + 1 < len(args) else ""
        if al.startswith("--command="):
            return args[i].split("=", 1)[1]
        if al.startswith("-c") and not al.startswith("--") and len(al) > 2:
            return args[i].split("=", 1)[1] if "=" in al else args[i][2:]
        if al.startswith("-") and not al.startswith("--") and len(al) > 1 and al.endswith("c") and \
                all(ch.isalpha() for ch in al[1:]):
            return args[i + 1] if i + 1 < len(args) else ""
        i += 1
    return None


def _wrapper_inner(args):
    """Index of the first word that heads a command the guard must re-check, when a wrapper is in front of it.
    Options are skipped; `--` forces the next word to be the command. None when nothing command-like is found (a
    bare `docker`/`ssh` with no target then yields no decision, so a false match stays harmless)."""
    if "--" in args:
        k = args.index("--") + 1
        return k if k < len(args) else None
    for k, a in enumerate(args):
        if a.startswith("-") and a != "-":
            continue
        nm = known_name(a)
        if nm in WRAP_HEADS or PY_RE.match(nm) or _orch_entry(nm, args[k + 1:])[0]:
            return k
    return None


def _unknown_wrapper(args, remote):
    """An unknown program (not a listed wrapper) in front of a command: do not hard-deny (it may not be a
    wrapper at all), but do not let it silently cloak an orch approval, or ssh/docker/scp to a known host, or a
    prune/daemon.json write either. Ask so the agent spells the command out (R-Q23)."""
    k = _wrapper_inner(args)
    if k is None:
        return
    inner = args[k:]
    toks = [t for a in inner for t in a.lower().split()]
    names = {cmdname(t) for t in toks}
    flags = {t.split("=", 1)[0] for t in toks}
    approve = any(f.startswith("--appr") and "--approve".startswith(f) for f in flags) and \
        bool(names & {"init", "orch_init", "orch_init.py"})
    if approve or (names & DYN_ANY) or any(_names_host(t) for t in inner) or \
            any(DAEMON_JSON_RE.search(t.replace("\\", "/")) for t in toks):
        shown = " ".join(args).replace("\x00", "$(...)")
        ask("v", "'%s' needs your OK (unrecognised wrapper: spell the command out)"
            % (shown if len(shown) <= 60 else shown[:57] + "..."))


def _orch_call(name, args, largs):
    """Any orch invocation passes unanalysed; an approval (`orch init --approve`, `orch_init.py --approve`,
    `python -m orch init --approve`, argparse prefixes too) asks the user (R39)."""
    ok, rest, init_mod = _orch_entry(name, args)
    if not ok:
        return _py_c_orch(name, args)      # python -c "...runpy.run_module(...orch...)" (R-Q22)
    lrest = [a.lower() for a in rest]
    flags = [a.split("=", 1)[0] for a in lrest]
    approve = any(f.startswith("--appr") and "--approve".startswith(f) for f in flags)
    if approve and (init_mod or first_nonopt(lrest) == "init"):
        ask("i", APPROVE_REASON)
    return True


def check_cmd(c, words, mode, remote, depth, stdin_text):
    psa = _ps_assign(words) if mode == "ps" else None
    dyn = _dyn_head(words, mode) if psa is None else None
    if dyn is not None:               # rule v: the command name is a $NAME / ${NAME} / $(...) expansion
        k, rest = dyn
        exps = _expand(rest[0], mode, CTX.vars) if depth <= MAX_DEPTH else [None]
        if len(exps) > CTX.budget:
            exps = [None]
        CTX.budget -= len(exps)
        for e in exps:
            if e is not None:         # every value the name was given, checked by every rule
                check_cmd(c, (words[:k] if k is not None else []) + e.split() + rest[1:], mode, remote, depth + 1,
                          stdin_text)
        if None not in exps:
            return
        _unresolved(rest, remote)     # cannot be told: ask when the rest looks relevant, then as before
    check_protected(c, words, remote)
    for op, t in c.redirs:
        if ">" in op and DAEMON_JSON_RE.search(t.lower().replace("\\", "/")):
            blk("c", "writing daemon.json (rule 4)", ORCH + " nodes")
    rk = _kind(remote)
    if rk == "prod":
        prod_redirs(c, remote)
    if psa is not None:               # PowerShell $x = <command args>: the command runs, its output is the value
        run = _ps_rhs(psa[2], CTX.vars)[1]
        if run:
            check_cmd(c, run, mode, remote, depth + 1, stdin_text)
        return
    env = {}
    words = unwrap(words, env)
    for k in env:                     # a user-only var set as a prefix/env assignment, any operator (R-Q21)
        if k[:1] != "\x00" and k.rstrip("+") in USERONLY_VARS:
            blk("e", "setting %s" % k.rstrip("+").upper(), USER)
    if not words:
        return
    CTX.cmd_hint = env.get(ENV_CWD)   # env -C / sudo -D working dir for this one command (rule d hint)
    name = known_name(words[0])
    args = words[1:]
    largs = [a.lower() for a in args]

    code = _inline_code(name, args, stdin_text)
    if code:                          # python -c, node -e, ...: the guard cannot read what the code does (MF4)
        _unreadable_code(name, code, remote)
    if _orch_call(name, args, largs):
        return
    if rk is None and name in CD_CMDS:
        _note_cd(args)
        return

    if name in CMD_STR_WRAPPERS:            # su/runuser/sg/flock/script -c '<cmd>' (R-Q23)
        inner = _dash_c_value(args, largs)
        if inner is not None:
            analyze(inner, "sh", remote, depth + 1)
            return
    if name in WRAPPERS or name in CMD_STR_WRAPPERS:   # a listed pass-through wrapper: unwrap and recheck fully
        k = _wrapper_inner(args)                       # its inner command is checked by every rule (R-Q23)
        if k is not None:
            inner_cmd = Cmd(c.fed)
            inner_cmd.stdin_redir, inner_cmd.heredocs, inner_cmd.heredoc_spans, inner_cmd.end = \
                c.stdin_redir, c.heredocs, c.heredoc_spans, c.end
            check_cmd(inner_cmd, args[k:], mode, remote, depth + 1, stdin_text)
            return
    elif name not in KNOWN and not _dyn_word(words[0], mode):   # unknown program cloaking a sensitive command
        _unknown_wrapper(args, remote)

    if name in SUBST_CMDS and depth <= MAX_DEPTH and any(_word_has_var(a, mode) for a in args):
        if _subst_targets(c, name, words, args, mode, remote, depth, stdin_text):   # scp $P:/x, docker -H $X ...
            return

    if env.get(XARGS_KEY) and name in XARGS_HEAVY and not LIGHT & set(largs) and not first_nonopt(largs):
        if rk == "prod":
            ask_prod("'xargs %s' (subcommand from stdin)" % name, remote)
        elif rk != "other":
            heavy("xargs %s (subcommand from stdin)" % name, [name], remote)

    if name == "find":
        i = 0
        while i < len(largs):
            if largs[i] in ("-exec", "-execdir", "-ok", "-okdir"):
                j = i + 1
                while j < len(args) and args[j] not in (";", "+"):
                    j += 1
                check_cmd(Cmd(False), args[i + 1:j], mode, remote, depth + 1, None)
                i = j
            i += 1
        if rk == "prod":
            prod_cmd(c, name, args, remote, stdin_text)
        return

    if name in ("start-process", "saps"):
        fp, al, wd, i = None, [], None, 0
        while i < len(args):
            a, o = args[i], largs[i]
            if o.startswith("-") and len(o) > 1:
                o = o[1:].split(":")[0]
                v = args[i + 1] if i + 1 < len(args) else ""
                if o in ("path", "pspath") or "filepath".startswith(o):
                    fp = v
                elif o == "args" or "argumentlist".startswith(o):
                    al.append(v)
                elif o == "wd" or (len(o) >= 2 and "workingdirectory".startswith(o)):
                    wd = v                       # -WorkingDirectory sets the working dir (rule d hint, R29)
                elif o in START_BOOL or (len(o) >= 3 and any(b.startswith(o) for b in START_BOOL)):
                    i += 1
                    continue
                i += 2
                continue
            if fp is None:
                fp = a
            else:
                al.append(a)
            i += 1
        if fp:
            if wd:
                CTX.dir_hints.append(_norm_path(wd, _base()))
            try:
                check_cmd(Cmd(False), [fp] + " ".join(al).replace(",", " ").split(), "ps", remote, depth + 1, None)
            finally:
                if wd:
                    CTX.dir_hints.pop()
        return

    if name in SHELLS:
        if name == "busybox" and largs[:1] in (["sh"], ["ash"]):
            args, largs = args[1:], largs[1:]
        i, reads_stdin = 0, False
        while i < len(args):
            a = args[i]
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                analyze(args[i + 1] if i + 1 < len(args) else "", "sh", remote, depth + 1)
                return
            if a in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                i += 2
                continue
            if a.startswith("-") or a.startswith("+"):
                reads_stdin = reads_stdin or ("s" in a[1:] and not a.startswith("--"))
                i += 1
                continue
            if reads_stdin:
                break
            check_cmd(Cmd(False), args[i:], mode, remote, depth + 1, None)  # script file + args
            return
        if stdin_text:
            analyze(stdin_text, "sh", remote, depth + 1)
        elif rk == "helper" and (c.fed or c.stdin_redir or CTX.session):   # an MCP terminal can feed a session
            blk("b", "%s on helper %s" % ("stdin-fed shell" if c.fed or c.stdin_redir else "shell session", remote),
                si(remote))
        elif rk == "prod":
            ask_prod(("stdin-fed '%s'" if c.fed or c.stdin_redir else
                      "'%s' session" + (UNCHECKED if CTX.session else "")) % name, remote)
        return

    if name in PWSH:
        pwd = _ps_val(args, largs, ("workingdirectory", "wd"))   # -WorkingDirectory (rule d hint, R29)
        if pwd:
            CTX.dir_hints.append(_norm_path(pwd, _base()))
        try:
            i = 0
            while i < len(args):
                a, al = args[i], largs[i]
                if al.startswith("-") or al.startswith("/"):
                    o = al.lstrip("-/")
                    text = None
                    if o in ("e", "ec", "en", "enc") or (len(o) >= 3 and "encodedcommand".startswith(o)):
                        text = _b64(args[i + 1] if i + 1 < len(args) else "")
                    elif o == "c" or (len(o) >= 2 and "command".startswith(o)):
                        rest = " ".join(args[i + 1:])
                        text = (stdin_text or "") if rest.strip() == "-" else rest
                    if text is not None:
                        if text:              # .NET calls and expressions are opaque to the guard (MF4)
                            _unreadable_code(name, text, remote)
                        analyze(text, "ps", remote, depth + 1)
                        return
                    if o == "f" or (len(o) >= 2 and "file".startswith(o)):
                        check_cmd(Cmd(False), args[i + 1:], "ps", remote, depth + 1, None)
                        return
                    if o in ("executionpolicy", "ex", "ep", "windowstyle", "w", "workingdirectory", "wd",
                             "configurationname", "outputformat", "of", "inputformat", "if", "settingsfile",
                             "version", "v", "psconsolefile", "custompipename"):
                        i += 2
                        continue
                    i += 1
                    continue
                analyze(" ".join(args[i:]), "ps", remote, depth + 1)
                return
            if stdin_text:
                analyze(stdin_text, "ps", remote, depth + 1)
            elif rk == "helper" and CTX.session:   # an MCP terminal can feed a session
                blk("b", "shell session on helper %s" % remote, si(remote))
            elif rk == "prod":
                ask_prod("'%s' session" % name, remote)
        finally:
            if pwd:
                CTX.dir_hints.pop()
        return

    if name == "cmd":
        for i, al in enumerate(largs):
            off = 1 if al.startswith("//") else 0   # Git Bash/MSYS maps `//c` -> `/c` for the native cmd.exe (R21)
            o = al[off:]
            if o in ("/c", "/k", "/r"):
                analyze(" ".join(args[i + 1:]), "cmd", remote, depth + 1)
                return
            if o[:2] in ("/c", "/k") and len(o) > 2 and o[2] not in "abcdefghijklmnopqrstuvwxyz:":
                analyze(args[i][off + 2:] + " " + " ".join(args[i + 1:]), "cmd", remote, depth + 1)
                return
        if rk == "prod":
            ask_prod("'cmd' session", remote)
        return

    if name in ("eval", "iex", "invoke-expression"):
        analyze(" ".join(args), "sh" if name == "eval" else "ps", remote, depth + 1)
        return

    if name == "wsl":
        if rk == "helper":
            blk("b", "wsl on helper %s" % remote, ORCH + " nodes")
        for a in largs:
            if a in ("-e", "--exec", "--"):
                break
            if a in ("--shutdown", "--terminate", "-t"):
                blk("c", "wsl %s (stops Docker for everyone, rule 4)" % a, ORCH + " nodes")
        if rk == "prod":
            ask_prod("'wsl'", remote)
        i = 0
        while i < len(largs):
            a = largs[i]
            if a in ("-e", "--exec", "--"):
                check_cmd(Cmd(False), args[i + 1:], "sh", remote, depth + 1, None)
                return
            if a in ("-d", "--distribution", "-u", "--user", "--cd", "--distribution-id"):
                i += 2
                continue
            if a.startswith("-"):
                return
            analyze(" ".join(args[i:]), "sh", remote, depth + 1)
            return
        return

    if name == "ssh":
        check_ssh(c, args, remote, depth, stdin_text, mode)
        return

    if name in DOCKERS:
        check_docker(name, args, env, remote, depth)
        return

    if name == "git" and first_nonopt(largs, {"-c", "-C"}) == "push":
        for a in args:
            hp = _host_port(a) if "://" in a else _copy_host(a, False)
            r = _remote(*hp) if hp and hp[0] else None
            if r is not None and r.kind == "prod":
                ask_prod("'git push'", r)
    if rk != "helper":
        check_rule4(c, name, args, largs)  # on a helper, rule b below names the job
    if name in ("scp", "rsync", "sftp"):
        check_copy(name, args)
    if rk == "other":
        return
    if rk == "prod":
        prod_cmd(c, name, args, remote, stdin_text)
        return
    if rk == "helper" and name in REMOTE_BAD:
        if name == "init" and largs[:1] not in (["0"], ["6"], ["1"]):
            pass
        else:
            blk("b", "'%s' on helper %s (R1)" % (name, remote), si(remote))
    if rk == "helper" and name == "sc" and largs[:1] and largs[0] in ("stop", "delete", "config", "pause"):
        blk("b", "'sc %s' on helper %s (R1)" % (largs[0], remote), si(remote))

    check_heavy(name, args, largs, env, remote)

def unwrap(words, env, dyn=None):
    """Drop leading assignments (into env), prefix words and wrappers (env, sudo, nice, timeout, xargs, ...) to reach
    the real command. dyn = a lexer mode: stop at a command word that is a variable or $(...) expansion (rule v)."""
    while words:
        w0 = words[0]
        if ASSIGN_RE.match(w0):
            k, v = w0.split("=", 1)
            env[k.rstrip("+").lower()] = v
            words = words[1:]
            continue
        if dyn and _dyn_word(w0, dyn):
            break
        name = cmdname(w0)
        if name in PREFIX_WORDS or name == "":
            words = words[1:]
            continue
        if name == "env":
            words = words[1:]
            while words:
                a = words[0]
                if a in ("-C", "--chdir") and len(words) > 1:
                    env[ENV_CWD] = _norm_path(words[1], _base())   # env runs the command in this dir (R29)
                    words = words[2:]
                elif a.startswith("--chdir="):
                    env[ENV_CWD] = _norm_path(a.split("=", 1)[1], _base())
                    words = words[1:]
                elif a in ("-u", "--unset"):
                    words = words[2:]
                elif a in ("-S", "--split-string") and len(words) > 1:
                    words = words[1].split() + words[2:]
                elif a.startswith("-S") and len(a) > 2:
                    words = a[2:].split() + words[1:]
                elif a == "--":
                    words = words[1:]
                    break
                elif a.startswith("-") or ASSIGN_RE.match(a):
                    if ASSIGN_RE.match(a):
                        k, v = a.split("=", 1)
                        env[k.lower()] = v
                    words = words[1:]
                else:
                    break
            continue
        if name in WRAPPER_VALOPTS:
            if name == "xargs":
                env[XARGS_KEY] = "1"
            valopts = WRAPPER_VALOPTS[name]
            words = words[1:]
            while words and words[0].startswith("-") and len(words[0]) > 1:
                a = words[0]
                words = words[1:]
                if a == "--":
                    break
                if name == "sudo" and a in ("-D", "--chdir") and words:
                    env[ENV_CWD] = _norm_path(words[0], _base())   # sudo -D runs the command in this dir (R29)
                    words = words[1:]
                elif name == "sudo" and a.startswith("--chdir="):
                    env[ENV_CWD] = _norm_path(a.split("=", 1)[1], _base())
                elif a in valopts and words:
                    words = words[1:]
            words = words[WRAPPER_POSITIONAL.get(name, 0):]
            continue
        break
    return words


DAEMON_JSON_RE = re.compile(r"(^|[\\/:])daemon\.json$")
DOCKER_SVC_RE = re.compile(r"docker|containerd")
COPY_LAST = {"cp", "mv", "install", "ln", "copy", "move", "xcopy", "copy-item", "cpi", "move-item", "mi"}
WRITE_ANY = {"tee", "set-content", "add-content", "out-file", "sed", "dd"}


def check_rule4(c, name, args, largs):
    """Rule 4 on any host: docker service restarts and daemon.json writes (commands)."""
    paths = [a.replace("\\", "/") for a in largs if not a.startswith("-")]
    if name == "sed" and not any(a.startswith("-i") or a.startswith("--in-place") for a in largs):
        name = "sed (read)"
    if name in COPY_LAST and paths and DAEMON_JSON_RE.search(paths[-1]) or \
            name in WRITE_ANY and any(DAEMON_JSON_RE.search(p) for p in paths):
        blk("c", "'%s' writing daemon.json (rule 4)" % name, ORCH + " nodes")
    svc = None
    if name == "systemctl" and any(a in ("restart", "stop") for a in largs):
        svc = next((a for a in paths if DOCKER_SVC_RE.search(a)), None)
    elif name == "service" and len(paths) > 1 and paths[1] in ("restart", "stop"):
        svc = paths[0] if DOCKER_SVC_RE.search(paths[0]) else None
    elif name in ("restart-service", "stop-service", "spsv"):
        svc = next((a for a in largs if DOCKER_SVC_RE.search(a)), None)
    if svc:
        blk("c", "%s %s (rule 4)" % (name, svc), ORCH + " nodes")


def _b64(v):
    import base64
    try:
        raw = base64.b64decode(v + "=" * (-len(v) % 4))
        try:
            return raw.decode("utf-16-le")
        except Exception:
            return raw.decode("utf-8", "replace")
    except Exception:
        return ""


DOCKER_GLOBAL_VAL = {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level",
                     "--tlscacert", "--tlscert", "--tlskey"}
COMPOSE_VAL = {"-f", "--file", "-p", "--project-name", "--profile", "--env-file",
               "--project-directory", "--ansi", "--progress", "--parallel"}
RUN_LONG_BOOL = {"--rm", "--detach", "--interactive", "--tty", "--privileged", "--init",
                 "--read-only", "--publish-all", "--no-healthcheck", "--oom-kill-disable",
                 "--sig-proxy", "--quiet", "--disable-content-trust", "--help", "--use-api-socket"}


def run_image(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            i += 1 if ("=" in a or a.lower() in RUN_LONG_BOOL or a == "--") else 2
        elif a.startswith("-") and len(a) > 1:
            fl = a[1:]
            if all(ch in "ditPq" for ch in fl):
                i += 1
            else:
                i += 2 if len(fl) == 1 else 1
        else:
            return a.lower()
    return ""


def _publishes(args):
    """True when docker run/create options (before the image) publish host ports."""
    i = 0
    while i < len(args):
        a = args[i]
        al = a.lower()
        if a.startswith("--"):
            if al in ("--publish", "--publish-all") or al.startswith("--publish=") or \
                    al.startswith("--publish-all=") and not al.endswith("false"):
                return True
            i += 1 if ("=" in a or al in RUN_LONG_BOOL or a == "--") else 2
        elif a.startswith("-") and len(a) > 1:
            fl = a[1:]
            for ch in fl:
                if ch == "P" or ch == "p":
                    return True
                if ch not in "ditq":
                    break
            if all(ch in "ditPq" for ch in fl):
                i += 1
            else:
                i += 2 if len(fl) == 1 else 1
        else:
            return False
    return False


# ---------------------------------------------------------------- ssh and file copies
SSH_VAL = set("bcDEeFIiJLlmOopQRSWwB")
SCP_VAL = set("cFiJloPSXD")
SFTP_VAL = set("BbcDFiJloPRSs")


def _ssh_optval(f, v, st):
    if f == "J":
        st["jumps"] += v.split(",")
    elif f == "p" and v.strip().isdigit():
        st["port"] = int(v.strip())
    elif f == "o":
        m = re.match(r"\s*(\w+)\s*(?:=|\s)\s*(.*)$", v)
        key = m.group(1).lower() if m else ""
        val = (m.group(2) if m else "").strip().strip("'\"")
        if key == "hostname":
            st["hostname"] = val
        elif key == "proxyjump":
            st["jumps"] += val.split(",")
        elif key == "port" and val.strip().isdigit():
            st["port"] = int(val.strip())
        elif key == "remotecommand":       # runs on the remote host, like a trailing command (R25)
            st["rcmd"] = val
        elif key in ("localcommand", "proxycommand", "knownhostscommand"):
            st["localcmds"].append(val)     # runs on the machine that runs ssh (the master)


def _ssh_opts(args, i, st, first):
    """Consume ssh option words from args[i]. first pass: stop at the destination and return it. Second pass
    (OpenSSH getopt-permutes options placed after the host): consume option words only, stop at the first
    non-option word. Returns (i, dest, dashdash)."""
    while i < len(args):
        a = args[i]
        if a == "--":
            if not first:
                return i, None, False
            i += 1
            dest = args[i] if i < len(args) else None
            return (i + 1 if dest is not None else i), dest, True
        if a.startswith("-") and len(a) > 1:
            flags, i = a[1:], i + 1
            for k, f in enumerate(flags):
                st["seen"] += f
                if f in SSH_VAL:
                    if k == len(flags) - 1:
                        v = args[i] if i < len(args) else ""
                        i += 1
                    else:
                        v = flags[k + 1:]
                    _ssh_optval(f, v, st)
                    break
            continue
        if not first:
            return i, None, False
        return i + 1, a, False
    return i, None, False


UNCHECKED = " (what an MCP terminal types into it later is not checked)"
LOCAL_SUB = ("ssh to production %s with a substitution the master's shell runs first ($(...), backticks, a "
             "PowerShell (...) or {...}) needs your OK (what would run there is unknown: spell the remote command out)")


def _local_sub(c, rest, rcmd, fed, shell, stdin_text, mode):
    """MF2: what ssh will run remotely holds a substitution the master's shell expands first, so the remote command
    is unknown until it runs: a NUL marker ($(...) / backticks outside single quotes, PowerShell $(...), an
    unquoted-delimiter heredoc's $(...)) in the remote command words, in -o RemoteCommand or in the stdin script
    a remote shell reads, or (PowerShell) ssh's arguments running on into a local (...) / {...}."""
    if any("\x00" in w for w in rest) or (not rest and rcmd and "\x00" in rcmd):
        return True
    if fed and (shell or not rest) and stdin_text and "\x00" in stdin_text:
        return True
    return mode == "ps" and c.end in ("(", "{")


def check_ssh(c, args, remote, depth, stdin_text, mode="sh"):
    st = {"seen": "", "hostname": None, "jumps": [], "port": None, "rcmd": None, "localcmds": []}
    i, dest, dashdash = _ssh_opts(args, 0, st, True)
    if dest is not None and not dashdash:
        i, _, _ = _ssh_opts(args, i, st, False)   # options placed after the destination (R20)
    hostname, jumps, port, seen = st["hostname"], st["jumps"], st["port"], st["seen"]
    if dest is None or "G" in seen or "V" in seen:
        return  # ssh -G / -V: nothing runs remotely
    host_unres = [False]

    def _hosts(t):                           # resolve a host position held in a variable (rule v, R-open-item)
        if t and _word_has_var(t, "sh"):
            exps = _expand(t, "sh", CTX.vars)
            if None in exps:
                host_unres[0] = True
            return [e for e in exps if e is not None]
        return [t] if t else []
    cands = [(h, port) for h in _hosts(hostname)] + [(h, port) for h in _hosts(dest)] + \
            [(h, None) for j in jumps for h in _hosts(j)]
    node = None
    for t, p in cands:
        h, hp = _host_port(t)
        r = _remote(h, p or hp)
        if r.kind != "other":
            node = r
            break
    if node is None:
        base = next((e for t in (hostname, dest) for e in _hosts(t)), hostname or dest or "")
        node = _remote(_host_port(base)[0])  # not in the view (or a variable host): only rules c and 4 apply
    for lc in st["localcmds"]:               # LocalCommand/ProxyCommand/KnownHostsCommand run on the master (R25)
        if lc:
            analyze(lc, "sh", remote, depth + 1)
    rest = args[i:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if host_unres[0] and node.kind == "other":   # host from an unresolvable variable: ask when it mutates
        _unresolved_host("ssh", rest or args)
    fed = c.fed or c.stdin_redir
    words = " ".join(rest).split()            # as the remote shell splits it: ssh h 'bash -s' runs bash
    inner = unwrap(list(words), {})
    heads, unknown, dynw = inner[:1], False, unwrap(list(words), {}, "sh")
    if dynw and _dyn_word(dynw[0], "sh"):      # remote command name held in a variable (rule v)
        for e in _expand(dynw[0], "sh", CTX.vars):
            if e is None:
                unknown = True
            elif e.split():
                heads.append(e.split()[0])
    shell = next((h for h in heads if known_name(h) in STDIN_RUNNERS), None)
    if node.kind != "other" and _local_sub(c, rest, st["rcmd"], fed, shell, stdin_text, mode):
        if node.kind == "helper":
            blk("b", "local $(...) in ssh to helper %s" % node, si(node))
        ask("p", LOCAL_SUB % node.label)     # then the visible part still meets rule p (a deny wins)
    if not rest and st["rcmd"]:              # -o RemoteCommand with no trailing command runs on the remote (R25)
        analyze(st["rcmd"], "sh", node, depth + 1)
        return
    if fed and node.kind != "other" and (shell or unknown):
        if stdin_text:
            analyze(stdin_text, "sh", node, depth + 1)
        elif shell is None:
            ask("v", _spell(["ssh", str(node)] + rest, dynw[0]))
        elif node.kind == "helper":
            blk("b", "stdin script to '%s' on helper %s" % (cmdname(shell), node), si(node))
        else:
            ask_prod("a stdin script to '%s'" % cmdname(shell), node)
    if rest:
        analyze(" ".join(rest), "sh", node, depth + 1)
        return
    if stdin_text:
        analyze(stdin_text, "sh", node, depth + 1)
    elif fed and node.kind == "helper":
        blk("b", "stdin script to ssh %s" % node, si(node))
    elif node.kind == "helper" and CTX.session and not set("NW") & set(seen):   # an MCP terminal can type into it
        blk("b", "interactive ssh session to helper %s" % node, si(node))
    elif node.kind == "prod" and "N" not in seen:
        ask_prod("a stdin script" if fed else "an interactive ssh session" + (UNCHECKED if CTX.session else ""), node)


def _copy_host(a, bare):
    """(host, port) named by a scp/rsync/sftp operand ([user@]host:path, host::module, scp://...), else None."""
    al = a.lower()
    if al.startswith("-") or re.match(r"^[a-z]:[\\/]", al):
        return None
    m = re.match(r"^(?:scp|sftp|rsync)://([^/]+)", al)
    if m:
        return _host_port(m.group(1))
    if al.startswith("[") and "]" in al:
        return al[1:al.index("]")], None
    if ":" in al:
        return _host_port(al.split(":", 1)[0])
    return _host_port(al) if bare else None


def check_copy(name, args):
    """Rule b (any copy touching a helper) and rule p (an upload or session to production asks)."""
    vals = SCP_VAL if name == "scp" else (SFTP_VAL if name == "sftp" else set())
    ops, port, i = [], None, 0
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith("-") and len(a) > 1:
            for k, f in enumerate(a[1:]):
                if f in vals:
                    v = a[k + 2:]
                    if not v and i < len(args):
                        v, i = args[i], i + 1
                    if f == "P" and v.isdigit():
                        port = int(v)
                    break
            continue
        ops.append(a)
    prod = []
    for k, a in enumerate(ops):
        hp = _copy_host(a, name == "sftp")
        if not hp or not hp[0]:
            continue
        r = _remote(hp[0], port or hp[1])
        if r.kind == "helper":
            blk("b", "%s to helper %s (R2)" % (name, r), "%s run <job> --node %s" % (ORCH, r))
        if r.kind == "prod":
            prod.append((k, r))
    if prod:
        if name == "scp" and all(k < len(ops) - 1 for k, _ in prod) and not _copy_host(ops[-1], False):
            return  # download from production: read-only
        dest = [r for k, r in prod if k == len(ops) - 1]
        ask_prod("'%s' %s" % (name, "session" if name == "sftp" else "upload"), (dest or [prod[0][1]])[0])


# ---------------------------------------------------------------- docker
CONTEXT_PREFIX = "orch-"     # orch init's docker context names: orch-<node>[-<8 hex>] (scripts/orch_discover.py)
CTX_CHANGE = {"use", "update", "rm", "remove", "create", "import"}
CTX_VAL = {"--docker", "--description", "--from", "--kubernetes", "--default-stack-orchestrator"}


def _ctx_names(args):
    """The context names of `docker context <action> ...`: its words that are neither options nor option values."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and a != "-":
            i += 2 if a.lower() in CTX_VAL and "=" not in a else 1
            continue
        out.append(a)
        i += 1
    return out


def check_docker(name, args, env, remote, depth=0):
    if _kind(remote) == "helper":
        blk("b", "docker over ssh to helper %s" % remote, si(remote))
    target, i = None, 0
    if name != "docker-compose":
        while i < len(args):
            a = args[i]
            al = a.lower()
            if not a.startswith("-") or a == "-":
                break
            if a in DOCKER_GLOBAL_VAL or al in ("--host", "--context", "--config", "--log-level"):
                v = args[i + 1] if i + 1 < len(args) else ""
                if a in ("-H", "-c") or al in ("--host", "--context"):
                    target = target or v
                i += 2
                continue
            if al.startswith("--host=") or al.startswith("--context="):
                target = target or a.split("=", 1)[1]
            elif a.startswith("-H") and len(a) > 2:
                target = target or a[2:]
            i += 1
        sub = args[i].lower() if i < len(args) else ""
        rest = args[i + 1:]
    else:
        sub, rest = "compose", args
    for k in ("docker_host", "docker_context"):
        if k in env and not target:
            target = env[k]
    where = remote
    if target:
        th, tp = _host_port(target)
        r = _remote(th, tp) if th else None
        if r is not None and r.kind == "helper":
            blk("a", "raw docker to helper %s" % r, si(r))
        if r is not None and (r.kind == "prod" or re.match(r"^(ssh|tcp|https?)://", target.lower())):
            where = r  # unknown remote engines: only rules c and 4
    lr = [r.lower() for r in rest]
    j = " ".join(lr)
    if sub == "context":
        act, names = (lr[0] if lr else ""), _ctx_names(rest[1:])
        if act in CTX_CHANGE:                            # orch creates, updates and removes its own contexts
            for n in names if act in ("rm", "remove") else names[:1]:
                if n.lower().startswith(CONTEXT_PREFIX):
                    blk("a", "docker context %s %s (orch manages its %s* contexts)" % (act, n, CONTEXT_PREFIX),
                        ORCH + " nodes" if act == "use" else INIT)
        if lr[:1] == ["use"] and len(lr) > 1:
            r = _remote(lr[1])
            if r.kind == "helper":
                blk("a", "docker context use %s" % lr[1], ORCH + " nodes")
            if r.kind == "prod":
                CTX.docker_default = CTX.docker_default or r
                ask("p", "docker context use %s needs your OK (later docker commands would reach production %s "
                         "unseen)" % (lr[1], r.label))
        if lr[:1] and lr[0] in ("create", "update"):
            for m in re.finditer(r"(?:ssh|tcp)://[^\s,\"']+", j):
                r = _remote(*_host_port(m.group(0)))
                if r.kind == "helper":
                    blk("a", "docker context to helper %s" % r, ORCH + " nodes")
                if r.kind == "prod":
                    ask_prod("a docker context", r)
        return
    # (c) destructive anywhere
    cleanup = ORCH + " gc --session-end"
    if "prune" in lr:
        label = orch_label()
        lab = r"--filter[= ]label=%s=1\b" % re.escape(label)
        if sub == "system":
            blk("c", "docker system prune (other projects' objects)", cleanup)
        if sub in ("volume", "network"):
            blk("c", "docker %s prune (other projects' objects)" % sub, cleanup)
        if sub == "image" and any(r in ("-a", "--all") or re.match(r"^-[a-z]*a[a-z]*$", r)
                                  or (r.startswith("--all=") and not r.endswith("false")) for r in lr):
            blk("c", "docker image prune -a (other projects' images)", cleanup)
        if sub == "image" and not re.search(lab, j):
            blk("c", "docker image prune without --filter label=%s=1" % label, cleanup)
        if sub in ("builder", "buildx") and not re.search(r"--builder[= ]%sbuilder\b" % re.escape(orch_prefix()), j):
            blk("c", "docker %s prune without --builder %sbuilder" % (sub, orch_prefix()), cleanup)
        if sub == "container" and not re.search(lab, j):
            blk("c", "docker container prune without --filter label=%s=1" % label, cleanup)
    # (rule 4) shared-host changes anywhere
    if sub == "compose" and any(r == "--rmi=all" or (r == "--rmi" and lr[k + 1:k + 2] == ["all"])
                                for k, r in enumerate(lr)):
        blk("c", "docker compose --rmi all (other projects' images, rule 4)", cleanup)
    if sub == "login" and any(r in ("-p", "--password", "--password-stdin") or r.startswith("--password=")
                              or (r.startswith("-p") and not r.startswith("--")) for r in lr):
        blk("c", "docker login with a password (rule 4)", ORCH + " nodes")
    pub_args = rest if sub in ("run", "create") else \
        (rest[1:] if sub == "container" and lr[:1] and lr[0] in ("run", "create") else None)
    if pub_args is not None and _publishes(pub_args):
        blk("c", "docker %s publishing host ports (rule 4)" % sub, ORCH + " run <job>")
    rk = _kind(where)
    if rk == "prod":
        prod_docker(sub, rest, lr, where, depth)
        return
    if rk is None and not target and CTX.docker_default is not None:
        prod_docker(sub, rest, lr, CTX.docker_default, depth)  # default engine is production; rule d still applies
    if rk == "other":
        return
    # (d) heavy
    tool = ["docker", sub] + lr
    first = first_nonopt(lr, {"--builder"} if sub == "buildx" else ())
    if sub == "build" or (sub in ("image", "builder") and first == "build") or \
            (sub == "buildx" and first in ("build", "b", "bake")):
        heavy("docker build", tool, where, rest)
        return
    if sub == "compose":
        csub = first_nonopt(lr, COMPOSE_VAL)
        if csub in ("up", "build"):
            heavy("docker compose " + csub, tool, where, rest)
        return
    run_args = None
    if sub in ("run", "create"):
        run_args = rest
    elif sub == "container" and lr[:1] and lr[0] in ("run", "create"):
        run_args = rest[1:]
    if run_args is not None:
        img = run_image(run_args)
        if heavy_image(img):
            heavy("docker run " + img, tool, where, run_args)


def heavy_image(img):
    last = img.split("/")[-1]
    return "mcr.microsoft.com/dotnet/sdk" in img or "mcr.microsoft.com/playwright" in img or \
        last == "node" or last.startswith("node:") or last.startswith("node@")


# ---------------------------------------------------------------- rule p: production hosts
DOCKER_READ = {"ps", "logs", "inspect", "stats", "top", "images", "version", "info", "port", "diff", "history",
               "events", "search", "help"}
DOCKER_MGMT = {"container", "image", "volume", "network", "system", "builder", "buildx", "stack", "service",
               "node", "secret", "config", "plugin", "swarm", "manifest", "trust", "checkpoint"}
DOCKER_MGMT_READ = {"ls", "list", "ps", "inspect", "logs", "top", "stats", "port", "diff", "history", "df",
                    "info", "events", "show", "du"}
DOCKER_GONE = {"rm", "rmi", "kill", "stop", "restart", "remove", "prune", "leave"}
COMPOSE_READ = {"ps", "logs", "config", "ls", "images", "top", "version", "port", "events"}
COMPOSE_GONE = {"down", "rm", "kill", "stop", "restart"}
EXEC_VAL = {"-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir", "--detach-keys"}
PROD_READ = {"cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "zgrep", "zcat", "rg", "ls", "dir",
             "stat", "df", "du", "free", "uptime", "whoami", "id", "hostname", "date", "ps", "ss", "netstat",
             "cd", "pwd", "echo", "printf", "true", "false", ":", "test", "[", "[[", "sleep", "wc", "sort", "uniq",
             "cut", "tr", "nproc", "uname", "lscpu", "lsblk", "vmstat", "iostat", "w", "who", "printenv", "which",
             "type", "file", "readlink", "realpath", "basename", "dirname", "md5sum", "sha1sum", "sha256sum", "jq",
             "tac", "nl", "column", "get-content", "gc", "get-childitem", "gci", "get-item", "gi", "test-path",
             "select-string", "sls", "findstr", "get-process", "gps", "get-service", "gsv", "get-date",
             "write-output", "write-host", "tasklist", "select-object", "select", "where-object", "where",
             "sort-object", "measure-object", "format-table", "ft", "format-list", "fl", "out-string",
             "convertto-json"}
SYSTEMCTL_READ = {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "list-timers",
                  "list-sockets", "list-dependencies", "show", "cat"}
JOURNAL_WRITE = ("--vacuum", "--rotate", "--flush", "--sync", "--relinquish-var", "--smart-relinquish-var",
                 "--setup-keys", "--update-catalog")
FIND_ACT = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
PROD_GONE = {"dd", "truncate", "shutdown", "reboot", "poweroff", "halt", "kill", "pkill", "killall", "shred",
             "wipefs", "fdisk", "sfdisk", "parted", "stop-computer", "restart-computer", "stop-process", "spps",
             "taskkill", "format", "diskpart", "clear-disk", "format-volume", "remove-partition"}
SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "invoke-sqlcmd", "mongo", "mongosh", "redis-cli",
               "cqlsh", "clickhouse-client"}
SQL_GONE = re.compile(r"\b(drop|delete|truncate|flushall|flushdb)\b")
NULL_SINKS = {"/dev/null", "/dev/stdout", "/dev/stderr", "nul", "$null", "-"}
CURL_SEND = ("--data", "--form", "--upload-file", "--json", "--config", "--quote", "--remote-name", "--output-dir",
             "--create-dirs", "--dump-header", "--cookie-jar", "--trace", "--libcurl", "--etag-save", "--hsts",
             "--alt-svc", "--mail")
CURL_VAL = set("AbcCdDeEFHKmoPQrtTuUwxXYyz")
WGET_VAL = set("OoaeiBtTwQPUlARDIX")


def prod_docker(sub, rest, lr, remote, depth):
    """Docker on a production host: read-only -> allow; removals, stops, restarts -> deny; else -> ask."""
    if sub == "compose":
        csub = first_nonopt(lr, COMPOSE_VAL)
        what = "docker compose " + csub
        if csub in COMPOSE_GONE:
            deny_prod("'%s'" % what, remote)
        if not csub or csub in COMPOSE_READ or LIGHT & set(lr):
            return
    elif sub in DOCKER_MGMT:
        obj = first_nonopt(lr)
        what = ("docker %s %s" % (sub, obj)).strip()
        if obj in DOCKER_GONE:
            deny_prod("'%s'" % what, remote)
        if not obj or obj in DOCKER_MGMT_READ or LIGHT & set(lr):
            return
    else:
        what = "docker " + sub
        if sub in DOCKER_GONE:
            deny_prod("'%s'" % what, remote)
        if not sub or sub in DOCKER_READ or LIGHT & set(lr):
            return
        if sub == "exec":
            n, k = len(CTX.asks), 0
            while k < len(rest) and rest[k].startswith("-"):
                k += 2 if rest[k].lower() in EXEC_VAL else 1
            if rest[k + 1:]:
                check_cmd(Cmd(False), rest[k + 1:], "sh", remote, depth + 1, None)
                if len(CTX.asks) == n:
                    return  # a read-only command inside a production container
    ask_prod("'%s'" % what, remote)


def prod_redirs(c, remote):
    """Output redirection on a production host: `> file` overwrites (deny), `>> file` appends (ask)."""
    for op, t in c.redirs:
        if ">" in op and t.lower() not in NULL_SINKS:
            if op.endswith(">>"):
                ask_prod("appending to %s" % t, remote)
            else:
                deny_prod("overwriting %s" % t, remote)


def prod_cmd(c, name, args, remote, stdin_text):
    """One simple command on a production host: read-only -> allow; destructive -> deny; else -> ask."""
    largs = [a.lower() for a in args]
    flags = "".join(a[1:] for a in largs if a.startswith("-") and not a.startswith("--"))
    sub = first_nonopt(largs, {"-c", "-C"})
    gone = None
    if name in PROD_GONE or name.startswith("mkfs") or (name in ("init", "telinit") and largs[:1] in (["0"], ["6"])):
        gone = name
    elif name == "rm" and (set(flags) & set("rf") or {"--recursive", "--force"} & set(largs)):
        gone = "rm -r/-f"
    elif name in ("remove-item", "ri", "del", "erase", "rd", "rmdir") and \
            any(a in ("/s", "/q") or a.startswith("-rec") or a.startswith("-fo") for a in largs):
        gone = name + " recursive/force"
    elif name == "sed" and _sed_inplace(largs):
        gone = "sed -i"
    elif name in ("tee", "tee-object") and not {"-a", "--append", "-append"} & set(largs) and \
            any(not a.startswith("-") for a in largs):
        gone = name + " (overwrites)"
    elif name in ("chmod", "chown", "chgrp") and ("r" in flags or "--recursive" in largs):
        gone = name + " -R"
    elif name == "git" and (sub == "clean" or (sub == "reset" and "--hard" in largs)):
        gone = "git " + sub
    elif name == "systemctl" and first_nonopt(largs) in ("poweroff", "reboot", "halt", "kill", "kexec"):
        gone = "systemctl " + first_nonopt(largs)
    elif name == "find" and "-delete" in largs:
        gone = "find -delete"
    elif name in SQL_CLIENTS:
        text = " ".join(largs) + " " + (stdin_text or "").lower()
        if SQL_GONE.search(text) or (name == "redis-cli" and re.search(r"\b(del|unlink)\b", text)):
            gone = name + " DROP/DELETE/TRUNCATE"
    if gone:
        deny_prod("'%s'" % gone, remote)
    if not _prod_read(name, args, largs):
        ask_prod("'%s'" % (name or "$(...)"), remote)   # "" = a command name built by a substitution


def _prod_read(name, args, largs):
    if name == "systemctl":
        return first_nonopt(largs) in SYSTEMCTL_READ
    if name == "top":
        return any(a.startswith("-") and "b" in a for a in largs)
    if name == "journalctl":
        return not any(a.startswith(JOURNAL_WRITE) for a in largs)
    if name == "find":
        return not FIND_ACT & set(largs)
    if name == "curl":
        return _curl_read(args)
    if name == "wget":
        return _wget_read(args)
    return name in PROD_READ


def _curl_read(args):
    """curl GET/HEAD printing to stdout only."""
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith("--"):
            o, eq, v = a.partition("=")
            if o.startswith(CURL_SEND):
                return False
            if o in ("--request", "--output"):
                if not eq:
                    v, i = (args[i] if i < len(args) else ""), i + 1
                if (o == "--request" and v.upper() not in ("GET", "HEAD")) or \
                        (o == "--output" and v not in ("-", "/dev/null")):
                    return False
        elif a.startswith("-") and len(a) > 1:
            for k, f in enumerate(a[1:]):
                if f in "dFTKQO":
                    return False
                if f in CURL_VAL:
                    v = a[k + 2:]
                    if not v:
                        v, i = (args[i] if i < len(args) else ""), i + 1
                    if (f == "X" and v.upper() not in ("GET", "HEAD")) or \
                            (f in "oDc" and v not in ("-", "/dev/null", "/dev/stdout")):
                        return False
                    break
    return True


def _wget_read(args):
    """wget printing to stdout (-O -) or --spider, without POST/PUT bodies or files left on the host."""
    out = spider = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith("--"):
            o, eq, v = a.partition("=")
            if o.startswith(("--post", "--method", "--body", "--save-cookies", "--output-file", "--append-output",
                             "--warc", "--directory-prefix")):
                return False
            if o == "--spider":
                spider = True
            elif o == "--output-document":
                if not eq:
                    v, i = (args[i] if i < len(args) else ""), i + 1
                out = v == "-"
        elif a.startswith("-") and len(a) > 1:
            for k, f in enumerate(a[1:]):
                if f in WGET_VAL:
                    v = a[k + 2:]
                    if not v:
                        v, i = (args[i] if i < len(args) else ""), i + 1
                    if f == "O":
                        out = v == "-"
                    elif f in "oaP":
                        return False
                    break
    return out or spider


def check_heavy(name, args, largs, env, remote):
    if name in ("dotnet", "npm", "pnpm", "yarn") and LIGHT & set(largs):
        return
    tool = [name] + largs
    n = len(CTX.pending)
    _heavy_kind(name, args, largs, tool, remote)
    if remote is None and len(CTX.pending) == n and not LIGHT & set(largs) and not flag_exists() and \
            _best_job(tool, _projects_at(_hints(args))):
        # a tool this project's own jobs use (job index): rule d inside that project, whatever new_heavy says
        CTX.pending.append((" ".join([name] + args[:2]).replace("\x00", "$(...)"), tool, _hints(args)))


def _heavy_kind(name, args, largs, tool, remote):
    if name == "dotnet":
        sub = first_nonopt(largs)
        if sub == "watch":
            heavy("dotnet watch " + first_nonopt(largs[largs.index(sub) + 1:]), tool, remote, args)
        elif sub in DOTNET_HEAVY:
            heavy("dotnet " + sub, tool, remote, args)
    elif name == "npm":
        sub = first_nonopt(largs, NPM_VAL)
        if sub in ("exec", "x") and any("playwright" in a for a in largs):
            heavy("npm exec playwright", tool, remote, args)
        elif sub in NPM_HEAVY:
            heavy("npm " + sub, tool, remote, args)
        elif sub in ("run", "run-script", "rum", "urn"):
            script = first_nonopt(largs[largs.index(sub) + 1:], NPM_VAL)
            if _script_heavy(script):
                heavy("npm run " + script, tool, remote, args)
    elif name == "npx":
        if any("playwright" in a for a in largs):
            heavy("npx playwright", tool, remote, args)
    elif name in ("pnpm", "yarn"):
        sub = first_nonopt(largs, {"-c", "--dir", "--filter", "-f", "--cwd"})
        if name == "yarn" and not largs:
            heavy("yarn (install)", tool, remote, args)
        elif sub in ("install", "i", "add", "test", "t", "build", "ci"):
            heavy("%s %s" % (name, sub), tool, remote, args)
        elif sub == "run" or sub == "run-script":
            script = first_nonopt(largs[largs.index(sub) + 1:])
            if _script_heavy(script):
                heavy("%s run %s" % (name, script), tool, remote, args)
        elif "playwright" in sub or (sub in ("exec", "dlx") and any("playwright" in a for a in largs)):
            heavy("%s playwright" % name, tool, remote, args)
    else:
        what = new_toolchain(name, largs)
        if what:
            new_heavy(what, remote, tool, args)


NPM_VAL = {"--prefix", "-c", "--workspace", "-w", "--cache", "--registry", "--userconfig"}
NPM_HEAVY = {"ci", "clean-install", "install", "i", "in", "add", "test", "t", "tst",
             "install-test", "it", "cit", "install-ci-test"}


def _script_heavy(s):
    return s in ("build", "test", "lint") or s.split(":", 1)[0] in ("build", "test", "lint") and ":" in s


def new_toolchain(name, largs):
    sub = first_nonopt(largs)
    if name in ("pytest", "py.test", "mvn", "mvnw", "gradle", "gradlew", "make"):
        return name
    if name in ("pip", "pip3") and sub == "install":
        return name + " install"
    if PY_RE.match(name):
        for k, a in enumerate(largs):
            mod = largs[k + 1] if a == "-m" and k + 1 < len(largs) else (a[2:] if a.startswith("-m") else None)
            if mod is not None:
                rest = largs[k + 2:] if a == "-m" else largs[k + 1:]
                if mod == "pytest" or (mod == "pip" and first_nonopt(rest) == "install"):
                    return "%s -m %s" % (name, mod)
                return None
            if not a.startswith("-") or a in ("-c", "-"):
                return None
        return None
    pairs = {"uv": ("run", "sync", "pip"), "bun": ("install", "test"), "deno": ("test", "task"),
             "cargo": ("build", "test"), "go": ("build", "test")}
    if name in pairs and sub in pairs[name]:
        return "%s %s" % (name, sub)
    if name == "cmake" and "--build" in largs:
        return "cmake --build"
    return None


DOTNET_HEAVY = {"build", "test", "publish", "run", "pack", "restore", "msbuild", "vstest"}
XARGS_HEAVY = {"dotnet", "npm", "npx", "pnpm", "yarn", "docker", "docker-compose", "podman", "nerdctl"}
START_BOOL = ("wait", "nonewwindow", "passthru", "usenewenvironment", "loaduserprofile")
PY_RE = re.compile(r"^(python[0-9.]*|py)$")
NEW_TOOLS = {"pytest", "py.test", "pip", "pip3", "python", "python3", "py", "uv", "bun", "deno", "make",
             "cmake", "cargo", "go", "mvn", "mvnw", "gradle", "gradlew"}
KNOWN = (SHELLS | PWSH | DOCKERS | REMOTE_BAD | E_SAFE | set(WRAPPER_VALOPTS) | PREFIX_WORDS | NEW_TOOLS | CD_CMDS |
         DELETERS | MOVERS | COPIERS | PROD_GONE | SQL_CLIENTS |
         {"cmd", "eval", "iex", "invoke-expression", "wsl", "ssh", "env", "dotnet", "npm", "npx", "pnpm", "yarn",
          "git", "sc", "find", "start-process", "saps", "scp", "rsync", "sftp", "orch", "orch.py", "tee", "sed",
          "curl", "wget", "systemctl"})
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
# On an internal error or an unreadable view, fail closed only for commands that look relevant; anything else
# fails open so a broken guard can never block ordinary work.
PREFILTER = re.compile(r"docker|podman|nerdctl|ssh|scp|rsync|sftp|dotnet|npm|npx|pnpm|yarn|allow_local"
                       r"|helper_orch_local|prune|guard\.json|guard-view|trust\.json|registry\.json|--appr|wsl"
                       r"|systemctl|service|daemon\.json", re.I)
DYN_HEAVY = re.compile(PREFILTER.pattern + r"|\b(build|test|install|publish|restore|pack|pytest|pip)\b", re.I)
VIEW_BAD = _msg("guard view unreadable", INIT)


def _mask(command, view):
    """The command with every registered deploy/rollback command of an approved project replaced by `true`, only
    where it stands as a whole simple command (extra arguments could reach a remote shell, so they still count)."""
    for p in view.projects:
        for r in p["commands"]:
            toks = r.split()
            if toks:
                rx = r"(^|[;&|(\n])(\s*)" + r"\s+".join(re.escape(t) for t in toks) + r"(?=\s*(?:$|[;&|)\n]))"
                command = re.sub(rx, lambda m: m.group(1) + m.group(2) + "true", command)
    return command


def check(command, tool_name="Bash", cwd=None, view=None, prefs=None, session=False, log=True):
    """One shell command -> None (no decision) or (decision, rule, reason); decision is "deny" or "ask".
    Deny wins over ask. Production asks that come only from a registered deploy/rollback command are dropped.
    tool_name picks the dialect (PowerShell, cmd, else sh); session: an MCP terminal runs it (MF3); log=False skips
    guard-warn.log (a second dialect of the same MCP command)."""
    global CTX
    prefs = prefs if prefs is not None else guard_prefs()
    mode = {"PowerShell": "ps", "cmd": "cmd"}.get(tool_name, "sh")
    CTX = ctx = Ctx(view, cwd, prefs, command)
    ctx.mode, ctx.session = mode, session
    try:
        analyze(command, mode, None, 0)
        for what, tool, hints in ctx.pending:
            if _d_scope(hints):
                blk("d", "%s on master" % what, _use_run(tool, None, hints))
    except Deny as d:
        return "deny", d.rule, d.msg
    for reason, hints in ctx.dyn:
        if _d_scope(hints):
            ask("v", reason)
    for line, hints in ctx.warns:
        if log and (hints is None or _d_scope(hints)):
            _log("guard-warn.log", line)
    asks = ctx.asks
    if any(r == "p" for r, _ in asks):
        masked = _mask(command, ctx.view)
        if masked != command:
            CTX = sub = Ctx(ctx.view, cwd, prefs, masked)
            sub.mode, sub.session = mode, session
            try:
                analyze(masked, mode, None, 0)
                asks = [a for a in asks if a[0] != "p"] + [a for a in sub.asks if a[0] == "p"]
            except Deny:
                pass  # cannot happen when the full command passed; keep the full command's asks
            CTX = ctx
    if not asks:
        return None
    reasons = []
    for _, r in asks:
        if r not in reasons:
            reasons.append(r)
    return "ask", asks[0][0], "; ".join(reasons)


def check_file(path, cwd=None, view=None):
    """A Write/Edit/MultiEdit/NotebookEdit target -> None or ("deny", rule, reason)."""
    global CTX
    CTX = Ctx(view, cwd, {}, path or "")
    p = (path or "").replace("\\", "/").lower()
    if not p:
        return None
    if "allow_local" in p or PREFS_JSON_RE.search(p):
        return "deny", "e", _msg("editing a user-only file (%s)" % p.rsplit("/", 1)[-1], USER)
    if DAEMON_JSON_RE.search(p):
        return "deny", "c", _msg("editing daemon.json (rule 4)", ORCH + " nodes")
    k = _protected(path, True)
    if k and k[0] == "s":
        return "deny", "s", _msg("editing orch setup file %s" % k[1], INIT)
    return None


# ---------------------------------------------------------------- MCP tools (MF3)
MCP_RUN = re.compile(r"process|command|shell|exec|terminal")
MCP_CMD_KEYS = ("command", "cmd", "input")
MCP_WRITE = ("write_file", "edit_block", "edit_file", "move_file", "create_directory", "delete", "write",
             "remove", "rename", "unlink", "copy")
MCP_GONE = ("delete", "remove", "move", "rename", "unlink")   # path inputs that may take a setup file away
MCP_PATH_KEYS = {"path", "file_path", "source", "destination", "paths"}
MCP_TEXT_KEYS = {"content", "contents", "old_string", "new_string", "text", "data", "body"}   # a file tool's data


def _mcp(tool):
    return isinstance(tool, str) and tool.startswith("mcp__")


def _mcp_command(tool, ti):
    """(key, text) of an MCP tool that runs commands: its name contains process/command/shell/exec/terminal (so
    every *start_process, *interact_with_process, *execute_command, *run_command, *exec, *shell, *terminal) and
    its input has a non-empty command/cmd/input string. None otherwise."""
    if not MCP_RUN.search(tool.lower()):
        return None
    for k in MCP_CMD_KEYS:
        v = ti.get(k)
        if isinstance(v, str) and v.strip():
            return k, v
    return None


def _mcp_modes(ti, key):
    """Dialects an MCP command is read in: the shell another input names (PowerShell, cmd, a POSIX shell), else
    both sh and PowerShell, since the server's default shell is unknown (Desktop Commander: PowerShell on
    Windows)."""
    for k, v in ti.items():
        n = cmdname(v.strip()) if k != key and isinstance(v, str) else ""
        if n in PWSH:
            return ("PowerShell",)
        if n == "cmd":
            return ("cmd",)
        if n in SHELLS:
            return ("Bash",)
    return ("Bash", "PowerShell")


def _mcp_paths(ti):
    """(key, path) of every path input: path, file_path, source, destination, paths[] and any *path(s) key."""
    for k, v in ti.items():
        kl = str(k).lower()
        if kl in MCP_PATH_KEYS or kl.endswith(("path", "paths")):
            for p in (v if isinstance(v, list) else [v]):
                if isinstance(p, str) and p.strip():
                    yield kl, p


def _mcp_strings(ti, skip):
    """Every string in an MCP input, nested ones included, leaving out the top-level keys in skip; at most TEXT_MAX
    characters in all."""
    out, stack, budget = [], [v for k, v in ti.items() if str(k).lower() not in skip], TEXT_MAX
    while stack and budget > 0:
        x = stack.pop()
        if isinstance(x, str):
            out.append(x[:budget])
            budget -= len(x)
        elif isinstance(x, dict):
            stack += list(x.values())
        elif isinstance(x, list):
            stack += x
    return out


def _merge(results):
    """Several decisions on one call -> the first deny, else one ask holding every reason once, else None."""
    res = [r for r in results if r]
    deny = next((r for r in res if r[0] == "deny"), None)
    if deny or not res:
        return deny
    reasons = []
    for r in res:
        for part in r[2].split("; "):
            if part not in reasons:
                reasons.append(part)
    return "ask", res[0][1], "; ".join(reasons)


def check_mcp(tool, ti, cwd=None, view=None, prefs=None):
    """An MCP tool call -> None or (decision, rule, reason).
    - A command tool's command meets every shell rule (in the dialect another input names, else in both sh and
      PowerShell); a session it opens to a helper is denied, since later input reaches it unchecked. An "input" sent
      to a running process may be a REPL's code, so it also gets the inline-code check (rule x).
    - A file tool's path inputs meet the Write/Edit protection; a delete or move also the setup-folder checks.
    - Any other input string (not a file tool's data) that names a helper or production host of the view or a
      protected path asks (rule m). Harmless calls get no decision."""
    global CTX
    ti = ti if isinstance(ti, dict) else {}
    low, res, used = tool.lower(), [], set()
    short = tool.rsplit("__", 1)[-1][:60]
    got = _mcp_command(tool, ti)
    if got:
        key, text = got
        used.add(key)
        for k, m in enumerate(_mcp_modes(ti, key)):
            res.append(check(text, m, cwd, view, prefs, session=True, log=k == 0))
        CTX = Ctx(view, cwd, {}, text)
        what = _text_names(text) if key == "input" else None
        if what:
            res.append(("ask", "x", "input to a running process naming %s needs your OK (the guard cannot tell "
                                    "which program reads it; spell the command out)" % what))
    file_tool = any(w in low for w in MCP_WRITE)
    if file_tool:
        used |= MCP_TEXT_KEYS
        for k, p in _mcp_paths(ti):
            used.add(k)
            r = check_file(p, cwd, view)          # sets CTX for the setup-folder checks
            if r is None and any(w in low for w in MCP_GONE) and _covers(p):
                r = "deny", "s", _msg("'%s' on orch setup files (%s)" % (short, p), INIT)
            if r is None and k == "destination" and _into_setup(p):
                r = "deny", "s", _msg("'%s' into orch setup folder %s" % (short, p), INIT)
            res.append(r)
    if not any(r and r[0] == "deny" for r in res):
        CTX = Ctx(view, cwd, {}, "")
        for s in _mcp_strings(ti, used):
            what = _text_names(s)
            if what:
                res.append(("ask", "m", "MCP tool %s naming %s needs your OK (allow only what you would allow as "
                                        "a shell command)" % (tool[5:], what)))
                break
    return _merge(res)


def decide(data, view):
    """Hook payload (dict) -> None or (decision, rule, reason)."""
    tool, ti = data.get("tool_name"), data.get("tool_input") or {}
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
    if tool in FILE_TOOLS:
        return check_file(str(ti.get("file_path") or ti.get("notebook_path") or ""), cwd, view)
    if _mcp(tool):
        return check_mcp(tool, ti, cwd, view)
    cmd = ti.get("command")
    if tool in SHELL_TOOLS and isinstance(cmd, str) and cmd.strip():
        return check(cmd, tool, cwd, view)
    return None


def _emit(decision, reason):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))
    sys.stdout.flush()


def _hook_text(data):
    """The command a payload runs (a shell tool's, or an MCP command tool's), for the fail-closed prefilter."""
    tool, ti = data.get("tool_name"), data.get("tool_input") or {}
    if _mcp(tool):
        got = _mcp_command(tool, ti) if isinstance(ti, dict) else None
        return got[1] if got else None
    return ti.get("command")


def main():
    cmd = None
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        data = json.loads(raw) if raw.strip() else {}
        cmd = _hook_text(data)
        if data.get("tool_name") not in FILE_TOOLS + SHELL_TOOLS and not _mcp(data.get("tool_name")):
            return 0
        try:
            view = load_view()
            if view is None and os.path.exists(os.path.join(orch_home(), "trust.json")):
                # an approved setup existed (registry.json alone does not count: init may write it first)
                raise ViewError("guard-view.json is missing although trust.json exists")
        except ViewError as e:
            _log("guard-errors.log", "guard view unreadable: %s" % e)
            if isinstance(cmd, str) and PREFILTER.search(cmd):
                _emit("deny", VIEW_BAD)
                return 0
            sys.stderr.write("helper-orch guard: guard view unreadable (%s); run orch init\n" % e)
            view = View(EMPTY)  # the rules that need no view still apply
        if view is None:
            return 0  # no view: nothing approved on this master
        res = decide(data, view)
        if res:
            _emit(res[0], res[2])
    except BaseException as e:
        try:
            if isinstance(cmd, str) and PREFILTER.search(cmd):
                _log("guard-errors.log", "fail-closed %r :: %s" % (e, cmd[:300]))
                _emit("deny", "blocked: guard internal error on a docker/ssh/build command; use: %s" % RUNJOB)
            else:
                _log("guard-errors.log", "fail-open %r" % (e,))
                sys.stderr.write("helper-orch guard: internal error, failing open: %r\n" % (e,))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
