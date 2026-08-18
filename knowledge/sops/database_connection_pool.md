# SOP: Database Connection Pool Exhaustion

## Purpose
Recover services that cannot obtain DB connections due to pool saturation or leaks.

## Symptoms
- Connection pool > 95%
- Timeout / ConnectionPoolTimeout errors in Sentry
- Increased latency
- Often **no** recent successful deploy

## Diagnosis
1. Check `db_connections_in_use` Prometheus gauge.
2. Inspect Sentry for `ConnectionPoolTimeout` / DB timeout.
3. Confirm Postgres itself is reachable (DependencyAnalyst).
4. Check GitHub for recent deploys (absence strengthens pool-leak hypothesis).

## Remediation
1. Restart affected application replicas (T1) to clear leaked connections.
2. Re-check pool gauge and error rate.
3. If exhaustion returns quickly, investigate connection leak in code and escalate.

## Escalation
Persistent exhaustion after restart → escalate; do not blindly roll back without deploy evidence.
