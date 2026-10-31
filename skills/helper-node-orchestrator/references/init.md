# orch init: the setup interview

orch owns the interview. Every `orch init` call ends with ONE JSON line that says what happens next. The agent relays that line to the user and sends the answers back; it never decides the next question itself. The interview is part of a task, so it runs inside a journal task (`orch journal start` first, [journal.md](journal.md)). Below, `orch` stands for the full command (`bash ${CLAUDE_SKILL_DIR}/scripts/orch`). Names and addresses are illustrative; the outputs are real ones, shortened only where marked.

## Calls
| call | does |
|---|---|
| `orch init` | status, after re-probing every saved host in parallel (helpers with Docker, production-only hosts ssh only) |
| `orch init --offline` | status from saved state: no ssh probe, no registry |
| `orch init --apply` | reads answer JSON on stdin, checks the whole answer, saves it at once, probes new or changed hosts, prints the new status |
| `orch init --detect` | re-runs job detection, stores the proposal, prints it and how it differs from the saved jobs |
| `orch init --approve` | pins every job image to a digest, records the approval, writes the guard view |
| `orch init --show` | the full saved setup in readable form (after a probe, unless `--offline`) |
| `orch init --ask deploy` / `--ask production` | the deploy gate's question for a deploy or production answer that is "later" or missing, as the `ask` (with a deploy command saved but no health URL: the health URL question that `orch live` asks); nothing is probed or saved; answer it with `--apply` |

- **Project:** `--project DIR`, else `ORCH_PROJECT`, else the current folder; a git repo is found from any folder inside it. A folder outside git is a project only when named with `--project` (or `ORCH_PROJECT`); orch then keeps the setup in `DIR/.orch/` (created with a `.gitignore` of `*`) and warns. `orch journal start --project DIR` makes that folder too, so the task opened before the interview gets its first answer. Pass `--project DIR` to `orch journal` and `orch init` until the first answer is saved (`DIR/.orch/config.json`); from then on, calls from inside the folder find it themselves.
- **Outside every project** plain `orch init` creates nothing and prints `no project here (<folder>): cd into your project or pass --project DIR` (state `new`, next `fix`, the same text in `problems`). `--apply`, `--detect` and `--approve` exit 2 there.
- **Nothing is written before the first answer:** plain `orch init` in a new git repo only shows the first question (and writes the guard view, so the guard is on before anything is approved). `--apply` and `--detect` create the store.
- **Exit codes:** 0, or 2 for a usage or config error (bad JSON, unknown key, invalid value, a secret, a refused change); with 2 nothing of the answer is saved, the JSON line is the real current status (as `--offline` shows it, the pending `ask` included) plus `error`, and stderr repeats it as `orch init: <error>`. So the journal records the state that really exists, and the agent corrects the call and sends it again.
- Options are never abbreviated: `--appr` is an error, not `--approve`.

## The final JSON line
Real output after `{"helper_count": 2}`, with two `~/.ssh/config` aliases on offer:
```json
{"state": "incomplete", "step": "helpers", "summary": ["helpers: none yet", "production: not answered yet", "deploy: not answered yet", "alerts: not answered yet", "jobs: not chosen yet"], "problems": [], "ask": {"kind": "choice", "header": "Helper ssh", "question": "For each of the 2 helpers, what do you type after ssh? (alias, or user@IP[:port]; comma separated)", "options": ["app-builder (192.0.2.11:22)", "spare (198.51.100.30:22)"], "multi": true, "answer_key": "helpers", "example": {"helpers": ["deploy@192.0.2.11", "deploy@192.0.2.12"]}}, "next": "ask", "applied": ["helper_count: 2 helpers"], "warnings": []}
```
| key | values |
|---|---|
| `state` | `new` nothing saved; `incomplete` answers missing; `unapproved` saved but never approved, or changed since; `drift` approved, but a host or job changed underneath; `ready` |
| `step` | `count`, `helpers`, `setup`, `jobs`, `approve`, `fix`, `none` |
| `summary` | short lines to show the user; `ready` has exactly one |
| `problems` | `[{"host", "what", "tried", "fix"}]`: what failed, the exact command orch ran, the exact fix |
| `ask` | the question to relay, or null |
| `next` | `ask` relay `ask`; `fix` relay `ask` (it lists every blocking problem as `host: what -> fix`), then wait; `approve` relay `ask` (a one-line digest of the setup, loosening changes, then "Save and use this setup?"); `ready` continue |
| `applied` | what this call saved, one line per answer key (and `approval: kept (no change loosens protection)` when an approved setup changed without loosening) |
| `warnings` | for example two specs that are the same machine (merged), a helper that is production in another project, a job folder that does not exist |
| `error` | only with exit 2: what was wrong and the fix |

