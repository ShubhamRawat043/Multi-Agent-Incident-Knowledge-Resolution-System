"""
Lightweight behavioral checks that can run without full infra.

Usage:
  python -m tests.run_smoke          # unit-level, no docker
  python -m tests.run_scenario_check # needs ingestion + demo up
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_normalize_and_fingerprint():
    from ingestion.normalize import normalize

    event = normalize(
        "manual",
        {
            "service": "payments-api",
            "title": "TypeError in payment processing",
            "stacktrace": "TypeError: NoneType",
            "deploy_tag": "v1.8",
        },
    )
    assert event.service == "payments-api"
    assert event.fingerprint
    print("OK normalize", event.fingerprint)


def test_redaction():
    from tools.redaction import redact

    text = "user admin@example.com token=sk-abcdefghijklmnopqrstuvwxyz123456"
    out = redact(text)
    assert "admin@example.com" not in out
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
    print("OK redaction", out)


def test_docker_allowlist_logic():
    from agent.config import settings

    assert "payments-api" in settings.demo_container_allowlist or any(
        "payment" in x for x in settings.demo_container_allowlist
    )
    assert settings.dry_run is True or settings.dry_run is False
    print("OK docker allowlist", settings.demo_container_allowlist, "dry_run", settings.dry_run)


def test_loop_caps_present():
    from agent.config import settings

    assert settings.supervisor_max_rounds >= 1
    assert settings.specialist_max_steps >= 1
    assert settings.critic_max_revisions >= 1
    assert settings.verification_max_retries >= 1
    print(
        "OK caps",
        settings.supervisor_max_rounds,
        settings.specialist_max_steps,
        settings.critic_max_revisions,
        settings.verification_max_retries,
    )


def test_heuristic_planner():
    from agent.planner import _heuristic_plan

    state = {
        "root_cause": {"summary": "bad deploy", "category": "bad_deployment", "confidence": 0.9},
        "event": {"service": "payments-api", "deploy_tag": "v1.8"},
        "retrieved": [{"source": "knowledge/sops/deployment_rollback.md", "content": "rollback"}],
    }
    plan = _heuristic_plan(state)
    assert any(s.action_type == "rollback_deploy" for s in plan.steps)
    assert any(s.requires_approval for s in plan.steps)
    print("OK planner", plan.summary)


def test_supervisor_max_rounds():
    from agent.supervisor import supervisor_node
    from agent.config import settings

    state = {
        "supervisor_rounds": settings.supervisor_max_rounds,
        "confidence": 0.4,
        "root_cause": None,
        "investigation_rounds": 0,
        "verification_retries": 0,
    }
    out = supervisor_node(state)
    assert out["next_action"] == "escalate"
    print("OK supervisor max_rounds escalate")


def test_catalogue_exists():
    cat = ROOT / "tests" / "scenarios" / "catalogue.yaml"
    assert cat.exists()
    text = cat.read_text(encoding="utf-8")
    assert "T16" in text
    print("OK catalogue")


def main():
    test_normalize_and_fingerprint()
    test_redaction()
    test_docker_allowlist_logic()
    test_loop_caps_present()
    test_heuristic_planner()
    test_supervisor_max_rounds()
    test_catalogue_exists()
    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    main()
