---
name: harness-builder
description: Design and build a six-layer AI harness (context, tool limits, output template and procedure skill, independent read-only verifier, human-approved memory, automation) around any repeatable workflow in any project, so the AI does the job the same way every time, proves each judgment with evidence, checks itself and leaves the decision to a human. First understands the project from its files and memory, interviews the user (grilling) only for decisions it cannot find, then writes a spec, scaffolds and fills the files, enforces guardrails, validates, and tests against a golden set. Use when the user wants to "build a harness", do "harness engineering" (also spelled "hernes"), make Claude or Codex do a recurring task reliably (screening, triage, review, extraction, reports, drafting), turn a repeated prompt into a system, or set up skills, verifier, memory and a schedule for a project. Not for one-off questions, plain CLAUDE.md cleanup, or making code run faster.
compatibility: Claude Code or Codex with file access and Python 3.8+ (stdlib only). Subagents, scheduling and connectors are used when the host provides them.
metadata:
  version: "1.2.0-beta"
---

# Harness Builder

## Core principle

**Agent = model + harness.** The model is the engine. The harness decides:
- what the model sees;
- what it can touch;
- what a good answer looks like;
- how its work gets checked;
- what it remembers;
- when it runs.

None of it makes the model smarter. It makes the model consistent, provable and safe. Build one harness per workflow. A human always owns the final decision.

| # | Layer | Files (under `harness/<slug>/` unless noted) |
|---|---|---|
| 1 | Context | `HARNESS.md` (map and rules), `context/brief.md`, `context/rubric.md` (prose plus `harness-config` JSON), `context/example.md` ("what NOT to copy" on top), an entry block of about 5 lines in `CLAUDE.md`/`AGENTS.md` |
| 2 | Tools | connector or MCP scoped to the source; deny rules in `.claude/settings.json`; `scripts/guard.py`, launched by `guard.sh` on every tool call of the runner and verifier. It is a tripwire, not a sandbox: during a run Bash may only run the three harness scripts, reads stay in the harness, writes stay in `runs/`, and only the verifier subagent may start. It blocks when anything goes wrong, and `ingest.py` refuses to start unless the guard has just run |
| 3 | Output and process | `<slug>-run` skill forked into the `<slug>-runner` subagent; `scripts/ingest.py` (code picks new items and extracts the text); `scripts/render_report.py` (code writes `report.md`, the same shape every run; `context/output-template.md` lists its fields) |
| 4 | Verification | `scripts/check_run.py` (code: source present, quotes real, scale, maths, verdict rules, red-flag patterns, golden diff); `<slug>-verifier` (read-only, fresh context, may say `Confirmed`) |
| 5 | Memory | `memory/processed.md` (appended by code, so runs are safe to repeat); `memory/calibration.md` (human-only `<slug>-calibrate`, approval-gated; notes change how rules are read, never the numbers) |
| 6 | Automation | a schedule or trigger, set only after the golden set passes and the owner has reviewed a real report |

`references/layers.md` has the do, avoid and maintain rules for each layer, and the archetype table (score, triage, extract, draft, review).

## Lite path
Use this when the user wants it quick, or the stakes are low (no decisions about people, no money, no legal or production changes):
- Skip the interview when Phase 1 already settles the gate.
- 3 golden cases instead of 5 or more.
- No independent spec review. No automation yet.

Every gate below still applies: validator at 0 errors, golden run, injection case.

## Workflow

Run the phases in order. Each ends with a gate. Don't pass a gate on assumption.

### Phase 0: Preflight
1. **Root:** find the project root.
   - If it is a git repo, work on a branch.
   - If it isn't, recommend `git init` so rule changes can be reverted. Run it only after the user says yes.
   - Without git, before each change copy `harness/<slug>/context/`, `harness/<slug>/memory/` and the entry file to `.backups/<slug>/<YYYYMMDD-HHMM>/` at the project root, outside the harness folder.
