# Runbook: Dependency Failure

## Pattern
Caller (`orders-api`) fails with 502/503 while dependency (`payments-api`) is unhealthy.

## Diagnosis
- DependencyAnalyst: upstream/downstream health
- LogAnalyst: "payments-api unreachable" / 502 bodies
- MetricsAnalyst: error rate on both services

## Actions
1. Fix or restart the **dependency**, not only the caller.
2. Avoid cascading restarts.
3. Verify orders recover after payments recovers.
