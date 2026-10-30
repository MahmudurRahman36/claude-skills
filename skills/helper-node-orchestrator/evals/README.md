# Running the behaviour evals

No eval touches a real host: the runner uses `evals/stub/orch`, which prints canned output per scenario in the real CLI's formats (journal verb included), logs every call and the JSON every `init --apply` and `journal start|add|end` received, and refuses stdin that is not one JSON object when a Python 3 is on PATH.

1. Export the package to a sandbox: `git -C <repo> archive --format=tar --prefix=helper-node-orchestrator/ HEAD | tar -x -C <sandbox>`.
2. Swap the real CLI for the stub: make `<sandbox>/helper-node-orchestrator/scripts/orch` the one line `exec bash "$(dirname "$0")/../evals/stub/orch" "$@"`, and remove `scripts/orch.py` so nothing real can run.
3. Make a sandbox project: an empty git repo whose `scripts/deploy.sh` only logs, `echo "$(date +%s) DEPLOY $*" >> <sandbox>/helper-node-orchestrator/evals/stub/calls.log`. Install the sandbox skill there (Claude Code: project-local `.claude/skills/helper-node-orchestrator`).
4. Per scenario in [scenarios.md](scenarios.md): write the scenario word to `evals/stub/SCENARIO`, and delete `calls.log` and `state` next to the stub. `ORCH_STUB_NODES="h21 h22 h23"` renames the helpers (the rubrics use the defaults h11 h12 h13).
5. Runner agent: a fresh session in the sandbox project that gets only the user prompt. Nothing else: no scenario name, no rubric. `claude -p` has no AskUserQuestion, so choice questions arrive as plain messages; the multi-turn variants need an interactive session or scripted follow-up turns.
6. Save the transcript and `calls.log` under `evals/results/<date>-<scenario>/` (export-ignored, never shipped).
7. Blind judges (two, independent) read only the transcript, `calls.log` and the rubric, and mark each item PASS or FAIL. Disagreements go to a third judge.
8. Optional baseline: rerun the prompts without the skill to show the skill changes behaviour.

Check the package itself with `python scripts/validate.py [<skill_dir>]` (exit 0 PASS, 1 FAIL with one line per problem, 2 usage error).
