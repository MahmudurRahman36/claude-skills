# Release through post-live verification

Load only when shipping is in scope. Resolve the repository's actual release path; do not invent universal tags or server commands.

## Before release

- Match requested target, branch/remotes, credentials and authorized actions. Preserve existing protections.
- Verify current revision/config against acceptance gates, review staged diff, exclude secrets and unrelated changes.
- Record artifact identifier (commit/tag/image digest), migration order, compatibility and rollout/rollback procedure.
- For destructive data work, verify backup and recovery constraints before execution; code rollback alone may not restore data.
- Confirm meaningful observability: logs, errors, health/readiness and relevant latency/business checks.

## Publish and deploy

- Commit focused changes; push only intended branch/remotes. For multiple mirrors, check each remote history, reconcile without force-pushing, then verify each resulting ref.
- Run configured CI/release trigger and read its result. A successful push is not a deployment.
- Follow environment promotion sequence. Confirm actual deployed artifact/config and dependency readiness.
- Use a supported wait/monitor mechanism for asynchronous rollout. Do not claim a deployment complete while it is pending.

## Post-live acceptance

Verify from a user-relevant access point, against the deployed revision:
- health/readiness and dependency connectivity;
- critical user journey, including browser behavior for UI;
- intended permissions and relevant negative paths;
- persistence/data integrity and migrations;
- logs/errors and performance against established baseline/acceptance thresholds.

Use approved test accounts/fixtures; do not cause real payments, messages or destructive production writes without authorization.
Record timestamp, environment, artifact, check, expected/actual result and evidence location without credentials or personal data.
Use the agreed observation window; if none, perform immediate checks and disclose that sustained monitoring was not performed. Schedule continuing monitoring only when requested.

## Failure and closure

Stop promotion when a required gate fails. Diagnose from logs and artifact identity. Apply an authorized fix or rollback only when safe for schema/data compatibility, then retest affected gates.
For partial migrations, preserve recovery evidence and reconcile migration records with actual schema/data before retrying. Verify retry idempotency; assess forward repair versus restore, backup validity and data-loss implications. Never blindly rerun a migration or roll back incompatible application code. Escalate recovery decisions exceeding existing authority; verify data integrity and application compatibility after recovery.
If blocked on access, third-party action or a user decision, report exactly what passed and what remains incomplete; do not broaden permissions or fabricate success.
Handoff: release identity, verified behaviors, residual risks, rollback/runbook location, ownership and any scheduled monitoring.
