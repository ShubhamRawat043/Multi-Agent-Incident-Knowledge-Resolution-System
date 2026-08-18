# INC-007 — Cascading orders → payments failure

## Service
orders-api + payments-api

## Severity
P1

## Symptoms
- Both services elevated error rates
- orders failures dominated by upstream 5xx
- payments root exception present

## Root cause
payments-api primary failure cascading into orders-api.

## Remediation
Remediate payments first (restart or rollback), then verify orders.

## Verification
Successful after payments recovery; orders followed.
