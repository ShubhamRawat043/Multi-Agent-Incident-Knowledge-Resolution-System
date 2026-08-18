"""Optional end-to-end scenario check against a running ingestion API."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.config import settings  # noqa: E402
from chaos.controller import apply_scenario, load_scenario  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="scenario_02_bad_deploy")
    parser.add_argument("--skip-chaos", action="store_true")
    args = parser.parse_args()

    if not args.skip_chaos:
        scenario = load_scenario(args.scenario)
        print("Injecting:", scenario.get("id"))
        print(json.dumps(apply_scenario(scenario), indent=2))

    payload = {
        "service": "payments-api",
        "title": f"Scenario {args.scenario}",
        "stacktrace": "TypeError in payment processing",
        "deploy_tag": "v1.8",
        "kind": "error",
        "source": "chaos",
    }
    url = f"{settings.ingestion_url.rstrip('/')}/incidents"
    with httpx.Client(timeout=300.0) as client:
        resp = client.post(url, json=payload)
        print("status", resp.status_code)
        print(json.dumps(resp.json(), indent=2)[:4000])


if __name__ == "__main__":
    main()
