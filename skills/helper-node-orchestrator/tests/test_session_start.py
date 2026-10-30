"""Tests for hooks/session_start.py, the optional SessionStart hook: one constant line when the session's folder is
inside an approved project root of $ORCH_HOME/guard-view.json, nothing anywhere else, never a failure.

Paths are generic examples (C:/work/shop, /srv/app). This file ships in the package and is scanned by
scripts/validate.py, so other drive letters and a bare drive root are built at runtime with j().
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(PKG, "hooks", "session_start.py")
README = os.path.join(PKG, "README.md")
sys.path.insert(0, os.path.join(PKG, "hooks"))
sys.path.insert(0, os.path.join(PKG, "scripts"))
import session_start as ss  # noqa: E402
import orch_config  # noqa: E402

ROOT = "C:/work/shop"
LINE = ss.LINE


def j(*parts):
    return "".join(parts)


def view(*roots):
    """A guard view in the shape orch_config.build_guard_view writes (dev/v2-contract.md)."""
    return {"schema": 1, "generated": "2026-10-30T09:00:00",
            "namespace": {"label": "io.orch.agent", "prefix": "agentx-"}, "hosts": [],
            "projects": [{"root": r, "store": r + "/.git/orch", "deploy": [], "rollback": [], "jobs": {}}
                         for r in roots]}


def payload(cwd):
    """A SessionStart payload as Claude Code sends it on stdin."""
    return json.dumps({"session_id": "s1", "transcript_path": "s1.jsonl", "hook_event_name": "SessionStart",
                       "source": "startup", "cwd": cwd}).encode()


def no_git(argv, **kw):
    """run= stand-in for orch_config: git says the folder is not a repository, so the store is <folder>/.orch."""
    return SimpleNamespace(returncode=128, stdout="",
                           stderr="fatal: not a git repository (or any of the parent directories): .git")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "orch-home")
        os.mkdir(self.home)
        self.env = {"ORCH_HOME": self.home}

    def write_view(self, data):
        if not isinstance(data, bytes):
            data = (data if isinstance(data, str) else json.dumps(data)).encode("utf-8")
        with open(os.path.join(self.home, "guard-view.json"), "wb") as f:
            f.write(data)

    def msg(self, cwd=None, raw=None, here="/srv/elsewhere"):
        """message() for a payload with cwd (or raw stdin bytes); here = the hook process's own folder."""
        if raw is None:
            raw = b"" if cwd is None else payload(cwd)
        return ss.message(raw, env=self.env, getcwd=lambda: here)

    def project(self, name):
        """A folder with a saved (not yet approved) orch setup, made by orch_config itself."""
        d = os.path.join(self.tmp, name)
        os.mkdir(d)
        p, _ = orch_config.init_project(start=d, env=self.env, run=no_git)
        orch_config.save_config(p, {})
        return p


class PathTest(unittest.TestCase):
    def test_spellings_normalise_alike(self):
        for p in ("C:\\Work\\Shop\\", "c:/work/shop", "C:/work//shop/", "C:/work/./shop", "C:/work/api/../shop",
                  "C:\\work/shop", " C:/WORK/SHOP "):
            self.assertEqual(ss.norm(p), "c:/work/shop", p)
        self.assertEqual(ss.norm("/srv/app/"), "/srv/app")
        self.assertEqual(ss.norm("/"), "/")
        self.assertEqual(ss.norm(j("C", ":/")), "c:")  # a drive root
        self.assertEqual(ss.norm("~/shop"), ss.norm(os.path.expanduser("~") + "/shop"))

    def test_not_a_path(self):
        for p in ("", "   ", "work/shop", ".", "C:work", "C:", None, 5, [ROOT], "C:/work/\x00shop"):
            self.assertEqual(ss.norm(p), "", repr(p))

    def test_inside(self):
        r = ss.norm(ROOT)
        for p in (ROOT, ROOT + "/api", ROOT + "/api/src/x.py", "/c/work/shop/api", "/mnt/c/work/shop",
                  "/cygdrive/c/work/shop/web"):
            self.assertTrue(ss.inside(ss.norm(p), r), p)
        for p in ("C:/work/shop2", "C:/work/sho", "C:/work", j("C", ":/"), "/srv/app", "/d/work/shop",
                  j("D", ":/work/shop"), "work/shop", ""):
            self.assertFalse(ss.inside(ss.norm(p), r), p)
        self.assertTrue(ss.inside(ss.norm("C:\\work\\shop\\api"), ss.norm("/c/work/shop")))  # a Git Bash root
        self.assertTrue(ss.inside("/srv/app", "/"))
        self.assertTrue(ss.inside(ss.norm("C:/work"), ss.norm(j("C", ":/"))))
        self.assertFalse(ss.inside("", ""))


