# orch setup files

Everything here is written by orch. Read the setup with `orch init --show`; change it with `orch init --apply` (see [init.md](init.md)). Hand edits leave the setup unapproved, and the guard denies agent writes to these files. Names, addresses and values below are illustrative.

## Where things live
| path | holds | written by |
|---|---|---|
| `<store>/config.json` | the user's decisions (schema 1); hashed | `init --apply`, `init --approve` (image pins) |
| `<store>/state/hosts.json` | probe observations per saved host | `init`, `init --show` (every host), `init --apply` (new or changed hosts) |
| `<store>/state/jobs-detected.json` | the last job proposal | `init --detect`; `init` when the jobs step opens or saved jobs came from detection |
| `<store>/state/project.json` | `{"name": ...}`: the project name that root jobs are named after (`<name>-unit`), kept for every worktree | `init --apply`, when jobs are first saved |
| `<store>/state/runs.jsonl` | one line per finished run; the deploy gate reads it | `run`, `ci` |
| `<store>/state/journal-current.json` | open journal tasks, the current one, sequence counters | `orch journal`, every orch verb in the project |
| `<store>/journal/journal.jsonl`, `MEMORY.md` | the project journal: every task's records, and one readable section per finished task ([journal.md](journal.md)) | every orch verb in the project (`init --apply` adds one decision per answer key, `--approve` one more); `orch journal start|add|end` |
| `$ORCH_HOME/registry.json` | node name -> identity; the list of approved stores | `init --apply` (new names), every probe (docker IDs), `init --approve` |
| `$ORCH_HOME/trust.json` | store -> approval record | `init --approve`; `init --apply` when an approved setup changes without loosening |
| `$ORCH_HOME/guard-view.json` | every approved project, for the guard hook | every `init` call in a project (also outside one when the view exists or something is approved) |
| `$ORCH_HOME/` (other) | `lock`, `state.json` (cordons, holds), `live/`, `results/`, `node-logs/`, `ledger.jsonl`, `audit.jsonl`, `alerts.log`, `selftest/`, guard logs | runtime verbs |
| `*.lock` next to config, registry and trust files | cross-process locks for read-modify-write | orch |
| `$ORCH_HOME/guard.json` | guard preferences (`new_heavy`, `heavy_scope`) | the user only |
| `$ORCH_HOME/ALLOW_LOCAL` | the local override | the user only |

- **Project root** = `git rev-parse --show-toplevel` from the current folder, or `--project DIR` / `ORCH_PROJECT` (`--project` wins).
- **Store** = `$(git rev-parse --git-common-dir)/orch/`: inside `.git`, so it is never committed, survives `git clean -fdx`, is shared by every worktree of the repo and is not copied by a clone. A folder outside git gets a store only when named with `--project` or `ORCH_PROJECT`: `<folder>/.orch/`, with a warning; orch writes `<folder>/.orch/.gitignore` holding `*` when it creates the folder, so the setup and journal stay out of version control, and jobs there need a `repo` (git archive ships a commit).
- **Multi-repo workspaces:** a job may set `repo` to a relative path of another git repo under the root (default `.`).
- `ORCH_HOME` defaults to `~/.helper-orch`, is shared by every project on the master, and only the user sets it.
- Every write is atomic (temporary file, then rename); files are mode 0600 where the OS supports it.

## config.json
```json
{"schema": 1,
 "hosts": {
   "h11": {"ssh": "app-builder", "roles": ["helper"], "jobs": "all"},
   "h12": {"ssh": "deploy@192.0.2.12", "roles": ["helper", "prod"], "jobs": "tests",
           "watch": ["http://127.0.0.1:8080/app/"]},
   "h20": {"ssh": "deploy@192.0.2.20:2222", "roles": ["prod"]}},
 "production": {"answer": "set"},
 "deploy": {"command": "./scripts/deploy.sh", "rollback": "./scripts/rollback.sh",
            "health": ["https://shop.example/health", {"url": "https://app.internal.example/health", "insecure": true}]},
 "alerts": {"command": "./scripts/notify.sh"},
 "jobs": {
   "api-unit": {"stage": "unit", "kind": "container",
                "image": "mcr.microsoft.com/dotnet/sdk:8.0@sha256:<digest>",
                "dir": "api", "prep": "dotnet restore", "cmd": "dotnet test --no-restore",
                "memory": "3g", "timeout": 1200, "cache": {"name": "nuget", "path": "/root/.nuget/packages"},
                "detected": "dotnet 8 (api/Api.sln)", "files": ["api/Api.sln", "api/Api.csproj"],
                "manifest": "sha256:<digest>"},
   "web-unit": {"stage": "unit", "kind": "container", "image": "node:22-bookworm@sha256:<digest>",
                "dir": "web", "prep": "npm ci", "cmd": "npm test", "memory": "2g", "timeout": 900,
                "cache": {"name": "npm", "path": "/root/.npm"}, "detected": "node 22 (web/package.json)",
                "files": ["web/package-lock.json", "web/package.json"], "manifest": "sha256:<digest>"},
   "api-image": {"stage": "image", "kind": "build", "dockerfile": "api/Dockerfile", "context": "api",
                 "memory": "4g", "timeout": 1800, "detected": "Dockerfile (api/Dockerfile)",
                 "files": ["api/Dockerfile"], "manifest": "sha256:<digest>"}},
 "policy": {"deploy_require": ["lint", "unit"]}}
```

