# Delivery loop

One work item travels: Requirement -> Investigation -> Development -> Unit tests -> Build -> E2E -> Deploy -> Live check -> UAT -> Done.
Fail routes: unclear requirement -> Blocked -> Requirement; unit or build fail -> Development; e2e fail -> Investigation; deploy gate not met -> its fix, then the gate again; deploy, live check or severe UAT fail -> Rollback -> Investigation.

Roles: M = master (this machine), H = helper, P = production (reached from M only). Node names come from `orch nodes`, never from memory.

## Stage -> phase
| stage | phase | orch | on fail |
|---|---|---|---|
| investigate | Investigation | `orch run shell-investigate [--node N] -- <read-only cmd>` | Blocked if not reproducible |
| lint, unit | Unit tests | `orch ci --stages lint,unit` | Development |
| build | Build | `orch ci --stages build` | Development |
| image | Build | `orch ci --stages image` (helpers with scope `all` only) | Development |
| e2e | E2E | `orch ci --stages e2e` | Investigation |

`orch ci` with no `--stages` runs lint+unit, then build+image, then e2e, and stops after a red stage unless `--keep-going`.

## Helper selection (4 rules, in order)
1. **Heavy or light.** Reading, editing, planning and git stay on the master. Builds, installs, tests and suites go to a helper (heavy list: RULES.md rule 1).
2. **Allowed there.** Reachable, Docker up, not cordoned, the job's stage inside the helper's scope, required tags present. Production-only hosts never run jobs.
3. **Safe right now.** Free memory minus the job stays above the reserve (25% of the helper); memory PSI (or iowait where PSI is absent), swap, disk, inodes and load under their limits.
4. **Best of the rest.** Highest score wins: memory headroom, then free load, then image/cache affinity.

orch applies rules 2-4 (`orch plan <job>` shows them). The agent applies rule 1.

## Phases and checks
Each check: role | tool or orch call | evidence required | on fail.

### Requirement (M)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Task journaled | M | `orch journal start` (summary, request, plan; outside git with `--project DIR`) | the task id | - |
| Request read in full, attachments opened | M | - | items and attachments counted | Blocked |
| Earlier related work checked | M | project notes, git log, `orch journal show` / `MEMORY.md` | related changes and earlier tasks listed | - |
| Clear and testable | M | - | every item has a testable rule | Blocked |
| Setup ready | M | `orch init` | `state` ready (one summary line) | Start in SKILL.md |

### Blocked (M)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| One question to the user | M | - | the question names what is missing | - |
| Decision journaled | M | `orch journal add` (decision: stop and ask the user) | the record | - |
| Answer received | M | - | the answer | stays blocked |
Answered -> Requirement. Block only for real blockers (ambiguous rule, missing access, unreproducible issue), never to avoid work. A task that ends blocked closes with `orch journal end` (outcome `blocked`, the question under `open`).

