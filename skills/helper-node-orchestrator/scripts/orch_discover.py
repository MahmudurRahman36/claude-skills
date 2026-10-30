#!/usr/bin/env python3
"""orch_discover - what orch learns about a host before it uses it (design section 5).

ssh_resolve / ssh_args / ssh_cmdline turn what the user typed after `ssh` into exact argv and display text. probe_host
runs ONE read-only ssh round trip per host (a POSIX sh script on stdin; a PowerShell probe when the remote shell is not
POSIX) and returns the state/hosts.json record. classify_ssh_error maps a failure to a class, the exact command tried
and the exact fix. choose_transport / ensure_context / remove_context pick and maintain the docker transport.

Stdlib only. Every process goes through run= (subprocess.run signature) so unit tests never touch ssh, docker or the
network. Nothing here writes files; the caller (orch init) stores the records.
"""
import base64
import collections
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess

# Host keys stay strict whatever ~/.ssh/config says: a command-line -o wins over the config file.
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
            "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
CONTEXT_PREFIX = "orch-"
DEFAULT_LABEL, DEFAULT_PREFIX = "io.orch.agent", "agentx-"  # same defaults as orch_config; env ORCH_LABEL/PREFIX add
ENDPOINT_MAX = 8
# Published ports that never speak HTTP (databases, brokers, mail, remote shells, the docker API): an HTTP probe there
# only adds noise to a tenant's logs (Redis logs it as an attack), so endpoint discovery skips them.
NON_HTTP_PORTS = (21, 22, 23, 25, 53, 110, 111, 135, 139, 143, 389, 445, 465, 587, 636, 993, 995, 1433, 1521, 1883,
                  2049, 2181, 2375, 2376, 3306, 3389, 4222, 5432, 5671, 5672, 6379, 6380, 8883, 9042, 9092, 11211,
                  26379, 27017)
PS_PREFIX = "powershell -NoProfile -NonInteractive -EncodedCommand "
PS_MAX = 8000  # cmd.exe (the default sshd shell on Windows) refuses command lines over 8191 characters

_SPEC = re.compile(r"(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]*)@)?"
                   r"(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<host>[A-Za-z0-9_][A-Za-z0-9._-]*))(?::(?P<port>[0-9]{1,5}))?")
_NS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_CONTEXT = re.compile(CONTEXT_PREFIX + r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_NULL_FILES = {"/dev/null", "nul", "none"}
_R = collections.namedtuple("_R", "returncode stdout stderr")


class DiscoverError(RuntimeError):
    """A local step failed (ssh or docker CLI missing, bad ssh config, timeout). str(e) is a plain sentence that names
    the thing and the fix; .kind is "missing", "timeout" or "error"."""

    def __init__(self, msg: str, kind: str = "error"):
        super().__init__(msg)
        self.kind = kind


# ----------------------------------------------------------------------------- small helpers
def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _dec(b) -> str:
    if b is None:
        return ""
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else str(b)


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _clean(s: str) -> str:
    """One untrusted output line, safe to show: no ANSI escapes or control characters, at most 300 chars."""
    s = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)
    return re.sub(r"[\x00-\x1f\x7f]", " ", s).strip()[:300]


def _lines(text: str, skip=None) -> list[str]:
    out = [_clean(x) for x in _dec(text).splitlines()]
    return [x for x in out if x and not (skip and re.match(skip, x))]


def _last_line(text: str, skip=r"debug\d: ") -> str:
    ls = _lines(text, skip)
    return ls[-1] if ls else ""


def _show(argv) -> str:
    """argv as a shell line; an encoded PowerShell payload is shortened (it is not something a human retypes)."""
    return shlex.join([a if len(a) < 300 else a[:60] + "..." for a in map(str, argv)])


def _call(run, argv, input=None, timeout=60, env=None) -> _R:
    """run() with captured output. Input goes as bytes: Windows text-mode stdin turns \\n into \\r\\n, which breaks
    the remote sh. No input -> stdin is /dev/null, so ssh never eats orch's own stdin. Output may be bytes (the real
    subprocess.run) or str (test fakes)."""
    kw = {"capture_output": True, "timeout": timeout}
    if input is None:
        kw["stdin"] = subprocess.DEVNULL
    else:
        kw["input"] = input.encode("utf-8") if isinstance(input, str) else input
    if env is not None:
        kw["env"] = env
    try:
        r = run(list(argv), **kw)
    except FileNotFoundError:
        raise DiscoverError(f"{argv[0]} is not installed on this machine or not on PATH: install it, then retry",
                            "missing") from None
    except subprocess.TimeoutExpired:
        raise DiscoverError(f"{_show(argv)} did not finish within {timeout} s", "timeout") from None
    except OSError as e:
        raise DiscoverError(f"could not start {argv[0]}: {e}") from None
    return _R(r.returncode, _dec(r.stdout), _dec(r.stderr))


def _namespaces() -> tuple[list[str], list[str]]:
    """Labels and name prefixes that mark orch's own containers: the defaults plus ORCH_LABEL / ORCH_PREFIX (the test
    namespace) when set and well-formed, so neither is ever taken for a co-tenant."""
    labels, prefixes = [DEFAULT_LABEL], [DEFAULT_PREFIX]
    for lst, var in ((labels, "ORCH_LABEL"), (prefixes, "ORCH_PREFIX")):
        v = os.environ.get(var, "")
        if v and _NS.fullmatch(v) and v not in lst:
            lst.append(v)
    return labels, prefixes


def own_namespace() -> tuple[str, str]:
    """(label, prefix) of THIS master: ORCH_LABEL / ORCH_PREFIX when set and well-formed, else the defaults."""
    lab, pfx = os.environ.get("ORCH_LABEL", ""), os.environ.get("ORCH_PREFIX", "")
    return (lab if _NS.fullmatch(lab) else DEFAULT_LABEL), (pfx if _NS.fullmatch(pfx) else DEFAULT_PREFIX)