class MessageTest(Base):
    def test_inside_an_approved_root(self):
        self.write_view(view(ROOT))
        for cwd in (ROOT, ROOT + "/api/src", "C:\\work\\shop", "c:\\WORK\\Shop\\api\\", "C:/work//shop/./web",
                    "C:\\work/shop\\api", "/c/work/shop/api", "/mnt/c/work/shop"):
            self.assertEqual(self.msg(cwd), LINE, cwd)

    def test_root_spelled_with_backslashes_and_capitals(self):
        self.write_view(view("C:\\Work\\Shop\\"))
        self.assertEqual(self.msg("c:/work/shop/web"), LINE)

    def test_outside_every_root(self):
        self.write_view(view(ROOT, "/srv/app"))
        for cwd in ("C:/work/shop2", "C:/work/sho", "C:/work", "C:/srv/app", j("D", ":/work/shop"), "/d/work/shop",
                    "/srv", "/srv/application", "/srv/apps/app"):
            self.assertIsNone(self.msg(cwd), cwd)

    def test_posix_root(self):
        self.write_view(view("/srv/app"))
        self.assertEqual(self.msg("/srv/app"), LINE)
        self.assertEqual(self.msg("/srv/app/web/"), LINE)
        self.assertIsNone(self.msg("/srv/apps"))

    def test_several_projects_give_one_line(self):
        self.write_view(view(ROOT, ROOT + "/api", "/srv/app"))  # a worktree inside the main tree: still one line
        self.assertEqual(self.msg(ROOT + "/api/src"), LINE)
        self.assertEqual(self.msg("/srv/app"), LINE)

    def test_missing_view(self):
        self.assertIsNone(self.msg(ROOT))
        self.env = {"ORCH_HOME": os.path.join(self.tmp, "no-such-home")}
        self.assertIsNone(self.msg(ROOT))

    def test_default_home(self):
        user = os.path.join(self.tmp, "user")
        os.makedirs(os.path.join(user, ".helper-orch"))
        with open(os.path.join(user, ".helper-orch", "guard-view.json"), "w", encoding="utf-8") as f:
            json.dump(view(ROOT), f)
        with mock.patch.object(ss.os.path, "expanduser", side_effect=lambda p: p.replace("~", user, 1)):
            self.assertEqual(ss.message(payload(ROOT), env={}, getcwd=lambda: "/srv/x"), LINE)
            self.assertIsNone(ss.message(payload(ROOT), env={"ORCH_HOME": self.home}, getcwd=lambda: "/srv/x"))

    def test_corrupt_view(self):
        bad = ["", "{", "null", "[]", '"view"', "1", "[" * 100000, b"\xff\xfe\x00{",
               json.dumps(view(ROOT))[:-1],
               json.dumps({"projects": [{"root": ROOT}]}),
               json.dumps({"schema": 2, "projects": [{"root": ROOT}]}),
               json.dumps({"schema": "1", "projects": [{"root": ROOT}]}),
               json.dumps({"schema": 1, "projects": {"root": ROOT}}),
               json.dumps({"schema": 1, "projects": [ROOT, None, 7, {"root": 5}, {"root": ""}, {"root": "work/shop"},
                                                     {"store": ROOT + "/.git/orch"}]})]
        for data in bad:
            self.write_view(data)
            self.assertIsNone(self.msg(ROOT), repr(data)[:60])

    def test_view_with_bom(self):
        self.write_view(b"\xef\xbb\xbf" + json.dumps(view(ROOT)).encode())  # Windows PowerShell writes one
        self.assertEqual(self.msg(ROOT), LINE)

    def test_oversized_view(self):
        data = json.dumps(view(ROOT))
        self.write_view(data + " " * 100)  # the part under the cap is valid JSON; the file is too big
        with mock.patch.object(ss, "MAX_VIEW", len(data) + 10):
            self.assertIsNone(self.msg(ROOT))
        with mock.patch.object(ss, "MAX_VIEW", len(data) + 100):
            self.assertEqual(self.msg(ROOT), LINE)  # exactly at the cap: still read

    def test_unreadable_view(self):
        os.mkdir(os.path.join(self.home, "guard-view.json"))
        self.assertIsNone(self.msg(ROOT))

    def test_payload_cwd_wins(self):
        self.write_view(view(ROOT))
        self.assertIsNone(self.msg("/srv/other", here=ROOT))
        self.assertEqual(self.msg(ROOT, here="/srv/other"), LINE)
        self.assertEqual(self.msg(raw=b"\xef\xbb\xbf" + payload(ROOT)), LINE)

    def test_falls_back_to_the_process_folder(self):
        self.write_view(view(ROOT))
        for raw in (b"", b"{not json", b"[1, 2]", b"null", b'{"cwd": 5}', b'{"cwd": "  "}', b'{"session_id": "s1"}',
                    b"[" * 100000, b"\xff\xfe"):
            self.assertEqual(self.msg(raw=raw, here=ROOT + "/api"), LINE, raw[:20])
            self.assertIsNone(self.msg(raw=raw, here="/srv/other"), raw[:20])

    def test_relative_cwd_joins_the_process_folder(self):
        self.write_view(view(ROOT))
        self.assertEqual(self.msg("api", here=ROOT), LINE)
        self.assertEqual(self.msg(".", here=ROOT), LINE)
        self.assertIsNone(self.msg("..", here=ROOT))

    def test_fast_with_a_large_view(self):
        self.write_view(view(*["C:/work/p%04d" % i for i in range(2000)]))
        times = []
        for _ in range(5):
            t = time.perf_counter()
            self.assertIsNone(self.msg("C:/work/other/api"))  # worst case: every root compared
            times.append(time.perf_counter() - t)
        self.assertLess(min(times), 0.1)


