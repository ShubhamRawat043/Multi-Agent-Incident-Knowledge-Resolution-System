# INC-006 — Configuration mismatch after deployment

## Service
payments-api

## Severity
P2

## Symptoms
- ValueError: invalid DATABASE_URL / API key
- Errors immediately after config-touching deploy

## Root cause
Configuration mismatch introduced with release.

## Remediation
Correct config + restart (T1). Rollback if config cannot be fixed quickly (T2 HITL).

## Verification
Successful after config fix.
