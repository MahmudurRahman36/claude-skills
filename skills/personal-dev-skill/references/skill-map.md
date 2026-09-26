# Capability routing

Use every available skill as a candidate, not as mandatory context. Discover names/descriptions from the current host; the examples below cover the installed collection at authoring time and are not guaranteed dependencies.
Select by the requested outcome. Read the chosen SKILL.md before use. Prefer exact user selection, then project convention, then the most specific compatible skill. Resolve same-name copies by explicit path/provider; never load all variants.

| Phase / need | Candidate skills or families | Selection rule |
|---|---|---|
| Requirements, ambiguity, discovery | brainstorming; grill-me/grilling/grill-with-docs; repository-onboarding; product-management write-spec, synthesize-research, competitive-brief; to-questionnaire, research, wayfinder | One interview/spec owner; skip interviews for explicit small fixes. |
| Planning/domain/architecture | feature-development; codebase-design, domain-modeling, to-spec, to-tickets; architect, how, interrogate; product-management roadmap/sprint; figma generate-project-plan | Produce acceptance gates and dependency order, not parallel competing plans by default. |
| RCA and observability | debugging-and-rca, diagnosing-bugs, triage; devops-rca-copilot; observability | Evidence before repair; respect diagnose-only scope. |
| Backend/API/data | backend-development, backend-patterns, api-development, database-engineering, mcp-server-patterns, mcp-builder | Choose stack/domain expert; use official version-specific APIs. |
| Frontend/UI | frontend-development; frontend-design, design-taste-frontend/taste-skill, impeccable, ui-ux-pro-max, open-design; vercel-react-best-practices; Figma design/use/code-connect families | One visual direction owner plus needed implementation tool; do not combine conflicting design mandates. |
| Implementation/refactoring | implement/implement-spec, tdd; refactoring, legacy-modernization, dependency-management; ponytail, unlazy, pstack principles | Apply compatible tactics. Minimal code must still satisfy acceptance; no new orchestrator loop inside this loop. |
| Parallel work | dispatching-parallel-agents, create-subagent, swarm | Only available internal workers; disjoint scope, bounded inputs, one integration owner. |
| Tests/browser | test-engineering, playwright, browser-use, computer-use | One browser controller per session; use the host-supported path. |
| Quality/security | code-review, review/review-bugbot/review-security, gh-address-comments; secure-coding, security-best-practices, security-threat-model, vibe-security; caveman-review/evidence-review | Match risk; distinguish findings from speculative improvements. |
| Build/release | gh-fix-ci, git-and-pull-request, release-management, devops-and-ci-cd, cloud-deployment; vercel-deploy/deploy-with-vercel, redeploy-196, autopilot | Repo-specific trigger and authorized target; specialized deployment skills apply only to their own project/provider. |
| Live operations | observability, devops-rca-copilot; automate/loop/schedule, autopilot | Verify artifact and user behavior; no unrequested recurring jobs or indefinite loops. |
| Documentation/writing | documentation, no-ai-slop, unslop, technical-writing, writing-for-agents; stakeholder-update, escalation-email-drafter | Normal precise persisted prose; message sending needs applicable authority. |
| Data/reports | data analyze/explore-data/sql-queries/write-query/statistical-analysis/validate-data/build-dashboard/create-viz/data-visualization/data-context-extractor; visualize, canvas | Evidence and reproducible calculations; choose one artifact workflow. |
| Office documents | docs/documents, docx, pdf, xlsx/spreadsheets, excel-live-control, pptx/presentations/ppt-generation; theme-factory/template-creator | Format-specific parser, renderer and validation; avoid loading all provider variants. |
| Images/design/video | imagegen, canvas-design, web-artifacts-builder; Figma FigJam/slides; hyperframes; open-design | Load only when deliverable calls for it; external runtimes/assets may be required. |
| Research/provider docs | openai-docs, agent-reach, available official docs/web connectors | Current primary sources; no automatic browser-cookie export or provider setup. |
| Token/context | caveman, token-compress, explain-usage; caveman-compress/manage/learn; rtk | Concise output and scoped reads first. No automatic memory rewrites, log truncation hiding failures or CLI installation. |
| Higher-cost deliberation | llm-council | Explicit request or consequential unresolved tradeoff; account for all advisor/review tokens. |
| Skills/plugins/config | skill-creator/create-skill, skill-installer, plugin-creator/plugin-management, cowork-plugin-management; create-rule/create-hook/migrate-to-skills; update-cli-config/update-cursor-settings, statusline | Only when setup or reusable automation is in scope. |
| Host/project/task utilities | goal, onboard, origin, sdk, new-repo, share, shell, split-to-prs, rename-chat; acp-router | Use requested utility; creating goals/tasks, publishing and external harnesses are separate actions. |
| Personal/non-development | job-application-assistant; morning, import-memory, consolidate-memory, supermemory, setup-claude/setup-cowork | Use only for an explicitly relevant outcome. Do not load personal data for an unrelated coding task. |
| Any newly available skill | Discover current metadata | Match capability, scope, dependencies and evidence value; otherwise keep unloaded. |

If two skills disagree, host/project/user instructions govern; keep this task's acceptance criteria and authorization. Resolve consequential ambiguity; do not silently discard a required gate.
Missing skills/tools do not erase requirements: perform supported steps inline and state unavailable checks. Do not install an entire catalog to satisfy a task.