class ApprovalTest(Base):
    """The real writer (orch_config.write_guard_view) lists approved projects only; the hook follows it."""

    def test_unapproved_projects_ignored(self):
        shop, web = self.project("shop"), self.project("web")
        orch_config.write_guard_view(self.home, run=no_git)  # what plain orch init writes before any approval
        for p in (shop, web):
            self.assertIsNone(self.msg(str(p.root)), p.root)
            self.assertIsNone(self.msg(str(p.root / "api")), p.root)
        orch_config.trust_set(self.home, shop, orch_config.load_config(shop), {})
        orch_config.write_guard_view(self.home, run=no_git)
        self.assertEqual(self.msg(str(shop.root)), LINE)
        self.assertEqual(self.msg(shop.root.as_posix() + "/api"), LINE)
        self.assertIsNone(self.msg(str(web.root)))  # still unapproved, next to an approved one

    def test_edit_waiting_for_approval_keeps_the_line(self):
        shop = self.project("shop")
        orch_config.trust_set(self.home, shop, orch_config.load_config(shop), {})
        orch_config.save_config(shop, {"production": {"answer": "none"}})  # now differs from the approved hash
        orch_config.write_guard_view(self.home, run=no_git)
        self.assertEqual(self.msg(str(shop.root)), LINE)  # the last approved setup still holds


class MainTest(Base):
    def test_terminal_stdin_is_never_read(self):
        class Tty:
            touched = False

            def isatty(self):
                return True

            @property
            def buffer(self):
                Tty.touched = True
                return io.BytesIO(b"{}")
        with mock.patch.object(ss.sys, "stdin", Tty()):
            self.assertEqual(ss.read_stdin(), b"")
        self.assertFalse(Tty.touched)
        with mock.patch.object(ss.sys, "stdin", None):
            self.assertEqual(ss.read_stdin(), b"")

    def test_pipe_stdin(self):
        pipe = SimpleNamespace(isatty=lambda: False, buffer=io.BytesIO(payload(ROOT)))
        with mock.patch.object(ss.sys, "stdin", pipe):
            self.assertEqual(ss.read_stdin(), payload(ROOT))

        def boom(n):
            raise OSError("stdin gone")
        with mock.patch.object(ss.sys, "stdin", SimpleNamespace(isatty=lambda: False,
                                                                buffer=SimpleNamespace(read=boom))):
            self.assertEqual(ss.read_stdin(), b"")

    def test_main_never_raises(self):
        class Broken:
            def write(self, s):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError
        self.write_view(view(ROOT))
        with mock.patch.dict(os.environ, {"ORCH_HOME": self.home}), \
                mock.patch.object(ss, "read_stdin", return_value=payload(ROOT)), \
                mock.patch.object(ss.sys, "stdout", Broken()):
            ss.main()
        with mock.patch.object(ss, "read_stdin", return_value=b""), \
                mock.patch.object(ss, "message", side_effect=MemoryError):
            ss.main()


