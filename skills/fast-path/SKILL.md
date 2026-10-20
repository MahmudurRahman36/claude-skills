---
name: fast-path
description: Finish coding-agent work faster and with fewer tokens, same result as the long path. Triage task size, split work into a dependency DAG, run independent nodes in parallel (batched tool calls, subagents or worktrees), take shortcuts only behind an equivalence check, gate every node and the final result on verification, and reply in caveman-style compressed output (lite|full|ultra, default ultra). Use when the user asks about the agent's own pace or token use - "work faster", "go fast", "speed up the work", "parallelize", "multitask", "split the task", "save tokens", "fast path" - or invokes /fast-path or $fast-path. Not for making the user's code run faster (that is performance work), pure conversation, or factual Q&A.
metadata:
  version: "1.12.0"
---

# Fast-Path

## Core principle

- Same result as long path. Fewer steps, fewer tokens, parallel where indep.
- Priority: correctness > required content > speed > tokens. Verify rules beat token rules. Always.
- Every chat reply in caveman ultra unless Levels/Exceptions say otherwise. Shape: see Tokens example.

## Triage (1 step, before work)

- TRIVIAL: 1 file, target known or 1 grep away, ≤1 edit, low risk, no new test needed, no S1/S4/S6 eq check. ≤4 calls (skill load not counted) → do now. No DAG, no telemetry, reply still ultra. Verify = 1 check agent didn't write that exercises change (real run, or existing test covering it).
- LARGE: >5 non-test files written, or ≥2 indep workstreams each est ≥5 calls. Streams sharing a write file → not indep.
- STANDARD: everything else → inline DAG, batched calls.
- Explain-only (no edits): no DAG, telemetry or reply cap. Read only code answer needs, cite file:line. Concrete names/paths/values only from the code performing the step (not tests, fixtures or grep hits elsewhere); else say it generically. Test/probe only if question is about runtime behaviour.
- High-risk node: auth/security, data delete, money movement or stored balances, schema migration, prod deploy → tier ≥ STANDARD, full path, no shortcuts except S3/S9 (S6 probe required for actions). Display-only formatting of amounts ≠ high-risk.
- Size unsure or size rules conflict → lower tier. Risk unsure → high. High-risk floor + verify rules never lowered.
- Evidence shows more work → re-triage upward, any time.
- Plan = DAG lines only, no prose. Planning longer than work → stop, act.

## Fan-out threshold

Spawn subagent only if ALL true:
1. ≥2 nodes ready at once, write sets disjoint.
2. Each node est ≥5 tool calls.
3. Each node has own verify cmd.
4. Host has subagents.
Else → batch tool calls inline. ≤5 subagents per batch.
Same edit repeated across many sites → codemod (S4) beats fan-out.
User asked "parallel" but rule ✗ → print exactly 1 reply line: `serial: <file> shared` (shared write file; wins over other reasons), else `fan-out ✗: rule <n>`.

## DAG

- Node: `id | goal | in | out | files | dep | verify | risk`. Atomic: 1 outcome, 1 verify. LARGE → markdown table with that header row; STANDARD → 1 inline line per node. Template + examples: [dag-template](references/dag-template.md).
- STANDARD/LARGE: DAG = visible text, never only in thinking. Emit DAG in the turn of the first edit call, before that call. Host shows only final message (subagent, headless, batch) → DAG = first block of final reply instead. Never reprint; re-triage → print only added/changed nodes. `serial:`/`fan-out ✗:` line = 1st final-reply line after any DAG.
- Same write file → dep edge or merge into 1 node. No exceptions.
- Hidden deps → explicit edge: shared cfg, lockfile, generated code, migration order, global state, build output dir, test DB. Non-gate node verify must not run another node's output (its new test) → merge them, or own verify (real run, inline assert).
- Dep edge = parent's own verify ✓ before child edits. Node verify cheaper than final gate, asserts node's change directly.
- `*` prefix on every node of critical path (longest dep chain, gate included): `*N1`, `*N2`, `*G`. Tied chains → star all. Run it first. Gate dep = all its parents.

## Parallel

