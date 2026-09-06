"""
Two ways to drive a chaos scenario through the agent and get a final state back.

offline_run()  - compiles the real graph with the outside world simulated:
                 in-memory checkpointer, canned specialist hypotheses, keyword
                 RAG over the local knowledge/ folder, and a fake demo target
                 whose fault clears (or does not) exactly like the real one.
                 No Docker, no Postgres, no OpenAI, no cost.

live_run()     - drives the real stack: injects the fault, generates traffic,
                 posts the incident to the ingestion API, answers the approval
                 prompt, and reads the finished incident back.

Both return the same shape, so tests/expectations.py can judge either one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def enable_utf8_stdout() -> None:
    """Scenario text contains arrows and dashes; the Windows console is cp1252."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


KNOWLEDGE_DIR = ROOT / "knowledge"
DOC_TYPE_DIRS = {"sop": "sops", "runbook": "runbooks", "historical_rca": "historical_rca"}

# Faults a process restart genuinely cures (mirrors demo_target/faults/injectors.py).
RESTART_CLEARED_FAULTS = {"db_pool_exhaustion", "memory_leak"}


# ---------------------------------------------------------------- fixtures

def _h(agent: str, statement: str, evidence: list[str], confidence: float) -> dict:
    return {
        "statement": statement,
        "evidence": evidence,
        "confidence": confidence,
        "source_agent": agent,
    }