| key | meaning |
|---|---|
| `schema` | always 1 |
| `hosts.<name>` | the registry name of the machine (`h<last IPv4 octet>`, else the first DNS label; `h11b` on a clash) |
| `hosts.<name>.ssh` | exactly what the user typed after `ssh`. The resolved address, user and port are observations in `state/hosts.json`, never here |
| `hosts.<name>.roles` | `helper`, `prod` or both |
| `hosts.<name>.jobs` | helper scope: `all`, `tests`, `readonly` (only the built-in `shell-investigate`), `none`. Helper + production: `readonly` by default, at most `tests`. Missing = not answered yet |
| `hosts.<name>.watch` | extra URLs on a shared helper whose latency the watchdog guards during jobs |
| `production.answer` | `set` (hosts with role `prod` listed), `none` or `later` |
| `deploy` | `command` (runs on the master and must reach production itself), `rollback` (or null), `health` (URLs for `orch live`; an entry `{"url", "insecure": true}` skips the certificate check); or `{"answer": "later"}` |
| `alerts.command` | run detached on the master with the message on stdin and in `$MSG` when a helper's own apps degrade; null = no alert |
| `jobs` | one entry per job (keys below); `{}` = the user chose no project jobs; missing = not answered yet |
| `policy` | overrides of the code defaults (below); validated, and values past the safety floors are refused |
| `pending` | interview answers not complete yet (`helper_count` until the helpers are named); not hashed |

