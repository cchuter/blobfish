#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def read_text(path: Path, limit: int | None = None) -> str:
    try:
        data = path.read_text(errors="replace")
    except FileNotFoundError:
        return ""
    if limit is None or len(data) <= limit:
        return data
    return data[-limit:]


def tail_lines(path: Path, n: int = 20) -> list[str]:
    text = read_text(path, limit=50000)
    if not text:
        return []
    return text.splitlines()[-n:]


def trial_dirs(job_dir: Path) -> list[Path]:
    return sorted(
        [p for p in job_dir.iterdir() if p.is_dir() and "__" in p.name],
        key=lambda p: p.name,
    )


def classify_trial(trial_dir: Path) -> tuple[str, str]:
    result_path = trial_dir / "result.json"
    reward_path = trial_dir / "verifier" / "reward.txt"

    if reward_path.exists():
        reward = read_text(reward_path).strip()
        if reward == "1":
            return "passed", "reward=1"
        if reward == "0":
            return "failed", "reward=0"
        if reward:
            return "finished", f"reward={reward}"

    if result_path.exists():
        try:
            data = json.loads(read_text(result_path))
            subtype = data.get("subtype", "")
            if subtype == "success":
                return "finished", "result=success"
            if subtype:
                return "finished", f"result={subtype}"
        except json.JSONDecodeError:
            pass
        return "finished", "result.json"

    return "running", "in-progress"


def latest_debug_line(trial_dir: Path) -> str:
    debug_dir = trial_dir / "agent" / "sessions" / "debug"
    if not debug_dir.exists():
        return ""
    files = sorted(debug_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return ""
    lines = tail_lines(files[0], 10)
    return lines[-1] if lines else ""


def latest_output_excerpt(trial_dir: Path) -> list[str]:
    output_path = trial_dir / "agent" / "blobfish-output.txt"
    return tail_lines(output_path, 12)


def top_recent_trials(trials: Iterable[Path], n: int = 5) -> list[Path]:
    return sorted(trials, key=lambda p: p.stat().st_mtime, reverse=True)[:n]


def read_job_result(job_dir: Path) -> dict | None:
    result_path = job_dir / "result.json"
    if not result_path.exists():
        return None
    try:
        return json.loads(read_text(result_path))
    except json.JSONDecodeError:
        return None


def job_is_finished(job_dir: Path) -> bool:
    data = read_job_result(job_dir)
    if not data:
        return False
    return bool(data.get("finished_at"))


def build_snapshot(job_dir: Path) -> str:
    trials = trial_dirs(job_dir)
    counts = Counter()
    details: list[tuple[Path, str, str]] = []
    job_result = read_job_result(job_dir)

    for trial in trials:
        status, note = classify_trial(trial)
        counts[status] += 1
        details.append((trial, status, note))

    lines: list[str] = []
    lines.append(f"## Snapshot {now_utc()}")
    lines.append("")
    lines.append(f"- Job: `{job_dir}`")
    lines.append(f"- Trials discovered: {len(trials)}")
    lines.append(f"- Passed: {counts['passed']}")
    lines.append(f"- Failed: {counts['failed']}")
    lines.append(f"- Finished other: {counts['finished']}")
    lines.append(f"- Running: {counts['running']}")
    if job_result:
        lines.append(f"- Job started_at: {job_result.get('started_at')}")
        lines.append(f"- Job finished_at: {job_result.get('finished_at')}")
        lines.append(f"- Harbor n_trials: {job_result.get('stats', {}).get('n_trials')}")
        lines.append(f"- Harbor n_total_trials: {job_result.get('n_total_trials')}")
        metrics = (
            job_result.get("stats", {})
            .get("evals", {})
            .get("cchuter__minimax-m2.5__terminal-bench", {})
            .get("metrics", [])
        )
        if metrics:
            lines.append(f"- Harbor mean reward: {metrics[0].get('mean')}")

    recent = top_recent_trials(trials)
    if recent:
        lines.append("")
        lines.append("### Recent trials")
        for trial in recent:
            status, note = classify_trial(trial)
            lines.append(f"- `{trial.name}`: {status} ({note})")

    running_trials = [trial for trial, status, _ in details if status == "running"]
    if running_trials:
        active = sorted(running_trials, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        lines.append("")
        lines.append(f"### Active trial: `{active.name}`")
        debug_line = latest_debug_line(active)
        if debug_line:
            lines.append(f"- Latest debug line: `{debug_line}`")
        excerpt = latest_output_excerpt(active)
        if excerpt:
            lines.append("- Tail of agent output:")
            lines.extend([f"  {line}" for line in excerpt])

    if job_result:
        lines.append("")
        lines.append("### Job result.json")
        lines.extend(
            [f"  {line}" for line in json.dumps(job_result, indent=4).splitlines()]
        )

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--interval", type=int, default=1800)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--max-snapshots", type=int, default=48)
    args = parser.parse_args()

    job_dir = args.job_dir.resolve()
    report_path = args.report or (job_dir / "monitor-report.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    header = [
        f"# Harbor Monitor Report",
        "",
        f"- Job dir: `{job_dir}`",
        f"- Started: {now_utc()}",
        f"- Interval: {args.interval}s",
        "",
    ]
    report_path.write_text("\n".join(header), encoding="utf-8")

    snapshots = 0
    while snapshots < args.max_snapshots:
        snapshot = build_snapshot(job_dir)
        with report_path.open("a", encoding="utf-8") as fh:
            fh.write(snapshot)
            fh.write("\n")

        if job_is_finished(job_dir):
            break

        snapshots += 1
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
