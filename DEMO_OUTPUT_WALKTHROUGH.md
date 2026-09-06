# Reading the Demo Output — Line by Line

A complete walkthrough of what `python -m tests.run_scenario_check --scenario scenario_02_bad_deploy --reset`
prints, block by block: **who produced it, from which file, and what it means.**

This is the script for explaining the terminal to someone watching over your shoulder.

---

## 0. First, the one-sentence version

> "One command does four things: it breaks a service on purpose, files an incident
> about it, lets a team of AI agents diagnose and fix it — pausing to ask me for
> approval before it touches production — and then proves the fix actually worked by
> sending real traffic through the service again."

Everything below is that sentence expanded.

### The processes involved

The output looks like one program, but **four separate processes** are talking to
each other. Knowing which is which is the key to reading the log:

| # | Process | Where it runs | Role |
|---|---|---|---|
| 1 | `tests/run_scenario_check.py` | your terminal | the **driver** — the script you typed |
| 2 | `payments-api` / `orders-api` | Docker containers (8002 / 8001) | the **victim** services being broken |
| 3 | Ingestion API + LangGraph agents | uvicorn on port 8080 | the **brain** — where the AI actually runs |
| 4 | Postgres + Prometheus | Docker | **memory** and **metrics** |

Every JSON block below comes from one of these four. The driver prints them all to
one terminal, which is why it can look like a single program.

---

## 1. Block 1 — The injection banner

```
Injecting: scenario_02_bad_deploy - Bad payments-api deployment causing TypeError + error spike
```

**Who printed it:** [`tests/run_scenario_check.py`](tests/run_scenario_check.py).

**Where the text came from:** [`chaos/scenarios/scenario_02_bad_deploy.json`](chaos/scenarios/scenario_02_bad_deploy.json)
— specifically its `id` and `description` fields. Nothing has happened yet; this is
the driver announcing which scenario file it loaded.

**What to say:** *"This is me choosing which failure to simulate. It's a declarative
JSON file, so the same failure is reproducible on every run."*

---

## 2. Block 2 — The chaos controller result

```json
{
  "faults": [ { "url": "http://localhost:8002", "fault": "bad_deploy",
                "status": 200, "body": { "service": "payments-api", "fault_mode": "bad_deploy" } } ],
  "traffic": [ { "i": 0, "status": 502, "body": "..." }, ... ]
}
```

**Who printed it:** the driver, but the JSON is the **return value of
`apply_scenario()`** in [`chaos/controller.py`](chaos/controller.py).

This one block is two distinct steps, and it is worth separating them for your mentor.

### 2a. `"faults"` — arming the failure

```json
{ "url": "http://localhost:8002", "fault": "bad_deploy", "status": 200,
  "body": { "service": "payments-api", "fault_mode": "bad_deploy" } }
```

The controller sent `POST http://localhost:8002/fault` with body `{"mode": "bad_deploy"}`.

- `"status": 200` — the HTTP response code. **This is the chaos injection succeeding,
  not the incident.** A very easy thing to misread out loud.
- `"body"` — the echo back from the service. It comes from `inject_fault()` in
  [`demo_target/payments_api/main.py`](demo_target/payments_api/main.py), which calls
  `set_fault()` in [`demo_target/faults/injectors.py`](demo_target/faults/injectors.py).

payments-api is now **armed** but nothing has broken yet, because the fault only
fires inside a request.

### 2b. `"traffic"` — pulling the trigger

```json
{ "i": 0, "status": 502,
  "body": "{\"detail\":\"payments-api failure: {\\\"detail\\\":\\\"[payments-api] TypeError in payment processing: unexpected NoneType for amount (introduced in bad deploy)\\\"}\"}" }
```

The controller now fires 8 real `POST /orders` requests at **orders-api** (`count: 8`
in the scenario file). `i` is just the request index, 0 through 7.

The nested-quotes mess in `body` is actually the most interesting thing on screen,
because it is **an error crossing a service boundary**. Unwrapped, it is two layers:

```
orders-api says:      "payments-api failure: <...>"
payments-api said:    "[payments-api] TypeError in payment processing:
                       unexpected NoneType for amount (introduced in bad deploy)"
```

