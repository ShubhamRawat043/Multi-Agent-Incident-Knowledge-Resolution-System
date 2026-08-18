# SOP: Deployment Rollback

## Purpose
Roll a demo service back to a previously known-good image tag after a bad deploy.

## Preconditions
- ChangeCorrelator evidence shows a deploy immediately preceding the anomaly window.
- Stack traces or error signatures point at the new release.
- Risk tier: **T2** (production-impacting) — **requires HITL approval**.

## Steps
1. Identify current `IMAGE_TAG` / deploy SHA.
2. Identify previous known-good tag (e.g. `v1.7` if current is `v1.8`).
3. Obtain human approval.
4. Execute `rollback_deploy` to the previous tag (allow-listed images only).
5. Run healthcheck and verify Prometheus error rate / Sentry issue volume.

## Expected outcome
Error rate drops to baseline; Sentry stops emitting new matching issues.

## Rollback of the rollback
Re-deploy the newer tag only after a fix is confirmed.

## Escalation
If rollback does not recover, escalate to on-call — root cause may be data/config, not the binary.
