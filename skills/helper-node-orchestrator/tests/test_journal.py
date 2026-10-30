"""orch_journal unit tests: task lifecycle, untracked records, stale tasks, parallel tasks, rotation, secret redaction,
resolution linking, MEMORY.md rendering, stats numbers, ORCH_JOURNAL=0, bad input, concurrent appends from threads,
torn lines and the command line. No network, ssh or docker: the only processes are local git in temp folders."""
import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import orch_config as oc  # noqa: E402
import orch_journal as oj  # noqa: E402

T0 = dt.datetime(2026, 10, 30, 9, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
TS_RX = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d")
TASK_RX = re.compile(r"t\d{8}-\d{6}-[0-9a-f]{4}")
# a heredoc that runs as printed: the marker ends its line, the JSON object is one line, EOF stands alone
HEREDOC = re.compile(r"^[ \t]*orch journal (start|add|end) <<'EOF'\n[ \t]*(\{.*\})\n[ \t]*EOF[ \t]*$", re.M)
ONE_LINE = re.compile(r"<<'EOF'[ \t]*\S")  # text after the marker: the shell never finds the EOF line
DOC = ROOT / "references" / "journal.md"


def at(**kw):
    return T0 + dt.timedelta(**kw)


def heredocs(text):
    """(action, JSON object) of every `orch journal ... <<'EOF'` heredoc in text that runs as printed."""
    return [(m.group(1), json.loads(m.group(2))) for m in HEREDOC.finditer(text)]


def doc_part(start, end):
    """The reference text between two of its headings."""
    return DOC.read_text(encoding="utf-8").split(f"\n## {start}\n", 1)[1].split(f"\n## {end}\n", 1)[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-journal-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p = oc.Project(self.tmp / "app", self.tmp / "store", True)
        (self.p.store / "state").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"ORCH_JOURNAL": "1", "GIT_CEILING_DIRECTORIES": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        for k in ("ORCH_PROJECT", "ORCH_SESSION", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)

    def state(self):
        return json.loads(oj.paths(self.p)["current"].read_text(encoding="utf-8"))

    def raw(self):
        d = oj.journal_dir(self.p)
        return "".join(f.read_text(encoding="utf-8") for f in sorted(d.glob("*.jsonl"))) + (
            (d / "MEMORY.md").read_text(encoding="utf-8") if (d / "MEMORY.md").exists() else "")

    def memory(self):
        return oj.paths(self.p)["memory"].read_text(encoding="utf-8")

    def run_task(self):
        """A finished task with every record type; returns (task-start record, task-end record)."""
        p = self.p
        s = oj.start(p, {"summary": "fix the cart tests", "request": "make the web tests pass",
                         "plan": ["reproduce", "fix", "orch ci"]}, now=T0)
        oj.add(p, {"type": "decision", "what": "run web-unit first", "why": "smallest job that fails",
                   "options": ["web-unit", "full ci"]}, now=at(seconds=30))
        oj.record_invocation(p, "run", ["run", "web-unit"], at(seconds=40), at(seconds=100), 1,
                             {"job": "web-unit", "node": "h12", "jobid": "j1", "result": "test-fail",
                              "reason": "2 tests failed", "wall_s": 55.0})
        oj.add(p, {"type": "step", "what": "fixed the date parsing | cart.ts", "outcome": "done"}, now=at(seconds=400))
        oj.add(p, {"type": "resolution", "for": 4, "how": "set TZ=UTC in the test setup", "outcome": "fixed"},
               now=at(seconds=410))
        oj.record_invocation(p, "ci", ["ci"], at(seconds=420), at(seconds=520), 0, {"result": "pass"})
        oj.add(p, {"type": "difficulty", "what": "slow image pull on h11", "detail": "registry far away"},
               now=at(seconds=530))
        oj.add(p, {"type": "note", "text": "the user wants one commit"}, now=at(seconds=540))
        oj.add(p, {"type": "plan", "steps": ["ship it"]}, now=at(seconds=550))
        e = oj.end(p, {"outcome": "done", "summary": "cart tests pass", "lessons": ["pin TZ in tests"],
                       "open": ["image pull speed"]}, now=at(seconds=600))
        return s, e


class LifecycleTest(Base):
    def test_full_task(self):
        s, e = self.run_task()
        task = s["task"]
        self.assertRegex(task, TASK_RX)
        self.assertEqual((s["seq"], s["type"], s["by"], s["plan"]), (1, "task-start", "agent", ["reproduce", "fix", "orch ci"]))
        self.assertNotIn("since_prev_s", s)
        recs = oj.read(self.p)
        self.assertEqual([r["seq"] for r in recs], list(range(1, 12)))
        self.assertEqual({r["task"] for r in recs}, {task})
        self.assertTrue(all(TS_RX.fullmatch(r["ts"]) and r["v"] == 1 for r in recs))
        self.assertEqual([r["type"] for r in recs], ["task-start", "decision", "step", "difficulty", "step", "resolution",
                                                     "step", "difficulty", "note", "plan", "task-end"])
        self.assertEqual(recs[1]["since_prev_s"], 30.0)
        run, diff = recs[2], recs[3]
        self.assertEqual((run["by"], run["verb"], run["args"], run["exit"], run["result"], run["duration_s"]),
                         ("orch", "run", ["run", "web-unit"], 1, "test-fail", 60.0))
        self.assertEqual((run["what"], run["details"]["node"]), ("orch run web-unit", "h12"))
        self.assertEqual((diff["by"], diff["class"], diff["step"], diff["detail"]), ("orch", "test-fail", 3, "2 tests failed"))
        self.assertEqual(diff["what"], "orch run web-unit exited 1 (test-fail)")
        self.assertIn("orch logs j1", diff["fix"])
        self.assertEqual((e["seq"], e["outcome"], e["duration_s"], e["started"]), (11, "done", 600.0, s["ts"]))
        self.assertEqual(e["counts"], {"steps": 3, "decisions": 1, "difficulties": 2, "resolved": 1, "unresolved": 1,
                                       "orch_seconds": 160.0})
        self.assertEqual(e["memory"], oj.paths(self.p)["memory"].as_posix())
        st = self.state()
        self.assertEqual((st["current"], st["open"]), (None, {}))
        self.assertIn(f"## {task} - ", self.memory())
        after = oj.add(self.p, {"type": "note", "text": "later"})
        self.assertEqual((after["task"], after["seq"]), ("untracked", 1))

    def test_untracked_records(self):
        a = oj.add(self.p, {"type": "decision", "what": "use h11", "why": "it is idle"}, now=T0)
        b = oj.add(self.p, {"type": "difficulty", "what": "h12 slow"}, now=at(seconds=5))
        c = oj.record_invocation(self.p, "nodes", ["nodes"], at(seconds=6), at(seconds=8), 0, {})
        r = oj.add(self.p, {"type": "resolution", "for": 2, "how": "waited"}, now=at(seconds=9))
        self.assertEqual([(x["task"], x["seq"]) for x in (a, b, c, r)],
                         [("untracked", 1), ("untracked", 2), ("untracked", 3), ("untracked", 4)])
        self.assertEqual((b["since_prev_s"], c["result"], c["duration_s"]), (5.0, "ok", 2.0))
        s = oj.start(self.p, {"summary": "next task"}, now=at(seconds=10))
        self.assertEqual(s["seq"], 1)
        self.assertEqual(oj.add(self.p, {"type": "note", "text": "x"}, now=at(seconds=11))["task"], s["task"])
        self.assertEqual(len(oj.read(self.p, task="untracked")), 4)
        self.assertEqual(oj.stats(self.p)["unresolved"], [])  # untracked difficulties are not task debts

    def test_stale_task_closed_as_abandoned(self):
        t1 = oj.start(self.p, {"summary": "first"}, now=T0)["task"]
        oj.add(self.p, {"type": "note", "text": "working"}, now=at(hours=1))
        t2 = oj.start(self.p, {"summary": "second"}, now=at(hours=2))
        self.assertEqual((t2["open"], t2.get("closed")), ([t1], None))  # idle 1 h: still open (maybe another agent)
        oj.end(self.p, {"outcome": "done"}, now=at(hours=3))
        self.assertEqual(self.state()["current"], None)
        t3 = oj.start(self.p, {"summary": "third"}, now=at(hours=13, minutes=30))
        self.assertEqual((t3["closed"], t3["open"]), ([t1], []))
        end1 = [r for r in oj.read(self.p, task=t1) if r["type"] == "task-end"]
        self.assertEqual(len(end1), 1)
        self.assertEqual((end1[0]["outcome"], end1[0]["by"], end1[0]["duration_s"], end1[0]["summary"]),
                         ("abandoned", "orch", 3600.0, "first"))
        self.assertIn("12 h", end1[0]["reason"])
        self.assertRegex(self.memory(), rf"## {t1} - .* - abandoned - 1h 00m")
        self.assertIn("**Closed by orch:** no orch journal end within 12 h", self.memory())
        self.assertEqual(self.state()["current"], t3["task"])

    def test_parallel_tasks_by_id(self):
        t1 = oj.start(self.p, {"summary": "agent one"}, now=T0)["task"]
        t2 = oj.start(self.p, {"summary": "agent two"}, now=at(seconds=1))["task"]
        self.assertEqual(oj.add(self.p, {"type": "note", "text": "one", "task": t1}, now=at(seconds=2))["task"], t1)
        self.assertEqual(oj.add(self.p, {"type": "note", "text": "two"}, now=at(seconds=3))["task"], t2)
        self.assertEqual(oj.end(self.p, {"outcome": "done", "task": t1}, now=at(seconds=4))["task"], t1)
        self.assertEqual(self.state()["current"], t2)
        self.assertEqual(oj.end(self.p, {"outcome": "partial"}, now=at(seconds=5))["task"], t2)
        with self.assertRaisesRegex(oj.JournalError, "is not open"):
            oj.add(self.p, {"type": "note", "text": "x", "task": t1})


class RotationTest(Base):
    def test_rotation_keeps_every_record_in_order(self):
        with mock.patch.object(oj, "ROTATE_BYTES", 600):
            task = oj.start(self.p, {"summary": "many notes"})["task"]
            for i in range(30):
                oj.add(self.p, {"type": "note", "text": f"note {i} " + "x" * 60})
            e = oj.end(self.p, {"outcome": "done"})
        d = oj.journal_dir(self.p)
        rotated = sorted(f.name for f in d.glob("journal-*.jsonl"))
        self.assertGreaterEqual(len(rotated), 3)
        self.assertTrue(all(re.fullmatch(r"journal-\d{8}-\d{6}(-\d+)?\.jsonl", n) for n in rotated))
        for f in d.glob("*.jsonl"):
            for ln in f.read_text(encoding="utf-8").splitlines():
                json.loads(ln)  # no torn line anywhere
        self.assertEqual([r["seq"] for r in oj.read(self.p, task=task)], list(range(1, 33)))
        self.assertEqual([r["seq"] for r in oj.read(self.p, last=5)], [28, 29, 30, 31, 32])
        self.assertEqual(len(oj.read(self.p, last=40)), 32)
        self.assertEqual(e["counts"]["steps"], 0)
        self.assertIn(f"## {task} - ", self.memory())
        self.assertIn("#31 note 29", self.memory())
        oldest = d / rotated[0]
        n_old = len(oldest.read_text(encoding="utf-8").splitlines())
        month_ago = oldest.stat().st_mtime - 30 * 86400
        os.utime(oldest, (month_ago, month_ago))  # stats --days skips files last written before the window
        self.assertEqual(oj.stats(self.p, days=7)["records"], 32 - n_old)
        self.assertEqual(oj.stats(self.p)["records"], 32)


class RedactionTest(Base):
    KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU\n-----END OPENSSH PRIVATE KEY-----"
    AWS, GH, SLACK = "AKIA" + "ABCDEFGHIJKLMNOP", "ghp_" + "a1" * 18, "xoxb-" + "1234567890-abcdef"
    JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".c2lnbmF0dXJlLXZhbHVl"

    def test_agent_text_redacted_everywhere(self):
        p = self.p
        oj.start(p, {"summary": "rotate keys", "request": f"use this key {self.KEY} please"})
        oj.add(p, {"type": "decision", "what": "log in with password=hunter2pass", "why": f"aws {self.AWS} works"})
        oj.add(p, {"type": "note", "text": f"gh {self.GH} slack {self.SLACK} jwt {self.JWT}"})
        oj.add(p, {"type": "difficulty", "what": "clone failed", "detail": "https://deploy:s3cr3tpw@git.example.com/x.git"})
        oj.add(p, {"type": "step", "what": "curl -H 'Authorization: Bearer abcdefghijklmnop'", "extra": {"pwd": "Zx9pw77"}})
        oj.end(p, {"outcome": "done", "lessons": ["token: tok_live_998877 must not be logged"]})
        raw = self.raw()
        for secret in ("b3BlbnNzaC1rZXktdjEAAAAABG5vbmU", "hunter2pass", self.AWS, self.GH, self.SLACK, self.JWT,
                       "s3cr3tpw", "abcdefghijklmnop", "tok_live_998877"):
            self.assertNotIn(secret, raw)
        for mark in ("[REDACTED-PRIVATE-KEY]", "[REDACTED-AWS-KEY]", "[REDACTED-GH-TOKEN]", "[REDACTED-SLACK]",
                     "[REDACTED-JWT]", "Bearer [REDACTED]", "https://deploy:[REDACTED]@git.example.com"):
            self.assertIn(mark, raw)
        self.assertIn('"pwd": "Zx9pw77"', raw)  # a JSON key alone is not a secret pattern; values named pwd= are

    def test_args_redacted(self):
        args = ["run", "api-key-test", "--token", "abc123", "--password=pw1", "-e", "API_KEY=zzz", "DB_PASS=qq",
                "-u", "admin:pw2", "--", "sh", "-c",
                "curl -H 'Authorization: Bearer abcdefghijkl' https://u:p4ssw0rd@example.com/x"]
        want = ["run", "api-key-test", "--token", "***", "--password=***", "-e", "API_KEY=***", "DB_PASS=***",
                "-u", "admin:***", "--", "sh", "-c",
                "curl -H 'Authorization: Bearer [REDACTED]' https://u:[REDACTED]@example.com/x"]
        self.assertEqual(oj.redact_args(args), want)
        oj.record_invocation(self.p, "run", args, T0, at(seconds=3), 2,
                             {"reason": "login failed: password=Hunter22", "result": "usage"})
        raw = self.raw()
        for secret in ("abc123", "pw1", "zzz", "=qq", "pw2", "p4ssw0rd", "abcdefghijkl", "Hunter22"):
            self.assertNotIn(secret, raw)
        rec = oj.read(self.p)[0]
        self.assertEqual(rec["args"], want)
        self.assertTrue(rec["what"].startswith("orch run api-key-test --token ***"))

    def test_scrub_leaves_plain_text(self):
        for s in ("orch run api-unit", "sha256:" + "a" * 64, "max_tokens=500", "TZ=UTC", "the token expired",
                  "C:/work/shop", "basic configuration"):
            self.assertEqual(oj.scrub(s), s)
        self.assertEqual(oj.scrub("pwd=abc password: 'x1'"), "pwd=[REDACTED] password: '[REDACTED]'")
        self.assertEqual(oj.scrub("-----BEGIN RSA PRIVATE KEY-----\nAAAA truncated"), "[REDACTED-PRIVATE-KEY]")

    # Q25 (R38 parity): every spelling orch init's command and URL check refuses, and the ones review round 2 found
    # it should (DB_PASS=, SECRET_KEY=, mysql -pX, docker login -p X, ...), each with the secret that must go
    S = "Hunter2xyzQ"
    CASES = [(f"./scripts/rollback.sh --password {S}", S), (f"./scripts/deploy.sh --token {S}", S),
             (f"./scripts/deploy.sh --api-token '{S}'", S), (f"./scripts/deploy.sh --password={S}", S),
             (f"./deploy.sh --api-key {S}", S), (f'./deploy.sh --db-password "{S} 2"', S), (f"./deploy.sh --pw {S}", S),
             (f"./deploy.sh --passphrase {S}", S), (f"openssl enc -pass pass:{S}", S),
             ("./deploy.sh --token ab1", "ab1"),
             (f"./deploy.sh --client-secret={S}", S), (f"./deploy.sh --credentials {S}", S),
             (f"curl -fsS -u ops:{S} https://hooks.example/notify", S),
             (f"curl --user=ops:{S} https://hooks.example/n", S),
             (f"curl --user ops:{S} https://hooks.example/n", S), (f"curl -uops:{S} https://hooks.example/n", S),
             (f"wget --http-user=ops:{S} https://hooks.example/n", S),
             ("curl -u 12345:67890 https://api.example/s", "67890"),
             (f"used -u deploy:{S} for the health check", S), ("curl -u ops:ops https://hooks.example/n", "ops:ops"),
             (f"sshpass -p {S} ssh deploy@192.0.2.20 ./deploy.sh", S), (f"sshpass -p{S} ssh deploy@192.0.2.20 true", S),
             (f"sshpass -p '{S}' ssh h11", S), (f"mysql -h db -uroot -p{S} shop", S), (f"mysqldump -p'{S}' shop", S),
             (f"docker login -u ci -p {S} registry.example", S), (f"docker login -u ci -p={S} registry.example", S),
             (f"podman login -p {S} registry.example", S), (f"redis-cli -h db -a {S} ping", S),
             (f"DEPLOY_PASSWORD={S} ./scripts/deploy.sh", S), (f"DB_PASSWORD={S} ./scripts/deploy.sh", S),
             (f"PGPASSWORD={S} psql -h db.example -c 'select 1'", S), (f'PASSWORD="{S}" ./scripts/deploy.sh', S),
             (f"DB_PASS={S} ./deploy.sh", S), (f"SMTP_PASS={S} ./deploy.sh", S), (f"SECRET_KEY={S} ./deploy.sh", S),
             (f"DJANGO_SECRET_KEY={S} ./deploy.sh", S), (f"PRIVATE_KEY={S} ./deploy.sh", S),
             (f"STRIPE_KEY={S} ./deploy.sh", S), (f"API_KEY_ID={S} ./deploy.sh", S), ("SECRET_KEY=ab1 ./x", "ab1"),
             (f"DB_CREDENTIALS=ops:{S} ./deploy.sh", S), (f"MYSQL_PWD={S} mysql", S), (f"BASIC_AUTH={S} ./x", S),
             (f"curl -H 'X-Auth: {S}' https://hooks.example/n", S),
             (f"curl -H 'X-Api-Key: {S}' https://hooks.example/n", S),
             (f'{{"password": "{S}"}}', S), (f"login failed: password={S}", S), (f"error: token={S}", S),
             (f"PASSWORD='ab&cd {S}'", S),
             ("curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA==' https://hooks.example/n", "dXNlcjpwYXNzd29yZA=="),
             (f"curl -H 'Authorization: token {S}' https://api.example/x", S),
             (f"curl https://ops:{S}@hooks.example/notify", S), (f"git clone https://deploy:{S}@git.example/x.git", S),
             (f"curl -X POST https://hooks.slack.com/services/T000/B000/{S}", S),
             (f"curl https://discord.com/api/webhooks/123/{S}", S),
             (f"curl https://api.telegram.org/bot123456:{S}/sendMessage", S),
             (f"https://shop.example/health?key={S}", S), (f"https://shop.example/h?code={S}&x=1", S),
             (f"https://shop.example/h?sig={S}", S), (f"https://shop.example/h?token={S}&page=2", S),
             ("glpat-abcdefghij0123456789", "abcdefghij0123456789"), ("ghp_abcdefghij0123", "abcdefghij0123"),
             # orch init's own error texts echo the refused answer: a two-line command's repr, a curl as a health URL
             (f"deploy.command must be one line (got './scripts/deploy.sh --password {S}\\n./scripts/notify.sh')", S),
             (f"deploy.health: 'curl -u ops:{S} https://shop.example/health' is not an http(s) URL", S)]
    KEPT = ["./scripts/deploy.sh --token-file /run/secrets/deploy",
            "./scripts/deploy.sh --password-file /run/secrets/p",
            "PGPASSFILE=/run/secrets/pgpass psql -h db.example", "TOKEN_FILE=/run/secrets/t ./scripts/deploy.sh",
            "docker run --rm -u 1000:1000 app:1 ./migrate", "docker exec -u app:app web sh",
            "curl -u ops https://hooks.example/notify", './scripts/deploy.sh --token "$DEPLOY_TOKEN"',
            "PGPASSWORD=$PGPW psql -h db.example", "curl -u ops:$OPS_PW https://hooks.example/notify",
            "./scripts/deploy.sh --env production", "docker run -p 8080:80 app:1", "ssh -p 2222 deploy@192.0.2.20",
            "mysql -p -h db shop", 'mysql -p"$DBPW" shop', "KEY_PATH=/etc/app/tls.pem ./x", "./x --key-file /run/k",
            "SSH_KEY_FILE=~/.ssh/id_ed25519 ./x", "docker login --password-stdin registry.example",
            'redis-cli -a "$REDIS_PW" ping', "BUILD_ENV=production ./x", "https://shop.example/health?ver=2",
            "Pass: 120, Fail: 0", "ssh: Permission denied (publickey)", "key: value", "the auth step",
            "author: someone", "highs_and_lows_report", "password=<your password>", "--password -",
            "sudo -u deploy ./x"]

    def test_scrub_masks_option_user_url_and_name_forms(self):
        """Q25: the value goes, the option, name or URL around it stays readable."""
        for text, secret in self.CASES:
            with self.subTest(text=text):
                self.assertNotIn(secret, oj.scrub(text))
        S = self.S
        for text, want in ((f"./scripts/rollback.sh --password {S} --env prod", "./scripts/rollback.sh --password "
                                                                                "[REDACTED] --env prod"),
                           (f"curl -fsS -u ops:{S} https://hooks.example/n", "curl -fsS -u ops:[REDACTED] "
                                                                              "https://hooks.example/n"),
                           (f"sshpass -p{S} ssh h11", "sshpass -p[REDACTED] ssh h11"),
                           (f"DB_PASS={S} SECRET_KEY='{S}' ./deploy.sh", "DB_PASS=[REDACTED] SECRET_KEY='[REDACTED]' "
                                                                          "./deploy.sh"),
                           (f"mysql -uroot -p{S} shop", "mysql -uroot -p[REDACTED] shop"),
                           (f"https://shop.example/h?code={S}&ver=2", "https://shop.example/h?code=[REDACTED]&ver=2")):
            self.assertEqual(oj.scrub(text), want)
        for text in self.KEPT:
            self.assertEqual(oj.scrub(text), text)

    def test_scrub_masks_whatever_the_init_check_refuses(self):
        """Q25 parity with orch_init._SECRET (the check of deploy, rollback and alert commands and URLs): on this table
        and on init's own refused rows, every match of that check left in the scrubbed text is a masked value."""
        import orch_init  # noqa: PLC0415 - the check this parity is about
        rows = [t for t, _ in self.CASES]
        try:
            import test_init  # noqa: PLC0415 - its TestSecrets.REFUSED rows, as the init owner extends them
            rows += list(getattr(getattr(test_init, "TestSecrets", None), "REFUSED", None) or [])
        except ImportError:
            pass
        checked = 0
        for text in rows:
            if not orch_init._SECRET.search(text):
                continue
            checked += 1
            s = oj.scrub(text)
            for m in orch_init._SECRET.finditer(s):
                self.assertIn("[REDACTED", s[m.start():m.end() + len("[REDACTED")], f"{text!r} -> {s!r}")
        self.assertGreater(checked, 20)

    def test_secret_forms_never_reach_journal_or_memory(self):
        """Q25: agent text and orch's own error details quoting a command keep no secret in journal.jsonl or
        MEMORY.md."""
        p, S = self.p, self.S
        oj.start(p, {"summary": "fix the deploy", "request": f"run ./rollback.sh --password {S}1 first"})
        oj.add(p, {"type": "difficulty", "what": "health check 401",
                   "detail": f"curl -u ops:{S}2 https://shop.example/h"})
        oj.add(p, {"type": "decision", "what": f"use sshpass -p {S}3 on the old host", "why": f"DB_PASS={S}4 is set"})
        oj.record_invocation(p, "init", ["init", "--apply"], T0, at(seconds=1), 2,
                             {"error": f"deploy.command must be one line (got './deploy.sh --password {S}5\\n./x')",
                              "answer_keys": ["deploy"]})
        oj.end(p, {"outcome": "blocked", "lessons": [f"docker login -u ci -p {S}6 registry.example fails"]})
        raw = self.raw()
        self.assertNotIn(S, raw)
        for mark in ("--password [REDACTED]", "-u ops:[REDACTED]", "sshpass -p [REDACTED]", "DB_PASS=[REDACTED]",
                     "docker login -u ci -p [REDACTED]"):
            self.assertIn(mark, raw)

    # council 2 MF7: bare API keys (no NAME= or flag in front), built here so no scanner mistakes them for real ones
    KEYS = ["sk-" + "A1b2C3d4E5" * 5, "sk-proj-" + "Ab1_cD2-eF" * 9, "sk-ant-api03-" + "Zx9_Yw8-Vu" * 9 + "AA",
            "sk-svcacct-" + "Q1w2E3r4T5" * 6, "AIza" + "SyA1b2C3d4" * 3 + "E5f6G", "ghp_" + "a1B2c3D4e5" * 4,
            "xoxb-" + "1234567890-" * 3 + "AbCdEf", "AKIA" + "ABCDEFGHIJKLMNOP"]

    def test_scrub_masks_bare_api_keys(self):
        for k in self.KEYS:
            for text in (k, f"the key {k} leaked", f"export OPENAI_KEY={k}", f'{{"llm": "{k}"}}', f"({k})",
                         f"https://maps.example/geo?ll=1,2&k={k}", f"error 401 for '{k}'"):
                with self.subTest(text=text):
                    s = oj.scrub(text)
                    self.assertNotIn(k[6:], s)
                    self.assertIn("[REDACTED", s)
        self.assertEqual(oj.scrub(f"use {self.KEYS[1]} here"), "use [REDACTED-API-KEY] here")
        for text in ("task-start", "risk-assessment-for-the-quarterly-release", "desk-booking-weekly-report-generator",
                     "sk-short", "the sk- prefix", "AIza", "AIzaShort1", "orch journal add task-end"):
            self.assertEqual(oj.scrub(text), text)

    def test_scrub_time_is_linear(self):
        """scrub() reads up to 64 KB of orch output: no rule may take quadratic time on it."""
        n = 1 << 16
        for s in ("x-" * (n // 2), "a." * (n // 2), "--pass-" * (n // 7), "eyJ" * (n // 3), "mysql " * (n // 6),
                  "-u a:b " * (n // 7), "token_" * (n // 6), "xa://b:" * (n // 7), "'" * n, "sk-" * (n // 3),
                  " sk-a" * (n // 5), "AIza" * (n // 4), " AIzaB" * (n // 6), "sk-" + "a" * n):
            t = time.perf_counter()
            oj.scrub(s)
            self.assertLess(time.perf_counter() - t, 1.5, s[:12])


class ResolutionTest(Base):
    def test_linking_and_unresolved(self):
        p = self.p
        task = oj.start(p, {"summary": "two problems"}, now=T0)["task"]
        oj.add(p, {"type": "difficulty", "what": "lint fails."}, now=at(seconds=1))
        oj.add(p, {"type": "difficulty", "what": "e2e flaky"}, now=at(seconds=2))
        oj.add(p, {"type": "resolution", "for": "#2", "how": "fixed the import."}, now=at(seconds=3))
        oj.add(p, {"type": "resolution", "for": 3, "how": "retried with more time", "outcome": "failed"},
               now=at(seconds=4))
        with self.assertRaisesRegex(oj.JournalError, r"newest record of .* is #5"):
            oj.add(p, {"type": "resolution", "for": 99, "how": "x"})
        with self.assertRaisesRegex(oj.JournalError, r"#1 is not a difficulty .*#2, #3"):
            oj.add(p, {"type": "resolution", "for": 1, "how": "x"})
        e = oj.end(p, {"outcome": "partial"}, now=at(seconds=9))
        self.assertEqual((e["counts"]["resolved"], e["counts"]["unresolved"]), (1, 1))
        md = self.memory()
        self.assertIn("- #2 lint fails. Resolved by #4: fixed the import.", md)
        self.assertIn("- #3 e2e flaky. Tried #5: retried with more time (failed). UNRESOLVED", md)
        u = oj.stats(p)["unresolved"]
        self.assertEqual([(x["task"], x["seq"], x["what"]) for x in u], [(task, 3, "e2e flaky")])


class MemoryTest(Base):
    def test_render_sections(self):
        s, e = self.run_task()
        md = oj.render_task_md(oj.read(self.p, task=s["task"]))
        lines = md.splitlines()
        self.assertRegex(lines[0], rf"^## {s['task']} - \d{{4}}-\d\d-\d\d \d\d:\d\d - done - 10m 00s$")
        for want in ("**Summary:** cart tests pass", "**Request:** make the web tests pass", "**Plan:**", "1. reproduce",
                     "3. orch ci", "**Plan update (#10):**", "1. ship it", "**Decisions:**",
                     "- #2 run web-unit first. Why: smallest job that fails. Options: web-unit, full ci.",
                     "**Steps:**", "| step | by | outcome | time |", "|---|---|---|---|",
                     "| #3 orch run web-unit | orch | test-fail (exit 1) | 1m 00s |",
                     "| #5 fixed the date parsing \\| cart.ts | agent | done | ~5m 00s |",
                     "| #7 orch ci | orch | pass | 1m 40s |", "**Difficulties:**",
                     "- #4 orch run web-unit exited 1 (test-fail): 2 tests failed. Resolved by #6: set TZ=UTC in the "
                     "test setup (fixed).",
                     "- #8 slow image pull on h11: registry far away. UNRESOLVED", "**Notes:**",
                     "- #9 the user wants one commit", "**Lessons:**", "- pin TZ in tests", "**Open items:**",
                     "- image pull speed",
                     "Counts: 3 steps, 1 decision, 2 difficulties (1 resolved, 1 unresolved), orch time 2m 40s."):
            self.assertIn(want, lines)
        self.assertEqual(oj.render_task_md([]), "")
        self.assertTrue(self.memory().startswith(oj.MEMORY_HEAD))
        self.assertIn(md, self.memory())

    def test_open_task_and_second_section(self):
        s = oj.start(self.p, {"summary": "one"}, now=T0)
        oj.add(self.p, {"type": "note", "text": "n"}, now=at(seconds=90))
        self.assertIn(" - open - 1m 30s", oj.render_task_md(oj.read(self.p, task=s["task"])))
        oj.end(self.p, {"outcome": "blocked"}, now=at(seconds=95))
        oj.start(self.p, {"summary": "two"}, now=at(seconds=100))
        oj.end(self.p, {"outcome": "done"}, now=at(seconds=101))
        md = self.memory()
        self.assertEqual((md.count("\n## t"), md.count("# Project journal")), (2, 1))
        self.assertIn("**Summary:** one", md)


class StatsTest(Base):
    def test_numbers(self):
        p = self.p
        oj.start(p, {"summary": "old"}, now=at(days=-40))
        oj.end(p, {"outcome": "done"}, now=at(days=-40, seconds=60))
        oj.start(p, {"summary": "a"}, now=T0)
        for i, (wall, node, code) in enumerate(((9, "h11", 0), (19, "h12", 1), (29, "h11", 0))):
            t = at(minutes=i)
            oj.record_invocation(p, "run", ["run", "web-unit"], t, t + dt.timedelta(seconds=wall + 1), code,
                                 {"job": "web-unit", "node": node, "wall_s": wall,
                                  "result": "pass" if code == 0 else "test-fail"})
        oj.add(p, {"type": "resolution", "for": 4, "how": "fixed"}, now=at(minutes=4))
        oj.record_invocation(p, "ci", ["ci"], at(minutes=5), at(minutes=5, seconds=100), 0, {"result": "pass"})
        oj.end(p, {"outcome": "done"}, now=at(seconds=600))
        oj.start(p, {"summary": "b"}, now=at(hours=1))
        oj.record_invocation(p, "run", ["run", "api-unit"], at(hours=1), at(hours=1, seconds=5), 5,
                             {"job": "api-unit", "node": "h12", "result": "infra", "reason": "ssh dropped"})
        oj.end(p, {"outcome": "failed"}, now=at(hours=1, seconds=1200))
        open_task = oj.start(p, {"summary": "c"}, now=at(hours=2))["task"]
        s = oj.stats(p, now=at(hours=3))
        self.assertEqual(s["tasks"], {"total": 4, "open": 1, "by_outcome": {"done": 2, "failed": 1},
                                      "seconds": {"n": 3, "mean": 620.0, "p50": 600.0, "p95": 1200.0}})
        self.assertEqual(s["verbs"]["run"], {"n": 4, "mean": 16.25, "p50": 10.0, "p95": 30.0, "failed": 2})
        self.assertEqual(s["verbs"]["ci"], {"n": 1, "mean": 100.0, "p50": 100.0, "p95": 100.0, "failed": 0})
        self.assertEqual(s["jobs"]["web-unit"], {"n": 3, "mean": 19.0, "p50": 19.0, "p95": 29.0,
                                                 "results": {"pass": 2, "test-fail": 1}})
        self.assertEqual(s["jobs"]["api-unit"]["n"], 1)  # no wall_s: the step duration counts
        self.assertEqual(s["failure_classes"], {"test-fail": 1, "infra": 1})
        self.assertEqual(s["helpers"], {"h11": 2, "h12": 2})
        self.assertEqual([(x["seq"], x["class"]) for x in s["unresolved"]], [(3, "infra")])
        self.assertEqual(s["top_difficulties"][0]["count"], 1)
        self.assertNotIn(open_task, [x["task"] for x in s["unresolved"]])
        w = oj.stats(p, days=7, now=at(hours=3))
        self.assertEqual((w["tasks"]["by_outcome"], w["tasks"]["total"], w["days"]), ({"done": 1, "failed": 1}, 3, 7))
        lines = oj.stats_lines(s)
        self.assertEqual(lines[0], f"journal stats (all time): {s['records']} records")
        self.assertIn("tasks 4 (done 2, failed 1; open 1); duration 3x mean 10m 20s, p50 10m 00s, p95 20m 00s", lines)
        self.assertIn("job web-unit: 3x mean 19s, p50 19s, p95 29s (pass 2, test-fail 1)", lines)

    def test_empty_journal(self):
        s = oj.stats(self.p)
        self.assertEqual((s["records"], s["tasks"]["total"], s["verbs"], s["unresolved"]), (0, 0, {}, []))
        self.assertIn("tasks 0 (none ended; open 0); duration no data", oj.stats_lines(s))


class DisabledTest(Base):
    def test_off_writes_nothing(self):
        os.environ["ORCH_JOURNAL"] = "0"
        self.assertTrue(oj.start(self.p, {"summary": "x"})["disabled"])
        self.assertTrue(oj.add(self.p, {"type": "note", "text": "x"})["disabled"])
        self.assertTrue(oj.end(self.p, {"outcome": "done"})["disabled"])
        self.assertIsNone(oj.record_invocation(self.p, "run", ["run", "t-ok"], T0, at(seconds=1), 0, {}))
        self.assertFalse(oj.journal_dir(self.p).exists())
        self.assertFalse(oj.paths(self.p)["current"].exists())
        with self.assertRaisesRegex(oj.JournalError, 'needs "summary"'):
            oj.start(self.p, {})


class BadInputTest(Base):
    def test_messages_name_the_field(self):
        p = self.p
        cases = [
            (lambda: oj.start(p, {}), 'journal start needs "summary"'),
            (lambda: oj.start(p, {"summary": "x", "plan": {"a": 1}}), '"plan" must be a list of short texts'),
            (lambda: oj.add(p, {"type": "decision", "what": "x"}), 'journal add decision needs "why"'),
            (lambda: oj.add(p, {"type": "difficulty"}), 'journal add difficulty needs "what"'),
            (lambda: oj.add(p, {"type": "resolution", "how": "x"}), 'journal add resolution needs "for"'),
            (lambda: oj.add(p, {"type": "resolution", "for": 1}), 'journal add resolution needs "how"'),
            (lambda: oj.add(p, {"type": "step", "what": "x", "duration_s": -1}), '"duration_s" must be seconds'),
            (lambda: oj.add(p, {"type": "plan", "steps": []}), 'journal add plan needs "steps"'),
            (lambda: oj.add(p, {"type": "note"}), 'journal add note needs "text"'),
            (lambda: oj.add(p, {"type": "bogus"}), 'journal add needs "type"'),
            (lambda: oj.add(p, {"type": "task-end"}), "run that command instead"),
            (lambda: oj.add(p, {"type": "note", "text": "x"}, by="someone"), 'by must be "agent" or "orch"'),
            (lambda: oj.add(p, ["not", "an", "object"]), "one JSON object"),
            (lambda: oj.end(p, {}), 'journal end needs "outcome"'),
            (lambda: oj.end(p, {"outcome": "maybe"}), '"outcome" is \'maybe\'; use one of done, partial'),
            (lambda: oj.end(p, {"outcome": "done"}), "no current journal task to end; start one"),
        ]
        for fn, msg in cases:
            with self.subTest(msg=msg), self.assertRaises(oj.JournalError) as cm:
                fn()
            self.assertIn(msg, str(cm.exception))
        self.assertFalse(oj.paths(p)["journal"].exists())  # nothing half-written


class ConcurrencyTest(Base):
    def test_threads_never_tear_lines(self):
        task = oj.start(self.p, {"summary": "parallel"})["task"]
        errors = []

        def worker(n):
            try:
                for i in range(10):
                    oj.add(self.p, {"type": "note", "text": f"thread {n} note {i} " + "y" * 200})
                    if i % 2:  # monotonic readings, as time.monotonic() gives
                        oj.record_invocation(self.p, "run", ["run", f"job{n}"], 1.0, 2.5, 0, {"job": f"job{n}"})
            except Exception as e:  # pragma: no cover - reported below
                errors.append(repr(e))
        th = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in th:
            t.start()
        for t in th:
            t.join()
        self.assertEqual(errors, [])
        lines = oj.paths(self.p)["journal"].read_text(encoding="utf-8").splitlines()
        recs = [json.loads(ln) for ln in lines]
        self.assertEqual(len(recs), 1 + 6 * 15)
        self.assertEqual(sorted(r["seq"] for r in recs), list(range(1, 92)))
        self.assertEqual({r["task"] for r in recs}, {task})
        self.assertEqual(self.state()["open"][task]["seq"], 91)
        orch = [r for r in recs if r["by"] == "orch"]
        self.assertEqual((len(orch), {r["duration_s"] for r in orch}), (30, {1.5}))
        self.assertTrue(all(TS_RX.fullmatch(r["started"]) for r in orch))


class RecordInvocationTest(Base):
    def test_no_ops(self):
        self.assertIsNone(oj.record_invocation(None, "run", ["run", "x"], T0, T0, 0, {}))
        self.assertIsNone(oj.record_invocation(self.p, "journal", ["journal", "add"], T0, T0, 0, {}))
        self.assertFalse(oj.journal_dir(self.p).exists())

    def test_never_raises(self):
        bad = self.tmp / "a-file"
        bad.write_text("x")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(oj.record_invocation(oc.Project(self.tmp, bad, True), "run", ["run"], T0, T0, 0, {}))
        self.assertIn("orch: warn: journal not written:", err.getvalue())

    def test_decisions_codes_and_string_args(self):
        oj.start(self.p, {"summary": "setup"}, now=T0)
        oj.record_invocation(self.p, "init", "init --apply", at(seconds=1), at(seconds=2), 0,
                             {"state": "setup", "decisions": [{"what": "h11 scope tests"}, "production none", {"x": 1}]})
        oj.record_invocation(self.p, "run", ["run", "t-ok"], at(seconds=3), at(seconds=4), None, {})
        oj.record_invocation(self.p, "run", ["run", "t-ok"], at(seconds=5), at(seconds=6), 9, {"fix": "do this"})
        recs = oj.read(self.p)
        self.assertEqual([(r["type"], r["by"]) for r in recs[1:4]], [("step", "orch"), ("decision", "orch"),
                                                                      ("decision", "orch")])
        self.assertEqual((recs[1]["args"], recs[1]["details"]), (["init", "--apply"], {"state": "setup"}))
        self.assertEqual([(r["what"], r["why"]) for r in recs[2:4]],
                         [("h11 scope tests", "user answer"), ("production none", "user answer")])
        crash, odd = recs[5], recs[7]
        self.assertEqual((crash["what"], crash["class"], recs[4]["result"]), ("orch run t-ok crashed", "crash", "crash"))
        self.assertEqual((odd["class"], odd["fix"], odd["what"]), ("exit-9", "do this", "orch run t-ok exited 9 (exit-9)"))

    def test_torn_line_is_skipped_and_never_glued(self):
        oj.add(self.p, {"type": "note", "text": "first"})
        with open(oj.paths(self.p)["journal"], "ab") as f:
            f.write(b'{"v": 1, "ts": "2026-10-30T09:00:00')  # a crash mid-write
        oj.add(self.p, {"type": "note", "text": "second"})
        lines = oj.paths(self.p)["journal"].read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[2])["text"], "second")
        self.assertEqual([r["text"] for r in oj.read(self.p)], ["first", "second"])


class CliTest(Base):
    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "shop"
        (self.dir / ".orch").mkdir(parents=True)
        (self.dir / ".orch" / "config.json").write_text("{}", encoding="utf-8")

    def cli(self, action, stdin=None, **kw):
        a = SimpleNamespace(action=action, task=None, last=None, days=None, json=False, project=str(self.dir))
        a.__dict__.update(kw)
        out, err = io.StringIO(), io.StringIO()
        fake = io.TextIOWrapper(io.BytesIO((stdin or "").encode("utf-8")), encoding="utf-8")
        with mock.patch.object(sys, "stdin", fake), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = oj.cmd_journal(a)
        return rc, out.getvalue().splitlines(), err.getvalue()

    def test_start_add_end_show_stats_path(self):
        rc, out, _ = self.cli("start", json.dumps(oj.EXAMPLES["task-start"]))
        self.assertEqual((rc, len(out)), (0, 2))
        res = json.loads(out[1])
        task = res["task"]
        self.assertEqual(out[0], f"journal: task {task} started")
        self.assertEqual((res["ok"], res["seq"], res["closed"], res["open"]), (True, 1, [], []))
        self.assertIn("orch journal add <<'EOF'", res["next"])
        rc, out, _ = self.cli("add", '{"type": "difficulty", "what": "web-unit fails on h12"}')
        self.assertEqual((rc, out[0]), (0, f"journal: {task} #2 difficulty recorded"))
        self.assertIn('"for": 2', json.loads(out[1])["next"])
        rc, out, _ = self.cli("add", '\ufeff{"type": "resolution", "for": 2, "how": "set TZ"}\n')
        self.assertEqual(rc, 0)
        rc, out, _ = self.cli("end", json.dumps(oj.EXAMPLES["task-end"]))
        self.assertEqual(rc, 0)
        self.assertRegex(out[0], rf"^journal: task {task} ended done in .*: 0 steps, 0 decisions, 1 difficulty "
                                 rf"\(1 resolved, 0 unresolved\); summary in .*/shop/\.orch/journal/MEMORY\.md$")
        self.assertEqual(json.loads(out[1])["counts"]["resolved"], 1)
        rc, out, _ = self.cli("show")
        self.assertEqual((rc, len(out)), (0, 4))
        self.assertRegex(out[0], rf"^\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d {task} #1 task-start agent fix the failing cart tests$")
        rc, out, _ = self.cli("show", task=task, last=1)
        self.assertRegex(out[0], r"^\S+ \S+ #4 task-end agent done in \S+: cart tests pass on every helper$")
        rc, out, _ = self.cli("show", json=True)
        self.assertEqual([json.loads(x)["seq"] for x in out], [1, 2, 3, 4])
        rc, out, _ = self.cli("stats")
        self.assertEqual((rc, out[0]), (0, "journal stats (all time): 4 records"))
        rc, out, _ = self.cli("stats", json=True, days=1)
        self.assertEqual(json.loads(out[0])["tasks"]["by_outcome"], {"done": 1})
        rc, out, _ = self.cli("path")
        store = (self.dir / ".orch").resolve().as_posix()
        self.assertEqual(out, [f"journal {store}/journal/journal.jsonl", f"memory {store}/journal/MEMORY.md"])
        rc, out, _ = self.cli("path", json=True)
        self.assertEqual(sorted(json.loads(out[0])), ["current", "journal", "memory"])

    def test_add_untracked_and_disabled(self):
        rc, out, _ = self.cli("add", '{"type": "note", "text": "no task yet"}')
        self.assertEqual((rc, out[0]), (0, "journal: untracked #1 note recorded (no open task: start one with orch "
                                           "journal start)"))
        os.environ["ORCH_JOURNAL"] = "0"
        rc, out, _ = self.cli("start", '{"summary": "x"}')
        self.assertEqual((rc, out[0], json.loads(out[1])), (0, "journal: off (ORCH_JOURNAL=0), nothing recorded",
                                                            {"ok": True, "disabled": True}))

    def test_bad_input_exit_2(self):
        cases = [("start", "", "got empty stdin"), ("start", "{not json", "stdin is not valid JSON (line 1"),
                 ("start", "[1, 2]", "takes one JSON object, not a list"),
                 ("add", '{"type": "decision", "what": "x"}', 'needs "why"'),
                 ("end", '{"outcome": "done"}', "no current journal task"),
                 ("add", '{"type": "note", "text": "x"}', "task t-none is not open"),
                 ("show", None, "--last must be 0 or more"), ("stats", None, "--days must be a number above 0"),
                 ("nope", None, "unknown journal action 'nope'")]
        for action, stdin, msg in cases:
            kw = {"task": "t-none"} if msg.startswith("task t-none") else {"last": -1} if action == "show" else \
                {"days": 0} if action == "stats" else {}
            with self.subTest(msg=msg):
                rc, out, err = self.cli(action, stdin, **kw)
                self.assertEqual(rc, 2)
                self.assertIn(msg, err)
                self.assertEqual((json.loads(out[-1])["ok"], json.loads(out[-1])["exit"]), (False, 2))

    def test_project_resolution(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        p = oj.project(str(repo))
        self.assertEqual(p.store, (repo / ".git" / "orch").resolve())
        self.assertTrue((p.store / "state").is_dir())
        self.assertIsNone(oc.find_project(str(repo)))  # the journal does not make a setup
        # Q18: a folder outside git named with --project gets the store orch init --apply will use, DIR/.orch
        bare = self.tmp / "plain"
        bare.mkdir()
        warns = []
        p = oj.project(str(bare), warnings=warns)
        self.assertEqual((p.root, p.store, p.git), (bare.resolve(), (bare / ".orch").resolve(), False))
        self.assertTrue((p.store / "state").is_dir())
        self.assertIsNone(oc.find_project(str(bare)))
        self.assertEqual(len(warns), 1)
        self.assertIn("is not a git repository, so orch keeps this setup in", warns[0])
        warns = []
        oj.project(str(bare), warnings=warns)
        self.assertEqual(warns, [])  # said once, when the folder was made
        other = self.tmp / "other"  # nothing names a folder outside git: refused, naming the flag that works
        other.mkdir()
        with mock.patch.object(os, "getcwd", return_value=str(other)):
            with self.assertRaisesRegex(oj.JournalError, r"/other is not a git repository: pass --project DIR to keep "
                                                         r"orch's setup and journal in DIR/\.orch$"):
                oj.project()
            self.assertEqual(oj.project(str(other), create=False).store, (other / ".orch").resolve())
        self.assertFalse((other / ".orch").exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(oj.main(["path", "--project", str(repo)]), 0)
        self.assertIn("/.git/orch/journal/journal.jsonl", out.getvalue())

    def test_project_flag_beats_orch_project(self):
        """--project beats ORCH_PROJECT beats the current folder, as for orch init: records of a command run with
        --project land in that project, also when its store is still to be made inside .git."""
        other = self.tmp / "other"
        (other / ".orch").mkdir(parents=True)
        (other / ".orch" / "config.json").write_text("{}", encoding="utf-8")
        os.environ["ORCH_PROJECT"] = str(other)
        journal = "journal {}/journal/journal.jsonl"
        rc, out, _ = self.cli("path")
        self.assertEqual((rc, out[0]), (0, journal.format((self.dir / ".orch").resolve().as_posix())))
        rc, out, _ = self.cli("path", project=None)
        self.assertEqual((rc, out[0]), (0, journal.format((other / ".orch").resolve().as_posix())))
        repo, plain = self.tmp / "repo", self.tmp / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        os.environ["ORCH_PROJECT"] = str(plain)
        with mock.patch.object(os, "getcwd", return_value=str(plain)):  # never the real working folder
            self.assertEqual(oj.project(str(repo)).store, (repo / ".git" / "orch").resolve())
        self.assertFalse((plain / ".orch").exists())

    def test_folder_outside_git_made_only_by_writes(self):
        """Q18: show, stats and path of a folder outside git make nothing, nor does start with ORCH_JOURNAL=0; start
        makes DIR/.orch and its reply says so once."""
        plain = self.tmp / "plain"
        plain.mkdir()
        for act in ("show", "stats", "path"):
            rc, out, err = self.cli(act, project=str(plain))
            self.assertEqual(rc, 0, err)
        self.assertEqual(out[0], f"journal {(plain / '.orch').resolve().as_posix()}/journal/journal.jsonl")
        os.environ["ORCH_JOURNAL"] = "0"
        self.assertEqual(self.cli("start", '{"summary": "x"}', project=str(plain))[0], 0)
        self.assertFalse((plain / ".orch").exists())
        os.environ["ORCH_JOURNAL"] = "1"
        rc, out, err = self.cli("start", '{"summary": "set up orch"}', project=str(plain))
        self.assertEqual(rc, 0, err)
        res = json.loads(out[1])
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn(f"so orch keeps this setup in {(plain / '.orch').resolve().as_posix()}", res["warnings"][0])
        rc, out, err = self.cli("add", '{"type": "note", "text": "one helper"}', project=str(plain))
        self.assertEqual((rc, json.loads(out[1])["task"]), (0, res["task"]))
        self.assertNotIn("warnings", json.loads(out[1]))

    def test_hints_are_runnable_heredocs(self):
        """The start reply, a difficulty reply and the error messages print heredocs that run as printed."""
        nxt = oj.NEXT_START
        self.assertNotRegex(nxt, ONE_LINE)
        self.assertEqual([(a, o["type"]) for a, o in heredocs(nxt)],
                         [("add", "decision"), ("add", "difficulty"), ("add", "resolution")])
        self.assertIn("orch logs its commands itself", nxt)
        self.assertLess(len(nxt), 400)  # read at every task start: one line and the three templates
        rc, out, _ = self.cli("start", json.dumps({"summary": "follow the hints"}))
        self.assertEqual((rc, json.loads(out[1])["next"]), (0, nxt))
        rc, out, _ = self.cli("add", json.dumps({"type": "difficulty", "what": "the bug stays"}))
        hint = json.loads(out[1])["next"]
        self.assertNotRegex(hint, ONE_LINE)
        [(act, obj)] = heredocs(hint)
        self.assertEqual((act, obj), ("add", {"type": "resolution", "for": 2, "how": "..."}))
        rc, out, err = self.cli(act, json.dumps(obj))
        self.assertEqual((rc, json.loads(out[1])["type"]), (0, "resolution"), err)
        for action, stdin in (("end", ""), ("add", '{"type": "decision", "what": "x"}'), ("add", '{"type": "fix"}'),
                              ("start", "{not json")):
            with self.subTest(action=action, stdin=stdin):
                rc, out, err = self.cli(action, stdin)
                msg = json.loads(out[-1])["error"]
                self.assertEqual(rc, 2)
                self.assertNotRegex(msg, ONE_LINE)
                [(act, obj)] = heredocs(msg)
                self.assertEqual(act, action)
                rc, out, err = self.cli(act, json.dumps(obj))
                self.assertEqual(rc, 0, err)

    def test_reference_rule_runs_as_printed(self):
        """references/journal.md: the agent's rule runs as printed, its examples are the ones the error messages
        show, and it says what orch records and resolves by itself."""
        rule = doc_part("The agent's rule", "Task lifecycle")
        cmds = heredocs(rule)
        self.assertEqual(([a for a, _ in cmds], rule.count("<<'EOF'")), (["start", "add", "add", "add", "end"], 5))
        for act, obj in cmds:
            rc, out, err = self.cli(act, json.dumps(obj))
            self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out[1])["counts"]["unresolved"], 0)
        self.assertEqual([o for _, o in cmds], [oj.EXAMPLES[k] for k in ("task-start", "decision", "difficulty",
                                                                         "resolution", "task-end")])
        self.assertIn("only for a problem orch cannot see", rule)
        who = doc_part("Who writes what", "The agent's rule")
        for want in ("adds one `resolution`", "top-level answer keys must match"):
            self.assertIn(want, who)
        classes = doc_part("Record schema", "Example")
        for cls in [c for c in oj.EXIT_NAMES.values() if c != "pass"] + ["live-down", "crash", "exit-<n>"]:
            self.assertIn(f"`{cls}`", classes)

    def test_reference_example_is_what_orch_writes(self):
        """The example records (orch's auto-resolution included) come out of the code as shown, and its MEMORY.md
        section is what render_task_md prints for them."""
        blocks = re.findall(r"```\n(.*?)```", doc_part("Example", "Reading it"), re.S)
        doc = [json.loads(x) for x in blocks[0].splitlines()]
        for r in doc:
            if r["type"] == "task-start":
                oj.start(self.p, {k: r[k] for k in ("summary", "request", "plan") if k in r}, now=r["ts"])
            elif r["type"] == "task-end":
                oj.end(self.p, {k: r[k] for k in ("outcome", "summary", "lessons", "open") if k in r}, now=r["ts"])
            elif r["type"] == "step" and r["by"] == "orch":
                oj.record_invocation(self.p, r["verb"], r["args"], r["started"], r["ended"], r["exit"], r["details"])
            elif r["by"] == "agent":
                oj.add(self.p, {k: v for k, v in r.items() if k == "type" or k not in oj.RESERVED}, now=r["ts"])
        same = lambda recs: [{k: v for k, v in r.items() if k not in ("ts", "task", "started", "ended")}  # noqa: E731
                             for r in recs]
        self.assertEqual(same(oj.read(self.p)), same(doc))
        md, want = oj.render_task_md(doc).splitlines(), blocks[1].splitlines()
        self.assertRegex(md[0], r"^## t20261030-090000-1a2b - \d{4}-\d\d-\d\d \d\d:\d\d - done - 10m 00s$")
        self.assertEqual(md[1:], want[1:])


class AutoResolve(Base):
    """orch links a later pass of the same command to the earlier failure, and ci never repeats its children's
    failures (a runtime run on a real helper showed both: duplicated difficulties and none ever resolved)."""

    def recs(self, typ):
        return [r for r in oj.read(self.p) if r["type"] == typ]

    def test_later_pass_resolves_the_failure_and_end_counts_it(self):
        p = self.p
        oj.start(p, {"summary": "fix the cart tests"}, now=T0)
        oj.record_invocation(p, "run", ["run", "web-unit", "--wait", "600"], at(seconds=1), at(seconds=40), 1,
                             {"job": "web-unit", "result": "test-fail"})
        oj.record_invocation(p, "run", ["run", "other-job"], at(seconds=41), at(seconds=50), 0, {"job": "other-job"})
        self.assertEqual(self.recs("resolution"), [])  # a different job does not resolve it
        oj.record_invocation(p, "run", ["run", "web-unit", "--wait", "300"], at(seconds=60), at(seconds=99), 0,
                             {"job": "web-unit", "result": "pass"})
        diff, res = self.recs("difficulty"), self.recs("resolution")
        self.assertEqual(len(diff), 1)
        self.assertEqual((res[0]["for"], res[0]["by"], res[0]["outcome"]), (diff[0]["seq"], "orch", "pass"))
        end = oj.end(p, {"outcome": "done", "summary": "green"}, now=at(seconds=120))
        self.assertEqual((end["counts"]["difficulties"], end["counts"]["resolved"], end["counts"]["unresolved"]),
                         (1, 1, 0))

    def test_ci_line_adds_no_difficulty_when_children_ran(self):
        p = self.p
        oj.start(p, {"summary": "ci"}, now=T0)
        oj.record_invocation(p, "run", ["run", "web-unit"], at(seconds=1), at(seconds=30), 1, {"job": "web-unit"})
        oj.record_invocation(p, "ci", ["ci", "--jobs", "web-unit"], at(seconds=0), at(seconds=31), 1,
                             {"jobs": [{"job": "web-unit", "exit": 1}], "result": "test-fail"})
        self.assertEqual(len(self.recs("difficulty")), 1)  # only the child's
        oj.record_invocation(p, "ci", ["ci", "--jobs", "bad"], at(seconds=40), at(seconds=41), 2,
                             {"error": "unknown job bad"})
        self.assertEqual(len(self.recs("difficulty")), 2)  # a ci that never started children still reports

    def test_signature_ignores_wait_and_keeps_mode_flags(self):
        self.assertEqual(oj._signature("run", ["run", "a", "--wait", "5"], {"job": "a"}),
                         oj._signature("run", ["run", "a"], {"job": "a"}))
        self.assertNotEqual(oj._signature("init", ["init", "--apply"], {"answer_keys": []}),
                            oj._signature("init", ["init", "--approve"], {}))
        self.assertIsNone(oj._signature("nodes", ["nodes"], {}))

    def test_every_failure_of_a_command_is_resolved_when_it_passes(self):
        p = self.p
        task = oj.start(p, {"summary": "fix the cart tests"}, now=T0)["task"]
        for i, wait in enumerate(([], ["--wait", "600"])):
            oj.record_invocation(p, "run", ["run", "web-unit", *wait], at(seconds=10 * i), at(seconds=10 * i + 5), 1,
                                 {"job": "web-unit", "result": "test-fail"})
        self.assertEqual(self.state()["open"][task]["pending"], {"run web-unit": [3, 5]})
        oj.record_invocation(p, "run", ["run", "web-unit"], at(seconds=30), at(seconds=40), 0, {"job": "web-unit"})
        res = self.recs("resolution")
        self.assertEqual([(r["seq"], r["for"], r["by"], r["outcome"]) for r in res], [(7, 3, "orch", "pass"),
                                                                                    (8, 5, "orch", "pass")])
        self.assertEqual(res[1]["how"], "orch run web-unit passed later (step #6)")
        self.assertEqual(self.state()["open"][task]["pending"], {})
        end = oj.end(p, {"outcome": "done", "summary": "green"}, now=at(seconds=60))
        self.assertEqual((end["counts"]["difficulties"], end["counts"]["resolved"], end["counts"]["unresolved"]),
                         (2, 2, 0))
        self.assertEqual(oj.stats(p)["unresolved"], [])
        self.assertNotIn("UNRESOLVED", self.memory())

    def test_pending_seq_kept_by_an_older_version_still_resolves(self):
        p = self.p
        task = oj.start(p, {"summary": "upgrade mid-task"}, now=T0)["task"]
        oj.record_invocation(p, "run", ["run", "web-unit"], at(seconds=1), at(seconds=2), 1, {"job": "web-unit"})
        st = self.state()
        st["open"][task]["pending"] = {"run web-unit": 3}  # one int per command, as the previous version kept it
        oc.save_state(p, oj.STATE, st)
        oj.record_invocation(p, "run", ["run", "web-unit"], at(seconds=3), at(seconds=4), 1, {"job": "web-unit"})
        self.assertEqual(self.state()["open"][task]["pending"], {"run web-unit": [3, 5]})
        oj.record_invocation(p, "run", ["run", "web-unit"], at(seconds=5), at(seconds=6), 0, {"job": "web-unit"})
        self.assertEqual([r["for"] for r in self.recs("resolution")], [3, 5])
        self.assertEqual((oj._seqs(4), oj._seqs([4, True, "6", 7]), oj._seqs(None), oj._seqs("x")),
                         ([4], [4, 7], [], []))

    def test_init_apply_resolves_only_a_failure_with_the_same_answer_keys(self):
        p = self.p
        oj.start(p, {"summary": "set up orch"}, now=T0)
        apply = ["init", "--apply"]
        oj.record_invocation(p, "init", apply, at(seconds=1), at(seconds=2), 2,
                             {"state": "count", "answer_keys": ["helpers"], "error": "deploy@192.0.2.11;x is refused"})
        oj.record_invocation(p, "init", apply, at(seconds=3), at(seconds=4), 0, {"answer_keys": ["helper_count"]})
        self.assertEqual(self.recs("resolution"), [])  # a good helper_count answer does not fix a refused helpers one
        oj.record_invocation(p, "init", apply, at(seconds=5), at(seconds=6), 2, {"error": "stdin is not JSON"})
        oj.record_invocation(p, "init", apply, at(seconds=7), at(seconds=8), 0, {"answer_keys": []})
        self.assertEqual(self.recs("resolution"), [])  # answers orch could not read never auto-resolve
        oj.record_invocation(p, "init", apply + ["--project", "/srv/app"], at(seconds=9), at(seconds=10), 0,
                             {"answer_keys": ["helpers"]})
        self.assertEqual([(r["for"], r["outcome"]) for r in self.recs("resolution")], [(3, "pass")])
        self.assertEqual(oj._signature("init", apply, {"answer_keys": ["production", "hosts", "hosts"]}),
                         "init --apply answers=hosts,production")
        self.assertIsNone(oj._signature("init", apply, {"answer_keys": "helpers"}))
        self.assertEqual(oj._signature("init", ["init", "--approve"], {}), "init --approve")

    def test_verb_details_decide_class_detail_and_fix(self):
        """orch live and deploy --check name their own class, detail (reason) and fix; the journal reads them from
        the verb's details, with no rule of its own per verb."""
        p = self.p
        oj.start(p, {"summary": "ship the cart fix"}, now=T0)
        gate_fix = "orch init (finish the setup and approve it), then orch deploy --check again"
        oj.record_invocation(p, "deploy", ["deploy", "--check"], at(seconds=1), at(seconds=2), 7,
                             {"verb": "deploy", "exit": 7, "ready": False, "next": gate_fix,
                              "gates": [{"name": "setup approved", "ok": False, "detail": "not approved",
                                         "fix": gate_fix}], "reason": "setup approved: not approved", "fix": gate_fix})
        live_fix = "tell the user (no rollback command is saved), then investigate"
        url_err = "http://192.0.2.1/health: no answer within 10 s"
        oj.record_invocation(p, "live", ["live"], at(seconds=3), at(seconds=33), 1,
                             {"verb": "live", "exit": 1, "ok": False, "result": "down", "class": "live-down",
                              "checks": [{"url": "http://192.0.2.1/health", "error": "no answer within 10 s"}],
                              "reason": url_err, "fix": live_fix})
        oj.record_invocation(p, "live", ["live"], at(seconds=40), at(seconds=50), 1, {"class": "live-down"})
        gate, down, bare = self.recs("difficulty")
        self.assertEqual((gate["class"], gate["detail"], gate["fix"]), ("deploy-gate", "setup approved: not approved",
                                                                        gate_fix))
        self.assertEqual((down["what"], down["class"], down["detail"], down["fix"]),
                         ("orch live exited 1 (live-down)", "live-down", url_err, live_fix))
        self.assertEqual([r["result"] for r in self.recs("step")][:2], ["deploy-gate", "down"])
        self.assertEqual((bare["class"], bare["fix"]), ("live-down", oj._fix("live-down", {})))
        self.assertIn("roll back only when they ask", bare["fix"])  # not the test-fail hint (orch logs <jobid>)
        self.assertEqual(oj.stats(p)["failure_classes"], {"live-down": 2, "deploy-gate": 1})
        oj.end(p, {"outcome": "failed"}, now=at(seconds=60))
        self.assertIn(f"- #5 orch live exited 1 (live-down): {url_err}. UNRESOLVED", self.memory().splitlines())


class ThroughOrchTest(Base):
    """The real orch.py, orch_init and orch_journal in a process: temp folders, a temp ORCH_HOME and DOCKER_CONFIG,
    an empty home folder (no ssh config, so orch init offers no ssh alias) and --offline (no probe, no registry)."""

    def setUp(self):
        super().setUp()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        self.env.update(ORCH_HOME=str(self.tmp / "home"), DOCKER_CONFIG=str(self.tmp / "docker"), HOME=str(self.tmp),
                        USERPROFILE=str(self.tmp), PYTHONIOENCODING="utf-8")

    def orch(self, cwd, *args, stdin=None):
        r = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "orch.py"), *args], cwd=cwd, env=self.env,
                           input=stdin, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        return r.returncode, r.stdout.strip().splitlines(), r.stderr

    def test_outside_git_the_first_setup_answer_lands_in_the_open_task(self):
        """Q18: SKILL.md step 0 works outside git: journal start --project DIR makes DIR/.orch, the first orch init
        --apply answer and its step join that task (not untracked), and journal end counts them; without --project the
        error names the flag that works."""
        shop = self.tmp / "shop"
        shop.mkdir()
        start = '{"summary": "set up orch", "request": "use my helper"}'
        rc, out, err = self.orch(shop, "journal", "start", stdin=start)
        self.assertEqual(rc, 2, err)
        self.assertIn(f"{shop.resolve().as_posix()} is not a git repository: pass --project DIR to keep orch's setup "
                      f"and journal in DIR/.orch", err)
        self.assertFalse((shop / ".orch").exists())
        rc, out, err = self.orch(shop, "journal", "start", "--project", str(shop), stdin=start)
        self.assertEqual(rc, 0, err)
        task = json.loads(out[-1])["task"]
        rc, out, err = self.orch(shop, "init", "--offline", "--apply", "--project", str(shop), stdin='{"helper_count": 1}')
        self.assertEqual(rc, 0, err)
        rc, out, err = self.orch(shop, "journal", "end", stdin='{"outcome": "partial", "summary": "first answer saved"}')
        self.assertEqual(rc, 0, err)
        end = json.loads(out[-1])
        self.assertEqual((end["task"], end["counts"]["steps"], end["counts"]["decisions"]), (task, 1, 1))
        recs = [json.loads(x) for x in (shop / ".orch" / "journal" / "journal.jsonl").read_text(encoding="utf-8")
                .splitlines()]
        self.assertEqual([(r["task"], r["type"], r["by"]) for r in recs],
                         [(task, "task-start", "agent"), (task, "decision", "orch"), (task, "step", "orch"),
                          (task, "task-end", "agent")])
        self.assertEqual(recs[1]["what"], "helper_count: 1 helper")
        self.assertTrue(recs[2]["what"].startswith("orch init --offline --apply --project "), recs[2]["what"])
        memory = (shop / ".orch" / "journal" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("Counts: 1 step, 1 decision, 0 difficulties", memory)

    def test_init_error_echoing_an_answer_keeps_no_secret(self):
        """Q25: orch init echoes a refused answer in its error (a two-line deploy command, a curl given as a health
        URL); orch's step and difficulty records of those calls hold no secret."""
        repo = self.tmp / "shop"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        apply = ("init", "--offline", "--apply", "--project", str(repo))
        rc, out, err = self.orch(repo, *apply, stdin='{"helper_count": 1}')
        self.assertEqual(rc, 0, err)
        for ans in ({"deploy": {"command": "./scripts/deploy.sh --password Hunter2BBB222\n./scripts/notify.sh"}},
                    {"deploy": {"command": "./scripts/deploy.sh",
                                "health": ["curl -u ops:Hunter2EEE555 https://shop.example/health"]}}):
            rc, out, err = self.orch(repo, *apply, stdin=json.dumps(ans))
            self.assertEqual(rc, 2, out)
            self.assertRegex(err, r"journal: difficulty #\d+ recorded \(orch\)")
        raw = (repo / ".git" / "orch" / "journal" / "journal.jsonl").read_text(encoding="utf-8")
        self.assertEqual(sum(1 for x in raw.splitlines() if json.loads(x)["type"] == "difficulty"), 2)
        self.assertNotIn("Hunter2", raw)


if __name__ == "__main__":
    unittest.main()
