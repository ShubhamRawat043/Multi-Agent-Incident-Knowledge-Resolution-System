# Runbook — how to start and demo this project (Windows / VS Code)

Written for someone who knows LangGraph but not the rest of the stack. Every
command is PowerShell, which is what VS Code opens by default on Windows.

---

## Contents

1. [Do this first — one thing may be broken](#1-do-this-first--one-thing-may-be-broken)
2. [The mental model: what runs where](#2-the-mental-model-what-runs-where)
3. [Terminal plan](#3-terminal-plan)
4. [Step-by-step startup](#4-step-by-step-startup)
5. [The demo script for your mentor](#5-the-demo-script-for-your-mentor)
6. [Resetting between demos](#6-resetting-between-demos)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Do this first — one thing may be broken

Your `.env` has:

```
OPENAI_FAST_MODEL=gpt-5.6-luna
OPENAI_STRONG_MODEL=gpt-5.6-luna
```

I could not verify that model name without spending your API credits, so **you
need to check it**. This matters more than anything else in this runbook,
because of how the code is written: *every* LLM call in this project is wrapped
in `try / except` with a deterministic fallback. If the model name is wrong,
nothing crashes — the system quietly stops using AI at all.

What that looks like in the demo: all four specialists return
`"LogAnalyst failed: ..."` with confidence `0.1`, synthesis has nothing to work
with, the supervisor sees low confidence, re-investigates three times, and
escalates. Your mentor sees a system that gives up on every incident, and no
error message explains why.

**Run this check (Terminal 4, venv active). It costs a fraction of a cent:**

```powershell
python -c "from agent.llm import get_fast_llm; print(get_fast_llm().invoke('Reply with the single word: ok').content)"
```

- Prints `ok` → your model name is fine, skip to section 2.
- Prints a `NotFoundError` / `model_not_found` error → edit `.env` and set both
  models to values your key actually has. The defaults this project ships with
  in `agent/config.py` are `gpt-4o-mini` (fast) and `gpt-4o` (strong), so those
  are the safe fallback:

```
OPENAI_FAST_MODEL=gpt-4o-mini
OPENAI_STRONG_MODEL=gpt-4o
```

Two models are used deliberately: the **fast** one runs the four specialists and
the supervisor (many small calls), the **strong** one runs synthesis, the planner
and the critic (fewer, harder calls). That is the cost-control design.

> **Whenever you edit `.env`, restart every Python process.** `agent/config.py`
> caches settings with `@lru_cache` at import time, so a running server will not
> notice the change.

---

## 2. The mental model: what runs where

This trips up most people the first time. There are **two worlds**:

```
┌─────────────────────── DOCKER (containers) ────────────────────────┐
│                                                                     │
│   postgres:5432      pgvector - checkpointer, RAG, audit tables     │
│   prometheus:9090    scrapes metrics from the two demo services     │
│   payments-api:8002  the FastAPI service you break on purpose       │
│   orders-api:8001    calls payments-api, so failures cascade        │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                  ▲
                                  │  the agent talks to all of these
                                  │  over localhost, and to Docker itself
                                  │
┌────────────────────── YOUR HOST (venv, VS Code) ───────────────────┐
│                                                                     │
│   uvicorn ingestion.app:app --port 8080   ← the LangGraph agent     │
│   streamlit run ui/streamlit_app.py       ← the incident console    │
│   your command terminal                   ← chaos + tests           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**The agent runs on your host, not in Docker.** That is deliberate: it needs to
talk to the Docker daemon to restart containers, and a container cannot easily
do that. This is why `DATABASE_URL` in your `.env` says `localhost:5432` — that
is the host's view of the Postgres container.

The demo services are the **patient**. The agent is the **doctor**. You make the
patient sick with `chaos/`, and watch the doctor work.

---

## 3. Terminal plan

Open four terminals in VS Code (`` Ctrl+Shift+` `` for each, or click the `+` in
the terminal panel). Terminals 2, 3 and 4 need the venv active:

```powershell
.\.venv\Scripts\Activate.ps1
```

You will know it worked when the prompt starts with `(.venv)`.

| Terminal | Purpose | Stays running? |
|----------|---------|----------------|
| **1 — Docker** | `docker compose` commands and logs | No, frees up after start |
| **2 — Agent API** | `uvicorn ingestion.app:app` | **Yes, leave it open** |
| **3 — UI** | `streamlit run ui/streamlit_app.py` | **Yes, leave it open** |
| **4 — Commands** | chaos injection, tests, scenario runs | No, this is your workbench |

> If `Activate.ps1` fails with "running scripts is disabled on this system", run
> this once, then try again:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```

---

## 4. Step-by-step startup

### Step 1 — Refresh dependencies (Terminal 4)

I added `PyYAML` to `requirements.txt` (the catalogue runner parses YAML). It is
probably already installed as a LangChain dependency, but be safe:

```powershell
pip install -r requirements.txt
```

### Step 2 — Start Docker (Terminal 1)

Make sure Docker Desktop is actually running — the whale icon in your system
tray should say "Docker Desktop is running". Then:

```powershell
docker compose up -d --build
```

The first run takes a few minutes: it downloads Postgres and Prometheus, and
builds the two demo service images. `-d` means "detached" (runs in the
background so you get your terminal back).

**You do not need `psql`.** The README mentions it, but `docker-compose.yml`
mounts `db/schema.sql` into the Postgres container's init directory, so the
schema is created automatically the first time the database volume is made.

Check all four containers are healthy:

```powershell
docker compose ps
```

You want to see `incident-postgres`, `incident-prometheus`,
`incident-payments-api` and `incident-orders-api` all `running`.

Now confirm the demo services actually answer:

```powershell
Invoke-RestMethod http://localhost:8002/health
```

```powershell
Invoke-RestMethod http://localhost:8001/health
```

Each should return `status: ok` and `fault_mode: none`. (`Invoke-RestMethod` is
PowerShell's version of `curl` — plain `curl` behaves oddly in PowerShell.)

### Step 3 — Build the RAG index (Terminal 4, once)

This reads the 16 markdown files in `knowledge/`, splits them into chunks, calls
OpenAI's embedding model, and stores the vectors in pgvector. It is what makes
the retrieval step able to cite real runbooks.

```powershell
python -m rag.build_index
```

Run this **once**, after Postgres is up. It costs a few cents at most. Re-run it
only if you edit the files in `knowledge/`.

### Step 4 — Start the agent API (Terminal 2)

```powershell
uvicorn ingestion.app:app --reload --port 8080
```

Leave this running. This process **is** your LangGraph application — it compiles
the graph at import time and runs every incident. Watch this terminal during the
demo: it prints what the agent is doing.

If it starts cleanly you will see `Application startup complete`. Verify:

```powershell
Invoke-RestMethod http://localhost:8080/health
```

> If you see `[checkpointer] Using MemorySaver fallback (...)` in this terminal,
> the agent could not reach Postgres and is keeping state in memory only.
> Incidents will still work, but they vanish when you restart the server. Fix
> Postgres before demoing.

### Step 5 — Start the UI (Terminal 3)

```powershell
streamlit run ui/streamlit_app.py
```

It opens `http://localhost:8501` in your browser. This is the console you will
show your mentor: it has tabs for the hypothesis blackboard, the plan, the
approval prompt, the RCA report, and the audit trail.

### Step 6 — Decide on DRY_RUN

This is the single most important demo decision.

`DRY_RUN=true` (your current setting) means remediation actions are **simulated
only** — `restart_service` logs "would restart" and changes nothing. Since
nothing changes, the fault is still there, verification honestly reports "not
recovered", and **every incident escalates**. That is correct behaviour, but it
is not a satisfying demo.

For the live demo, set in `.env`:

```
DRY_RUN=false
```

Then **restart Terminal 2** (`Ctrl+C`, re-run uvicorn) so the new setting loads.

With `DRY_RUN=false` the agent really does restart containers and clear faults —
and incidents actually resolve. This is safe: `tools/docker_tools.py` enforces an
allow-list of container names **and** requires the `incident-demo=true` label,
so it physically cannot touch anything except your two demo containers. Rollback
is additionally restricted to a fixed list of image tags.

---

## 5. The demo script for your mentor

Five acts, roughly 20 minutes. Run everything in **Terminal 4**.

### Act 1 — "Here is the whole behaviour suite" (2 min, no infra needed)

Open with breadth. This runs all 16 behavioral tests and needs nothing running:

```powershell
python -m tests.run_catalogue
```

You get 16 scenarios with PASS/FAIL per expectation and the evidence for each.
Say what this is: *the real LangGraph graph, compiled and executed — supervisor
routing, planner, critic, execution with real human-approval interrupts, and the
real verification node. Only the outside world is simulated (LLM specialists,
Docker, pgvector, the demo services).* That is why it takes seconds and costs
nothing, yet still tests orchestration and safety for real.

Then show one in detail, with the agent's full audit trail:

```powershell
python -m tests.run_catalogue --test T10 --verbose
```

T10 is the interesting one — a failed verification forcing a re-plan. Point at
the `replan_requested` line in the trail.

### Act 2 — "And here it is against real infrastructure"

Show the moving parts in the browser:

- `http://localhost:8501` — the incident console
- `http://localhost:9090` — Prometheus (try the query `http_error_rate`)
- `http://localhost:8002/info` — the payments service, currently healthy

### Act 3 — The headline run: a bad deployment (5 min)

Break the service:

```powershell
python -m chaos.controller --scenario scenario_02_bad_deploy
```

Show that it is genuinely broken — this now returns a 500:

```powershell
Invoke-RestMethod -Method Post http://localhost:8002/pay -ContentType "application/json" -Body '{"order_id":"demo","amount":10}'
```

Now report the incident and let the agent work:

```powershell
python -m tests.run_scenario_check --scenario scenario_02_bad_deploy
```

While it runs, narrate what Terminal 2 is printing. Then, when it pauses:

**This is the moment to switch to the Streamlit UI.** The agent has hit a
LangGraph `interrupt()` because a rollback is a T2 (production-impacting) action.
Go to the **Approve** tab, show the pending request, and click approve.

Watch the rest: the rollback runs, verification sends four real requests through
`/pay`, they succeed, and the incident closes as **resolved**. Show the **RCA**
tab (downloadable markdown) and the **Audit** tab.

Then open **LangSmith** (`smith.langchain.com`, project
`incident-resolution-system`) and show the full trace tree — every specialist,
every tool call, grouped under one incident.

### Act 4 — The safety story (5 min)

This is what separates a toy from something an SRE team would let near
production. Three things to show:

**(a) A human "no" actually stops it.**

```powershell
python -m tests.run_scenario_check --scenario scenario_02_bad_deploy --approve no
```

The rollback comes back `cancelled`, not executed.

**(b) It refuses to roll back when there was no deployment.**

```powershell
python -m chaos.controller --scenario scenario_04_db_pool
```

```powershell
python -m tests.run_scenario_check --scenario scenario_04_db_pool
```

This incident has no `deploy_tag`. The agent restarts the service instead of
rolling back. Explain why that matters: rolling back a release that had nothing
to do with the incident is a classic way to make an outage worse. This is
covered by test T04.

**(c) The tools are physically fenced in.** Open `tools/docker_tools.py` and show
`_is_allowed()` — the container name allow-list plus the required
`incident-demo=true` label. Then show that the guard actually fires:

```powershell
python -m tests.run_smoke
```

### Act 5 — "It knows when it is beaten" (3 min)

The restart-then-rollback storyline. A restart cannot fix a bad deploy, because
the bad code is still what is running:

```powershell
python -m tests.run_catalogue --test T16 --verbose
```

Show the audit trail: execute → verify fails → `replan_requested` → a **new**
plan → execute again → verify fails again → `verification_exhausted` → escalate.
Emphasise the last part: it escalates for a *reason it can state*, not because it
ran out of turns.

If you want the deeper version of this story, tell your mentor about the fault
model in `demo_target/faults/injectors.py`: fault state is persisted outside the
process, and `RESTART_CLEARED_FAULTS` distinguishes faults a restart genuinely
cures (a leaked connection pool, a memory leak — the leak dies with the process)
from ones it cannot (a bad deploy, a bad config — the broken version is still
deployed). That distinction is what makes "we tried a restart and it did not
help" possible to demonstrate at all.

### If your mentor asks what is NOT done

Have this ready — being straight about it lands better than being caught out.
`AUDIT_AND_FIXES.md` section 11 lists eight known open issues. The two most
honest ones to volunteer: `/approve` has no authentication (the approver's role
is trusted from the request body), and webhooks run the whole investigation
synchronously inside the HTTP request, which would time out a real Sentry
webhook.

---

## 6. Resetting between demos

Clear the injected faults on both services:

```powershell
python -c "from chaos.controller import reset_all; print(reset_all(['http://localhost:8002','http://localhost:8001']))"
```

Most scenario commands also accept `--reset` to clean up after themselves.

Full restart of the demo services:

```powershell
docker compose restart payments-api orders-api
```

> Note: a restart no longer clears a `bad_deploy` or `config_error` fault — that
> is the fix described above, and it is intentional. Use the `reset_all` command
> to clear those.

Wipe everything including the database and start completely fresh (you will need
to re-run `python -m rag.build_index` afterwards):

```powershell
docker compose down -v
```

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Every incident escalates, specialists say "failed" | Wrong model name in `.env` | Section 1 |
| `OpenAIError: Missing credentials` on uvicorn start | `OPENAI_API_KEY` not loaded | Check `.env` is in the project root; restart the terminal |
| `[checkpointer] Using MemorySaver fallback` | Postgres unreachable | `docker compose ps`; check `DATABASE_URL` says `localhost:5432` |
| `Connection refused` on port 8002 | Demo containers not up | `docker compose up -d` |
| `restart_service` returns `denied` | Container missing the `incident-demo=true` label | You are pointing at the wrong container; check `docker compose ps` |
| Nothing changes after editing `.env` | Settings are cached at import | Restart Terminals 2 and 3 |
| Incidents resolve suspiciously fast, actions say `dry_run` | `DRY_RUN=true` | Section 6 of startup, then restart Terminal 2 |
| `Activate.ps1 cannot be loaded` | PowerShell execution policy | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| Retrieval cites nothing / RAG empty | Index never built | `python -m rag.build_index` |
| Catalogue `--live` is very slow | 16 scenarios × real LLM calls | Use `--test T05` for one at a time |

### A note on the live catalogue

```powershell
python -m tests.run_catalogue --live --test T05
```

`--live` drives the real stack: it injects the fault, generates traffic, posts a
real incident, answers the approval prompt, and reads the result back. Running
all 16 that way means 16 full LLM investigations — slow and not free. Run one or
two live and use the offline mode for breadth.
