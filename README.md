# Claude Skills

A collection of [Claude Code](https://code.claude.com) Agent Skills. Each skill lives in its own folder under `skills/`.

## Skills

| Skill | What it does |
|---|---|
| [personal-dev-skill](skills/personal-dev-skill) | Carries coding tasks from request to pushed, verified code: root-cause investigation, size-based planning, independent plan check, parallel execution, real smoke tests, parallel test/QC/review, fix until clean, ship. Scales effort to task size to keep token use low. [How it works and benefits](skills/personal-dev-skill/README.md) |

## Install a skill

Copy the skill folder into your personal skills directory (all projects) or a project's `.claude/skills/` (one project):

```bash
git clone https://gitlab.com/mrkolince/claude-skills.git
cp -r claude-skills/skills/<skill-name> ~/.claude/skills/
```

Then invoke it with `/<skill-name>` in Claude Code, or let Claude pick it up from its description.

## Adding a skill

1. Branch off `main`: `git checkout -b skill/<skill-name>`.
2. Add `skills/<skill-name>/SKILL.md` (frontmatter `name` = folder name: lowercase letters, digits, hyphens; `description` in third person, what it does + when to use it). Keep `SKILL.md` under 500 lines; put detail in `references/` linked one level deep.
3. Add a row to the table above.
4. Open a merge request into `main`.