# What the four specialists would report for each injected fault. Used only in
# offline mode; live mode gets these from the real LLM-backed specialists.
SPECIALIST_FIXTURES: dict[str, list[dict]] = {
    "exception": [
        _h("LogAnalyst",
           "RuntimeError raised inside the payments-api /pay handler",
           ["RuntimeError: Injected application exception", "frame main.py:52 in pay"],
           0.78),
        _h("MetricsAnalyst",
           "payments-api error rate jumped to 0.85 with flat latency",
           ["http_error_rate=0.85", "p95 unchanged at 0.12s"],
           0.62),
        _h("ChangeCorrelator",
           "no code change correlates with the incident window",
           ["no release in the last 24h"],
           0.30),
        _h("DependencyAnalyst",
           "upstream services are healthy; the failure is local to payments-api",
           ["orders-api healthy"],
           0.40),
    ],
    "bad_deploy": [
        _h("LogAnalyst",
           "TypeError in payment processing, first seen right after the v1.8 release",
           ["TypeError: unexpected NoneType for amount", "first seen 14:02"],
           0.85),
        _h("MetricsAnalyst",
           "error rate and p95 latency both spiked at 14:02",
           ["http_error_rate=0.85", "p95 rose to 2.5s"],
           0.70),
        _h("ChangeCorrelator",
           "payments-api deploy v1.8 shipped at 14:00, two minutes before the first error",
           ["deploy_tag=v1.8", "sha 9f3c2ab", "previous good tag v1.7"],
           0.90),
        _h("DependencyAnalyst",
           "upstream services healthy; the fault is internal to payments-api",
           ["orders-api only fails while calling payments-api"],
           0.40),
    ],
    "latency": [
        _h("LogAnalyst",
           "no exceptions logged; requests complete but slowly",
           ["zero new issues in the window"],
           0.35),
        _h("MetricsAnalyst",
           "payments-api p95 latency rose from 0.12s to 2.54s",
           ["http_request_duration_seconds p95=2.54", "no error rate change"],
           0.80),
        _h("ChangeCorrelator",
           "no code change correlates with the slowdown",
           ["no release in the last 24h"],
           0.30),
        _h("DependencyAnalyst",
           "orders-api is slow only while waiting on payments-api",
           ["orders-api p95 tracks payments-api p95"],
           0.50),
    ],
    "db_pool_exhaustion": [
        _h("LogAnalyst",
           "ConnectionPoolTimeout raised on every /pay call",
           ["TimeoutError: ConnectionPoolTimeout: pool at 100% capacity (in_use=20)"],
           0.80),
        _h("MetricsAnalyst",
           "db_connections_in_use is pinned at the pool maximum",
           ["db_connections_in_use=20", "no free connections for 6m"],
           0.75),
        _h("ChangeCorrelator",
           "no code change in the incident window",
           ["no release in the last 24h"],
           0.30),
        _h("DependencyAnalyst",
           "database saturation is local to payments-api",
           ["orders-api healthy apart from downstream errors"],
           0.40),
    ],
    "dependency_failure": [
        _h("LogAnalyst",
           "orders-api errors: upstream payments-api unavailable on every order",
           ["Upstream dependency unavailable", "orders-api returning 503"],
           0.75),
        _h("MetricsAnalyst",
           "orders-api error rate 0.9 while payments-api is unreachable",
           ["http_error_rate=0.9 on orders-api"],
           0.70),
        _h("ChangeCorrelator",
           "no code change correlates with the outage",
           ["no release in the last 24h"],
           0.30),
        _h("DependencyAnalyst",
           "payments-api is the failing upstream dependency for orders-api",
           ["payments-api unreachable from orders-api", "orders-api healthy in isolation"],
           0.85),
    ],
    "memory_leak": [
        _h("LogAnalyst",
           "no exceptions; the process memory grows monotonically",
           ["no new issues in the window"],
           0.35),
        _h("MetricsAnalyst",
           "payments-api memory rose from 50MB to 355MB over 30m with no plateau",
           ["process_memory_bytes_sim=355000000", "no GC recovery"],
           0.80),
        _h("ChangeCorrelator",
           "no code change correlates with the growth",
           ["no release in the last 24h"],
           0.30),
        _h("DependencyAnalyst",
           "no upstream involvement in the growth pattern",
           ["orders-api memory flat"],
           0.30),
    ],
    # Deliberately low confidence: a genuinely ambiguous incident, which is what
    # T09 (low confidence -> re-investigate -> capped) is meant to exercise.
    "config_error": [
        _h("LogAnalyst",
           "ValueError: configuration mismatch on the payment path",
           ["invalid DATABASE_URL / API key"],
           0.35),
        _h("MetricsAnalyst",
           "error rate elevated but the pattern is not conclusive",
           ["http_error_rate=0.85"],
           0.30),
        _h("ChangeCorrelator",
           "v1.8 rolled out 20 minutes earlier, but it touched no payment logic",
           ["deploy_tag=v1.8", "diff does not cover the failing path"],
           0.35),
        _h("DependencyAnalyst",
           "no upstream signal either way",
           [],
           0.25),
    ],
}


def fixtures_for(scenario: dict) -> list[dict]:
    targets = scenario.get("targets") or []
    fault = targets[0]["fault"] if targets else "exception"
    return [dict(h) for h in SPECIALIST_FIXTURES.get(fault, SPECIALIST_FIXTURES["exception"])]


def fault_of(scenario: dict) -> str:
    targets = scenario.get("targets") or []
    return targets[0]["fault"] if targets else "none"


# ---------------------------------------------------------------- fake world


class FakeTarget:
    """Stands in for the demo service, with the same cure rules as the real one."""

    def __init__(self, fault: str, dry_run: bool):
        self.fault = fault
        self.dry_run = dry_run
        self.log: list[str] = []

    def restart(self) -> None:
        self.log.append("restart")
        if not self.dry_run and self.fault in RESTART_CLEARED_FAULTS:
            self.fault = "none"

    def rollback(self) -> None:
        self.log.append("rollback")
        if not self.dry_run:
            self.fault = "none"

    @property
    def error_rate(self) -> float:
        return 0.0 if self.fault == "none" else 1.0