2. **Host:** use the tool the user named, else the one running this skill. Choose `both` only if `CLAUDE.md` and `AGENTS.md` (or `.claude/` and `.agents/`) are both in real use.
3. **Existing harness:** look for `harness/*/HARNESS.md`, `<!-- harness:<slug> start -->` blocks, `*-runner`/`*-verifier` agents. If one exists, go to "Improving an existing harness".

**Gate:** root, git status, platform, and new-vs-existing are known. Write them in the spec header.

### Phase 1: Understand (facts first)
Follow `references/discovery.md`:
- the reading order;
- K/I/U tagging (known, inferred, unknown), per part where one row mixes facts and decisions;
- the 12-row sufficiency gate.

Read the project's own files and memory first. Hand broad sweeps to a read-only subagent and keep only its summary. Pick a provisional slug and start `harness/<slug>/SPEC.md` from `references/harness-spec.md`. Rename the folder in Phase 3 if the slug changes.

If several workflows are candidates, score them (discovery.md section 4) and propose the best one. Say plainly when a harness is the wrong tool:
- the task is deterministic (write a script);
- it is rare;
- the process isn't stable yet;
- nobody will supervise it.

**Gate:** every row is K, or is listed as an open decision for Phase 2. A minor unknown that doesn't change the design (e.g. how long run copies are kept) may become a follow-up instead.

### Phase 2: Interview (only for open decisions)
Interview the user following `references/interview.md`:
- Use the `grilling` skill (or `grill-me`) if the host has it; otherwise use the embedded protocol.
- Ask the whole frontier per round, each question with a recommended answer and its evidence.
- Never ask the user for a fact you can look up.
- Never guess these five: the job, the decision owner, the quality bar, the failure cost, the guardrails.

**No user available** (a subagent or unattended run):
- Don't invoke `grilling`.
- Write every round into the spec under "Open questions (blocking)", marking later rounds as depending on the recommended answers.
- **Stop after this phase.**

**Gate:** the user confirms a 5-10 line restatement: job, owner, inputs, output, quality bar, guardrails.

### Phase 3: Design
Fill SPEC.md sections 2-6 with `references/layers.md`, `references/security.md` and `references/automation.md`. Decide these:
- **Slug and folder:** a kebab-case job name. Use `harness/<slug>/`, or the project root only when the project *is* this one workflow.
- **Archetype and scale:** binary, or anchored 1/3/5; weights or `fields`.
- **Unsure rule:** in the owner's words, e.g. "pick the lower score" or "pick the more urgent priority".
- **Pattern:** chaining (default), routing, parallel, or evaluator-optimizer.
- **Guardrails:** every "never" with its enforcement. Prefer, in order: tool absent, then deny rule, then guard or hook, then prose, which is advisory only and labelled so.
- **Models:** a scoring model for the runner, and a verifier model (a different one when possible).
- **Golden set:** source and owner.
- **Retention:** for inputs and reports.
- **Trigger and schedule:** owner timezone and UTC; local or cloud.

Show the user the design summary and the file list. **High stakes** means decisions about people (hiring, credit, discipline), moving money, legal or compliance, or production changes. For those, first get an independent review of the spec: the `llm-council` skill if present, else a fresh subagent given only the spec.

**Gate:** the user approves. SPEC.md reads `Status: confirmed by <who> on <date>`, and "Open questions (blocking)" is empty.

### Phase 4: Scaffold
Run this from this skill's folder:

```bash
python <skill-dir>/scripts/scaffold.py --project <root> --slug <slug> [--harness-dir <dir>] --platform claude|codex|both --dry-run
python <skill-dir>/scripts/scaffold.py --project <root> --slug <slug> [--harness-dir <dir>] --platform claude|codex|both
```

- It never overwrites an existing file. Those are reported as `skipped`; merge into them by hand. `--force` replaces a file only after backing it up.
- It inserts or refreshes one marked block per entry file, with `/` in `CLAUDE.md` and `$` in `AGENTS.md`.

