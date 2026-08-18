# INC-004 — Payments dependency timeout

## Service
orders-api (symptom) / payments-api (cause)

## Severity
P2

## Symptoms
- orders-api 502/503
- payments-api timeouts
- Dependency health failing

## Root cause
payments-api unavailable (dependency failure), not an orders-api code bug.

## Remediation
Restore payments-api; verify orders recover. Avoid restarting only orders-api.

## Verification
Successful after dependency recovery.
