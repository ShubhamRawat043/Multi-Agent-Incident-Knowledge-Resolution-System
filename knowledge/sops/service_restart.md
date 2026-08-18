# SOP: Service Restart

## Purpose
Safely restart a stateless demo service replica when it is unhealthy or leaking resources.

## Preconditions
- Service is in the demo allow-list (`orders-api`, `payments-api`).
- Health endpoint `/health` is reachable (or was recently).
- Risk tier: **T1** (reversible state change).

## Steps
1. Confirm target container name and label `incident-demo=true`.
2. Capture current `/health` and error rate from Prometheus.
3. Restart the container via Docker API (`restart_service`).
4. Wait 10–30 seconds.
5. Re-check `/health` and Prometheus error rate.

## Expected outcome
Error rate returns near baseline; health reports `ok`.

## Rollback
N/A — restart is idempotent. If restart loops, escalate.

## Escalation
If error rate remains elevated after restart, treat root cause as unresolved (likely bad deploy or dependency).
