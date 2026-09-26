---
name: personal-dev-skill
description: Carries software work from requirements through investigation, planning, implementation, testing, repair, release and post-live verification. Use for an end-to-end feature, bug list, build-and-deploy request, or PersonalDevSkill workflow; honor narrower diagnose, review and local-only requests.
metadata:
  version: "2.0.0"
---

# PersonalDevSkill

Deliver the requested outcome with the smallest sufficient context, changes and verification.
Use the user's message as input. No provider-specific tool names or extra skills are required.

## Operating contract

- Follow host instructions and applicable project rules (AGENTS.md, CLAUDE.md, GEMINI.md, scoped rules). Read relevant context once; refresh changed facts.
- Preserve user changes, exclusions and existing authorization. A skill never grants new authority. Continue authorized work; ask only for missing decisions or access that materially blocks it.
- Default chat to Caveman-style compression: result first, short sentences, no filler or repeated logs. Use an available caveman skill if compatible. Preserve negation, uncertainty, numbers, units and evidence; use full sentences for consequential decisions. Code, documentation and release text stay normal.
- Discover available skill metadata, then load only the smallest relevant set. Choose one owner per capability; skills are optional accelerators. Read [skill-map](references/skill-map.md) only when selecting specialists.
- Inspect host capabilities before delegation or model routing; use [platforms](references/platforms.md) if needed. No automatic provider switch, credential use, dependency installation or new user-visible tasks.
- Success needs observed evidence for the current revision and environment. Distinguish tested, inferred, unavailable and not applicable. Never convert a blocked gate into PASS.

## Scale effort to risk

S: localized, low risk; brief acceptance statement and inline plan/review.
M: several components; short written plan, targeted review.
L: risky data, security, infrastructure or cross-system behavior; explicit rollback and independent review when available.
File count is only a hint. Compare alternatives only for genuine design choices. No mandatory council, parallelism or duplicate clean test runs.

## Delivery loop

1. **Requirements.** Identify intended users, current/desired behavior, constraints, non-goals, acceptance criteria, deployment target and authorized endpoint. Separate must-haves from preferences. Ask only unresolved questions that change the outcome; continue independent work. For M/L use [plan-format](references/plan-format.md).
2. **Investigate.** Inspect repository state, relevant code, configuration and failure evidence. Reproduce bugs safely; if reproduction is unavailable, label the hypothesis and choose a discriminating check. Assess dependencies, compatibility, data, security and operational impact.
3. **Plan.** Map each acceptance criterion to changes and verification. Order dependent work; note rollback for risky changes. Cover design, implementation, tests and release where in scope. Check the plan for missing requirements before editing.
4. **Execute.** Make focused changes in verifiable increments. Reuse existing conventions; update affected docs and contracts. Delegate only independent work or valuable review, with bounded inputs and disjoint edit ownership; use [briefs](references/briefs.md). Otherwise work inline.
5. **Test.** Run relevant static/build checks and behavior tests. For bugs, demonstrate regression coverage where practical. Exercise actual runtime and user journey: UI needs browser interaction, API needs real requests, integrations need their dependencies. Check errors, permissions and data integrity proportional to risk. A build or healthy container alone is not user-visible verification.
6. **Find weaknesses and repair.** Review requirements coverage, diff, regressions, security and operability. Prioritize reproducible defects and concrete failure scenarios. Fix the cause; rerun affected tests and impacted integration checks. Reuse still-valid evidence. Three unsuccessful attempts trigger a strategy review, not abandonment: isolate the issue, reassess evidence and pursue safe, materially different diagnostics. Stop only at the requested endpoint or a concrete access, authority or external dependency blocker; distinguish unresolved diagnosis from such a blocker.
7. **Release and post-live test.** If authorized/in scope, follow [delivery](references/delivery.md): verify release artifact, push, deployment, and critical behavior in the actual target. Otherwise stop at the requested endpoint and state what remains unverified.
8. **Handoff.** Report outcome, evidence, revision/environment, remaining limitations and next required action. Never claim shipped from a local test or claim deployment from a successful push.

## Context and cost discipline

Search narrowly before reading whole files. Batch independent reads; keep decisive output and paths to detailed logs. Load references just in time. Reuse valid evidence until inputs change. Do not invent token savings percentages or model superiority.
For long work, preserve a compact checkpoint: acceptance gates, facts/hypotheses, decisions, changed paths, tested revision, remaining work and authorization. Save only within permitted task storage; never rewrite persistent memory or compress original evidence automatically.

## Completion gate

Every in-scope acceptance criterion passes with evidence. An unresolved defect violating acceptance means incomplete, not complete with caveats. Distinguish user-accepted residual risk from unmet requirements; never waive a gate unilaterally.
If live delivery is requested, release identity and post-live behavior are verified, or the task is explicitly incomplete with the exact blocker.
For capability/permission limitations, finish useful independent work and state the unavailable gate.

Final format: outcome; changes; tests with results; release/live status; unresolved risks. Keep proportional to task.
