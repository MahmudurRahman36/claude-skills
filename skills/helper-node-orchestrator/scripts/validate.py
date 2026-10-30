#!/usr/bin/env python3
"""PASS/FAIL checker for the helper-node-orchestrator skill package.

Usage: validate.py [ROOT] [--dir DIR] [--denylist FILE]

ROOT (default: the folder above scripts/) is checked as a skill package: SKILL.md frontmatter, size and
headings, relative links, agents/openai.yaml, and a zero-site-data scan of every package file (name and
content). The package is what `git archive` ships: `git ls-files` (tracked plus untracked, not ignored)
minus export-ignore paths from .gitattributes. Outside a git checkout, and always with --dir DIR (an
unpacked or installed package), it is every file under the folder except .git and Python caches.
The scan fails on private IPv4 addresses (RFC 1918; the job subnet 10.252.20.0/24 is allowed), drive-letter
paths (C:/work, C:/srv, C:/tmp, Windows system folders and placeholder user names allowed), paths under /home or /Users, phone numbers, and every
string in the denylist: one per line, case-insensitive, "word:" prefix = whole word only, "#" = comment.
Default denylist: dev/site-denylist.txt under ROOT or under this script's own package, when present; the
denylist file itself is never scanned.
Exit 0 PASS, 1 FAIL (one line per problem), 2 usage error.
"""
import argparse
import ipaddress
import os
import posixpath
import re
import subprocess
import sys
from urllib.parse import unquote

SECTIONS = ["## Start", "## Core", "## Where work runs", "## Run", "## Results", "## Tokens", "## Stop", "## More"]
MAX_LINES, MAX_BYTES, MAX_DESC = 130, 9500, 1024
MAX_HITS = 5  # per file; the rest is summarised in one line
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
DENYLIST = os.path.join("dev", "site-denylist.txt")
SKIP_DIRS = {".git", "__pycache__"}  # walk mode: VCS data and Python's own caches are never package content
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)")
# RFC 1918 (10/8, 172.16/12, 192.168/16), built from numbers so this file passes its own scan
PRIVATE = [ipaddress.ip_network((a << 24 | b << 16, bits)) for a, b, bits in ((10, 0, 8), (172, 16, 12), (192, 168, 16))]
JOB_SUBNET = ipaddress.ip_network("10.252.20.0/24")  # the generic default; documentation ranges are not private
# A drive letter starts a path only after a non-word, non-escape character: "$s://host" and "\d:\d" are not paths.
DRIVE = re.compile(r"(?<![A-Za-z0-9$%\\])[A-Za-z]:[\\/][^\s\"'`<>|]*")
END = r"(?=[\\/\s\"'`).,;:]|$)"
PLACEHOLDER = r"(?:u|m|x|dev|user|you|me|example|someone|name)" + END  # generic user names in examples
DRIVE_OK = re.compile(r"C:[\\/]+(?:(?:work|srv|tmp|temp|x|\.\.\.|Program Files(?: \(x86\))?|ProgramData|Windows)" + END
                      + r"|Users[\\/]+" + PLACEHOLDER + ")", re.I)
# a Git Bash drive prefix counts too; placeholder names (/home/u, /home/dev) and regex text (/home/[a-z]) pass
HOME = re.compile(r"(?<![\w.~:-])/(?:[A-Za-z]/)?(?:home|Users)/(?!" + PLACEHOLDER + r")(?=\w)[^\s\"'`<>|]*")
PHONE = re.compile(r"(?<![\w+])\+\d(?:[ -]?\d){9,14}(?!\d)")


class UsageError(Exception):
    pass


def frontmatter(text):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip("\"'")
    return fm


def load_denylist(path):
    """[(line number, regex)] for every entry of a denylist file."""
    out = []
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            s = raw.strip()
            word = s[:5].lower() == "word:"
            s = s[5:].strip() if word else s
            if not s or (s.startswith("#") and not word):
                continue
            pat = re.escape(s.replace("\\", "/")).replace("/", r"[\\/]+")  # either slash matches
            if word and re.match(r"\w", s[0]):
                pat = r"(?<![A-Za-z0-9_])" + pat
            if word and re.match(r"\w", s[-1]):
                pat += r"(?![A-Za-z0-9_])"
            out.append((n, re.compile(pat, re.I)))
    return out


