#!/usr/bin/env python3
"""orch_detect - propose CI jobs from a project tree (design section 6, contract "orch_detect.py"). Stdlib only.

Stacks: dotnet, node, python and Dockerfile image builds; any other manifest becomes an "unknown" entry (the agent
asks the user for the image and the command). A container job splits into prep (runs with network: restore or
install, fills the cache mount) and cmd (runs offline in the same container). Versions resolve first-hit-wins.
Workspace manifests (pnpm-workspace.yaml, package.json "workspaces", a .sln/.slnx, a uv workspace) claim their
members, so one workspace yields one job set. The file list comes from `git ls-files` through run= (falls back to
os.walk); nested git repositories become the job's "repo" (dir, dockerfile and context are then relative to it).
Nothing is written. Quick look: python orch_detect.py [ROOT]
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
from pathlib import Path

try:  # 3.11+; 3.10, and a pyproject.toml that is not valid TOML, use the line-based fallback in _pyproject
    import tomllib as _toml
except ImportError:  # pragma: no cover - Python 3.10
    _toml = None

# every dot-dir (.git, .venv, .next, .nuxt, .devcontainer, ...) is skipped as well
IGNORE_DIRS = frozenset({"node_modules", "vendor", "bin", "obj", "dist", "build", "target", "venv", "env",
                         "coverage", "out", "examples", "__fixtures__", "fixtures", "testdata", "__snapshots__",
                         "bower_components", "__pycache__", "site-packages"})
GENERIC_DIRS = frozenset({"src", "source", "sources", "lib", "libs", "code", "main", "packages", "test", "tests",
                          "unit", "integration", "e2e"})   # named with their parent: kernel-src-build, not src-build
SIGNAL_DEPTH = 2         # files this many levels below the deepest project dir still count (tests/unit/test_x.py)
MAX_READ = 2 << 20       # bytes read per manifest
STAGE_RANK = {"lint": 0, "unit": 1, "build": 2, "image": 3}
DOTNET_FAIL = ["error CS[0-9]+", "Failed!", "Failed: +[1-9]"]
NODE_FAIL = ["FAIL ", "\u2715", "[0-9]+ failing", "error TS[0-9]+"]
PY_FAIL = ["FAILED", "ERROR "]
RUFF_FAIL = [":[0-9]+:[0-9]+: [A-Z]+[0-9]+ ", "Found [0-9]+ error"]
IMAGE_FAIL = ["did not complete successfully: exit code"]   # a RUN step failed (not a pull or network error)
NODE_DEFAULT, PY_DEFAULT = 22, "3.12"
NODE_LTS = {"argon": 4, "boron": 6, "carbon": 8, "dubnium": 10, "erbium": 12, "fermium": 14, "gallium": 16,
            "hydrogen": 18, "iron": 20, "jod": 22, "krypton": 24}
NODE_PM = {  # pm -> (lockfiles, install, install without lockfile, run, exec, cache name, cache path, all members)
    "npm": (("package-lock.json", "npm-shrinkwrap.json"), "npm ci --no-audit --no-fund",
            "npm install --no-audit --no-fund", "npm run", "npx", "npm", "/root/.npm",
            "npm run test --workspaces --if-present"),
    "pnpm": (("pnpm-lock.yaml",), "pnpm install --frozen-lockfile", "pnpm install", "pnpm run", "pnpm exec",
             "pnpm", "/root/.local/share/pnpm/store", "pnpm -r run test"),
    "yarn1": (("yarn.lock",), "yarn install --frozen-lockfile --non-interactive", "yarn install --non-interactive",
              "yarn run", "yarn", "yarn", "/usr/local/share/.cache/yarn", "yarn workspaces run test"),
    "berry": (("yarn.lock",), "yarn install --immutable", "yarn install", "yarn run", "yarn", "yarn-berry",
              "/root/.yarn/berry/cache", "yarn workspaces foreach -A run test")}
UNKNOWN = {"go.mod": "go", "go.work": "go", "cargo.toml": "rust", "pom.xml": "java", "build.gradle": "java",
           "build.gradle.kts": "java", "settings.gradle": "java", "settings.gradle.kts": "java", "gemfile": "ruby",
           "composer.json": "php", "mix.exs": "elixir", "package.swift": "swift", "pubspec.yaml": "dart",
           "cmakelists.txt": "c/c++", "meson.build": "c/c++", "deno.json": "deno", "deno.jsonc": "deno",
           "build.sbt": "scala", "project.clj": "clojure", "deps.edn": "clojure", "stack.yaml": "haskell",
           "rebar.config": "erlang", "dune-project": "ocaml", "pipfile": "pipenv"}
PROJ_EXT, SLN_EXT = (".csproj", ".fsproj", ".vbproj"), (".sln", ".slnx")
_SLN_PROJ = re.compile(r'^\s*Project\("[^"]*"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+)"', re.M)
_SLNX_PROJ = re.compile(r'<Project\b[^>]*?\bPath\s*=\s*"([^"]+)"')
_TFM = re.compile(r"<TargetFrameworks?(?:\s[^>]*)?>([^<]*)</TargetFrameworks?>", re.I)
_TEST_REF = re.compile(r"Microsoft\.NET\.Test\.Sdk|\bxunit|\bnunit\b|\bmstest|\btunit\b|\bexpecto\b"
                       r"|<IsTestProject>\s*true", re.I)
_NODE_CMP = re.compile(r"(>=|<=|>|<|=|\^|~)?\s*v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?")
_REQ_INSTALL = ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt", "dev-requirements.txt",
                "requirements-test.txt", "requirements_test.txt", "requirements-tests.txt", "test-requirements.txt")
_EXTRAS = ("test", "tests", "testing", "dev")
_PYTEST_DEP = re.compile(r"""(?im)^\s*["']?pytest\b|["']pytest\b""")   # requirement lines / quoted deps / poetry keys
_RUFF_DEP = re.compile(r"""(?im)^\s*["']?ruff\b|["']ruff\b""")
_TESTFILE = re.compile(r"(test_.+|.+_test|tests?)\.py")


def _join(d, name):
    return f"{d}/{name}" if d else name


def _depth(d):
    return d.count("/") + 1 if d else 0


def _under(d, top):
    """d lies strictly below top (root-relative dirs, "" = the root)."""
    return d != top and (not top or d.startswith(top + "/"))


def _rel(d, top):
    return d[len(top) + 1:] if top else d


def _skip(parts):
    return any(p.startswith(".") or p.lower() in IGNORE_DIRS for p in parts)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:40].strip("-")