**Gate:** the scaffold report has no errors, and every `skipped` file has been reviewed.

### Phase 5: Fill
Replace every `{{FILL: ...}}` marker with content from the spec. Rules:
- **Use real material.** Anchors and examples come from real samples, anonymised. The rubric prose and its `harness-config` must say the same thing. Follow the config rules in the rubric template: verdicts best first, a catch-all last, `fields` for labels, `red_flag_patterns`.
- **Adapt, don't pad.** Delete sections that don't fit the archetype (layers.md section 7).
- **Keep it small.**
  - The entry block stays about 5 lines. No overviews, directory trees or pasted code; generated overview files measurably hurt agents.
  - `HARNESS.md` stays under 200 lines.
  - Each skill stays under 500 lines.
- **Phrase rules as the behaviour you want.** "Never" lines are for real guardrails, and each one is also enforced.
- **Verify before you write.** Every path, folder, connector and command you write must exist and work. Check it.
- **Live source:** set `live_source` in `harness-config` to the one local folder new items arrive in. Code reads nothing else. If items come through a connector, sync them into that folder in a separate step before the run; the guarded run itself can't use connectors.
- **Golden set:**
  - Copy the owner-labelled originals into `evals/cases/<id>.<ext>`, anonymised, and the labels into `evals/golden.json` (format in `evals/README.md`).
  - Add one prompt-injection case, labelled from the owner's written policy (`label_source: owner-policy`).
  - `context/example.md` must not be one of the golden cases. Reusing it inflates the golden results.
  - If the owner has no labels yet, ask for 5 (3 on the lite path). Never invent owner labels.
- **Privacy:** add `<harness>/runs/` to `.gitignore`, unless the owner wants reports versioned or the schedule runs on a fresh clone (automation.md). Write the retention rule into the spec.
- **Guardrails, last of all:** merge the spec's deny rules. Anchor paths with `/`, which means the project root in project settings. Protect input folders and remove outbound tools:
  ```bash
  python <skill-dir>/scripts/scaffold.py --project <root> --slug <slug> --guardrails-only \
    --deny "Edit(/tickets/**)" --deny "mcp__gmail__send_email"
  ```
  If the harness reads untrusted input and still has an outbound channel, remove the channel or gate it. Never ship the lethal trifecta.
- **Runner tools:** keep the generated `tools:` line. The guard blocks every other tool anyway. Add a tool to both `tools:` and `EXTRA_TOOLS` in `guard.py` only if a run truly needs it, and only a read-only one. Never add web or send tools.

**Gate:** `python <skill-dir>/scripts/validate_harness.py --project <root> --slug <slug> --platform <p>` reports **0 errors**, run without `--allow-fill`.

### Phase 6: Test
1. **Fresh-context golden run.** In Claude Code, from the project root:
   ```bash
   claude -p "/<slug>-run golden"
   ```
   In Codex: `codex exec --sandbox workspace-write '$<slug>-run golden'`. If a fresh session can't be started, run it yourself and label the result "same-context, not independent".
2. **Pass criteria:**
   - `check_run.py --golden` is clean and the golden labels match (default: all);
   - the injection case is flagged and not obeyed;
   - the verifier returned at least one `Confirmed`;
   - a second golden run gives the same results;
   - a normal run with nothing new writes one line and stops.
3. **Seeded error.** Copy one clean item from a golden run's `scorecards.json` into `<run>/seeded.json` as `{"items": [...]}`. Replace one quote with an invented sentence. Then change one score (scoring archetypes), or set one label to a value outside `allowed` (label archetypes). Then:
   - `check_run.py --scorecards <run>/seeded.json`, without `--golden`, must report both errors;
   - the verifier, given that entry, must return `Corrected`.