# ----------------------------------------------------------------------------- another orch namespace's objects
FOREIGN_KINDS = ("container", "volume", "network")
FOREIGN_MAX = 24  # names written into the fix; the rest are counted
_DOCKER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_FAMILY = re.compile(r"[a-z0-9][a-z0-9.-]{0,39}")


def labels_of(text) -> dict:
    """docker's {{.Labels}} text (key=value pairs joined by commas) -> {key: value}. A value holding a comma is cut
    there: only the keys and the agent value are read."""
    out = {}
    for part in str(text or "").split(","):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out.setdefault(k.strip(), v.strip())
    return out


def foreign_objects(rows, prefix, label) -> list:
    """[(kind, name, labels text)] -> the containers, volumes and networks named prefix* that belong to another orch
    namespace: they carry neither label=1 nor any other label of its family (label minus its last part), so an older
    orch or another master made them. Each {"kind", "name", "family"}: family = the <family> of a <family>.agent label
    they carry, else None (no orch label at all). A name docker never prints is left out (it would reach a fix line)."""
    fam = label.rsplit(".", 1)[0] + "."
    out, seen = [], set()
    for row in rows:
        kind, name, text = (list(row) + ["", "", ""])[:3]
        name = str(name).strip()
        if kind not in FOREIGN_KINDS or not name.startswith(prefix) or not _DOCKER_NAME.fullmatch(name) \
                or (kind, name) in seen:
            continue
        labs = labels_of(text)
        if labs.get(label) == "1" or any(k.startswith(fam) for k in labs):
            continue  # ours, or our family without the agent label: an ownership collision, not another namespace
        seen.add((kind, name))
        fams = sorted(k[:-6] for k in labs if k.endswith(".agent") and _FAMILY.fullmatch(k[:-6]))
        out.append({"kind": kind, "name": name, "family": fams[0] if fams else None})
    return out


def foreign_problem(objs, prefix, tried="-") -> dict:
    """foreign_objects() output -> the problem record (class foreign-namespace) with the exact fix: that orch's gc, or
    the docker commands that remove the listed names by hand. orch never removes them itself."""
    n = len(objs)
    fams = sorted({f"{o['family']}.*" if o.get("family") else "none" for o in objs}, key=lambda x: (x == "none", x))
    listed = objs[:FOREIGN_MAX]
    cmds = [f"docker {verb} " + " ".join(o["name"] for o in listed if o["kind"] == kind)
            for kind, verb in (("container", "rm -f"), ("volume", "volume rm"), ("network", "network rm"))
            if any(o["kind"] == kind for o in listed)]
    rest = n - len(listed)
    more = (f"; then list the other {rest} with docker ps -a, docker volume ls and docker network ls, each with "
            f"--filter name={prefix}") if rest else ""
    what = (f"{n} object{'s' if n != 1 else ''} named {prefix}* belong{'s' if n == 1 else ''} to another orch "
            f"namespace (label {', '.join(fams)}): an older orch or another master")
    fix = (f"run that orch's gc --session-end on this host (removing volumes needs the user's OK); or, if no other "
           f"master uses this host, remove them by hand: {'; '.join(cmds)}{more}")
    return _problem("foreign-namespace", what, fix, tried)


# ----------------------------------------------------------------------------- ssh spec -> argv
def _split_spec(spec) -> tuple:
    """`[user@]host[:port]` (host = alias, DNS name, IPv4 or [IPv6]) -> (user|None, host, port|None). Rejects
    anything else, including a leading "-" (it would be read as an ssh option) and shell metacharacters."""
    m = _SPEC.fullmatch(spec) if isinstance(spec, str) else None
    port = _int(m["port"]) if m and m["port"] else None
    if not m or (port is not None and not 0 < port < 65536) or (m["v6"] and ":" not in m["v6"]):
        raise ValueError(f"bad ssh spec {spec!r}: use what you type after ssh - an alias, host, user@host or "
                         "user@host:port (IPv6 as user@[address]:port)")
    return m["user"], m["v6"] or m["host"], port


def ssh_args(spec) -> list[str]:
    """Destination argv after SSH_BASE: alias or host -> [spec]; user@host:port -> ["-p", port, "user@host"]."""
    user, host, port = _split_spec(spec)
    dest = f"{user}@{host}" if user else host
    return ["-p", str(port), dest] if port else [dest]


def ssh_cmdline(spec, remote="true") -> str:
    """The exact command, as a shell line, for a problem's "tried"."""
    return shlex.join(SSH_BASE + ssh_args(spec) + ([remote] if remote else []))


def docker_url(spec) -> str:
    """docker context endpoint: ssh://alias, ssh://user@host:port, ssh://user@[2001:db8::1]:port."""
    user, host, port = _split_spec(spec)
    return ("ssh://" + (f"{user}@" if user else "") + (f"[{host}]" if ":" in host else host)
            + (f":{port}" if port else ""))