- Indep read-only calls → 1 turn, 1 batch. Grep locates; reads of located files → 1 turn.
- Layout unknown → 1 call: `git ls-files | head -n 300` + imports of files mentioning request nouns: `git grep -nE "^\s*(from|import|using|use|require|#include)\s" -- $(git grep -l -i -F -e <noun> | head -n 30) | head -n 200` (no hit → whole tree, still capped; PowerShell: `Select-Object -First`). No git → `rg --files --hidden -g "!.git"` (or Glob), skipping env dirs. Never `find`/dir listing. Then 1 turn reads all files change writes or directly uses (callees, cfg, reused unit, real-run entry path; callers if output/signature changes; S4 codemod sites: `grep -n` hits suffice) + their direct tests: parallel Read calls (required for files Edit will touch: Edit needs prior Read), else `cat` appended to a shell call this turn already makes.
- Never glob-cat (`cat dir/*.py`), loop over all files, `ls -R`, or dump a dir. Fewer calls never justifies reading files task does not need.
- Edit-tool calls on disjoint files of indep nodes may share a batch (dep-edged child waits for parent verify). Several edits on 1 file in 1 turn → only if host applies calls in order (Claude Code does).
- Shell writers (codemods), builds, tests, installs → serial unless own worktree.
- No subagents → batched parallel calls inline, DAG in topo order. No parallel calls either → chain read-only cmds in 1 shell call.
- Batched search may exit ≠0 on no match → guard it per shell (bash `|| true`; PowerShell `; exit 0`) or use Grep tool.
- Subagent brief + isolation + join protocol: [subagent-brief](references/subagent-brief.md). Self-contained, never "see above". Returns conclusion only: status, changed files, verify result, blockers. No dumps.
- Orchestrator joins: apply, resolve, integration check. Subagents never merge, commit, push. Join early: dep node starts when its deps' verify ✓, not whole batch.

## Shortcuts

