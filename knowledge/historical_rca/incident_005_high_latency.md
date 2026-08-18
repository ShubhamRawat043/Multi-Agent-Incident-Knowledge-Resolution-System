# INC-005 — High latency from inefficient path

## Service
payments-api

## Severity
P3

## Symptoms
- Latency histogram elevated
- Error rate near normal
- No deploy in window

## Root cause
Slow code path / artificial delay under load.

## Remediation
Investigate slow path; temporary scale/restart if workers wedged.

## Verification
Latency returned after fault cleared.