Commands, URLs and job `prep`, `cmd` and `env` never hold secrets: orch saves, prints and journals them, so `--apply` refuses a secret in them (the forms: [init.md](init.md#step-3-setup-one-message): a secret's `NAME=value`, a secret option with its value, `-u user:pass`, tokens, webhook and credential URLs, `user:pass@`), and `--approve` re-checks hand edits. A refused answer exits 2 and saves nothing of it.

### Job keys
| key | meaning |
|---|---|
| `stage` | `lint`, `unit`, `build` (compile only), `e2e`, `investigate` for container jobs; `image` for build jobs |
| `kind` | `container` (commands in a capped container) or `build` (docker image build) |
| `image` | container image reference (`python:3.12-slim`, `registry.example:5000/team/app:1`); pinned to a digest at approval, and an unpinned image does not run while `policy.require_digest` is on |
| `dir` | folder the commands run in, relative to the repo (the source lands in `/src`; every command starts in `/src/<dir>`, with `/out` ready for results) |
| `repo` | another git repo under the project root (default `.`) |
| `prep` | restore or install step, one line; the only step with network, registries only |
| `cmd` | the lint, test or build command, one line; runs with no network |
| `cache` | `{"name", "path"}`: an agent cache volume (`<prefix>cache-<name>`) mounted at the absolute `path` |
| `env` | extra environment variables (string values); never secrets: a secret-looking name with a value (`DB_PASS`, `STRIPE_KEY`, `API_TOKEN`) is refused (`*_FILE`, `*_PATH`, `*_DIR` names are fine), and so is any `NAME=value` the command check refuses |
| `memory`, `cpus`, `pids`, `timeout`, `prep_timeout` | caps: memory from 16m to `policy.limits.memory_max`, timeouts in whole seconds up to `policy.limits.timeout_max`, `cpus` above 0 and at most 64 (default 2), `pids` from 16 (default 2048), `prep_timeout` default 900 s |
| `fail_patterns` | regular expressions that mark failure lines in the log |
| `result_paths` | absolute paths copied back after a container run (default `/out`) |
| `dockerfile`, `context` | build jobs: the Dockerfile and its build context folder, relative to the repo |
| `detected`, `manifest`, `files` | what detection saw, the sha256 over the files it read, and those files; a job without `manifest` is the user's own and is never compared with detection |

Paths (`dir`, `repo`, `context`, `dockerfile`) must stay inside the project: no absolute paths, no `..`. In an `--apply` job object a `null` value removes that key. Where a job runs: stages lint, unit, build, e2e and investigate on helpers with scope `tests` or `all`, image only on `all`; `readonly` helpers run no project job.

### Policy defaults
| key | default |
|---|---|
| `limits` | `memory_max` 6g, `timeout_max` 3600 s |
| `require_digest` | true |
| `deploy_require` | `["lint", "unit"]`: stages that need a pass for the exact commit before `deploy --check` is ready; removing such a job, or moving it out of these stages, loosens protection |
| `admission` | memory reserve 25% of the helper, `max_heavy` 1 heavy job per helper, memory PSI, swap, disk, inode and load limits |
| `watchdog` | samples every 10 s; trips on memory PSI, iowait (`iowait_max` 30) or endpoint latency over 2x baseline + 0.2 s, 3 samples in a row |
| `lease` | 300 s, heartbeat 60 s |
| `subnet_pool`, `subnet_prefix` | `10.252.20.0/24`, /28 per job network |
| `builder` | agent builder caps; build cache up to 15 GB, cache volumes up to 6000 MB each |
| `io` | null = no disk bandwidth caps. `{"read_bps": "150mb", "write_bps": "80mb", "blkio_weight": 100}` caps job and builder containers on Linux helpers (`--device-read-bps` / `--device-write-bps` on the Docker root device the probe found, `--blkio-weight` 10 to 1000); rates are a number with kb, mb or gb, or bytes per second |
| `never_touch` | extra name prefixes orch must never touch (on top of the label + prefix rule) |
| `windows_host_services` | `sshd`, `com.docker.service`, `WinRM`, `TermService`, `LanmanServer` |
| `source_exclude` | secret-looking paths never shipped to a helper (`.env`, `*.pfx`, `*.pem`, ...) |

## state/hosts.json
Rewritten from the probes; never hashed. Only saved hosts keep a record.
```json
{"h12": {"name": "h12", "ok": true, "probed": "2026-10-30T09:00:00", "spec": "deploy@192.0.2.12",
         "resolved": {"hostname": "192.0.2.12", "user": "deploy", "port": 22, "alias": false},
         "facts": {"os": "linux", "arch": "x86_64", "cpus": 16, "mem_bytes": 66500000000, "disk_free_bytes": 400000000000,
                   "docker": "26.1.4", "docker_id": "<daemon id>", "rootless": false, "buildx": true, "psi": true,
                   "cgroup": 2, "socket": "default", "cotenants": [{"name": "app1-web", "image": "app1:1", "ports": [8080]}],
                   "endpoints": [{"name": "app1-web :8080", "url": "http://127.0.0.1:8080/", "insecure": false}]},
         "transport": "context", "context": "orch-h12", "docker_host": null, "problem": null}}
```
- `cotenants`: containers orch does not own that publish ports. `endpoints`: their published TCP ports that answer 2xx or 3xx on 127.0.0.1 (at most 8), measured on the helper; together with `watch` URLs they are the watchdog's baseline.
- `transport`: `context` = a docker context that `orch init` creates or updates (it respects `DOCKER_CONFIG`); `ssh-docker` = `ssh <spec> docker -H <docker_host>` for a Linux helper whose default socket fails or when this master has no docker CLI (image builds then off). Production-only hosts are probed with `ssh <spec> true` only: `facts` is `{}`, no transport.
- **Context naming:** `orch-<name>`. When another project's `orch-<name>` already points at a different endpoint (the same machine reached as another ssh user), this project gets `orch-<name>-<first 8 hex of the endpoint's sha256>`; `context` records which one. orch updates or removes only its own context, and never one another project's state still uses. Image builds follow the same split: orch's buildx instance on this master is per Docker endpoint (`<prefix>builder-<name>`, or one named after the `orch-<name>-<8 hex>` context), and an instance that points at any other endpoint is removed and created again before a build, so one project's builds never go through another project's ssh user.
- `ok` is false when the host is unreachable, its Docker does not work, its ssh config loosens host-key checking (helpers), or orch cannot drive that Docker from this master (no docker CLI for a Windows helper, a context that could not be set up). Then `problem` = `{"what", "tried", "fix", "class"}` (classes: [init.md](init.md#problems-and-fixes)).

## state/runs.jsonl
One JSON line per finished run in project mode:
```json
{"job": "api-unit", "sha": "<40-hex commit>", "wt": false, "node": "h11", "result": "pass", "exit": 0, "wall_s": 212.4, "jobid": "1030091512a1b2", "ts": "2026-10-30T09:15:12"}
```
`orch deploy --check` looks for a `pass` with `wt` false and the exact HEAD sha for every job whose stage is in `policy.deploy_require`. The newest 10000 lines are kept.

## Master-wide files
```json
{"nodes": {"h11": {"hostname": "192.0.2.11", "port": 22, "docker_id": "<daemon id>"}},
 "projects": ["C:/work/shop/.git/orch"]}
```
`registry.json`: the same identity (resolved hostname + port, and the Docker ID once known) keeps the same name in every project; a clash gets a suffix (`h11b`). A probe that finds a different Docker ID or address under a known name prints a warning.
```json
{"C:/work/shop/.git/orch": {"hash": "<sha256>", "approved_at": "2026-10-30T09:20:00",
                            "scripts": {"scripts/deploy.sh": "<sha256>", "scripts/rollback.sh": "<sha256>"},
                            "cotenants": {"h11": [], "h12": ["app1-db", "app1-web"]},
                            "root": "C:/work/shop", "git": true, "config": {"schema": 1, "hosts": {"...": "..."}}}}
```
`trust.json`: one approval per store: the config hash, when, the deploy and rollback script hashes, each host's co-tenant names at approval (a dedicated helper that gains containers is drift), and the approved config itself, which keeps protecting while a later change waits for approval.
- **master_prod (production is master-wide):** from the registry and `trust.json`, orch collects every host (resolved hostname + port) that an approved config saves with the role `prod`. In every other project such a host is saved as helper + production when added as a helper (`readonly` by default), its setup line names the project that deploys there, and compiling the setup clamps its scope to at most `tests`, so no image build and no builder ever run on a production server of any approved project. "Other project" is decided by store: the project's own store is left out, so a linked worktree of the same repository (another root, the same store) never counts as another project.
```json
{"schema": 1, "generated": "2026-10-30T09:20:00",
 "namespace": {"label": "io.orch.agent", "prefix": "agentx-"},
 "hosts": [{"name": "h12", "roles": ["helper", "prod"], "spec": "deploy@192.0.2.12", "hostname": "192.0.2.12",
            "port": 22, "user": "deploy", "ips": ["192.0.2.12"], "aliases": ["app-host"], "contexts": ["orch-h12"]}],
 "projects": [{"root": "C:/work/shop", "store": "C:/work/shop/.git/orch", "deploy": ["./scripts/deploy.sh"],
               "rollback": ["./scripts/rollback.sh"], "jobs": {"dotnet test": "api-unit", "npm test": "web-unit"}}]}
```
`guard-view.json`: the union of every approved project (each worktree root counts); a host's roles are the union over the projects, so production wins there too. `aliases` are the non-wildcard `Host` names in `~/.ssh/config` that resolve to the same hostname and port. `jobs` maps a heavy command to the job the guard suggests instead. With no view the guard is off, so `orch init` writes it (even empty) from the first call in a project, and rewrites an unreadable one. Once `registry.json` or `trust.json` exists, the guard treats a missing view as unreadable and fails closed: docker, ssh and build commands are denied until `orch init` writes it again.

## What is hashed
- **Config hash:** sha256 of the canonical JSON of `config.json` without `pending`. `--approve` stores it in `trust.json`; every `orch init` compares. A different hash = unapproved. Changes made with `--apply` that do not loosen protection are approved again at once; loosening ones wait for `--approve` ([init.md](init.md#loosening-and-tightening)).
- **Deploy and rollback scripts:** every word of the deploy and rollback commands that names an existing file inside the project (resolved; network paths never count): the script itself (`./deploy.sh`, also with Windows backslashes, `.\scripts\deploy.ps1`), an interpreter's script after options that take a value (`bash -o pipefail ./deploy.sh`, `pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/deploy.ps1`), the script quoted inline code runs (`pwsh -Command "& ./deploy.ps1"`), an rc file. Approval stores `{project-relative path: sha256 hex}`; `orch deploy --check` warns when the content changed since. A command that names a script-like word (`.sh`, `.ps1`, `.py`, `.js`, `.cmd`, ...) but no file inside the project gets an approval warning, because its edits cannot be seen. An automatic re-approval keeps the approved hashes.
- **Job manifests:** the sha256 over the files detection read (`manifest`). When it changes, orch compares the job detection would make now with the saved one (stage, kind, image without its digest, dir, repo, prep, cmd, cache, env, Dockerfile, context); only a real difference is drift, shown with one confirmation.
- **Not hashed:** `state/` files, the journal and the guard view; they are observations or derived.

## Namespace
Agent objects carry the label `io.orch.agent=1` and the name prefix `agentx-`. The user can move them for the whole master with `ORCH_LABEL` and `ORCH_PREFIX`; the other ownership labels follow the label's family (`io.orch.owner`, `.session`, `.job`, `.lease`, `.role`, `.heavy`), and a label in the default family that differs from the default is refused. The test suites use `io.orchtest.agent` and `agentt-`, so they never see real objects. Objects with the prefix but another label family (v1's, for example) are a *foreign namespace*: orch never uses or removes them. Discovery reports that helper as a problem, so `orch init` does not call it ready, and a job's preflight stops there with the same what -> fix: run the other orch's `gc --session-end` on that helper (removing its volumes needs the user's OK).