def ssh_resolve(spec, run=subprocess.run) -> dict:
    """`ssh -G` (local, no network) -> {"hostname","user","port","alias"}. alias: a bare name (no @, no :) that ssh
    config rewrites (HostName differs) or that a non-wildcard Host block matched (the -v "Applying options" lines).
    When the ssh config turns host-key checking off for spec (StrictHostKeyChecking no or accept-new, or
    UserKnownHostsFile /dev/null), the result also holds "hostkey": a hostkey problem with the fix. orch's own ssh
    calls stay strict (SSH_BASE), but docker's ssh (the context transport) and ProxyJump hops obey only the config."""
    argv = ["ssh", "-v", "-G"] + ssh_args(spec)
    r = _call(run, argv, timeout=15)
    kv = {}
    for line in r.stdout.splitlines():
        k, _, v = line.strip().partition(" ")
        if k and v.strip():
            kv.setdefault(k.lower(), v.strip())
    if r.returncode != 0 or "hostname" not in kv:
        why = _last_line(r.stderr) or f"exit code {r.returncode}"
        raise DiscoverError(f"ssh could not read its configuration for {spec}: {why}; fix ~/.ssh/config, then retry: "
                            f"{shlex.join(argv)}")
    host = kv["hostname"]
    matched = any(not re.search(r"[*?!]", p) for p in re.findall(r"Applying options for (\S+)", r.stderr))
    bare = "@" not in spec and ":" not in spec
    out = {"hostname": host, "user": kv.get("user") or None, "port": _int(kv.get("port")) or 22,
           "alias": bare and (host.lower() != spec.lower() or matched)}
    loose = _loose_hostkeys(spec, kv, shlex.join(argv))
    if loose:
        out["hostkey"] = loose
    return out


def _loose_hostkeys(spec, kv, tried):
    """`ssh -G` settings -> a hostkey problem when the config accepts unknown or changed host keys, else None."""
    strict = kv.get("stricthostkeychecking", "").lower()
    files = kv.get("userknownhostsfile", "").split()
    lines = []
    if strict in ("false", "no", "off", "accept-new"):
        lines.append(f"StrictHostKeyChecking {strict}")
    if files and all(f.lower() in _NULL_FILES for f in files):
        lines.append(f"UserKnownHostsFile {' '.join(files)}")
    if not lines:
        return None
    _, host, _ = _split_spec(spec)
    fix = "StrictHostKeyChecking yes" + (" / UserKnownHostsFile ~/.ssh/known_hosts"
                                         if any(x.startswith("UserKnownHostsFile") for x in lines) else "")
    risk = ("a new or changed host key would be accepted without asking" if strict in ("false", "no", "off") else
            "a new host key would be accepted without asking" if strict == "accept-new" else
            "no host key is ever kept, so the host is never checked")
    return _problem("hostkey", f"your ssh config sets {' and '.join(lines)} for {spec}, so {risk}",
                    f"add these lines near the top of ~/.ssh/config (above any Host * block): Host {host} / {fix}; "
                    f"then connect once interactively and check the fingerprint: ssh {' '.join(ssh_args(spec))}",
                    tried)


# ----------------------------------------------------------------------------- failure classes
def _problem(cls, what, fix, tried) -> dict:
    return {"class": cls, "what": what, "fix": fix, "tried": tried}


def classify_ssh_error(rc, stderr, spec, user=None, remote="true") -> dict:
    """A failure -> {"class","what","fix","tried"}. ssh's own errors (rc 255) are classified by stderr; docker errors
    from a probe by their text (any rc); anything else is "other" with the last stderr line. tried = the exact command
    that reproduces it: `ssh ... true` for ssh classes, `ssh ... 'docker info'` for docker classes, else `remote`.
    user: the remote account for the docker-group fix (default: the spec's user, else $USER). Host keys are never
    accepted automatically: the fix always sends the user to check the fingerprint."""
    lines = _lines(stderr)
    try:
        dest = " ".join(ssh_args(spec))  # "app1" | "-p 2222 deploy@192.0.2.20"
        login, dtried, otried = ssh_cmdline(spec), ssh_cmdline(spec, "docker info"), ssh_cmdline(spec, remote)
        s_user, s_host, s_port = _split_spec(spec)
    except ValueError:
        dest = login = dtried = otried = str(spec)
        s_user, s_host, s_port = None, str(spec), None

    def hit(rx, flags=0):
        return next((x for x in lines if re.search(rx, x, flags)), None)

    def ssh_what(x):
        return x if x.startswith("ssh:") else f"ssh: {x}"

    if rc == 255:
        x = hit(r"REMOTE HOST IDENTIFICATION HAS CHANGED|host key for \S+ has changed", re.I)
        if x:
            rm = hit(r"ssh-keygen .*-R ")
            rm = rm[rm.index("ssh-keygen"):] if rm else "ssh-keygen -R " + (f"'[{s_host}]:{s_port}'" if s_port
                                                                            else s_host)
            return _problem("hostkey", ssh_what(f"the host key of {spec} has changed ({x.strip('@ ')})"),
                            f"ask the owner of {spec} whether its host key really changed (it can also be an attack); "
                            f"only if it did, remove the old key ({rm}), then connect once interactively and check "
                            f"the fingerprint: ssh {dest}", login)
        x = hit(r"No \S+ host key is known|Host key verification failed")
        if x:
            x = hit(r"No \S+ host key is known") or x
            return _problem("hostkey", ssh_what(x),
                            f"connect once interactively and check the fingerprint: ssh {dest}", login)
        x = hit(r"Permission denied \(|Too many authentication failures")
        if x:
            return _problem("auth", ssh_what(x), f"ssh-copy-id {dest}", login)
        x = hit(r"Could not resolve hostname|Name or service not known|No such host is known|nodename nor servname|"
                r"Temporary failure in name resolution")
        if x:
            return _problem("dns", ssh_what(x),
                            f"the name in {spec} does not resolve from this machine: fix the spelling, add a Host "
                            "block with a HostName to ~/.ssh/config, or use the IP address", login)
        x = hit(r"timed out|Connection refused|No route to host|Network is unreachable|Connection reset|"
                r"Connection closed|kex_exchange_identification|closed by remote host|Broken pipe|not responding", re.I)
        if x:
            m = re.search(r"connect to host (\S+) port (\d+)", x)
            h, p = (m.group(1), m.group(2)) if m else (s_host, str(s_port or 22))
            if re.search(r"closed by remote host|Broken pipe|not responding", x, re.I):
                fix = f"the connection to {h} dropped after login (the host rebooted, or sshd or the network cut " \
                      "it): check the host"
            elif re.search(r"timed out", x, re.I):
                fix = f"check that {h} is up and that port {p} is reachable from this machine (VPN, firewall)"
            elif "refused" in x:
                fix = f"nothing accepts ssh on {h} port {p}: start sshd there or put the right port in the ssh spec " \
                      "(user@host:port)"
            elif re.search(r"No route|unreachable", x):
                fix = f"this machine has no network route to {h}: check the VPN or the network"
            else:
                fix = f"{h} closed the connection before login (sshd MaxStartups, a firewall or fail2ban): wait a " \
                      "minute"
            return _problem("network", ssh_what(x), fix, login)  # tried already holds the command to retry
    u = user or s_user or "$USER"
    x = hit(r"command not found|not recognized|docker: not found", re.I)
    if x:
        return _problem("docker-missing", f"docker is not installed on {spec}: {x}",
                        f"install Docker Engine (Linux) or Docker Desktop (Windows) on {spec}", dtried)
    x = hit(r"permission denied while trying to connect to the docker|access is denied", re.I)
    if x:
        if re.search(r"//\./pipe/|npipe", x):
            fix = f"add {u} to the docker-users group on {spec} (as administrator: net localgroup docker-users {u} " \
                  "/add), then sign out and in again"
        else:
            fix = f"on {spec}: sudo usermod -aG docker {u}, then log in again"
        who = user or s_user or "the ssh user"
        return _problem("docker-permission", f"{who} may not use Docker on {spec}: {x}", fix, dtried)
    x = hit(r"cannot connect to the docker daemon|error during connect|is the docker daemon running|"
            r"did not answer within", re.I)
    if x:
        if "/run/user/" in x:
            fix = f"start rootless Docker on {spec}: systemctl --user start docker"
        elif re.search(r"//\./pipe/|npipe", x):
            fix = f"start Docker Desktop on {spec} and keep it running in that user's session"
        else:
            fix = f"start Docker on {spec}: sudo systemctl start docker"
        return _problem("docker-down", f"Docker is not running on {spec}: {x}", fix, dtried)
    x = lines[-1] if lines else f"exit code {rc}"
    return _problem("other", ssh_what(x) if rc == 255 else x,
                    f"run it yourself to see the full error: {otried}", otried)


