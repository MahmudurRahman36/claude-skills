# Harness Builder

**Status: beta (1.2.0-beta).** Tested end to end with Claude Code 2.1.330 on Windows (Git Bash), on one domain (support-ticket triage). Codex support is generated but has not been run live. See [Status and limits](#status-and-limits).

**Trust the project folder first.** Claude Code skips the guard hooks of project agents in a folder whose trust prompt you haven't accepted. The generated harness then refuses to start a run instead of running unguarded. Open the project in Claude Code once and accept the prompt.

Turns any repeated AI job in a project into a **harness**: a system around the model that makes it do the job the same way every time, prove each judgment with evidence, check its own work and leave the decision to a human.

Based on the six-layer method (context, tools, output and process, verification, memory, automation) from the video "Stop Upgrading Your AI Model (Build A Harness Instead)" by 100x Engineers (https://www.youtube.com/watch?v=QRFyNJnrHxM). It adds guidance from Anthropic, OpenAI, Böckeler/Fowler, HumanLayer, Hamel Husain, Eugene Yan, Simon Willison, the MCP security guide and the Claude Code and Codex docs, all cited inline in `references/`.

## What it does

1. **Understands the project.** It reads the project's files and memory first: entry files, `.claude/`, `.agents/`, docs, CI and sample inputs. Every fact is tagged known, inferred or unknown.
2. **Interviews.** It asks only about the decisions the files can't answer. It uses Matt Pocock's "grilling" method: it asks about all open decisions in one round, each question with a recommended answer.
3. **Designs.** It writes `harness/<slug>/SPEC.md` and waits for your confirmation.
4. **Scaffolds.** It generates the files deterministically. It never overwrites a file, so re-running is safe.
5. **Fills.** It writes the files from real samples and checks that every path and command works. Each guardrail is enforced with deny rules or hooks, not left as prose.
6. **Tests.**
   - The validator must report 0 errors.
   - A **golden set** (a few items you have labelled yourself) is run, and the results must match your labels.
   - A prompt-injection case and a deliberately planted error must both be caught.
   - The guardrails must be shown to block.
7. **Automates**, but only after the tests pass and you have reviewed one real report.

## Generated layout (Claude Code)

```
CLAUDE.md                                # +5-line marked block
.claude/settings.json                    # deny rules from the spec (inputs, outbound tools), merged in
.claude/skills/<slug>-run/SKILL.md       # numbered procedure; forks into the runner: /<slug>-run [rerun|golden|from <run>]
.claude/skills/<slug>-calibrate/SKILL.md # human-only, approval-gated learning
.claude/agents/<slug>-runner.md          # runs the procedure; every tool call goes through the guard
.claude/agents/<slug>-verifier.md        # read-only, fresh-context checker on another model; may say "Confirmed"
harness/<slug>/
  HARNESS.md  SPEC.md
  context/{brief,rubric,example,output-template}.md   # rubric = prose + harness-config JSON (rules, live_source)
  memory/{processed,pending,calibration}.md, run-log.jsonl
  evals/{golden.json,README.md,cases/}
  scripts/ingest.py                      # code intake: new items, text extraction, processed log
  scripts/check_run.py                   # code checks: source, quotes, scale, maths, verdicts, red-flag patterns, golden diff
  scripts/render_report.py               # code writes report.md from scorecards.json (same shape every run)
  scripts/guard.py, guard.sh             # run-time tripwire (see Security)
  runs/<date>/{manifest.json,sources/,scorecards.json,report.md}
```

Codex (`--platform codex`) uses `AGENTS.md` and `.agents/skills/` instead. Its `<slug>-verify` skill runs in `codex exec --skip-git-repo-check --sandbox read-only`. `--platform both` writes both. `scripts/sync_codex_templates.py` generates the Codex templates from the Claude ones.

## Security

What the generated harness enforces in code during a run:
- Bash can run only the three harness scripts, with a bare `python` and no shell operators.
- Reads stay inside the harness folder.
- Writes stay inside `runs/`.
- The verifier is the only subagent that can start. Every other tool is blocked.
- The guard **fails closed**. A crash, bad input or a missing Python blocks the call.
- `ingest.py` refuses to start if the guard hook didn't fire.
- The scripts keep their own paths inside the harness.
- An item is marked processed only if its scorecard passes the checks and appears in a report that code rendered. Items that need a human go to a visible pending list.

Claude Code runs the guard only in a folder you have trusted (accept the trust prompt once). In an untrusted folder it skips project agents' hooks, and the run stops at intake instead of running unguarded.

The guard is a **tripwire, not a sandbox**. It doesn't confine the operating system. For real isolation, turn on Claude Code's `/sandbox` and add deny rules for secret files. Codex harnesses have no guard; they rely on Codex's sandbox. Details are in `references/security.md`. To report a vulnerability, see `SECURITY.md`.

## Install

```bash
git clone https://github.com/MahmudurRahman36/claude-skills.git
cp -r claude-skills/skills/harness-builder ~/.claude/skills/      # Claude Code (or <project>/.claude/skills/)
cp -r claude-skills/skills/harness-builder ~/.agents/skills/      # Codex (or ~/.codex/skills/)
```

The interview step uses the `grilling` skill from https://github.com/mattpocock/skills (MIT) when it is installed. Otherwise it falls back to an embedded copy of that protocol.

## Use

Say "build a harness for <job> in this project" or "set up harness engineering here", or invoke `/harness-builder` (`$harness-builder` in Codex).

## Checks

```bash
python skills/harness-builder/scripts/validate.py                                   # package self-check
python -m unittest discover -s skills/harness-builder/tests -v                      # 51 tests, including 13 attack tests (test_hostile.py)
python skills/harness-builder/scripts/sync_codex_templates.py --check               # Codex templates in sync
python skills/harness-builder/scripts/validate_harness.py --project <p> --slug <s>  # a generated harness
```

Requires Python 3.8+ (stdlib only) and, for the Claude Code guard, `sh` (bundled with Git for Windows; present on macOS and Linux).

## Status and limits

- **Tested:**
  - unit and attack tests, run on Windows;
  - real headless `claude -p` runs on a support-desk fixture: the golden set matched twice with identical results, an injection ticket was flagged and sent to the pending list, and a red-team run with all permissions bypassed was stopped on every bypass attempt (by the guard, the scripts' own checks, or a deny rule).
- **Not yet tested:**
  - macOS and Linux;
  - a live Codex run;
  - a live `<slug>-calibrate` session;
  - inputs that arrive through a connector (sync them into `live_source` first);
  - PDF extraction, which needs `pdftotext` or `pypdf`.
- **Generated files are copies.** A harness generated by an older version keeps its old scripts. Each `HARNESS.md` records the version that generated it (`<!-- generated by harness-builder ... -->`). To upgrade, re-render with `scaffold.py --force` in a scratch folder and copy the new `scripts/` over.

## Glossary

- **Harness:** everything around the model (context files, tools, checks, memory, schedule) that makes its work repeatable.
- **Golden set:** a handful of real items with your own correct answers, re-run after every rule change, like a regression test.
- **Grilling:** an interview that asks about every open decision in one round, each question with a recommended answer.
- **Lethal trifecta:** private data, plus untrusted input, plus a way to send data out. Never give one run all three.

## License

MIT, see `LICENSE`.
