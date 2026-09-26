# Host and model adaptation

Select by capabilities and measured task fit, not provider reputation. A model and its host are different: tools, filesystem, browser, deployment and subagents depend on the host.

## Capability preflight

Inspect exposed tools and project rules: file read/edit, shell, network/docs, browser, tests, git, secrets access, subagents, isolated worktrees, deployment, monitoring.
Never invent tool names. Use actual platform APIs. If a capability is absent, use a supported equivalent or mark the gate unavailable.
A text-only model can produce plans or patches; it cannot truthfully report running tests or deploying.

| Host | Adaptation |
|---|---|
| OpenAI Codex | Use scoped AGENTS.md and exposed tools; discover SKILL.md in host-advertised locations. Use actual subagent tools when enabled. User-visible tasks are not a substitute for internal delegation. Optional agents/openai.yaml supplies UI metadata. |
| Claude Code | Use applicable CLAUDE.md and scoped instructions. Invoke skills/agents only through exposed host capabilities. Do not require shell-interpolated SKILL.md syntax. |
| Gemini CLI / other Gemini hosts | Use applicable GEMINI.md and host-discovered instructions. Verify skill loading, shell, browser and delegation support; fall back inline when absent. |
| Other models / API / chat | Supply this core and only selected references as context. Adapter implements tool calls and authorization. Return actionable artifacts and explicitly identify unexecuted checks. |

These are adaptation rules, not a claim of runtime testing on every host/version. Preserve the host's instruction precedence.

## Specialization without provider lock-in

- Repository search/mechanical edits: direct tools first; low-cost model only if selection is exposed and allowed.
- Implementation: configured default with stack expertise and enough context.
- Architecture, security or subtle failures: stronger reasoning/independent review if justified.
- Visual work: image-capable model plus actual browser/render evidence.
- Documents/data: appropriate parser/renderer or analysis runtime, not prose-only inspection.

Keep configured provider/model unless the user or host permits switching. Never hardcode current model names, prices or reasoning enums. If choosing between available models, evaluate correctness, tool support, latency and total input/output tokens on a representative task; include retry and reviewer cost. Escalate complexity only when evidence warrants it.
Do not send private code or data to another provider without applicable authorization. Same-model reviewers provide a second pass, not independent multi-provider validation.

## Degraded operation

No specialist skill: perform the capability inline.
No subagents: separate implementation and review passes; disclose self-review.
No browser: backend/unit checks may pass but UI acceptance remains unverified.
No production access: verify release readiness; report live validation blocked.
No model-switch tool: stay on current model.
No persistent state: provide a concise handoff checkpoint.
