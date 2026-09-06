"""Central config: loop caps, model tiers, safety flags, integration URLs."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> "Settings":
    return Settings()


class Settings:
    # Models (tiered for token/cost control)
    openai_api_key: str
    fast_model: str
    strong_model: str

    # Loop caps
    supervisor_max_rounds: int
    investigation_max_rounds: int
    specialist_max_steps: int
    critic_max_revisions: int
    verification_max_retries: int
    confidence_proceed: float
    confidence_investigate: float

    # Store
    database_url: str

    # Integrations
    sentry_dsn: str
    sentry_auth_token: str
    sentry_org: str
    sentry_project: str
    prometheus_url: str
    github_token: str
    github_repo: str

    # Docker safety
    dry_run: bool
    demo_container_allowlist: list[str]
    demo_container_label: str

    # Demo URLs
    orders_api_url: str
    payments_api_url: str
    ingestion_url: str

    # RBAC
    t3_approver_roles: list[str]

    def __init__(self) -> None:
        self.openai_api_key = _env("OPENAI_API_KEY")
        self.fast_model = _env("OPENAI_FAST_MODEL", "gpt-4o-mini")
        self.strong_model = _env("OPENAI_STRONG_MODEL", "gpt-4o")

        self.supervisor_max_rounds = _env_int("SUPERVISOR_MAX_ROUNDS", 10)
        self.investigation_max_rounds = _env_int("INVESTIGATION_MAX_ROUNDS", 3)
        self.specialist_max_steps = _env_int("SPECIALIST_MAX_STEPS", 4)
        self.critic_max_revisions = _env_int("CRITIC_MAX_REVISIONS", 3)
        self.verification_max_retries = _env_int("VERIFICATION_MAX_RETRIES", 2)
        self.confidence_proceed = _env_float("CONFIDENCE_PROCEED", 0.80)
        self.confidence_investigate = _env_float("CONFIDENCE_INVESTIGATE", 0.50)

        self.database_url = _env(
            "DATABASE_URL",
            "postgresql://incident:incident@localhost:5432/incident_db",
        )

        self.sentry_dsn = _env("SENTRY_DSN")
        self.sentry_auth_token = _env("SENTRY_AUTH_TOKEN")
        self.sentry_org = _env("SENTRY_ORG")
        self.sentry_project = _env("SENTRY_PROJECT")
        self.prometheus_url = _env("PROMETHEUS_URL", "http://localhost:9090")
        self.github_token = _env("GITHUB_TOKEN")
        self.github_repo = _env("GITHUB_REPO", "your-org/incident-demo")

        self.dry_run = _env_bool("DRY_RUN", True)
        allow = _env(
            "DEMO_CONTAINER_ALLOWLIST",
            "orders-api,payments-api,incident-orders-api,incident-payments-api",
        )
        self.demo_container_allowlist = [x.strip() for x in allow.split(",") if x.strip()]
        self.demo_container_label = _env("DEMO_CONTAINER_LABEL", "incident-demo=true")

        self.orders_api_url = _env("ORDERS_API_URL", "http://localhost:8001")
        self.payments_api_url = _env("PAYMENTS_API_URL", "http://localhost:8002")
        self.ingestion_url = _env("INGESTION_URL", "http://localhost:8080")

        roles = _env("T3_APPROVER_ROLES", "oncall,sre-lead")
        self.t3_approver_roles = [x.strip() for x in roles.split(",") if x.strip()]


# Convenience aliases used across modules
settings = get_settings()
