#!/usr/bin/env python3
"""Build a Blobfish leaderboard metadata payload from a Harbor job."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any


def load_results(job_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for child in sorted(job_dir.iterdir()):
        if not child.is_dir():
            continue
        result_path = child / "result.json"
        if not result_path.exists():
            continue
        try:
            results.append(json.loads(result_path.read_text()))
        except json.JSONDecodeError:
            continue
    return results


def mean_resolution_rate(results: list[dict[str, Any]]) -> float:
    per_task: dict[str, list[float]] = defaultdict(list)
    for r in results:
        task_name = r.get("task_name") or "unknown-task"
        verifier = r.get("verifier_result") or {}
        rewards = verifier.get("rewards") or {}
        reward = float(rewards.get("reward", 0.0) or 0.0)
        per_task[task_name].append(1.0 if reward > 0 else 0.0)
    if not per_task:
        return 0.0
    task_rates = [sum(v) / len(v) for v in per_task.values()]
    return sum(task_rates) / len(task_rates)


def infer_model_info(results: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for r in results:
        agent_info = r.get("agent_info") or {}
        model_info = agent_info.get("model_info") or {}
        provider = model_info.get("provider")
        name = model_info.get("name")
        if provider and name:
            return f"{provider}/{name}", provider.title()
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", required=True, help="Path to Harbor job directory")
    parser.add_argument("--agent-name", required=True, help="GitHub username")
    parser.add_argument(
        "--agent-org",
        default="teamblobfish.com",
        help="Agent organization shown on leaderboard",
    )
    parser.add_argument("--model-name", default=None, help="Optional override model name")
    parser.add_argument("--model-org", default=None, help="Optional override model org")
    parser.add_argument("--date", default=str(date.today()), help="Run date (YYYY-MM-DD)")
    args = parser.parse_args()

    job_dir = Path(args.job_dir).resolve()
    if not job_dir.is_dir():
        raise SystemExit(f"Job directory not found: {job_dir}")

    results = load_results(job_dir)
    if not results:
        raise SystemExit(f"No result.json files found in: {job_dir}")

    inferred_model_name, inferred_model_org = infer_model_info(results)
    model_name = args.model_name or inferred_model_name or "unknown"
    model_org = args.model_org or inferred_model_org or "unknown"
    accuracy = mean_resolution_rate(results)

    payload = {
        "agent_name": args.agent_name,
        "agent_org": args.agent_org,
        "model_name": model_name,
        "model_org": model_org,
        "date": args.date,
        "accuracy": round(accuracy * 100, 2),
        "accuracy_unit": "percent",
        "job_dir": str(job_dir),
    }

    output_path = job_dir / "blobfish-submission.json"
    output_path.write_text(json.dumps(payload, indent=2) + "\n")

    print("Prepared submission payload:")
    print(f"  Agent     : {payload['agent_name']}")
    print(f"  Model     : {payload['model_name']}")
    print(f"  Date      : {payload['date']}")
    print(f"  Agent Org : {payload['agent_org']}")
    print(f"  Model Org : {payload['model_org']}")
    print(f"  Accuracy  : {payload['accuracy']}%")
    print(f"  File      : {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