The chain of events, in code:

1. `create_order()` in [`demo_target/orders_api/main.py`](demo_target/orders_api/main.py)
   receives the order and calls `POST /pay` on payments-api.
2. `pay()` in payments-api calls `apply_fault(SERVICE)`, which returns a `TypeError`
   object (from the `bad_deploy` branch of `injectors.py`).
3. payments-api raises **HTTP 500** with that message, and sets its Prometheus
   `http_error_rate` gauge to `0.85`.
4. orders-api sees a 5xx from its dependency and raises its own **HTTP 502**,
   wrapping the downstream error text.

**Why `502` and not `500`?** Because 502 = "Bad Gateway" = *"the service I depend on
failed."* orders-api is correctly reporting that it is not the one at fault. That
distinction is real HTTP semantics, not decoration.

**What to say:** *"The failure is genuinely happening over the network between two
services. orders-api returns 502 rather than 500 because it's correctly saying the
problem is downstream, in payments-api."*

---

## 3. Block 3 — The incident report

```json
{
  "source": "chaos", "service": "payments-api", "kind": "error",
  "title": "TypeError spike in payments-api after v1.8 release",
  "stacktrace": "Traceback (most recent call last): ... TypeError: [payments-api] ...",
  "deploy_tag": "v1.8",
  "deploy_sha": "9f3c2ab"
}
```

**Where it comes from:** the `incident` block of the scenario JSON, printed by the
driver, then `POST`ed to `http://localhost:8080/incidents`.

**This is the most important conceptual boundary in the whole demo,** and it is the
one people miss:

> Up to here, we were breaking a service. From here on, we are **reporting an alert**
> to the AI system. The agents do **not** know what fault we injected. All they get is
> this payload — exactly what a real Sentry or PagerDuty alert would contain.

That is what makes the demo honest. If the agents could see `"fault": "bad_deploy"`,
the diagnosis would be cheating.

Field by field:

| Field | Meaning |
|---|---|
| `source: "chaos"` | which pipeline filed it (`manual`, `sentry`, `chaos`…) |
| `service` | the service the alert fired on |
| `kind: "error"` | exception-shaped alert (vs `"metric"` for latency/memory scenarios) |
| `title` | the human-readable alert headline |
| `stacktrace` | the raw evidence for the `LogAnalyst` specialist |
| `deploy_tag` / `deploy_sha` | **the correlation evidence** for the `ChangeCorrelator` |

Those last two are the crux of scenario 02. They are what let an agent say *"errors
began right after v1.8 shipped"* instead of just *"there is a TypeError."*

---

## 4. Block 4 — `status 200`

```
status 200
```

**Who printed it:** the driver. It is the HTTP status of `POST /incidents`, handled
by `create_incident()` in [`ingestion/app.py`](ingestion/app.py).

**Careful — this is not "the incident was resolved."** It only means *"the ingestion
API accepted my alert."*

But something very large happened behind that one line. `create_incident()` calls
`_invoke_new()`, which:

1. Generates the incident ID (`INC-2c58714729`).
2. `upsert_incident(...)` — writes a row to Postgres **before** any agent runs, so
   the incident is durable even if the graph crashes.
3. `append_audit(...)` — records the "received" audit entry.
4. `incident_graph.invoke(state, config)` — **runs the entire multi-agent graph
   synchronously.** Triage, four specialists, synthesis, RAG retrieval, planning,
   critic review — all of it happens inside this one blocking call.

So the several seconds of apparent silence before the next line is the actual AI work.
Worth saying out loud, because otherwise it looks like the program hung.

---

## 5. Block 5 — The human-in-the-loop interrupt

```
APPROVAL REQUESTED: Approve rollback_deploy on payments-api (risk T2)? yes/no/modify
  answering: yes
```

**This is the single best thing to demo.** Slow down here.

**Who produced the request:** `execution_node()` in
[`agent/execution.py`](agent/execution.py):

```python
if _needs_hitl(step):
    decision = interrupt({
        "type": "approval_request",
        "incident_id": state.get("incident_id"),
        "step": step,
        "message": f"Approve {action_type} on {step.get('target')} "
                   f"(risk {step.get('risk_tier')})? yes/no/modify",
    })
```

