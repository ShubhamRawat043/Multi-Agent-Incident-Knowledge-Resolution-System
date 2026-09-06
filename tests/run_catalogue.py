"""
Run the behavioral test catalogue and check every `expect:` tag for real.

  python -m tests.run_catalogue                  # all 16 tests, offline
  python -m tests.run_catalogue --test T05       # one test
  python -m tests.run_catalogue --level 6        # one level
  python -m tests.run_catalogue --verbose        # show the audit trail per test
  python -m tests.run_catalogue --live           # drive the real running stack

Offline mode needs nothing running: the graph is compiled for real, but the
specialists, RAG, Docker tools and demo services are simulated. Live mode
injects the chaos fault, drives traffic, and posts a real incident to the
ingestion API.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chaos.controller import load_scenario  # noqa: E402
from tests.expectations import Outcome, evaluate, known_tags  # noqa: E402
from tests.harness import enable_utf8_stdout, live_run, offline_run  # noqa: E402

CATALOGUE = ROOT / "tests" / "scenarios" / "catalogue.yaml"

MARK = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "UNKNOWN": "????"}


def load_catalogue() -> list[dict]:
    data = yaml.safe_load(CATALOGUE.read_text(encoding="utf-8"))
    return data.get("tests") or []


def summarize_state(state: dict) -> str:
    root = state.get("root_cause") or {}
    plan = state.get("plan") or {}
    verification = state.get("verification") or {}
    steps = [s.get("action_type") for s in plan.get("steps") or []]
    actions = [
        f"{a.get('action_type')}:{a.get('status')}" for a in state.get("executed_actions") or []
    ]
    return (
        f"    root_cause : {root.get('category')} (confidence {root.get('confidence')})\n"
        f"    plan       : {steps or 'none'}\n"
        f"    executed   : {actions or 'none'}\n"
        f"    verified   : recovered={verification.get('recovered')} "
        f"retries={state.get('verification_retries')}\n"
        f"    status     : {state.get('status')}"
    )


def run_one(entry: dict, mode: str, verbose: bool) -> tuple[str, list]:
    scenario = load_scenario(entry["scenario"])
    approve = str(entry.get("approval", "approve")).lower() not in {"reject", "no", "deny"}

    runner = live_run if mode == "live" else offline_run
    state = runner(scenario, approve=approve)

    outcome = Outcome(state=state, scenario=scenario, test=entry, mode=mode)
    results = evaluate(list(entry.get("expect") or []), outcome)

    print(f"\n{entry['id']}  [level {entry.get('level')}]  {entry.get('name')}")
    print(f"    scenario   : {entry['scenario']}  (approval: {'yes' if approve else 'no'})")
    print(summarize_state(state))
    if entry.get("notes"):
        print(f"    notes      : {entry['notes']}")
    for r in results:
        print(f"      [{MARK[r.outcome]}] {r.name}")
        print(f"             {r.detail}")
    if verbose:
        print("    audit trail:")
        for a in state.get("audit_log") or []:
            print(f"      {str(a.get('agent')):<12} {a.get('event_type')}")

    verdict = "PASS"
    if any(r.outcome == "FAIL" for r in results):
        verdict = "FAIL"
    elif any(r.outcome == "UNKNOWN" for r in results):
        verdict = "UNKNOWN"
    return verdict, results


def main() -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", help="Run a single catalogue id, e.g. T05")
    parser.add_argument("--level", type=int, help="Run only tests at this level")
    parser.add_argument("--live", action="store_true", help="Drive the real running stack")
    parser.add_argument("--verbose", action="store_true", help="Print the audit trail")
    args = parser.parse_args()

    mode = "live" if args.live else "offline"
    entries = load_catalogue()
    if args.test:
        entries = [e for e in entries if e["id"].upper() == args.test.upper()]
    if args.level:
        entries = [e for e in entries if e.get("level") == args.level]
    if not entries:
        print("No catalogue tests matched that filter.")
        return 1

    # Fail loudly if the catalogue names an expectation nobody implemented.
    missing = sorted(
        {t for e in entries for t in (e.get("expect") or [])} - known_tags()
    )

    from agent.config import settings

    print("=" * 78)
    print(f"BEHAVIORAL CATALOGUE  -  {len(entries)} test(s)  -  mode: {mode}")
    print(f"DRY_RUN={settings.dry_run}  supervisor_max_rounds={settings.supervisor_max_rounds}")
    if mode == "offline":
        print("Offline: real graph, simulated specialists / RAG / Docker / demo services.")
    else:
        print(f"Live: driving {settings.ingestion_url} with real chaos injection.")
    if missing:
        print(f"WARNING: no assertion registered for: {missing}")
    print("=" * 78)

    tally: dict[str, str] = {}
    for entry in entries:
        try:
            verdict, _ = run_one(entry, mode, args.verbose)
        except Exception:
            verdict = "ERROR"
            print(f"\n{entry['id']}  {entry.get('name')}")
            traceback.print_exc()
        tally[entry["id"]] = verdict

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for entry in entries:
        verdict = tally.get(entry["id"], "?")
        print(f"  [{verdict:<7}] {entry['id']}  {entry.get('name')}")

    failed = [k for k, v in tally.items() if v not in {"PASS"}]
    print("-" * 78)
    print(f"  {len(tally) - len(failed)}/{len(tally)} catalogue tests passed")
    if failed:
        print(f"  not passing: {', '.join(failed)}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
