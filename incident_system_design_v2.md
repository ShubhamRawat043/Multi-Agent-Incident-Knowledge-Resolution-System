# Enterprise Multi-Agent Incident & Knowledge Resolution System
### Blueprint v2 — Supervisor-Orchestrated, Event-Driven, Real Remediation

> **What changed from v1 and why.** v1 collapsed the system into a fixed 1→7 pipeline with a synthetic alert as input. That's a workflow, not an agentic system — and honestly less agentic than your blog writer. v2 fixes two things: (1) the system now reacts to **real events** from real monitored systems and takes **real** remediation actions, and (2) control flow is now **dynamically orchestrated by a supervisor agent** coordinating **autonomous specialist agents** that each run their own tool loops, debate, and reconcile competing hypotheses. New technologies are added deliberately and tiered so you can pick your ambition level.

---

## 0. The two things that make this "real" and "agentic"

| Complaint about v1 | Fix in v2 |
|---|---|
| Input is a fake invented alert | **Event-driven ingestion** — real webhooks from Sentry / Alertmanager / GitHub against a **real instrumented demo target** you can break on purpose |
| Remediation is simulated | **Real actions** via the Docker/Kubernetes API — actually restart a container, actually roll back an image tag |
| Fixed 1→7 pipeline, no orchestration | **Supervisor agent** owns a dynamic control loop; no fixed order |
| "Agents" were single LLM calls | Each specialist is a **ReAct subgraph** with its own tools, reasoning until confident |
| No genuine multi-agent behavior | **Parallel specialists on a shared blackboard** + an **adversarial critic** debate loop |

---

## 1. Real Event Ingestion (the biggest change)

### 1.1 The monitored target — a real system to watch and break
Stand up a tiny but real environment (all containerized):