`_needs_hitl()` returns `True` because the planner marked the rollback step
`risk_tier: "T2"` with `requires_approval: True`.

**What `interrupt()` actually does.** This is not `input()`. LangGraph **genuinely
suspends the graph mid-execution**: it serialises the entire state to the Postgres
checkpointer and returns control to the caller. The HTTP request to `/incidents`
returns *right now*, carrying a `pending_interrupt` field, with the graph frozen
part-way through the execute node.

**How it resumes.** The driver sees `pending_interrupt` in the response and POSTs to
`/approve`. `approve()` in [`ingestion/app.py`](ingestion/app.py) does:

```python
result = incident_graph.invoke(Command(resume=resume_value), config=config)
```

`Command(resume=...)` reloads the checkpointed state and continues **from the exact
line inside `execution_node` that called `interrupt()`** — the return value of
`interrupt()` becomes your `{"decision": "yes", "approver": ..., "role": ...}` payload.

**Why this matters:** the pause is durable, not a blocked thread. A real on-call
engineer could approve this an hour later, from a phone, from the Streamlit UI, and
the graph would pick up exactly where it stopped.

**`answering: yes`** is just the driver auto-answering because of the `--approve`
flag (default `yes`). In a live demo, do this instead — it proves the gate is real:

```bash
python -m tests.run_scenario_check --scenario scenario_02_bad_deploy --approve no --reset
```

With `no`, the rollback comes back `status: cancelled`, the fault is never cleared,
verification reports `recovered=False`, and the incident escalates. **The AI does not
get to overrule the human.**

**The risk tiers**, defined in `_needs_hitl()` and the planner:

| Tier | Meaning | Approval |
|---|---|---|
| T0 | read-only (`run_healthcheck`) | none |
| T1 | reversible (`restart_service`) | none, unless severity is P1/P2 |
| T2 | production-impacting (`rollback_deploy`) | **required** |
| T3 | destructive | required **and** approver's role must be in `T3_APPROVER_ROLES` |

---

## 6. Block 6 — The final summary

```
======================================================================
incident   : INC-2c58714729
expected   : bad deployment
root_cause : bad_deployment - The v1.8 deployment (SHA 9f3c2ab) introduced an application
             regression that allowed a None payment amount to reach payment processing,
             triggering a TypeError storm in payments-api.
confidence : 0.99
plan       : ['run_healthcheck', 'rollback_deploy']
executed   : [('run_healthcheck', 'success'), ('rollback_deploy', 'success')]
verified   : recovered=True - health ok; probe: 0/4 requests failed (error_rate=0.00);
             prometheus error_rate=0.85
status     : resolved
======================================================================
```

**Who printed it:** the driver, in the last ~20 lines of
[`tests/run_scenario_check.py`](tests/run_scenario_check.py). **But the driver
computed none of it.** After the approval resolves, it makes one final call:

```python
detail = client.get(f"{base}/incidents/{incident_id}").json()
```

That hits `get_incident()` in [`ingestion/app.py`](ingestion/app.py), which reads the
**final checkpointed LangGraph state** out of Postgres:

```python
snap = incident_graph.get_state(config)
values = snap.values or {}
```

Every line of the summary is one key plucked out of that state dict. So the box is a
**report on the graph's memory**, not a computation. Here is the provenance of each line.

---

### `incident : INC-2c58714729`

Generated by `_invoke_new()` in `ingestion/app.py` (`f"INC-{uuid.uuid4().hex[:10]}"`).
It doubles as the **LangGraph `thread_id`**, which is why `/approve` and
`/incidents/{id}` can both find the right conversation state later.

### `expected : bad deployment`

**Not produced by the AI at all.** This is the `expected_root_cause` field read
straight from the scenario JSON — the **answer key**. It is printed next to the
agent's answer so you can grade the run at a glance.

*Say:* *"Because I chose the fault, I always have ground truth. That's what makes this
testable rather than a demo that just looks impressive."*

### `root_cause : bad_deployment - <summary>`

**Produced by:** `synthesis_node()` in [`agent/synthesis.py`](agent/synthesis.py).

The pipeline that leads here:

