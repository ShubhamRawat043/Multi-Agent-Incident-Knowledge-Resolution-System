# INC-002 — Bad payments deployment

## Service
payments-api

## Severity
P1

## Symptoms
- Deploy v1.8 at T+0
- Error rate jumped at T+1m
- Sentry: TypeError in payment processing

## Root cause
Faulty release v1.8 introduced NoneType handling bug.

## Remediation
HITL-approved rollback to v1.7. Verified recovery.

## Verification
Successful.

## Lessons
ChangeCorrelator + stacktrace + anomaly window → high confidence rollback.
