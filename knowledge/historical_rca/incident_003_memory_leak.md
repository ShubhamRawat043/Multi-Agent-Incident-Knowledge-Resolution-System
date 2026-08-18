# INC-003 — Memory leak in orders-api

## Service
orders-api

## Severity
P3

## Symptoms
- Rising memory gauge
- Gradual latency increase
- No sudden error storm initially

## Root cause
Memory leak under repeated order creation path.

## Remediation
Restart orders-api; scheduled code fix.

## Verification
Partial then full after restart + deploy of fix.