1. **`investigate`** runs four specialist agents in
   [`agent/specialists/`](agent/specialists/). Each is a small ReAct agent with its own
   bounded, **read-only** toolset:

   | Specialist | Tools it can call | What it looks for |
   |---|---|---|
   | `LogAnalyst` | Sentry issues/events | exception types, stack frames |
   | `MetricsAnalyst` | Prometheus queries | error rate, latency, memory, connections |
   | `ChangeCorrelator` | deploy/commit history | *"what shipped just before this started?"* |
   | `DependencyAnalyst` | service health endpoints | *"is an upstream the real problem?"* |

   Each posts a **hypothesis** — a statement, a confidence, and cited evidence — to a
   shared blackboard (`state["hypotheses"]`). They run independently and can disagree.

2. **`synthesis`** reads all of them and **adjudicates**. It is explicitly not an
   average: it decides which signals are causes and which are symptoms, and emits one
   `RootCause` with a `category`, a `summary`, and a `confidence`.

The `category` (`bad_deployment`) is the machine-readable part — the planner branches
on it. The summary is the human-readable justification.

**Read the summary text carefully — it is your quality check.** In this run it cites
the specific SHA (`9f3c2ab`) and describes a real causal mechanism, which means the
specialists genuinely ran and found evidence. Compare with a bad run:

> *"...the reported CPU/dependency/log hypotheses contain no usable evidence because
> all analyst calls failed."*

That sentence means the specialists all errored and synthesis was reasoning over
nothing. **If you ever see confidence around 0.84 with wording like that, the run is
not trustworthy** — check the `hypotheses` array before believing the answer. (This
exact failure was a real bug we found and fixed: the model was rejecting tool-calling
because of a `reasoning_effort` setting, which silently degraded every investigation.)

### `confidence : 0.99`

Set by synthesis, then adjusted by the supervisor and verification as the run
proceeds. It is a **control signal, not decoration** — the supervisor routes on it,
using thresholds from [`agent/config.py`](agent/config.py):

| Confidence | Supervisor behaviour |
|---|---|
| `< 0.50` (`CONFIDENCE_INVESTIGATE`) | investigate again, or escalate if the budget is spent |
| `0.50 – 0.80` | mid-band: an LLM call refines the routing decision |
| `≥ 0.80` (`CONFIDENCE_PROCEED`) | confident enough to retrieve knowledge and plan |

`verification_node` also **subtracts 0.15** whenever recovery fails, so repeated
failures naturally drag the run toward escalation instead of looping forever.

### `plan : ['run_healthcheck', 'rollback_deploy']`

**Produced by:** `planner_node()` in [`agent/planner.py`](agent/planner.py), then
reviewed by the **critic** node before anything executes.

The order is deliberate: **confirm the problem is real (T0, free), then fix it (T2,
gated).** The planner reached it because the root cause category contained `"deploy"`:

```python
elif "deploy" in category:
    steps = [ run_healthcheck(...),                      # T0, "Confirm unhealthy"
              rollback_deploy(..., image_tag="v1.7",     # T2
                              requires_approval=True) ]
```

Two design points worth mentioning:

- **Grounding.** Every step carries a `source_citation` pointing at a real document in
  [`knowledge/`](knowledge/) (here, `knowledge/sops/deployment_rollback.md`), retrieved
  by the RAG step. A step with no citation is stamped
  `"no source — human review required"` and the plan is marked `grounded: false`.
  The agent may not invent procedures.
- **No blind rollback.** The planner only proposes a rollback when there is actual
  deploy evidence (`deploy_tag` / `deploy_sha`) on the incident. Scenario 04 files no
  deploy tag precisely to prove this guard holds.

The summary prints only the `action_type` of each step; the full objects (target, risk
tier, args, expected outcome, rollback procedure, citation) are in the `plan` JSON
visible in the Streamlit UI.

### `executed : [('run_healthcheck', 'success'), ('rollback_deploy', 'success')]`

**Produced by:** `execution_node()` in [`agent/execution.py`](agent/execution.py) —
a list of `(action_type, status)` pairs, in execution order.

Possible `status` values, and what each proves:

