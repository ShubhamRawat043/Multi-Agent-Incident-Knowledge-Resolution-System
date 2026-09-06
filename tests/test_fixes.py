"""
Regression checks for the four demo-breaking bugs found in the audit.

Covers:
  1. verification cannot report recovery without live evidence (no DRY_RUN override)
  2. a failed verification produces a SECOND remediation attempt
  3. verification judges only the latest execution round
  4. faults survive a container restart unless a restart genuinely cures them

Usage:
  python -m tests.test_fixes     # no docker, no database, no LLM calls
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- helpers


def _stub_verification_signals(probe: dict, prom_result: list | None = None):
    """Point verification at canned probe/health/Prometheus responses."""
    import agent.verification as v

    # These tests assert on DRY_RUN wording, so pin it instead of inheriting
    # whatever .env currently holds for the live demo.
    from agent.config import settings as _settings

    _settings.dry_run = True

    class _Prom:
        @staticmethod
        def invoke(_args):
            return {"data": {"result": prom_result or []}}

    v._health = lambda url: {"ok": True, "body": {"status": "ok"}}
    v._probe = lambda service, url: probe
    v.get_service_error_rate = _Prom
    return v


def _neutralize_llms():
    """Force every LLM call to fail so the deterministic fallbacks are exercised."""
    import agent.llm as llm_mod

    def boom(*_a, **_k):
        raise RuntimeError("LLM disabled in tests")

    llm_mod.get_fast_llm = boom
    llm_mod.get_strong_llm = boom
    for name in ("supervisor", "synthesis", "planner", "critic"):
        mod = importlib.import_module(f"agent.{name}")
        for attr in ("get_fast_llm", "get_strong_llm"):
            if hasattr(mod, attr):
                setattr(mod, attr, boom)


def _build_offline_graph(verification_results: list[bool]):
    """
    Compile the real graph with the external edges stubbed out: in-memory
    checkpointer, no Postgres, no pgvector, no specialists, no LLM.

    Returns (graph, stats) where stats records execution/planning calls.
    """
    import agent.checkpointer as checkpointer
    from langgraph.checkpoint.memory import MemorySaver

    checkpointer.build_checkpointer = lambda: MemorySaver()

    # Import before neutralizing: the specialist modules construct their
    # ChatOpenAI at import time, so they need get_fast_llm intact.
    import agent.graph as gmod
    import agent.investigate as investigate
    import agent.reporting as reporting
    import agent.triage as triage
    import rag.store as rag_store

    _neutralize_llms()

    # Offline runs simulate a world where remediation never really lands, so the
    # verification-failure and escalation paths stay reachable. Pin DRY_RUN here
    # rather than inheriting it from .env: flipping DRY_RUN for a live demo must
    # not change what these deterministic tests assert.
    from agent.config import settings as _settings

    _settings.dry_run = True


    hypothesis = {
        "statement": "payments-api failing after the v1.8 release",
        "evidence": ["TypeError in payment processing"],
        "confidence": 0.9,
        "source_agent": "LogAnalyst",
    }
    investigate.run_log_analyst = lambda ctx: dict(hypothesis)
    investigate.run_metrics_analyst = lambda ctx: dict(hypothesis)
    investigate.run_change_correlator = lambda ctx: dict(hypothesis)
    investigate.run_dependency_analyst = lambda ctx: dict(hypothesis)

    triage.find_open_by_fingerprint = lambda fp, **kw: None
    triage.match_known_issue = lambda event: None
    rag_store.similarity_search = lambda *a, **k: []
    reporting.upsert_incident = lambda *a, **k: None
    reporting.write_learning = lambda *a, **k: None

    stats: dict = {"executions": 0, "plans": []}
    real_planner = gmod.planner_node

    def counting_execution(state):
        stats["executions"] += 1
        actions = [
            {
                "action_type": step.get("action_type"),
                "target": step.get("target"),
                "risk_tier": step.get("risk_tier"),
                "status": "success",
                "detail": "stubbed execution",
                "dry_run": False,
                "args": {},
            }
            for step in (state.get("plan") or {}).get("steps") or []
        ]
        return {
            "executed_actions": actions,
            "last_executed_actions": actions,
            "status": "investigating",
        }

    def recording_planner(state):
        out = real_planner(state)
        stats["plans"].append([s["action_type"] for s in out["plan"]["steps"]])
        return out

    pending = list(verification_results)

    def scripted_verification(state):
        recovered = pending.pop(0) if pending else False
        retries = int(state.get("verification_retries") or 0)
        return {
            "verification": {"recovered": recovered, "notes": "scripted"},
            "verification_retries": retries if recovered else retries + 1,
            "confidence": 0.9,
        }

    gmod.execution_node = counting_execution
    gmod.planner_node = recording_planner
    gmod.verification_node = scripted_verification
    gmod.retrieval_node = lambda state: {
        "retrieved": [
            {
                "content": "restart, then roll back if the error rate stays high",
                "source": "knowledge/sops/service_restart.md",
                "doc_type": "sop",
                "score": 0.9,
            }
        ]
    }

    graph = gmod.build_graph(checkpointer=MemorySaver())
    stats["restore"] = lambda: setattr(gmod, "planner_node", real_planner)
    return graph, stats


def _run_to_completion(graph, incident_id: str) -> dict:
    """Invoke the graph, auto-approving every HITL interrupt."""
    import agent.graph as gmod
    from langgraph.types import Command

    state = gmod.initial_state(
        incident_id,
        {
            "source": "manual",
            "service": "payments-api",
            "kind": "error",
            "title": "payments-api returning 500s",
            "fingerprint": f"fp-{incident_id}",
            "raw": {},
        },
    )
    config = {"configurable": {"thread_id": incident_id}}
    out = graph.invoke(state, config=config)
    guard = 0
    while (out.get("__interrupt__") or []) and guard < 10:
        guard += 1
        out = graph.invoke(
            Command(resume={"decision": "yes", "approver": "tester", "role": "oncall"}),
            config=config,
        )
    return out


# ---------------------------------------------------------------- tests


def test_verification_needs_live_evidence():
    """DRY_RUN must not turn a still-broken service into a recovery."""
    v = _stub_verification_signals(
        {"sent": 4, "failed": 4, "error_rate": 1.0, "errors": ["HTTP 500"]}
    )
    out = v.verification_node(
        {
            "event": {"service": "payments-api"},
            "last_executed_actions": [
                {"action_type": "restart_service", "status": "dry_run", "dry_run": True}
            ],
            "confidence": 0.8,
            "verification_retries": 0,
        }
    )
    assert out["verification"]["recovered"] is False, out["verification"]["notes"]
    assert "DRY_RUN" in out["verification"]["notes"]
    print("OK verification honest under DRY_RUN")


def test_verification_confirms_real_recovery():
    v = _stub_verification_signals(
        {"sent": 4, "failed": 0, "error_rate": 0.0, "errors": []}
    )
    out = v.verification_node(
        {
            "event": {"service": "payments-api"},
            "last_executed_actions": [
                {"action_type": "rollback_deploy", "status": "success", "dry_run": False}
            ],
            "confidence": 0.8,
            "verification_retries": 1,
        }
    )
    assert out["verification"]["recovered"] is True, out["verification"]["notes"]
    assert out["verification"]["error_rate"] == 0.0
    print("OK verification confirms a real recovery")


def test_verification_without_any_signal():
    """No probe and no Prometheus data is not evidence of recovery."""
    v = _stub_verification_signals(
        {
            "sent": 0,
            "failed": 0,
            "error_rate": None,
            "detail": "probe unavailable: connection refused",
            "errors": [],
        }
    )
    out = v.verification_node(
        {
            "event": {"service": "payments-api"},
            "last_executed_actions": [
                {"action_type": "restart_service", "status": "success", "dry_run": False}
            ],
            "confidence": 0.8,
            "verification_retries": 0,
        }
    )
    assert out["verification"]["recovered"] is False, out["verification"]["notes"]
    print("OK verification refuses to guess without signal")


def test_verification_ignores_stale_actions():
    """executed_actions accumulates; only the latest round may be judged."""
    v = _stub_verification_signals(
        {"sent": 4, "failed": 3, "error_rate": 0.75, "errors": ["HTTP 500"]}
    )
    out = v.verification_node(
        {
            "event": {"service": "payments-api"},
            "executed_actions": [
                {"action_type": "restart_service", "status": "success", "dry_run": False}
            ],
            "last_executed_actions": [
                {"action_type": "rollback_deploy", "status": "cancelled", "dry_run": False}
            ],
            "confidence": 0.8,
            "verification_retries": 1,
        }
    )
    assert out["verification"]["recovered"] is False, out["verification"]["notes"]
    print("OK verification ignores stale successes")


def test_heuristic_planner_escalates_after_failed_restart():
    """With a deploy to revert, a failed restart escalates to a rollback."""
    from agent.planner import _heuristic_plan

    state = {
        "root_cause": {"summary": "still failing", "category": "unknown", "confidence": 0.9},
        "event": {"service": "payments-api", "deploy_tag": "v1.8"},
        "retrieved": [{"source": "knowledge/sops/service_restart.md", "content": "restart"}],
        "executed_actions": [{"action_type": "restart_service", "status": "success"}],
    }
    plan = _heuristic_plan(state)
    assert any(s.action_type == "rollback_deploy" for s in plan.steps), plan.steps
    assert not any(s.action_type == "restart_service" for s in plan.steps), plan.steps
    print("OK planner escalates restart -> rollback on retry")


def test_heuristic_planner_will_not_blind_rollback():
    """With no deploy to revert, a failed restart must NOT become a rollback."""
    from agent.planner import _heuristic_plan

    state = {
        "root_cause": {
            "summary": "connection pool still saturated",
            "category": "db_pool_exhaustion",
            "confidence": 0.9,
        },
        "event": {"service": "payments-api"},  # no deploy_tag, no deploy_sha
        "retrieved": [
            {"source": "knowledge/sops/database_connection_pool.md", "content": "restart"}
        ],
        "executed_actions": [{"action_type": "restart_service", "status": "success"}],
    }
    plan = _heuristic_plan(state)
    assert not any(s.action_type == "rollback_deploy" for s in plan.steps), plan.steps
    print("OK planner refuses a blind rollback with no deploy evidence")


def test_faults_survive_restart():
    """
    A container restart must not cure a bad deploy, but must clear the faults
    whose damage is leaked in-process resources.
    """
    state_path = Path(tempfile.gettempdir()) / "incident_fault_mode_test"
    previous = os.environ.get("FAULT_STATE_PATH")
    os.environ["FAULT_STATE_PATH"] = str(state_path)
    demo_root = ROOT / "demo_target"
    if str(demo_root) not in sys.path:
        sys.path.insert(0, str(demo_root))
    if state_path.exists():
        state_path.unlink()

    try:
        injectors = importlib.import_module("faults.injectors")
        injectors = importlib.reload(injectors)

        injectors.set_fault("bad_deploy")
        # A module reload gives a fresh process state, which is what a
        # container restart produces.
        injectors = importlib.reload(injectors)
        assert injectors.get_fault() == "bad_deploy", injectors.get_fault()

        injectors.set_fault("db_pool_exhaustion")
        injectors = importlib.reload(injectors)
        assert injectors.get_fault() == "none", injectors.get_fault()

        injectors.set_fault("bad_deploy")
        injectors.clear_fault()
        injectors = importlib.reload(injectors)
        assert injectors.get_fault() == "none", injectors.get_fault()
        print("OK faults are restart-proof, restart-curable ones still clear")
    finally:
        if previous is None:
            os.environ.pop("FAULT_STATE_PATH", None)
        else:
            os.environ["FAULT_STATE_PATH"] = previous
        if state_path.exists():
            state_path.unlink()


def test_failed_verification_retries_remediation():
    """restart fails -> re-investigate -> NEW plan -> rollback -> resolved."""
    graph, stats = _build_offline_graph([False, True])
    try:
        out = _run_to_completion(graph, "INC-TEST-RETRY")
    finally:
        stats["restore"]()

    assert stats["executions"] >= 2, f"execute ran {stats['executions']} time(s)"
    assert len(stats["plans"]) >= 2, stats["plans"]
    assert out["status"] == "resolved", out["status"]
    assert out.get("rca_report_md")
    events = [a.get("event_type") for a in out.get("audit_log") or []]
    assert "replan_requested" in events, events
    print("OK second remediation attempt runs and resolves", stats["plans"])


def test_repeated_failure_escalates_deliberately():
    """Escalation must come from exhausted retries, not from the round cap."""
    graph, stats = _build_offline_graph([False, False, False, False])
    try:
        out = _run_to_completion(graph, "INC-TEST-NEVER")
    finally:
        stats["restore"]()

    assert stats["executions"] >= 2, f"execute ran {stats['executions']} time(s)"
    assert out["status"] == "escalated", out["status"]
    events = [a.get("event_type") for a in out.get("audit_log") or []]
    assert "verification_exhausted" in events, events
    assert "max_rounds" not in events, events
    print("OK repeated failure escalates via verification_exhausted")


def main():
    test_verification_needs_live_evidence()
    test_verification_confirms_real_recovery()
    test_verification_without_any_signal()
    test_verification_ignores_stale_actions()
    test_heuristic_planner_escalates_after_failed_restart()
    test_heuristic_planner_will_not_blind_rollback()
    test_faults_survive_restart()
    # Graph tests last: they patch module-level nodes for the whole process.
    test_failed_verification_retries_remediation()
    test_repeated_failure_escalates_deliberately()
    print("\nAll fix regression tests passed.")


if __name__ == "__main__":
    main()