# ----------------------------------------------------------------------------- probe scripts
# POSIX sh, read-only, one round trip: KEY=VALUE lines. No set -e and every step wrapped, so one missing tool never
# stops the rest. docker info goes through `timeout` when present (a wedged dockerd must not eat the whole probe);
# memory and disk are printed in kB and multiplied in Python (old mawk prints big numbers in exponent form).
# Endpoint candidates: published TCP host ports of running non-orch containers, at most __MAX__, curled in parallel
# (http, then https), keeping only 2xx/3xx answers.
_PROBE_SH = r"""# orch discovery probe: read-only, prints KEY=VALUE lines
echo orch_probe=1
echo "uname=$(uname -s 2>/dev/null)"
echo "arch=$(uname -m 2>/dev/null)"
echo "user=$(id -un 2>/dev/null)"
echo "cpus=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null)"
if [ -r /proc/meminfo ]; then echo "mem_kb=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null)"
else echo "mem_bytes=$(sysctl -n hw.memsize 2>/dev/null)"; fi
if cat /proc/pressure/memory >/dev/null 2>&1; then echo psi=1; else echo psi=0; fi
[ -f /sys/fs/cgroup/cgroup.controllers ] && echo cgroupfs=2
command -v docker >/dev/null 2>&1 || { echo docker_cli=0; exit 0; }
echo docker_cli=1
T=$(mktemp 2>/dev/null) || T=/tmp/.orch-probe.$$
trap 'rm -f "$T"' EXIT
TO=; command -v timeout >/dev/null 2>&1 && TO="timeout 20"
di() {
  if [ -n "$1" ]; then out=$($TO docker -H "$1" info --format '{{json .}}' 2>"$T")
  else out=$($TO docker info --format '{{json .}}' 2>"$T"); fi
  rc=$?
  case $out in  # docker 23+ exits 0 with an empty ServerVersion when the daemon is unreachable
  *'"ServerVersion":""'*|*'"ServerVersion": ""'*) ;;
  *'"ServerVersion":"'*|*'"ServerVersion": "'*) return 0 ;;
  esac
  e=$(grep . "$T" 2>/dev/null | tail -n 1)
  [ "$rc" = 124 ] && e="docker info did not answer within 20 s"
  [ -n "$e" ] || e="docker info gave no server version (exit code $rc)"
  return 1
}
S=
if di ""; then S=default
else
  e0=$e
  for s in unix:///var/run/docker.sock "unix:///run/user/$(id -u 2>/dev/null)/docker.sock"; do
    if di "$s"; then S=$s; break; fi
  done
  echo "docker_err=$e0"
fi
echo "docker_socket=${S:-none}"
[ -n "$S" ] || exit 0
printf 'docker_info=%s\n' "$out"
dk() { if [ "$S" = default ]; then $TO docker "$@"; else $TO docker -H "$S" "$@"; fi; }
r=$(printf '%s\n' "$out" | sed -n 's/.*"DockerRootDir": *"\([^"]*\)".*/\1/p')
[ -n "$r" ] && echo "disk_free_kb=$(df -Pk "$r" 2>/dev/null | awk 'NR==2{print $4}')"
if $TO docker buildx version >/dev/null 2>&1; then echo buildx=1; else echo buildx=0; fi
for k in container volume network; do
  case $k in container) a='ps -a'; f='{{.Names}}' ;; volume) a='volume ls'; f='{{.Name}}' ;; *) a='network ls'; f='{{.Name}}' ;; esac
  dk $a --filter 'name=__NSP__' --format "$f\t{{.Labels}}" 2>/dev/null | head -n 100 | awk -v k=$k '{print "ns=" k "\t" $0}'
done
C=$(dk ps --format '__FMT__' 2>/dev/null | awk -F '\t' -v P='__PFX__' 'BEGIN{n=split(P,p," ")}
  {for(i=1;i<=n;i++) if(index($1,p[i])==1) next; if($4=="") print $1 "\t" $2 "\t" $3}')
[ -n "$C" ] || exit 0
printf '%s\n' "$C" | sed 's/^/ctr=/'
command -v curl >/dev/null 2>&1 || exit 0
printf '%s\n' "$C" | awk -F '\t' -v K=' __SKIP__ ' '{n=split($3,a,", "); for(i=1;i<=n;i++){x=a[i]
  if(x !~ /->/ || x !~ /\/tcp$/) continue
  h=substr(x,1,index(x,"->")-1); sub(/.*:/,"",h); sub(/-.*/,"",h)
  if(h ~ /^[0-9]+$/ && !index(K," " h " ") && !s[h]++) print h, $1}}' | sort -n | head -n __MAX__ | {
  while read -r p n; do
    ( for s in http https; do
        r=$(curl -sk -o /dev/null -w '%{http_code} %{time_total}' --max-time 3 "$s://127.0.0.1:$p/" 2>/dev/null)
        case $r in [23][0-9][0-9]\ *) echo "ep=$p $s $r $n"; break ;; esac
      done ) &
  done
  wait
}
"""

