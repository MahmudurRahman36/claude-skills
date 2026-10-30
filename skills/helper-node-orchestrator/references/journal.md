# Project journal

orch keeps a journal of every task in the project's store: what the task was, the plan, every decision, every step
with its outcome and time, every difficulty and how it was resolved. People read `MEMORY.md`; investigation and
monitoring tools read `journal.jsonl`. Below, `orch` stands for the full command (see [commands.md](commands.md)).

## Files
| file | content |
|---|---|
| `<store>/journal/journal.jsonl` | append-only, one JSON record per line; at 20 MB it is renamed to `journal-<yyyymmdd-hhmmss>.jsonl` and a new file starts |
| `<store>/journal/MEMORY.md` | one readable section per finished task, written by orch when the task ends |
| `<store>/state/journal-current.json` | open tasks, the current one and the sequence counters; orch's own, never edit it |

The store is `<git common dir>/orch/`: inside `.git`, never committed, shared by every worktree. `orch journal path`
prints the two journal files, for example `journal C:/work/shop/.git/orch/journal/journal.jsonl`. `--project DIR`
beats `ORCH_PROJECT`, which beats the current folder. Without a setup yet, `start`, `add` and `end` create the store
where `orch init --apply` will save the setup, so a task started before the setup interview gets its first answer:
inside `.git` in a git repository; outside git only for a folder named with `--project DIR` (or `ORCH_PROJECT`), as
`DIR/.orch/` (with a `.gitignore` of `*`), which the reply's `warnings` names once. Outside git without either, journal calls exit 2 with
`<folder> is not a git repository: pass --project DIR to keep orch's setup and journal in DIR/.orch`; pass
`--project DIR` to `orch journal` and `orch init` alike until the first answer is saved. `show`, `stats` and `path`
never create anything.

## Who writes what
- **orch, automatically, zero agent tokens.** Every orch command run in a project adds one `step` record: verb,
  redacted args, started, ended, `duration_s`, exit, result and the verb's details (below). A non-zero exit adds a
  `difficulty` record (class, detail, fix hint). When the same command later exits 0, orch adds one `resolution`
  record (outcome `pass`) for each difficulty that command left open. The same command means the same verb, job and
  mode flags (`--check`, `--apply`, `--approve`, `--detect`, `--stages`, `--jobs`, `--node`; not `--wait` or
  `--ref`). For `orch init --apply` the top-level answer keys must match too: a good `helper_count` answer never
  resolves a refused `helpers` answer, and an apply whose answer keys orch does not know is never resolved
  automatically. `orch init --apply` and `--approve` also add one `decision` record per setup change, with
  why = "user answer". After a non-zero exit orch prints one line on stderr naming the difficulty it recorded:
  `journal: difficulty #N recorded (orch)`, N being its `seq`. `orch ci` prints none for its jobs: each job's own
  `orch run` records its failure (`orch journal show --last 5` lists them). Not recorded: `orch journal` itself, and
  orch calls in a project with no saved setup yet (before the first `orch init --apply`).
- **The agent, through `orch journal start|add|end`.** One JSON object on stdin, sent with a quoted heredoc so the
  shell expands nothing. Read only the final JSON line of the reply. A bad call exits 2, names the field and shows
  a heredoc that runs as printed.

## The agent's rule
The rule covers every task that uses orch, the setup interview included: the user reads this journal later to see
what each task was, what was decided, what was done, how long it took, what went wrong and how it was resolved.
1. At the start of every task that uses orch (before `orch init` in a new project; in a folder outside git, add
   `--project DIR` here and to `orch init`), record the task:
   ```
   orch journal start <<'EOF'
   {"summary": "fix the failing cart tests", "request": "make the web tests pass again", "plan": ["reproduce with orch run web-unit", "fix the cause", "orch ci"]}
   EOF
   ```
