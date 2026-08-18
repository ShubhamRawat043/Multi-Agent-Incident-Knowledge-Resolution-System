"""Scenario-driven fault injection controller."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def load_scenario(name: str) -> dict[str, Any]:
    path = SCENARIOS_DIR / f"{name}.json"
    if not path.exists():
        # allow bare filename
        path = SCENARIOS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Scenario not found: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def apply_scenario(scenario: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """
    Apply faults defined in a scenario JSON.
    Expected shape:
    {
      "id": "...",
      "description": "...",
      "targets": [
        {"service_url": "http://localhost:8002", "fault": "bad_deploy"}
      ],
      "traffic": {"url": "...", "method": "POST", "json": {...}, "count": 5},
      "settle_seconds": 2
    }
    """
    results: dict[str, Any] = {"faults": [], "traffic": []}

    for target in scenario.get("targets", []):
        url = target["service_url"].rstrip("/")
        fault = target["fault"]
        payload = {"mode": fault}
        if dry_run:
            results["faults"].append({"url": url, "fault": fault, "dry_run": True})
            continue
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{url}/fault", json=payload)
            results["faults"].append(
                {"url": url, "fault": fault, "status": resp.status_code, "body": resp.json()}
            )

    settle = float(scenario.get("settle_seconds", 1))
    if not dry_run and settle:
        time.sleep(settle)

    traffic = scenario.get("traffic")
    if traffic:
        count = int(traffic.get("count", 1))
        for i in range(count):
            if dry_run:
                results["traffic"].append({"i": i, "dry_run": True})
                continue
            with httpx.Client(timeout=15.0) as client:
                method = traffic.get("method", "POST").upper()
                kwargs: dict[str, Any] = {}
                if "json" in traffic:
                    kwargs["json"] = traffic["json"]
                resp = client.request(method, traffic["url"], **kwargs)
                results["traffic"].append(
                    {
                        "i": i,
                        "status": resp.status_code,
                        "body": resp.text[:500],
                    }
                )

    return results


def reset_all(urls: list[str]) -> list[dict]:
    out = []
    for url in urls:
        with httpx.Client(timeout=10.0) as client:
            resp = client.delete(f"{url.rstrip('/')}/fault")
            out.append({"url": url, "status": resp.status_code, "body": resp.json()})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject a chaos scenario")
    parser.add_argument("--scenario", required=True, help="Scenario name or file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear faults on default demo services after run",
    )
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    print(json.dumps({"scenario": scenario.get("id"), "description": scenario.get("description")}, indent=2))
    result = apply_scenario(scenario, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))

    if args.reset:
        urls = [t["service_url"] for t in scenario.get("targets", [])]
        print(json.dumps({"reset": reset_all(urls)}, indent=2))


if __name__ == "__main__":
    main()
