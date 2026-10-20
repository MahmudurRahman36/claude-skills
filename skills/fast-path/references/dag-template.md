# DAG template

Use the table form for LARGE tasks and the inline form (one line per node) for STANDARD tasks. Print the DAG once as visible text, in the same turn as the first edit call and before it. On a host that shows only the final message (subagent, headless), put it as the first block of the final reply. Telemetry `nodes` = distinct node ids printed, gate included. Headers are abbreviated on purpose.

## Columns

| col | meaning |
|---|---|
| id | `N1`, `N2`… Prefix `*` when the node is on the critical path (the longest dependency chain; tied longest chains → star all) |
| goal | one outcome, ≤10 words |
| in | what the node needs (facts, read-only files, outputs of other nodes) |
| out | what it produces (diff, file, fact) |
| files | write set |
| dep | ids that must finish first (their own verify ✓), `-` if none |
| verify | exact command or check with expected result; cheaper than the final gate and asserting this node's change |
| risk | `low` / `med` / `high`. `high` → full path, no shortcuts except S3/S9, dry-run probe before an action, destructive steps confirmed |

## Template

```
| id | goal | in | out | files | dep | verify | risk |
|---|---|---|---|---|---|---|---|
| *N1 | | | | | - | | |
| N2 | | | | | - | | |
| *N3 | | | | | N1 | | |
| *G | final gate | all | report | - | all | full suite + independent check | <highest risk of its inputs> |
batches: B1 {N1,N2} → B2 {N3} → G
```

Rules:
- Two nodes share a write file → add a dep edge or merge them into one node. No exceptions.
- If a node's `in` names another node's output, that node is in `dep`.
- Hidden deps (shared config, lockfile, generated code, migration order, global state, build output, test DB) get an explicit edge.
- A non-gate node's verify never runs another node's output (for example the test another node writes). Merge the two nodes, or give the node its own verify (a real run or an inline assert). The gate runs everything.
- `G` is always last, never skipped, and is always on the critical path.
- Batches list what runs concurrently. A node joins the first batch after all its deps are done.
- Read-only calls and edit-tool calls on disjoint files may share a batch. Shell writers (codemods), builds, tests and installs run serially unless each has its own worktree.

## Example 1: STANDARD (inline form)

Request: add a `--csv` flag to the `export` command of a small todo CLI.

```
*N1 | add out.to_csv | out.py | to_csv(tasks)->str | todo/out.py | - | python -B -c "from todo.out import to_csv; assert to_csv([]).startswith('id,')" | low
*N2 | --csv on export | N1, cli.py | flag wired | todo/cli.py | N1 | real `python -B -m todo.cli export --csv` on temp copy → parses with csv.reader | low
*N3 | tests | N2 | 2 tests | tests/test_out.py | N2 | python -B -m unittest tests.test_out → OK | low
*G | gate | all | report | - | N1,N2,N3 | python -B -m unittest → OK + real `export --csv` on temp copy matches data rows | low
batches: layout call (not a batch) → B1 {read out.py, cli.py, test_out.py} → N1 → N2 → N3 → G
```

No subagents: each node is under 5 tool calls (fan-out rule 2 fails).

## Example 2: LARGE (table form)

Request: rename `log.emit()` to `log.write()` across 18 service modules, update the docs, and keep a deprecation shim.

| id | goal | in | out | files | dep | verify | risk |
|---|---|---|---|---|---|---|---|
| *N1 | count call sites | repo | site list (n=47) | - | - | `git grep -c "log\.emit(" -- src` sums to 47 | low |
| *N2 | add `write`, keep `emit` as shim | N1 | diff | src/log.py | N1 | `python -B -c "from src import log; log.write('x')"` exits 0 | med |
| *N3 | codemod call sites (S4) | N1 list | diff | tracked src/services/*.py (18) | N2 | `git grep -n "log\.emit(" -- src/services` → 0 hits; changed sites = 47 | low |
| N4 | update docs | docs/logging.md | diff | docs/logging.md | - | `git grep -n "emit(" -- docs/logging.md` → only the deprecation note | low |
| *N5 | shim warning test | N2 | diff | tests/test_log.py | N2 | `python -B -m unittest tests.test_log` → OK | low |
| *G | final gate | all | report | - | N3,N4,N5 | full suite OK + run one service entrypoint against a temp copy of the config | med |

```
batches: B1 {N1 grep, N4 read} → N2 → N3 codemod → B2 {N4 edit, N5 edit} → G
critical paths (tied): N1 → N2 → N3 → G, N1 → N2 → N5 → G
fan-out ✗: rule 2
```

When fan-out would apply: the same change across three separate repos, each with its own test suite (disjoint write sets, ≥5 calls each, own verify, separate trees).
