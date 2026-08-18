# Runbook: Postgres Timeout

## Symptoms
- Sentry: DB timeout / operational error
- Latency up
- Possibly pool saturation

## Differentiate
- **Pool exhaustion**: connections in use ~ max, Postgres reachable
- **Postgres down**: dependency health fails, connection refused
- Do **not** blindly apply the historical pool-exhaustion fix if DB is unreachable

## Actions
1. Confirm Postgres health.
2. If pool leak in app: restart app replicas.
3. If DB down: escalate infra; avoid useless app restarts.
