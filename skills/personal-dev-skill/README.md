# PersonalDevSkill 2

A portable, low-context software delivery workflow: requirements, investigation, plan, implementation, tests, weakness review, repair, release and post-live verification.

## Why this version

The original skill optimized task sizing, RCA and evidence. Version 2 adds explicit acceptance gates, full release verification, dynamic specialist routing and host capability fallbacks. It removes forced duplicate test rounds, hardcoded model names, provider-specific command syntax and unsubstantiated token multiplier claims.

Caveman-style chat is default; it never removes uncertainties, safety-critical meaning or evidence. Documentation and code remain normal. Only relevant skills/references load. Small changes stay inline; complex work can use independent review and disjoint workers.

## Use

Ask: "Use personal-dev-skill to build this feature through staging and post-live testing." Specify the intended target and any exclusions.
A review-only or local-only request remains within that scope. This skill does not independently authorize deployment.

Codex UI metadata is included. Claude, Gemini and other hosts can load the same SKILL.md and relevant references; actual tools, install paths and model selection depend on the host. This is instruction portability, not a claim of testing every AI model.

## Install the complete directory

```sh
git clone --branch skill/personal-dev-skill https://github.com/MahmudurRahman36/claude-skills.git
```

Copy `skills/personal-dev-skill/` with all companions into the host's documented skill location (for example a Codex workspace's `.agents/skills/` or Claude Code's `.claude/skills/`). For Gemini/other hosts, confirm native skill support or load the entrypoint and selected references explicitly. The GitLab mirror is https://gitlab.com/mrkolince/claude-skills on the same branch.

## Resources and validation

- SKILL.md: short operating loop.
- references/plan-format.md: requirements and acceptance ledger.
- references/skill-map.md: capability routing across available skills.
- references/platforms.md: host/model adaptation and missing-capability handling.
- references/briefs.md: bounded delegation and independent review.
- references/delivery.md: release, production checks and rollback.
- agents/openai.yaml: optional Codex interface.
- tests/scenarios.md: behavioral evaluation cases; structural checks alone do not establish quality.

Run `python scripts/validate.py` and `python -B -m unittest discover -s tests` from this directory (validation requires PyYAML). These check package metadata, local links, size budget, conflict markers and negative fixtures. Release checks must additionally inspect Git's unresolved index entries and tracked package completeness. Model behavior requires scenario evaluation separately.
