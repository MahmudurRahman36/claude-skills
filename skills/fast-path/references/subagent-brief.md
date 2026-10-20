# Subagent brief

The brief must be self-contained: the subagent has not seen the conversation. Compress the prose at caveman `full` level (fragments, no articles), not `ultra`: a reader with no context misreads abbreviations and arrows. Keep paths, symbols, commands and expected outputs exact. Code stays normal.

## Template

```
GOAL: <one-line outcome>
KNOWN: <facts already established, so nothing is rediscovered: paths, file:line, symbols, decisions, versions>
WRITE: <exclusive write set; nothing outside it>
READ: <read-only files worth opening, with line ranges if known>
DO: <steps or constraints, ≤5 lines>
DONT: edit outside WRITE · commit/push/merge · install deps · widen scope · weaken/skip tests · destructive ops
DONE WHEN: <verify cmd> → <expected result>
RETURN (≤15 lines, nothing else):
  STATUS: ok | blocked | failed
  CHANGED: <file> — <1-line what>
  VERIFY: <cmd> → <result>; on failure, first error line + last ≤5 lines, verbatim
  BLOCKERS: <none | what + why>
  NOTES: <only facts the orchestrator needs to join>
```

## Filled example

```
GOAL: add --csv flag to `export` cmd in repo B
KNOWN: repo B at /work/b; export cmd in todo/cli.py:41 (argparse subparser "export"); writers in todo/out.py; tests: python -B -m unittest
WRITE: todo/cli.py, todo/out.py, tests/test_out.py
READ: todo/cli.py:30-70, todo/out.py
DO: add out.to_csv(tasks) -> str (header row, RFC 4180 quoting); wire --csv on export; 2 tests (empty list, 1 task with a comma)
DONT: edit outside WRITE · commit/push/merge · install deps · widen scope · weaken/skip tests · destructive ops
DONE WHEN: python -B -m unittest (cwd /work/b) → OK
RETURN (≤15 lines, nothing else):
  STATUS: ok | blocked | failed
  CHANGED: <file> — <1-line what>
  VERIFY: <cmd> → <result>; on failure, first error line + last ≤5 lines, verbatim
  BLOCKERS: <none | what + why>
  NOTES: <only facts the orchestrator needs to join>
```

## Isolation

- Shared tree: the subagent's DONE WHEN must not write shared outputs (no build, no test run, no install). Give it a static check instead (grep, import, lint of its own files). The orchestrator runs builds and tests after the join, serially.
- Own worktree: create it in a fresh temp dir (`git worktree add --detach <tmp> HEAD`). Patches go to absolute paths in a separate fresh temp dir `<abs>`, outside both trees. If the node's deps are not in HEAD, seed first: `git diff HEAD --binary --output=<abs>/seed.patch` in the main tree, `git -C <tmp> apply <abs>/seed.patch`, and copy the files listed by `git ls-files -o --exclude-standard`. Always then `git -C <tmp> add -A` and `git -C <tmp> write-tree` → seed tree id T (the join diff base). No commit, so no hooks, signing or identity are needed.
- The orchestrator prepares the worktree's environment (venv, node_modules) before spawning; the subagent still installs nothing and runs Python with `-B`. Reused env with an editable install → check `<pkg>.__file__` resolves under the worktree, else its tests run main-tree code and DONE WHEN proves nothing.

## Orchestrator rules for the join

- Before fan-out, snapshot `git status --porcelain -uall` plus `git hash-object` of every dirty path that exists as a file. After each subagent, compare paths and hashes, ignoring gitignored and cache paths (`__pycache__`, build output): any change outside its WRITE set → node failed, inspect it. In a worktree, run the same check on the worktree's own status. Never auto-revert (the path may hold the user's own uncommitted work).
- Worktree join, one command per call (no `&&`, so it works in PowerShell too): `git -C <tmp> add -A -- <WRITE paths>`, then `git -C <tmp> diff --cached --binary <T> --output=<abs>/node.patch` (only the subagent's change, new files included, caches excluded), then in the main tree `git apply --check <abs>/node.patch` and `git apply <abs>/node.patch`. Check fails → drop that node and redo it serially. Remove the worktree (`git worktree remove --force <tmp>`) and `<abs>` afterwards.
- Read RETURN first. Open the diff when applying a worktree, when VERIFY is not ok, or when the snapshot check flags a path.
- Rerun each node's DONE WHEN yourself, serially, before marking it ✓. A subagent's ✓ is a claim, not evidence.
- STATUS `blocked` or `failed` → do not retry blindly. Fix the brief (missing fact, wrong write set) or take the node serially.
- After all nodes land: apply, resolve conflicts, then run the final gate yourself.