# Windows helper (Docker Desktop). Child processes go through ProcessStartInfo: exact argument strings (PowerShell
# 5.1 mangles embedded quotes in native arguments) and separate stdout/stderr. Function names are not one letter:
# aliases (r, h) win over functions. The docker ps JSON lines go back whole and are filtered in Python.
_PROBE_PS = r"""$ProgressPreference='SilentlyContinue'
function oStart($f,$a){if(!$f){return $null};$i=New-Object Diagnostics.ProcessStartInfo $f,$a;$i.UseShellExecute=$false;$i.RedirectStandardOutput=$true;$i.RedirectStandardError=$true;$i.CreateNoWindow=$true;try{[Diagnostics.Process]::Start($i)}catch{$null}}
function oWait($p){if(!$p){return @('','could not start',127)};$e=$p.StandardError.ReadToEndAsync();$o=$p.StandardOutput.ReadToEnd();$p.WaitForExit();@($o,$e.Result,$p.ExitCode)}
function oFind($n){(Get-Command $n -CommandType Application -ErrorAction SilentlyContinue|Select-Object -First 1).Source}
'orch_probe=1';'uname=windows';"arch=$env:PROCESSOR_ARCHITECTURE";"user=$env:USERNAME";'psi=0'
try{$c=Get-CimInstance Win32_ComputerSystem;"cpus=$($c.NumberOfLogicalProcessors)";"mem_bytes=$($c.TotalPhysicalMemory)"}catch{}
try{"disk_free_bytes=$((Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$env:SystemDrive'").FreeSpace)"}catch{}
$D=oFind docker
if(!$D){'docker_cli=0';exit 0}
'docker_cli=1'
$r=oWait (oStart $D 'info --format "{{json .}}"')
if($r[0] -notmatch '"ServerVersion":\s*"[^"]'){'docker_socket=none';"docker_err=$(@($r[1] -split "`n"|Where-Object{$_.Trim()})[-1])";exit 0}
'docker_socket=default';"docker_info=$($r[0].Trim())"
$b=oWait (oStart $D 'buildx version');"buildx=$([int]($b[2] -eq 0))"
foreach($q in 'container|ps -a|Names','volume|volume ls|Name','network|network ls|Name'){$k,$c,$f=$q -split '\|';foreach($l in (oWait (oStart $D ($c+' --filter name=__NSP__ --format "{{.'+$f+'}}\t{{.Labels}}"')))[0] -split "`n"){$l=$l.Trim();if($l){"ns=$k`t$l"}}}
$P=@{}
foreach($l in (oWait (oStart $D 'ps --format "{{json .}}"'))[0] -split "`n"){$l=$l.Trim();if(!$l){continue};"ctrj=$l"
try{$j=$l|ConvertFrom-Json}catch{continue};$g=",$($j.Labels),";if(__OURS__){continue}
foreach($m in [regex]::Matches("$($j.Ports)",':(\d+)(?:-\d+)?->\d+(?:-\d+)?/tcp')){$k=[int]$m.Groups[1].Value;if(@(__SKIP__) -notcontains $k -and !$P.ContainsKey($k)){$P[$k]=$j.Names}}}
$U=oFind curl
if(!$U -or !$P.Count){exit 0}
$L=@($P.Keys|Sort-Object|Select-Object -First __MAX__)
foreach($s in 'http','https'){$q=@{};foreach($k in $L){$q[$k]=oStart $U "-sk -o NUL -w ""%{http_code} %{time_total}"" --max-time 3 ${s}://127.0.0.1:$k/"}
$n=@();foreach($k in $L){$o=(oWait $q[$k])[0].Trim();if($o -match '^[23]\d\d '){"ep=$k $s $o $($P[$k])"}else{$n+=$k}};$L=$n}
"""


def probe_script_sh() -> str:
    labels, prefixes = _namespaces()
    fmt = r"{{.Names}}\t{{.Image}}\t{{.Ports}}\t" + "".join('{{.Label "%s"}}' % x for x in labels)
    return (_PROBE_SH.replace("__FMT__", fmt).replace("__PFX__", " ".join(prefixes))
            .replace("__NSP__", own_namespace()[1])
            .replace("__SKIP__", " ".join(map(str, NON_HTTP_PORTS))).replace("__MAX__", str(ENDPOINT_MAX)))