def _globs_match(rel, globs):
    """npm/pnpm/uv workspace globs, '!' excludes. fnmatch's * also crosses '/', which only widens a claim."""
    def norm(g):
        g = g.strip()
        return (g[2:] if g.startswith("./") else g).rstrip("/")
    pos = [norm(g) for g in globs if not g.strip().startswith("!")]
    neg = [norm(g.strip()[1:]) for g in globs if g.strip().startswith("!")]
    return (any(fnmatch.fnmatchcase(rel, g) for g in pos)
            and not any(fnmatch.fnmatchcase(rel, g) for g in neg))


def manifest_hash(root, files) -> str:
    """sha256 over each file's root-relative path and content ("sha256:<hex>"). CRLF reads as LF, so a checkout's
    line-ending setting never shows as drift; a missing file hashes as absent; order and duplicates do not matter."""
    h = hashlib.sha256()
    for rel in sorted(set(files)):
        try:
            data = (Path(root) / rel).read_bytes().replace(b"\r\n", b"\n")
            mark = b"F%d\0" % len(data)
        except OSError:
            data, mark = b"", b"-\0"
        h.update(rel.encode("utf-8") + b"\0" + mark + data)
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------------------------------------------- file list
def _git_ls(top, run):
    """Tracked + untracked-not-ignored paths of the repository at top (relative to it), or None if git fails."""
    try:
        r = run(["git", "-C", str(top), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    out = r.stdout or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p]


def _list_files(root, max_depth, run):
    """-> (files, repos): sorted root-relative posix paths (dot-dirs, IGNORE_DIRS and anything more than
    max_depth + SIGNAL_DEPTH levels deep dropped) and the root-relative dirs that are git repositories ("" = the
    root). Each repository is listed with `git ls-files`; without git (or outside a repository) the tree is walked."""
    cap, files, repos, todo = max_depth + SIGNAL_DEPTH, set(), set(), [""]
    while todo:
        base = todo.pop()
        top, pre = (root / base if base else root), (base + "/" if base else "")
        listed = _git_ls(top, run)
        if listed is not None or (top / ".git").exists():
            repos.add(base)
        if listed is not None:
            for p in listed:
                rel = pre + p.rstrip("/")
                parts = rel.split("/")
                if _skip(parts[:-1]):
                    continue
                # an untracked nested repository is listed as "dir/", a submodule as a bare gitlink path
                if p.endswith("/") or (len(parts) <= max_depth and (root / rel).is_dir()):
                    if len(parts) <= max_depth and not _skip(parts[-1:]) and (root / rel / ".git").exists():
                        todo.append(rel)
                    continue
                if len(parts) - 1 <= cap:
                    files.add(rel)
            continue
        for dp, dns, fns in os.walk(top):
            d = Path(dp).relative_to(root).as_posix()
            d = "" if d == "." else d
            keep = []
            for n in dns:
                if _skip([n]) or _depth(d) + 1 > cap:
                    continue
                if (Path(dp) / n / ".git").exists():  # nested repository: listed on its own
                    if _depth(d) + 1 <= max_depth:
                        todo.append(_join(d, n))
                    continue
                keep.append(n)
            dns[:] = keep
            files.update(_join(d, f) for f in fns)
    return sorted(files), repos


class _Tree:
    def __init__(self, root, files, repos, pname):
        self.root, self.files, self.pname = root, files, pname
        self.set, self.lower = set(files), {f.lower(): f for f in files}
        self.repos = sorted((r for r in repos if r), key=len, reverse=True)  # deepest first; "" maps to "."
        self.dirs, self.alldirs = {}, set()
        for f in files:
            d, _, b = f.rpartition("/")
            self.dirs.setdefault(d, set()).add(b)
            while d and d not in self.alldirs:
                self.alldirs.add(d)
                d = posixpath.dirname(d)
        self._cache = {}

    def read(self, rel):
        if rel not in self._cache:
            try:
                with open(self.root / rel, "rb") as fh:
                    self._cache[rel] = fh.read(MAX_READ).decode("utf-8-sig", "replace")
            except OSError:
                self._cache[rel] = None
        return self._cache[rel]

    def locate(self, p):
        """(repo, path inside it) for a root-relative path; repo "." = the project root."""
        for r in self.repos:
            if p == r or p.startswith(r + "/"):
                return r, p[len(r) + 1:]
        return ".", p

    def repo_dir(self, d):
        r = self.locate(d)[0]
        return "" if r == "." else r

    def up(self, d, name, stop, every=False):
        """Paths of `name` (case-insensitive) in d and its parents up to stop, nearest first: only what the
        container sees, because it gets the repository at stop and nothing above it."""
        out = []
        while True:
            hit = [b for b in self.dirs.get(d, ()) if b.lower() == name]
            out += [_join(d, b) for b in sorted(hit)[:1]]
            if (out and not every) or d == stop or not d:
                return out
            d = posixpath.dirname(d)

    def label(self, d):
        b = posixpath.basename(d)
        if b.lower() in GENERIC_DIRS:
            return f"{posixpath.basename(posixpath.dirname(d)) or self.pname}-{b}"
        return b or self.pname


class _Reads:
    """Reads the files of one project and remembers which (they make up its manifest)."""
    def __init__(self, t):
        self.t, self.paths = t, []

    def __call__(self, rel):
        if rel not in self.paths:
            self.paths.append(rel)
        return self.t.read(rel) or ""


# ------------------------------------------------------------------------------------------------------- dotnet
def _tfm(s):
    """'net8.0' -> (8, 0, None); 'net8.0-windows' -> (8, 0, 'windows'); 'netcoreapp3.1' -> (3, 1, None);
    'net48' -> (4, 8, 'framework'); netstandard, $(Property) and the rest -> None."""
    s = s.strip().lower()
    m = re.fullmatch(r"net(\d+)\.(\d+)(?:-([a-z]+)[\d.]*)?", s)
    if m and int(m[1]) >= 5:
        return int(m[1]), int(m[2]), m[3]
    m = re.fullmatch(r"netcoreapp(\d+)\.(\d+)", s)
    if m:
        return int(m[1]), int(m[2]), None
    m = re.fullmatch(r"net(\d)(\d)\d?", s)
    return (int(m[1]), int(m[2]), "framework") if m else None


def _sln_members(t, rd, sln):
    """Root-relative project paths a .sln/.slnx lists (inside the root; solution folders skipped)."""
    base, out = posixpath.dirname(sln), []
    for p in (_SLNX_PROJ if sln.lower().endswith(".slnx") else _SLN_PROJ).findall(rd(sln)):
        p = posixpath.normpath(posixpath.join(base, p.replace("\\", "/")))
        p = p if p in t.set else t.lower.get(p.lower(), p)   # solutions written on Windows may differ in case
        if p.lower().endswith(PROJ_EXT) and not p.startswith("../") and not posixpath.isabs(p) and p not in out:
            out.append(p)
    return out


def _dotnet(t, dirs, add, notes, unknown):
    claimed = set()
    for d in dirs:
        bs = t.dirs[d]
        slns = sorted(b for b in bs if b.lower().endswith(SLN_EXT))
        pick = slns or [b for b in sorted(bs) if b.lower().endswith(PROJ_EXT) and _join(d, b) not in claimed]
        if not pick:
            continue   # no project here, or every one is built by an earlier (shallower) solution
        stem = (posixpath.basename(d) or t.pname).lower()   # target: named like its dir, else first; .sln first
        target = sorted(pick, key=lambda n: (posixpath.splitext(n)[0].lower() != stem,
                                             n.lower().endswith(".slnx"), n.lower()))[0]
        rd, tpath = _Reads(t), _join(d, target)
        members = _sln_members(t, rd, tpath) if slns else [tpath]
        if members and all(m in claimed for m in members):
            continue   # every project is built by an earlier (shallower) solution
        claimed.update(members)
        if len(pick) > 1:
            notes.append(f"{d or '.'}/: several {'solutions' if slns else 'projects'} ({', '.join(pick)}); orch "
                         f"proposes {target}, say which one to use if another is right.")
        stop, tfms, test = t.repo_dir(d), [], False
        for m in members:
            for f in [m] + t.up(posixpath.dirname(m), "directory.build.props", stop, every=True):
                txt = rd(f)
                tfms += [x.strip() for v in _TFM.findall(txt) for x in v.split(";") if x.strip()]
                test = test or bool(_TEST_REF.search(txt))
        parsed = [p for p in map(_tfm, tfms) if p]
        modern = [(a, b) for a, b, plat in parsed if plat is None]
        found = ", ".join(sorted(set(tfms))) or "none"
        if parsed and not modern:
            unknown.append(tpath)
            notes.append(f"{tpath}: target frameworks {found} need Windows or extra workloads; tell orch the image "
                         "and the commands to use, or leave it out.")
            continue
        ver = None
        for gj in t.up(d, "global.json", stop)[:1]:   # global.json sdk.version wins over the target frameworks
            m = re.search(r'"sdk"\s*:\s*\{[^{}]*?"version"\s*:\s*"(\d+)\.(\d+)', rd(gj), re.S)
            ver = (int(m[1]), int(m[2])) if m else None
        ver = ver or (max(modern) if modern else None)
        if not ver:
            unknown.append(tpath)
            notes.append(f"{tpath}: no .NET SDK version found (target frameworks: {found}); add a global.json with "
                         "sdk.version, or tell orch which SDK image to use.")
            continue
        x, y = ver
        repo, rdir = t.locate(d)
        q = shlex.quote(target)
        job = {"kind": "container", "image": f"mcr.microsoft.com/dotnet/sdk:{x}.{y}", "dir": rdir, "repo": repo,
               "prep": f"dotnet restore {q}", "cache": {"name": "nuget", "path": "/root/.nuget/packages"},
               "env": {"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}, "fail_patterns": DOTNET_FAIL,
               "result_paths": ["/out"], "detected": f"dotnet {x if y == 0 else str(x) + '.' + str(y)} ({tpath})"}
        if test:
            add(d, "test", dict(job, stage="unit", memory="3g", timeout=1200,
                                cmd=f"dotnet test {q} --no-restore --nologo --logger trx --results-directory /out"),
                rd.paths)
        add(d, "build", dict(job, stage="build", memory="4g", timeout=1800,
                             cmd=f"dotnet build {q} --no-restore -c Release --nologo"), rd.paths)


# --------------------------------------------------------------------------------------------------------- node
def _node_major(spec):
    """Node major for an engines.node range: the default 22 when the range allows it, else the highest allowed
    major below 22, else the lowest allowed one; None when nothing parses."""
    allowed = set()
    for alt in str(spec).split("||"):
        alt = alt.strip()
        hy = re.fullmatch(r"v?(\d+)\S*\s+-\s+v?(\d+)\S*", alt)
        if hy:
            allowed.update(range(int(hy[1]), int(hy[2]) + 1))
            continue
        lo, hi, seen = 0, 60, False
        for op, ma, mi, pa in _NODE_CMP.findall(alt):
            seen = True
            if not ma.isdigit():
                continue   # x / *: any major
            m, bare = int(ma), not mi.isdigit()
            zero = bare or (mi == "0" and (not pa.isdigit() or pa == "0"))
            if op in ("", "=", "^", "~"):
                lo, hi = max(lo, m), min(hi, m)
            elif op == ">=":
                lo = max(lo, m)
            elif op == ">":
                lo = max(lo, m + 1 if bare else m)
            elif op == "<":
                hi = min(hi, m - 1 if zero else m)
            else:   # <=
                hi = min(hi, m)
        if seen and lo <= hi:
            allowed.update(range(lo, hi + 1))
    allowed = {m for m in allowed if 4 <= m <= 60}
    if not allowed:
        return None
    if NODE_DEFAULT in allowed:
        return NODE_DEFAULT
    below = [m for m in allowed if m < NODE_DEFAULT]
    return max(below) if below else min(allowed)


def _node_file_major(text):
    """.nvmrc / .node-version: 'v20.11.0', '20', 'lts/iron' -> major; 'lts/*', 'node' -> None (next source)."""
    line = (text.strip().splitlines() or [""])[0].strip().lower()
    m = re.match(r"v?(\d+)", line)
    if m:
        return int(m[1]) if int(m[1]) >= 4 else None
    m = re.fullmatch(r"lts/([a-z]+)", line)
    return NODE_LTS.get(m[1]) if m else None


def _pnpm_globs(text):
    """The packages: list of a pnpm-workspace.yaml (block or flow style)."""
    out, inside = [], False
    for line in text.splitlines():
        if re.match(r"packages\s*:", line):
            flow = re.search(r"\[(.*)\]", line)
            if flow:
                return [x.strip().strip("'\"") for x in flow[1].split(",") if x.strip()]
            inside = True
            continue
        if inside:
            m = re.match(r"\s*-\s*(['\"]?)(.+?)\1\s*(#.*)?$", line)
            if m:
                out.append(m[2])
            elif line.strip() and not line.lstrip().startswith("#") and not line[:1].isspace():
                break
    return out


def _node(t, dirs, add, notes, unknown):
    claimed = set()
    for d in dirs:
        bs = t.dirs[d]
        if "package.json" not in bs or d in claimed:
            continue
        rd, p = _Reads(t), _join(d, "package.json")
        try:
            pj = json.loads(rd(p) or "null")
        except ValueError:
            pj = None
        if not isinstance(pj, dict):
            unknown.append(p)
            notes.append(f"{p}: not a readable JSON object; fix it, then run orch init --detect again.")
            continue
        ws = pj.get("workspaces")
        ws = ws.get("packages") if isinstance(ws, dict) else ws
        globs = [g for g in ws if isinstance(g, str)] if isinstance(ws, list) else []
        if "pnpm-workspace.yaml" in bs:
            globs += _pnpm_globs(rd(_join(d, "pnpm-workspace.yaml"))) or ["**"]
        if globs:   # workspace root: its members run from here
            claimed.update(e for e, eb in t.dirs.items()
                           if "package.json" in eb and _under(e, d) and _globs_match(_rel(e, d), globs))
        field = pj.get("packageManager")
        if isinstance(field, str) and "@" in field:
            name, _, ver = field.partition("@")
            pm = ("yarn1" if ver.startswith("1.") else "berry") if name == "yarn" else name
        elif "pnpm-lock.yaml" in bs:
            pm = "pnpm"
        elif "yarn.lock" in bs:
            pm = "berry" if ".yarnrc.yml" in bs else "yarn1"
        elif bs & {"bun.lock", "bun.lockb"}:
            pm = "bun"
        else:
            pm = "npm"
        if pm not in NODE_PM:
            unknown.append(p)
            notes.append(f"{p}: the {pm} package manager is not supported by orch; tell orch the image and the "
                         "test command.")
            continue
        locks, install, install_free, runner, exe, cname, cpath, every = NODE_PM[pm]
        stop, major = t.repo_dir(d), None
        eng = pj.get("engines")
        if isinstance(eng, dict) and isinstance(eng.get("node"), str):
            major = _node_major(eng["node"])
        for n in (".nvmrc", ".node-version"):
            for f in ([] if major else t.up(d, n, stop)):
                major = _node_file_major(rd(f))
        major = major or NODE_DEFAULT
        unlocked = not bs & set(locks)
        install = install_free if unlocked else install
        env = {"CI": "true"}
        if pm in ("pnpm", "berry"):   # corepack ships with node up to 24; node 25+ needs it installed first
            install = ("npm install -g corepack --no-audit --no-fund && " if major >= 25 else "") \
                + "corepack enable && " + install
            env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
        scripts = pj.get("scripts") if isinstance(pj.get("scripts"), dict) else {}
        has = lambda s: isinstance(scripts.get(s), str) and scripts[s].strip()
        deps = {**(pj.get("dependencies") or {}), **(pj.get("devDependencies") or {})} \
            if all(isinstance(pj.get(k) or {}, dict) for k in ("dependencies", "devDependencies")) else {}
        want = []
        if has("lint"):
            want.append(("lint", "lint", f"{runner} lint"))
        if has("typecheck"):
            want.append(("lint", "typecheck", f"{runner} typecheck"))
        elif "tsconfig.json" in bs and "typescript" in deps:
            want.append(("lint", "typecheck", f"{exe} tsc --noEmit"))
        if has("test") and re.search(r"no test specified", scripts["test"]):
            notes.append(f"{p}: the test script is npm's placeholder, so orch proposes no test job for it.")
        elif has("test"):
            want.append(("unit", "test", f"{runner} test"))
        if has("build"):
            want.append(("build", "build", f"{runner} build"))
        if not want:
            notes.append(f"{p}: no lint, typecheck, test or build script, so orch proposes no job for it; add a "
                         "script or tell orch the command." + (f" A workspace root runs its members through its own "
                                                               f"scripts, for example \"test\": \"{every}\"."
                                                               if globs else ""))
        elif unlocked:
            notes.append(f"{p}: no lockfile, so prep runs `{install_free}` and resolves fresh versions on every run; "
                         "commit the lockfile for repeatable runs.")
        repo, rdir = t.locate(d)
        for stage, suffix, cmd in want:
            big = stage == "build"
            add(d, suffix, {"stage": stage, "kind": "container", "image": f"node:{major}-bookworm-slim", "dir": rdir,
                            "repo": repo, "prep": install, "cmd": cmd, "cache": {"name": cname, "path": cpath},
                            "env": dict(env, NODE_OPTIONS="--max-old-space-size=3072") if big else env,
                            "memory": "4g" if big else "2g", "timeout": 1800 if big else 1200,
                            "fail_patterns": NODE_FAIL, "result_paths": ["/out"], "detected": f"node {major} ({p})"},
                rd.paths)


# ------------------------------------------------------------------------------------------------------- python
def _toml_sections(text):
    """{table name: its body lines} by a line scan ("" = before the first table header)."""
    body, sec = {"": []}, ""
    for line in text.splitlines():
        m = re.match(r"\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", line)
        if m:
            sec = re.sub(r"[\"'\s]", "", m[1])
            body.setdefault(sec, [])
        else:
            body[sec].append(line)
    return body


# a small TOML value reader for the 3.10 fallback: strings (escapes kept as written), arrays and inline tables
_TWS = re.compile(r"(?:\s|#[^\n]*)*")   # whitespace, newlines and comments
_TKEY = re.compile(r"""[ \t]*(?:([A-Za-z0-9_-]+)|"((?:[^"\\\n]|\\.)*)"|'([^'\n]*)')[ \t]*""")
_TSTR = re.compile(r'"""((?:[^\\]|\\.)*?)"""' r"|'''(.*?)'''" r'|"((?:[^"\\\n]|\\.)*)"' r"|'([^'\n]*)'", re.S)
_TBARE = re.compile(r"""[^\s,\]}#=\[{"']+""")   # numbers, booleans, dates: read as None


def _toml_key(s, i):
    """([parts], end) of the bare, quoted or dotted key at s[i]; ValueError when there is none."""
    keys = []
    while True:
        m = _TKEY.match(s, i)
        if not m:
            raise ValueError("no key")
        keys.append(next(g for g in m.groups() if g is not None))
        if s[m.end():m.end() + 1] != ".":
            return keys, m.end()
        i = m.end() + 1


def _toml_put(d, keys, value):
    for k in keys[:-1]:
        d = d.setdefault(k, {})
        if not isinstance(d, dict):
            return
    d[keys[-1]] = value


def _toml_value(s, i):
    """(value, end) of the value at s[i]; ValueError when it cannot be read."""
    i = _TWS.match(s, i).end()
    c = s[i:i + 1]
    if c == "[" or c == "{":
        out, close = ([], "]") if c == "[" else ({}, "}")
        i = _TWS.match(s, i + 1).end()
        while s[i:i + 1] != close:
            if close == "]":
                v, i = _toml_value(s, i)
                out.append(v)
            else:
                keys, i = _toml_key(s, i)
                if s[i:i + 1] != "=":
                    raise ValueError("no =")
                v, i = _toml_value(s, i + 1)
                _toml_put(out, keys, v)
            i = _TWS.match(s, i).end()
            if s[i:i + 1] == ",":
                i = _TWS.match(s, i + 1).end()
            elif s[i:i + 1] != close:
                raise ValueError("unclosed")
        return out, i + 1
    m = _TSTR.match(s, i)
    if m:   # a multi-line string drops the newline right after its opening quotes
        v = next(g for g in m.groups() if g is not None)
        return (re.sub(r"\A\r?\n", "", v) if m.lastindex <= 2 else v), m.end()
    m = _TBARE.match(s, i)
    if not m:
        raise ValueError("no value")
    return None, m.end()


def _toml_table(text):
    """The key/value pairs of one table body as a dict; a pair it cannot read is skipped up to the next line."""
    d, i = {}, 0
    while True:
        i = _TWS.match(text, i).end()
        if i >= len(text):
            return d
        try:
            keys, j = _toml_key(text, i)
            if text[j:j + 1] != "=":
                raise ValueError("no =")
            v, i = _toml_value(text, j + 1)
            _toml_put(d, keys, v)
        except (ValueError, RecursionError):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j + 1


def _py_reqs(proj):
    """([project] dependencies, {extra: its requirements}) of a parsed [project] table."""
    strs = lambda v: [x for x in v if isinstance(x, str)] if isinstance(v, list) else None
    proj = proj if isinstance(proj, dict) else {}
    opt = proj.get("optional-dependencies")
    extras = {k: strs(v) for k, v in opt.items() if isinstance(v, list)} if isinstance(opt, dict) else {}
    return strs(proj.get("dependencies")) or [], extras


def _pyproject(text):
    """The few pyproject.toml facts detection needs: tomllib on 3.11+, else (and for invalid TOML) a line scan, with
    the [project] dependency lists read in all three spellings (section, inline table, dotted keys)."""
    info = {"project": False, "poetry": False, "build": False, "pytest": False, "ruff": False, "uv_members": None,
            "requires_python": None, "poetry_python": None, "dependencies": [], "extras": {}}
    d = None
    if _toml is not None and text:
        try:
            d = _toml.loads(text)
        except ValueError:   # tomllib.TOMLDecodeError
            d = None
    if d is not None:
        tab = lambda x, k: x.get(k) if isinstance(x, dict) and isinstance(x.get(k), dict) else None
        tool, proj = tab(d, "tool") or {}, tab(d, "project")
        poetry, ws = tab(tool, "poetry"), tab(tab(tool, "uv") or {}, "workspace")
        py = (tab(poetry, "dependencies") or {}).get("python") if poetry else None
        deps, extras = _py_reqs(proj)
        info.update(project=proj is not None, poetry=poetry is not None, build="build-system" in d,
                    pytest=any(str(k).startswith("pytest") for k in tool), ruff="ruff" in tool,
                    uv_members=[m for m in (ws.get("members") if isinstance(ws.get("members"), list) else [])
                                if isinstance(m, str)] if ws is not None else None,
                    requires_python=str(proj.get("requires-python") or "") or None if proj else None,
                    poetry_python=str((py.get("version") if isinstance(py, dict) else py) or "") or None,
                    dependencies=deps, extras=extras)
        return info
    body = _toml_sections(text)
    has = lambda name: any(s == name or s.startswith(name + ".") for s in body)
    txt = lambda s: "\n".join(body.get(s, []))
    ws = re.search(r"members\s*=\s*\[(.*?)\]", txt("tool.uv.workspace"), re.S)
    rp = re.search(r"""^\s*requires-python\s*=\s*["']([^"']*)["']""", txt("project"), re.M)
    pp = re.search(r"""^\s*python\s*=\s*(?:\{[^}]*?version\s*=\s*)?["']([^"']*)["']""",
                   txt("tool.poetry.dependencies"), re.M)
    proj = _toml_table(txt("project"))
    for k, v in _toml_table(txt("project.optional-dependencies")).items():
        _toml_put(proj, ["optional-dependencies", k], v)
    deps, extras = _py_reqs(proj)
    info.update(project=has("project"), poetry=has("tool.poetry"), build=has("build-system"),
                pytest=any(s.startswith("tool.pytest") for s in body), ruff=has("tool.ruff"),
                uv_members=re.findall(r"""["']([^"']+)["']""", ws[1]) if ws else
                ([] if "tool.uv.workspace" in body else None),
                requires_python=rp[1] if rp else None, poetry_python=pp[1] if pp else None,
                dependencies=deps, extras=extras)
    return info


def _py_minor(s):
    m = re.search(r"(\d+)\.(\d+)", s or "")
    return f"{m[1]}.{m[2]}" if m else None


def _py_lowest(spec):
    """Lowest X.Y a requires-python / poetry python range allows ('>=3.10,<4' -> '3.10'); None if none."""
    lows = [(int(a), int(b)) for op, a, b in re.findall(r"(===|==|~=|!=|<=|>=|<|>|\^|~|=)?\s*(\d+)\.(\d+)", spec or "")
            if op not in ("<", "<=", "!=")]
    return "%d.%d" % min(lows) if lows else None


def _python(t, dirs, add, notes, unknown):
    roots, workspaces = [], []
    for d in dirs:
        bs = t.dirs[d]
        reqs = sorted(b for b in bs if b.lower().startswith("requirements") and b.lower().endswith(".txt"))
        if not (reqs or bs & {"pyproject.toml", "setup.py"}):
            continue
        rd = _Reads(t)
        pp_text = rd(_join(d, "pyproject.toml")) if "pyproject.toml" in bs else ""
        pp = _pyproject(pp_text)
        own = bool(bs & {"setup.py", "setup.cfg"}) or pp["project"] or pp["poetry"] or pp["uv_members"] is not None
        if not (own or reqs or pp["build"] or bs & {"uv.lock", "poetry.lock"}):
            continue   # a pyproject.toml holding tool settings only
        if any(_under(d, w) and _globs_match(_rel(d, w), g) for w, g in workspaces):
            continue   # uv workspace member: synced and tested from the workspace root
        if not own and any(_under(d, r) for r in roots):
            continue   # requirements-only dir (docs/, tools/) inside a project
        roots.append(d)
        if pp["uv_members"]:
            workspaces.append((d, pp["uv_members"]))
        primary = _join(d, "pyproject.toml" if "pyproject.toml" in bs else "setup.py" if "setup.py" in bs else reqs[0])
        stop = t.repo_dir(d)
        ver = None
        for f in t.up(d, ".python-version", stop):
            ver = _py_minor((rd(f).strip().splitlines() or [""])[0])
        ver = ver or _py_lowest(pp["requires_python"]) or _py_lowest(pp["poetry_python"]) or PY_DEFAULT
        install_reqs = [r for r in _REQ_INSTALL if r in bs] or reqs
        req_text = {r: rd(_join(d, r)) for r in sorted(set(reqs) | set(install_reqs))}
        mgr = "uv" if "uv.lock" in bs else "poetry" if ("poetry.lock" in bs or pp["poetry"]) else "pip"
        declared = "\n".join([pp_text, *req_text.values()])
        pkg = mgr == "pip" and bool(bs & {"setup.py", "setup.cfg"} or pp["project"])   # pip install -e .
        ex = [e for e in _EXTRAS if e in pp["extras"]] if pkg else []
        # what prep installs: uv/poetry sync every declared group; pip only the requirement files and, with -e .,
        # the [project] dependencies and the chosen extras (a tool declared anywhere else is added to prep)
        installed = declared if mgr != "pip" else "\n".join(
            [req_text[r] for r in install_reqs] + (pp["dependencies"] if pkg else [])
            + [x for e in ex for x in pp["extras"][e]])
        pre = d + "/" if d else ""
        sub = [f[len(pre):] for f in t.files if f.startswith(pre)]
        tests = [f for f in sub if _TESTFILE.fullmatch(posixpath.basename(f))]
        pytest_cfg = pp["pytest"] or "pytest.ini" in bs or any(posixpath.basename(f) == "conftest.py" for f in sub)
        pytest_dep = bool(_PYTEST_DEP.search(declared))
        # pytest also runs unittest-style tests, and unittest's default pattern (test*.py) misses *_test.py
        use_pytest = pytest_dep or pytest_cfg or any(
            (f.startswith("tests/") and posixpath.basename(f).startswith("test_")) or f.endswith("_test.py")
            for f in tests)
        ruff_dep = bool(_RUFF_DEP.search(declared))
        if mgr == "uv":
            image, cache, env = f"ghcr.io/astral-sh/uv:python{ver}-bookworm-slim", ("uv", "/root/.cache/uv"), \
                {"UV_LINK_MODE": "copy"}
            base, exe = "uv sync --frozen --all-extras --dev" + (" --all-packages" if pp["uv_members"] else ""), \
                "uv run --frozen --offline "
            prep = lambda tool: base + (f" && uv pip install {tool}" if tool else "")
        elif mgr == "poetry":
            image, cache, env = f"python:{ver}-slim", ("poetry", "/root/.cache/pypoetry"), \
                {"POETRY_VIRTUALENVS_IN_PROJECT": "true"}
            base, exe = "pip install poetry && poetry install --no-interaction", "poetry run "
            prep = lambda tool: base + (f" && poetry run pip install {tool}" if tool else "")
        else:
            image, cache, env, exe = f"python:{ver}-slim", ("pip", "/root/.cache/pip"), {}, "/src/.venv/bin/"
            args = [f"-r {shlex.quote(r)}" for r in install_reqs]
            if pkg:
                args.append("-e " + shlex.quote("." + (f"[{','.join(ex)}]" if ex else "")))
            prep = lambda tool: "python -m venv /src/.venv" + (
                " && /src/.venv/bin/pip install " + " ".join(args + ([tool] if tool else [])) if args or tool else "")
        repo, rdir = t.locate(d)
        job = {"kind": "container", "image": image, "dir": rdir, "repo": repo, "env": env,
               "cache": {"name": cache[0], "path": cache[1]}, "memory": "2g", "timeout": 1200,
               "result_paths": ["/out"], "detected": f"python {ver} ({primary})"}
        missing = []
        if ruff_dep or pp["ruff"] or bs & {"ruff.toml", ".ruff.toml"}:
            missing += [] if ruff_dep else ["ruff"]
            add(d, "lint", dict(job, stage="lint", prep=prep(None if _RUFF_DEP.search(installed) else "ruff"),
                                cmd=f"{exe}ruff check .", fail_patterns=PY_FAIL + RUFF_FAIL), rd.paths)
        if tests or pytest_cfg:
            if use_pytest:
                missing += [] if pytest_dep else ["pytest"]
                cmd = f"{exe}pytest -q -p no:cacheprovider" if mgr != "pip" else \
                    "/src/.venv/bin/python -m pytest -q -p no:cacheprovider"
            else:   # a tests dir without __init__.py is not importable from the top: start discovery inside it
                sdir = next((s for s in ("tests", "test") if any(f.startswith(s + "/") for f in tests)
                             and f"{s}/__init__.py" not in sub), None)
                cmd = ("/src/.venv/bin/python" if mgr == "pip" else exe + "python") + " -m unittest discover" + (
                    f" -s {sdir}" if sdir else "")
            add(d, "test", dict(job, stage="unit", prep=prep(
                "pytest" if use_pytest and not _PYTEST_DEP.search(installed) else None), cmd=cmd,
                fail_patterns=PY_FAIL), rd.paths)
        else:
            notes.append(f"{primary}: no tests found (test_*.py files, pytest.ini, conftest.py or [tool.pytest] "
                         "settings), so orch proposes no test job for it; add tests or tell orch the command.")
        if missing:
            it = "it" if len(missing) == 1 else "them"
            notes.append(f"{primary}: prep installs the latest {' and '.join(missing)} because the project does not "
                         f"declare {it}; add {it} to the dev dependencies to pin the version.")


# ------------------------------------------------------------------------------------------------ docker, other
def _context(t, d, top, text):
    """Build context of a Dockerfile in d: its own dir, unless its COPY/ADD sources exist only from the repository
    root top (deploy/web.Dockerfile with COPY web/ ./ is built as `docker build -f deploy/web.Dockerfile .`)."""
    here = there = 0
    for line in re.findall(r"(?im)^\s*(?:COPY|ADD)\s+(.+)$", text):
        line = line.strip()
        if "--from" in line:
            continue   # copies from another build stage
        try:
            args = json.loads(line) if line.startswith("[") else [a for a in line.split() if not a.startswith("--")]
        except ValueError:
            continue
        for src in [a for a in args if isinstance(a, str)][:-1]:
            src = posixpath.normpath(src).strip("/") if not re.search(r"[$*?\[]|://", src) else ""
            if src and src != "." and not src.startswith(".."):
                here += _join(d, src) in t.set or _join(d, src) in t.alldirs
                there += _join(top, src) in t.set or _join(top, src) in t.alldirs
    return top if there > here else d


def _docker(t, dirs, add, notes, unknown):
    for d in dirs:
        for b in sorted(t.dirs[d]):
            lb = b.lower()
            if lb.endswith(".dockerignore") or not (lb == "dockerfile" or lb.startswith("dockerfile.")
                                                    or lb.endswith(".dockerfile")):
                continue
            rd, p = _Reads(t), _join(d, b)
            fps = list(IMAGE_FAIL)
            for img in re.findall(r"(?im)^\s*FROM\s+(?:--\S+\s+)*(\S+)", rd(p)):
                img = img.lower()
                extra = (DOTNET_FAIL if "dotnet/sdk" in img else
                         NODE_FAIL + ["Failed to compile"] if re.search(r"(^|/)node([:@]|$)", img) else
                         PY_FAIL if re.search(r"(^|/)python([:@]|$)", img) or "astral-sh/uv" in img else [])
                fps += [x for x in extra if x not in fps]
            repo, ctx = t.locate(_context(t, d, t.repo_dir(d), rd(p)))   # dir == context, whichever one reads it
            variant = _slug(re.sub(r"(?i)^dockerfile\.|\.dockerfile$", "", b)) if lb != "dockerfile" else ""
            add(d, f"{variant}-image" if variant else "image", {
                "stage": "image", "kind": "build", "dir": ctx, "repo": repo, "dockerfile": t.locate(p)[1],
                "context": ctx or ".", "memory": "4g", "timeout": 2400, "fail_patterns": fps, "result_paths": [],
                "detected": f"docker ({p})"}, rd.paths)


def _app_platform_dir(t, d):
    """d is inside the android/, ios/ or macos/ folder of a Flutter or React Native app (the app's own toolchain
    builds those)."""
    parts = d.split("/")
    return any(p.lower() in ("android", "ios", "macos") and t.dirs.get("/".join(parts[:i]), set())
               & {"pubspec.yaml", "package.json"} for i, p in enumerate(parts))


def _other(t, dirs, notes, unknown):
    seen = []   # (dir, stack): a workspace of an unknown stack is reported once, at its top
    for d in dirs:
        if _app_platform_dir(t, d):
            continue
        bs = t.dirs[d]
        for b in sorted(bs):
            stack = UNKNOWN.get(b.lower())
            if not stack or (stack == "pipenv" and (bs & {"pyproject.toml", "setup.py"} or any(
                    x.lower().startswith("requirements") and x.lower().endswith(".txt") for x in bs))):
                continue
            if any(s == stack and (e == d or _under(d, e)) for e, s in seen):
                continue
            seen.append((d, stack))
            unknown.append(_join(d, b))
            notes.append(f"{_join(d, b)}: orch does not detect {stack} projects; tell orch the image and the test "
                         "command.")


def detect(root, max_depth=3, project_name=None, run=subprocess.run) -> dict:
    """{"jobs": {name: config job}, "notes": [...], "unknown": [root-relative paths]} for the tree at root.
    Job names are <dir>-<stage or script> ([a-z0-9-], the root dir = project_name, default the root folder
    name), unique (-2, -3...). Each job carries detected, files (the paths read) and manifest (their hash)."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"project root {root} is not a directory; run orch init from inside the project")
    pname = _slug(project_name or root.name) or "project"
    t = _Tree(root, *_list_files(root, max_depth, run), pname)
    dirs = sorted((d for d in t.dirs if _depth(d) <= max_depth), key=lambda d: (_depth(d), d))
    protos, notes, unknown = [], [], []

    def add(d, suffix, job, files):
        protos.append((d, STAGE_RANK[job["stage"]], len(protos), suffix, job, list(files)))

    for fn in (_dotnet, _node, _python, _docker):
        fn(t, dirs, add, notes, unknown)
    _other(t, dirs, notes, unknown)
    if not protos and not unknown:
        unknown.append(".")
        notes.append(f"orch found no job to propose within {max_depth} levels of {pname} (it detects dotnet, node, "
                     "python and Dockerfiles); tell orch the image and the test command.")
    jobs = {}
    for d, _, _, suffix, job, files in sorted(protos, key=lambda x: x[:3]):
        base = f"{_slug(t.label(d)) or pname}-{suffix}"
        name, i = base, 2
        while name in jobs:
            name, i = f"{base}-{i}", i + 1
        jobs[name] = dict(copy.deepcopy(job), files=sorted(set(files)), manifest=manifest_hash(root, files))
    return {"jobs": jobs, "notes": list(dict.fromkeys(notes)), "unknown": list(dict.fromkeys(unknown))}


def _mem_words(m):
    m = str(m or "-").strip()
    u = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([gGmM])[bB]?", m)
    return f"{u[1]} {'GB' if u[2] in 'gG' else 'MB'}" if u else m


def proposal_lines(jobs: dict) -> list[str]:
    """One aligned line per job: name  stage  stack  dir/  memory  cmd (cmd cut to 60 characters)."""
    rows = []
    for name, j in jobs.items():
        repo, d = str(j.get("repo") or "."), str(j.get("dir") or "")
        where = "/".join(x for x in (repo if repo != "." else "", d) if x)
        cmd = j.get("cmd") or (f"docker build -f {j.get('dockerfile') or 'Dockerfile'} {j.get('context') or '.'}"
                               if j.get("kind") == "build" else "-")
        cmd = " ".join(str(cmd).split())
        rows.append([str(name), str(j.get("stage") or "-"), str(j.get("detected") or "-").split(" (")[0],
                     where + "/" if where else "./", _mem_words(j.get("memory")),
                     cmd if len(cmd) <= 60 else cmd[:57] + "..."])
    width = [max((len(r[i]) for r in rows), default=0) for i in range(5)]
    return ["  ".join(r[i].ljust(width[i]) for i in range(5)) + "  " + r[5] for r in rows]


if __name__ == "__main__":
    try:
        res = detect(sys.argv[1] if len(sys.argv) > 1 else ".")
    except NotADirectoryError as e:
        sys.exit(str(e))
    print("\n".join(proposal_lines(res["jobs"]) + [f"note: {n}" for n in res["notes"]]
                    + [f"unknown: {u}" for u in res["unknown"]]))
