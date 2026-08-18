-- Enterprise Incident System — Postgres + pgvector schema
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Services inventory (demo allow-list metadata)
CREATE TABLE IF NOT EXISTS services (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    container   TEXT NOT NULL,
    base_url    TEXT,
    image_tag   TEXT DEFAULT 'v1.0',
    labels      JSONB DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO services (name, container, base_url, image_tag, labels) VALUES
    ('orders-api', 'incident-orders-api', 'http://localhost:8001', 'v1.0', '{"incident-demo":"true"}'),
    ('payments-api', 'incident-payments-api', 'http://localhost:8002', 'v1.7', '{"incident-demo":"true"}')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS deployments (
    id          SERIAL PRIMARY KEY,
    service     TEXT NOT NULL,
    sha         TEXT,
    image_tag   TEXT,
    deployed_at TIMESTAMPTZ DEFAULT NOW(),
    source      TEXT DEFAULT 'github',
    metadata    JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    fingerprint     TEXT,
    source          TEXT,
    service         TEXT,
    kind            TEXT,
    title           TEXT,
    severity        TEXT,
    status          TEXT DEFAULT 'triage',
    confidence      DOUBLE PRECISION DEFAULT 0,
    root_cause      JSONB,
    event           JSONB,
    rca_report_md   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(fingerprint);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_service ON incidents(service);

CREATE TABLE IF NOT EXISTS actions (
    id              SERIAL PRIMARY KEY,
    incident_id     TEXT REFERENCES incidents(id) ON DELETE CASCADE,
    action_type     TEXT NOT NULL,
    risk_tier       TEXT,
    target          TEXT,
    args            JSONB DEFAULT '{}'::jsonb,
    result          JSONB,
    dry_run         BOOLEAN DEFAULT TRUE,
    status          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS approvals (
    id              SERIAL PRIMARY KEY,
    incident_id     TEXT REFERENCES incidents(id) ON DELETE CASCADE,
    action_id       INT REFERENCES actions(id) ON DELETE SET NULL,
    decision        TEXT NOT NULL,
    approver        TEXT,
    role            TEXT,
    reason          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     TEXT,
    agent           TEXT,
    event_type      TEXT,
    payload         JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_incident ON audit_logs(incident_id);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id              SERIAL PRIMARY KEY,
    doc_type        TEXT NOT NULL,  -- sop | runbook | historical_rca
    service         TEXT,
    title           TEXT NOT NULL,
    source_path     TEXT,
    content         TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Chunked embeddings for RAG + known-issue matching
-- text-embedding-3-small = 1536 dims
CREATE TABLE IF NOT EXISTS embeddings (
    id              SERIAL PRIMARY KEY,
    doc_id          INT REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL DEFAULT 0,
    chunk_text      TEXT NOT NULL,
    doc_type        TEXT,
    service         TEXT,
    source          TEXT,
    embedding       vector(1536),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_embeddings_doc_type ON embeddings(doc_type);
CREATE INDEX IF NOT EXISTS idx_embeddings_service ON embeddings(service);

-- Long-term learnings (distilled across incidents)
CREATE TABLE IF NOT EXISTS learnings (
    id              SERIAL PRIMARY KEY,
    fingerprint     TEXT,
    service         TEXT,
    symptom         TEXT,
    root_cause      TEXT,
    remediation     TEXT,
    mttr_seconds    INT,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_learnings_service ON learnings(service);
