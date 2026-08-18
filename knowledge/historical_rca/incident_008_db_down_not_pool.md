# INC-008 — Similar-looking DB issue but DB unreachable

## Service
payments-api

## Severity
P1

## Symptoms
- DB-related errors in logs (looks like INC-001)
- But Postgres health checks fail / connection refused
- Pool gauge not at capacity

## Root cause
Database unreachable — **not** connection pool exhaustion.

## Remediation
Escalate infra; do **not** blindly apply INC-001 restart-only fix.

## Verification
Recovered after DB restored.

## Lessons
RAG can retrieve similar historical RCA; Diagnostic must not copy the fix blindly.
