# SOP: Memory Pressure

## Purpose
Respond to rising memory usage / possible leaks in demo services.

## Symptoms
- Rising `process_memory_bytes_sim`
- Eventual OOM or latency growth
- May coincide with retry storms (symptom, not always root cause)

## Diagnosis
- MetricsAnalyst: memory trend and anomaly window
- LogAnalyst: repeated exceptions / retry loops
- ChangeCorrelator: recent deploy correlation
- Beware **false leads**: CPU/memory spikes can be symptoms of bad deploys

## Remediation
1. Restart service (T1) for temporary relief.
2. If tied to a bad deploy, prefer rollback (T2 + HITL).
3. Verify memory returns toward baseline.

## Escalation
Recurring leak without deploy correlation → escalate for code fix.