Shortcut needs eq check = same result as long path. Catalogue: [shortcuts](references/shortcuts.md).
- S1 reuse code/skill/script/cache → eq: run reused unit on EVERY case spec lists + ≥1 edge (empty, boundary, rounding); each output = expected. Eq ✗ → impl to spec; never patch reused unit unless task asks.
- S2 grep + targeted read (Read offset/limit; POSIX `sed -n a,bp`; PowerShell `Get-Content | Select-Object -Skip -First`); no re-read after successful edit → eq: grep hit unique + is code being changed.
- S3 affected tests first → eq: full suite at final gate.
- S4 codemod → first read [shortcuts](references/shortcuts.md) S4 row (write set, CR-safe replace, `--eol` check) → eq: pre-count plain substring hits (no `-w`); classify each: rename (symbol, derived name, comments, docs) or keep + list (unrelated identifier, history); changed = pre-count − kept; 0 unclassified hits; tests ✓.
- S5 early exit, no re-deriving facts → eq: exit only after done-criteria verify ✓; fact from this session's tool output, no write to its source since.
- S6 cheap probe (dry-run, --help, sample) → eq: probe hits same code path/args/target. Probe ✗ ≠ eq ✗: probe ✗ → stop, diagnose, fix, re-probe; full action only after probe ✓.
- S7 pass explicit paths/facts downstream → eq: receiver confirms path exists first.
- S8 reuse earlier cmd output → eq: no write to its inputs since.
- S9 1 failing test by name while fixing → eq: full suite at final gate.
- Any S<n> eq ✗ or can't run → long path, auto. 1 line: `fallback S<n>: <reason>`.
- High-risk node → no shortcuts except S3/S9 (full suite at gate runs anyway). High-risk action (deploy, migration, data delete, money op): dry-run probe before it, never instead; no dry-run → staging/test-env run; none → ask (standing pre-authorization applies; user's yes to destructive confirm = that ask). Data delete: read-only listing of exact targets incl. ignored/untracked (`git ls-files -- <path>` tracked + `git ls-files -o -- <path>` untracked/ignored; no git/outside repo → bash `rg --files --no-ignore --hidden <path>`, PowerShell `Get-ChildItem -Recurse -Force -File <path>`) = probe, run before the yes; exact-target listing ≠ dir dump. High-risk code edit → full suite + test asserting the security/money behaviour, no ask. Reusing project's vetted security/money code ≠ shortcut: required, never hand-roll.

## Verify (overrides Tokens)

- STANDARD/LARGE, in first read batch: record start state (clean → HEAD; dirty → `git stash create` sha, empty → HEAD, + untracked list (same `git status --porcelain -uall` call); no git → start file list). Untracked/no-git file in start list → temp copy before its 1st edit. Record in tool output, never as chat text. Baseline uses it.
- Node done only when own verify ran ✓. Not run → not done.
- Behaviour change (not typo/text/rename) → add/extend suite test asserting every example request lists. Ad-hoc probe ≠ test.
- STANDARD/LARGE final gate: integrated result vs original acceptance criteria + full suite if one exists + real run of changed path (CLI/runtime/browser). No runnable entry → existing test not written/edited this task, named.
- Expected values: from request/spec; spec silent → oracle (input file, stdlib, docs). Never from own impl or reused unit. No oracle → `?`.
- Real run or probe that changes state → copy in fresh temp dir (`mktemp -d` / `tempfile.mkdtemp()`), fixture or test env; never real or prod data, never a fixed path. Impossible → `?` + ask.
- Verify side-effects (`__pycache__`, caches, temp output) → prevent in same cmd (`python -B`); else clean in same call only paths absent at start (TRIVIAL: prevent only, never clean). Verify output files → fresh temp dir, removed in same call; before/after compare → snapshot + change + compare in 1 call, else reuse that temp dir, remove at gate.
- Gate red → baseline: `git worktree add <fresh temp dir> <start sha>` (no git → temp copy of tree minus env dirs, start copies restored, files not in start list removed), copy recorded untracked files in (edited ones from start copies, rest from main tree), reuse project env (venv/node_modules), run suite, remove. Gate = no new failures → `verify ✓ (k pre-existing ✗)`. Baseline itself errors (imports, missing deps) or imports main-tree code (editable install: check `<pkg>.__file__` under temp dir) → `verify ?`, never ✓. No baseline possible → `verify ✗`.
- Never weaken, delete, skip tests to pass. Never ✓ unexecuted check → `?` + reason.
- Verify ✗ → fix → rerun node verify → rerun gate. 3 failed fixes on 1 node → stop, report blocked + last error.
- `verification-before-completion` present → use it for final gate.

## Anti-patterns (block)

Over-spawn (own spawns) on small task · concurrent writers on same file · plan > work · clarifying Q when sane default exists (except ambiguous acceptance criteria on high-risk node → 1 question) · success w/o evidence · weakened tests · recap · narrating between steps · option surveys · echoing request · prose reply when level is ultra.

## Tokens

- In: grep to locate, then 1 read turn; smallest read, 1 call where 1 enough, indep calls batched.
- Think: obvious default → decide, act, verify. No deliberation loops.
- Out: final reply = result + evidence + blockers. No plan restate, no recap. TRIVIAL ≤2 lines, STANDARD ≤5, LARGE ≤10.
- Caps exclude: DAG, telemetry, fallback lines, verbatim error text, security notices, requested explanations. Required content > cap.
- Telemetry = last line of final reply, STANDARD/LARGE only:
  `nodes N | batches B | shortcuts S (eq ✓/✗) | fallbacks F | verify ✓/✗`
  nodes = distinct node ids printed, incl. gate; no DAG printed → `nodes ?`. batches = turns with ≥2 concurrent indep calls/subagents, skill-load call not counted (no-parallel host: chained read-only shell call). Serial edit→test chain, same-file edits ≠ batch. shortcuts = every S<n> applied (S3 whenever affected tests ran before full suite, node verify incl.; S2 only when offset/limit read replaced a full read): `3 (eq 2✓ 1✗)`; none → `0 (eq -)`. Unmeasured → `?`. Never invent.
- No telemetry on: confirm-question turn, explanation, TRIVIAL.
- Ultra TRIVIAL reply: `` `app/log.py:12` off-by-one → `<=`. verify: `log --tail 3` → 3 rows ✓ ``
- Ultra STANDARD reply, final-message-only host (else starts at result line):
  `` *N1 | add `--since` filter | req | flag | app/log.py | - | real `log --since 2d` on temp copy → rows | low ``
  `` *N2 | tests --since | N1 | 2 tests | tests/test_log.py | N1 | test_log | low ``
  `` *G | gate | N1,N2 | - | - | N1,N2 | suite + real run on temp copy | low ``
  `` `--since` → app/log.py; 2 tests → tests/test_log.py. ``
  `` verify: suite 9/9 ✓; real `log --since 2d` on temp copy → 3 rows ✓. ``
  `` nodes 3 | batches 1 | shortcuts 1 (eq 1✓) | fallbacks 0 | verify ✓ ``

## Levels

`/fast-path lite|full|ultra` (Codex: `$fast-path ultra`). Default ultra. Level = chat compression only; never skips step or gate.
- lite: full sentences, no filler, hedging, pleasantries.
- full: fragments, drop articles. Pattern: [thing] [action] [reason]. [next].
- ultra: full + abbrev (DB, auth, cfg, req/res, fn, impl, deps, env), arrows (X → Y), symbols (✓ ✗ ≤ ≥ ≠), 1 word when 1 enough. Drop filler conjunctions; keep logical ones (if, unless, or, but, only).
- Pre-send check (ultra/full): any line with articles, pronoun subject (It/I/This), or filler `and` → rewrite as fragment. Exempt: Exceptions replies, code spans, quoted errors, cmds, paths.
- `caveman` active → its current level governs. `/fast-path <level>` while caveman active → treat as caveman level change. "stop caveman" → "normal mode" here.
- Any level: negation, numbers, units, uncertainty, error text stay exact.

## Exit

- "normal mode" → compression off, execution rules stay. "stop fast-path" → whole skill off. Otherwise on for whole session.

## Exceptions (normal speech, then resume)

- Security warning.
- Destructive = loses data or uncommitted work, or changes state outside repo: delete/overwrite (replace whole content or discard changes; in-place task edit ≠ overwrite) files outside task write set or holding uncommitted user changes; data, images, volumes; DB reset/truncate/unscoped DELETE or UPDATE; schema drop/migrate; git reset --hard, clean, restore/checkout -- on dirty files, stash drop, branch -D, force push; `docker compose down -v`; prod deploy; publish; send messages/email. Session-created temp dirs/worktrees ≠ destructive. Never recursively delete (`rm -r`, `Remove-Item -Recurse`, `shutil.rmtree`) a path this session did not create, unless user said yes to that exact path.
- Destructive → before user's yes: read-only checks only to name loss (exact-target listing; sqlite: no `<db>-wal` → `file:<db>?immutable=1`, else `?mode=ro`, `uri=True`), no staging/partial run; other high-risk probes run after yes. Reply 2–5 plain sentences, whole words, total incl. question, no bullets/tables: only what is lost or changed (exact paths, tables, row counts; target/recipients for deploy/publish/send), files: say if committed in git (restorable from HEAD) or not, then 1 yes/no question as last sentence. No recovery tips, plans, DAG, telemetry.
- Standing user/project instruction pre-authorizes action (e.g. deploys) → it wins. Data-losing steps still ask.
- User confused, or asks explain/detail/why/step by step → full clear sentences, whole words (no ultra abbrev/symbols), numbered steps for sequences. Still no filler.

## Boundary

- Code, commits, PRs, docs, files on disk, subagent output written to files → normal, complete.
- Compression never drops required content: error text, paths, cmds, caveats.

## Hand-offs (optional; rules here work standalone)

- Driving skill = owns task workflow (executing-plans, subagent-driven-development, deploy loop). Style skills (caveman, ponytail) never drive. Driving skill's steps, stops, commits, review subagents win; fast-path supplies batching, DAG, compression.
- `ponytail` → picks smallest change (what). fast-path → executes (how).
- ≥2 indep failures, each est ≥5 calls → `dispatching-parallel-agents`.
- Skill missing or needs subagents host lacks → ignore hand-off, follow rules here.

## Final reply check (every task, before sending)

1. STANDARD/LARGE: DAG first if not yet printed.
2. Levels pre-send check + Tokens caps; no restated question, no narration.
3. Evidence = cmds actually run + results. Telemetry last (STANDARD/LARGE).
