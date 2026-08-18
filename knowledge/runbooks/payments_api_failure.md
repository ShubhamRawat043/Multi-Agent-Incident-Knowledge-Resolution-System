# Runbook: payments-api Failure

## Service
`payments-api`

## Common failure modes
1. Application exception / HTTP 500 storm
2. Bad deployment (TypeError in payment processing)
3. DB timeout / connection pool exhaustion
4. High latency (artificial delay or slow queries)
5. Config error (bad DATABASE_URL / API key)

## Triage checklist
- Sentry: latest issue title + stacktrace
- Prometheus: `http_error_rate{service="payments-api"}`, latency histogram
- GitHub: deploys in last 30 minutes
- Dependency: Postgres reachable?

## Remediation matrix
| Root cause | First action | Risk |
|---|---|---|
| Crash / transient | restart_service | T1 |
| Bad deploy | rollback_deploy | T2 (HITL) |
| Pool exhaustion | restart_service | T1 |
| Dependency down | do not restart callers endlessly; escalate | — |
| Config error | fix config + restart | T1 |

## Verification
Error rate < 5% for 2 minutes; `/health` ok; no new matching Sentry events.