| Status | Meaning |
|---|---|
| `success` | the tool ran and worked |
| `failed` | the tool ran and errored |
| `dry_run` | `DRY_RUN=true` — it reported what it *would* do and changed nothing |
| `denied` | blocked by a safety guard (not on the allow-list, missing label, bad tag, wrong T3 role) |
| `cancelled` | **a human said no** at the approval gate |

Note it maps 1:1 onto `plan` here. If the agent had needed a second attempt, you would
see four entries — the plan re-ran — which is exactly the shape of a scenario that does
not recover on the first try.

**What the two tools actually did**
([`tools/docker_tools.py`](tools/docker_tools.py)):

- **`run_healthcheck`** — a real `GET /health`. It retries up to 4 times with a 0.5s
  gap, because Docker reports a container "running" ~1–1.5s before uvicorn has bound
  its port; without the retry, a healthy service gets misreported as down. (Another
  real bug we found and fixed.)
- **`rollback_deploy`** — a **simulated** rollback: instead of rebuilding a Docker
  image (too slow for a demo), it calls `DELETE /fault` on payments-api, clearing the
  fault. That is a faithful simulation, because reverting to the previous release
  genuinely does remove the bad code's behaviour.

  Be upfront about this simulation with your mentor. Then point at the guards that are
  **not** simulated: the tag must be in `ALLOWED_ROLLBACK_TAGS`, the container must be
  in `DEMO_CONTAINER_ALLOWLIST` **and** carry the label `incident-demo=true`, and
  there is deliberately **no generic shell or exec tool anywhere in the system.** The
  agent's entire blast radius is six individually-guarded functions.

### `verified : recovered=True - health ok; probe: 0/4 requests failed (error_rate=0.00); prometheus error_rate=0.85`

**Produced by:** `verification_node()` in [`agent/verification.py`](agent/verification.py).
This is the line that separates *"the agent claims it fixed it"* from *"the fix is
proven."*

Three independent signals:

1. **`health ok`** — `GET /health` returned 200.
2. **`probe: 0/4 requests failed`** — `_probe()` sent **four real business requests**
   to `POST /pay` and none returned 5xx.
3. **`prometheus error_rate=0.85`** — the gauge scraped from Prometheus.

**Why probe traffic exists — this is the sharpest engineering point in the demo.**
`/health` is a static 200; it returns "ok" even with the fault fully active. And the
Prometheus gauge only moves when a request actually hits `/pay`. So a verifier that
only polled those two would be reading **stale state left over from the incident** and
would happily declare victory over a service that was still broken. Driving real
traffic is what turns checking into confirming.

**Why does Prometheus still say `0.85` when the probe says `0.00`?** Not a bug —
a **scrape lag**. [`observability/prometheus.yml`](observability/prometheus.yml) sets
`scrape_interval: 15s`, so Prometheus is still serving the value from before the
rollback. The probe measured the *present*; Prometheus is reporting up to 15 seconds
*ago*. This is exactly why the recovery decision is made on the probe:

```python
recovered = bool(probe_err < RECOVERED_ERROR_RATE and health.get("ok"))
```

`RECOVERED_ERROR_RATE` is `0.10`. Prometheus is only the fallback when probing is
unavailable. If your mentor spots that `0.85` and asks about it, you have a great
answer ready — and it shows you know which of your signals is authoritative.

The node also annotates honestly when it can't take credit: if `DRY_RUN` was on it
notes that recovery *"is not attributable to the plan"*, and if recovery happened with
no successful remediation it flags *"may be transient."*

### `status : resolved`

**Produced by:** `supervisor_node()` in [`agent/supervisor.py`](agent/supervisor.py),
via its status map:

```python
"close": "resolved" if (verification or {}).get("recovered") else state.get("status", "triage"),
```

**`resolved` is only reachable through a passing verification.** The supervisor cannot
close an incident just because the plan finished — `recovered` must be `True`. If it
is `False`, the supervisor re-investigates while budget remains
(`INVESTIGATION_MAX_ROUNDS = 3`, `SUPERVISOR_MAX_ROUNDS = 10`) and otherwise escalates.

**Say this explicitly, because it pre-empts the obvious skeptical question:**
*"The system can't mark something resolved on its own say-so. `resolved` requires
independent evidence that traffic is flowing again."*

---

## 7. Block 7 — The reset

