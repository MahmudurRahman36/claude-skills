# Discovery: understand the project before designing anything

Goal: collect every **fact** yourself and bring only **decisions** to the user. Claude Code's own test applies: "Could it find this by reading the code? Would it actually go and look?" What the repo can't tell you (a decision made in a meeting, a process that lives in one person's head) is what you ask about. (https://code.claude.com/docs/en/best-practices)

## 1. Reading order (token-budgeted)

Read in this order. Stop a tier early when it already answers the sufficiency gate (section 3). Hand broad sweeps to a read-only subagent and keep only its summary.

1. **Existing harness and instructions.** `CLAUDE.md`, `AGENTS.md`, `CLAUDE.local.md`, `.claude/` (skills, agents, commands, rules, `settings.json` hooks and deny rules), `.agents/skills`, `.cursor/rules`, `.github/copilot-instructions.md`, `.mcp.json`. Also project memory: `memory/`, `MEMORY.md`, `.claude/agent-memory/`, and the host's auto-memory for this project. For Claude Code that is `~/.claude/projects/<project path with every / \ and : replaced by ->/memory/MEMORY.md`; it may not exist. This is the harness that already exists. Extend it; never duplicate it or contradict it.
2. **Purpose.** `README*`, `docs/` index, `SPEC.md`, `PRD*`, `BRIEF*`, product or job descriptions, any `context/` folder.
3. **What runs.** Manifests (`package.json` scripts, `pyproject.toml`, `Makefile`, `justfile`, `docker-compose*`), CI (`.github/workflows`, `.gitlab-ci.yml`, `Jenkinsfile`). CI shows the real build, test and deploy commands.
4. **Decisions.** ADRs (`docs/adr`, `doc/architecture/decisions`), `CONTRIBUTING`, `CODEOWNERS`, runbooks.
5. **Behaviour signals.** `git log --since=90.days --name-only` for churn. Repeated commit subjects ("weekly report", "triage", "bump") and folders of dated outputs (`reports/2026-*`) point at repeated work, which is what a harness automates.
6. **Samples.** Two or three real input items and any past outputs. You need these for the rubric anchors, the example file and the golden set.

Non-code projects (a folder of documents, a team workspace) usually stop after tiers 1, 2 and 6.

## 2. Record what you learn

Fill the first section of the harness spec (`references/harness-spec.md`) as you go. Tag each item:
- **K (known):** cite the file or command that proves it.
- **I (inferred):** your best guess plus the evidence. The user must confirm it.
- **U (unknown):** nothing found.

Never present an inference as a fact.

## 3. Sufficiency gate

You may design only when every row below is K, or is I/U and then confirmed by the user. A minor unknown that doesn't change the design (e.g. how long run copies are kept) may be recorded as a non-blocking follow-up instead.

| # | Must know | Fact or decision |
|---|---|---|
| 1 | The job: which repeated task, its goal, and what "done" means | decision (you can infer it from repeated outputs) |
| 2 | Decision owner: who acts on the output, who approves | decision |
| 3 | Trigger and rhythm: manual, schedule or event; volume | fact if visible in logs, else decision |
| 4 | Inputs: where they live, format, access path or connector | fact |
| 5 | Output: consumer, format, destination | decision |
| 6 | Quality bar: the criteria a good result meets, and what evidence proves each | decision. Samples help you draft it |
| 7 | Oracle: what can be checked by code, and what needs judgment | fact plus decision |
| 8 | Failure cost: what is irreversible, and the blast radius | decision |
| 9 | Guardrails: what the AI must never do (delete, send, contact, spend) | decision |
| 10 | Sensitive data: personal data, secrets, legal or bias exposure; retention | fact plus decision |
| 11 | State: what must be remembered between runs | decision |
| 12 | Golden examples: are there 5+ past items with known right answers, and who labels them? | fact plus decision |

If rows 1, 2, 6, 8 or 9 are not settled, run the interview (`references/interview.md`). Never guess these five.

## 4. Several candidate workflows?

Score each candidate from 1 to 5 on four things. Value = frequency × repeatability × verifiability × (6 − risk).
- **Frequency:** how often it runs.
- **Repeatability:** same steps every time.
- **Verifiability:** an objective check exists.
- **Risk:** cost of a wrong result.

Then:
- Build one harness per workflow, top candidate first. Each gets its own slug.
- **Verifiability below 3:** the harness drafts and a human checks. Never let it act.
- **Irreversible actions:** the harness proposes and a human approves.
- **Don't build an LLM harness when:**
  - the task is deterministic (write a script);
  - it is rare (xkcd 1205: five-year saving = 5 × runs per year × time saved per run);
  - the process isn't stable yet;
  - nobody will own supervision.

Say so plainly and suggest the simpler option.

## 5. Pick the process pattern from the task's shape

The pattern list is from Anthropic's "Building effective agents" (https://www.anthropic.com/engineering/building-effective-agents).

| Task shape | Pattern | What it means for the run skill |
|---|---|---|
| Fixed steps | Prompt chaining | Numbered steps (default) |
| Different categories need different handling | Routing | Step 2 classifies, then one branch per category |
| Independent items | Parallelization | Score items in parallel subagents |
| Clear criteria, quality matters | Evaluator-optimizer | Verifier loop (default) |
| Unknown number of steps | Agent | Avoid for business workflows; add max turns and a budget |

Start with the simplest pattern that works.
