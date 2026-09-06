# Audit & Fixes — what was broken and what I changed

This document explains the bugs found in the codebase, why each one mattered, and
exactly how it was fixed. It is written for someone new to the project, so each
section starts with the plain-English version before showing code.

**Summary:** 16 real problems were confirmed. Four of them would have visibly
broken the demo; those are fixed and covered by tests (sections 2-5). Two more —
the test catalogue being documentation rather than tests, and the scenario runner
ignoring its own argument — were fixed afterwards so the whole behavioral suite
can actually be demonstrated (section 10). The remaining ten are documented at
the end as known issues: they are real, but nothing was changed for them.

---

## Table of contents

1. [How the system is supposed to work](#1-how-the-system-is-supposed-to-work)
2. [Bug 1 — Verification could not tell the truth](#2-bug-1--verification-could-not-tell-the-truth)
3. [Bug 2 — The system could never try a second fix](#3-bug-2--the-system-could-never-try-a-second-fix)
4. [Bug 3 — Old actions leaked into new verdicts](#4-bug-3--old-actions-leaked-into-new-verdicts)
5. [Bug 4 — Restarting a container cured every fault](#5-bug-4--restarting-a-container-cured-every-fault)
6. [Two small extras I fixed along the way](#6-two-small-extras-i-fixed-along-the-way)
7. [Every file I changed](#7-every-file-i-changed)
8. [How to run the tests](#8-how-to-run-the-tests)
9. [Important: what this changes about your demo](#9-important-what-this-changes-about-your-demo)
10. [The behavioral test catalogue](#10-the-behavioral-test-catalogue)
11. [Known issues that are still open](#11-known-issues-that-are-still-open)

---

## 1. How the system is supposed to work

Before the bugs make sense, here is the loop the agent is meant to run. Think of
it as an on-call engineer working through an incident:

```
incident arrives
   ↓
triage        → is this a duplicate of something we already know?
   ↓
investigate   → four specialist agents each form a hypothesis
   ↓
synthesis     → combine hypotheses into one root cause
   ↓
retrieve      → pull the matching runbook / SOP from the knowledge base
   ↓
plan          → propose remediation steps (restart? roll back?)
   ↓
critic        → sanity-check the plan
   ↓
execute       → actually run the steps (with human approval for risky ones)
   ↓
verify        → did that actually fix it?
   ↓
   ├── yes → write the RCA report, close
   └── no  → go back and try something else
```

The **supervisor** is the component that decides which step comes next. It sits
in the middle of that loop and is re-entered after every step.

Three of the four bugs live in the bottom half of this diagram — the `verify`
step and the "no → try something else" arrow. That part of the loop is the whole
point of the project, which is why these were worth fixing first.

---

## 2. Bug 1 — Verification could not tell the truth

**File:** `agent/verification.py`

### The plain-English version

After the agent tries to fix something, the `verify` step is supposed to check
whether the service actually recovered. It could not do this correctly in either
mode of operation — it either always said "fixed" or always said "still broken."

### What was wrong (part A): the DRY_RUN shortcut

The original code ended with this block:

```python
# If DRY_RUN and actions succeeded as dry_run, mark recovered for demo continuity
# only when health is ok — still honest about dry-run
if settings.dry_run and health.get("ok") and had_success:
    recovered = True
    notes.append("DRY_RUN mode: treating successful dry-run + healthy as recovered for demo")
```

Three facts combine to make this a serious problem:

1. `DRY_RUN=true` is the **default** (`agent/config.py`, and it is set in `.env`).
   In dry-run mode, no remediation actually happens — the tools just log what
   they *would* do.
2. The `/health` endpoint in `demo_target/common/instrumentation.py` returns HTTP
   200 **unconditionally**. It reports the fault mode in the response body but it
   never actually fails. So `health.get("ok")` is basically always `True`.
3. This block ran **last**, so it overwrote the real error-rate check above it.

The result: on a normal run, this branch fired every single time and declared
recovery regardless of the real state of the service.

Here is the proof, from a test that feeds it an 85% error rate with the fault
still active:

```
[FAIL] DRY_RUN does not fake recovery at error_rate=0.85
       recovered=True, dry_run=True,
       notes=health ok; error_rate=0.85; DRY_RUN mode: treating successful
             dry-run + healthy as recovered for demo
```

An 85% error rate means roughly 5 out of 6 requests are failing. The system
called that "recovered."

### What was wrong (part B): turning DRY_RUN off did not help

You might think the fix is just `DRY_RUN=false`. That produces the *opposite*
failure.

The error-rate number comes from a Prometheus **gauge** called `ERROR_RATE`. A
gauge is just a number that sits there until something updates it. Look at where
it gets updated (`demo_target/payments_api/main.py`):

```python
@app.post("/pay")
def pay(body: PayRequest) -> dict:
    fault = apply_fault(SERVICE)
    ...
    if fault is not None:
        ERROR_RATE.labels(service=SERVICE).set(0.85)   # ← only set here
        raise HTTPException(status_code=500, detail=str(fault))

    ERROR_RATE.labels(service=SERVICE).set(0.01)       # ← and here
```

The gauge only moves when a request hits `/pay`. Nothing in the verification path
sends such a request — `run_healthcheck` only calls `/health`, which does not
touch the gauge.

So the sequence was:

1. Fault active, traffic runs → gauge is set to `0.85`.
2. Agent successfully rolls back the deploy → **service is genuinely fixed**.
3. Verification reads the gauge → still `0.85`, because nobody has sent a request
   since the fix.
4. Verification reports "not recovered" — forever.

There is a second reason Prometheus alone is not enough: `observability/prometheus.yml`
sets `scrape_interval: 15s`. Even if the gauge *did* update, Prometheus only
collects it every 15 seconds, so reading it immediately after a fix would return
a stale value anyway.

### How I fixed it

Two changes.

**1. Deleted the `DRY_RUN` override entirely.** There is no longer any branch that
sets `recovered = True` for demo convenience.

**2. Added a traffic probe.** Before reading any metrics, verification now sends
real requests through the business endpoint and counts how many fail:

```python
PROBE_COUNT = 4
PROBE_TIMEOUT = 10.0
RECOVERED_ERROR_RATE = 0.1


def _probe(service: str, url: str) -> dict:
    """
    Send a few real requests through the business endpoint.

    /health is a static 200 and the Prometheus error-rate gauge only moves when
    a request hits /pay or /orders, so without this the post-remediation
    metrics are whatever the incident left behind.
    """
    if "payment" in service:
        path = "/pay"
        body = {"order_id": "verify-probe", "amount": 1.0, "currency": "USD"}
    else:
        path = "/orders"
        body = {"item": "verify-probe", "amount": 1.0, "currency": "USD"}

    sent = 0
    failed = 0
    errors: list[str] = []
    with httpx.Client(timeout=PROBE_TIMEOUT) as client:
        for _ in range(PROBE_COUNT):
            sent += 1
            try:
                resp = client.post(f"{url.rstrip('/')}{path}", json=body)
                if resp.status_code >= 500:
                    failed += 1
                    errors.append(f"HTTP {resp.status_code}: {resp.text[:120]}")
            except Exception as exc:
                failed += 1
                errors.append(f"{type(exc).__name__}: {exc}")

    return {
        "sent": sent,
        "failed": failed,
        "error_rate": (failed / sent) if sent else None,
        "errors": errors[:3],
    }
```

This solves both halves of the problem at once. The probe generates fresh
evidence rather than reading stale evidence, **and** as a side effect it updates
the Prometheus gauge for the dashboard.

The decision logic is now a clear priority order:

```python
if probe_err is not None:
    # Best evidence: we just sent real traffic and watched what happened.
    recovered = bool(probe_err < RECOVERED_ERROR_RATE and health.get("ok"))
elif prom_err is not None:
    # Fallback: the service is unreachable for probing, use Prometheus.
    recovered = bool(prom_err < RECOVERED_ERROR_RATE and health.get("ok"))
else:
    # No evidence at all is not evidence of recovery.
    recovered = False
```

That last `else` matters. The old code, when Prometheus returned nothing, would
fall back to "health is OK and some action succeeded, so call it fixed." Absence
of information is now treated as absence of information.

Finally, when running in dry-run mode the verdict carries an honest note:

```python
if settings.dry_run and any(a.get("status") == "dry_run" for a in actions):
    notes.append(
        "DRY_RUN: remediation was simulated, not applied — "
        "any recovery here is not attributable to the plan"
    )
```

### Proof it works

```
[PASS] DRY_RUN + failing probes -> not recovered
       health ok; probe: 4/4 requests failed (error_rate=1.00);
       DRY_RUN: remediation was simulated, not applied
[PASS] healthy probes + real action -> recovered
       health ok; probe: 0/4 requests failed (error_rate=0.00)
[PASS] no probe + empty prometheus -> not recovered
```

---

## 3. Bug 2 — The system could never try a second fix

**File:** `agent/supervisor.py`

### The plain-English version

The headline feature of this project is: *try a fix → check → if it did not work,
think again and try something better.* That second attempt never happened. Not
once. The system would loop uselessly and then give up because it ran out of
turns.

### What was wrong

The supervisor decides what to do next using a chain of `if / elif` checks. The
relevant part:

```python
elif not state.get("retrieved"):
    decision = ... "retrieve"
elif plan is None:
    decision = ... "plan"
elif verification and not verification.get("recovered"):
    # Failed remediations: re-investigate if budget remains
    if inv_rounds < settings.investigation_max_rounds:
        decision = _Decision(
            next_action="investigate",
            reason="Verification failed — re-investigate",
            confidence=confidence,
        )
```

The failed-verification branch *does* fire, and it sends the agent back to
`investigate`. That part works. The problem is what it leaves behind.

`plan` and `verification` are pieces of shared state. When the agent loops back to
investigate, **nothing clears them**. So on the next pass through the supervisor:

- `plan is None`? No — the old, already-executed plan is still sitting there.
- So we fall through to `verification and not recovered` again → investigate again.

It gets stuck in `investigate → synthesis → supervisor → investigate` forever,
until the round counter runs out. Here is the real audit trail from a test run
where verification always fails:

```
exec=1 plan=1 rounds=7 status=escalated

triage_complete
decision          (investigate)
blackboard_update
root_cause
decision          (retrieve)
decision          (plan)
plan_created
approved
decision          (execute)     ← the ONLY execution
decision          (investigate) ← verification failed
blackboard_update
root_cause
decision          (investigate) ← stuck
blackboard_update
root_cause
max_rounds                      ← gave up by running out of turns
escalated
```

`execute` ran once. `plan` ran once. It escalated by exhausting its budget, not by
making any actual decision.

> **A note on the original audit's explanation.** The report I was checking said
> the `elif plan is None` check "short-circuits before the failure branch can ever
> produce a new plan." That is backwards — `plan` is *not* `None`, so that branch
> is skipped and the failure branch *does* run. The conclusion (no second attempt)
> was right; the mechanism was described inside-out. Worth knowing so you
> understand your own code correctly.

### How I fixed it

**Change 1 — clear the stale state.** The supervisor now builds its return value
into a variable so it can add fields, and when it loops back after a failure it
wipes the spent plan and verdict:

```python
# Looping back after a failed remediation: drop the spent plan and its
# verification, otherwise `plan is not None` routes every later round
# straight back to investigate and a second attempt never happens.
if (
    verification
    and not verification.get("recovered")
    and decision.next_action in {"investigate", "retrieve"}
):
    failed = [
        a.get("action_type")
        for a in (state.get("last_executed_actions") or [])
        if a.get("action_type")
    ]
    update["plan"] = None
    update["verification"] = None
    update["critic_feedback"] = (
        "The previous remediation did not restore the service "
        f"(attempted: {', '.join(failed) or 'none'}; "
        f"verification: {verification.get('notes')}). "
        "Propose a different, stronger remediation — do not repeat the same steps."
    )
    update["audit_log"] = update["audit_log"] + [
        {
            "agent": "supervisor",
            "event_type": "replan_requested",
            "payload": {"failed_actions": failed, "attempt": ver_retries + 1},
        }
    ]
```

Setting `plan = None` is the key line. Now on the next pass, `elif plan is None`
is true, and the agent goes to the planner for a genuinely new plan.

The `critic_feedback` string is a bonus: the planner already feeds
`critic_feedback` into its LLM prompt, so telling it what just failed steers it
away from repeating itself.

**Change 2 — success must beat the round cap.** In the original code the "did we
run out of turns?" check came *before* the "did we succeed?" check:

```python
# BEFORE
if rounds > max_rounds:
    return escalate
if verification and verification.get("recovered"):
    return close
```

That means a second attempt that actually *worked* could still be reported as
"escalated" simply because it happened on a late round. I swapped the order:

```python
# AFTER
if verification and verification.get("recovered"):
    return close
if rounds > max_rounds:
    return escalate
```

**Change 3 — raise the round budget.** A two-attempt run needs 8 supervisor rounds:

| Round | Action |
|-------|--------|
| 1 | investigate |
| 2 | retrieve |
| 3 | plan |
| 4 | execute → verify fails |
| 5 | investigate (plan cleared here) |
| 6 | plan |
| 7 | execute → verify |
| 8 | close or escalate |

The cap was `6`, so the run died at round 7 — right before the second execution.
I raised the default to `10` in `agent/config.py`:

```python
self.supervisor_max_rounds = _env_int("SUPERVISOR_MAX_ROUNDS", 10)
```

⚠️ **Your `.env` file also had `SUPERVISOR_MAX_ROUNDS=6`, which overrides the
default.** I changed that line to `10` as well. Without that edit the fix would
have had no effect on your machine.

### Proof it works

```
--- restart fails, rollback succeeds ---
INC-RETRY: exec=2 rounds=8 status=resolved
[PASS] second remediation attempt happens
[PASS] successful second attempt resolves

--- never recovers ---
INC-NEVER: exec=2 rounds=8 status=escalated
[PASS] two attempts before giving up
[PASS] escalation is deliberate, not round-cap
       audit: ... decision, replan_requested, blackboard_update, root_cause,
              decision, plan_created, approved, decision,
              verification_exhausted, escalated
```

Two executions instead of one. And notice the last event is now
`verification_exhausted` — a deliberate "I tried twice and it did not work"
decision — rather than `max_rounds`, which just means "I ran out of turns."

---

## 4. Bug 3 — Old actions leaked into new verdicts

**Files:** `agent/state.py`, `agent/execution.py`, `agent/verification.py`

### The plain-English version

The list of actions the agent has taken keeps growing across the whole incident.
Verification was reading that entire history, so a success from attempt #1 made
attempt #2 look successful too — even if attempt #2 did nothing at all.

### What was wrong

In `agent/state.py`, `executed_actions` is declared with a **reducer**:

```python
executed_actions: Annotated[list[dict], _add_dicts]
```

`_add_dicts` just concatenates lists. In LangGraph, a reducer means "when a node
returns this key, *append* it to what is already there rather than replacing it."
So `executed_actions` is a permanent, append-only history of everything the agent
has ever done during this incident.

Verification was scanning that whole history:

```python
actions = state.get("executed_actions") or []
had_success = any(
    a.get("status") in {"success", "dry_run"}
    and a.get("action_type") in {"restart_service", "rollback_deploy"}
    for a in actions
)
```

So if round 1 had a successful restart, then `had_success` stayed `True` for
every later round, no matter what happened in those rounds. A round where the
human *rejected* the action would still look like a round with a success in it.

### How I fixed it

The clean fix is to give verification a view of *only the latest round*. I added
a new state key with **no reducer**, which means it gets overwritten each time
instead of appended to:

```python
# agent/state.py

# execution
executed_actions: Annotated[list[dict], _add_dicts]
# Just the most recent execution round — executed_actions accumulates,
# so verification must not read it to judge the latest attempt.
last_executed_actions: list[dict]
audit_log: Annotated[list[dict], _add_dicts]
```

The execution node writes both — the growing history *and* the fresh snapshot:

```python
# agent/execution.py
return {
    "executed_actions": results,       # appended to the history
    "last_executed_actions": results,  # overwrites — just this round
    "status": "investigating",
    ...
}
```

And verification reads the snapshot:

```python
# Only this round's actions — executed_actions accumulates across rounds.
actions = state.get("last_executed_actions")
if actions is None:
    actions = state.get("executed_actions") or []
```

I also tightened the status check from `{"success", "dry_run"}` to just
`"success"`, since a dry run is by definition not a real success.

> **Worth understanding:** after the Bug 1 fix, this one is no longer
> load-bearing. Recovery is now decided by live probe evidence, not by "did some
> action succeed." But `had_success` is still used for the warning notes, and
> leaving a known-wrong data read in place is how bugs come back later.

---

## 5. Bug 4 — Restarting a container cured every fault

**File:** `demo_target/faults/injectors.py`

### The plain-English version

The fake faults were stored in the app's memory. Restarting the container wipes
memory, so restarting fixed *everything* — including a bad code deploy, which in
real life a restart cannot possibly fix. That makes the storyline "we tried a
restart, it did not help, so we rolled back" impossible to demonstrate.

### What was wrong

```python
# Shared process-local fault state (can also be driven by FAULT_MODE env)
_fault_lock = threading.Lock()
_active_fault: str = os.getenv("FAULT_MODE", "none")
```

`_active_fault` is a plain module-level variable — it exists only inside the
running Python process. `set_fault()` also wrote `os.environ["FAULT_MODE"]`, but
that only changes the environment of the *current* process, not the container's
configuration.

And `docker-compose.yml` sets:

```yaml
FAULT_MODE: "none"
```

So the moment the container restarts, a fresh process starts, reads
`FAULT_MODE=none` from the environment, and every fault is gone.

### How I fixed it

Fault state is now written to a **file**, which survives a restart. This works
because `docker restart` reuses the same container and keeps its writable
filesystem layer — only the process is replaced.

```python
# Fault state is persisted outside the process so a container restart does not
# silently cure every fault (which would make "restart didn't help" impossible).
# `docker restart` keeps the writable layer, so a file under /tmp survives it.
_STATE_PATH = Path(
    os.getenv("FAULT_STATE_PATH", str(Path(tempfile.gettempdir()) / "incident_fault_mode"))
)
```

But I did **not** make every fault immortal, because that would break a different
scenario. Some faults genuinely *are* cured by a restart, and the system should
model that honestly:

```python
# Faults a restart genuinely cures: their damage is leaked in-process resources,
# which the fresh process no longer holds. Code/config faults survive a restart
# because the bad release is still the one running.
RESTART_CLEARED_FAULTS = {"db_pool_exhaustion", "memory_leak"}
```

Think about what each fault actually *is*:

| Fault | Does a restart fix it in real life? | Why |
|-------|-------------------------------------|-----|
| `db_pool_exhaustion` | **Yes** | The leaked connections live in the process. A new process holds none. |
| `memory_leak` | **Yes** | Same — the leaked memory dies with the process. |
| `bad_deploy` | **No** | The buggy code is still the version being run. |
| `config_error` | **No** | The bad config is still the config. |
| `dependency_failure` | **No** | The upstream service is still down. |

The loader implements exactly that distinction:

```python
def _load_persisted_fault() -> str:
    try:
        mode = _STATE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return os.getenv("FAULT_MODE", "none") or "none"
    if not mode or mode in RESTART_CLEARED_FAULTS:
        # This process is fresh, so the leaked resources are gone with it.
        _persist("none")
        return "none"
    return mode
```

`set_fault()` and `clear_fault()` both write to the file, so the state stays
correct. `rollback_deploy` calls the service's `DELETE /fault` endpoint, which
calls `clear_fault()` — so a rollback still genuinely fixes a bad deploy.

### Proof it works

```
[PASS] bad_deploy survives a restart          -- bad_deploy
[PASS] db_pool_exhaustion is cured by a restart -- none
[PASS] clear_fault (rollback) persists as none  -- none
```

The full storyline now works: inject `bad_deploy` → agent tries a restart →
restart does not help → agent re-plans → rolls back → **actually fixed**.

---

## 6. Two small extras I fixed along the way

These were not in the list of four. I am calling them out separately so you know
exactly what is "the four fixes" and what is not — both are easy to revert.

### 6a. The heuristic planner repeated itself

**File:** `agent/planner.py`

Making a second attempt *possible* (Bug 2) is only useful if the second plan is
*different*. When the LLM is unavailable, the planner falls back to
`_heuristic_plan()`, which picks steps purely from the root-cause category — so
it produced the identical plan every time. The agent would retry a restart that
had already failed.

I added one branch at the top of the heuristic planner:

```python
# A restart that already failed verification must not be re-proposed —
# escalate to the next remediation up the ladder. Only when there is a
# deploy to revert: rolling back an incident with no release behind it is
# exactly the blind rollback the safety rules forbid.
tried = {a.get("action_type") for a in (state.get("executed_actions") or [])}
has_deploy_evidence = bool(
    event.get("deploy_tag") or event.get("deploy_sha") or "deploy" in category
)
if (
    "restart_service" in tried
    and "rollback_deploy" not in tried
    and has_deploy_evidence
):
    steps = [
        RemediationStep(
            action_type="rollback_deploy",
            target=service,
            risk_tier="T2",
            requires_approval=True,
            args={"image_tag": "v1.7"},
            expected_outcome="Restart did not clear the fault — revert the running release",
            rollback="Re-deploy newer tag after fix",
            source_citation=cite or "knowledge/sops/deployment_rollback.md",
        ),
        ...
    ]
elif "deploy" in category:
    ...
```

Note this is one of the few places where reading the *accumulated*
`executed_actions` is correct — we genuinely want the full history of what has
been tried.

> **The `has_deploy_evidence` guard was added later**, when the catalogue runner
> caught the first version failing three tests. Without it, a DB-pool incident
> with no release behind it would escalate to a rollback — which is precisely the
> "blind rollback" the safety rules exist to prevent. This is a good example of
> why the executable catalogue was worth building: it caught a bug in the fix.

The planner also now records the full step list in its audit entry, not just a
count, so the audit trail shows what each round actually proposed:

```python
"payload": {
    "summary": result.summary,
    "count": len(result.steps),
    "grounded": result.grounded,
    "steps": [s.model_dump() for s in result.steps],
},
```

### 6b. The memory-leak fault had no ceiling

**File:** `demo_target/faults/injectors.py`

The `memory_leak` fault allocated 5 MB on **every single request**, with no upper
bound. Left running under traffic, it would eventually exhaust the container's
memory and get the process killed — which would take your demo down mid-run.

```python
# ~500 MB ceiling on the simulated leak.
MEMORY_BALLAST_MAX_CHUNKS = int(os.getenv("FAULT_MEMORY_MAX_CHUNKS", "100"))

...

if mode == "memory_leak":
    # Continuously allocate ~5MB, up to a bounded ceiling
    if len(_memory_ballast) < MEMORY_BALLAST_MAX_CHUNKS:
        _memory_ballast.append(bytearray(5 * 1024 * 1024))
    return None
```

The leak still looks like a leak on the dashboard; it just cannot kill the box.

---

## 7. Every file I changed

| File | What changed |
|------|--------------|
| `agent/verification.py` | Rewritten. Removed the `DRY_RUN` override, added `_probe()`, evidence-based recovery decision, honest notes. |
| `agent/supervisor.py` | Clears `plan`/`verification` when re-planning; moved the recovery check above the round cap; added the `replan_requested` audit event. |
| `agent/state.py` | Added the `last_executed_actions` key (no reducer — overwrites). |
| `agent/execution.py` | Also returns `last_executed_actions`. |
| `agent/graph.py` | `initial_state()` seeds `last_executed_actions: []`. |
| `agent/config.py` | `SUPERVISOR_MAX_ROUNDS` default `6` → `10`. |
| `agent/planner.py` | Heuristic planner escalates restart → rollback on a retry. |
| `demo_target/faults/injectors.py` | Fault state persisted to a file; `RESTART_CLEARED_FAULTS`; memory-leak ceiling. |
| `.env` | `SUPERVISOR_MAX_ROUNDS=6` → `10`. **This is your local config file — I edited it.** |
| `tests/test_fixes.py` | **New file.** 9 regression tests for the fixes above. |
| `chaos/scenarios/*.json` | Each scenario now carries its own `incident` payload. |
| `tests/expectations.py` | **New file.** The 24 `expect:` tags as real assertions. |
| `tests/harness.py` | **New file.** Offline and live scenario runners. |
| `tests/run_catalogue.py` | **New file.** Runs the 16-test catalogue and reports PASS/FAIL. |
| `tests/run_scenario_check.py` | Rewritten to use the scenario's own payload. |
| `tests/scenarios/catalogue.yaml` | Added `approval: reject` to T15; documented the format. |
| `requirements.txt` | Added `PyYAML` (the catalogue parser needs it). |

Roughly 215 lines changed across 9 source files for the four fixes, plus the
test catalogue work described in section 10.

---

## 8. How to run the tests

There are now three suites. Run them in this order:

```bash
python -m tests.run_smoke
```

```bash
python -m tests.test_fixes
```

```bash
python -m tests.run_catalogue
```

The first two are unit-level; the third runs the 16 behavioral scenarios and is
described in section 10. Expected output from `test_fixes`:

```
OK verification honest under DRY_RUN
OK verification confirms a real recovery
OK verification refuses to guess without signal
OK verification ignores stale successes
OK planner escalates restart -> rollback on retry
OK planner refuses a blind rollback with no deploy evidence
OK faults are restart-proof, restart-curable ones still clear
OK second remediation attempt runs and resolves [['run_healthcheck', 'rollback_deploy'], ['run_healthcheck', 'rollback_deploy']]
OK repeated failure escalates via verification_exhausted

All fix regression tests passed.
```

### How these tests work

They need **no Docker, no database, and no LLM calls** — so they run in a couple
of seconds and cost nothing.

The two graph tests are the interesting ones. They compile the **real**
`build_graph()` and run a real incident through it, with only the outside world
stubbed out: an in-memory checkpointer instead of Postgres, canned specialist
hypotheses instead of LLM agents, and a scripted verification result. That means
they exercise the actual supervisor routing logic — the thing that was broken —
rather than a mock of it.

Two things to know if you edit the file:

- **The graph tests run last**, and `main()` orders them that way deliberately.
  They patch module-level functions on `agent.graph` that are not all restored
  afterwards.
- **Import order matters** in `_build_offline_graph()`: `agent.graph` must be
  imported *before* the LLMs are disabled, because the specialist modules build
  their `ChatOpenAI` object at import time. That is known issue #1 below. There
  is a comment in the file explaining it.

---

## 9. Important: what this changes about your demo

**This is the practical consequence you most need to know.**

Verification is now honest. In `DRY_RUN=true` mode, remediation actions are only
*simulated* — nothing actually changes on the service. So:

1. The agent "restarts" the service (simulated — the fault is still there).
2. Verification probes `/pay` → the requests still fail.
3. Verification correctly reports **not recovered**.
4. The agent re-plans and tries a rollback (also simulated — still no change).
5. Verification fails again → the incident **escalates**.

That is the correct, honest behaviour. But it means **your demo will now end in
`escalated` rather than `resolved` while `DRY_RUN=true`.**

To show the full happy path — restart fails, rollback succeeds, incident resolved
— you need real actions:

```bash
DRY_RUN=false
```

With `DRY_RUN=false` and the demo containers running, `rollback_deploy` genuinely
calls `DELETE /fault` on the service, the fault clears, the probe requests
succeed, and the incident resolves for a real reason.

**Recommendation:** run the demo with `DRY_RUN=false` against the demo containers.
The Docker safety rails (the container allow-list, the required
`incident-demo=true` label, and the image-tag allow-list) are still enforced, and
those were verified as working correctly — the agent cannot touch anything
outside the two demo containers.

---

## 10. The behavioral test catalogue

`tests/scenarios/catalogue.yaml` has always listed 16 behavioral tests, but the
`expect:` entries were free text that nothing read. And `run_scenario_check.py`
sent the *same* bad-deploy payload no matter which scenario you asked for, so six
of the eight scenarios could not be exercised faithfully. Both are now fixed.

### What changed

**1. Every scenario carries its own incident.** Each file in `chaos/scenarios/`
gained an `incident` block — the payload that gets reported to the agent:

```json
{
  "id": "scenario_04_db_pool",
  "expected_root_cause": "db connection pool exhaustion",
  "incident": {
    "source": "chaos",
    "service": "payments-api",
    "kind": "error",
    "title": "payments-api ConnectionPoolTimeout - pool at 100% capacity",
    "stacktrace": "TimeoutError: ConnectionPoolTimeout: pool at 100% capacity (in_use=20)"
  }
}
```

The detail that matters most: **the pool scenario has no `deploy_tag`.** That is
what makes the "do not roll back when there was no deploy" test meaningful. The
old hardcoded payload always sent `deploy_tag: v1.8`, which steered every
scenario toward `bad_deployment`.

**2. Every `expect:` tag is now a Python function.** `tests/expectations.py`
holds one predicate per tag. For example:

```python
@expectation(
    "no_blind_rollback_without_deploy",
    "No rollback is proposed when the incident carries no deploy evidence",
)
def _no_blind_rollback(o: Outcome) -> tuple[bool, str]:
    if o.has_deploy_evidence():
        return True, "deploy evidence present, rollback would be legitimate"
    planned = o.all_planned_actions()
    rolled = "rollback_deploy" in planned
    return not rolled, f"deploy_tag=None planned={planned}"
```

Each returns `(passed, detail)`, and the detail is printed either way — so a
failure tells you *why*, not just that it failed. If the catalogue ever names a
tag with no matching function, the runner prints a warning instead of silently
passing.

**3. A runner that executes the whole catalogue.**

```bash
python -m tests.run_catalogue
```

```bash
python -m tests.run_catalogue --test T05
```

```bash
python -m tests.run_catalogue --level 6 --verbose
```

```bash
python -m tests.run_catalogue --live
```

### Two modes, and why both exist

**Offline (the default)** needs nothing running — no Docker, no Postgres, no
OpenAI key, no cost. It compiles the **real** graph and runs a real incident
through it. What is simulated is only the outside world:

| Real | Simulated |
|------|-----------|
| supervisor routing, synthesis, planner, critic | the four LLM specialists (canned hypotheses per fault) |
| execution node, HITL interrupts, RBAC, risk tiers | the Docker tools |
| verification node and its decision logic | the demo services (a `FakeTarget` with the same cure rules) |
| retrieval node | pgvector (keyword search over `knowledge/`) |

So offline mode genuinely tests **orchestration and safety behaviour**. It does
not test whether the LLM specialists reason correctly — that is what live mode is
for. Expectations that can only be judged live (currently
`retrieval_finds_historical_rca`, which needs real pgvector) are marked
`live_only` and reported as **SKIP** offline, so an offline run never shows a red
result for something it could not have tested.

**Live** injects the real fault, drives real traffic, posts the incident to the
ingestion API, answers the approval prompt, and reads the finished incident back.
This is the one to show your mentor for the real thing — see the `DRY_RUN` note in
section 9.

### Current result

```
==============================================================================
BEHAVIORAL CATALOGUE  -  16 test(s)  -  mode: offline
DRY_RUN=True  supervisor_max_rounds=10
==============================================================================

T06  [level 2]  Dependency failure
    scenario   : scenario_05_dependency_failure  (approval: yes)
    root_cause : dependency_failure (confidence 0.85)
    plan       : [run_healthcheck, restart_service]
    executed   : [run_healthcheck:success, restart_service:dry_run, ...]
    verified   : recovered=False retries=2
    status     : escalated
      [PASS] dependency_analyst_signal
             dependency signals=[dependency, upstream, unreachable, payments-api]
      [PASS] remediate_dependency_first
             reported_by=orders-api first_remediation_target=payments-api

...

  16/16 catalogue tests passed
==============================================================================
```

### It immediately found a bug

The first full run came back **13/16**, and all three failures were real:

- **T04 and T12** — the restart-to-rollback escalation I added in section 6a was
  firing on incidents with no deploy behind them. A DB-pool incident was
  escalating to a rollback: a textbook blind rollback. Fixed by the
  `has_deploy_evidence` guard.
- **T06** — the same rule was overriding the dependency plan, so remediation
  targeted `orders-api` (the service that *reported* the problem) instead of
  `payments-api` (the upstream that was actually broken).

That is the whole point of making the catalogue executable: it caught a real
safety regression that reading the code did not.

### One thing to know for the demo

Two assertions read the plan, but the supervisor **clears** `plan` when it loops
back to re-plan (see section 3), so the final state only ever shows the last one.
The planner now records its full step list in the audit trail, and the
expectations reconstruct plan history from there — `first_plan_steps()` for
"what did it try first" and `all_plan_steps()` for "did it ever propose X".

---

## 11. Known issues that are still open

These were all confirmed as real, but **nothing was changed for them**. They are
listed roughly in order of how much they would hurt.

### 1. The app cannot start without an `OPENAI_API_KEY`

Each specialist module builds its LLM client at *import* time:

```python
# agent/specialists/log_analyst.py
_graph = build_specialist_graph("LogAnalyst", SYSTEM, sentry_tools)
```

That calls `get_fast_llm()` → `ChatOpenAI(...)`, which raises immediately if no
key is present. So `uvicorn ingestion.app:app` dies before serving anything.

It works on your machine because your `.env` has a key. It would fail on a fresh
clone. **Fix:** build the LLM lazily inside the node functions instead of at
module level. This would also make the deterministic fallbacks testable.

### 2. `/approve` has no authentication

`ingestion/app.py` takes the approver's role from the **request body**:

```python
class ApproveBody(BaseModel):
    ...
    role: str = "oncall"
```

The T3 role check in `agent/execution.py` (`role not in settings.t3_approver_roles`)
is therefore bypassed by anyone who types `"role": "sre-lead"`. Note that
`Header` is imported in `ingestion/app.py` and never used, which suggests an
API-key check was planned and not finished.

### 3. Webhooks run the whole investigation synchronously

`/webhook/{source}` and `/incidents` call `incident_graph.invoke(...)` inside the
HTTP request. A full investigation will outlast Sentry's and Alertmanager's
webhook timeouts, and they will retry — creating duplicate incidents. **Fix:**
return immediately with a `BackgroundTask`, and let the caller poll
`/incidents/{id}`.

### 4. Three database tables are never written

`db/schema.sql` defines `actions`, `approvals`, and `deployments`, and the
blueprint promises an immutable audit trail. The only `INSERT` statements in the
entire codebase are for `incidents` and `audit_logs` (both in `ingestion/dedup.py`).

Also, `upsert_incident`'s `ON CONFLICT` clause only updates `status` and
`updated_at`, so `root_cause`, `confidence`, and `rca_report_md` stay `NULL` in
Postgres forever. The UI works anyway because it reads through the LangGraph
checkpointer, so this does not break the demo — but the persistence story is
thinner than the blueprint describes.

### 5. Approval can cause a step to run twice

In `agent/execution.py`, `interrupt()` is called *inside* the `for step in steps`
loop. LangGraph re-runs a node from the top when it resumes, so any step that
executed before the pause runs a second time. Harmless for the current heuristic
plans (they put a read-only `run_healthcheck` first), but an LLM-generated plan
could put a restart before a rollback.

### 6. The UI blanks two tabs right after you approve

`/approve` returns `_public_result(...)`, which does not include
`executed_actions` or `audit_log`. The Streamlit app overwrites its cached detail
with that response (`ui/streamlit_app.py`, in the approve handler), so those two
tabs go empty. **Fix:** re-fetch `/incidents/{id}` after approving.

### 7. Some code paths still have no coverage

The catalogue now covers the rejected-action path (T15), escalation (T16), the
HITL gate (T14), and low-risk auto-execution (T13). Still uncovered:

- The duplicate fast-path out of `triage` (an entire conditional edge). Note
  `normalize_manual` mixes the current timestamp into the fingerprint, so two
  identical manual reports never match — this path cannot be reached from a
  manual or chaos incident at all, only from Sentry or Alertmanager.
- T3 RBAC denial — the heuristic planner never emits a T3 step, so it is only
  reachable via the LLM
- The escalation acknowledgement
- The "no source — human review required" grounding guard
- The prompt-injection defence the specialist prompts advertise
- A healthy-system negative control (nothing checks that the agent does *not*
  invent a root cause when there is no fault)

### 8. Unused dependencies

Nothing in your source imports these: `langchain-postgres`, `sqlalchemy`,
`psycopg2-binary`, `tenacity`, `pandas`, `numpy`. Safe to drop from
`requirements.txt`.

---

## What was verified as already correct

Worth knowing what *did* hold up, so you know where not to spend time:

- All eight scenario files reference fault modes that actually exist in the
  injector, and there are no orphaned scenario files.
- The Docker allow-list and the `incident-demo=true` label gate correctly deny a
  non-demo container.
- The rollback tag allow-list correctly rejects unknown image tags.
- The pgvector SQL parameter ordering in `similarity_search` is correct.
- The happy path runs end to end, through the human-approval pause, to a
  downloadable RCA report.
- Redaction of emails and API keys works.
- Every loop cap is present and enforced.