### Investigation (M, H, P read-only)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Read code and recent changes | M | read, git log | suspect commits or files named | - |
| What runs on a helper | M | `orch inventory <node>` | agent items, fingerprint verdict | - |
| Reproduce | H | `orch ci --stages e2e --jobs <job>` or `orch run shell-investigate -- <cmd>` | failing case reproduced | Blocked if not reproducible |
| Read-only production check | M -> P | GET, SELECT, `docker ps`/`logs` (RULES.md rule 12) | same rows or response as reported | - |
| Root cause | M | - | cause stated with evidence, journaled (`orch journal add`: a difficulty orch cannot see, or a note on one of orch's) | Blocked |

### Development (M, H)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Code changed in a git worktree or branch | M | edit, git | files changed | - |
| Compiles | H | `orch ci --stages build` | result pass, wall time | Development |
| Independent review | M | review | findings none or fixed | Development |
Review before long runs (image, e2e) saves a rerun.

### Unit tests (H)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Guard and secret scan | M | guard hook, orch source scan | no deny, scan clean | deny `blocked: <what>; use: <orch cmd>`: use that command; exit 2: fix, never bypass |
| Helper allowed and safe | H | orch admission (`orch plan <job>`) | admit line or reason | exit 75: stop |
| Lint and tests, no network, capped | H | `orch ci --stages lint,unit` | pass count = total count | Development |
| Watchdog and clean-up | H | orch | the helper's apps unchanged, agent items removed | exit 6: stop |

### Build (H)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Compile and image build | H (scope `all` for images) | `orch ci --stages build,image` | result pass, image built | Development |

### E2E (H)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| End-to-end suites | H | `orch ci --stages e2e` | every suite green, counts read | Investigation |

### Deploy (M -> P): the deploy gate, then the saved command
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| The user asked to deploy in this conversation (or their own instructions send deploys through their deploy skill) | M | - | the request or instruction | do not deploy |
| Gate | M | `orch deploy --check` (`--ref R` checks that commit instead of HEAD) | exit 0, `ready` true | exit 7: do `next` (the first failing gate's fix), then check again |
| Deploy or production still "later" | M | relay the `ask` in the gate's JSON line (`orch init --ask deploy` or `--ask production` prints the same), `orch init --apply` the answer; a new deploy command needs `orch init --approve` after the user's yes | the gate passes on the next check | Blocked until the user answers |
| Deploy | M -> P | the deploy command named in `next`, or the user's own deploy skill when their instructions say so | the command's own success output | Rollback |
| Deploy journaled | M | `orch journal add` `{"type": "step", "what": "deploy: <cmd>", "outcome": "ok\|failed", "duration_s": N}` (orch never runs the deploy, so only this records it); a failed deploy adds a difficulty | the step in `orch journal show` | - |
| Planned restarts on a helper + production host | M | `orch hold <node> <minutes>`, only when the user asks | hold line | - |

`orch deploy --check` runs every gate, in this order, and prints a line only for each failing or warning gate, `FAIL|warn <gate>: <detail>`, then one summary line, `ready: ...` or `NOT ready: ...` (the gates counted by status). Then one JSON line: `{"ready", "gates": [{"name", "ok", "status", "detail", "fix"}], "next", "ref", "sha"}` with all seven gates and each one's fix, plus `"shas": {repo: sha}` when the jobs use more than one repository. The agent relays the printed lines; the fixes stay in the JSON line.

| gate | met when | fix when not met |
|---|---|---|
| setup approved | the config hash equals the hash approved in `trust.json`; no setup at all fails here too | `orch init`; approve only after the user says yes |
| production answered | at least one host has the production role (production-only, or helper + production) and a deploy command is saved; production `later`, `none` without such a host, or deploy `later` fail; a deploy command without a health URL is a warning (`orch live` would have nothing to check) | relay the JSON line's `ask` (answer_key `production` or `deploy`; its example is what the [brackets] say), save the answer with `orch init --apply`, approve a new deploy command after the user's yes, check again; for the warning, ask for the health URL (`orch init --ask deploy` prints the question) |
| tree clean | `git status --porcelain` lists nothing (untracked files and submodules included, whatever the git settings say) in the project and in every repo a job names | commit or stash, then check again |
| HEAD pushed | the commit is on its upstream branch, compared with the last fetch (orch never fetches). No upstream: `no upstream branch: push first (git push -u origin <branch>)`. Ahead: `N commits not pushed to origin/<branch>`. A detached commit must be on a remote branch | the printed `git push` command (diverged: `git pull --rebase, then git push`) |
| required jobs | every job whose stage is in `policy.deploy_require` (default lint, unit) has a `pass` in `state/runs.jsonl` for this exact full sha, from a run without `--wt`. No job with such a stage = warning | `orch ci --stages lint,unit`, or `orch ci --jobs <the missing ones>` when only some are missing |
| scripts unchanged | every deploy or rollback script file hashed at approval still has that content | warning only, never blocks: review the change; `orch init --approve` records the new content |
| production reachable | every production host answers `ssh ... true`, all checked in parallel; not checked while the setup is unapproved | the printed fix, e.g. `ssh-copy-id -p 2222 deploy@192.0.2.20` |

`skip` (in the JSON line and the summary count) = the gate could not be checked because an earlier gate failed, and that gate names the fix. Ready -> exit 0, `next` = `run the deploy command: <cmd>, then orch live`. Not ready -> exit 7, `next` = the first failing gate's fix. `orch deploy` without `--check` exits 2: `orch does not deploy: run orch deploy --check, then the deploy command saved in the setup`. Real, an approved setup whose tests passed for HEAD, with both production hosts unreachable (JSON shortened):
```
FAIL production reachable: h20 (deploy@192.0.2.20:2222): ssh: connect to host 192.0.2.20 port 2222: Connection timed out; h12 (deploy@192.0.2.12:2222): ssh: connect to host 192.0.2.12 port 2222: Connection timed out
NOT ready: 1 failed, 6 ok (7 gates)
{"ready": false, "gates": [{"name": "setup approved", "ok": true, "status": "ok", "detail": "approved 2026-10-30 20:02", "fix": ""}, ..., {"name": "required jobs", "ok": true, "status": "ok", "detail": "api-unit, api-lint passed for 8e9043d (runs without --wt)", "fix": ""}, ...], "next": "check that 192.0.2.20 is up and that port 2222 is reachable from this machine (VPN, firewall)", "ref": "HEAD", "sha": "<40-hex commit>"}
```

### Live check (M -> P, read-only)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Health URLs | M -> P | `orch live` | exit 0: every URL 2xx or 3xx | exit 1: Rollback; exit 2: no health URL saved: relay the `ask` in its JSON line, `orch init --apply` the answer, run `orch live` again |
| Expected change visible | M -> P | GET, SELECT, one at a time | the new version, rows or response | Rollback |
| Matches expectation | M | - | known failures listed, not ignored | Rollback |

`orch live` GETs every saved health URL in parallel: 10 s timeout, 3 tries (2 s, then 4 s apart). 2xx or 3xx = up; a redirect is an answer and is never followed. Certificates are verified; only an entry saved as `{"url": "...", "insecure": true}` skips that, and its line says `(certificate not checked)`. A user or password in a URL is sent as a basic-auth header and printed as `***`. One line per URL, then `{"ok", "checks": [{"url", "status", "ms", "tries", "error", "insecure"}], "next"}`. Exit 0 = all up, 1 = any down (`next` names the saved rollback command; run it only when the user asks), 2 = no health URL saved (the JSON line carries the health URL question as `ask`, `next` = `orch init --ask deploy`).
```
ok   https://shop.example/health 200 0.21s
FAIL https://shop.example/api/health 503 0.05s (3 tries): HTTP 503 Service Unavailable
```

### UAT (users)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Users test the new version | P | real flows | user report | Rollback (severe bug) |
| Sign-off | M | - | the user's sign-off | Rollback (severe bug) |

### Rollback (M -> P)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Roll back | M -> P | the saved rollback command, only when the user asks; none saved -> the user | the command's success output | stop, tell the user |
| Rollback journaled | M | `orch journal add` step `{"what": "rollback: <cmd>", "outcome", "duration_s"}`, and a resolution `for` the `live-down` difficulty | the records in `orch journal show` | - |
| Last good version healthy | M -> P | `orch live`, read-only checks | healthy | stop, tell the user |
Then -> Investigation.

### Done (M)
| check | role | tool / call | evidence | on fail |
|---|---|---|---|---|
| Session clean-up (session over) | M | `orch gc --session-end`, before the journal ends, so its step and any helper left dirty land in the task | 0 agent items left | report; the helper goes under `open` and the outcome is `partial` |
| Work item closed | M | `orch journal end` (outcome, summary, lessons, open items) | the MEMORY.md section | - |

## Evidence rules
- Missing evidence = FAILED. No evidence, no pass.
- Fresh evidence per revision: a new commit or worktree change needs new runs; old results do not carry over. The deploy gate enforces this for `deploy_require` stages.
- Assert content, not just status: read pass/total counts, named suites, row values. Exit 0 with 0 tests is a fail.
- Three failures of one stage -> strategy review back in Investigation; do not try a fourth time the same way.