def scan(text, deny=()):
    """(line, problem) for every piece of site data in text."""
    def at(i):
        return text.count("\n", 0, i) + 1

    def short(s):
        return s if len(s) <= 60 else s[:57] + "..."
    for m in IPV4.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group())
        except ValueError:
            continue  # an octet over 255 or a leading zero: a version string, not an address
        if any(ip in n for n in PRIVATE) and ip not in JOB_SUBNET:
            yield at(m.start()), ("private IPv4 address %s; use a documentation address "
                                  "(192.0.2.x, 198.51.100.x or 203.0.113.x)" % ip)
    for rx in (DRIVE, HOME):
        for m in rx.finditer(text):
            if rx is DRIVE and DRIVE_OK.match(text, m.start()):
                continue
            yield at(m.start()), ("absolute path %s; write ~/... or a generic example such as "
                                  "C:/work/shop or /srv/app" % short(m.group()))
    for m in PHONE.finditer(text):
        yield at(m.start()), "phone number %s; remove it" % m.group()
    for n, rx in deny:
        for m in rx.finditer(text):
            yield at(m.start()), ("site string from denylist line %d; replace it with a generic name "
                                  "such as h11, app1 or shop" % n)


def _git(root, run, *args, data=None):
    return run(["git", "-C", root] + list(args), input=data, capture_output=True, text=True,
               encoding="utf-8", errors="replace")


