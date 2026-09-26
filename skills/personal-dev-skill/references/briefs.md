# Delegation and review

Delegate only when enabled, authorized and useful. Keep small fixes inline.
Pass the minimum relevant context, exact acceptance gates and accessible paths; include content only when a worker cannot access those paths.
Do not send every skill or the full conversation to every worker.

```text
Objective:
Inputs: requirements/gates, facts, paths, base revision, relevant diff
Scope: allowed edits, exclusions, dependency order, no extra publication
Acceptance and verification:
Return: findings/changes, evidence and exit codes, limitations; aim for 15 lines
```

Parallel workers need independent tasks and disjoint edit ownership, preferably isolated worktrees. One integrator owns merge/conflict handling and integration tests. Never share mutable database fixtures or release environments without coordination.

Plan review: check missing requirements, assumptions, design alternatives when real, rollback and test coverage.
Code review: inspect working changes as well as committed changes against the chosen base; give concrete failure scenario and file/line.
Requirements QC: PASS/FAIL/BLOCKED per gate, with evidence and environment.
Runtime review: exercise the actual artifact; do not substitute a worker's assertion for logs or observed behavior.
Keep review findings concise without suppressing material defects. Do not pass the desired verdict into an independent evaluation.