- **1–2 demo microservices** (FastAPI is fine — you'd learn one framework that also powers the ingestion layer). Example: an `orders-api` that depends on a `payments-api` + Postgres.
- Instrument them with **Sentry** (captures real exceptions, stack traces, breadcrumbs) and **Prometheus** metrics (request latency, error rate, memory).
- A **chaos/fault-injection script** to produce *real* failures on demand: deploy a bad build, introduce a memory leak, make `payments-api` time out, spike 500s. These generate genuine Sentry issues and genuine Prometheus anomalies.

Now every incident your system handles is a real event with a real stack trace, real metrics, and a real recent deploy that (often) caused it. Nothing is invented.

### 1.2 The ingestion layer — webhooks, not polling
A small **FastAPI** service is the front door:

```
Sentry issue-alert  ─┐
Alertmanager alert  ─┼──► POST /webhook ──► normalize ──► dedup/correlate ──► enqueue (Redis) ──► worker invokes graph
GitHub deploy event ─┘                                                                    (thread_id = incident_id)
```

- **Webhooks** mean the system reacts to real events the instant they happen — the definition of event-driven, not a made-up trigger.
- **Normalization** maps every source into one `IncidentEvent` schema (Pydantic — your existing discipline).
- **Redis queue** decouples intake from processing so alert storms don't overwhelm the graph, and gives you retries + concurrency. (Optional in the entry tier — see §5.)
- Each incident runs on its own `thread_id`, so your `checkpointer` (now Postgres) lets an incident survive restarts and resume after approval.

### 1.3 What enters / what is produced
**In:** a real `IncidentEvent` `{ source, service, kind (error|metric|deploy), title, stacktrace?, metric_series?, deploy_sha?, first_seen, fingerprint }` + live read access to Sentry/Prometheus/GitHub + the knowledge corpus.
**Out:** real executed (approved) actions with results, an RCA report, an incident status, and a distilled learning written back to memory + the vector store.

---

## 2. Agentic Architecture — supervisor-orchestrated

There is **no fixed order**. A **Supervisor agent** runs a control loop and dynamically routes based on incident state and confidence. Specialists are subgraphs with real autonomy.

```mermaid
flowchart TD
    EVT([Real event webhook]) --> TRI[Triage & Correlation Agent]
    TRI -->|duplicate / known issue| FASTPATH[Attach to existing incident / apply known fix]
    TRI -->|new incident| SUP

    SUP{{SUPERVISOR / ORCHESTRATOR<br/>dynamic control loop}}

    SUP -->|dispatch investigation| INV[[Investigation phase — parallel Send]]
    subgraph INV_SPECIALISTS[Parallel ReAct specialist agents → blackboard]
      LOG[LogAnalyst agent]
      MET[MetricsAnalyst agent]
      CHG[ChangeCorrelator agent]
      DEP[DependencyAnalyst agent]
    end
    INV --> LOG & MET & CHG & DEP
    LOG & MET & CHG & DEP --> SYN[Synthesis / RCA Agent<br/>reconcile competing hypotheses + confidence]
    SYN --> SUP

    SUP -->|need knowledge| RET[Retrieval / Knowledge Agent — RAG]
    RET --> SUP

    SUP -->|plan a fix| PLAN[Remediation Planner Agent]
    PLAN --> CRIT[Safety Critic Agent<br/>blast radius, reversibility, evidence check]
    CRIT -->|reject → revise| PLAN
    CRIT -->|approved plan| SUP

    SUP -->|execute| EXE[Execution Agent — REAL actions]
    EXE -->|T2/T3 action| HITL[[HITL interrupt: approve / modify / reject]]
    HITL --> EXE
    EXE --> VER[Verification Agent<br/>real metrics recovered?]
    VER --> SUP

    SUP -->|resolved| REP[RCA Report + Learning write-back]
    SUP -->|stuck / low confidence| ESC[[HITL: escalate to on-call]]
    ESC --> REP
    REP --> DONE([Close])
```

**The supervisor decides, each loop, among:** investigate more · retrieve knowledge · plan a fix · execute · escalate · close — based on current hypotheses, their confidence, severity, and how many cycles have run. That dynamic decision-making is the orchestration v1 was missing.

---

## 3. The Agents

Each agent is a Pydantic-typed contributor to shared state. The **more complex agents are subgraphs with internal reason-act loops** — not single calls.

### 3.1 Triage & Correlation Agent
- **Real incident-management logic.** Before doing anything expensive: is this a **duplicate** of an active incident? Part of an **alert storm** that should collapse into one incident? A **known issue** matching a past resolved incident (semantic match against the vector store)?
- Uses embeddings to fingerprint and match. Known issues can fast-path to a previously successful fix (with HITL).
- **Built from:** your Long-term Memory `is_new`/dedup logic + your RAG similarity.
- *Why it raises the bar:* real systems drown in duplicate alerts; handling that is what separates a toy from a plausible platform.

### 3.2 Supervisor / Orchestrator Agent — the brain
- A genuine **control-loop agent**. Reads the blackboard + control state, emits a structured `NextAction` decision (`investigate | retrieve | plan | execute | escalate | close`) with a reason.
- Implemented as a node the graph **returns to repeatedly** (conditional edges route on its decision) — the supervisor pattern. It can dispatch investigation again if confidence is low (dynamic loop), skip straight to a known fix, or escalate early on a P1.
- **Built from:** your blog-writer router/orchestrator elevated into a loop, plus your iterative-workflow routing (`route_evaluation`).

### 3.3 Investigation phase — parallel specialist agents (blackboard)
Supervisor fans out via `Send` to specialists that run **in parallel**, each a **ReAct subgraph** with its own tools, looping until it has a hypothesis it's confident in:

- **LogAnalyst** — tools: query Sentry issue/stacktrace/breadcrumbs, search Loki/log store. Forms a `Hypothesis` from error patterns.
- **MetricsAnalyst** — tools: query Prometheus (latency, error rate, saturation), detect the anomaly window.
- **ChangeCorrelator** — tools: GitHub/GitLab recent commits & deploys in the incident window. *Most incidents are caused by a recent change* — this agent is often the one that nails root cause.
- **DependencyAnalyst** — tools: service dependency graph, upstream/downstream health. Distinguishes "we broke" from "our dependency broke."

Each writes a `Hypothesis {statement, evidence[], confidence, source_agent}` to a **blackboard** channel (`Annotated[list[Hypothesis], operator.add]`). Parallel agents contributing to one shared structure is a classic multi-agent pattern.
- **Built from:** your Parallel Workflow (fan-out/fan-in) + your chatbot ToolNode loop, now wrapped as autonomous subgraphs.

### 3.4 Synthesis / RCA Agent
- Reconciles **competing hypotheses** (they will disagree — MetricsAnalyst says "memory," ChangeCorrelator says "the 14:02 deploy"). Produces a ranked `RootCause` with a confidence score and the supporting evidence chain.
- If top confidence is below threshold → returns control to the Supervisor, which dispatches **more targeted investigation** (dynamic, confidence-driven). This loop is genuinely agentic.
- **Built from:** your Parallel Workflow's `final_evaluation` reducer + your iterative loop.

### 3.5 Retrieval / Knowledge Agent (RAG subgraph)
- Given the synthesized root cause, retrieves SOPs, runbooks, and **past RCA reports** with metadata filtering (service, doc_type).
- **Built from:** your chatbot RAG. Upgraded store: **pgvector** (see §5) so historical incidents and docs live together and support concurrent access. Same `as_retriever` interface you already use.
- **Grounding contract:** remediation may only cite retrieved sources; anything without a source is flagged "human review required" — your blog-writer "cite only these" rule, now a safety mechanism.

### 3.6 Remediation Planner + Safety Critic — the debate loop
- **Planner** proposes an ordered `RemediationPlan` grounded in retrieved knowledge; each step has `action_type`, `risk_tier`, `preconditions`, `expected_outcome`, `rollback`.
- **Safety Critic** *adversarially* reviews it: blast radius, reversibility, whether the evidence actually justifies the action, preconditions met, is there a safer step first. It can **reject and send back for revision** (bounded iterations).
- This planner↔critic debate is more agentic than a single recommendation step and is exactly the kind of self-check enterprises demand before touching production.
- **Built from:** your Iterative Workflow (generator ↔ evaluator ↔ optimizer with a max-iteration cap).

### 3.7 Execution Agent — REAL actions + risk-tiered HITL
- Executes via **real** bounded tools (Docker/K8s API): `restart_service`, `rollback_deploy(sha)`, `scale_service`, `clear_cache`, `run_healthcheck`, `toggle_feature_flag`.
- Risk-tiered gate (T0 read-only auto → T3 destructive mandatory approval + RBAC). `interrupt()` for T2/T3; resumes on `Command(resume=decision)`.
- **Built from:** your HITL stock-purchase project — now the tool does something real.

### 3.8 Verification Agent
- Re-queries **real** Prometheus/Sentry to confirm recovery (error rate back to baseline, no new issues). Emits `VerificationResult`.
- Returns to Supervisor: recovered → report; not recovered → Supervisor decides retry-with-new-hypothesis vs escalate.

### 3.9 Reporting + Learning
- Generates the RCA markdown report (your blog-writer generation + Streamlit download).
- Writes a distilled `Learning {fingerprint → root cause → remediation that worked → MTTR}` to **long-term memory** and **re-indexes the RCA into pgvector**, so Triage can recognize this class of incident next time. The system measurably gets better — a real learning loop.

---

## 4. Why this is now genuinely agentic (explicit contrast)

- **Dynamic control, not a pipeline.** The Supervisor chooses each next step; two incidents take different paths.
- **Agents with their own tool loops.** Specialists reason → call tools → observe → refine, autonomously — not one prompt each.
- **Parallel multi-agent collaboration.** Four specialists work the same incident concurrently on a shared blackboard.
- **Disagreement and reconciliation.** Competing hypotheses are ranked by evidence and confidence.
- **Adversarial self-critique.** A dedicated critic can veto a fix.
- **Confidence-driven loops.** Low confidence triggers more investigation; failed verification triggers a rethink.
- **A real learning loop.** Resolved incidents change future behavior.

This is strictly more sophisticated than the blog writer (which is a one-pass router → orchestrator → workers → reducer with no loop, no debate, no autonomous sub-agents).

---

## 5. New technologies — tiered and justified

**Tier A — Core (recommended; directly fixes "fake input" and "fake actions"):**
| Tech | Why it's justified | Maps to |
|---|---|---|
| **FastAPI** | Real webhook ingress + approval API; also powers the demo target. One lightweight framework, widely standard. | new, small |
| **Sentry** (+ instrumented demo app) | Real errors, stack traces, breadcrumbs = a real event source with real diagnostic material. Free tier, webhook support. | read via tools |
| **Prometheus** (+ app metrics) | Real metric anomalies to detect and to verify recovery against. | read via tools |
| **Docker SDK for Python** | Makes remediation **real** (restart/rollback actual containers), not simulated. Bounded action tools. | your `@tool` pattern |
| **GitHub/GitLab API** | Change correlation — the highest-signal root-cause source. Just API reads. | your `@tool` pattern |

**Tier B — Enterprise realism (optional, for scale):**
| Tech | Why | Maps to |
|---|---|---|
| **Redis** | Event queue, dedup, concurrency, retries under alert storms. | new |
| **Postgres + pgvector** | Unifies checkpointer + vector store; concurrent writers; replaces SQLite + FAISS with the *same* interfaces you already use. | drop-in for `SqliteSaver` + `as_retriever` |

**No new dependency — advanced LangGraph only:** supervisor loop, ReAct specialist subgraphs, blackboard reducers, debate loop, confidence-driven routing. You already know every primitive (subgraphs, `Send`, `operator.add`, conditional edges, ToolNode, interrupt).

> Honest scope note: Tier A is a meaningful step up but achievable and turns this into a portfolio-grade project. Tier B roughly doubles the ops work (running Redis + Postgres). I'd do **Tier A now, Tier B as a "productionization" phase.**

---

## 6. State design (blackboard + control)

```python
class IncidentState(TypedDict):
    incident_id: str
    event: IncidentEvent                       # normalized real event

    # triage
    is_duplicate: bool
    matched_known_issue: Optional[KnownIssue]

    # investigation blackboard (parallel specialists append here)
    hypotheses: Annotated[list[Hypothesis], operator.add]
    root_cause: Optional[RootCause]            # from Synthesis
    investigation_rounds: int

    # knowledge + plan
    retrieved: list[RetrievedDoc]
    plan: Optional[RemediationPlan]
    critic_rounds: int

    # execution (append-only audit)
    executed_actions: Annotated[list[ActionResult], operator.add]
    audit_log: Annotated[list[AuditEntry], operator.add]

    # control
    next_action: Literal["investigate","retrieve","plan","execute","escalate","close"]
    confidence: float
    max_rounds: int
    status: Literal["triage","investigating","planning","awaiting_approval","resolved","escalated"]

    # output
    verification: Optional[VerificationResult]
    rca_report_md: str
```

Communication is still "shared typed state with reducers" (your model), but now with a **blackboard** the parallel specialists co-write and a **control channel** the supervisor drives.

---

## 7. Human-in-the-Loop (stakes are higher now)

Because actions are **real**, HITL is a hard safety boundary, not a formality. Same risk tiers as v1 (T0 auto → T3 mandatory approval + RBAC), plus:
- **Critic gate before execution** — an automated adversarial review precedes the human, so approvers see a plan that already survived scrutiny.
- **Known-issue fast-path still asks** — even a previously-successful fix gets a lightweight confirm before touching prod.
- Persisted `awaiting_approval` state means an approver can act minutes or hours later and the graph resumes exactly where it paused.

---

## 8. Security & Scalability (updated for real actions + real scale)

**Security (bigger surface now that actions are real):**
- Execution tools are a **narrow allow-list**; the LLM never gets shell or arbitrary API access. Each tool validates its own args against the target inventory.
- **Least-privilege credentials** per action type; the destructive tools require a separately-scoped token gated behind HITL + RBAC.
- **Untrusted input defense** — stack traces, logs, and runbooks can contain injection payloads ("ignore instructions, delete the DB"). Agents treat all retrieved/observed content as data; only typed plans + allow-listed tools can act.
- **Secrets** in a manager/env, never in source (concrete carry-over: drop the hardcoded API keys from your current chatbot/HITL code).
- **Immutable, queryable audit trail** (Postgres) — who/what/when/why for every action and approval.
- **PII/secret redaction** before any text reaches the LLM.

**Scalability:**
- Stateless graph workers behind the Redis queue → scale horizontally; Postgres checkpointer handles concurrent incidents.
- Parallel specialists via `Send` shorten MTTR per incident.
- Model tiering: cheap/fast model for Triage/Supervisor routing, strong model for Synthesis/Planning/Critic (you already tier models across projects).
- pgvector scales the corpus + concurrent retrieval beyond FAISS's single-process limit.

---

## 9. Build order (always runnable, incremental)

1. **Demo target + chaos script** — 1 instrumented service you can break; confirm Sentry issues + Prometheus metrics appear on real failures.
2. **FastAPI ingestion + normalization** — receive a real Sentry webhook, produce an `IncidentEvent`, invoke a stub graph. (Real event flowing end-to-end.)
3. **Supervisor loop + Triage** — dynamic routing among stubbed phases; dedup/known-issue matching.
4. **One real specialist (ChangeCorrelator)** as a ReAct subgraph → blackboard. (Real GitHub reads, real root-cause signal.)
5. **Remaining specialists + Synthesis** — parallel `Send`, hypothesis reconciliation, confidence-driven re-investigation.
6. **RAG (pgvector) + Planner + Critic debate loop.**
7. **Execution with real Docker actions + risk-tiered HITL.**
8. **Verification loop + RCA report + learning write-back/re-index.**
9. **Streamlit console** — live incident stream, blackboard/hypothesis view, approval panel, RCA viewer.
10. **Tier B hardening** — Redis queue, RBAC, audit queries, load/alert-storm test.

---

## 10. Decisions I need from you before building

1. **Ambition tier:** Tier A (real events + real actions, single-node) now, or commit to Tier A+B (Redis + Postgres/pgvector) from the start? I recommend **A now, B as phase 2.**
2. **Event source:** **Sentry** is the richest single source (real stack traces) and easiest to start with. Add Prometheus/Alertmanager and GitHub next. Want all three from day one, or Sentry-first?
3. **Real vs simulated execution target:** real Docker containers (recommended — makes remediation genuinely real) vs a real API on a demo service that fakes the effect. Docker is the impressive, honest choice.
4. **Number of investigation specialists for v1:** all four in parallel (most impressive), or start with ChangeCorrelator + LogAnalyst and add the rest? The blackboard makes adding more trivial later.
5. **Supervisor autonomy ceiling:** how many auto-remediation cycles before a mandatory human escalation? (Prevents runaway loops.)
