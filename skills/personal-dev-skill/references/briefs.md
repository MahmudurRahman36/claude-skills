# Subagent briefs

Every delegation states objective, inputs, boundaries, output format. Pass paths, not file contents. Name the model explicitly.

## Template
```
Objective: <one sentence>
Context: <finding / plan excerpt, ≤10 lines>; files: <paths>
Boundaries: <read-only | may edit only X>; do not <push, touch Y, refactor unrelated code>
Done when: <verifiable condition + command to run>
Return (≤15 lines): verdict first; then file:line evidence; then commands run with result. No prose summary of what you read.
```

## Plan judge (step 4)
```
Objective: pick the best plan for <task>. You did not write these; be adversarial.
Context: finding + plans A/B/C below.
Return: chosen plan (or merge), top 3 defects with evidence from the code, missing verification. ≤15 lines.
```

## Implementer (step 6)
```
Objective: implement plan tasks <n..m> exactly; nothing beyond them.
Boundaries: edit only <paths>; match surrounding style; no new deps without saying so.
Done when: <test command> passes and you have read its output.
Return: files changed, commands run + exit codes, anything you could not do.
```

## Spec QC (step 8)
```
Objective: check the change against requirements, not code style.
Context: requirements list; diff: `git diff <base>...HEAD`.
Return: per requirement PASS/FAIL + evidence (command output or file:line).
```

## Code reviewer (step 8)
```
Objective: find correctness bugs, regressions, requirement gaps in `git diff <base>...HEAD`.
Boundaries: report only issues with a concrete failure scenario; no style nits.
Return: ranked findings: file:line, scenario → wrong result, suggested fix.
```
