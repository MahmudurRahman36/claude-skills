# Requirements and plan

Use one compact ledger for M/L tasks; keep S tasks inline. Do not create ceremony without value.

```text
Outcome/users:
Current behavior / desired behavior:
Must-haves / non-goals:
Constraints: stack, data/privacy, accessibility, performance, budget
Target and endpoint: local / PR / staging / production / post-live
Authorization and exclusions:
Unknowns: blocking questions / stated reversible assumptions
Acceptance:
A1 | behavior | check | environment | pending/pass/fail/blocked/N/A
Plan:
1. paths/component | change | depends on | verification
Risk/rollback:
Release trigger and live acceptance:
```

Acceptance must be observable, including meaningful negative paths. Each gate needs an owner when delegated. Track evidence by revision/config/environment and invalidate only what subsequent changes affect.
Feature: clarify actor, permissions, inputs, outputs and failure behavior.
Bug: record reproduction, expected/actual behavior, cause or testable hypothesis.
Migration: compatibility, backup/restore, ordering, idempotency and rollback limits.
Do not infer production deployment from a generic request to build. Preserve explicit end-to-end authorization without repeated confirmation.