```json
{ "reset": [ { "url": "http://localhost:8002", "status": 200,
               "body": { "service": "payments-api", "fault_mode": "none" } } ] }
```

From `reset_all()` in [`chaos/controller.py`](chaos/controller.py), triggered by
`--reset`. It calls `DELETE /fault` on each target: clears the fault mode, empties the
leaked-connection and memory-ballast lists, and resets the Prometheus gauges.

Always run with `--reset`. A leftover fault from a previous scenario is the number-one
cause of a confusing result in the next one.

---

## 8. The whole flow on one page

```
YOU:  python -m tests.run_scenario_check --scenario scenario_02_bad_deploy --reset
  |
  |-- 1. chaos/controller.py --> POST /fault {"mode":"bad_deploy"}      [Block 2a]
  |                              payments-api is now armed
  |
  |-- 2. chaos/controller.py --> 8x POST /orders                        [Block 2b]
  |         orders-api --> payments-api --> TypeError --> 500 --> 502
  |         Prometheus gauge http_error_rate = 0.85
  |
  |-- 3. POST :8080/incidents  {stacktrace, deploy_tag: v1.8}           [Blocks 3-4]
  |         ingestion/app.py: write Postgres row, then invoke the graph
  |         |
  |         |-- triage        dedup, redact, fingerprint
  |         |-- investigate   4 specialists --> competing hypotheses
  |         |-- synthesis     adjudicate --> root_cause: bad_deployment (0.99)
  |         |-- retrieve      RAG over knowledge/ --> deployment_rollback.md
  |         |-- plan          [run_healthcheck T0, rollback_deploy T2]
  |         |-- critic        review the plan
  |         |-- execute       T0 runs... T2 hits interrupt() --> GRAPH PAUSES
  |
  |-- 4. APPROVAL REQUESTED --> POST /approve {"decision":"yes"}        [Block 5]
  |         Command(resume=...) --> graph continues inside execute
  |         |-- execute       rollback_deploy --> DELETE /fault --> success
  |         |-- verify        4 real probe requests --> 0/4 failed --> recovered=True
  |         |-- supervisor    recovered --> close --> status = resolved
  |         |-- report        RCA markdown + persisted learning
  |
  |-- 5. GET /incidents/{id} --> read final checkpointed state          [Block 6]
  |         driver prints the summary box
  |
  |-- 6. DELETE /fault --> clean slate                                  [Block 7]
```

---

## 9. Likely mentor questions

**"Does the AI know which fault you injected?"**
No. The injection goes to the containers on port 8002. The agents receive only the
alert payload in Block 3 — a title, a stacktrace, and a deploy tag, exactly like a
real Sentry alert. The word `bad_deploy` never reaches them.

**"What's the difference between `status 200` in Block 4 and `status: resolved` at the end?"**
The first is an HTTP code meaning "your alert was accepted." The second is the
incident's lifecycle state after verification passed. Completely unrelated things that
happen to share the word "status."

**"Why is Prometheus showing 0.85 if it recovered?"**
15-second scrape interval — Prometheus is serving a pre-rollback sample. The recovery
decision is made on live probe traffic (0/4 failed), not on the lagging gauge.

**"What if the human says no?"**
Run it with `--approve no`. The rollback comes back `cancelled`, the fault is never
cleared, verification fails, and the incident escalates instead of resolving.

**"Is the rollback real?"**
The *decision, the approval gate, and the guards* are real. The rollback mechanism is
simulated — it clears the fault flag rather than rebuilding a Docker image, because a
compose rebuild would take too long for a demo. The observable effect is identical.

**"How do you know it isn't just pattern-matching the word 'deploy' in the title?"**
Scenario 04 files a pool-exhaustion incident with **no deploy tag at all**, and
scenario 08 files a *config* error that *does* carry a v1.8 tag. Those two are the
controls that separate reasoning from keyword matching.

**"What stops it restarting the wrong container?"**
`_guard()` in `tools/docker_tools.py` checks the container against
`DEMO_CONTAINER_ALLOWLIST` **and** requires the label `incident-demo=true`, or it
raises `PermissionError`. There is no generic shell tool, so the agent cannot reach
anything outside those six wrapped functions.