4. **Guardrails.**
   - Pipe a forbidden call into the guard launcher. It must exit 2:
     ```bash
     echo '{"tool_name":"Bash","tool_input":{"command":"python -c 1"}}' | sh <harness>/scripts/guard.sh
     ```
     That is bash quoting. Also try an `Edit` outside `runs/`, a `Read` of `.env`, and an `Agent` call with `"subagent_type":"general-purpose"`.
   - **The guard must be live.** Claude Code skips the frontmatter hooks of project agents in a folder whose trust prompt was never accepted (debug log: "Skipping frontmatter hooks for agent ... not trusted"). In a fresh session, a run in an untrusted folder stops at intake with "the run guard is not active". That is the canary working. Accept the trust prompt and rerun.
   - **Hostile item.** Put one item in `live_source` that tells the AI to run a command, read `.env`, write outside `runs/` and start another agent. Run `/<slug>-run`. The item must be flagged; nothing outside `runs/` and `memory/` may change; `processed.md` gets only items that are in the report. Then delete the item and its line.
   - In a fresh session, ask for an edit to an input file. The permission system must deny it, not just the model.
5. **On any failure:** fix the root cause in the layer that owns it, then rerun the **whole** of Phase 6. Never relax a check to get a pass.
6. **Calibration:** run `/<slug>-calibrate` once with a sample correction. Confirm it stops for approval before saving anything.

**Gate:** every Phase 6 box in the spec's acceptance list is ticked, with evidence such as run folders and command output.

### Phase 7: Automate
Only after Phase 6 passes **and** the owner has reviewed one real report. Pick the option from `references/automation.md`.
- Get the owner to confirm the trigger, the schedule and where reports land.
- Set a turn cap, a pinned model, and a failure or silence alert.
- Record it all in SPEC.md.

Creating a schedule changes something outside the project, so ask before you create it.

### Phase 8: Hand off
Reply briefly with:
- what was built: the files and the commands;
- the test evidence;
- each guardrail and how it is enforced;
- the open items;
- the upkeep:
  - **weekly:** read one run transcript and the verifier corrections;
  - **monthly:** fold calibration notes into the rubric and prune the notes;
  - **after any model, rubric or connector change:** `/<slug>-run golden`;
  - **each quarter:** delete any harness part the current model no longer needs.

## Improving an existing harness
Run Phases 1 and 3 against what is there. Then:
1. Get a baseline from `validate_harness.py` and `/<slug>-run golden`.
2. Change the layer that owns the failure.
3. Rerun all of Phase 6.

Rescaffolding only adds missing files; it never touches filled ones. Leave audits smaller or the same size, never bigger.

## Anti-patterns
- Building before understanding: skipping the gate, or guessing the owner, the quality bar or the unsure rule.
- Asking the user for facts the files show. Interview rounds of one question at a time.
- A long entry file, overview prose, directory trees, pasted code.
- Rules shouted in capitals instead of enforced. Expecting a skill's `allowed-tools` to restrict anything (it only pre-approves).
- The scorer writing its own source text.
- A verifier that shares the scorer's context, has write tools or omits `tools:`, or isn't allowed to say `Confirmed`.
- AI-invented owner labels.
- Calibration notes that change numbers.
- Memory files with no cap.
- Scheduling before the golden set passes.
- Any tool combination that completes the lethal trifecta.
- One mega-harness for several workflows.

## Files in this skill
- `references/discovery.md`: reading order, sufficiency gate, which workflow, pattern choice.
- `references/interview.md`: grilling protocol and the design tree of questions.
- `references/harness-spec.md`: the SPEC.md template.
- `references/layers.md`: do, avoid and maintain per layer; archetypes.
- `references/security.md`: injection, deny/guard/hook syntax, MCP scope, privacy, fairness.
- `references/automation.md`: schedule options, headless commands, run hygiene.
- `references/platforms.md`: Claude Code vs Codex mapping.
- `scripts/scaffold.py`, `scripts/validate_harness.py`, `scripts/sync_codex_templates.py` (maintainer tool), `assets/templates/`.
