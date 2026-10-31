# Agent Skills

Portable skills for Claude Code, OpenAI Codex, Gemini and other capable hosts. Each complete package lives under `skills/`.

| Skill | Purpose |
|---|---|
| [PersonalDevSkill](skills/personal-dev-skill/README.md) | Requirements through post-live testing, with evidence gates, task-sized effort, optional specialists and concise Caveman-style communication. |
| [Fast-Path](skills/fast-path/README.md) | Faster, lower-token task execution: triage, dependency DAG, parallel batches, shortcuts only behind equivalence checks, verification gates, Caveman-ultra replies. |
| [Helper-Node Orchestrator](skills/helper-node-orchestrator/README.md) | Runs heavy builds and tests on helper Docker servers picked by live load, asks for helpers, production and deploy details on first use and saves them in the project, gates deploy questions, keeps production read-only and journals every task. Interactive [decision flow](docs/helper-node-orchestrator/decision-flow.html). |

Use the `skill/personal-dev-skill` branch for this version. Follow the package's installation instructions and retain companion files. Host tools and permissions determine which steps can actually execute.

Publish focused changes on a skill branch. Validate the package and realistic behavior before updating GitHub/GitLab mirrors.
