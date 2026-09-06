"""
Drive ONE chaos scenario end to end against a running ingestion API.

The incident payload comes from the scenario file itself (`incident` block), so
`--scenario scenario_04_db_pool` really does report a pool timeout with no
deploy tag, rather than the bad-deploy payload every scenario used to send.

Usage:
  python -m tests.run_scenario_check --scenario scenario_04_db_pool
  python -m tests.run_scenario_check --scenario scenario_02_bad_deploy --approve no
  python -m tests.run_scenario_check --list

To check behavior rather than just eyeball the output, use:
  python -m tests.run_catalogue --live
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import settings  # noqa: E402
from chaos.controller import SCENARIOS_DIR, apply_scenario, load_scenario, reset_all  # noqa: E402
from tests.harness import enable_utf8_stdout  # noqa: E402


def main() -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="scenario_02_bad_deploy")
    parser.add_argument("--skip-chaos", action="store_true", help="Do not inject the fault")
    parser.add_argument(
        "--approve",
        default="yes",
        choices=["yes", "no"],
        help="How to answer a human-approval prompt",
    )
    parser.add_argument("--reset", action="store_true", help="Clear faults when finished")
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    args = parser.parse_args()

    if args.list:
        for path in sorted(SCENARIOS_DIR.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"  {path.stem:<34} {data.get('description', '')}")
        return 0

    scenario = load_scenario(args.scenario)
    payload = scenario.get("incident")
    if not payload:
        print(f"Scenario {args.scenario} has no 'incident' block to send.")
        return 1

    urls = [t["service_url"] for t in scenario.get("targets") or []]

    if not args.skip_chaos:
        print(f"Injecting: {scenario.get('id')} - {scenario.get('description')}")
        reset_all(urls)
        print(json.dumps(apply_scenario(scenario), indent=2)[:1500])

    print("\nReporting incident:")
    print(json.dumps(payload, indent=2))

    base = settings.ingestion_url.rstrip("/")
    with httpx.Client(timeout=300.0) as client:
        resp = client.post(f"{base}/incidents", json=payload)
        print("\nstatus", resp.status_code)
        result = resp.json()
        incident_id = result.get("incident_id")

        guard = 0
        while result.get("pending_interrupt") and guard < 10:
            guard += 1
            pending = result["pending_interrupt"]
            print(f"\nAPPROVAL REQUESTED: {pending.get('message')}")
            print(f"  answering: {args.approve}")
            resp = client.post(
                f"{base}/approve",
                json={
                    "incident_id": incident_id,
                    "decision": args.approve,
                    "approver": "scenario-check",
                    "role": "oncall",
                },
            )
            result = resp.json()

        detail = client.get(f"{base}/incidents/{incident_id}").json()

    root = detail.get("root_cause") or {}
    plan = detail.get("plan") or {}
    verification = detail.get("verification") or {}
    print("\n" + "=" * 70)
    print(f"incident   : {incident_id}")
    print(f"expected   : {scenario.get('expected_root_cause')}")
    print(f"root_cause : {root.get('category')} - {root.get('summary')}")
    print(f"confidence : {detail.get('confidence')}")
    print(f"plan       : {[s.get('action_type') for s in plan.get('steps') or []]}")
    print(
        "executed   : "
        f"{[(a.get('action_type'), a.get('status')) for a in detail.get('executed_actions') or []]}"
    )
    print(f"verified   : recovered={verification.get('recovered')} - {verification.get('notes')}")
    print(f"status     : {detail.get('status')}")
    print("=" * 70)

    if args.reset:
        print(json.dumps({"reset": reset_all(urls)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
