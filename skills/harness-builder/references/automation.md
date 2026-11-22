# Automation: run without anyone pressing the button

Automate only after the golden set passes and the owner has reviewed one real report. Before going unattended, answer five questions (https://code.claude.com/docs/en/best-practices):
1. Does it know what done looks like?
2. Can it check its own work?
3. What is it allowed to touch?
4. What stops it?
5. Who signs off on the result?

## Options

| Option | Runs where | Machine on? | Good for |
|---|---|---|---|
| Claude Desktop scheduled task (Schedule, then New task; prompt `/<slug>-run`, choose the folder and the permissions) | local app | yes, awake, app open | data or connectors that only exist on this machine |
| Cloud scheduled task (`/schedule`, formerly "routines"): cron, GitHub event or API trigger, at most hourly | Anthropic cloud, fresh clone | no | repo-based harnesses; reports pushed to a `claude/*` branch |
| GitHub Action `anthropics/claude-code-action@v1` on `schedule:` cron | CI runner | no | team repos; secrets in CI |
| OS cron or Task Scheduler + `claude -p` | any server | that server | servers you already run |
| `/loop` in an open session | session | yes | short-lived polling only (expires after 7 days) |
| Codex scheduled tasks (ChatGPT app) or `codex exec` from cron | app or server | depends | Codex-based harnesses |

Sources: https://code.claude.com/docs/en/scheduled-tasks, https://code.claude.com/docs/en/desktop-scheduled-tasks, https://code.claude.com/docs/en/routines, https://code.claude.com/docs/en/github-actions, https://learn.chatgpt.com/docs/non-interactive-mode

## Headless commands

Claude Code (check the flags against `claude --help` on the installed version):
```bash
claude -p "/<slug>-run" \
  --model <model> --allowedTools "Read,Grep,Glob,Write,Edit,Agent,Bash(python <harness>/scripts/*),mcp__<drive>__*" \
  --permission-mode dontAsk --max-turns 60 --output-format json --strict-mcp-config --mcp-config .mcp.json
```
- `--bare` skips local config and needs `ANTHROPIC_API_KEY`. Don't use it when the harness depends on project skills.
- Pin the model with `--model`, and allow the subagent tool (`Agent`) so the runner can call the verifier. Check both against `claude --help` on the installed version.
- `-p` expands `/<slug>-run` like an interactive session does.
- The project folder must be trusted, or Claude Code skips the runner's guard hook and `ingest.py` refuses to start. On a server or a fresh clone, open the folder once interactively, or set `projects["<absolute path>"].hasTrustDialogAccepted: true` in `~/.claude.json`.
- Windows Git Bash rewrites an argument that starts with `/` into a path (`/x-run` became `C:/Program Files/Git/x-run`). Run `MSYS_NO_PATHCONV=1 claude -p "/<slug>-run"`, or call it from PowerShell or cmd.

Codex:
```bash
codex exec --skip-git-repo-check --sandbox workspace-write '$<slug>-run'
```

GitHub Action sketch:
```yaml
on: { schedule: [ { cron: "0 3 * * 1-5" } ] }   # 09:00 Asia/Dhaka = 03:00 UTC
permissions: { contents: write }
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          prompt: "/<slug>-run"
          claude_args: --max-turns 60
```

## Run hygiene
- Make every run safe to repeat: `processed.md` stops double work, and a run with nothing new writes one line and stops.
- **Fresh-clone runners** (cloud tasks, GitHub Actions) forget everything not committed. Commit `memory/processed.md` and the run's `report.md` back (to a `claude/*` branch or the default branch), or keep the harness on a machine that persists. Don't gitignore what such runners need.
- `codex exec` refuses to run outside a git repo unless given `--skip-git-repo-check`.
- Cap the cost: max turns, model choice, and one item per verifier call.
- Alert on failure or on silence: no report by the expected time means something broke. Point the scheduler's failure notice at the owner.
- Log the model, run folder and calibration count in every report (the template does this) so any verdict can be explained later.
- Cron in the cloud is UTC. Convert the owner's local time and write both in the spec.
