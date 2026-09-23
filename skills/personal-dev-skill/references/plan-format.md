# Plan format

Keep each plan ≤15 lines. Write to chat (or `plan.md` for L tasks spanning sessions).

```
Plan <A>: <mechanism in one line>
Out of scope: <what this will not touch>
Risk: <what could break> | Rollback: <how>
Tasks:
1. <path> — <change> — verify: <command>
2. ...
End-to-end check: <runtime scenario proving the user-visible result>
```

Alternatives (L only) must differ in mechanism, e.g. patch at call site vs fix shared helper vs schema change. If you cannot name a real alternative, the task is M — write one plan.