def probe_script_ps() -> str:
    labels, prefixes = _namespaces()
    ours = " -or ".join([f"$j.Names -like '{p}*'" for p in prefixes] + [f"$g -like '*,{x}=1,*'" for x in labels])
    return (_PROBE_PS.replace("__OURS__", ours).replace("__NSP__", own_namespace()[1])
            .replace("__SKIP__", ",".join(map(str, NON_HTTP_PORTS))).replace("__MAX__", str(ENDPOINT_MAX)))


def probe_command_ps() -> str:
    """The remote command for the PowerShell probe (works under cmd.exe, PowerShell or a POSIX login shell)."""
    return PS_PREFIX + base64.b64encode(probe_script_ps().encode("utf-16-le")).decode()


# ----------------------------------------------------------------------------- probe output -> facts
def _kv(text) -> dict:
    """KEY=VALUE lines -> {key: [values]} (ctr, ctrj and ep repeat); anything else is ignored. \\r from Windows."""
    d = {}
    for line in _dec(text).splitlines():
        k, sep, v = line.rstrip("\r").partition("=")
        if sep and re.fullmatch(r"[a-z_]+", k):
            d.setdefault(k, []).append(v)
    return d


def _one(kv, k) -> str:
    return (kv.get(k) or [""])[0].strip()


def _ours(name: str, labels: str = "") -> bool:
    lab, pfx = _namespaces()
    if any(name.startswith(p) for p in pfx):
        return True
    have = dict(x.partition("=")[::2] for x in labels.split(",")) if labels else {}
    return any(have.get(x) == "1" for x in lab)


def _host_ports(ports: str) -> list[int]:
    """docker ps Ports text -> published TCP host ports. '0.0.0.0:8080->80/tcp, :::8080->80/tcp' -> [8080];
    a range '9100-9101->9100-9101/tcp' expands (at most 16 per range); UDP and unpublished ports are left out."""
    out = set()
    for m in re.finditer(r":(\d+)(?:-(\d+))?->\d+(?:-\d+)?/tcp\b", ports or ""):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        out.update(range(a, min(b, a + 15) + 1))
    return sorted(p for p in out if 0 < p < 65536)


def _arch(a: str):
    a = (a or "").strip().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(a, a or None)


def _os_of(kv):
    u = _one(kv, "uname").lower()
    return "windows" if u.startswith(("windows", "mingw", "msys", "cygwin")) else (u or None)


def _facts(kv, os_name=None) -> dict:
    one = lambda k: _one(kv, k)  # noqa: E731
    os_name = os_name or _os_of(kv)
    try:
        info = json.loads(one("docker_info") or "{}")
    except ValueError:
        info = {}
    info = info if isinstance(info, dict) else {}

    def size(b, kb):
        v = _int(one(b))
        if v is None and _int(one(kb)) is not None:
            v = _int(one(kb)) * 1024
        return v

    cot = {}
    for v in kv.get("ctr", []):  # sh: name \t image \t ports (labelled orch containers already dropped by awk)
        f = (v.split("\t") + ["", ""])[:3]
        name = _clean(f[0])
        if name and not _ours(name):
            cot.setdefault(name, {"name": name, "image": _clean(f[1]), "ports": _host_ports(f[2])})
    for v in kv.get("ctrj", []):  # PowerShell: one docker ps JSON object per line
        try:
            j = json.loads(v)
        except ValueError:
            continue
        name = _clean(str(j.get("Names") or "")) if isinstance(j, dict) else ""
        if name and not _ours(name, str(j.get("Labels") or "")):
            cot.setdefault(name, {"name": name, "image": _clean(str(j.get("Image") or "")),
                                  "ports": _host_ports(str(j.get("Ports") or ""))})
    eps = {}
    for v in kv.get("ep", []):  # port scheme code seconds container
        f = v.split(None, 4)
        if (len(f) == 5 and re.fullmatch(r"[0-9]{1,5}", f[0]) and 0 < int(f[0]) < 65536 and f[1] in ("http", "https")
                and re.fullmatch(r"[23]\d\d", f[2]) and _clean(f[4]) and not _ours(_clean(f[4]))):
            p = int(f[0])
            eps.setdefault(p, {"name": f"{_clean(f[4])} :{p}", "url": f"{f[1]}://127.0.0.1:{p}/",
                               "insecure": f[1] == "https"})
    sock = one("docker_socket")
    return {"os": os_name, "arch": _arch(one("arch")), "cpus": _int(one("cpus")),
            "mem_bytes": size("mem_bytes", "mem_kb"), "disk_free_bytes": size("disk_free_bytes", "disk_free_kb"),
            "docker": str(info.get("ServerVersion") or "") or None, "docker_id": str(info.get("ID") or "") or None,
            "rootless": any("name=rootless" in str(x) for x in info.get("SecurityOptions") or []),
            "buildx": one("buildx") == "1", "psi": one("psi") == "1",
            "cgroup": _int(info.get("CgroupVersion")) or _int(one("cgroupfs")),
            "socket": sock if sock and sock != "none" else None,
            "cotenants": [cot[k] for k in sorted(cot)], "endpoints": [eps[p] for p in sorted(eps)][:ENDPOINT_MAX]}


def parse_probe_sh(text) -> dict:
    """POSIX probe output -> the "facts" dict of a hosts.json record (os "windows" when sh ran under MSYS/Cygwin)."""
    return _facts(_kv(text))


def parse_probe_ps(text) -> dict:
    """PowerShell probe output -> the "facts" dict of a hosts.json record."""
    return _facts(_kv(text), "windows")


