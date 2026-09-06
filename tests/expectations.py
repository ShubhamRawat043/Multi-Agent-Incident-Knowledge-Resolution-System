"""
Executable assertions for tests/scenarios/catalogue.yaml.

Each `expect:` tag in the catalogue maps to one predicate here. A predicate
receives the final incident state and returns (passed, detail).

Some expectations can only be judged against the live stack (they need pgvector
or real specialist reasoning). Those are registered with `live_only=True` and
are reported as SKIP when the catalogue runs in offline mode, so an offline run
never shows a red result for something it could not have tested.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEPLOY_WORDS = ("deploy", "release", "rollback", "commit", "image_tag", "v1.8", "version")
LATENCY_WORDS = ("latency", "slow", "p95", "duration", "timeout", "cpu")
DEPENDENCY_WORDS = ("dependency", "upstream", "downstream", "unreachable", "payments-api")
LOG_WORDS = ("exception", "error", "traceback", "typeerror", "runtimeerror",
             "valueerror", "stacktrace", "sentry", "500")


# ---------------------------------------------------------------- context


@dataclass
class Outcome:
    """Everything an expectation is allowed to look at."""

    state: dict
    scenario: dict
    test: dict
    mode: str = "offline"

    @property
    def event(self) -> dict:
        return self.state.get("event") or self.scenario.get("incident") or {}

    @property
    def hypotheses(self) -> list[dict]:
        return self.state.get("hypotheses") or []

    @property
    def root_cause(self) -> dict:
        return self.state.get("root_cause") or {}

    @property
    def plan(self) -> dict:
        return self.state.get("plan") or {}

    @property
    def plan_steps(self) -> list[dict]:
        return self.plan.get("steps") or []

    @property
    def actions(self) -> list[dict]:
        return self.state.get("executed_actions") or []

    @property
    def retrieved(self) -> list[dict]:
        return self.state.get("retrieved") or []

    @property
    def verification(self) -> dict | None:
        return self.state.get("verification")

    @property
    def audit(self) -> list[dict]:
        return self.state.get("audit_log") or []

    @property
    def status(self) -> str:
        return self.state.get("status") or ""

    def audit_events(self) -> list[str]:
        return [a.get("event_type") or "" for a in self.audit]

    def hypothesis_by(self, agent: str) -> dict | None:
        for h in self.hypotheses:
            if (h.get("source_agent") or "").lower() == agent.lower():
                return h
        return None

    def hypothesis_agents(self) -> set[str]:
        return {h.get("source_agent") or "" for h in self.hypotheses if h.get("source_agent")}

    def has_deploy_evidence(self) -> bool:
        """Did this incident actually come with a deploy to blame?"""
        return bool(self.event.get("deploy_tag") or self.event.get("deploy_sha"))

    def planned_actions(self) -> list[str]:
        return [s.get("action_type") or "" for s in self.plan_steps]

    def plan_history(self) -> list[list[dict]]:
        """Steps from every plan the planner produced, oldest first.

        The supervisor clears `plan` when it loops back to re-plan, so the
        state alone only ever shows the last one. The audit trail keeps them all.
        """
        rounds = [
            a.get("payload", {}).get("steps") or []
            for a in self.audit
            if a.get("event_type") == "plan_created"
        ]
        if not rounds and self.plan_steps:
            rounds = [self.plan_steps]
        return rounds

    def first_plan_steps(self) -> list[dict]:
        history = self.plan_history()
        return history[0] if history else self.plan_steps

    def all_plan_steps(self) -> list[dict]:
        steps: list[dict] = []
        for round_steps in self.plan_history():
            steps.extend(round_steps)
        if not steps:
            steps = list(self.plan_steps)
        return steps

    def all_planned_actions(self) -> list[str]:
        """Every action planned across all rounds, plus everything executed."""
        planned = [s.get("action_type") or "" for s in self.all_plan_steps()]
        for action in self.actions:
            if action.get("action_type"):
                planned.append(action["action_type"])
        return planned


def _text(items: list[dict]) -> str:
    parts = []
    for h in items:
        parts.append(str(h.get("statement") or ""))
        parts.extend(str(e) for e in (h.get("evidence") or []))
    return " ".join(parts).lower()


def _mentions(blob: str, words: tuple[str, ...]) -> list[str]:
    return [w for w in words if w in blob]


# ---------------------------------------------------------------- registry


@dataclass
class Expectation:
    name: str
    describe: str
    check: Callable[[Outcome], tuple[bool, str]]
    live_only: bool = False


REGISTRY: dict[str, Expectation] = {}


def expectation(name: str, describe: str, live_only: bool = False):
    def wrap(fn: Callable[[Outcome], tuple[bool, str]]):
        REGISTRY[name] = Expectation(name, describe, fn, live_only)
        return fn

    return wrap


# ---------------------------------------------------------------- level 1


@expectation(
    "sentry_or_stack_signal",
    "LogAnalyst produced a hypothesis grounded in the exception/stacktrace",
)
def _sentry_or_stack_signal(o: Outcome) -> tuple[bool, str]:
    h = o.hypothesis_by("LogAnalyst")
    if not h:
        return False, "no LogAnalyst hypothesis on the blackboard"
    hits = _mentions(_text([h]), LOG_WORDS)
    return bool(hits), f"LogAnalyst signals={hits or 'none'}"


@expectation(
    "restart_or_healthcheck_considered",
    "The plan proposes a restart or a healthcheck",
)
def _restart_or_healthcheck(o: Outcome) -> tuple[bool, str]:
    planned = o.all_planned_actions()
    hit = [a for a in planned if a in {"restart_service", "run_healthcheck"}]
    return bool(hit), f"planned={planned}"


@expectation("verification_runs", "The verification step actually ran")
def _verification_runs(o: Outcome) -> tuple[bool, str]:
    if o.verification is not None:
        return True, f"recovered={o.verification.get('recovered')}"
    ran = "checked" in o.audit_events()
    return ran, "no verification result in state"


@expectation(
    "metrics_and_logs_identify_anomaly",
    "MetricsAnalyst and LogAnalyst both contributed a hypothesis",
)
def _metrics_and_logs(o: Outcome) -> tuple[bool, str]:
    agents = o.hypothesis_agents()
    have = {"MetricsAnalyst", "LogAnalyst"} & agents
    return len(have) == 2, f"agents={sorted(agents)}"


@expectation(
    "metrics_analyst_latency_hypothesis",
    "MetricsAnalyst named latency (or a latency proxy) as the anomaly",
)
def _latency_hypothesis(o: Outcome) -> tuple[bool, str]:
    h = o.hypothesis_by("MetricsAnalyst")
    if not h:
        return False, "no MetricsAnalyst hypothesis"
    hits = _mentions(_text([h]), LATENCY_WORDS)
    return bool(hits), f"latency signals={hits or 'none'}"


# ---------------------------------------------------------------- level 2


@expectation(
    "root_category_db_pool_exhaustion",
    "Root cause was categorised as db_pool_exhaustion",
)
def _root_db_pool(o: Outcome) -> tuple[bool, str]:
    cat = o.root_cause.get("category")
    return cat == "db_pool_exhaustion", f"category={cat}"


@expectation(
    "no_blind_rollback_without_deploy",
    "No rollback is proposed when the incident carries no deploy evidence",
)
def _no_blind_rollback(o: Outcome) -> tuple[bool, str]:
    if o.has_deploy_evidence():
        return True, "deploy evidence present, rollback would be legitimate"
    planned = o.all_planned_actions()
    rolled = "rollback_deploy" in planned
    return not rolled, f"deploy_tag=None planned={planned}"


@expectation(
    "change_correlator_signal",
    "ChangeCorrelator connected the incident to a deploy",
)
def _change_correlator(o: Outcome) -> tuple[bool, str]:
    h = o.hypothesis_by("ChangeCorrelator")
    if not h:
        return False, "no ChangeCorrelator hypothesis"
    hits = _mentions(_text([h]), DEPLOY_WORDS)
    return bool(hits), f"deploy signals={hits or 'none'}"


@expectation(
    "rollback_requires_hitl",
    "Every rollback step is T2+ and flagged for human approval",
)
def _rollback_hitl(o: Outcome) -> tuple[bool, str]:
    rollbacks = [s for s in o.all_plan_steps() if s.get("action_type") == "rollback_deploy"]
    if not rollbacks:
        return False, "no rollback step was ever planned"
    bad = [
        s
        for s in rollbacks
        if not (s.get("requires_approval") or s.get("risk_tier") in {"T2", "T3"})
    ]
    return not bad, f"{len(rollbacks)} rollback step(s), unguarded={len(bad)}"


@expectation(
    "dependency_analyst_signal",
    "DependencyAnalyst pointed at the upstream service",
)
def _dependency_signal(o: Outcome) -> tuple[bool, str]:
    h = o.hypothesis_by("DependencyAnalyst")
    if not h:
        return False, "no DependencyAnalyst hypothesis"
    hits = _mentions(_text([h]), DEPENDENCY_WORDS)
    return bool(hits), f"dependency signals={hits or 'none'}"


@expectation(
    "remediate_dependency_first",
    "Remediation targets the failing upstream, not the service that reported",
)
def _dependency_first(o: Outcome) -> tuple[bool, str]:
    remediations = [
        s
        for s in o.first_plan_steps()
        if s.get("action_type") not in {"run_healthcheck", "gather_more_evidence"}
    ]
    if not remediations:
        return False, "plan has no remediation step"
    target = remediations[0].get("target") or ""
    reporter = o.event.get("service") or ""
    ok = "payments" in target.lower()
    return ok, f"reported_by={reporter} first_remediation_target={target}"


# ---------------------------------------------------------------- level 3


@expectation(
    "multiple_hypotheses_on_blackboard",
    "More than one specialist contributed to the blackboard",
)
def _multiple_hypotheses(o: Outcome) -> tuple[bool, str]:
    agents = o.hypothesis_agents()
    return len(agents) >= 2, f"{len(o.hypotheses)} hypotheses from {sorted(agents)}"


@expectation(
    "synthesis_ranks_hypotheses",
    "Synthesis recorded which hypotheses supported its conclusion",
)
def _synthesis_ranks(o: Outcome) -> tuple[bool, str]:
    supporting = o.root_cause.get("supporting_hypotheses") or []
    return bool(supporting), f"supporting={supporting}"


@expectation(
    "synthesis_prefers_deploy_over_cpu_alone",
    "With both deploy and CPU/latency signals present, the deploy wins",
)
def _deploy_over_cpu(o: Outcome) -> tuple[bool, str]:
    cat = o.root_cause.get("category")
    noise = _mentions(_text(o.hypotheses), LATENCY_WORDS)
    ok = cat == "bad_deployment"
    return ok, f"category={cat} competing_signals={noise or 'none'}"


# ---------------------------------------------------------------- level 4


@expectation(
    "supervisor_may_investigate_again",
    "The supervisor is able to route back to investigation",
)
def _may_investigate_again(o: Outcome) -> tuple[bool, str]:
    decisions = [
        a.get("payload", {}).get("next_action")
        for a in o.audit
        if a.get("agent") == "supervisor" and a.get("event_type") == "decision"
    ]
    n = decisions.count("investigate")
    return n >= 1, f"investigate decisions={n} rounds={o.state.get('investigation_rounds')}"


@expectation(
    "investigation_rounds_capped",
    "Investigation never exceeded INVESTIGATION_MAX_ROUNDS",
)
def _rounds_capped(o: Outcome) -> tuple[bool, str]:
    from agent.config import settings

    rounds = int(o.state.get("investigation_rounds") or 0)
    cap = settings.investigation_max_rounds
    return rounds <= cap, f"rounds={rounds} cap={cap}"


@expectation(
    "verification_can_fail",
    "Verification is capable of returning 'not recovered'",
)
def _verification_can_fail(o: Outcome) -> tuple[bool, str]:
    retries = int(o.state.get("verification_retries") or 0)
    verdict = o.verification or {}
    failed = retries >= 1 or verdict.get("recovered") is False
    return failed, f"verification_retries={retries} last_recovered={verdict.get('recovered')}"


@expectation(
    "supervisor_rethink_or_escalate",
    "A failed verification leads to a new plan or a deliberate escalation",
)
def _rethink_or_escalate(o: Outcome) -> tuple[bool, str]:
    events = o.audit_events()
    replanned = "replan_requested" in events
    escalated = o.status == "escalated" or "verification_exhausted" in events
    executions = events.count("executed")
    ok = replanned or escalated
    return ok, f"replan_requested={replanned} escalated={escalated} executions={executions}"


# ---------------------------------------------------------------- level 5


@expectation(
    "retrieval_finds_historical_rca",
    "RAG returned at least one historical RCA document",
    live_only=True,
)
def _finds_historical(o: Outcome) -> tuple[bool, str]:
    kinds = [d.get("doc_type") for d in o.retrieved]
    return "historical_rca" in kinds, f"retrieved doc_types={kinds}"


@expectation(
    "rag_is_evidence_not_dictate",
    "Retrieved docs are cited, but do not override the current evidence",
)
def _rag_not_dictate(o: Outcome) -> tuple[bool, str]:
    steps = o.all_plan_steps()
    if not steps:
        return False, "no plan to inspect"
    uncited = [s.get("action_type") for s in steps if not s.get("source_citation")]
    blind_rollback = (
        any(s.get("action_type") == "rollback_deploy" for s in steps)
        and not o.has_deploy_evidence()
    )
    ok = not uncited and not blind_rollback
    return ok, f"uncited={uncited or 'none'} blind_rollback={blind_rollback}"


# ---------------------------------------------------------------- level 6


@expectation(
    "t0_t1_may_auto_execute",
    "Low-risk (T0/T1) steps execute without stopping for approval",
)
def _low_risk_auto(o: Outcome) -> tuple[bool, str]:
    auto = [
        a
        for a in o.actions
        if a.get("risk_tier") in {"T0", "T1"} and a.get("status") in {"success", "dry_run"}
    ]
    approvals = [a for a in o.audit if a.get("event_type") == "hitl_decision"]
    approved_tiers = {
        (a.get("payload", {}).get("step") or {}).get("risk_tier") for a in approvals
    }
    ok = bool(auto) and not ({"T0", "T1"} & approved_tiers)
    return ok, f"auto_executed={[a.get('action_type') for a in auto]} approvals_for={sorted(t for t in approved_tiers if t)}"


@expectation(
    "t2_rollback_interrupts",
    "A T2 rollback pauses the graph for human approval",
)
def _t2_interrupts(o: Outcome) -> tuple[bool, str]:
    approvals = [a for a in o.audit if a.get("event_type") == "hitl_decision"]
    for a in approvals:
        step = a.get("payload", {}).get("step") or {}
        if step.get("risk_tier") in {"T2", "T3"} or step.get("action_type") == "rollback_deploy":
            return True, f"approval requested for {step.get('action_type')} ({step.get('risk_tier')})"
    return False, f"{len(approvals)} approval interrupts, none for a T2 step"


@expectation(
    "human_no_cancels_step",
    "Answering 'no' cancels the step instead of running it",
)
def _human_no_cancels(o: Outcome) -> tuple[bool, str]:
    cancelled = [a for a in o.actions if a.get("status") == "cancelled"]
    executed_rollbacks = [
        a
        for a in o.actions
        if a.get("action_type") == "rollback_deploy" and a.get("status") in {"success", "dry_run"}
    ]
    ok = bool(cancelled) and not executed_rollbacks
    return ok, f"cancelled={[a.get('action_type') for a in cancelled]} ran_anyway={len(executed_rollbacks)}"


# ---------------------------------------------------------------- level 7


@expectation(
    "max_rounds_or_retries_escalate",
    "A stuck incident escalates rather than looping forever",
)
def _escalates(o: Outcome) -> tuple[bool, str]:
    events = o.audit_events()
    reason = [e for e in events if e in {"max_rounds", "verification_exhausted"}]
    ok = o.status == "escalated" and bool(reason)
    return ok, f"status={o.status} reason={reason or 'none'}"


# ---------------------------------------------------------------- runner API


@dataclass
class Result:
    name: str
    outcome: str  # PASS / FAIL / SKIP / UNKNOWN
    detail: str
    describe: str = ""


def evaluate(tags: list[str], outcome: Outcome) -> list[Result]:
    results: list[Result] = []
    for tag in tags:
        exp = REGISTRY.get(tag)
        if exp is None:
            results.append(Result(tag, "UNKNOWN", "no assertion registered for this tag"))
            continue
        if exp.live_only and outcome.mode != "live":
            results.append(
                Result(tag, "SKIP", "needs the live stack (pgvector / real RAG)", exp.describe)
            )
            continue
        try:
            passed, detail = exp.check(outcome)
        except Exception as exc:  # an assertion must never crash the run
            results.append(Result(tag, "FAIL", f"assertion error: {exc}", exp.describe))
            continue
        results.append(Result(tag, "PASS" if passed else "FAIL", detail, exp.describe))
    return results


def known_tags() -> set[str]:
    return set(REGISTRY)