def _fake_tools(world: FakeTarget):
    """Replacements for the Docker tools, keeping the real execution_node logic."""
    from agent.config import settings

    def result(action: str, tier: str, target: str, detail: str, **extra) -> dict:
        return {
            "action": action,
            "target": target,
            "status": "dry_run" if settings.dry_run else "success",
            "detail": detail,
            "dry_run": settings.dry_run,
            "risk_tier": tier,
            **extra,
        }

    class _Tool:
        def __init__(self, fn):
            self._fn = fn

        def invoke(self, args: dict) -> dict:
            return self._fn(args)

    def healthcheck(args):
        return {
            "action": "run_healthcheck",
            "target": args.get("target"),
            "status": "success",
            "http_status": 200,
            "body": {"status": "ok", "fault_mode": world.fault},
            "dry_run": False,
            "risk_tier": "T0",
        }

    def restart(args):
        world.restart()
        return result("restart_service", "T1", args.get("target"), "restart simulated")

    def rollback(args):
        world.rollback()
        return result(
            "rollback_deploy", "T2", args.get("target"),
            f"rollback to {args.get('image_tag')} simulated",
            args={"image_tag": args.get("image_tag")},
        )

    def scale(args):
        return result("scale_service", "T1", args.get("target"), "scale simulated")

    def clear(args):
        return result("clear_cache", "T1", args.get("target"), "cache clear simulated")

    def flag(args):
        return result("toggle_feature_flag", "T1", args.get("target"), "flag simulated")

    return {
        "run_healthcheck": _Tool(healthcheck),
        "restart_service": _Tool(restart),
        "rollback_deploy": _Tool(rollback),
        "scale_service": _Tool(scale),
        "clear_cache": _Tool(clear),
        "toggle_feature_flag": _Tool(flag),
    }


def _local_similarity_search(query: str, k: int = 3, service=None, doc_type=None, **_):
    """Keyword search over the knowledge/ folder, standing in for pgvector."""
    subdir = DOC_TYPE_DIRS.get(doc_type or "")
    roots = [KNOWLEDGE_DIR / subdir] if subdir else [
        KNOWLEDGE_DIR / d for d in DOC_TYPE_DIRS.values()
    ]
    terms = {w for w in query.lower().replace("_", " ").split() if len(w) > 3}
    hits = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            blob = f"{path.stem} {text}".lower()
            score = sum(1 for t in terms if t in blob) / max(len(terms), 1)
            if score <= 0:
                continue
            hits.append(
                {
                    "content": text[:1200],
                    "source": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "doc_type": doc_type
                    or next((d for d, s in DOC_TYPE_DIRS.items() if s == root.name), "unknown"),
                    "service": service,
                    "score": round(min(score, 0.99), 3),
                }
            )
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:k]


# ---------------------------------------------------------------- offline