2. After every decision you make (which job, which fix, stop or ask the user), add one record. Add a difficulty
   only for a problem orch cannot see, such as tests that pass while the bug stays, a flaky helper or a limit the
   user set: orch already records every failed orch command and resolves it when that command passes again. Put
   your diagnosis of an orch failure in a `note`. When you fixed one of orch's difficulties some other way, add a
   resolution whose `for` is that difficulty's seq (the stderr line names it; `orch journal show --last 5` lists
   it). The reply to your own difficulty prints its `seq` and the resolution to send when it is fixed. Setup answers
   need no decision of yours: orch records each applied answer itself.
   ```
   orch journal add <<'EOF'
   {"type": "decision", "what": "run web-unit before the full ci", "why": "it is the smallest job that shows the failure", "options": ["web-unit only", "full orch ci"]}
   EOF
   orch journal add <<'EOF'
   {"type": "difficulty", "what": "web-unit passes but the bug stays east of UTC", "detail": "the helpers run in UTC, so no test covers that case"}
   EOF
   orch journal add <<'EOF'
   {"type": "resolution", "for": 3, "how": "added a test that runs with TZ=Asia/Tokyo", "outcome": "fixed"}
   EOF
   ```
   Also available: `{"type": "step", "what": "...", "outcome": "done", "duration_s": 300}` for work done outside
   orch, `{"type": "plan", "steps": [...]}` when the plan changes, `{"type": "note", "text": "..."}`.
   A deploy or rollback command you ran is such work, and a step is its only record, because orch never runs it:
   right after it, add `{"type": "step", "what": "deploy: ./scripts/deploy.sh", "outcome": "ok", "duration_s": 240}`
   (`"rollback: <cmd>"` for a rollback; outcome `failed`, plus a difficulty, when it failed). A rollback after a
   failed `orch live` also gets a resolution whose `for` is that `live-down` difficulty, so the outage never reads as
   if it fixed itself.
3. At the end, close the task, also when it stops blocked or failed. `outcome` is done, partial, failed or blocked
   (abandoned when the user dropped it). When the work session is over, run `orch gc --session-end` before the end:
   its step, and a helper it could not clean, then belong to this task (after the end they go to `untracked`).
   ```
   orch journal end <<'EOF'
   {"outcome": "done", "summary": "cart tests pass on every helper", "lessons": ["pin the time zone in test setup"], "open": []}
   EOF
   ```
- Keep text short and plain. Never put secrets, keys or passwords in it; orch redacts the formats it knows, but
  the rule is not to send them.
- Parallel agents in one project (worktrees share the store): put `"task": "<id from start>"` into every add and
  end, or pass `--task ID`. orch's own step records attach to the most recently started task.