Human lines before the JSON: one line per probed host (`--apply` for the hosts it probed, `--show` for all), `warning: ...` lines, pinned images (`--approve`), the job proposal (`--detect`), the readable setup (`--show`). Plain `orch init` prints only the JSON line (and warnings, or the `no project here` line). Problems are never printed as human lines: they are in `problems`, once.

## Relaying
- `kind` choice: orch uses it only with 2 to 4 options and a `header` of at most 12 characters, so it fits AskUserQuestion: `header`, `question`, `options`, and `multi` as multiSelect (true only when more than one answer is wanted). The tool adds a free-text "Other" answer. Without AskUserQuestion (Codex, `claude -p`, a plain chat), send one plain message: the question, then the options.
- `kind` text: one plain message holding `ask.question` exactly. A single candidate is named inside the question (`; known: spare (198.51.100.30:22)`) instead of being a one-option choice.
- `problems`: the fix question already lists each blocking problem as `host: what -> fix`. Show a problem it does not list (a production-only host, which never blocks) as one line of the same shape; give `tried` (the exact command orch ran) when the user asks.
- Send the answer with a quoted heredoc, so the shell leaves `$`, backticks and backslashes in the answers alone:
  ```
  bash ${CLAUDE_SKILL_DIR}/scripts/orch init --apply <<'EOF'
  {"helper_count": 2}
  EOF
  ```
- The JSON is shaped like `ask.example` (the same keys) and holds the user's answer; when options lead to different answers, `ask.examples` maps each option to its JSON. `ask.answer_key` names what is being asked (the setup step's `setup` may also be sent as a wrapper: `{"setup": {...}}`). Never invent an answer the user did not give. A plain "ok" on a question with bracketed defaults means the defaults: send `ask.example` unchanged.
- One step per turn, in order: count -> helpers -> setup -> jobs -> approve. Answers are saved at once, so an interrupted interview resumes where it stopped, and later sessions ask only what is still unanswered.
- orch journals every applied answer as a decision (by orch, why "user answer"); the agent journals its own decisions only.
- In a subagent, or with no way to reach the user: do not answer anything. Return `ask` and `problems` to the caller and stop.

## Step 1: count
Question: `How many helper servers will orch use?` Options `1`, `2`, `3`, `4+` ("Other" takes any number).
```json
{"helper_count": 2}
```
A whole number from 1 to 32 (a digit string works too). `4+` is not a number: ask the user how many.

## Step 2: helpers
Question: `For each of the 2 helpers, what do you type after ssh? (alias, or user@IP[:port]; comma separated)` (one helper: `For the helper, what do you type after ssh? (alias, or user@IP[:port])`). Options, at most 4: helpers of other approved projects on this master, then `~/.ssh/config` Host aliases that set a HostName, shown as `alias (hostname:port)`; never this machine, never a host the project already has. With fewer than 2 candidates the question is text. An option can be sent exactly as offered; orch drops the part in brackets.
```json
{"helpers": ["app-builder", "deploy@192.0.2.12"]}
```
- The list holds one spec per helper (`helper_count`, given in the same answer or saved from step 1). Once helpers are saved, the count is no longer needed.
- `helpers` adds: it never removes a saved host (that is `remove_hosts`). A saved production server typed here becomes helper + production.
- **A helper added later** (`{"helper_count": 3}` on a setup with two): the question names the saved ones and asks only for the new one, for example `Saved: h11 (app-builder), h12 (deploy@192.0.2.12). For the new helper, what do you type after ssh? (alias, or user@IP[:port]); known: spare (198.51.100.30:22)`. Answer with only the new specs (or all of them); anything else exits 2, for example `helpers lists 1 ssh target but helper_count is 4 and 2 helpers are saved already; give only the 2 new ones, or all 4, or change helper_count`.
- Each spec is resolved with `ssh -G` (local, no network). The same machine typed twice (same resolved hostname and port) is saved once, with a warning. A spec that is this machine is refused (exit 2).
- Names come from the registry: `h<last IPv4 octet>` of the resolved address, else the first DNS label; the same machine keeps its name in every project on this master, another machine on a taken name gets a suffix (`h11b`).
- **Production elsewhere:** a machine that another approved project on this master saves as production is saved as helper + production (read-only commands by default, lint + tests at most), with a warning that names that project. A project is told apart by its store, so a linked worktree of the same repository (another root, the same `.git/orch/`) is this project, never "another project", whichever worktree approved last.

