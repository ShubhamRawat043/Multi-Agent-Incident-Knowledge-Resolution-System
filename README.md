# Enterprise Multi-Agent Incident & Knowledge Resolution System

Supervisor-orchestrated multi-agent system for automated incident detection, diagnosis, and remediation.

## Stack
- **Orchestration:** LangGraph (supervisor + ReAct specialists + HITL)
- **Integrations:** Sentry, Prometheus, GitHub, Docker
- **Store:** PostgreSQL + pgvector (checkpointer, RAG, audit, long-term memory)
- **Demo target:** orders-api → payments-api (FastAPI) with fault injection
- **UI:** Streamlit incident console

## Quick start

```bash
# 1. Copy env and fill secrets
cp .env.example .env

# 2. Start infra + demo services
docker compose up -d

# 3. Install Python deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 4. Apply DB schema and build RAG index
psql "$DATABASE_URL" -f db/schema.sql
python -m rag.build_index

# 5. Start ingestion API
uvicorn ingestion.app:app --reload --port 8080

# 6. Start Streamlit console
streamlit run ui/streamlit_app.py

# 7. Inject a scenario
python -m chaos.controller --scenario scenario_02_bad_deploy
```

## Architecture
See [incident_system_blueprint.md](incident_system_blueprint.md).

## Safety
- Docker actions are allow-listed and dry-run by default (`DRY_RUN=true`).
- T2/T3 remediations require human approval via HITL interrupt.
- Never commit real API keys; use `.env` only.