## Task lifecycle
- `start` creates the task id `t<yyyymmdd-hhmmss>-<4 hex>` and makes it current.
- `add` and orch's own records attach to the task named in the JSON, else the current task, else `untracked`.
- `end` writes `task-end` (duration, counts), appends the task's `MEMORY.md` section and clears the current task.
- A task with no record for 12 hours is closed as `abandoned` (by orch) at the next `start`; its duration runs to
  its last record. A task that is open but not idle stays open (it may be another agent's); the start reply lists it
  under `open`.

## Record schema
Every record carries:
| field | meaning |
|---|---|
| `v` | schema version, 1 |
| `ts` | when it was written, ISO 8601 with the local UTC offset, e.g. `2026-10-30T09:04:10.250+01:00` |
| `seq` | sequence number within its task, from 1 (`untracked` has its own sequence) |
| `task` | `t<yyyymmdd-hhmmss>-<4 hex>`, or `untracked` |
| `type` | task-start, plan, decision, step, difficulty, resolution, note, task-end |
| `by` | `agent` or `orch` |
| `since_prev_s` | seconds since the previous record of the same task (absent on the first) |

| type | fields (required in bold) |
|---|---|
| task-start | **summary**, request, plan [], session (the agent session id, when set), closed [] (tasks it closed as abandoned) |
| plan | **steps** [] |
| decision | **what**, **why**, options [] |
| step (agent) | **what**, outcome, duration_s |
| step (orch) | what (`orch <verb> <args>`), verb, args [] (redacted), started, ended, duration_s, exit, result, details {} |
| difficulty | **what**, detail, class, step (seq of the step), fix (orch's hint) |
| resolution | **for** (seq of the difficulty), **how**, outcome (`failed` = an attempt that did not fix it; orch writes `pass`) |
| note | **text** |
| task-end | **outcome**, summary, lessons [], open [], started, duration_s, counts {steps, decisions, difficulties, resolved, unresolved, orch_seconds}, reason (abandoned only) |

Difficulty classes from orch's exit codes: 1 `test-fail`, 2 `usage`, 3 `oom`, 4 `timeout`, 5 `infra`, 6 `r1-guard`,
7 `deploy-gate`, 75 `not-admitted`, other codes `exit-<n>`, a crash `crash`. A verb can set its own: `orch live`
exit 1 is `live-down` (a health URL failed; detail = each failing URL with its error, fix = live's own next step),
and `orch deploy --check` exit 7 stays `deploy-gate` with detail = the first failing gate and fix = that gate's fix.
A difficulty counts as resolved when a resolution of the same task names its seq and that resolution's outcome is
not `failed`.

Details orch records per verb (the keys stats reads): run = `job`, `node`, `jobid`, `result`, `wall_s` (or
`timings.wall_s`), `reason`; ci = per-job lines; init = state, step, applied changes, warnings, `answer_keys` (the
top-level keys of an `--apply` answer, not its values); deploy --check = gates; live = checks. The verb's details
also decide the difficulty: `class`, `reason` (its detail; else `error`) and `fix` replace the exit code's class and
hint, and `result` names the step's result. The journal has no rules of its own per verb.

## Example
```
{"v": 1, "ts": "2026-10-30T09:00:00.000+01:00", "seq": 1, "task": "t20261030-090000-1a2b", "type": "task-start", "by": "agent", "summary": "fix the failing cart tests", "plan": ["reproduce", "fix", "orch ci"]}
{"v": 1, "ts": "2026-10-30T09:01:40.000+01:00", "seq": 2, "task": "t20261030-090000-1a2b", "type": "step", "by": "orch", "since_prev_s": 100.0, "what": "orch run web-unit", "verb": "run", "args": ["run", "web-unit"], "started": "2026-10-30T09:00:40.000+01:00", "ended": "2026-10-30T09:01:40.000+01:00", "duration_s": 60.0, "exit": 1, "result": "test-fail", "details": {"job": "web-unit", "node": "h12", "jobid": "1030090040a1b2", "result": "test-fail", "wall_s": 55.0, "reason": "2 tests failed"}}
{"v": 1, "ts": "2026-10-30T09:01:40.000+01:00", "seq": 3, "task": "t20261030-090000-1a2b", "type": "difficulty", "by": "orch", "since_prev_s": 0.0, "what": "orch run web-unit exited 1 (test-fail)", "class": "test-fail", "detail": "2 tests failed", "fix": "read the failures with orch logs 1030090040a1b2, fix the code, then run the job again", "step": 2}
{"v": 1, "ts": "2026-10-30T09:04:10.000+01:00", "seq": 4, "task": "t20261030-090000-1a2b", "type": "note", "by": "agent", "since_prev_s": 150.0, "text": "the date tests assume the master's time zone; the helpers run in UTC"}
{"v": 1, "ts": "2026-10-30T09:06:50.000+01:00", "seq": 5, "task": "t20261030-090000-1a2b", "type": "step", "by": "orch", "since_prev_s": 160.0, "what": "orch run web-unit", "verb": "run", "args": ["run", "web-unit"], "started": "2026-10-30T09:05:50.000+01:00", "ended": "2026-10-30T09:06:50.000+01:00", "duration_s": 60.0, "exit": 0, "result": "pass", "details": {"job": "web-unit", "node": "h12", "jobid": "1030090550c3d4", "result": "pass", "wall_s": 54.0}}
{"v": 1, "ts": "2026-10-30T09:06:50.000+01:00", "seq": 6, "task": "t20261030-090000-1a2b", "type": "resolution", "by": "orch", "since_prev_s": 0.0, "for": 3, "how": "orch run web-unit passed later (step #5)", "outcome": "pass"}
{"v": 1, "ts": "2026-10-30T09:10:00.000+01:00", "seq": 7, "task": "t20261030-090000-1a2b", "type": "task-end", "by": "agent", "since_prev_s": 190.0, "outcome": "done", "summary": "cart tests pass", "lessons": ["pin TZ in tests"], "started": "2026-10-30T09:00:00.000+01:00", "duration_s": 600.0, "counts": {"steps": 2, "decisions": 0, "difficulties": 1, "resolved": 1, "unresolved": 0, "orch_seconds": 120.0}}
```
Its `MEMORY.md` section:
```
## t20261030-090000-1a2b - 2026-10-30 09:00 - done - 10m 00s

**Summary:** cart tests pass

**Plan:**
1. reproduce
2. fix
3. orch ci

**Steps:**

| step | by | outcome | time |
|---|---|---|---|
| #2 orch run web-unit | orch | test-fail (exit 1) | 1m 00s |
| #5 orch run web-unit | orch | pass | 1m 00s |

**Difficulties:**
- #3 orch run web-unit exited 1 (test-fail): 2 tests failed. Resolved by #6: orch run web-unit passed later (step #5) (pass).

**Notes:**
- #4 the date tests assume the master's time zone; the helpers run in UTC

**Lessons:**
- pin TZ in tests

Counts: 2 steps, 0 decisions, 1 difficulty (1 resolved, 0 unresolved), orch time 2m 00s.
```
An agent step without `duration_s` shows `~<since_prev_s>` as its time: the gap since the record before it.

## Reading it
- `orch journal show` - the newest 50 records, one line each: local time, task, `#seq`, type, by, what, outcome and
  time. `--task ID` = every record of one task, `--last N` = the newest N (0 = all), `--json` = one record per line.
- `orch journal stats` - tasks by outcome (and still open) with mean / p50 / p95 duration; per orch verb and per job
  (from `run` steps) the same numbers plus failures and results; failure classes; helpers used; unresolved
  difficulties of real tasks; the most common difficulty texts. `--days N` = only the last N days, `--json` = one
  object for tools.
- jq on the files (run in the folder `orch journal path` names; add `journal-*.jsonl` for older records):
  ```
  jq -c 'select(.type=="difficulty")' journal.jsonl
  jq -c 'select(.task=="t20261030-090000-1a2b" and .type=="decision") | {what, why}' journal.jsonl
  jq -r 'select(.type=="step" and .verb=="run") | [.details.job, .duration_s] | @tsv' journal.jsonl | sort -k2 -n | tail
  jq -r 'select(.type=="difficulty") | [.ts[0:10], .class] | @tsv' journal.jsonl | sort | uniq -c
  cat journal-*.jsonl journal.jsonl | jq -c 'select(.type=="task-end" and .counts.unresolved > 0) | {task, summary}'
  ```
  A line torn by a crash is not valid JSON: tools should skip lines that do not parse (jq: `jq -R 'fromjson?'`).

## Monitoring ideas
- Tail `journal.jsonl` with any log shipper that reads JSON lines; rotation renames the file, so follow by name.
- Alert on a task ending with unresolved difficulties, on abandoned tasks, on any `live-down` (a health URL failed
  after a deploy), and on rising `infra`, `r1-guard` or `not-admitted` classes.
- Chart weekly `stats --json`: job p50 / p95 per job (a slow-down shows as p95 growth), tasks by outcome, failure
  classes, the share of runs per helper.
- A long `since_prev_s` inside an open task means the agent waited or got stuck there.
- For an investigation, `orch journal show --task ID` gives the whole story: plan, decisions, steps, difficulties
  and fixes, in order.

## Secrets and limits
- Args: the value after `-e`, `--env`, `--build-arg` keeps only its KEY (`KEY=***`); the value after a flag whose
  name contains token, pass, secret or key (and `--flag=value` forms of it); any `KEY=value` whose KEY does; the
  password in `user:password` after `-u` or `--user`.
- Every text (agent and orch, error messages included) loses the secret value and keeps what is around it
  (`--password [REDACTED]`): private key blocks; AWS keys; GitHub, GitLab and Slack tokens; bare `sk-`, `sk-proj-`,
  `sk-ant-` and `AIza` API keys; JWTs; `Bearer` and
  `Authorization:` values; URL passwords (`https://user:[REDACTED]@host`); Slack, Discord and Telegram webhook paths;
  URL query credentials (`?key=`, `?code=`, `?sig=`, `?token=`); the value of a secret option (`--password X`,
  `--api-token=X`, `--pw`, `--passphrase`, `sshpass -p X`, `mysql -pX`, `docker login -p X`, `redis-cli -a X`); the
  password of `-u user:password` (`curl -u`, `--user`); and `NAME=value` or `NAME: value` for secret names
  (`DB_PASSWORD=`, `PGPASSWORD=`, `DB_PASS=`, `SECRET_KEY=`, `X-Auth:`). That covers every form `orch init` refuses in
  a command or URL. `$VAR` references, file-style names (`TOKEN_FILE=`, `--password-file`) and docker's `-u uid:gid`
  stay as written.
- stdin is never stored (the answers of `orch init --apply` reach the journal only as decision summaries and the
  names of their top-level keys).
- Each text is capped at 2000 characters, each list at 100 items; journal files are 0600 on POSIX.
- The user can turn writing off (`ORCH_JOURNAL=0`, used by the test suites); reading still works. The user may
  delete the journal folder; orch starts a new one.
