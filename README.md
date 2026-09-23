# Claude Skills

A collection of [Claude Code](https://code.claude.com) Agent Skills. Each skill lives in its own folder under `skills/`.

## Skills

| Skill | What it does |
|---|---|
| [personal-dev-skill](skills/personal-dev-skill) | End-to-end dev loop: investigate, plan, independent plan check, execute, smoke test, parallel test/QC/review, fix until clean, commit and push. Token-lean. |

## Install a skill

Copy the skill folder into your personal skills directory (all projects) or a project's `.claude/skills/` (one project):

```bash
git clone https://github.com/MahmudurRahman36/claude-skills.git
cp -r claude-skills/skills/<skill-name> ~/.claude/skills/
```

Then invoke it with `/<skill-name>` in Claude Code, or let Claude pick it up from its description.

## Adding a skill

1. Branch off `main`: `git checkout -b skill/<skill-name>`.
2. Add `skills/<skill-name>/SKILL.md` (frontmatter `name` = folder name: lowercase letters, digits, hyphens; `description` in third person, what it does + when to use it). Keep `SKILL.md` under 500 lines; put detail in `references/` linked one level deep.
3. Add a row to the table above.
4. Open a pull request into `main`.
