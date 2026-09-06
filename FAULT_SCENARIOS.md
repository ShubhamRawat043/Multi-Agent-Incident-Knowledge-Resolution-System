# Fault Scenarios — What We Break, How We Break It, and How the System Handles It

This document explains the "bad scenarios" (faults / chaos experiments) that this
project injects into its demo services, why each one exists, exactly how it is
implemented in code, and what the agent system is expected to do about it.

It is written to be read top-to-bottom by someone who has not seen the code.

---

## 1. The big picture

### 1.1 Why inject faults at all?

The whole point of this project is an **agentic incident-response system**: when a
production service breaks, a set of LLM agents investigate it, figure out the root
cause, propose a fix, get human approval for risky actions, execute the fix, and
verify whether it worked.

To build or demo that, you need incidents. Real production outages are rare,
unpredictable, and you obviously cannot cause one on purpose. So we run two
**fake microservices** that we fully control, and we deliberately break them in
specific, repeatable ways. This practice is called **fault injection** or **chaos
engineering** — the same idea behind Netflix's Chaos Monkey.

Deliberate, controlled breakage gives us three things that are impossible otherwise:

1. **Repeatability** — the same fault every run, so a change in agent behaviour is
   attributable to the agent, not to a different incident.
2. **A known correct answer** — we injected `db_pool_exhaustion`, so we *know* the
   root cause. That lets us grade the agent instead of just admiring its output.
3. **Coverage of hard cases** — including cases designed to *fool* the agent
   (red herrings, conflicting evidence), which never appear on demand in real life.

### 1.2 The demo environment being broken

Two small FastAPI services run in Docker, deliberately arranged in a dependency chain:

```
   client traffic
        |
        v
  orders-api  (port 8001)  ──calls──>  payments-api  (port 8002)
```

- **[`demo_target/orders_api/main.py`](demo_target/orders_api/main.py)** — accepts
  `POST /orders`, then calls payments-api to pay for the order.
- **[`demo_target/payments_api/main.py`](demo_target/payments_api/main.py)** —
  accepts `POST /pay`.

That upstream/downstream relationship is not decoration. It is what makes
**cascading failure** possible: break payments-api and orders-api starts returning
503s even though there is nothing wrong with orders-api itself. That is one of the
hardest real-world diagnostic problems (the service that *reports* the error is not
the service that *has* the problem), and it is scenario 05.

Both services also expose observability surfaces that the agents read:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness. Used by `run_healthcheck` and by verification. |
| `GET /metrics` | Prometheus metrics (error rate, latency histogram, DB connections, memory). |
| `GET /info` | Current image tag, active fault mode, leaked-connection count. |
| `POST /fault` | **Injects** a fault (the chaos control plane). |
| `DELETE /fault` | **Clears** all faults and resets metrics. |

The `/fault` endpoints are the switchboard. Everything in this document ultimately
comes down to: something POSTs a fault mode to `/fault`, then traffic is sent.

### 1.3 The injection mechanism

All faults live in one file: **[`demo_target/faults/injectors.py`](demo_target/faults/injectors.py)**.

The design is a single mutable "fault mode" string plus one function, `apply_fault()`,
which every business endpoint calls at the top of the request:

```python
@app.post("/pay")
def pay(body: PayRequest) -> dict:
    fault = apply_fault(SERVICE)      # <-- injection point
    ...
    if fault is not None:
        ERROR_RATE.labels(service=SERVICE).set(0.85)
        raise HTTPException(status_code=500, detail=str(fault))
    ...                                # normal happy path
```

`apply_fault()` returns either:
- `None` → request proceeds normally, **or**
- an `Exception` object → the endpoint raises HTTP 500 with that exception's message.

Some faults also cause **side effects** before returning (sleeping to create latency,
allocating memory, appending to a leaked-connection list).

Two design details are worth pointing out to your mentor, because they were
deliberate and they matter:

**(a) Fault state is persisted to disk, not just held in memory.**

```python
_STATE_PATH = Path(os.getenv("FAULT_STATE_PATH", ... "incident_fault_mode"))
```

If the fault mode lived only in a Python variable, then *every* fault would vanish
the moment the container restarted. And since "restart the service" is the agent's
most common first remediation, that would mean **every fault would look
restart-curable** — the system would appear to fix everything, and the demo would
be meaningless. Persisting to a file (which survives `docker restart`, because the
container's writable layer is preserved) keeps "restart didn't help" a reachable,
honest outcome.

**(b) But two faults are *deliberately* restart-curable.**

```python
RESTART_CLEARED_FAULTS = {"db_pool_exhaustion", "memory_leak"}
```

On startup, if the persisted fault is one of these, it is reset to `none`. This is
not a cheat — it is **physically accurate**. Both of those faults do their damage by
leaking *in-process* resources (open connections, allocated bytes). A fresh process
genuinely does not hold them any more. Meanwhile a bad deploy or a bad config
survives a restart, because the broken release/config is still the one running.

This split is the single most important thing to understand when reading results:

| Fault type | Restart cures it? | Correct final outcome |
|---|---|---|
| `db_pool_exhaustion`, `memory_leak` | **Yes** | `resolved` |
| `exception`, `bad_deploy`, `config_error`, `latency`, `dependency_failure` | **No** | `escalated` (or resolved only via rollback) |

So when a scenario ends in `escalated`, that is often the **correct** answer, not a
failure of the agent. An agent that "resolved" a bad deploy by restarting it would
be lying.

### 1.4 How a scenario is driven

Scenarios are declarative JSON files in
[`chaos/scenarios/`](chaos/scenarios/), executed by
[`chaos/controller.py`](chaos/controller.py). A scenario file has four parts:

```jsonc
{
  "id": "scenario_04_db_pool",
  "expected_root_cause": "db connection pool exhaustion",  // the grading key
  "incident":  { ... },     // the alert payload we file with the agent system
  "targets":   [ { "service_url": "http://localhost:8002", "fault": "db_pool_exhaustion" } ],
  "traffic":   { "url": "...", "method": "POST", "json": {}, "count": 6 },
  "settle_seconds": 1
}
```

`apply_scenario()` then does, in order:

1. **Inject** — POST `{"mode": <fault>}` to each target's `/fault`.
2. **Settle** — sleep `settle_seconds` so metrics have a moment to move.
3. **Generate traffic** — fire `count` real HTTP requests at `orders-api`. This is
   what actually *triggers* the fault: `apply_fault()` only runs inside a request.
   No traffic, no errors, no metrics, no evidence for the agents to find.

The `incident` block is the alert that gets filed with the agent system — the
stacktrace, service name, and (where relevant) deploy tag that a real
Sentry/PagerDuty alert would carry. **[`tests/run_scenario_check.py`](tests/run_scenario_check.py)**
ties it all together end to end: inject → file the incident → auto-answer approval
prompts → print the result against `expected_root_cause`.

```bash
python -m tests.run_scenario_check --scenario scenario_04_db_pool --reset
```

`--reset` clears the faults afterwards so the next run starts clean.

---

## 2. The eight scenarios

Difficulty **level** below is the scenario's own `level` field: 1 = single obvious
signal, 2 = requires correlation, 3 = actively adversarial.

### Scenario 01 — Application exception (`exception`) · level 1

**What it is generally.** The simplest possible production failure: a code path
throws an unhandled exception and the request 500s. In real life this is a null
dereference, a bad cast, an unhandled edge case — the bread and butter of Sentry.

**How we inject it.**
```python
if mode == "exception":
    return RuntimeError(f"[{service}] Injected application exception")
```
Every `/pay` call raises. The incident filed carries the matching stacktrace, so
`log_analyst` has something real to reason over.

**How it is handled.** Root cause classifies as an application exception. It matches
no `deploy`/`pool`/`dependency` branch in
[`agent/planner.py`](agent/planner.py), so the plan falls through to the default:
`restart_service` → `run_healthcheck`. The restart does **not** cure it (the fault
persists to disk), verification's probe traffic still 500s, `recovered=False`, the
supervisor loops, and eventually **escalates to a human** — which is right. A
plain code bug is not something an autonomous agent should claim to have fixed.

**Why this scenario exists.** It is the baseline sanity check, and it is the clean
demonstration that the system knows when to *give up and ask a human*.

---

### Scenario 02 — Bad deployment (`bad_deploy`) · level 2

**What it is generally.** By a wide margin the most common cause of real outages:
the code was fine, someone shipped a release, and now it isn't. The signature is
temporal — errors start at the deploy timestamp.

**How we inject it.**
```python
if mode == "bad_deploy":
    return TypeError(f"[{service}] TypeError in payment processing: "
                     "unexpected NoneType for amount (introduced in bad deploy)")
```
Crucially, the scenario's incident block also carries deployment metadata:
```json
"deploy_tag": "v1.8", "deploy_sha": "9f3c2ab"
```
That is the correlation evidence the `change_correlator` specialist keys off.

**How it is handled.** Root cause category contains `"deploy"`, so the planner takes
the deploy branch: `run_healthcheck` (confirm unhealthy) → **`rollback_deploy` to
`v1.7`**. Rollback is risk tier **T2**, so `requires_approval=True` — the graph
*pauses on a human-in-the-loop interrupt* and waits for a yes/no.

`rollback_deploy` in [`tools/docker_tools.py`](tools/docker_tools.py) is a
**simulated** rollback: rather than rebuilding a Docker image, it calls
`DELETE /fault` on the service, which clears the fault. That is a faithful
simulation — reverting to the previous release genuinely does remove the bad code's
behaviour. It is also guarded: the tag must be in `ALLOWED_ROLLBACK_TAGS`, and the
container must be on the allow-list *and* carry the `incident-demo=true` label.

Verified result: `root_cause: bad_deployment`, rollback approved and applied,
`recovered=True`, `status: resolved`.

**Why this scenario exists.** It is the flagship happy-path demo: correct diagnosis,
a T2 action correctly gated behind human approval, a real fix, and verified recovery.

---

### Scenario 03 — High latency (`latency`) · level 1

**What it is generally.** Nothing errors. Everything is slow. This is a *brownout*,
and it is nastier than an outage because error-rate dashboards stay green while
users are timing out. You detect it via p95/p99 latency against an SLO.

**How we inject it.**
```python
if mode == "latency":
    time.sleep(float(os.getenv("FAULT_LATENCY_SECONDS", "2.5")))
    return None          # <-- note: no exception
```
Returning `None` is the whole point — the request **succeeds**, just 2.5 seconds
late. The `REQUEST_LATENCY` Prometheus histogram records it.

The incident payload is therefore `"kind": "metric"` rather than `"kind": "error"`,
carrying a metric series (`p95_before: 0.12` → `p95_now: 2.54`) instead of a
stacktrace. There is nothing for a log analyst to find.

**How it is handled.** The evidence lives in metrics, so `metrics_analyst` must carry
the diagnosis while `log_analyst` correctly finds nothing. Latency has no dedicated
planner branch, so it takes the default restart path, does not recover, and escalates.

**Why this scenario exists.** It proves the system is not merely a stacktrace parser.
It also exercises the multi-agent design: different specialists genuinely see
different things, and low-signal agents must not be allowed to drown out the one
that actually has evidence.

---

### Scenario 04 — DB connection pool exhaustion (`db_pool_exhaustion`) · level 2

**What it is generally.** An app holds a fixed pool of database connections. If code
leaks them (fails to return one to the pool), the pool fills, and every later request
blocks then times out waiting for a free connection. The classic tell: the database
itself is perfectly healthy, but the app cannot talk to it.

**How we inject it.**
```python
if mode == "db_pool_exhaustion":
    for _ in range(20):
        _leaked_connections.append(object())      # leak 20 "connections" per request
    return TimeoutError(f"[{service}] ConnectionPoolTimeout: pool at 100% capacity "
                        f"(in_use={len(_leaked_connections)})")
```
The leak is real state: `_leaked_connections` grows, and the service publishes it as
the `db_connections_in_use` Prometheus gauge and in `/info`. So the agent can observe
a monotonically climbing connection count — the actual diagnostic signature.

Note the scenario deliberately files **no deploy tag**. This is the control case
against scenario 02: if the agent blames a deploy here, it is pattern-matching rather
than reasoning.

**How it is handled.** Category contains `pool`/`db`, so the planner picks
`restart_service` → `run_healthcheck`, citing
`knowledge/sops/database_connection_pool.md`. Because `db_pool_exhaustion` is in
`RESTART_CLEARED_FAULTS`, the fresh process starts with an empty list — the fault is
genuinely gone. Verification's probe traffic succeeds, `recovered=True`,
**`status: resolved`**.

**Why this scenario exists.** This is the "the agent actually fixed it, autonomously,
with a T1 action" story — and it is honest, because the restart really did clear the
leaked resources.

---

### Scenario 05 — Dependency failure / cascading outage (`dependency_failure`) · level 2

**What it is generally.** Service A depends on service B. B dies. A starts throwing
503s. The alert pages the team that owns **A**, but the bug is in **B**. Distinguishing
"I am broken" from "my dependency is broken" is one of the genuinely hard problems in
distributed systems, and getting it wrong means restarting a healthy service while the
real outage continues.

**How we inject it.** Note the asymmetry — the fault goes on **payments-api**:
```json
"targets": [ { "service_url": "http://localhost:8002", "fault": "dependency_failure" } ]
```
but the incident is filed against **orders-api**:
```json
"incident": { "service": "orders-api", "title": "orders-api 503s - upstream payments-api unreachable" }
```
The cascade is real, not scripted. `orders_api/main.py` calls payments-api, sees a
5xx or a connection error, and raises its own 502/503:
```python
except httpx.RequestError as exc:
    ERROR_RATE.labels(service=SERVICE).set(0.9)
    raise HTTPException(status_code=503, detail=f"payments-api unreachable: {exc}")
```

**How it is handled.** The `dependency_analyst` specialist exists for exactly this
case. When the root cause category contains `dependency`, the planner **redirects the
remediation to the upstream service, not the reporting one**:
```python
elif "dependency" in category:
    steps = [ run_healthcheck(target="payments-api"),
              restart_service(target="payments-api") ]   # hard-coded to the dependency
```
That target switch is the interesting bit to point at in a demo.

**Why this scenario exists.** It is the strongest evidence that the system reasons
about the *system topology* rather than about whichever service happened to page.

---

### Scenario 06 — Memory leak (`memory_leak`) · level 3

**What it is generally.** Memory grows without plateauing. Nothing fails at first;
then GC pressure slows everything down; eventually the OOM killer takes the process.
Diagnosis is a *trend over time*, not a single data point, and the classic mistake is
misreading the slowdown as a latency or CPU problem.

**How we inject it.**
```python
if mode == "memory_leak":
    if len(_memory_ballast) < MEMORY_BALLAST_MAX_CHUNKS:
        _memory_ballast.append(bytearray(5 * 1024 * 1024))   # +5 MB per request
    return None      # <-- again, no exception
```
Requests keep succeeding, so error rate stays flat. The only signal is the
`process_memory_bytes_sim` gauge climbing. The ceiling (`MEMORY_BALLAST_MAX_CHUNKS`,
default 100 → ~500 MB) is a safety rail so a demo cannot OOM your laptop.

The incident is `"kind": "metric"` with `start_bytes: 50 MB → now_bytes: 355 MB` over
30 minutes. The scenario file calls itself "false-lead capable" because the symptom
profile looks a lot like a CPU/latency problem.

**How it is handled.** Falls to the default `restart_service` → `run_healthcheck`
plan. `memory_leak` is in `RESTART_CLEARED_FAULTS`, the ballast dies with the process,
and the incident correctly resolves — **`status: resolved`**.

**Why this scenario exists.** It is the level-3 case where the correct answer is
"restart it, and *also* tell a human the leak will come back". A restart mitigates a
memory leak; it does not fix the underlying bug. That nuance belongs in the RCA report.

---

### Scenario 07 — Conflicting evidence (`bad_deploy` + symptoms) · level 3

**What it is generally.** The adversarial case. Multiple signals fire at once and
point in different directions: errors, a latency spike, a CPU spike, and a recent
deploy. Some of those are the **cause**; the rest are **symptoms of the cause**.
A junior responder chases the CPU graph. A good one notices everything started at
the deploy.

**How we inject it.** The injected fault is `bad_deploy` — same mechanism as scenario
02 — but the incident is framed to be misleading:
```json
"title": "payments-api errors and CPU/latency spike after v1.8 release",
"expected_root_cause": "bad deployment (CPU/latency are symptoms)"
```
That `expected_root_cause` string is the grading key, and it explicitly names the trap.

**How it is handled.** This is what the multi-agent architecture is *for*. The four
specialists ([`agent/specialists/`](agent/specialists/)) investigate independently
and post competing hypotheses to a shared blackboard:

| Specialist | Likely hypothesis here |
|---|---|
| `log_analyst` | TypeError in payment processing |
| `metrics_analyst` | latency / CPU elevated |
| `change_correlator` | v1.8 deployed immediately before onset |
| `dependency_analyst` | upstreams healthy |

[`agent/synthesis.py`](agent/synthesis.py) then has to **adjudicate**, not average:
recognise that the latency spike is downstream of the exception, and that the deploy
correlation is causal. If the system merely aggregated confidence scores, it would
land on the loudest symptom.

**Why this scenario exists.** It is the direct justification for having multiple
specialists plus a synthesis step, instead of one prompt with all the data in it.
If your mentor asks "why is this multi-agent and not just one big LLM call?", this
is the scenario to show them.

---

### Scenario 08 — Configuration error (`config_error`) · level 2

**What it is generally.** The code is correct; the environment is not. A wrong
`DATABASE_URL`, a missing API key, a stale secret, a bad env var promoted from
staging. It typically appears right after a deploy, which makes it look exactly like
a bad deploy — but rolling back the *code* does not fix a bad *config*.

**How we inject it.**
```python
if mode == "config_error":
    return ValueError(f"[{service}] Configuration mismatch: invalid DATABASE_URL / API key")
```
The incident carries `"deploy_tag": "v1.8"` — deliberately, to overlap with the
bad-deploy signature.

**How it is handled.** Ideally the root cause is `config_error`, not `bad_deployment`,
even though a deploy tag is present — the discriminator is the *exception type*
(`ValueError` about configuration vs `TypeError` in application logic). Because
`config_error` is not in `RESTART_CLEARED_FAULTS`, a restart cannot cure it, and if
the agent wrongly rolls back it will find the fault still present after verification.
That failed verification is itself the safety net: the supervisor sees
`recovered=False` and escalates rather than declaring victory.

**Why this scenario exists.** It is the near-miss twin of scenario 02, and it tests
whether classification is genuinely discriminating or just keying on "was there a
deploy tag".

---

## 3. Summary table

| # | Scenario | Fault mode | Service broken | Alert filed against | Signal type | Restart cures? | Expected outcome |
|---|---|---|---|---|---|---|---|
| 01 | Application exception | `exception` | payments-api | payments-api | stacktrace | No | escalated |
| 02 | Bad deployment | `bad_deploy` | payments-api | payments-api | stacktrace + deploy tag | No | resolved via T2 rollback |
| 03 | High latency | `latency` | payments-api | payments-api | metrics only | No | escalated |
| 04 | DB pool exhaustion | `db_pool_exhaustion` | payments-api | payments-api | stacktrace + gauge | **Yes** | resolved via T1 restart |
| 05 | Dependency failure | `dependency_failure` | payments-api | **orders-api** | cascading 503s | No | remediation retargeted upstream |
| 06 | Memory leak | `memory_leak` | payments-api | payments-api | metrics only | **Yes** | resolved via T1 restart |
| 07 | Conflicting evidence | `bad_deploy` | payments-api | payments-api | mixed / misleading | No | correct adjudication to deploy |
| 08 | Config error | `config_error` | payments-api | payments-api | stacktrace + deploy tag | No | escalated, not mis-rolled-back |

Two fault modes exist in `injectors.py` but are not currently wired to a scenario
file: `http_500` (an error storm) and `db_timeout` (slow-then-timeout). They are
available for ad-hoc injection via `POST /fault`.

---

## 4. How a fault flows through the system

Once injected, every scenario travels the same LangGraph pipeline
([`agent/graph.py`](agent/graph.py)):

```
START → triage → supervisor ⇄ { investigate → synthesis, retrieve, plan → critic }
                      ↓
                   execute → verify → supervisor
                      ↓
                 escalate → report → END
```

Node by node:

1. **triage** ([`agent/triage.py`](agent/triage.py)) — redacts secrets from the
   payload, fingerprints the incident, checks whether it duplicates an open incident,
   and looks for a matching known issue or historical RCA. A duplicate short-circuits
   the whole pipeline.
2. **investigate** — runs the four specialists, each a ReAct sub-graph with its own
   bounded read-only toolset (Sentry, Prometheus, GitHub/deploy history, service
   health). Each posts a hypothesis with a confidence score and cited evidence.
3. **synthesis** — adjudicates competing hypotheses into one root cause. This is
   where scenario 07 is won or lost.
4. **retrieve** — RAG over [`knowledge/`](knowledge/): runbooks, SOPs, and eight
   historical RCA documents. This is what makes remediation *grounded* — a plan step
   without a source citation is flagged `"no source — human review required"`.
5. **plan** ([`agent/planner.py`](agent/planner.py)) — produces risk-tiered steps.
   The LLM plans first; `_heuristic_plan()` is a deterministic fallback if that fails.
6. **critic** — reviews the plan before anything touches a container.
7. **execute** ([`agent/execution.py`](agent/execution.py)) — runs allow-listed tools
   only, pausing for human approval on anything T2+.
8. **verify** ([`agent/verification.py`](agent/verification.py)) — sends **real probe
   traffic** through `/pay` or `/orders`, then re-reads health and Prometheus.
9. **supervisor** — routes the whole loop; escalates on repeated verification failure,
   low confidence (`CONFIDENCE_INVESTIGATE` = 0.50, `CONFIDENCE_PROCEED` = 0.80), or
   round limits (`SUPERVISOR_MAX_ROUNDS` = 10, `INVESTIGATION_MAX_ROUNDS` = 3).
10. **report** — writes the RCA markdown and persists a learning for next time.

### 4.1 Safety rails worth showing your mentor

These are the parts that make it demo-able without being reckless:

- **Risk tiers.** `T0` read-only · `T1` reversible · `T2` production-impacting
  (approval required) · `T3` destructive (approval required *and* the approver's role
  must be in `T3_APPROVER_ROLES`).
- **Human-in-the-loop.** Execution calls LangGraph's `interrupt()` for T2+, which
  genuinely suspends the graph mid-run. State is checkpointed to Postgres, so a human
  can answer minutes later via `POST /approve` and the graph resumes exactly where it
  paused.
- **Container allow-list + required label.** `restart_service` and friends will only
  touch a container whose name is in `DEMO_CONTAINER_ALLOWLIST` *and* which carries
  the `incident-demo=true` label. Anything else raises `PermissionError`. There is
  deliberately **no generic shell/exec tool**.
- **Rollback tag allow-list.** `rollback_deploy` only accepts tags in
  `ALLOWED_ROLLBACK_TAGS`.
- **`DRY_RUN`.** Defaults to `True`. When on, mutating tools report what they *would*
  do and change nothing — and verification explicitly annotates that any apparent
  recovery is not attributable to the plan.
- **Redaction.** Emails, tokens and keys are scrubbed from incident text before it
  ever reaches an LLM.
- **No blind rollback.** The planner refuses to propose a rollback unless there is
  actual deploy evidence (`deploy_tag` / `deploy_sha`) behind the incident.

### 4.2 Verification: why it drives real traffic

This is a subtle point that reads well in a demo. `/health` is a static 200 — it would
report "ok" even with the fault fully active. And the Prometheus error-rate gauge only
moves when a request actually hits `/pay` or `/orders`. So a verification step that
only polled `/health` and read the gauge would be reading **stale state from the
incident itself** and would happily declare victory over an unfixed service.

Instead, `_probe()` fires four real business requests and computes an error rate from
what actually happened. Recovery requires `error_rate < 0.10` **and** a healthy
`/health`. That is the difference between checking and confirming.

---

## 5. Running the scenarios

List every scenario with its description:

```bash
python -m tests.run_scenario_check --list
```

Run one end to end (inject → file incident → auto-approve → report → clean up):

```bash
python -m tests.run_scenario_check --scenario scenario_02_bad_deploy --reset
```

Same, but reject the approval prompt — proves the HITL gate actually blocks:

```bash
python -m tests.run_scenario_check --scenario scenario_02_bad_deploy --approve no --reset
```

Inject a fault without filing an incident (manual poking):

```bash
python -m chaos.controller --scenario scenario_06_memory_leak
```

Clear all faults from both services:

```bash
python -c "from chaos.controller import reset_all; print(reset_all(['http://localhost:8001','http://localhost:8002']))"
```

Always reset between runs. A leftover fault from a previous scenario is the single
most common cause of a confusing result.

---

## 6. Anticipated questions

**"Are these real failures or are you just printing error messages?"**
Real, within the demo's scope. The latency fault really sleeps; the memory leak
really allocates megabytes; the pool exhaustion really accumulates objects and
publishes the count as a metric; the cascade really happens because orders-api really
calls payments-api over HTTP and really gets a 5xx. What is simulated is the
`rollback_deploy` tool — it clears the fault rather than rebuilding a Docker image,
because a real image swap needs a compose rebuild that would slow the demo down. The
observable effect is the same.

**"Why does scenario 01 end in `escalated`? Isn't that a failure?"**
No — it is the correct outcome. An application exception is a code bug. It is not
restart-curable, and there is no deploy to revert. The right behaviour for an
autonomous system is to investigate, produce a well-evidenced RCA, and hand off to a
human. A system that reported "resolved" there would be the broken one.

**"Why break payments-api in scenario 05 but file the alert against orders-api?"**
Because that is what actually happens in production: the alert fires on the service
that *observes* the failure, not the one that *causes* it. It tests whether the system
can trace a failure across a service boundary.

**"How do you know the agent is right and not just guessing?"**
Every scenario file carries `expected_root_cause`, and
[`tests/run_catalogue.py`](tests/run_catalogue.py) asserts behaviour rather than
eyeballing output. Because we chose the fault, we always have ground truth.

**"What stops it from restarting the wrong container in production?"**
The allow-list plus the required `incident-demo=true` label, checked in `_guard()`
before any mutating Docker call — and the absence of any generic shell tool, so the
agent's blast radius is limited to six specific, individually-guarded functions.
