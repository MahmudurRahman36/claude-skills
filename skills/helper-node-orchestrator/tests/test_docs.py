"""The docs carry council 2's must-fixes (fix round 3): README's evidence, not-proven list, limits, day-one order,
hook registration and troubleshooting rows; the behaviour the guard and core implement (MCP tools, inline code, ssh
with a local substitution, docker context use, the fail-closed missing view, helper + production readonly, selftest
off production, v1 leftovers, .orch/.gitignore, bare keys in the journal scrub) in RULES.md, SKILL.md and the
references; the same record in the design and contract (development repo only) and in the behaviour evals.
Text checks only: no processes, network or package imports."""
import json
import os
import re
import unittest

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV = os.path.join(PKG, "dev")  # export-ignored: the design and contract exist in the development repo only
MATCHER = "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|mcp__.*"


def read(rel):
    with open(os.path.join(PKG, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def flat(text):
    return " ".join(text.split())


def section(text, heading):
    """The body under a markdown heading of any level, up to the next heading of the same or a higher level."""
    m = re.search(r"(?m)^(#+) %s[ \t]*$" % re.escape(heading), text)
    if not m:
        raise AssertionError("no heading %r" % heading)
    rest = text[m.end():]
    nxt = re.search(r"(?m)^#{1,%d} " % len(m.group(1)), rest)
    return rest[:nxt.start()] if nxt else rest


def rows(text):
    return [r for r in text.splitlines() if r.startswith("|")]


class ReadmeTest(unittest.TestCase):
    def setUp(self):
        self.text = read("README.md")
        self.proven = flat(section(self.text, "Proven / not proven"))

    def test_no_tbd_left(self):
        self.assertNotIn("TBD", self.text)

    def test_proven_holds_the_evidence(self):
        for w in ("720+ unit tests", "740+ guard fixtures", "Python 3.14.7 and 3.12.14", "`validate.py` PASS",
                  "zero site data", "review rounds 1 and 2: 40 and 25 confirmed defects, all fixed",
                  "a Windows dev PC that is itself a helper", "(pass, fail, oom, timeout, prep)",
                  "`orch selftest` 21/21", "nothing left behind", "a busy shared Linux helper that is also production",
                  "the interview through to ready", "digest pinning", "after an admission wait", "the journal",
                  "`orch deploy --check` exit 7 with its asks", "0 objects left", "admission correctly refused",
                  "real headless agent runs", "in the user's order with journal start and end",
                  "one line and no question", "nothing was deployed"):
            self.assertIn(w, self.proven)

    def test_not_proven_list(self):
        self.assertIn("Not proven:", self.proven)
        self.assertIn("Limits that stay:", self.proven)
        part = self.proven[self.proven.index("Not proven:"):self.proven.index("Limits that stay:")]
        for w in ("the real master (Windows)", "a Linux master", "a real Python 3.10", "a Windows helper over ssh",
                  "a third helper", "the user's real project jobs", "the guard's ask prompt",
                  "the session-start hook in a live interactive Claude Code session", "Codex"):
            self.assertIn(w, part)

    def test_limits_are_stated_plainly(self):
        self.assertIn("Limits that stay:", self.proven)
        part = self.proven[self.proven.index("Limits that stay:"):]
        for w in ("matches command text; it is not a security boundary", "are the real protection",
                  "roughly 100-150 ms to every matched tool call", "every session on this master, redeploys included",
                  "master dies mid-job", "keeps running on the helper until the next orch run cleans it"):
            self.assertIn(w, part)

    def test_guard_registration(self):
        blocks = [b for b in re.findall(r"```json\n(.*?)```", self.text, re.S) if "hooks/guard.py" in b]
        self.assertEqual(len(blocks), 1)
        entry = json.loads(blocks[0])
        self.assertEqual(entry["matcher"], MATCHER)
        self.assertEqual(entry["hooks"], [{"type": "command", "command": "python",
                                           "args": ["-I", "<skill dir>/hooks/guard.py"], "timeout": 10,
                                           "onFailure": "block"}])
        for tool in ("Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit",
                     "mcp__desktop-commander__start_process", "mcp__plugin_dc_dc__interact_with_process",
                     "mcp__fs__write_file"):
            self.assertTrue(re.fullmatch(entry["matcher"], tool), tool)
        for tool in ("Read", "Grep", "Glob", "WebFetch", "Task"):
            self.assertIsNone(re.fullmatch(entry["matcher"], tool), tool)
        g = flat(section(self.text, "Guard hook (Claude Code)"))
        for w in ("print(sys.executable)", "**Absolute interpreter path:** in both hook entries",
                  "`mcp__.*` covers every MCP tool", "`start_process` and `interact_with_process`", "Review `/mcp`",
                  "a missing view counts as unreadable"):
            self.assertIn(w, g)

    def test_day_one_order(self):
        items = re.findall(r"(?m)^(\d+)\. (.*)$", section(self.text, "Day one, in this order"))
        self.assertEqual([int(n) for n, _ in items], list(range(1, 11)))
        want = [("`claude --version`", "2.1.295"),
                ("absolute path of the interpreter", "`%s`" % MATCHER, "`onFailure: block`", "session-start hook",
                 "recommended"),
                ("Review `/mcp`", "runs commands or writes files"),
                ("BatchMode", "`~/.ssh/known_hosts` holds every one"),
                ("old orch's `gc --session-end` on every helper first", "needs your OK"),
                ("ONE dedicated Linux helper", "`orch selftest --quick --node <name>`"),
                ("also runs production starts read-only", "it is the default"),
                ('deploy command "later" until the first green run',),
                ("Windows helper only after its ssh key works",),
                ("`orch init --approve` shows NO permission prompt", "the guard is not active: stop")]
        for (n, line), words in zip(items, want):
            for w in words:
                self.assertIn(w, line, "day-one step %s" % n)

    def test_troubleshooting_rows(self):
        table = rows(section(self.text, "Troubleshooting"))
        for words in (("`agentx-*` objects of another label family", "foreign namespace", "`gc --session-end`",
                       "needs your OK"),
                      ("asks about an MCP tool call", "protected path"),
                      ("`python -c`, `node -e`", "cannot read what it does"),
                      ("`$(...)` or backticks", "expands the substitution first", "spell the remote command out"),
                      ("`guard view unreadable`", "trust file exists")):
            self.assertEqual(len([r for r in table if all(w in r for w in words)]), 1, words)
        self.assertIn("Bash/PowerShell/Write/MCP call is blocked", "\n".join(table))

    def test_production_readonly_and_v1_leftovers(self):
        t = flat(self.text)
        self.assertIn("runs only read-only commands there by default (lint + tests at most", t)
        self.assertIn("v1 leftovers, a foreign namespace", t)
        self.assertIn("created with a `.gitignore` of `*`", t)
        self.assertIn("never run on a helper that is also production", t)  # runbook: selftest
        self.assertIn("No prompt means the guard is not active", t)  # first run, approve


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.text = flat(read("RULES.md"))

    def test_council2_rules(self):
        for w in ("its job scope is `readonly` unless the user chooses `tests`, and never more",
                  "(`readonly` by default, at most `tests`",
                  "Every rule holds for every tool: Bash, PowerShell, the file tools, MCP tools",
                  "every MCP tool, `mcp__.*`", "Desktop Commander `start_process`, `interact_with_process`",
                  "inline interpreter code (`python -c`, `node -e` and the like) that names a helper or production host",
                  "`docker context use orch-<node>`", "`docker context use orch-*`: denied",
                  "a substitution the master's shell expands first", "PowerShell `$(...)`",
                  "ssh whose remote command holds a local substitution",
                  "`DIR/.orch/`, which orch creates with a `.gitignore` of `*`",
                  "bare `sk-`, `sk-proj-`, `sk-ant-` and `AIza` keys",
                  "never run on a host that is also production",
                  "a missing view counts as unreadable", "not a security boundary", "roughly 100-150 ms",
                  "redeploys of other apps to those hosts included", "its container keeps running until the next orch run cleans it",
                  "Leftovers of another orch (v1)"):
            self.assertIn(w, self.text)
        self.assertNotIn("neither are script files and inline interpreter code", self.text)

    def test_enforcement_names_every_matched_tool(self):
        line = next(x for x in read("RULES.md").splitlines() if x.startswith("- **Claude Code:** a PreToolUse"))
        for tool in MATCHER.split("|"):
            self.assertIn(tool, line)


class SkillTest(unittest.TestCase):
    def test_skill_rules_and_limits(self):
        raw = read("SKILL.md")
        self.assertLessEqual(len(raw.encode("utf-8")), 9500)
        self.assertLessEqual(raw.count("\n"), 130)
        t = flat(raw)
        for w in ("Helper + production: production rules win; scope readonly by default, at most tests.",
                  "by any tool (MCP, inline code)", "never reword it or switch tools (MCP, inline code) to dodge it"):
            self.assertIn(w, t)


class ReferencesTest(unittest.TestCase):
    def test_commands(self):
        t = read("references/commands.md")
        st = next(r for r in rows(t) if r.startswith("| `selftest"))
        self.assertIn("failure-path checks (t-fail, t-sleep, t-oom, cordon, kill mid-job) never run on a helper that "
                      "is also production", st)
        table = rows(section(t, "Troubleshooting"))
        for words in (("MCP tool call", "protected path"), ("`python -c`, `node -e`", "dodge"),
                      ("`$(...)` or backticks", "local substitution"), ("`docker context use orch-...`",),
                      ("guard view unreadable", "trust file exists"),
                      ("`agentx-*` objects of another label family", "never remove them yourself")):
            self.assertEqual(len([r for r in table if all(w in r for w in words)]), 1, words)

    def test_config(self):
        t = flat(read("references/config.md"))
        for w in ("Helper + production: `readonly` by default, at most `tests`.",
                  "when added as a helper (`readonly` by default)", "`<folder>/.orch/.gitignore` holding `*`",
                  "the guard treats a missing view as unreadable and fails closed", "are a *foreign namespace*",
                  "removing its volumes needs the user's OK"):
            self.assertIn(w, t)
        self.assertNotIn("ignoring `.orch/` in version control is then up to you", t)

    def test_init_defaults(self):
        t = read("references/init.md")
        self.assertIn("2 other containers [helper only; lint + tests, no image builds]", t)
        self.assertIn("**Production is only what the user names.**", t)
        self.assertNotIn("[helper + production; lint + tests, no image builds]", t)
        ok = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", t, re.S) if '"deploy": "later", "alerts"' in b]
        self.assertEqual(len(ok), 1)  # the setup step's ask.example: what "ok" saves
        self.assertEqual(ok[0]["hosts"]["h12"], {"roles": ["helper"], "jobs": "tests"})
        f = flat(t)
        for w in ("(read-only commands by default, lint + tests at most)", "**v1 leftovers (foreign namespace):**",
                  "`DIR/.orch/` (created with a `.gitignore` of `*`)", "runs `readonly` by default and at most `tests`"):
            self.assertIn(w, f)

    def test_journal_scrub_and_gitignore(self):
        t = flat(read("references/journal.md"))
        self.assertIn("bare `sk-`, `sk-proj-`, `sk-ant-` and `AIza` API keys", t)
        self.assertIn("`DIR/.orch/` (with a `.gitignore` of `*`)", t)


class EvalsTest(unittest.TestCase):
    def test_v1_leftovers_scenario(self):
        s = section(read("evals/scenarios.md"), "16. v1-leftovers")
        for w in ('- Prompt: "Run the unit tests."', "- SCENARIO: `v1-leftovers`", "- Expected:", "- Rubric: C1-C8;"):
            self.assertIn(w, s)
        f = flat(s)
        for w in ("`agentx-*` objects of another label family", "needs the user's OK", "removes nothing itself",
                  "`docker volume rm`", "neither h11 nor the setup is called ready", "no `run` or `ci`"):
            self.assertIn(w, f)


@unittest.skipUnless(os.path.isdir(DEV), "dev/ is not part of the package")
class DevRecordTest(unittest.TestCase):
    def test_design_records_council2(self):
        t = read("dev/v2-design.md")
        s = flat(section(t, "14. Council 2 must-fixes (fix round 3; dev/research/council2-verdict.md)"))
        self.assertEqual(re.findall(r"(?<=\s)([1-7])\. ", " " + s), list("1234567"))
        for w in ("no TBD", MATCHER, "local substitution", "start_process", "`python -c`, `node -e`",
                  "foreign namespace", "another label family", "defaults to `readonly`", "skip production nodes",
                  "sk-ant-", "AIza", "`.gitignore` holding `*`", "`docker context use orch-*`", "tests/test_docs.py"):
            self.assertIn(w, s)
        self.assertNotIn("Absent/unreadable -> exit 0", t)
        self.assertNotIn('job scope default "tests"', t)

    def test_contract_records_council2(self):
        t = flat(read("dev/v2-contract.md"))
        for w in (MATCHER, "interact_with_process", "local substitution", "`docker context use orch-*` -> deny",
                  "handled as an unreadable view", "defaults to readonly, at most tests", '.gitignore holding "*"',
                  "bare sk-, sk-proj-, sk-ant- and AIza keys", "instead of \"ownership assertion failed\"",
                  "never use a production node", "tests/test_docs.py"):
            self.assertIn(w, t)


if __name__ == "__main__":
    unittest.main()
