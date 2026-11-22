# The six layers: how to fill each one

**Core idea:** agent = model + harness. Changing only the harness took one agent from 52.8% to 66.5% on Terminal Bench 2.0 (https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering). Böckeler's terms (https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html):
- **Guides** steer the agent before it acts: context files and skills.
- **Sensors** check its work after it acts: code checks and the verifier.
- Each of them is either computational (code) or inferential (an LLM). Put cheap code sensors first.

## Contents
1. Context
2. Tools
3. Output and process
4. Verification
5. Memory
6. Automation
7. Archetypes: adapting the templates

## 1. Context

**Do**
- Keep the entry file (`CLAUDE.md` / `AGENTS.md`) a map, not a manual. Under 200 lines in total; the harness adds about 5. Detail goes in `HARNESS.md` and `context/`, which load only when the skill runs.
- Write the brief (what the job is) separately from the rubric (how to judge it). A job description says what the role is but not how to judge it, and that gap is where inconsistent answers come from.
- Rubric rules:
  - 3-7 criteria.
  - Every anchor observable and concrete ("specific numbers on outcomes", not "strong results").
  - "Unsure: pick the lower score and flag it."
  - "No quote, no score."
  - Verdict rules written in prose **and** in the `harness-config` JSON, so code can check them.
- Choose the scale:
  - Binary pass/fail is clearer and more consistent (Hamel Husain, Eugene Yan).
  - 1/3/5 with anchors works when strength matters, as in the video.
  - Never use a 1-10 scale.
- Example file: start it with "What NOT to copy". A model given an example will copy its surface features.
- State the human decision owner in one line: "The AI recommends; a human decides."

**Avoid**
- Contradictions between files (the model picks one arbitrarily).
- Vague anchors.
- Long lists of edge cases. Add a rule only after a failure you have seen.

**Maintain**
- Fold approved calibration notes into the rubric every month.
- Re-read the context after every model upgrade; newer models may need less scaffolding.

## 2. Tools

**Do**
- Connect the source where the data already lives (an MCP connector or a CLI) rather than copy-pasting.
- Give each connector the narrowest scope it offers: one folder, read-only where possible.
- Map every "never" in `HARNESS.md` to real enforcement, strongest first:
  1. The tool isn't available at all: a bare tool name in `permissions.deny`, or leave it out of a subagent's `tools:`.
  2. A path or command deny rule: `"Edit(./inbox/**)"`, `"mcp__gmail__send_email"`.
  3. A PreToolUse hook for anything a pattern can't express (see `security.md`).
  4. Prose in `HARNESS.md` only, labelled as advisory in the spec.
- The verifier gets `tools: Read, Grep, Glob`. Omitting `tools` would give it **every** tool.
- The run skill forks (`context: fork`) into the `<slug>-runner` subagent. Its PreToolUse hook (`guard.sh` running `guard.py`, on every tool) allows Bash only for the harness scripts, reads only in the harness, writes only in `runs/` and no subagent except the verifier, for the duration of the run. A model can ignore prose "never" lines, but it can't ignore the guard. The guard is still a tripwire, not a sandbox (`security.md`).

**Avoid**
- Assuming a skill's `allowed-tools` restricts anything. It only pre-approves.
- Bash argument patterns as security: `Bash(curl http://x *)` misses `curl -X GET ...`.
- Wildcard MCP scopes.
- Overlapping tools.

**Maintain**
- Re-check connector scopes whenever a tool is added.
- Read the transcripts for unexpected tool calls.

## 3. Output and process

**Do**
- Use one fixed output template: a summary table first, details only for items that need attention, and a verifier status per item.
- Let code decide what is new and produce the evidence text: `ingest.py` lists items, skips processed ids, and extracts the text to `runs/<run>/sources/`. The scorer never transcribes its own evidence.
- Write the run skill as numbered steps:
  - stop when an input is missing;
  - judge one item at a time, without comparing items;
  - keep machine-readable state as JSON (`scorecards.json`); models overwrite JSON less often than Markdown;
  - end with a short summary.
- Make skills with outside or irreversible effects manual-only (`disable-model-invocation: true`; in Codex, `allow_implicit_invocation: false`). That means sending, publishing, deleting, paying, or changing rules (calibrate). A run that only writes its own run folder and processed log may stay model-invocable.
- Set stop conditions: no new items means one line and stop.

**Avoid**
- Free-form reports.
- Long prompts typed by hand each day.
- Hiding steps inside one giant instruction.

## 4. Verification

**Do**
- Grade in this order: code first, then an LLM, then a human (https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).
  - `check_run.py` does quotes, scale, maths and verdict rules in code.
  - The verifier subagent does judgment: does the quote support the score, were red flags missed, is there bias.
- Keep the verifier independent:
  - fresh context, with no access to the scorer's reasoning;
  - ideally a different model (self-preference bias is plausible but contested);
  - read-only.
- Give it an escape hatch, `Needs a human look`, and make `Confirmed` an explicitly valid answer. Otherwise it invents problems to look useful.
- Seed known-bad cases in the golden set. A sensor that never fires may just be weak.
- Keep two lists apart: `red_flags` (problems in the item, which can block a top verdict) and `uncertain` (the AI's own doubt, which is routed to a human but never changes the rules).
- For high stakes, consider a blind verifier pass: it judges from the source first, then compares with the scorecard, to avoid anchoring on the scorer's answer.

**Avoid**
- The same context checking its own work.
- One judge grading every dimension in a single call when each dimension matters a lot. Split per criterion if the golden set shows confusion.

**Maintain**
- Track how often the verifier corrects the scorer. If it never corrects anything, test it with a seeded error.
- If it corrects too often, the rubric anchors are vague.

## 5. Memory

**Do**
- Two kinds:
  - `processed.md`: what was done, append-only. It makes runs safe to repeat.
  - `calibration.md`: what was learned, human-approved rules only.
- The calibrate skill:
  1. find the root cause;
  2. propose a **general** rule;
  3. list conflicts;
  4. **wait for approval**;
  5. append;
  6. rerun the items and the golden set;
  7. report expected and unexpected changes.
- Keep human-written rules (rubric) apart from AI-proposed rules (calibration).

**Avoid**
- Letting the AI rewrite its own rules unsupervised. Small edits compound into drift.
- Unbounded memory files. Loaders cut files off silently (about 200 lines or 25KB).

**Maintain**
- Keep fewer than 60 calibration notes. Every month the owner merges stable notes into the rubric.
- Version everything in git so any rule can be reverted.

## 6. Automation

See `automation.md`. Schedule only after the golden set passes and the owner has reviewed one real report.

## 7. Archetypes: adapting the templates

| Archetype | Example | Scale | Output | What changes |
|---|---|---|---|---|
| Score / screen | resumes, leads, grant applications | 1/3/5 weighted or binary | ranked table plus details | none; this is the default |
| Classify / triage / route | support tickets, alerts, emails | labels per `fields` | queue per category | no weights; `fields: ["priority","category"]`; the golden set holds `expected_fields` |
| Extract | invoices, contracts, forms | per field: found and correct | JSON plus a review table | `fields` for the extracted values, `claims` with a quote per value; the golden set holds `expected_fields` |
| Draft / generate | replies, summaries, release notes | binary quality checklist | drafts for human edit | rubric = checklist, verdict = Ready / Needs edit; the verifier checks claims against sources |
| Review / audit | PRs, content, compliance | binary per rule | findings list | findings need a quote and a location; Confirmed = no issues |

For binary scales, use `"scale": [0, 1]`, equal or explicit weights, and verdict rules on `min_score`. Delete template sections that don't apply instead of leaving filler.