`--apply` probes the new hosts in parallel and prints one line per host. Real output for two unreachable addresses:
```
h11 (deploy@192.0.2.11) problem: network (see problems)
h12 (deploy@192.0.2.12:2222) problem: network (see problems)
```
and for reachable ones:
```
h11 (app-builder) ok: linux x86_64, 8 cpu, 16 GB, docker 27.3.1, no other containers
h12 (deploy@192.0.2.12) ok: linux x86_64, 8 cpu, 16 GB, docker 27.3.1, 2 other containers
```
A helper that probes ok gets its docker context `orch-<name>` created or updated (`orch-<name>-<8 hex>` when another project's `orch-<name>` reaches that machine as another ssh user). A helper with a problem stops the interview at the `fix` step (see [Problems and fixes](#problems-and-fixes)).

## Step 3: setup (one message)
One text question, the same for Claude Code and Codex; relay it as printed. The first time it holds every line (real output):
```
Defaults are in [brackets]; reply ok to accept them all, or change any line.
Helpers (role: helper only | helper + production; jobs: everything | lint + tests, no image builds | read-only commands only | nothing):
  h11 (app-builder): 8 cpu, 16 GB, no other containers [helper only; everything]
  h12 (deploy@192.0.2.12): 8 cpu, 16 GB, 2 other containers [helper only; lint + tests, no image builds]
Production servers (what you type after ssh, comma separated), or none / later [none]
Deploy command - runs on THIS machine and must reach production itself, e.g. ./deploy.sh [later]
Health URL(s) checked after a deploy [-]
Rollback command (optional) [-]
Alert command when a helper's own apps degrade (optional; gets the message on stdin) [none]
Extra app URLs to watch on shared helpers (optional) [-]
```
with `ask.example` = what "ok" saves:
```json
{"hosts": {"h11": {"roles": ["helper"], "jobs": "all"}, "h12": {"roles": ["helper"], "jobs": "tests"}}, "production": "none", "deploy": "later", "alerts": "none"}
```
- **Only what is unanswered:** later the question holds only the open lines, and the example only their keys. A helper added later gets just its own line and the watch line:
  ```
  Defaults are in [brackets]; reply ok to accept them all, or change any line.
  Helpers (role: helper only | helper + production; jobs: everything | lint + tests, no image builds | read-only commands only | nothing):
    h13 (deploy@192.0.2.13): 8 cpu, 16 GB, no other containers [helper only; everything]
  Extra app URLs to watch on shared helpers (optional) [-]
  ```
- The step is done when every helper has a job scope and production, deploy and alerts are answered ("later" counts as an answer).
- **Production is only what the user names.** A helper is never proposed as production on a guess. A shared helper (other containers, or not known yet) defaults to `[helper only; lint + tests, no image builds]`: tests run under admission and the watchdog guards its other apps, while image builds wait for an answer of `everything`. A dedicated helper defaults to `[helper only; everything]`. A helper becomes helper + production only when the user answers so, types a saved production server as a helper, or another approved project on this master already saves it as production; that last case says so on its line (`..., production server in C:/work/billing [helper + production; read-only commands only]`).
- A full answer:
  ```json
  {"hosts": {"h11": {"roles": ["helper"], "jobs": "all"},
             "h12": {"roles": ["helper", "prod"], "jobs": "tests", "watch": ["http://127.0.0.1:8080/app/"]}},
   "production": ["deploy@192.0.2.20:2222"],
   "deploy": {"command": "./scripts/deploy.sh", "health": ["https://shop.example/health"],
              "rollback": "./scripts/rollback.sh"},
   "alerts": {"command": "./scripts/notify.sh"}}
  ```
- `hosts`: saved names only (a printed label such as `h12 (deploy@192.0.2.12)` works as the key). `roles` and `jobs` also take the words of the question (`"helper + production"`, `"lint + tests, no image builds"`, `"everything"`). A production host runs `readonly` by default and at most `tests`: `"jobs": "all"` on it is refused (`hosts.h12: h12 (deploy@192.0.2.12) is also a production server, so it runs at most lint + tests; answer jobs tests, readonly or none`), and a helper that becomes production drops from `all` to `tests` with a warning. `readonly` runs only the built-in `shell-investigate`, never a project job. `watch` replaces the list (`[]` clears it). `max_heavy` (1 or 2, `null` = the policy value) lets a dedicated helper run two heavy jobs at once; it is refused on a production host, and raising it needs `--approve`. `reach: "deploy-only"` marks a production-only server that this machine cannot ssh to (only the deploy command reaches it): orch stops probing it, and `orch deploy --check` warns instead of failing its reachability gate; setting it needs `--approve`.
- `production` lists every production-only server: a saved production-only server left out is removed. A listed helper becomes helper + production. `"none"` removes the production-only servers; `"later"` keeps everything as it is. Production "later" never blocks test offload; it only fails `orch deploy --check`.
- `deploy` merges into the saved one (`{"health": [...]}` keeps the command); a command is required, `rollback` `null` or `"-"` clears it; `"later"` = not decided. A health entry may be `{"url": "https://...", "insecure": true}` for a self-signed internal certificate, and `"resolve": "<IP>"` when the name does not resolve from this machine (orch connects to that IP and still sends the name as SNI and Host).
- **"later" comes back at deploy time.** `orch deploy --check` fails the production gate on a "later" or missing deploy or production answer and carries the question as `ask` in its JSON line; `orch init --ask deploy` (or `--ask production`) prints the same question. Its `example` is exactly what the [brackets] say, so a plain "ok" saves no value the user did not give: with deploy "later" it is `{"deploy": "later"}` (real, `--ask deploy`):
  ```json
  {"kind": "text", "header": "Deploy", "question": "A deploy needs the deploy command (defaults in [brackets]; reply with the command, or later):\nDeploy command - runs on THIS machine and must reach production itself, e.g. ./deploy.sh [later]\nHealth URL(s) checked after a deploy [-]\nRollback command (optional) [-]", "options": [], "multi": false, "answer_key": "deploy", "example": {"deploy": "later"}}
  ```
  The user's answer replaces it, for example `{"deploy": {"command": "./scripts/deploy.sh", "health": ["https://shop.example/health"], "rollback": "./scripts/rollback.sh"}}`. A new deploy command loosens protection, so `--approve` (after the user's yes) follows before the gate passes. The production ask works the same way (`[none]` or `[later]` -> `{"production": "none"}` or `"later"`; saved servers -> their list).