def offline_run(scenario: dict, approve: bool = True) -> dict:
    """Run one scenario end to end with every external dependency simulated."""
    # Offline runs simulate a world where remediation never really lands, so the
    # verification-failure and escalation paths stay reachable. Pin DRY_RUN here
    # rather than inheriting it from .env: flipping DRY_RUN for a live demo must
    # not change what these deterministic tests assert.
    from agent.config import settings as _settings

    _settings.dry_run = True

    import agent.checkpointer as checkpointer
    from langgraph.checkpoint.memory import MemorySaver

    checkpointer.build_checkpointer = lambda: MemorySaver()

    # Import before neutralizing the LLM: the specialist modules build their
    # ChatOpenAI client at import time.
    import agent.graph as gmod
    import agent.investigate as investigate
    import agent.llm as llm_mod
    import agent.reporting as reporting
    import agent.triage as triage
    import agent.verification as verification
    import rag.store as rag_store
    import tools.docker_tools as docker_tools
    from agent.config import settings
    from langgraph.types import Command

    def boom(*_a, **_k):
        raise RuntimeError("LLM disabled in offline mode")

    llm_mod.get_fast_llm = boom
    llm_mod.get_strong_llm = boom
    for name in ("supervisor", "synthesis", "planner", "critic"):
        mod = __import__(f"agent.{name}", fromlist=["x"])
        for attr in ("get_fast_llm", "get_strong_llm"):
            if hasattr(mod, attr):
                setattr(mod, attr, boom)

    # Specialists: canned, scenario-appropriate hypotheses.
    hypotheses = fixtures_for(scenario)
    by_agent = {h["source_agent"]: h for h in hypotheses}
    investigate.run_log_analyst = lambda ctx: dict(by_agent["LogAnalyst"])
    investigate.run_metrics_analyst = lambda ctx: dict(by_agent["MetricsAnalyst"])
    investigate.run_change_correlator = lambda ctx: dict(by_agent["ChangeCorrelator"])
    investigate.run_dependency_analyst = lambda ctx: dict(by_agent["DependencyAnalyst"])

    # No Postgres.
    triage.find_open_by_fingerprint = lambda fp, **kw: None
    triage.match_known_issue = lambda event: None
    reporting.upsert_incident = lambda *a, **k: None
    reporting.write_learning = lambda *a, **k: None

    # RAG over local files instead of pgvector.
    rag_store.similarity_search = _local_similarity_search

    # A simulated demo target, and tools that act on it.
    world = FakeTarget(fault_of(scenario), settings.dry_run)
    docker_tools.ACTION_TOOL_MAP.update(_fake_tools(world))
    import agent.execution as execution

    execution.ACTION_TOOL_MAP = docker_tools.ACTION_TOOL_MAP

    # Verification probes the simulated target rather than the network.
    class _Prom:
        @staticmethod
        def invoke(_args):
            return {"data": {"result": []}}

    def fake_probe(service, url):
        rate = world.error_rate
        failed = int(round(4 * rate))
        return {
            "sent": 4,
            "failed": failed,
            "error_rate": rate,
            "errors": [f"HTTP 500: fault_mode={world.fault}"] if failed else [],
        }

    verification._health = lambda url: {"ok": True, "body": {"fault_mode": world.fault}}
    verification._probe = fake_probe
    verification.get_service_error_rate = _Prom

    graph = gmod.build_graph(checkpointer=MemorySaver())
    incident_id = f"INC-{scenario.get('id', 'scenario')}"
    state = gmod.initial_state(incident_id, dict(scenario.get("incident") or {}))
    config = {"configurable": {"thread_id": incident_id}}

    out = graph.invoke(state, config=config)
    guard = 0
    while (out.get("__interrupt__") or []) and guard < 10:
        guard += 1
        out = graph.invoke(
            Command(
                resume={
                    "decision": "yes" if approve else "no",
                    "approver": "catalogue-runner",
                    "role": "oncall",
                }
            ),
            config=config,
        )

    out = dict(out)
    out["_world"] = {"fault": world.fault, "actions": world.log}
    return out


# ---------------------------------------------------------------- live


def live_run(scenario: dict, approve: bool = True, timeout: float = 300.0) -> dict:
    """Inject the fault against the running stack and drive a real incident."""
    import httpx

    from agent.config import settings
    from chaos.controller import apply_scenario, reset_all

    base = settings.ingestion_url.rstrip("/")
    urls = [t["service_url"] for t in scenario.get("targets") or []]

    reset_all(urls)
    apply_scenario(scenario)

    payload = dict(scenario.get("incident") or {})
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{base}/incidents", json=payload)
        resp.raise_for_status()
        result = resp.json()
        incident_id = result["incident_id"]

        guard = 0
        while result.get("pending_interrupt") and guard < 10:
            guard += 1
            resp = client.post(
                f"{base}/approve",
                json={
                    "incident_id": incident_id,
                    "decision": "yes" if approve else "no",
                    "approver": "catalogue-runner",
                    "role": "oncall",
                },
            )
            resp.raise_for_status()
            result = resp.json()

        detail = client.get(f"{base}/incidents/{incident_id}").json()

    reset_all(urls)
    detail.setdefault("event", payload)
    return detail