class ProcessTest(Base):
    """The script as Claude Code runs it: python -I <skill dir>/hooks/session_start.py, payload on stdin."""

    def penv(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        e.update(self.env)
        return e

    def run_hook(self, stdin=b"", cwd=None, hook=HOOK, isolated=True):
        cmd = [sys.executable] + (["-I"] if isolated else []) + [hook]
        return subprocess.run(cmd, input=stdin, capture_output=True, cwd=cwd, env=self.penv(), timeout=60)

    def shop(self):
        d = os.path.join(self.tmp, "shop")
        os.mkdir(d)
        self.write_view(view(d.replace("\\", "/")))  # orch writes forward slashes (Path.as_posix())
        return d

    def test_line_inside_the_project(self):
        d = self.shop()
        t = time.perf_counter()
        r = self.run_hook(payload(d))
        took = time.perf_counter() - t
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertEqual(r.stdout.decode("ascii").splitlines(), [LINE])
        self.assertLess(took, 3.0)  # far below the 5 s timeout of the registration (README.md)
        r = self.run_hook(payload(os.path.join(d, "api", "src")))
        self.assertEqual(r.stdout.decode("ascii").splitlines(), [LINE])

    def test_nothing_outside(self):
        self.shop()
        r = self.run_hook(payload(self.tmp))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_process_folder_without_payload(self):
        d = self.shop()
        r = self.run_hook(b"", cwd=d)
        self.assertEqual((r.returncode, r.stdout.decode("ascii").splitlines(), r.stderr), (0, [LINE], b""))
        r = self.run_hook(b"x" * (3 << 20), cwd=d)  # junk over the read limit: still the process folder
        self.assertEqual((r.returncode, r.stdout.decode("ascii").splitlines(), r.stderr), (0, [LINE], b""))
        r = self.run_hook(b"", cwd=self.tmp)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_never_fails(self):
        d = self.shop()
        os.remove(os.path.join(self.home, "guard-view.json"))
        os.mkdir(os.path.join(self.home, "guard-view.json"))  # unreadable view
        r = self.run_hook(payload(d))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        not_a_dir = os.path.join(self.tmp, "file")
        with open(not_a_dir, "w") as f:
            f.write("x")
        self.env = {"ORCH_HOME": not_a_dir}
        r = self.run_hook(b"\xff[" * 1000)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_exit_0_when_stdout_is_gone(self):
        d = self.shop()
        p = subprocess.Popen([sys.executable, "-I", HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=self.penv())
        p.stdout.close()  # nobody reads the line: writing it fails inside the hook
        p.stdin.write(payload(d))
        p.stdin.close()
        err = p.stderr.read()
        p.stderr.close()
        self.assertEqual((p.wait(timeout=60), err), (0, b""))

    def test_isolated_mode_ignores_planted_modules(self):
        d = self.shop()
        copy = os.path.join(self.tmp, "hookcopy")
        os.mkdir(copy)
        shutil.copy(HOOK, copy)
        with open(os.path.join(copy, "json.py"), "w", encoding="utf-8") as f:
            f.write("print('PLANTED')\nraise SystemExit(3)\n")
        hook = os.path.join(copy, "session_start.py")
        r = self.run_hook(payload(d), hook=hook)
        self.assertEqual((r.returncode, r.stdout.decode("ascii").splitlines()), (0, [LINE]))
        r = self.run_hook(payload(d), hook=hook, isolated=False)  # control: without -I the planted json.py runs
        self.assertIn(b"PLANTED", r.stdout)


class ContractTest(unittest.TestCase):
    def test_line(self):
        self.assertLessEqual(len(LINE), 300)
        self.assertNotIn("\n", LINE)
        LINE.encode("ascii")
        for w in ("helper-node-orchestrator", "test", "build", "lint", "deploy", "release", "production",
                  "orch journal start"):
            self.assertIn(w, LINE)

    def test_no_processes_network_or_package_imports(self):
        with open(HOOK, encoding="utf-8") as f:
            src = f.read()
        for mod in ("subprocess", "socket", "urllib", "http", "ssl", "orch_config", "orch", "guard"):
            self.assertNotRegex(src, r"(?m)^\s*(?:import|from)\s+%s\b" % mod)

    def test_readme_registration(self):
        with open(README, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("hooks.SessionStart", text)
        blocks = [b for b in re.findall(r"```json\n(.*?)```", text, re.S) if "session_start.py" in b]
        self.assertEqual(len(blocks), 1)
        entry = json.loads(blocks[0])
        self.assertEqual(entry["matcher"], "startup|resume|clear|compact")
        self.assertEqual(entry["hooks"], [{"type": "command", "command": "python",
                                           "args": ["-I", "<skill dir>/hooks/session_start.py"], "timeout": 5}])
        self.assertTrue(os.path.isfile(HOOK))


if __name__ == "__main__":
    unittest.main()
