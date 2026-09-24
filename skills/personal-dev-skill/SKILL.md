---
name: personal-dev-skill
description: Runs the user's end-to-end development loop (PersonalDevSkill) on one task or a task list — investigate root cause, plan (competing plans only when a real design choice exists), independent plan check, execute (parallel only for independent work), runtime smoke test, parallel test/QC/review, fix-until-clean, then commit and push. Token-lean by design. Use when the user hands over coding work to carry end to end — a bug list, feature requests, "do these items", "fix and deploy", "build X and push", "PersonalDevSkill", or pastes several tasks at once — even if they don't list the steps.
compatibility: Claude Code and OpenAI Codex (tool mapping in references/platforms.md). Needs git and a shell.
---

# PersonalDevSkill

Tasks: $ARGUMENTS
<!-- In Claude Code the line above is replaced by the invocation arguments. In Codex (or if it still reads "$ARGUMENTS"), the tasks are the user's request. -->

Start by running `git status -sb` to see repo state.

Works in **Claude Code** and **Codex**. Tool names below are Claude Code's; in Codex use the equivalents in [platforms.md](references/platforms.md).

## Non-negotiables (read first; survive compaction)
1. Chat output in caveman **ultra** (load `caveman` skill if not active). Code, commits, PR text: normal.
2. Project rules win: read CLAUDE.md / AGENTS.md / memory / project context doc once at start. Their deploy, doc-update, read-only-host and secret rules override this skill.
3. **No claim without fresh evidence.** "Done/fixed/passing" requires a command run *this round* whose output you read. Words like "should", "probably", "seems" = not verified. A subagent's "success" is a claim until checked.
4. **No fix without root cause.** Reproduce, locate, explain the mechanism, then change code.
5. Tokens are the budget. Subagents cost ~4x chat and multi-agent ~15x — spawn one only when it buys isolation, parallelism, or an independent opinion. Every subagent gets the brief template in [briefs.md](references/briefs.md) and returns ≤15 lines.
6. Stop and ask (plain prose) only for: irreversible/destructive actions, security-sensitive changes, touching systems the project marks off-limits, or a requirement ambiguity that changes the design. Otherwise keep going.

## Size each task first
- **S** — diff describable in one sentence, 1–2 files: investigate → fix → verify → ship. No plans, no subagents except the final review.
- **M** — several files, one obvious approach: one written plan, one reviewer.
- **L** — design choice, cross-module, risky data/infra: 2–3 competing plans + independent judge + full verify.

## Checklist (copy into TodoWrite / Codex `update_plan` / reply and tick as you go)
```
- [ ] 1 Context: project rules loaded, tasks sized S/M/L, dependencies between tasks noted
- [ ] 2 Investigate: root cause / change surface found, with file:line evidence
- [ ] 3 Plan: per plan-format in references/plan-format.md
- [ ] 4 Plan check (M/L): independent agent verdict adopted or rebutted with evidence
- [ ] 5 Skills: relevant skills chosen from references/skill-map.md
- [ ] 6 Execute: independent tasks in parallel, overlapping files serialized
- [ ] 7 Smoke: real app/runtime exercised for the user-visible behavior
- [ ] 8 Verify: tests + spec QC + code review (parallel), findings triaged
- [ ] 9 Fix loop until two clean rounds (cap: see below)
- [ ] 10 Ship: commit, push, deploy trigger per project rules, post-deploy check
```

## Step notes (only what isn't obvious)
**2 Investigate.** Known target: Grep/Read directly. Broad sweep (many dirs/naming conventions): one `Explore` agent (`model: haiku`), conclusions only. Bug: reproduce first; change one variable at a time.

**3–4 Plan.** Plan = ordered tasks of a few minutes each with exact paths, what changes, and the verification command per task, plus an explicit out-of-scope line. For L, write alternatives that differ in *mechanism*, not wording. Judge = one fresh agent (`model: opus` for L, `sonnet` for M) given the finding + plans; it picks one, lists defects, may merge. Adopt unless code evidence contradicts it.

**6 Execute.** Parallel `Agent` calls in one message only when tasks share no files and no ordering; give each `isolation: "worktree"` if it edits. Identical small edits across many files = one agent, not many. Model: `haiku`/`sonnet` for mechanical 1–2-file edits, `sonnet` for integration, `opus` for architecture. Keep S tasks inline — delegating them costs more than doing them.

**7 Smoke.** Build + unit tests are not a smoke test. Run the actual thing (`run` skill, docker stack, curl the endpoint, Playwright the page) along the user's path. Quote the one decisive output line.

**8 Verify** — launch together in one message:
- Tests: add/adjust tests that fail without the change; run the full relevant suite; read exit code.
- Spec QC (fresh agent): does the result satisfy each requirement? pass/fail per item. Runs first in priority.
- Code review: `code-review` skill (`high` for L/risky, `medium` otherwise); add `security-review` when auth, input handling, secrets, network, or permissions changed.
Triage findings: fix correctness and requirement gaps; drop style nits and speculative hardening — chasing every finding over-engineers.

**9 Fix loop.** Each confirmed issue: root-cause it, re-plan just that issue, redo 6–8. Done = two consecutive clean verify rounds. After 3 failed attempts on the same issue, stop patching: question the approach, try once with a fresh stronger agent, and if still failing report the blocker with evidence instead of burning tokens.

**10 Ship.** Branch off default if on it; conventional commit with required trailers; never stage secrets/credentials; push; run the project's deploy trigger (e.g. tag) if one exists; verify deployed behavior when reachable.

## Final report (caveman, no process narration)
One line per task: `S/M/L | status | change file:line | evidence (cmd → result) | commit/tag`. Then blockers/open risks. Nothing else.