# ----------------------------------------------------------------------------- probing
def _probe(spec, run, timeout, user):
    """-> (kv, None) or (None, problem). sh first; PowerShell when the sh attempt shows a non-POSIX shell (no marker
    and not an ssh failure) or sh ran under MSYS on a Windows host (its facts are kept if PowerShell fails)."""
    base = SSH_BASE + ssh_args(spec)
    r = _call(run, base + ["sh -s"], input=probe_script_sh(), timeout=timeout)
    kv = _kv(r.stdout)
    if "orch_probe" in kv and _os_of(kv) != "windows":
        return kv, None
    if "orch_probe" not in kv and r.returncode == 255:
        return None, classify_ssh_error(255, r.stderr, spec, user=user)
    r2 = _call(run, base + [probe_command_ps()], timeout=timeout)
    kv2 = _kv(r2.stdout)
    if "orch_probe" in kv2:
        return kv2, None
    if "orch_probe" in kv:
        return kv, None
    if r2.returncode == 255:
        return None, classify_ssh_error(255, r2.stderr, spec, user=user)
    first = _last_line(r.stderr) or _last_line(r.stdout) or f"exit code {r.returncode}"
    second = _last_line(r2.stderr) or _last_line(r2.stdout) or f"exit code {r2.returncode}"
    return None, _problem("other", f"the probe did not run on {spec}: sh said {first!r}, powershell said {second!r}",
                          f"make sure the login shell of the ssh user on {spec} can run sh (Linux) or powershell "
                          f"(Windows)", ssh_cmdline(spec, "sh -s"))


def probe_host(name, spec, docker=True, run=subprocess.run, timeout=40) -> dict:
    """ONE host -> its state/hosts.json record minus transport: {"name","ok","probed","spec","resolved","facts",
    "problem"}. docker=False (production-only hosts): resolve + `ssh ... true` only, facts {}. ok = reachable and,
    when docker=True, a working Docker without another orch namespace's objects under this master's prefix (problem
    class foreign-namespace, facts kept). Never raises for a host problem; it lands in "problem"."""
    rec = {"name": name, "ok": False, "probed": _now(), "spec": spec, "resolved": None, "facts": {}, "problem": None}
    try:
        res = rec["resolved"] = ssh_resolve(spec, run=run)
        loose = res.pop("hostkey", None)
        if loose and docker:  # a helper's docker context runs docker's own ssh, which obeys only the config
            rec["problem"] = loose
            return rec
        if not docker:
            r = _call(run, SSH_BASE + ssh_args(spec) + ["true"], timeout=timeout)
            rec["ok"] = r.returncode == 0
            if not rec["ok"]:
                rec["problem"] = classify_ssh_error(r.returncode, r.stderr, spec, user=res["user"])
            return rec
        kv, rec["problem"] = _probe(spec, run, timeout, res["user"])
        if kv is None:
            return rec
        facts = rec["facts"] = _facts(kv)
        u = _one(kv, "user")  # the account docker runs as there; it goes into a fix command, so only a plain name
        u = u if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", u) else res["user"]
        if _one(kv, "docker_cli") == "0":
            rec["problem"] = classify_ssh_error(127, "docker: command not found", spec, user=u)
        elif not facts["docker"]:
            rec["problem"] = classify_ssh_error(1, _one(kv, "docker_err") or "docker info returned no server version",
                                                spec, user=u, remote="docker info")
        else:
            label, prefix = own_namespace()
            fo = foreign_objects([v.split("\t", 2) for v in kv.get("ns", [])], prefix, label)
            if fo:  # Docker works, but another orch's leftovers carry our names: not ready until they are gone
                rec["problem"] = foreign_problem(fo, prefix, ssh_cmdline(spec, f"docker ps -a --filter name={prefix}"))
            else:
                rec["ok"] = True
    except ValueError as e:  # a bad spec: orch_config.parse_spec normally rejects it before this point
        rec["problem"] = _problem("other", str(e), f"run orch init and type the ssh spec again (now {spec!r})",
                                  f"ssh -G {spec}")
    except DiscoverError as e:
        if e.kind == "missing":
            tried, fix = "ssh -V", "install the OpenSSH client on this machine (Windows: Settings > Optional " \
                                   "features > OpenSSH Client; Linux: the openssh-client package), then retry"
        elif rec["resolved"] is None:
            tried = shlex.join(["ssh", "-G"] + ssh_args(spec))
            fix = "fix ~/.ssh/config (ssh -G must finish at once; check Match exec lines)"
        else:
            tried = ssh_cmdline(spec, "docker info" if docker else "true")
            fix = (f"check that {spec} answers (machine load, a hung Docker daemon)"
                   if e.kind == "timeout" else f"run it yourself to see the full error: {tried}")
        rec["problem"] = _problem("other", str(e), fix, tried)
    return rec


def probe_hosts(specs: dict, docker_names: set, run=subprocess.run, workers=8) -> dict:
    """{name: spec} -> {name: record}, all hosts in parallel (wall time = the slowest host). Hosts named in
    docker_names get the full probe, the rest (production-only) resolve + ssh only. One host failing, even inside
    orch, never loses the others' records."""
    def safe(name, spec):
        try:
            return probe_host(name, spec, docker=name in docker_names, run=run)
        except Exception as e:  # noqa: BLE001 - a bug for one host must not hide every other host
            return {"name": name, "ok": False, "probed": _now(), "spec": spec, "resolved": None, "facts": {},
                    "problem": _problem("other", f"the probe of {spec} failed inside orch: {type(e).__name__}: {e}",
                                        "report this as an orch bug, with the message", f"probe {spec}")}

    if not specs:
        return {}
    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(specs)))) as ex:
        futs = {name: ex.submit(safe, name, spec) for name, spec in specs.items()}
        return {name: f.result() for name, f in futs.items()}