- **A deploy command without a health URL** passes the production gate with a warning, and `orch live` exits 2 with the health URL question as its `ask` (`orch init --ask deploy` prints the same; real):
  ```json
  {"kind": "text", "header": "Health URL", "question": "No health URL is saved, so nothing checks the app after a deploy (defaults in [brackets]; reply with the URL(s), or ok for none):\nHealth URL(s) checked after a deploy, comma separated [-]", "options": [], "multi": false, "answer_key": "deploy", "example": {"deploy": {"health": []}}}
  ```
  The answer (`{"deploy": {"health": ["https://shop.example/health"]}}`) merges into the saved deploy answer: the command stays, and added URLs need no new approval.
- `alerts`: `{"command": "..."}`, or `"none"` / `{"command": null}`. orch runs the command itself on the master, so adding or changing one needs approval.
- `policy` (any time, also outside the setup step): overrides of the code defaults ([config.md](config.md#policy-defaults)), merged into the saved ones; `null` removes a key or a section, e.g. `{"policy": {"admission": {"max_heavy": 2}}}`. Unknown keys and values past the safety floors (`reserve_frac` >= 0.2, `max_heavy` 1..2, PSI limits <= 25) are refused; every change needs `--approve`.
- Commands are one line, URLs http or https. orch saves, prints and journals them, so a command or URL that holds a secret is refused (exit 2: `deploy.command seems to hold a secret; orch saves, prints and journals this command, so read the secret from a file or an environment variable inside your script instead`; a URL: `deploy.health: https://... holds a credential (a password, a token or a key); orch saves and prints these URLs, so use one without credentials`). That covers `NAME=value`, `--name=value` and `"name": value` for a secret's name (it holds password, passphrase, secret, token, credential or an api, access or private key word, or ends in pass, pwd, pw, key or auth: `DB_PASSWORD`, `PGPASSWORD`, `DB_PASS`, `SECRET_KEY`, `STRIPE_KEY`, `X-Auth`), a secret option with its value (`--password X`, `--pw X`, `--passphrase X`, `--api-token X`, `--credentials X`), `curl`/`wget` `-u user:pass` (also `-uuser:pass`, `--proxy-user`), `mysql -pX`, `docker login -p X`, `redis-cli -a X`, `sshpass -p X`, an `Authorization:` header, bearer tokens and well-known token formats (GitHub, GitLab, Slack, AWS keys), chat webhook URLs (Slack, Discord, Telegram, Office), credential query values (`?key=`, `?sig=`, `?signature=`, `?code=`, `?apikey=`) and `user:pass@` in a URL. A `$VAR` reference is fine, and so are names that point at a secret (`TOKEN_FILE=`, `PGPASSFILE=`, `SSH_KEY_PATH=`, `--token-file`). Job `prep`, `cmd` and `env` get the same check; a job `env` name that looks like a secret's is refused with a value unless it ends in `_FILE`, `_PATH` or `_DIR`.

## Step 4: jobs
When the step opens, orch detects jobs (dotnet, node, python, Dockerfile), stores the proposal and asks:
```
Jobs found in this project:
  api-lint   lint   python 3.10  api/  2 GB  /src/.venv/bin/ruff check .
  api-test   unit   python 3.10  api/  2 GB  /src/.venv/bin/python -m pytest -q -p no:cacheprovider
  api-image  image  docker       api/  4 GB  docker build -f api/Dockerfile api
  web-lint   lint   node 22      web/  2 GB  npm run lint
  web-test   unit   node 22      web/  2 GB  npm run test
Use these jobs? (yes / change: tell me what to change)
```
(`--detect` prints the same lines under `jobs detected:`, then `compared with the saved jobs:` and the differences.) Job names are `<folder>-<stage or script>`; jobs at the project root use the project name, saved in `state/project.json` when jobs are first saved, so every worktree keeps the same names.
| user says | answer JSON |
|---|---|
| yes | `{"jobs": "detected"}` |
| give api-test 4 GB | `{"jobs": {"api-test": {"memory": "4g"}}}` |
| drop the image job | `{"jobs_remove": ["api-image"]}` |
| no jobs | `{"jobs": "none"}` |
| (unknown stack: orch asks for an image and a test command) | `{"jobs": {"tool-unit": {"stage": "unit", "image": "python:3.12-slim", "cmd": "python -m pytest -q"}}}` |

- `"detected"` takes the stored proposal: exactly what the user was shown. Jobs the user made (no `manifest`) stay; a job whose image tag did not change keeps its pin.
- Before any jobs are saved, a change (`jobs` object or `jobs_remove`) applies to the proposal, so "give api-test 4 GB" means "these jobs, with that change". After that it applies to the saved jobs. A `null` value removes that key.
- Job keys and limits: [config.md](config.md#job-keys). Checked: stage, kind, image reference, one-line `cmd`/`prep`, paths inside the project, memory up to `policy.limits.memory_max`, timeouts up to `policy.limits.timeout_max`, cache, env without secrets, valid `fail_patterns`.
- A folder outside git cannot ship its files (jobs send a commit with git archive): orch warns until the folder is a git repo or each job names a `repo`.

## Step 5: approve
The ask is one digest line, any changes that loosen protection since the last approval, any server changes and open problems, then the question. Real output:
```
Setup: helpers h11 (app-builder) all, h12 (deploy@192.0.2.12) tests, h13 (deploy@192.0.2.13) all | prod h20 (deploy@192.0.2.20:2222), h12 | deploy ./scripts/deploy.sh | alerts none | 4 jobs
Changes that loosen protection since the last approval:
  new helper h13 (deploy@192.0.2.13) (everything incl. image builds)
Save and use this setup?
```
Options `yes`, `no`. After the user's yes, run `orch init --approve`; Claude Code shows the guard's permission prompt for that call (the same confirmation, not an extra question). Approval:
1. needs every helper probed ok (a helper with a problem puts the `fix` step first, in every state), the setup and jobs steps answered (else exit 2 naming what is missing);
2. re-checks every job against the limits and every saved command and URL for secrets, including hand edits;
3. pins each image tag to a digest, trying in order: `docker buildx imagetools inspect <ref> --format '{{json .Manifest}}'` on this machine; the same command on a helper over ssh (helper-only before helper + production, Linux first); an anonymous registry v2 `HEAD /v2/<name>/manifests/<tag>` (bearer token from the `WWW-Authenticate` challenge, `Docker-Content-Digest`). An image no way can pin stops the approval (exit 2) with every reason and the fix (make the registry reachable, `docker login` for a private image, or set `"image": "<ref>@sha256:<digest>"` with `--apply`). With `policy.require_digest` false it is a warning, and the job runs on its tag;
4. records the config hash, the deploy and rollback script hashes and the co-tenant snapshot (`trust.json`), registers the project, writes the guard view and journals "setup approved". The scripts are every word of the deploy and rollback commands that names an existing file inside the project: the script itself (`./deploy.sh`, `.\scripts\deploy.ps1`), an interpreter's script after options that take a value (`bash -o pipefail ./deploy.sh`, `pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/deploy.ps1`), a script that inline code runs, an rc file. A command that names a script-like word (`.sh`, `.ps1`, `.py`, `.js`, ...) but no file inside the project gets a warning, `deploy.command (./deploy.sh) names no file inside <root>, so the approval records no script content for it and orch deploy --check cannot warn when that script changes`.

Then the setup is ready (real output):
```
pinned mcr.microsoft.com/dotnet/sdk:8.0 -> sha256:111111111111... (via this machine)
pinned node:22-bookworm -> sha256:222222222222... (via helper h11)
pinned python:3.12-slim -> sha256:333333333333... (via registry)
{"state": "ready", "step": "none", "summary": ["setup ok: helpers h11 (app-builder) all, h12 (deploy@192.0.2.12) tests | prod h20 (deploy@192.0.2.20:2222), h12 | 4 jobs | approved 2026-10-30"], "problems": [], "ask": null, "next": "ready", "applied": ["approved: 3 images pinned, 1 deploy script hashed"], "warnings": []}
```

## Later sessions
| situation | state, step, next | agent does |
|---|---|---|
| approved, hash unchanged, probe clean | `ready`, `none`, `ready` | says the one summary line, asks nothing, continues |
| a production-only host does not answer | `ready`, `none`, `ready` (summary ends `| problems: h20 (...)`) | says the line and the problem; nothing blocks until a deploy |
| a deploy command saved without a health URL | `ready`, `none`, `ready` | nothing until `orch live` exits 2 with the health URL question (the deploy gate warns); relays that `ask`, `--apply` the answer |
| answers missing (a new helper, an unanswered field) | `incomplete`, `helpers`/`setup`, `ask` | asks only that |
| config changed outside orch, or never approved | `unapproved`, `approve`, `approve` | relays the digest and "Save and use this setup?" |
| a helper down, Docker missing or stopped, ssh broken | `drift` (`incomplete`, `unapproved` before approval), `fix`, `fix` | relays the fix question, waits until the user fixed it or removes the host |
| a detected job would now be different | `drift`, `jobs`, `ask` | relays the change and "Take the new detection for these jobs? (yes / keep: keep the saved jobs and stop asking)"; sends `ask.examples` of the chosen option |
| a dedicated helper now runs other containers | `drift`, `approve`, `approve` | relays the change and "Keep this setup? (yes / change: tell me what to change)"; yes -> `--approve`, change -> `--apply` the user's change |
| a job image is not pinned (taken from a new detection) | `drift`, `approve`, `approve` | relays "Not pinned to a digest, so ... Pin it now?"; yes -> `--approve` |

- Every `orch init` re-runs detection when saved jobs came from it. A manifest file that changed while the job it yields stayed the same (same stage, kind, image tag, folders, prep, cmd, cache, env, Dockerfile) is not drift; a changed job is. Taking the new detection keeps the approved digest when the tag is unchanged.
- Runtime verbs (`run`, `ci`, `plan`, `nodes`, `inventory`, `gc`, `selftest`) refuse a new, incomplete or unapproved setup with exit 2 and `next` "run orch init".
- With the optional session-start hook (README.md), a Claude Code session in an approved project starts with one `orch: ...` line, so the agent loads this skill before tests, builds, lint or a deploy question.

## Loosening and tightening
| change made with `--apply` | result |
|---|---|
| a new helper; a dropped production role or production server; a widened job scope; a changed ssh target; removed watched URLs; an added, changed or removed alert command; an added or changed deploy or rollback command; a removed job, or a stage change, that the deploy gate requires; a job moved into a stage a narrower scope runs; changed policy | `unapproved` until `--approve` (the guard asks) |
| an added production role or server, a narrowed scope, a removed helper, added watched URLs, other job edits within the limits | saved and approved again at once (`applied` says `approval: kept (no change loosens protection)`); the approved co-tenant snapshot and script hashes stay, so drift found earlier still shows |

While a loosening change waits, the guard keeps enforcing the last approved setup, and the runtime verbs refuse the unapproved one.

## Answer keys
| key | shape | rules |
|---|---|---|
| `helper_count` | integer | 1 to 32 |
| `helpers` | `[spec]` or `"spec, spec"` | one per helper, or only the new ones when helpers are saved; adds, never removes |
| `hosts` | `{name: {"roles", "jobs", "watch", "ssh"}}` | roles from `helper`, `prod`; jobs `all`, `tests`, `readonly`, `none`; a production host is never `all`; watch = http(s) URLs; ssh may change only to the same machine |
| `production` | `[spec]`, `"none"` or `"later"` | the list is every production-only server |
| `deploy` | `{"command", "health": [url], "rollback"}` or `"later"` | merged into the saved one; one-line commands without secrets |
| `alerts` | `{"command"}` or `"none"` | the command gets the message on stdin and in `$MSG` |
| `jobs` | `"detected"`, `"none"` or `{name: {job keys}}` | job keys: [config.md](config.md#job-keys) |
| `jobs_remove` | `[name]` | an unknown name is a warning |
| `remove_hosts` | `[name]` | an approved helper is cleaned first (below); also removes its docker context; an unknown name is a warning |

- Keys are applied in this order: `remove_hosts`, `helper_count`, `helpers`, `hosts`, `production`, `deploy`, `alerts`, `jobs`, `jobs_remove`. The whole answer is checked first: one bad value and nothing is saved.
- An unknown key exits 2 and lists the valid keys: `unknown answer key 'colour'; valid keys: helper_count, helpers, hosts, production, deploy, alerts, jobs, jobs_remove, remove_hosts, policy`.
- A spec is `[user@]host[:port]`; host = an ssh alias, a DNS name, an IPv4 address or `[IPv6]`; no spaces or shell metacharacters.
- **Removing a helper cleans it first.** `remove_hosts`, or roles without `helper`, on a helper of the approved setup makes orch run `orch gc --session-end --node <name>` on it, so its builder, cache volumes and pulled base images do not stay behind (`applied`: `cleanup: h13 (deploy@192.0.2.13): orch's builder, cache volumes and pulled base images removed`). Unreachable, a job still running there or a failed cleanup -> exit 2 with the fix, nothing saved. A helper that did not answer the last probe is dropped with a warning that names the commands to list what is left.
- Every applied key is journaled as a decision (by orch, why "user answer").

## Problems and fixes
Every problem names the exact command orch ran (`tried`) and the exact fix. orch never accepts an unknown or changed host key by itself.
| class | what (from ssh or docker) | fix |
|---|---|---|
| auth | `Permission denied (publickey)` | `ssh-copy-id deploy@192.0.2.12` (`-p PORT` when the port is not 22) |
| hostkey | `Host key verification failed`, `No ... host key is known` | `connect once interactively and check the fingerprint: ssh deploy@192.0.2.12` |
| hostkey (changed) | `the host key of ... has changed` | ask the host's owner whether the key really changed (it can be an attack); only then remove the old key (`ssh-keygen -R ...`) and connect once interactively |
| hostkey (config) | `your ssh config sets StrictHostKeyChecking accept-new for ...` (or `no`, or `UserKnownHostsFile /dev/null`) | the `Host ... / StrictHostKeyChecking yes` lines to add near the top of `~/.ssh/config`, then one interactive connection |
| network | `Connection timed out`, `Connection refused`, `No route to host`, a dropped connection | timed out: `check that 192.0.2.11 is up and that port 22 is reachable from this machine (VPN, firewall)`; refused: start sshd there or put the right port in the spec; no route: check the VPN or the network |
| dns | `Could not resolve hostname` | fix the spelling, add a Host block with a HostName to `~/.ssh/config`, or use the IP address |
| docker-missing | `docker: command not found` | install Docker Engine (Linux) or Docker Desktop (Windows) on the helper |
| docker-permission | `permission denied` on the Docker socket | Linux: `on <spec>: sudo usermod -aG docker deploy, then log in again`; Windows: add the user to `docker-users` (`net localgroup docker-users <user> /add`), then sign out and in |
| docker-down | `Cannot connect to the Docker daemon` | `start Docker on <spec>: sudo systemctl start docker` (rootless: `systemctl --user start docker`; Docker Desktop: start it in the user's session) |
| windows-no-cli | Windows helper, but no docker CLI on this master | install the Docker CLI on this machine |
| other | anything else, e.g. the helper's docker context could not be set up | read `what`, run `tried` yourself to see the full error, fix it, run `orch init` again |

**v1 leftovers (foreign namespace):** a helper that holds `agentx-*` objects with another label family (v1's, or a setup with another `ORCH_LABEL`) is a problem, never ready. Its line shows what -> fix: run the other orch's `gc --session-end` on that helper; removing its volumes needs the user's OK. orch never uses or removes those objects, and the agent relays the fix and waits.

Real problem and fix question (two unreachable helpers):
```json
{"host": "h11 (deploy@192.0.2.11)", "what": "ssh: connect to host 192.0.2.11 port 22: Connection timed out", "tried": "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 -o LogLevel=ERROR -o ServerAliveInterval=15 -o ServerAliveCountMax=3 deploy@192.0.2.11 true", "fix": "check that 192.0.2.11 is up and that port 22 is reachable from this machine (VPN, firewall)"}
```
```json
{"kind": "choice", "header": "Fix", "question": "h11 (deploy@192.0.2.11): ssh: connect to host 192.0.2.11 port 22: Connection timed out -> check that 192.0.2.11 is up and that port 22 is reachable from this machine (VPN, firewall)\nh12 (deploy@192.0.2.12:2222): ssh: connect to host 192.0.2.12 port 2222: Connection timed out -> check that 192.0.2.12 is up and that port 2222 is reachable from this machine (VPN, firewall)\nRun the fixes shown, then choose retry; or remove the hosts.", "options": ["retry", "remove h11, h12"], "multi": false, "answer_key": "remove_hosts", "example": {"remove_hosts": ["h11", "h12"]}}
```
Relay that question: `retry` once the user ran the fix (it may prompt for a password, which the agent cannot answer) -> run `orch init` again; `remove` -> `--apply` the `example`. The agent never runs the fix itself on a helper.
