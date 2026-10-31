# orch commands

Always typed in full: `bash ${CLAUDE_SKILL_DIR}/scripts/orch <verb>` (Codex: `bash ~/.codex/skills/helper-node-orchestrator/scripts/orch <verb>`). Below, `orch` stands for that full command. Samples marked "real" were copied from the CLI in a temporary git project with unreachable documentation addresses (only the project path is shortened to `C:/work/shop`); the others show the exact format with illustrative values, because they need a reachable helper.

## Output
`run`, `ci`, `init`, `deploy`, `live` and `journal start|add|end` end with one JSON line: read only that line. The other verbs print short plain lines (`plan`'s result is its last line, `pick <node>` or `pick none: <reasons>`); `nodes --json`, `inventory --json` and `journal show|stats|path --json` print JSON instead. A missing or unapproved setup (exit 2) and an error inside orch print one JSON line on every verb.

## Flags, not environment
The agent sets no orch environment variable (the guard denies the user-only ones, RULES.md rule 8). `--project DIR` picks the project for `init`, `deploy`, `live` and `journal` (it beats `ORCH_PROJECT`; other verbs use the project of the current folder), and `-v` / `--verbose` on `run` shows full progress. In a folder outside git, `journal` and `init` need `--project DIR` until the first setup answer is saved in `DIR/.orch/`; from then on, calls from inside the folder find it themselves. The user's variables are in README.md.

## Exit codes
0 pass, 1 test-fail, 2 usage/refused, 3 oom, 4 timeout, 5 infra, 6 r1-guard, 7 deploy gate not met, 75 not-admitted. Any other code = infra: stop and report. `orch ci` exits with its worst job, in this order: 6 > 5 > 2 > 75 > 3 > 4 > 1 > 0.

Every verb that uses the setup (`run`, `ci`, `plan`, `nodes`, `inventory`, `gc`, `selftest`) refuses a missing or unapproved one with exit 2 and a JSON line naming what to do. Real:
```
{"result": "usage", "exit": 2, "error": "no orch setup here: run orch init", "next": "run orch init"}
{"result": "usage", "exit": 2, "error": "the orch setup of C:/work/shop is not approved yet: run orch init to review and approve it", "next": "run orch init"}
```

**Journal line.** In a project, every orch call is journaled by orch itself. After a non-zero exit, stderr ends with one line naming the difficulty orch recorded (real): `journal: difficulty #4 recorded (orch)`. That difficulty is resolved automatically when the same command passes later; never add a copy ([journal.md](journal.md)).

## init
Setup status and interview; every call ends with one JSON line (`state`, `step`, `summary`, `problems`, `ask`, `next`, `applied`, `warnings`, and `error` with exit 2). Flags:
| flag | use |
|---|---|
| (none) | status after re-probing every saved host |
| `--offline` | status from saved state, no probe |
| `--apply` | answer JSON on stdin (quoted heredoc) |
| `--detect` | re-run job detection, print the proposal and the differences from the saved jobs |
| `--approve` | pin images, record the approval, write the guard view; only after the user's yes (the guard asks) |
| `--show` | the full saved setup, readable |
| `--ask deploy` / `--ask production` | print the deploy gate's question for a "later" or missing deploy or production answer as the `ask` (with a deploy command saved but no health URL: the health URL question `orch live` asks); nothing probed or saved |
| `--project DIR` | the project folder |

Walk-through, answer keys and problem classes: [init.md](init.md). A fresh project (real):
```
{"state": "new", "step": "count", "summary": ["no orch setup yet for C:/work/shop"], "problems": [], "ask": {"kind": "choice", "header": "Helpers", "question": "How many helper servers will orch use?", "options": ["1", "2", "3", "4+"], "multi": false, "answer_key": "helper_count", "example": {"helper_count": 2}}, "next": "ask", "applied": [], "warnings": []}
```
Answering, then the next question (real; no `~/.ssh/config` candidates, so the question is text):
```
bash ${CLAUDE_SKILL_DIR}/scripts/orch init --apply <<'EOF'
{"helper_count": 2}
EOF
```
```
{"state": "incomplete", "step": "helpers", "summary": ["helpers: none yet", "production: not answered yet", "deploy: not answered yet", "alerts: not answered yet", "jobs: not chosen yet"], "problems": [], "ask": {"kind": "text", "header": "Helper ssh", "question": "For each of the 2 helpers, what do you type after ssh? (alias, or user@IP[:port]; comma separated)", "options": [], "multi": false, "answer_key": "helpers", "example": {"helpers": ["deploy@192.0.2.11", "deploy@192.0.2.12"]}}, "next": "ask", "applied": ["helper_count: 2 helpers"], "warnings": []}
```
`--apply` of two unreachable helpers prints one line per probed host, then the fix step (real, JSON shortened):
```
h11 (deploy@192.0.2.11) problem: network (see problems)
h12 (deploy@192.0.2.12:2222) problem: network (see problems)
{"state": "incomplete", "step": "fix", ..., "problems": [{"host": "h11 (deploy@192.0.2.11)", "what": "ssh: connect to host 192.0.2.11 port 22: Connection timed out", "tried": "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 -o LogLevel=ERROR -o ServerAliveInterval=15 -o ServerAliveCountMax=3 deploy@192.0.2.11 true", "fix": "check that 192.0.2.11 is up and that port 22 is reachable from this machine (VPN, firewall)"}, ...], "ask": {"kind": "choice", "header": "Fix", "question": "h11 (deploy@192.0.2.11): ssh: connect to host 192.0.2.11 port 22: Connection timed out -> check that 192.0.2.11 is up and that port 22 is reachable from this machine (VPN, firewall)\n...\nRun the fixes shown, then choose retry; or remove the hosts.", "options": ["retry", "remove h11, h12"], "multi": false, "answer_key": "remove_hosts", "example": {"remove_hosts": ["h11", "h12"]}}, "next": "fix", "applied": ["helpers: h11 (deploy@192.0.2.11) added; h12 (deploy@192.0.2.12:2222) added"], "warnings": []}
```
`--detect` (real):
```
jobs detected:
  api-lint   lint   python 3.10  api/  2 GB  /src/.venv/bin/ruff check .
  api-test   unit   python 3.10  api/  2 GB  /src/.venv/bin/python -m pytest -q -p no:cacheprovider
  api-image  image  docker       api/  4 GB  docker build -f api/Dockerfile api
  web-lint   lint   node 22      web/  2 GB  npm run lint
  web-test   unit   node 22      web/  2 GB  npm run test
{"state": ..., "next": ...}
```
`--show` (real, before the setup step was answered):
```
project C:/work/shop (setup in C:/work/shop/.git/orch)
host h11 (deploy@192.0.2.11): helper only; jobs not chosen yet; facts unknown; probed 2026-10-30T17:42:04 with a problem
host h12 (deploy@192.0.2.12:2222): helper only; jobs not chosen yet; facts unknown; probed 2026-10-30T17:42:04 with a problem
production: not answered yet
deploy: not answered yet
alerts: not answered yet
jobs: not chosen yet
policy overrides: none
approval: never approved
{"state": ..., "next": ...}
```
A refused answer exits 2 and saves nothing of it: the JSON line is the real current status (what `--offline` shows, the pending `ask` included) plus `error`, and stderr repeats the error as `orch init: <error>`. Real, on a fresh project:
```
{"state": "new", "step": "count", "summary": ["no orch setup yet for C:/work/shop"], "problems": [], "ask": {"kind": "choice", "header": "Helpers", "question": "How many helper servers will orch use?", "options": ["1", "2", "3", "4+"], "multi": false, "answer_key": "helper_count", "example": {"helper_count": 2}}, "next": "ask", "applied": [], "warnings": [], "error": "unknown answer key 'colour'; valid keys: helper_count, helpers, hosts, production, deploy, alerts, jobs, jobs_remove, remove_hosts, policy"}
```
A secret in a command or URL is refused the same way (real `error`s: `deploy.command seems to hold a secret; orch saves, prints and journals this command, so read the secret from a file or an environment variable inside your script instead`, `deploy.health: https://... holds a credential (a password, a token or a key); orch saves and prints these URLs, so use one without credentials`; the forms: [init.md](init.md#step-3-setup-one-message)). Ready (later sessions): only the JSON line, `"summary": ["setup ok: helpers h11 (app-builder) all, h12 (deploy@192.0.2.12) tests | prod h20 (deploy@192.0.2.20:2222), h12 | 4 jobs | approved 2026-10-30"]`, `"next": "ready"`.

## ci
`orch ci [--stages lint,unit,build,image,e2e | --jobs a,b] [--ref R | --wt] [--keep-going] [--wait S]`
Runs stages in order (lint+unit together, then build+image, then e2e); the jobs of one stage run in parallel as separate `orch run` processes, queued by admission. Stops after a red stage unless `--keep-going` (later jobs are listed as `skipped`). `--jobs` runs exactly the named jobs; `--wait` is the admission wait per job (default 600). Prints one reply-ready line per job, then the final JSON; exit = worst job, and the JSON's `next` is that job's. A pass names the next stage group (or `done`); a failure names the phase: `Development` (test-fail, oom), `Investigation` (timeout, and a failing `e2e` suite), `fix the call` (usage), `stop` (infra, r1-guard, not-admitted). A job of the chosen stages whose image is not pinned is named, never dropped: its line reads `<job> - usage 0s | job <job> cannot run yet: its image <ref> is not pinned to a digest; run orch init --approve (it pins images) | next: run orch init` and its stage counts as red; when no job of those stages can run, ci exits 2 at once (real: `{"result": "usage", "exit": 2, "error": "job api-lint cannot run yet: its image python:3.12-slim is not pinned to a digest; run orch init --approve (it pins images)"}`). Format:
```
api-lint h11 pass 38s | - | next: build+image
web-unit h12 test-fail 96s | tests failed: FAIL cart.spec add-item | next: Development
failed: FAIL cart.spec add-item, FAIL cart.spec remove-item
api-image - skipped 0s | not run: lint+unit is red (--keep-going runs it) | next: -
{"result": "test-fail", "exit": 1, "jobs": [{"job": "api-lint", "stage": "lint", "result": "pass", "exit": 0, "node": "h11", "wall_s": 38.2, "reason": null, "failures": null, "jobid": "1030091200c3d4", "sha": "<40-hex commit>", "wt": false, "next": "build+image"}, ...], "next": "Development"}
```
A failed job's own difficulty is journaled by its `orch run`; `orch journal show --last 5` lists its number.

## deploy --check
`orch deploy --check [--ref HEAD] [--project DIR]`. Checks seven gates in order. Prints a line only for each failing or warning gate (`FAIL|warn <gate>: <detail>`), then one summary line (`ready: ...` or `NOT ready: ...`, counting the gates by status), then `{"ready", "gates": [{"name", "ok", "status", "detail", "fix"}], "next", "ref", "sha"}` with all seven gates and their fixes, plus `"ask"` when the production gate fails on a "later" or missing deploy or production answer (relay it, answer with `orch init --apply`). Relay the printed lines as they are; `next` is the first failing gate's fix. Exit 0 = ready, 7 = a gate is not met, 2 = usage. orch never deploys: `orch deploy` without `--check` exits 2. Gate list: [delivery-loop.md](delivery-loop.md), phase Deploy. Real, a new project with deploy "later":
```
FAIL setup approved: this setup was never approved
FAIL production answered: h12 (deploy@192.0.2.12:2222); the deploy command is "later": not decided yet
FAIL HEAD pushed: no upstream branch: push first (there is no git remote yet: git remote add origin <url>, then git push -u origin main)
warn required jobs: no saved job has a stage in lint, unit, so nothing was tested for this commit
NOT ready: 3 failed, 1 warning, 2 skipped, 1 ok (7 gates)
{"ready": false, "gates": [...], "next": "orch init (finish the setup and approve it, only after the user says yes)", "ref": "HEAD", "sha": "<40-hex commit>", "ask": {"kind": "text", "header": "Deploy", "question": "A deploy needs the deploy command (defaults in [brackets]; reply with the command, or later):\nDeploy command - runs on THIS machine and must reach production itself, e.g. ./deploy.sh [later]\nHealth URL(s) checked after a deploy [-]\nRollback command (optional) [-]", "options": [], "multi": false, "answer_key": "deploy", "example": {"deploy": "later"}}}
```
The ask's `example` is exactly what its [brackets] say, so a plain "ok" keeps "later" and saves nothing the user did not give; a command the user names goes in as `{"deploy": {"command": "./scripts/deploy.sh", "health": ["https://shop.example/health"]}}`. A saved deploy command without a health URL is a warning (`orch live` would have nothing to check). `orch deploy` alone (real): `{"result": "usage", "exit": 2, "error": "orch does not deploy: run orch deploy --check, then the deploy command saved in the setup", "next": "orch deploy --check"}`. Ready: `next` = `run the deploy command: ./scripts/deploy.sh, then orch live`; run it only when the user asked to deploy in this conversation, then journal it yourself, because orch never runs it: `orch journal add` with `{"type": "step", "what": "deploy: ./scripts/deploy.sh", "outcome": "ok", "duration_s": 240}` (outcome `failed`, plus a difficulty, when it failed).

## live
`orch live [--project DIR]`: GET every saved health URL in parallel (10 s timeout, 3 tries, 2 s then 4 s apart); 2xx or 3xx = up, a redirect is never followed. One line per URL, then `{"ok", "checks": [...], "next"}`. Exit 0 = all up, 1 = any down (`next` names the rollback command; run it only when the user asks, then journal it as a step `"what": "rollback: <cmd>"`), 2 = no health URL saved (the JSON line carries the question as `ask` and `next` `orch init --ask deploy`: relay it, `--apply` the answer) or no setup. Format:
```
ok   https://shop.example/health 200 0.21s
FAIL https://shop.example/api/health 503 0.05s (3 tries): HTTP 503 Service Unavailable
```
Real, a deploy command saved without a health URL (exit 2; stderr repeats the error as `orch: ...`):
```
{"ok": false, "checks": [], "error": "no health URL is saved for C:/work/shop, so orch live has nothing to check: relay the ask (orch init --ask deploy prints it again) and save the answer with orch init --apply under the key deploy", "next": "orch init --ask deploy", "ask": {"kind": "text", "header": "Health URL", "question": "No health URL is saved, so nothing checks the app after a deploy (defaults in [brackets]; reply with the URL(s), or ok for none):\nHealth URL(s) checked after a deploy, comma separated [-]", "options": [], "multi": false, "answer_key": "deploy", "example": {"deploy": {"health": []}}}}
```
The answer, for example `{"deploy": {"health": ["https://shop.example/health"]}}`, keeps the saved command and needs no new approval. With the deploy command still "later", the ask is the whole deploy question and the error adds that a new deploy command needs `orch init --approve` after the user says yes.

## nodes
Live probe, one line per helper (brief default); the first line names the setup in use. Flags: `--all` (full table + tags), `--json` (one JSON object per helper; the setup line goes to stderr so stdout stays pure JSON). Exit 0 also when a helper is down. Format (the `down` line is real, its reason cut short):
```
config: project C:/work/shop
h11 ok docker 10.8G/15.6G psi=0.0 load=0.12/8 -
h12 down unreachable: ssh: connect to host 192.0.2.12 port 2222: Connection tim -
```

## plan <job>
Where a job would run and why; one line per helper (brief default): `<node> admit <score> ok`, `<node> skip - <reasons>` (filtered out before the probe: scope, tags, cordon) or `<node> skip <score or None> <reasons>` (probed and not admitted; `None` when the probe failed). The last line is the result. Flag: `--all` (every check and the score breakdown). Format:
```
config: project C:/work/shop
h11 admit 0.71 ok
h12 skip - job class api-image not in opt-in list
pick h11
```
No helper: last line `pick none: <reasons>`, exit 75. Real, two unreachable helpers:
```
config: project C:/work/shop
h11 skip - reachable: unreachable: ssh: connect to host 192.0.2.11 port 22: Connection timed out
h12 skip - reachable: unreachable: ssh: connect to host 192.0.2.12 port 2222: Connection timed out
pick none: h11: reachable: unreachable: ssh: connect to host 192.0.2.11 port 22: Connection timed out; h12: reachable: unreachable: ssh: connect to host 192.0.2.12 port 2222: Connection timed out
```

## run <job> [-- args]
Runs one job; blocks until it ends. Final stdout line = result JSON; process exit = `exit`. stderr by default: milestone lines (pick, start, prep done, tests start, end, cleanup) plus `warn: ...` lines; `-v`/`--verbose` = full progress.
| flag | use |
|---|---|
| `--node <n>` | `shell-investigate` only |
| `--ref <commit>` | test that commit (default HEAD of the job's repo) |
| `--wt` | include uncommitted and untracked files (still secret-scanned); never counts for the deploy gate |
| `--wait <s>` | seconds to wait for admission (default 600) |
| `--lease <s>` | lease seconds (default from policy) |
| `--heartbeat <s>` | lease heartbeat interval |
| `--no-retry` | skip orch's own retry (oom 1.5x, infra on another helper) |
| `-v`, `--verbose` | full progress on stderr |

Result keys, in this order:
```
{"result": "pass", "exit": 0, "job": "api-unit", "jobid": "1030091512a1b2", "node": "h11", "wall_s": 212.4, "wait_s": 1.8, "overhead_s": 6.1, "failures": [], "reason": null, "results": "<dir>", "log": "<path>", "sha": "<40-hex commit>", "wt": false}
{"result": "test-fail", "exit": 1, ..., "failures": ["FAIL CartTests.Add_item_twice"], "reason": "tests failed: FAIL CartTests.Add_item_twice", ...}
{"result": "oom", "exit": 3, ..., "reason": "out of memory at 3g", ...}
{"result": "timeout", "exit": 4, ..., "reason": "timeout after 1200s", ...}
```
`reason` is null on pass. `overhead_s` excludes `wait_s`. `failures`: at most 20 lines, each at most 200 characters; also stored in `results/<jobid>/meta.json`. In project mode every finished run is appended to `state/runs.jsonl`. An unknown job exits 2 and lists the known ones; a job left out because its image is not pinned says so and points at `orch init --approve`.

Built-in jobs: `shell-investigate` (read-only, any helper whose scope is not `none`, throwaway container, cannot see the host; `orch run shell-investigate --node h12 -- uname -a`) and the self-test jobs `t-ok`, `t-fail`, `t-sleep`, `t-oom`, `t-hold`. Project jobs come from the setup (`orch init --show`).

## status
Live jobs, cordons and holds, the last N finished, agent objects per helper. Flags: `--local` (no helper calls), `--last N` (default 10). Format:
```
session <id> owner <user> home <ORCH_HOME>
live jobs: none
recent jobs:
  1030091512a1b2 api-unit         h11  pass      total=219.3s work=212.4s overhead=6.1s ref=HEAD
h11: agent objects: none
```

## logs <id>
Brief by default: one meta summary line, up to 20 failure lines (meta.json `failures`, else the job's fail_patterns), last 80 log lines. Flags: `--tail N` (last N lines), `--all` (full log; redirect it to a file and grep it, never into context). Format:
```
1030091530e5f6 job=web-unit node=h12 result=test-fail exit=1 wall=96s lines=912 log=<path>
failures:
  FAIL cart.spec add-item
--- last 80 of 912 lines (--all for the full log)
```

## inventory <node>
Read-only: disk totals, agent-owned items, fingerprint verdict. The brief default never runs `docker system df -v`. Flags: `--all` (adds the -v tables), `--top N` (only with `--all`: top N rows per table), `--json` (brief JSON on stdout; the fingerprint verdict is its `fingerprint` key; errors go to stderr). Format:
```
h12 images 41.2G containers 3.1G volumes 18.0G build-cache 0.9G
agent: agentx-cache-npm 2.1G
fingerprint: unchanged
```
Everything not agent-owned belongs to someone else: show the list to the user; the user removes items or names exactly which ones; confirm before acting.

## gc --session-end [--node NAME]
End of a work session, before `orch journal end` (so its step, and a helper it could not clean, land in the task): removes expired and session agent items on every helper; never touches running jobs (`live jobs remain (other sessions)` then says what was kept). Exit 0, or 5 when a helper could not be cleaned (real: `h11: NOT cleaned (ssh: connect to host 192.0.2.11 port 22: Connection timed out); run gc again when it is reachable`). `--node NAME` cleans one helper of the approved setup only: exit 0 clean, 5 unreachable or orch objects still there, 75 a live job on it, 2 unknown node (real: `unknown node h99 (known: h11, h12): orch gc --node cleans a node of the setup; orch init cleans a helper itself before it leaves the setup`); `orch init` runs it by itself before a helper leaves the setup. Without `--session-end`: `usage: orch gc --session-end [--node NAME]`, exit 2 (real). Format:
```
h11: removed 2: container agentx-builder-h11, volume agentx-cache-npm
h11: agent objects left: 0
h12: removed 0: -
h12: agent objects left: 0
```

## journal
The project journal: [journal.md](journal.md) has the record schema, examples, jq recipes and monitoring ideas. orch records every command it runs in a project by itself (a step with timings; a difficulty on a non-zero exit, named on stderr; a resolution when the same command passes later); the agent adds the task around them.
| action | use |
|---|---|
| `start` | new task; stdin `{"summary", "request", "plan": []}`; closes tasks idle over 12 h as abandoned |
| `add` | stdin one record: `{"type": "decision", "what", "why", "options": []}`, `difficulty` (`what`, `detail`), `resolution` (`for` = the difficulty's seq, `how`, `outcome`), `step` (`what`, `outcome`, `duration_s`; always for a deploy or rollback the agent ran: `"what": "deploy: <cmd>"`), `plan` (`steps`), `note` (`text`) |
| `end` | stdin `{"outcome": "done\|partial\|failed\|blocked\|abandoned", "summary", "lessons": [], "open": []}`; writes the task's MEMORY.md section |
| `show` | the newest 50 records, one line each; `--task ID` (every record of one task), `--last N` (0 = all), `--json` (one record per line) |
| `stats` | tasks by outcome, mean/p50/p95 durations per task, verb and job, failure classes, helpers used, unresolved difficulties; `--days N`, `--json` |
| `path` | the journal and MEMORY.md paths (`--json` adds the state file) |

`start`, `add` and `end` read one JSON object on stdin, sent with a quoted heredoc, and print one line, then one JSON line. `--task ID` picks the task for `add` and `end` (default: the current task; none open -> `add` records it as `untracked`). `--project DIR` as for init: in a folder outside git, `start`, `add` and `end` with `--project DIR` create `DIR/.orch/` (the reply's `warnings` names it once), and without it every journal call exits 2 with `<folder> is not a git repository: pass --project DIR to keep orch's setup and journal in DIR/.orch`. Exit 0, or 2 with a message naming the missing or bad field. Real replies:
```
journal: task t20261030-174259-ca2d started
{"ok": true, "task": "t20261030-174259-ca2d", "seq": 1, "type": "task-start", "closed": [], "open": [], "next": "orch logs its commands itself (steps, failures, later passes); add your decisions, problems orch cannot see and their fixes (no secrets), then orch journal end:\norch journal add <<'EOF'\n{\"type\": \"decision\", \"what\": \"...\", \"why\": \"...\"}\nEOF\n..."}
```
```
bash ${CLAUDE_SKILL_DIR}/scripts/orch journal add <<'EOF'
{"type": "difficulty", "what": "both helpers unreachable", "detail": "ssh to 192.0.2.11 and 192.0.2.12:2222 timed out"}
EOF
```
```
journal: t20261030-174259-ca2d #5 difficulty recorded
{"ok": true, "task": "t20261030-174259-ca2d", "seq": 5, "type": "difficulty", "next": "when it is fixed:\norch journal add <<'EOF'\n{\"type\": \"resolution\", \"for\": 5, \"how\": \"...\"}\nEOF"}
```
`end` (real):
```
journal: task t20261030-174259-ca2d ended blocked in 9.8s: 1 step, 1 decision, 2 difficulties (1 resolved, 1 unresolved); summary in C:/work/shop/.git/orch/journal/MEMORY.md
{"ok": true, "task": "t20261030-174259-ca2d", "seq": 8, "type": "task-end", "outcome": "blocked", "duration_s": 9.811, "counts": {"steps": 1, "decisions": 1, "difficulties": 2, "resolved": 1, "unresolved": 1, "orch_seconds": 0.019}, "memory": "C:/work/shop/.git/orch/journal/MEMORY.md"}
```
`show --last 6` (real):
```
2026-10-30 17:43:00 t20261030-174259-ca2d #3 step orch orch run api-test -> usage (exit 2) 0.0s
2026-10-30 17:43:00 t20261030-174259-ca2d #4 difficulty orch orch run api-test exited 2 (usage) [usage]
2026-10-30 17:43:00 t20261030-174259-ca2d #5 difficulty agent both helpers unreachable
2026-10-30 17:43:08 t20261030-174259-ca2d #6 resolution agent for #5: the user removed both helpers and will add reachable ones (fixed)
2026-10-30 17:43:08 t20261030-174259-ca2d #7 resolution agent for #4: the run waits until the setup is approved (failed)
2026-10-30 17:43:09 t20261030-174259-ca2d #8 task-end agent blocked in 9.8s: no helper reachable; waiting for the user
```
`stats --days 7` (real, shortened):
```
journal stats (last 7 days): 26 records
tasks 1 (blocked 1; open 0); duration 1x mean 9.8s, p50 9.8s, p95 9.8s
verb deploy: 2x mean 0.1s, p50 0.0s, p95 0.2s, 2 failed
verb init: 6x mean 1.4s, p50 0.1s, p95 8.2s, 0 failed
verb run: 1x mean 0.0s, p50 0.0s, p95 0.0s, 1 failed
job api-test: 1x mean 0.0s, p50 0.0s, p95 0.0s (usage 1)
failure classes: usage 3, deploy-gate 1, other 1
helpers used: none
unresolved difficulties 1: t20261030-174259-ca2d #4 orch run api-test exited 2 (usage)
top difficulties: 1x orch deploy --check exited 7 (deploy-gate); 1x orch live exited 2 (usage); ...
```
`path` (real): `journal C:/work/shop/.git/orch/journal/journal.jsonl` and `memory C:/work/shop/.git/orch/journal/MEMORY.md`.

## Other verbs
| verb | use and output |
|---|---|
| `cordon <node> [--reason R] [--ttl S]` | pause a helper (until uncordon, or S seconds): `h12 cordoned: <reason> until uncordon` |
| `uncordon <node>` | only when the user asks; an unknown node is refused: `h12 uncordoned (was: <reason>)` |
| `hold <node> <minutes>` | suspend the fingerprint diff during a planned deploy on a helper + production host; only when the user asks; minutes must be a number > 0: `h12: fingerprint diff suspended until <time>` |
| `reconcile [--node N]` | sync the ledger with the agent objects actually present |
| `stop-all` | kill switch for all agent jobs: `killed local orch jobs: ...`; exit 5 when a kill fails |
| `selftest [--quick] [--unit-only] [--node N] [--out P]` | self checks with the built-in t-* jobs; `--quick` skips project jobs, `--unit-only` runs the offline checks only, `--node` (repeatable) limits helpers; the failure-path checks (t-fail, t-sleep, t-oom, cordon, kill mid-job) never run on a helper that is also production (skipped when every reachable helper is); needs the approved setup; evidence file in `$ORCH_HOME/selftest/` unless `--out`; one `[PASS]`/`[FAIL]`/`[SKIP] <check>: <detail>` line per check (a skip does not fail the run and is not counted as passed) |

## Troubleshooting
| exit / reason line | meaning | do |
|---|---|---|
| 2 `no orch setup here: run orch init` | no setup for this project | run `orch init` (SKILL.md Start) |
| 2 `the orch setup of <root> is not approved yet: run orch init ...` | setup changed or never approved | run `orch init`, relay its `ask` |
| 2 `... has no helper server` / `... have not been probed` | setup incomplete | run `orch init` |
| 2 `unknown answer key ...; valid keys: ...` (init --apply) | answer JSON has a wrong key | resend with a listed key |
| 2 `... seems to hold a secret ...` / `... holds a credential ...` (init --apply) | a command, URL or job env holds a secret | tell the user; the script must read it from a file or the environment; never resend it reworded |
| 2 `job <name> cannot run yet: its image <ref> is not pinned to a digest; ...` (run, ci) | the job image was never pinned | `orch init` and relay its ask; `--approve` only after the user's yes |
| 2 `orch: error: unrecognized arguments: ...` | a flag this verb does not take | check the flag table above |
| 75 `h11 docker down: ...` | Docker on that helper stopped (Docker Desktop: its user logged out) | tell the user: start Docker on h11 |
| 75 `MemAvailable X - job Y = Z vs reserve R` | memory under the reserve | stop, report; never `--node` around it |
| 75 / 6 `iowait N% > M%` | disk busy on that helper | stop, report |
| 75 `cordoned (<reason>) until ...` | helper paused | the user decides; `orch uncordon <node>` only on request |
| 75 `job class X not in opt-in list` | the helper's scope excludes that job's stage (readonly: every project job) | expected; report if no other helper fits |
| 75 `missing tags [...]` | helper lacks a capability | report |
| 75 `metric missing: <k>` | probe incomplete | report; infra on that helper |
| `warn: <node>: <reason>` then admitted | a new admission check in warn mode | note it in the report |
| guard `blocked: <what>; use: <orch cmd>` | the guard hook denied the command | run the named orch command |
| guard `blocked: setting ORCH_HOME; use: ask the user (only the user may set it)` | an orch environment variable or a user-only file | do not set it; use the flag, or ask the user |
| guard asks about a production command | not on the read-only list | the user decides |
| guard asks about an MCP tool call | its input names a helper or production host or a protected path (ORCH_HOME files, a store, ALLOW_LOCAL) | the user decides; reach helpers through orch, never through another tool |
| guard asks about `python -c`, `node -e` or other inline code | the code names a helper or production host or a protected path, and the guard cannot read it | the user decides; never move it into a script to dodge the ask |
| guard asks (production) or `blocked:` (helper) on ssh with `$(...)` or backticks | the master's shell would expand the substitution first, so the remote command is unknown | spell the remote command out without a local substitution |
| guard `blocked:` on `docker context use orch-...` | later docker calls would reach a helper without naming it | `orch nodes`, `orch status`, `orch inventory <node>` or `orch run shell-investigate` |
| guard `blocked: guard view unreadable; use: ... orch init` | the guard view is broken, or missing although orch's trust file exists (an approved setup existed) | run `orch init`; it writes the view again |
| `orch init` problem or job stop naming `agentx-*` objects of another label family (foreign namespace, v1 leftovers) | another orch left objects on that helper; orch neither uses nor removes them | relay the what -> fix: the user runs the old orch's `gc --session-end` there (removing its volumes needs the user's OK); never remove them yourself |
| session line `orch: this project has an approved helper-node-orchestrator setup ...` | the optional session-start hook (README.md) found an approved setup for this folder | use this skill for the task: `orch journal start`, `orch init`, then the work; a deploy or release question -> `orch deploy --check` |
| 2 `secret-pattern scan ... refusing to send` | a file name looks like a secret | remove or ignore the file |
| 2 `secret content scan matched ...` | content looks like a secret | same |
| 3 oom | memory cap hit after the 1.5x retry | ask the user before raising the limit |
| 5 infra | ssh or daemon failure after retry | report "Helper <node> failed: <reason>" |
| 6 r1-guard | the helper's own apps degraded; helper cordoned | stop, tell the user |
| 7 gate | a deploy gate is not met | do the gate's `fix` (relay its `ask` when it has one), run `orch deploy --check` again |
| 2 `no health URL is saved for <root>, so orch live has nothing to check: ...` (live) | the deploy answer has no health URL | relay the JSON line's `ask`, `--apply` the answer |
| 2 `unknown node <name> (known: ...)` (gc --node) | not a helper of the approved setup | check the name with `orch nodes` |
| 2 `journal add needs "type", one of plan, decision, step, difficulty, resolution, note; ...` | the JSON lacks a field or is not one object | resend it with the named field, through a quoted heredoc |
| 2 `no current journal task to end; start one with orch journal start` | no task is current (none started, or it already ended) | `orch journal start` first, or `--task ID` of a task the message lists as open |
| 2 `<folder> is not a git repository: pass --project DIR to keep orch's setup and journal in DIR/.orch` (journal) | a folder outside git | repeat the call with `--project DIR`, and pass it to `orch init` too until the first answer is saved |
| other | orch broken | stop, report |
