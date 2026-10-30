"""Tests for scripts/validate.py on synthetic packages: structure checks and the zero-site-data scan.

This file ships in the package and is scanned too, so every site-shaped string below (private addresses, user
paths, phone numbers) is built at runtime with j(); none appears literally.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "scripts"))
import validate  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def j(*parts):
    return "".join(parts)


SKILL = """---
name: helper-node-orchestrator
description: Use when a task needs builds or tests offloaded to helper servers.
allowed-tools: Bash(bash ${CLAUDE_SKILL_DIR}/scripts/orch *)
---
# Helper-node orchestrator
## Start
x
## Core
x
## Where work runs
x
## Run
x
## Results
x
## Tokens
x
## Stop
x
## More
See [RULES.md](RULES.md), [refs](references/) and [site](https://example.com).
"""
OPENAI = 'interface:\n  short_description: "Offload builds and tests to helpers"\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "helper-node-orchestrator")
        self.write("SKILL.md", SKILL)
        self.write("RULES.md", "# Rules\n")
        self.write("references/init.md", "# Init\nBack to [rules](../RULES.md#glossary).\n")
        self.write("agents/openai.yaml", OPENAI)
        self.empty = self.write("empty-deny.txt", "", self.tmp)  # keeps the real site denylist out of most tests

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text, root=None):
        p = os.path.join(root or self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def errs(self, walk=True, **kw):
        kw.setdefault("denylist", self.empty)
        return validate.validate(self.root, walk=walk, **kw)

    def assertFails(self, needle, **kw):
        e = self.errs(**kw)
        self.assertTrue(any(needle in x for x in e), e)
        return e

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = validate.main(list(args))
        return rc, out.getvalue() + err.getvalue()


class StructureTest(Base):
    def test_pass(self):
        self.assertEqual(self.errs(), [])
        rc, out = self.main("--dir", self.root, "--denylist", self.empty)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("PASS: 4 files checked (every file; denylist "), out)

    def test_no_denylist_is_reported(self):
        rc, out = self.main("--dir", self.root)
        if os.path.isfile(os.path.join(validate.SKILL_DIR, validate.DENYLIST)):
            self.assertIn("(every file; denylist ", out)  # the development checkout supplies its own default
        else:
            self.assertIn("no denylist, so site strings were not checked", out)
        self.assertEqual(rc, 0, out)

    def test_missing_heading_start(self):
        self.write("SKILL.md", SKILL.replace("## Start\n", ""))
        self.assertFails("missing the heading '## Start'")
        rc, out = self.main("--dir", self.root, "--denylist", self.empty)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: 1 problem in 4 files", out)

    def test_every_heading_required(self):
        for s in validate.SECTIONS:
            self.write("SKILL.md", SKILL.replace(s + "\n", ""))
            self.assertFails("missing the heading '%s'" % s)

    def test_size_limits(self):
        self.assertEqual((validate.MAX_LINES, validate.MAX_BYTES), (130, 9500))

    def test_oversize_lines(self):
        self.write("SKILL.md", SKILL + "x\n" * (validate.MAX_LINES - SKILL.count("\n") + 1))
        self.assertFails("SKILL.md has 131 lines, over the 130 limit")

    def test_lines_at_limit_pass(self):
        self.write("SKILL.md", SKILL + "x\n" * (validate.MAX_LINES - SKILL.count("\n")))
        self.assertEqual(self.errs(), [])

    def pad_to(self, size):
        """SKILL plus filler lines of 200 bytes, `size` bytes in all and well under the line limit."""
        pad = size - len(SKILL.encode("utf-8"))
        return SKILL + ("y" * 199 + "\n") * (pad // 200) + "z" * (pad % 200)

    def test_oversize_bytes(self):
        self.write("SKILL.md", self.pad_to(validate.MAX_BYTES + 1))
        self.assertFails("SKILL.md has 9501 bytes, over the 9500 limit")

    def test_bytes_at_limit_pass(self):
        self.write("SKILL.md", self.pad_to(validate.MAX_BYTES))
        self.assertEqual(self.errs(), [])

    def test_broken_link(self):
        self.write("SKILL.md", SKILL + "[x](missing.md)\n")
        self.assertFails("broken link in SKILL.md: missing.md")

    def test_broken_link_other_md(self):
        self.write("RULES.md", "[y](nope/a.md#s)\n")
        self.assertFails("broken link in RULES.md: nope/a.md#s")

    def test_link_outside_package(self):
        self.write("../outside.md", "x")
        self.write("RULES.md", "[y](../outside.md)\n")
        self.assertFails("broken link in RULES.md: ../outside.md")

    def test_links_in_code_fences_ignored(self):
        self.write("RULES.md", "```\n[y](not-a-file.md)\n```\n")
        self.assertEqual(self.errs(), [])

    def test_bad_description(self):
        self.write("SKILL.md", SKILL.replace("description: Use when", "description: Runs"))
        self.assertFails("start with 'Use when'")

    def test_long_description(self):
        self.write("SKILL.md", SKILL.replace("helper servers.", "x" * 1100))
        self.assertFails("characters, over the 1024 limit")

    def test_no_description(self):
        self.write("SKILL.md", "\n".join(l for l in SKILL.split("\n") if not l.startswith("description:")))
        self.assertFails("description is missing")

    def test_no_frontmatter(self):
        self.write("SKILL.md", SKILL.split("---\n", 2)[2])
        self.assertFails("has no frontmatter")

    def test_skill_missing(self):
        os.remove(os.path.join(self.root, "SKILL.md"))
        self.assertFails("SKILL.md is missing")

    def test_name_mismatch(self):
        self.write("SKILL.md", SKILL.replace("name: helper-node-orchestrator", "name: other"))
        self.assertFails("does not match the folder name")

    def test_bad_name(self):
        self.write("SKILL.md", SKILL.replace("name: helper-node-orchestrator", "name: Bad_Name"))
        self.assertFails("is not a valid skill name")

    def test_allowed_tools(self):
        self.write("SKILL.md", SKILL.replace("allowed-tools: Bash(bash ${CLAUDE_SKILL_DIR}/scripts/orch *)\n", ""))
        self.assertFails("allowed-tools does not mention scripts/orch")

    def test_short_description(self):
        self.write("agents/openai.yaml", "interface:\n  short_description: too short\n")
        self.assertFails("short_description has 9 characters")

    def test_openai_missing(self):
        os.remove(os.path.join(self.root, "agents", "openai.yaml"))
        self.assertFails("openai.yaml is missing")

    def test_config_dir_not_required(self):
        self.assertFalse(os.path.exists(os.path.join(self.root, "config")))
        self.assertEqual(self.errs(), [])

    def test_usage_errors_exit_2(self):
        rc, out = self.main("--dir", os.path.join(self.tmp, "nope"))
        self.assertEqual(rc, 2)
        self.assertIn("is not a folder", out)
        rc, out = self.main("--dir", self.root, "--denylist", os.path.join(self.tmp, "missing.txt"))
        self.assertEqual(rc, 2)
        self.assertIn("not found; pass an existing file or drop --denylist", out)


class SiteDataTest(Base):
    def hits(self, text, deny=()):
        return [m for _, m in validate.scan(text, deny)]

    def test_private_ipv4_fails(self):
        for ip in (j("10", ".1.2.3"), j("10.252", ".21.1"), j("172", ".16.0.1"), j("172.31", ".255.255"),
                   j("192", ".168.0.7")):
            self.assertEqual(len(self.hits("host %s:22" % ip)), 1, ip)
            self.write("RULES.md", "ssh deploy@%s\n" % ip)
            self.assertFails("RULES.md:1: private IPv4 address %s" % ip)

    def test_allowed_ipv4(self):
        for text in ("10.252.20.0/24", "10.252.20.9", "192.0.2.11", "198.51.100.7", "203.0.113.250", "127.0.0.1",
                     "172.15.255.255", "172.32.0.1", "version 1.2.3.4.5", "build 10.0.26100.1", "999.10.1.1"):
            self.assertEqual(self.hits(text), [], text)

    def test_user_paths_fail(self):
        for p in (j("C", ":/Us", "ers/alice/x"), j("D", ":\\data\\shop"), j("e", ":/repo"), j("/ho", "me/alice/.ssh"),
                  j("/Us", "ers/alice"), j("/c/Us", "ers/alice"), j("C", ":/workspace/x"), j('"C', ':\\\\Users\\\\a"')):
            self.assertEqual(len(self.hits("path %s here" % p)), 1, p)

    def test_generic_paths_pass(self):
        for p in ("C:/work/shop", "c:\\work\\shop\\api", "C:/work", "C:/Program Files/Git/bin/bash.exe",
                  "C:\\ProgramData\\ssh\\sshd_config", "C:/Windows/System32/OpenSSH/ssh.exe", "/srv/app",
                  "~/.claude/skills", "https://shop.example/home/x", "/root/.nuget/packages", "file.txt:/x",
                  "GET /users/42"):
            self.assertEqual(self.hits("path %s here" % p), [], p)

    def test_phone_numbers(self):
        for p in (j("+", "15550100123"), j("+44 20", " 7946 0000"), j("+1-555", "-010-0123")):
            self.assertEqual(len(self.hits("call %s now" % p)), 1, p)
        for text in ("+1-555-0100", "a+15550100123", "+06:00", "1+2"):
            self.assertEqual(self.hits(text), [], text)

    def test_denylist_entries(self):
        d = self.write("../deny.txt", "# comment\n\nacme-site\nword:hx9\nword:zz-\nbeta/path\n")
        deny = validate.load_denylist(d)
        self.assertEqual([n for n, _ in deny], [3, 4, 5, 6])
        self.assertEqual(len(self.hits("see ACME-Site docs", deny)), 1)  # case-insensitive substring
        self.assertEqual(len(self.hits("node hx9 (x)", deny)), 1)
        self.assertEqual(len(self.hits("agentx-hx9", deny)), 1)
        self.assertEqual(self.hits("hx90 shx9 hx9b", deny), [])  # whole word only
        self.assertEqual(len(self.hits("zz-app", deny)), 1)
        self.assertEqual(self.hits("azz-app", deny), [])
        self.assertEqual(len(self.hits("beta\\path", deny)), 1)  # either slash
        self.write("RULES.md", "x\nuse acme-site\n")
        self.assertFails("RULES.md:2: site string from denylist line 3", denylist=d)

    def test_file_names_scanned(self):
        d = self.write("../deny.txt", "word:hx9\n")
        self.write("tests/fixtures/probe-hx9.txt", "clean\n")
        self.assertFails("tests/fixtures/probe-hx9.txt (file name): site string from denylist line 1", denylist=d)

    def test_denylist_file_itself_not_scanned(self):
        d = self.write("dev/site-denylist.txt", "acme-site\n")
        self.assertEqual(self.errs(denylist=d), [])
        self.assertEqual(self.errs(denylist=None), [])  # found by default under ROOT/dev/
        self.write("dev/notes.md", "acme-site\n")  # --dir scans every other file, dev/ included
        self.assertFails("dev/notes.md:1: site string from denylist line 1", denylist=None)

    def test_walk_skips_git_and_python_caches(self):
        bad = j("192", ".168.1.1")
        self.write(".git", "gitdir: %s\n" % bad)
        self.write("scripts/__pycache__/x.cpython-312.pyc", bad)
        self.write("scripts/y.pyc", bad)
        self.assertEqual(self.errs(), [])

    def test_hits_capped_per_file(self):
        self.write("RULES.md", "".join("%s\n" % j("10", ".0.0.%d" % i) for i in range(1, 9)))
        e = self.errs()
        self.assertEqual(len(e), validate.MAX_HITS + 1, e)
        self.assertIn("RULES.md: 3 more site-data hits", e[-1])

    def test_self_clean(self):
        d = os.path.join(PKG, validate.DENYLIST)
        deny = validate.load_denylist(d) if os.path.isfile(d) else []
        for p in (validate.__file__, os.path.abspath(__file__)):
            with open(p, encoding="utf-8") as f:
                self.assertEqual(list(validate.scan(f.read(), deny)), [], p)


class FakeRun:
    """Canned git answers; records every call."""
    def __init__(self, files, ignored):
        self.files, self.ignored, self.calls = files, ignored, []

    def __call__(self, cmd, input=None, **kw):
        self.calls.append((cmd[3:], input))
        out = {"rev-parse": "true\n", "ls-files": "\0".join(self.files) + "\0"}.get(cmd[3], "")
        if cmd[3] == "check-attr":
            out = "".join("%s\0export-ignore\0%s\0" % (p, "set" if p in self.ignored else "unspecified")
                          for p in input.split("\0") if p)
        return subprocess.CompletedProcess(cmd, 0, out, "")


class GitModeTest(Base):
    def test_fake_git_export_ignore(self):
        files = ["SKILL.md", "dev/a.md", "dev/sub/b.md", "tests/selftest-1.md", "tests/keep.py", "gone.md"]
        for f in files[:-1]:
            self.write(f, "x\n")
        run = FakeRun(files, {"dev/", "tests/selftest-1.md"})
        self.assertEqual(validate.git_files(self.root, run), ["SKILL.md", "tests/keep.py"])  # gone.md: deleted
        queried = run.calls[-1][1].split("\0")
        self.assertIn("dev/", queried)
        self.assertIn("dev/sub/", queried)

    def test_empty_checkout(self):
        run = FakeRun([], set())
        self.assertEqual(validate.git_files(self.root, run), [])
        self.assertNotIn("check-attr", [c[0][0] for c in run.calls])
        e = validate.validate(self.root, walk=False, denylist=self.empty, run=run)  # SKILL.md exists but is not listed
        self.assertTrue(any(x.startswith("SKILL.md is ignored by git or marked export-ignore") for x in e), e)

    def test_not_a_checkout(self):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        self.assertIsNone(validate.git_files(self.root, run))

        def missing(cmd, **kw):
            raise FileNotFoundError("git")
        self.assertIsNone(validate.git_files(self.root, missing))

    @unittest.skipUnless(HAS_GIT, "git not installed")
    def test_real_git_matches_archive(self):
        root = os.path.join(self.tmp, "checkout")  # any folder name works for a checkout
        shutil.copytree(self.root, root)
        bad = "acme-site\n"
        self.write(".gitattributes", "dev/ export-ignore\ntests/selftest-*.md export-ignore\n", root)
        self.write(".gitignore", "ignored.txt\n", root)
        self.write("dev/notes.md", bad, root)
        self.write("dev/sub/deep.md", bad, root)
        self.write("tests/selftest-1.md", bad, root)
        self.write("tests/keep.py", "x = 1\n", root)
        self.write("ignored.txt", bad, root)
        self.write("RULES.md", "# Rules\n[design](dev/notes.md)\n", root)
        deny = self.write("deny.txt", "acme-site\n", self.tmp)

        def git(*a):
            return subprocess.run(["git", "-c", "core.autocrlf=false", "-C", root] + list(a),
                                  capture_output=True, check=True).stdout
        git("init", "-q")
        expect = sorted([".gitattributes", ".gitignore", "SKILL.md", "RULES.md", "agents/openai.yaml",
                         "references/init.md", "tests/keep.py"])
        self.assertEqual(sorted(validate.git_files(root)), expect)  # untracked files count before git add
        git("add", "-A")
        tree = git("write-tree").decode().strip()
        with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", tree))) as t:
            shipped = sorted(m.name for m in t.getmembers() if m.isfile())
        self.assertEqual(shipped, expect)  # the validator's list is exactly what git archive ships
        e = validate.validate(root, denylist=deny)
        self.assertEqual(e, ["broken link in RULES.md: dev/notes.md; point it at a file that ships in the package."])
        self.write("RULES.md", "# Rules\n", root)
        self.assertEqual(validate.validate(root, denylist=deny), [])  # no name check for a checkout folder
        self.write("new.md", bad, root)  # untracked and not ignored: would ship once added
        self.assertEqual(validate.validate(root, denylist=deny),
                         ["new.md:1: site string from denylist line 1; replace it with a generic name such as "
                          "h11, app1 or shop."])


class LiveSkillTest(unittest.TestCase):
    """The shipped SKILL.md: within the limits, and its description makes agents load the skill for deploy and
    release questions too (a real run answered "can we deploy?" without it)."""

    def setUp(self):
        with open(os.path.join(PKG, "SKILL.md"), encoding="utf-8") as f:
            self.text = f.read()
        self.desc = validate.frontmatter(self.text)["description"]

    def test_passes_the_skill_checks(self):
        errs = []
        validate.check_skill(PKG, ["SKILL.md"], True, errs)
        self.assertEqual(errs, [])

    def test_description_triggers(self):
        d = self.desc.lower()
        for w in ("deploy", "released", "shipped", "ready for production", "only as a check", "can we deploy",
                  "is it safe to release", "orch setup", "tests", "builds", "lint", "helper servers", "offloading",
                  "blocked", "orch command", "never with local tests"):
            self.assertIn(w, d)

    def test_description_is_a_plain_yaml_scalar(self):
        value = next(x for x in self.text.splitlines() if x.startswith("description:"))[len("description:"):].strip()
        self.assertEqual(value, self.desc)  # one line, not quoted
        self.assertNotIn(": ", value)  # YAML would read a mapping there and the skill would not load
        self.assertNotIn(" #", value)  # YAML would cut the rest off as a comment


if __name__ == "__main__":
    unittest.main()