# ----------------------------------------------------------------------------- master + transport
def master_facts(run=subprocess.run) -> dict:
    """This machine: {"os","python","git": bool,"docker_cli": client version|None} (`docker --version` needs no
    daemon)."""
    def out(argv):
        try:
            r = _call(run, argv, timeout=20)
        except DiscoverError:
            return None
        return r.stdout if r.returncode == 0 else None

    m = re.search(r"version\s+v?([0-9][^\s,]*)", out(["docker", "--version"]) or "")
    return {"os": "windows" if os.name == "nt" else platform.system().lower(), "python": platform.python_version(),
            "git": out(["git", "--version"]) is not None, "docker_cli": m.group(1) if m else None}


def context_name(name) -> str:
    if not isinstance(name, str) or not _NODE.fullmatch(name):
        raise ValueError(f"bad node name {name!r}: use letters, digits, dots and dashes (e.g. h11)")
    return CONTEXT_PREFIX + name


def choose_transport(rec, master, name=None) -> dict:
    """Probe record + master_facts -> {"transport","context","docker_host"} or {"problem": {...}} (design section 5).
    Master with a docker CLI -> docker context "orch-<name>" (Linux and Windows helpers). A Linux helper whose
    default socket failed but an explicit one worked -> "ssh-docker" with that docker_host (a context cannot pick
    the remote socket). No docker CLI here -> "ssh-docker" for Linux (docker_host None = the remote default; image
    builds need a context), and Windows helpers are unusable (windows-no-cli). name defaults to rec["name"]."""
    if not rec.get("ok"):
        return {"problem": rec.get("problem") or _problem("other", f"{rec.get('spec')} has not been probed",
                                                          "run orch init to probe it", "-")}
    f = rec.get("facts") or {}
    if not f:  # production-only host: probed without docker, no transport
        return {"transport": None, "context": None, "docker_host": None}
    ctx = context_name(name or rec.get("name"))
    if f.get("os") == "windows":
        if master.get("docker_cli"):
            return {"transport": "context", "context": ctx, "docker_host": None}
        return {"problem": _problem("windows-no-cli",
                                    f"{rec.get('spec')} is a Windows helper, reachable only through a docker context, "
                                    "and this machine has no docker CLI",
                                    "install the Docker CLI on this machine (Docker Desktop, or the static docker "
                                    "client binary), then run orch init again", "docker --version")}
    if f.get("os") != "linux":
        return {"problem": _problem("other", f"{rec.get('spec')} runs {f.get('os') or 'an unknown OS'}, and orch "
                                             "helpers must be Linux or Windows",
                                    f"use a Linux or Windows machine as a helper, or remove {rec.get('spec')} with "
                                    "orch init", ssh_cmdline(rec["spec"], "uname -s"))}
    sock = f.get("socket")
    if sock and sock != "default":
        return {"transport": "ssh-docker", "context": None, "docker_host": sock}
    if master.get("docker_cli"):
        return {"transport": "context", "context": ctx, "docker_host": None}
    return {"transport": "ssh-docker", "context": None, "docker_host": None}


def _context_host(text):
    try:
        return json.loads(text)[0]["Endpoints"]["docker"]["Host"]
    except (ValueError, LookupError, TypeError):
        return None


def endpoint_context(name, spec) -> str:
    """The context for spec when "orch-<name>" already points at another endpoint (the same machine reached as
    another ssh user, by another project): "orch-<name>-<first 8 hex of sha256(endpoint)>"."""
    return f"{context_name(name)}-{hashlib.sha256(docker_url(spec).encode('utf-8')).hexdigest()[:8]}"


def _set_context(run, env, verb, ctx, name, spec):
    argv = ["docker", "context", verb, ctx, "--description", f"orch helper {name} ({spec})", "--docker",
            f"host={docker_url(spec)}"]
    r = _call(run, argv, timeout=30, env=env)
    if r.returncode != 0:
        raise DiscoverError(f"docker could not {verb} context {ctx}: {_last_line(r.stderr) or r.returncode}; run it "
                            f"yourself to see the full error: {shlex.join(argv)}")
    return ctx


def ensure_context(name, spec, run=subprocess.run, env=None, own=()) -> str:
    """The docker context that reaches spec, created when missing -> its name: "orch-<name>", or endpoint_context()
    when "orch-<name>" points elsewhere. A context that points at another endpoint is updated only when it is in own
    (the caller's previous context for this host, used by no other project); otherwise it belongs to someone else and
    is never changed. env goes to every docker call unchanged (DOCKER_CONFIG isolates tests). Raises DiscoverError."""
    host = docker_url(spec)
    for ctx in (context_name(name), endpoint_context(name, spec)):
        r = _call(run, ["docker", "context", "inspect", ctx], timeout=30, env=env)
        if r.returncode != 0:
            return _set_context(run, env, "create", ctx, name, spec)
        if _context_host(r.stdout) == host:
            return ctx
        if ctx in own:
            return _set_context(run, env, "update", ctx, name, spec)
    raise DiscoverError(f"docker contexts {context_name(name)} and {endpoint_context(name, spec)} both point at "
                        f"other endpoints, so orch cannot reach {spec} through either; check them with docker context "
                        f"ls and remove the one no project uses (docker context rm <name>), then run orch init again")


def remove_context(name, run=subprocess.run, env=None, context=None) -> None:
    """Remove docker context `context` (default "orch-<name>"); already gone is fine. Raises ValueError for a name
    that is not an orch context and DiscoverError on any other failure."""
    ctx = context or context_name(name)
    if not isinstance(ctx, str) or not _CONTEXT.fullmatch(ctx):
        raise ValueError(f"{ctx!r} is not an orch docker context (orch-<name>)")
    argv = ["docker", "context", "rm", "--force", ctx]
    r = _call(run, argv, timeout=30, env=env)
    if r.returncode != 0 and not re.search(r"not found|does not exist|no such", r.stderr, re.I):
        raise DiscoverError(f"docker could not remove context {argv[-1]}: {_last_line(r.stderr) or r.returncode}; "
                            f"run it yourself to see the full error: {shlex.join(argv)}")
