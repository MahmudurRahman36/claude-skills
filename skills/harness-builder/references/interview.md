# Interview: settle the decisions discovery couldn't

Use this when the sufficiency gate in `discovery.md` still has open decisions.

## Protocol (grilling)

If the host has the `grilling` skill (or `grill-me`, which calls it), invoke it on the open decisions and pass it the design tree below. Otherwise follow these rules. They are Matt Pocock's grilling method (https://github.com/mattpocock/skills/tree/main/skills/productivity/grilling).

- Map the decisions as a **design tree**: each decision branches into the decisions that depend on it.
- Work in **rounds**. The **frontier** is every open decision whose prerequisites are already settled. Ask the whole frontier in one round, numbered. Give each question **your recommended answer**, drawn from discovery evidence, so the user can just reply "yes".
- A question that depends on another question still open in this round waits for a later round.
- **Facts are your job, never the user's.** If a question needs a fact (a file, a folder, a connector, a volume), look it up, using a subagent if needed. Don't block the round on it: ask the rest of the frontier now.
- Recompute the frontier after every answer. You are done when the frontier is empty and nothing is silently assumed.
- Then restate the shared understanding in 5-10 lines and get an explicit "yes" before designing.

Round format:

```
**Q1 - <title>**: <question, with options if useful>
Recommended: <answer + one-line why, citing evidence>

**Q2 - ...
```

No user available (unattended run, or a subagent)? Don't invoke `grilling`, and don't invent answers. Write **all** rounds into the spec's "Open questions (blocking)" section, each with your recommendation, marking later rounds as depending on the answers to earlier ones. Then stop after Phase 2.

## Design tree for a harness

Round 1 asks every root node whose answer doesn't depend on another: A, B and C below. Children are asked only after their parent is settled.

- **A. Version control:** the folder is not a git repo. May I `git init`? (Ask only if not a repo.)
- **B. Inputs:** confirm the source you found and the access method: connector, MCP, folder, API.
  - **Sensitive data?** Personal data, secrets, regulated data, attributes that must not influence judgment, retention.
- **C. Rhythm:** on demand, a schedule (which days, what time, **which timezone**), or an event. Volume per run.

- **D. Which job?** The repeated task and its goal, in the owner's words. Use 5 whys to reach the real outcome, not the symptom.
   1. **Who decides?** The decision owner, and what the AI must never decide.
      - **What is irreversible?** Actions the AI must never take: send, delete, contact, pay, publish, merge.
   2. **Worth it?** Given the job and the rhythm (C), is a harness right, or would a script or a one-off prompt do? Ask only when frequency or verifiability is low.
   3. **Output.** Who reads it, which format, which destination. Offer a draft template drawn from past outputs.
   4. **Quality bar.** The criteria that matter (3-7). For each, what evidence in the input proves it.
      - **Scale.** Binary pass/fail (recommended for most checks: clearer and more consistent), or a 3-point anchored scale such as 1/3/5 when grading strength matters, with weights.
      - **Unsure rule.** Pick the lower score, the more urgent option, or always send to a human?
      - **Verdict rules.** Thresholds and must-have criteria. When is a human always required?
      - **Example.** One strong past item, and the surface features that must NOT count.
   5. **Verification.** Is a different model allowed for the verifier? Which checks can be code?
   6. **Memory.** What must be remembered between runs (processed ids, learned rules)? Who approves rule changes?
   7. **Golden set.** 5-20 past items with the owner's labels. Who provides them, and when?
   8. **Automation.** Schedule now or later? Local (machine must be on) or cloud? Where should reports land?

Keep each question short. Lead with your recommendation. Never ask what a file or a tool call can answer.