def git_files(root, run=subprocess.run):
    """Files `git archive` would ship from the working tree, or None when root is not in a git checkout."""
    try:
        r = _git(root, run, "rev-parse", "--is-inside-work-tree")
    except OSError:
        return None  # no git on this machine
    if r.returncode != 0 or r.stdout.strip() != "true":
        return None
    r = _git(root, run, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if r.returncode != 0:
        raise UsageError("git ls-files failed in %s (%s); run it there by hand to see why." % (root, r.stderr.strip()))
    files = sorted({p for p in r.stdout.split("\0") if p and not p.endswith("/")})
    if not files:
        return []
    # export-ignore on a folder ("dev/") matches only the folder itself, queried with a trailing slash;
    # `git archive` then skips everything below it, so ancestors are checked too.
    dirs = sorted({p[:i + 1] for p in files for i, c in enumerate(p) if c == "/"})
    r = _git(root, run, "check-attr", "-z", "--stdin", "export-ignore", data="\0".join(files + dirs) + "\0")
    if r.returncode != 0:
        raise UsageError("git check-attr failed in %s (%s); check .gitattributes." % (root, r.stderr.strip()))
    f = r.stdout.split("\0")
    ignored = {f[i] for i in range(0, len(f) - 2, 3) if f[i + 2] == "set"}
    keep = [p for p in files
            if p not in ignored and not any(p[:i + 1] in ignored for i, c in enumerate(p) if c == "/")]
    return [p for p in keep if os.path.isfile(os.path.join(root, p))]


def walk_files(root):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
        for fn in sorted(fns):
            if fn != ".git" and not fn.endswith(".pyc"):
                out.append(os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/"))
    return out


def read(root, rel):
    with open(os.path.join(root, rel), "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def check_skill(root, files, git_mode, errs):
    if not os.path.isfile(os.path.join(root, "SKILL.md")):
        errs.append("SKILL.md is missing; create it at the package root.")
        return
    if "SKILL.md" not in files:
        errs.append("SKILL.md is ignored by git or marked export-ignore, so it would not ship; "
                    "fix .gitignore or .gitattributes.")
    with open(os.path.join(root, "SKILL.md"), "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    fm = frontmatter(text)
    if fm is None:
        errs.append("SKILL.md has no frontmatter; start it with a --- block holding name, description "
                    "and allowed-tools.")
        fm = {}
    name = fm.get("name", "")
    folder = os.path.basename(os.path.normpath(root))
    if not NAME_RE.match(name) or len(name) > 64:
        errs.append("SKILL.md name %r is not a valid skill name; use up to 64 lowercase letters, digits "
                    "and hyphens." % name)
    elif not git_mode and name != folder:  # a git checkout may live in any folder; an install may not
        errs.append("SKILL.md name %r does not match the folder name %r; install the package into a "
                    "folder named %s." % (name, folder, name))
    desc = fm.get("description", "")
    if not desc:
        errs.append("SKILL.md description is missing; add one that starts with 'Use when'.")
    else:
        if not desc.startswith("Use when"):
            errs.append("SKILL.md description must start with 'Use when'; reword its opening.")
        if len(desc) > MAX_DESC:
            errs.append("SKILL.md description has %d characters, over the %d limit; shorten it."
                        % (len(desc), MAX_DESC))
    if "scripts/orch" not in fm.get("allowed-tools", ""):
        errs.append("SKILL.md allowed-tools does not mention scripts/orch; add "
                    "Bash(bash ${CLAUDE_SKILL_DIR}/scripts/orch *).")
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    if lines > MAX_LINES:
        errs.append("SKILL.md has %d lines, over the %d limit; move detail to references/." % (lines, MAX_LINES))
    if len(raw) > MAX_BYTES:
        errs.append("SKILL.md has %d bytes, over the %d limit; move detail to references/." % (len(raw), MAX_BYTES))
    heads = set(l.rstrip() for l in text.splitlines() if l.startswith("## "))
    for s in SECTIONS:
        if s not in heads:
            errs.append("SKILL.md is missing the heading '%s'; add it." % s)


def check_links(root, files, errs):
    known = set(files) | {p[:i] for p in files for i, c in enumerate(p) if c == "/"}  # files and their folders
    for rel in files:
        if not rel.endswith(".md"):
            continue
        text = re.sub(r"```.*?```", "", read(root, rel), flags=re.S)
        for target in LINK.findall(text):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                continue
            path = unquote(target.split("#", 1)[0])
            if path and posixpath.normpath(posixpath.join(posixpath.dirname(rel), path)) not in known:
                errs.append("broken link in %s: %s; point it at a file that ships in the package." % (rel, target))


def check_openai(root, errs):
    oy = os.path.join(root, "agents", "openai.yaml")
    if not os.path.isfile(oy):
        errs.append("agents/openai.yaml is missing; add it with interface.short_description.")
        return
    with open(oy, encoding="utf-8") as f:
        m = re.search(r"^\s*short_description:\s*(.*)$", f.read(), re.M)
    sd = m.group(1).strip().strip("\"'") if m else ""
    if not 25 <= len(sd) <= 64:
        errs.append("agents/openai.yaml short_description has %d characters; use 25 to 64." % len(sd))


def check_site_data(root, files, deny, skip, errs):
    for rel in files:
        if os.path.normcase(os.path.realpath(os.path.join(root, rel))) == skip:
            continue
        hits = [("%s (file name)" % rel, msg) for _, msg in scan(rel, deny)]
        hits += [("%s:%d" % (rel, n), msg) for n, msg in sorted(scan(read(root, rel), deny), key=lambda h: h[0])]
        errs.extend("%s: %s." % h for h in hits[:MAX_HITS])
        if len(hits) > MAX_HITS:
            errs.append("%s: %d more site-data hits; fix the ones above, then rerun." % (rel, len(hits) - MAX_HITS))


def check(root, walk=False, denylist=None, run=subprocess.run):
    """(problems, info) for the package at root; problems == [] means PASS."""
    root = os.path.abspath(root)
    files = None if walk else git_files(root, run)
    git_mode = files is not None
    if files is None:
        files = walk_files(root)
    if denylist is None:
        denylist = next((p for p in (os.path.join(b, DENYLIST) for b in (root, SKILL_DIR)) if os.path.isfile(p)), None)
    elif not os.path.isfile(denylist):
        raise UsageError("denylist file %s not found; pass an existing file or drop --denylist." % denylist)
    deny = load_denylist(denylist) if denylist else []
    skip = os.path.normcase(os.path.realpath(denylist)) if denylist else None
    errs = []
    check_skill(root, files, git_mode, errs)
    check_links(root, files, errs)
    check_openai(root, errs)
    check_site_data(root, files, deny, skip, errs)
    return errs, {"files": len(files), "denylist": denylist, "entries": len(deny), "git": git_mode}


def validate(root, walk=False, denylist=None, run=subprocess.run):
    return check(root, walk, denylist, run)[0]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="validate.py", description="Check the helper-node-orchestrator package.")
    ap.add_argument("root", nargs="?", default=SKILL_DIR, help="package folder (default: this script's package)")
    ap.add_argument("--dir", help="check DIR as an unpacked or installed package: every file, no git")
    ap.add_argument("--denylist", help="site strings that must never ship (default dev/site-denylist.txt when present)")
    a = ap.parse_args(argv)
    root = a.dir or a.root
    if not os.path.isdir(root):
        print("%s is not a folder; pass the package folder." % root, file=sys.stderr)
        return 2
    try:
        errs, info = check(root, walk=bool(a.dir), denylist=a.denylist)
    except UsageError as e:
        print(e, file=sys.stderr)
        return 2
    dl = ("denylist %s (%d entries)" % (info["denylist"], info["entries"]) if info["denylist"]
          else "no denylist, so site strings were not checked (pass --denylist FILE)")
    src = "git package list" if info["git"] else "every file"
    if errs:
        print("FAIL: %d problem%s in %d files (%s; %s)."
              % (len(errs), "" if len(errs) == 1 else "s", info["files"], src, dl))
        for e in errs:
            print("- " + e)
        return 1
    print("PASS: %d files checked (%s; %s)." % (info["files"], src, dl))
    return 0


if __name__ == "__main__":
    sys.exit(main())
