# personal-dev-skill

A Claude Code skill that takes a coding task, or a whole list of tasks, from request to pushed and verified code without supervision, while keeping token use low.

```text
/personal-dev-skill <tasks, issue list, or path to a task file>
```

It also starts on its own when you hand Claude a bug list or feature requests to carry end to end.

## Benefits

- **Hands-off delivery.** State the requirement once; the skill carries it through investigation, build, testing, review, and push.
- **Fewer false "done" claims.** Nothing is reported as fixed or passing without a command run in that round and its output read. A helper agent's "success" counts as a claim until checked.
- **Real root causes, not patches.** Every fix starts with reproducing the problem and pointing to the `file:line` that causes it.
- **Lower token cost.** Process scales with task size, so a one-line fix doesn't pay for a multi-agent review. Helper agents are used only where they add value, their replies are capped at 15 lines, and reference files load only when needed.
- **Independent review.** The plan is judged, and the code reviewed, by agents that didn't write it, which catches the blind spots of self-review.
- **Tested for real.** Smoke tests run the actual app along the user's path; a build plus unit tests alone doesn't count.
- **Bounded effort.** A stuck issue gets 3 attempts, a fresh approach, then a clear blocker report instead of an endless loop.
- **Respects your project.** Deploy steps, read-only servers, secret handling and other project rules always override the skill.

## How it works

### 1. Size each task

| Size | Looks like | Process |
|---|---|---|
| Small | One-sentence change, 1–2 files | Investigate, fix, verify, ship. No plans; only the final review uses a helper agent. |
| Medium | Several files, one obvious approach | One short plan and one reviewer. |
| Large | Real design choice, cross-module or risky change | 2–3 competing plans, an independent judge, full verification. |

Why: a helper agent uses about 4x the tokens of plain chat, and a team of agents about 15x ([Anthropic](https://www.anthropic.com/engineering/multi-agent-research-system)). Full ceremony on small tasks is where most tokens are wasted.

### 2. The loop

1. **Load context.** Read project rules (CLAUDE.md, memory, project docs) once; size tasks; note dependencies between them.
2. **Investigate.** Find the root cause with `file:line` evidence. Direct search when the target is known; one cheap search agent for broad sweeps.
3. **Plan.** Tasks of a few minutes each, with exact paths, the change, a verification command per task, and an explicit out-of-scope line. For large tasks, alternatives must differ in mechanism (for example call-site patch vs shared-helper fix vs schema change), not wording.
4. **Check the plan.** A fresh agent that didn't write the plans picks one and lists defects. Its verdict is adopted unless the code proves it wrong.
5. **Pick skills.** Only the skills that change the result: docs lookup, debugging, `run`, `code-review`, `security-review`, and so on.
6. **Execute.** Independent tasks run in parallel, each in its own git worktree; tasks that touch the same files run one after another. Small tasks are done directly, since handing them off costs more than doing them.
7. **Smoke test.** Run the real app, container, endpoint or page along the user's path and quote the decisive output line.
8. **Verify in parallel.**
   - Tests that fail without the change, then the full relevant suite.
   - Requirements check: pass or fail per requirement.
   - Code review, plus a security review when authentication, input handling, secrets, network or permissions changed.

   Findings are triaged: correctness bugs and requirement gaps get fixed; style nits and speculative hardening are dropped to avoid over-engineering.
9. **Fix and repeat.** Each confirmed issue goes back through steps 3–8. Done means two clean verification rounds in a row.
10. **Ship.** Branch, commit with a conventional message, push, run the project's deploy trigger if it has one, and check the deployed result.

### 3. Model per job

| Job | Model |
|---|---|
| Broad search, mechanical 1–2-file edits | haiku / sonnet |
| Integration work, QC, tests | sonnet |
| Architecture, judging large-task plans, risky reviews | opus |

### 4. When it stops to ask

Only for irreversible or destructive actions, security-sensitive changes, systems the project marks off-limits, or a requirement unclear enough to change the design. Everything else proceeds.

### 5. Final report

One line per task, then open risks:

```text
size | status | change file:line | evidence (command → result) | commit/tag
```

## Files

```text
personal-dev-skill/
├── SKILL.md              # core loop; critical rules first so they survive context compaction
└── references/
    ├── briefs.md         # templates for helper agents (judge, implementer, QC, reviewer)
    ├── plan-format.md    # plan layout, at most 15 lines per plan
    └── skill-map.md      # which skill to use for which job
```

## Install

```bash
git clone https://gitlab.com/mrkolince/claude-skills.git
cp -r claude-skills/skills/personal-dev-skill ~/.claude/skills/
```

## Sources

- [Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) and [Claude Code skills docs](https://code.claude.com/docs/en/skills), Anthropic
- [Claude Code best practices](https://code.claude.com/docs/en/best-practices), Anthropic
- [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), Anthropic
- [obra/superpowers](https://github.com/obra/superpowers): verification-before-completion, systematic debugging, subagent-driven development
