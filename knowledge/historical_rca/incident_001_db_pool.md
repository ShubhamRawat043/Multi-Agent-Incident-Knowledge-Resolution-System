# INC-001 — DB connection pool exhaustion

## Service
payments-api

## Severity
P2

## Symptoms
- ConnectionPoolTimeout in Sentry
- db_connections_in_use at capacity
- Latency spike
- No recent deployment

## Root cause
payments-api leaked DB connections under load.

## Remediation
Restart payments-api replicas. Error rate recovered.

## Verification
Successful — pool gauge returned to baseline within 2 minutes.

## Lessons
Absence of deploy + pool metric is strong signal for leak vs bad release.
