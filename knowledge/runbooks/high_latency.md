# Runbook: High Latency

## Symptoms
- Prometheus latency histogram elevated
- Error rate may still be low

## Causes
- Injected delay / slow dependency
- Inefficient DB query
- Resource saturation (CPU/memory)

## Actions
1. Identify anomaly window via MetricsAnalyst.
2. Correlate with deploy (ChangeCorrelator).
3. Check dependency latency.
4. Prefer non-destructive investigation before T2 rollback.
5. Restart only if memory leak / wedged workers suspected.
