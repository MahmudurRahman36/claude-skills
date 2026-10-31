# helper-node-orchestrator (operator guide)

For the person who installs and runs the skill on a master, the machine the agent runs on. The agent never needs this file; it reads SKILL.md and RULES.md.

orch sends builds, tests and image builds from the master to helper servers over ssh and Docker, picks a helper by capability and live load, protects the apps already running there, keeps production read-only outside the saved deploy command, gates a deploy on tests that passed for the exact commit, and keeps a journal of every task in the project. Each project keeps its own setup, collected by a short interview the first time orch runs there.

## Requirements
- **Master:** Windows with Git Bash, or Linux. Python 3.10+ (`python3`, `python` or `py` on PATH, or `ORCH_PYTHON`), git, an OpenSSH client and tar. A docker CLI is recommended: without it, image builds are off and Windows helpers cannot be used. Claude Code 2.1.295 or later for the hooks (exec form; `onFailure` for the guard); Codex works without hooks.
- **Every helper:** ssh key login from the master without any prompt (`ssh -o BatchMode=yes <spec> true` succeeds); its host key already in `~/.ssh/known_hosts` ([Host keys](#host-keys)); Docker Engine on Linux or Docker Desktop on Windows (running in a logged-on session); the ssh user runs `docker` without sudo; access to the package registries the jobs need; free disk.
- **Every production host:** ssh key login and a known host key, for read-only checks and the deploy gate's reachability check. The deploy command runs on the master and must reach production by itself.
- **Every project:** a git repo with an upstream branch (the deploy gate checks that HEAD is pushed). Outside git, orch keeps the setup in `.orch/` (created with a `.gitignore` of `*`, so it is never committed) and warns.

## Day one, in this order
For the first install on a master; each step links to its details.
1. `claude --version` prints 2.1.295 or later (the hooks need it).
2. Register the hooks with the absolute path of the interpreter ([Guard hook](#guard-hook-claude-code)): PreToolUse matcher `Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|mcp__.*` with `onFailure: block`. The [session-start hook](#session-start-hook-claude-code-recommended) is recommended.
3. Review `/mcp`: an MCP server that runs commands or writes files reaches helpers and production outside the shell. The guard reads Desktop Commander's command tools like a shell and asks about any other MCP input that names a helper, a production host or a protected path; disable the servers you do not need.
4. BatchMode ssh to each host must already work: `ssh -o BatchMode=yes <spec> true` succeeds for every helper and production host, and `~/.ssh/known_hosts` holds every one of them ([Host keys](#host-keys)).
5. Run the old orch's `gc --session-end` on every helper first ([Migration from v1](#migration-from-v1)); it removes volumes too, so that needs your OK.
6. Start with ONE dedicated Linux helper and run `orch selftest --quick --node <name>`.
7. A host that also runs production starts read-only (it is the default; lint + tests at most, and only when you choose it).
8. Keep the deploy command "later" until the first green run.
9. Add a Windows helper only after its ssh key works (step 4 for that host).
10. If `orch init --approve` shows NO permission prompt, the guard is not active: stop and fix the hook registration.

## Install once per master
The package is what `git archive` ships: `.gitattributes` keeps `dev/`, self-test logs and evidence files out.
```
cd ~/.claude/skills
[ -d helper-node-orchestrator ] && mv helper-node-orchestrator "helper-node-orchestrator.bak-$(date +%Y%m%d)"   # rollback copy
git -C <repo> archive --format=tar --prefix=helper-node-orchestrator/ HEAD | tar -x -C ~/.claude/skills
python ~/.claude/skills/helper-node-orchestrator/scripts/validate.py                          # PASS
```
- `<repo>` = the development repo. From a release zip instead: `unzip <zip> -d ~/.claude/skills`; `SKILL.md` must end up at exactly `~/.claude/skills/helper-node-orchestrator/SKILL.md`. Make the zip with `git -C <repo> archive --format=zip --prefix=helper-node-orchestrator/ -o helper-node-orchestrator.zip HEAD`.
- Codex: the same steps under `~/.codex/skills`.
- Before sharing a package, check it for site data from the development repo: `python scripts/validate.py` (the git package list plus `dev/site-denylist.txt`), or `python scripts/validate.py --dir <unpacked folder> --denylist dev/site-denylist.txt` for an unpacked copy.

## Guard hook (Claude Code)
Preflight first; every line must pass before the hook changes. `<skill dir>` = the absolute path of the installed folder with forward slashes (Git Bash: `cd ~/.claude/skills/helper-node-orchestrator && pwd -W`; Linux: `pwd`).
```
claude --version                                       # 2.1.295 or later
python -I -c "import sys; print(sys.version); print(sys.executable)"   # 3.10+, a real interpreter (not a store alias); its path goes in "command"
python -I <skill dir>/scripts/validate.py              # PASS
bash <skill dir>/scripts/orch --help                   # the verb list, exit 0
echo '{"tool_name":"Bash","tool_input":{"command":"echo ok"},"cwd":"."}' | python -I <skill dir>/hooks/guard.py; echo "exit=$?"   # exit=0, no output
```
Back up `~/.claude/settings.json` as `settings.json.bak-helper-orch`. Under `hooks.PreToolUse`, add this entry, or REPLACE the existing entry that runs `helper-node-orchestrator/hooks/guard.py` (never two):
```json
{
  "matcher": "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|mcp__.*",
  "hooks": [
    {"type": "command", "command": "python", "args": ["-I", "<skill dir>/hooks/guard.py"], "timeout": 10, "onFailure": "block"}
  ]
}
```
- **Exec form:** `command` is the program and `args` its arguments, so no shell parses the line (no quoting trouble with spaces, and it works when Git Bash is missing). Write `<skill dir>` out literally: no `~`, no variables.
- **Absolute interpreter path:** in both hook entries, put the `sys.executable` path from the preflight (forward slashes) in `command` instead of `python`, for example `C:/Program Files/Python312/python.exe` or `/usr/bin/python3`. A bare `python` depends on the PATH Claude Code starts with, and with `onFailure: block` a missing or wrong one blocks every matched call.
- **`onFailure: block`:** a hook that crashes, times out or cannot start blocks the call instead of letting it through. Without it a broken guard fails open.
- **Matcher:** the guard must see `PowerShell` as well as `Bash`, or every rule can be dodged through the other shell; `Write`, `Edit`, `MultiEdit` and `NotebookEdit` cover direct writes to orch's files; `mcp__.*` covers every MCP tool: Desktop Commander's `start_process` and `interact_with_process` are checked as shell commands and path inputs like a Write, and any other MCP input that names a helper, a production host or a protected path is asked. Review `/mcp` and disable the command-running servers you do not need.
- The guard reads only `~/.helper-orch/guard-view.json`, which orch writes from the first `orch init` in a project and fills when a project is approved. Until then it allows everything; the deny check comes after the first run (below). Once orch's `registry.json` or `trust.json` exists, a missing view counts as unreadable: docker, ssh and build commands are denied (`guard view unreadable`) until `orch init` writes it again.
- Recovery: with `onFailure: block`, a missing or broken python blocks every matched tool call. Disable the hook with `/hooks`, or remove the `PreToolUse` entry by hand. Restoring `settings.json.bak-helper-orch` also undoes other settings edits made since the backup.
- Codex has no hooks: there the rules are advisory, and orch's own refusals remain (RULES.md, Enforcement).

## Session-start hook (Claude Code, recommended)
Agents do not always load the skill on their own for a deploy or release question or before running tests: in a real run in an approved project, an agent asked "Can we deploy this project now? Only check" never loaded the skill, ran the tests on the master and guessed the answer. `hooks/session_start.py` puts one line into the agent's context when a session starts, resumes, is cleared or compacts in a folder inside an approved project:
```
orch: this project has an approved helper-node-orchestrator setup. Load that skill before any test, build or lint run and for every deploy, release or production question (checks too); journal every task (orch journal start/end).
```
- **Cost:** that line (about 50 tokens) once per session, in approved projects only; anywhere else it prints nothing. It reads only `~/.helper-orch/guard-view.json` (no ssh, network or subprocess) and takes well under 100 ms.
- **Check:** `echo '{"cwd":"."}' | python -I <skill dir>/hooks/session_start.py; echo "exit=$?"` prints the line inside an approved project, nothing elsewhere, and always `exit=0`.
- **Register** it under `hooks.SessionStart` in the same `settings.json` (after the backup above), or REPLACE the existing entry that runs `helper-node-orchestrator/hooks/session_start.py` (never two):
```json
{
  "matcher": "startup|resume|clear|compact",
  "hooks": [
    {"type": "command", "command": "python", "args": ["-I", "<skill dir>/hooks/session_start.py"], "timeout": 5}
  ]
}
```
- No `onFailure: block` here: a missing python or a broken script must never stop a session, and the script exits 0 on every error anyway. It decides nothing; the guard still enforces.
- Codex has no hooks: there the skill's description is the only trigger.

## First run in a project
1. Open the project in Claude Code or Codex and ask for something heavy, for example "run the unit tests".
2. The agent opens a journal task (`orch journal start`), runs `orch init` and asks one step per turn: how many helpers -> what you type after `ssh` for each -> one setup message (role and job scope per helper, production servers, deploy command, health URLs, optional rollback and alert commands, extra URLs to watch) -> the detected jobs -> "Save and use this setup?".
3. A problem comes with the exact fix, for example `ssh-copy-id deploy@192.0.2.12`. Run it yourself (it may ask for a password), then tell the agent to retry.
4. Your yes leads to `orch init --approve`; Claude Code shows the guard's permission prompt for it, which is the same confirmation. No prompt means the guard is not active: stop and fix the hook registration.
5. In later sessions the agent says one line, `setup ok: ...` (the `summary` of `orch init`), and asks nothing. A helper added later is the only thing asked about; a deploy command answered "later" is asked for when a deploy is checked, and a missing health URL when `orch live` runs.
6. The journal of every task is in the project store: `orch journal path` prints where `MEMORY.md` (readable summaries) and `journal.jsonl` live, `orch journal show` and `orch journal stats` read them ([references/journal.md](references/journal.md)).

By hand: run `bash ~/.claude/skills/helper-node-orchestrator/scripts/orch init` in the project folder and answer with `orch init --apply <<'EOF' ... EOF` ([references/init.md](references/init.md)).

Acceptance runbook, from the project folder (`orch` = `bash ~/.claude/skills/helper-node-orchestrator/scripts/orch`):
| step | command | expected | if not |
|---|---|---|---|
| 1 | `orch init` | `setup ok: ...`, state ready | follow its ask or fix |
| 2 | `orch selftest --unit-only` | every check passes (it needs the approved setup) | stop, roll back |
| 3 | `orch nodes` | every helper `ok docker` | fix that helper, or accept it as skipped |
| 4 | `orch selftest --quick` | all checks pass on reachable helpers; the failure-path checks (t-fail, timeout, oom, cordon, kill mid-job) never run on a helper that is also production | read the failing line, fix, rerun |
| 5 | `orch ci --stages lint,unit` | every job pass, pass count = total | 1: real failures; 2 or 75: read the reason; 6: check that helper's apps; other: roll back |
| 6 | `orch deploy --check` | a line per failing or warning gate, then `ready: ...` or `NOT ready: ...` (7 gates); exit 0, or 7 with a fix | 2: usage, read the error |
| 7 | `orch gc --session-end` | `agent objects left: 0` per helper | `orch inventory <node>` |
| 8 | `orch journal stats --days 1` | the steps of the runs above | `orch journal path`; `ORCH_JOURNAL=0` set? |
| 9 | ask the agent to run `dotnet test` (or `npm test`) locally in the project | denied with `use: orch run <job>` | allowed: hook not registered, or project not approved |
| 10 | in a new Claude Code session in the project, ask "Can we deploy now? Only check." | the agent loads the skill and answers with `orch deploy --check`; nothing is deployed | no `orch: ...` line at session start: the session-start hook is not registered |

## Host keys
- orch's own ssh always runs with `StrictHostKeyChecking=yes` and `BatchMode=yes`: it never accepts a new or changed host key, and never asks.
- So `~/.ssh/known_hosts` must already hold every helper and production host. Connect once interactively (`ssh -p 2222 deploy@192.0.2.20`), check the fingerprint against the one the server's owner gives you, and accept it there.
- The docker context transport runs docker's own ssh, which follows only `~/.ssh/config`. A helper whose ssh config loosens checking (`StrictHostKeyChecking no` or `accept-new`, `UserKnownHostsFile /dev/null`, often in a `Host *` block) is reported as a `hostkey` problem with the lines to add; fix the config, then run `orch init` again.
- `REMOTE HOST IDENTIFICATION HAS CHANGED`: ask the host's owner whether the key really changed (it can be an attack); only then remove the old key (`ssh-keygen -R <host>`) and connect once interactively.

## Operator settings
- **Policy:** thresholds, limits, `deploy_require` and IO caps have code defaults ([references/config.md](references/config.md)). To override one, edit `policy` in `<store>/config.json` yourself (the agent may not; the store is `.git/orch/` of the project). The next `orch init` reports the setup unapproved; approve it there. Values past the safety floors are refused.
- **IO caps:** `"policy": {"io": {"read_bps": "150mb", "write_bps": "80mb", "blkio_weight": 100}}` caps the disk bandwidth of job and builder containers on Linux helpers (on the Docker root device the probe found). Default: none.
- **Admission warn -> enforce:** the newer admission checks (missing load or CPU metric, Windows host reserve) print `warn: <node>: <reason>` and still admit until `policy.admission.enforce_new_checks` is true. Missing memory or disk metrics always reject; the iowait gate always enforces.
- **Guard preferences,** `~/.helper-orch/guard.json` (you only; agent writes are denied): `{"new_heavy": "deny"}` denies newly covered toolchains instead of logging them to `guard-warn.log` (default `warn`; switch once the log shows no false positives; the tools of a project's own jobs are denied inside that project either way); `{"heavy_scope": "everywhere"}` denies heavy work on the master in every folder, not only in approved projects (for dedicated masters). Guard internal errors land in `guard-errors.log`.

## Environment variables
Set by you, never by the agent; the guard denies an agent-set `HELPER_ORCH_LOCAL`, `ORCH_HOME`, `ORCH_CONFIG`, `ORCH_CI_CHILD`, `ORCH_FAULT`, `ORCH_SELFTEST`, `ORCH_TEST_MODE` or `ORCH_GC_NODE_JSON`.
| variable | use |
|---|---|
| `ORCH_HOME` | master-wide state (registry, trust, guard view, ledger, logs, results); default `~/.helper-orch`. The guard reads the same folder |
| `ORCH_PROJECT` | the project to use when not run from inside it (`--project DIR` wins) |
| `ORCH_PYTHON` | the interpreter the `scripts/orch` wrapper runs |
| `ORCH_LABEL`, `ORCH_PREFIX` | move orch's object namespace for the whole master (default `io.orch.agent`, `agentx-`); a label in the default family that differs from the default is refused |
| `ORCH_VERBOSE=1` | full progress on stderr (the agent uses `-v`) |
| `ORCH_ALERT_DRYRUN=1` | print the alert command instead of running it |
| `ORCH_JOURNAL=0` | write no journal records (reading still works) |
| `HELPER_ORCH_LOCAL` | never set it: the local override is the file `~/.helper-orch/ALLOW_LOCAL`, and only you create it |

**Development and test only.** `ORCH_CONFIG=<folder>` is legacy/test mode: orch reads v1-shaped `nodes.json`, `jobs.json` and `protected.json` from that folder and skips the project setup, its approval and its journal. Inside a project approved on this master it is refused, unless `ORCH_TEST_MODE=1`, which the test suites and the dev harnesses (`dev/runtime_*.py`) set for themselves. A production host of the guard view is refused as a node either way: `run`, `ci`, `plan`, `nodes` and `selftest` resolve every legacy node with `ssh -G` (its `~/.ssh/config`, `-J` jump hosts and its docker context's endpoint) and refuse the config when a node reaches such a host or cannot be resolved, and a node's `ssh_args` may pass `-o` only for options that leave the destination alone (`-o HostName=`, `Port=` or `ProxyJump=` are refused; use `-p` and `-J`). `ORCH_FAULT` (fault injection), `ORCH_CI_CHILD` (the command `orch ci` starts per job) and `ORCH_REPO_<NAME>` are honoured only together with `ORCH_CONFIG` (else one `warn:` line); `ORCH_GC_NODE_JSON` (`gc --node` of a node gone from that `nodes.json`) only with `ORCH_CONFIG` and `ORCH_TEST_MODE=1`; in project mode `gc --node` cleans only helpers of the approved setup and never takes a node from it. `ORCH_SELFTEST` is set by `orch selftest` for its own child runs. Never put any of these into a shell profile or the agent's environment.

## Migration from v1
v1 kept its site config in the skill's `config/` folder (and `$ORCH_HOME/config/`); v2 keeps it per project and labels its objects `io.orch.agent`, so it never uses or removes objects v1 created. It does notice them: `agentx-*` objects with another label family on a helper (v1 leftovers, a foreign namespace) make `orch init` report that helper as a problem with the fix instead of ready, and a job there stops with the same what -> fix.
1. With v1 still installed, run v1 `orch gc --session-end`; every helper must report `agent objects left: 0` (`orch inventory <node>` shows leftovers). It removes v1's cache volumes too: your decision, never the agent's.
2. Install v2 (above) and point the hook at the v2 guard.
3. In each project, run `orch init` and answer the interview. The v1 config is not imported: enter the helpers again, and confirm or add the jobs in the jobs step.
4. v1 history in `$ORCH_HOME` (ledger, results, logs) stays readable. The v1 overlay `$ORCH_HOME/config/` is not read in project mode; remove it once every project is set up. v2 creates its own docker contexts (`orch-<name>`, or `orch-<name>-<8 hex>` when two projects reach one server as different users); old contexts can be removed whenever you like.

## Server-side hardening (recommended)
orch's protections run on the master. These server-side steps limit what a misused key can do:
- **A dedicated ssh user per role.** On a helper, a user for orch only. Its docker group membership is root-equivalent on that machine, so use helpers where that is acceptable. On production, a different user and key.
- **Restricted production keys.** In `~/.ssh/authorized_keys` on production, pin the deploy key to one command and the master's address, for example:
  `from="203.0.113.10",command="/srv/app/bin/deploy-entry",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... orch-deploy`
  A second key for read-only checks can be pinned the same way to a script that runs only read-only commands.
- **Separate Docker for the helper role on a production host.** A helper + production host shares one Docker daemon with the live app. Give the orch user a rootless Docker (its own socket, for example `unix:///run/user/<uid>/docker.sock`; orch then uses the `ssh-docker` transport) or a separate daemon, so jobs can neither see nor touch production containers. Simpler still: keep the helper role off production hosts. orch treats a host that any approved project deploys to as production in every project, and runs only read-only commands there by default (lint + tests at most, when you choose it).
- **Pinned host keys.** Verify every fingerprint out of band before the first connection ([Host keys](#host-keys)).
- **Network.** Allow ssh to helpers and production only from the master; let helpers reach only the package registries the prep steps need.
- **Windows helpers.** OpenSSH Server with key login (administrators read `C:\ProgramData\ssh\administrators_authorized_keys`), and Docker Desktop running in a logged-on session.

## Troubleshooting
| symptom | cause | do |
|---|---|---|
| every Bash/PowerShell/Write/MCP call is blocked after the hook change | the interpreter in `command` is missing or broken; `onFailure: block` | `/hooks` to disable it, fix the path, run the preflight again |
| the guard allows heavy work in the project | hook not registered, or the project not approved yet | check `hooks.PreToolUse`; finish `orch init` |
| the agent answers a deploy or release question, or runs tests, without the skill | the skill was not loaded | register the [session-start hook](#session-start-hook-claude-code-recommended), start a new session |
| `hostkey` problem in `orch init` | host key unknown, changed, or the ssh config loosens checking | [Host keys](#host-keys) |
| `auth` problem | no key login | run the printed `ssh-copy-id ...` yourself |
| `docker-permission` / `docker-down` | the ssh user is not in the docker group / Docker is stopped | the printed fix on the helper, then retry |
| `windows-no-cli` | a Windows helper needs a docker CLI on this master | install the Docker CLI here |
| `docker contexts orch-<name> and orch-<name>-<hash> both point at other endpoints` | contexts left by other setups | `docker context ls`; remove the one no project uses |
| removing a helper is refused: it `could not be reached to remove orch's builder` | orch cleans a helper before it leaves the setup | fix ssh and send the answer again; for a machine gone for good, run `orch init` first, so the unreachable helper is dropped with a list of what to remove by hand |
| `--approve` exits 2: `could not pin N images to a digest` | no registry access from this machine or a helper | reach the registry, `docker login` for a private image, or set `"image": "<ref>@sha256:<digest>"` with `--apply` |
| `ORCH_CONFIG is refused inside the approved orch project ...` | a test config in the shell environment | unset `ORCH_CONFIG` |
| `warn: ORCH_FAULT is test-only and is ignored without ORCH_CONFIG` | a test variable in the shell environment | unset it |
| `gc --session-end` exits 5 | a helper was unreachable | run it again when the helper is back |
| `orch journal start` exits 2: `<folder> is not a git repository: pass --project DIR ...` | a folder outside git | let the agent pass `--project DIR` to `orch journal` and `orch init` (orch then keeps the setup and journal in `DIR/.orch/`) until the first setup answer is saved |
| `orch live` exits 2: `no health URL is saved ...` | the deploy command was saved without a health URL | answer the question it relays (`orch init --ask deploy` prints it again); `ok` saves none |
| `--approve` warns `deploy.command (...) names no file inside ...` | the deploy or rollback script is outside the project or not a file there | keep the script inside the project, or accept that the scripts-unchanged gate cannot see its edits |
| `ORCH_GC_NODE_JSON is test-only ...` or `... never adds or changes one` | a test variable in the shell environment | unset `ORCH_GC_NODE_JSON` |
| `orch init` shows a helper problem about `agentx-*` objects of another label family (a foreign namespace), or a job stops on them | v1, or a setup with another `ORCH_LABEL`, left them there; v2 never uses or removes them | run the old orch's `gc --session-end` on that helper (it removes volumes too: needs your OK), then `orch init` again |
| the guard asks about an MCP tool call | its input names a helper, a production host or a protected path (ORCH_HOME files, a store, ALLOW_LOCAL) | allow only what you would allow as a shell command; helper work goes through orch |
| the guard asks about `python -c`, `node -e` or other inline code | the code names a helper, a production host or a protected path, and the guard cannot read what it does | refuse unless you have read the code |
| the guard asks (production) or blocks (helper) an ssh command holding `$(...)` or backticks | the master's shell expands the substitution first, so the remote command is unknown until it runs | let the agent spell the remote command out, without a local substitution |
| docker, ssh and build commands blocked: `guard view unreadable` | the guard view is broken, or missing while orch's trust file exists (an approved setup existed) | run `orch init` in an approved project; it writes the view again |

## Rollback
1. In each project, run v2 `orch gc --session-end`, so no v2 objects stay behind (v1 does not see them).
2. Move v2 aside and restore the old copy: `cd ~/.claude/skills && mv helper-node-orchestrator helper-node-orchestrator.v2-off && mv helper-node-orchestrator.bak-<date> helper-node-orchestrator` (Codex: the same under `~/.codex/skills`).
3. Restore the hook entries (guard, session start) from `settings.json.bak-helper-orch` if the registration changed.
4. The v2 files are inert for v1 and can stay: the project stores (`.git/orch/`, journal included) and `$ORCH_HOME/registry.json`, `trust.json` and `guard-view.json`.

## Changelog
- **v2.1 (2026-10-31)**: `hosts.<name>.max_heavy` (1 or 2) lets a dedicated helper run two heavy jobs at once (refused on production hosts; raising it needs approval). `policy` is an `orch init --apply` answer key (safety floors enforced, every change needs approval). A health URL takes `"resolve": "<IP>"` (like `curl --resolve`) for names that do not resolve from the master. A production-only server reached only through the deploy command takes `"reach": "deploy-only"`: never probed, and the `production reachable` gate warns instead of failing. `orch selftest` skips the two-heavy-jobs capacity check (instead of failing it) when the second job is refused for the node's load or the node runs 2 heavy jobs; per-job `source_allow` exempts exact paths from the outgoing secret scan (one file per entry, `^src/tests/x[.]py$`; wildcards and alternatives are refused, and adding one, also on a new job, needs approval). A skipped selftest check prints `SKIP` and is not counted as passed. A helper that becomes production (here or in another project) runs 1 heavy job whatever its `max_heavy` or the policy says, and removing a host's `max_heavy` needs approval when the policy value is higher.

## Proven / not proven
Evidence for this version from the development repo (the counts grow with every fix round):
| area | evidence |
|---|---|
| unit tests and guard fixtures | 720+ unit tests and 740+ guard fixtures pass on Python 3.14.7 and 3.12.14 |
| package check | `validate.py` PASS on the package: zero site data |
| code review | review rounds 1 and 2: 40 and 25 confirmed defects, all fixed |
| local runtime | a Windows dev PC that is itself a helper, with local Docker: every job path (pass, fail, oom, timeout, prep) and `orch selftest` 21/21, nothing left behind |
| real ssh | a busy shared Linux helper that is also production: the interview through to ready, digest pinning, a test job that passed after an admission wait, the journal, `orch deploy --check` exit 7 with its asks, cleanup with 0 objects left; while the helper was overloaded, admission correctly refused the job |
| agent behaviour | real headless agent runs: the interview in the user's order with journal start and end; a saved setup gives one line and no question; a deploy question leads to `orch deploy --check`, and nothing was deployed |

Not proven:
- install on the real master (Windows), on a Linux master, and with a real Python 3.10;
- a Windows helper over ssh;
- a third helper, and the user's real project jobs;
- the guard's ask prompt and the session-start hook in a live interactive Claude Code session;
- Codex.

Limits that stay:
- The guard matches command text; it is not a security boundary. Restricted keys on the servers ([Server-side hardening](#server-side-hardening-recommended)) are the real protection.
- It adds roughly 100-150 ms to every matched tool call.
- Its production rules apply to every session on this master, redeploys included: a redeploy run from here against a production host of any approved project gets the same asks, and its destructive steps (`docker compose down`, `docker rm`) are denied, unless it is that project's approved deploy or rollback command.
- If the master dies mid-job, the job's container keeps running on the helper until the next orch run cleans it (once its lease, 300 s by default, has expired).
