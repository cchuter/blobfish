#!/usr/bin/env python3
"""
Walk every jobs/<trial>/<task>/agent/trajectory.json under a trial directory,
join with the trial-level result.json, and emit a per-task signal table plus
a one-page failure-mode summary.

Usage:
    python3 scripts/analyze-deepseek4-trial.py jobs/2026-05-06__12-04-21
"""
from __future__ import annotations
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


HUNG_THRESHOLD_SEC = 60
EDIT_LOOP_THRESHOLD = 5
ERROR_LOOP_THRESHOLD = 3


def load_pass_fail(trial_dir: Path) -> dict[str, float]:
    """Return {trial_dir_name: reward} from trial-level result.json."""
    result_path = trial_dir / "result.json"
    if not result_path.exists():
        return {}
    data = json.loads(result_path.read_text())
    rs = (
        data.get("stats", {})
        .get("evals", {})
        .get("cchuter__deepseek-v4-flash__terminal-bench", {})
        .get("reward_stats", {})
        .get("reward", {})
    )
    out: dict[str, float] = {}
    for reward_str, names in rs.items():
        try:
            reward = float(reward_str)
        except ValueError:
            continue
        for name in names:
            out[name] = reward
    return out


def _parse_ts(ts: str | None) -> float | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Trajectories use ISO 8601 with 'Z' suffix.
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def extract_tool_uses(trajectory: dict) -> list[dict]:
    """Return a normalized list of tool-use events from a trajectory.

    DeepSeek V4 ATIF-v1.2 schema: trajectory has top-level `steps`. Each step
    is a dict with `source` ('user'|'agent'), optional `tool_calls` list and
    `observation` (tool result). Tool name is in `extra.tool_use_name` or in
    `tool_calls[0].function_name`. Tool args are in `extra.raw_arguments` or
    `tool_calls[0].arguments`. The observation/result text lives in
    `observation.results[*].content` plus
    `extra.tool_result_metadata.tool_use_result.{stdout,stderr}`.

    Each returned dict carries a normalized shape:
        {name, input, output, is_error, ts, ts_epoch}
    """
    steps = trajectory.get("steps") or []
    tool_uses: list[dict] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        extra = s.get("extra") or {}
        tool_calls = s.get("tool_calls") or []
        # Identify tool name
        name = extra.get("tool_use_name")
        args = extra.get("raw_arguments")
        if not name and tool_calls:
            tc = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
            name = tc.get("function_name") or tc.get("name")
            if args is None:
                args = tc.get("arguments")
        if not name:
            continue

        # Normalize observation text
        out_parts: list[str] = []
        obs = s.get("observation")
        if isinstance(obs, dict):
            for r in obs.get("results") or []:
                c = r.get("content") if isinstance(r, dict) else None
                if isinstance(c, str):
                    out_parts.append(c)
        meta = (extra.get("tool_result_metadata") or {}).get("tool_use_result") or {}
        for k in ("stdout", "stderr"):
            v = meta.get(k)
            if isinstance(v, str) and v:
                out_parts.append(v)

        ts = s.get("timestamp")
        tool_uses.append(
            {
                "name": name,
                "input": args if isinstance(args, dict) else {},
                "output": "\n".join(out_parts),
                "is_error": bool(extra.get("tool_result_is_error")),
                "ts": ts,
                "ts_epoch": _parse_ts(ts),
            }
        )
    return tool_uses


def parse_seconds(ev: dict, prev_ev: dict | None) -> float | None:
    """Extract a numeric seconds value from an event, if present.

    DeepSeek V4 trajectories don't store per-tool durations directly. Fall
    back to the wall-clock delta between this step's timestamp and the prior
    step's timestamp — for Bash that's a reasonable proxy for command duration.
    """
    for k in ("duration_seconds", "duration", "elapsed_seconds"):
        v = ev.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    if prev_ev is None:
        return None
    a, b = prev_ev.get("ts_epoch"), ev.get("ts_epoch")
    if a is None or b is None:
        return None
    delta = b - a
    if delta < 0:
        return None
    return delta


def per_task_signals(trial_dir: Path, task_dir: Path, reward: float | None) -> dict:
    traj_path = task_dir / "agent" / "trajectory.json"
    if not traj_path.exists():
        return {"task": task_dir.name.split("__")[0], "trial_dir": task_dir.name, "reward": reward, "trajectory": "missing"}
    try:
        traj = json.loads(traj_path.read_text())
    except Exception as exc:
        return {"task": task_dir.name.split("__")[0], "trial_dir": task_dir.name, "reward": reward, "trajectory": f"parse-error: {exc}"}

    tool_uses = extract_tool_uses(traj)
    by_name: Counter[str] = Counter()
    edits_per_path: Counter[str] = Counter()
    long_bash = 0
    cmd_not_found_then_install = 0
    error_strings: Counter[str] = Counter()
    first_write_index = None

    for i, ev in enumerate(tool_uses):
        name = ev.get("name") or "?"
        by_name[name] += 1
        inp = ev.get("input") or {}
        if name in {"Edit", "MultiEdit"}:
            file_path = inp.get("file_path", "")
            if file_path:
                edits_per_path[file_path] += 1
        if name == "Write" and first_write_index is None:
            first_write_index = i
        if name == "Bash":
            prev = tool_uses[i - 1] if i > 0 else None
            secs = parse_seconds(ev, prev)
            if secs is not None and secs >= HUNG_THRESHOLD_SEC:
                long_bash += 1
        out = ev.get("output") or ""
        if isinstance(out, str):
            if "command not found" in out.lower():
                lookahead = tool_uses[i + 1 : i + 3]
                installed = any(
                    (la.get("name") == "Bash")
                    and ("apt-get install" in str((la.get("input") or {}).get("command", "")) or
                         "pip install" in str((la.get("input") or {}).get("command", "")))
                    for la in lookahead
                )
                if installed:
                    cmd_not_found_then_install += 1
            for line in out.splitlines():
                low = line.lower().strip()
                if low.startswith(("error", "traceback", "fatal:", "exception")):
                    error_strings[line.strip()[:160]] += 1

    repeated_errors = sum(1 for _, n in error_strings.most_common() if n >= ERROR_LOOP_THRESHOLD)
    edit_loops = [(p, n) for p, n in edits_per_path.items() if n >= EDIT_LOOP_THRESHOLD]

    return {
        "task": task_dir.name.split("__")[0],
        "trial_dir": task_dir.name,
        "reward": reward,
        "tool_calls_total": sum(by_name.values()),
        "by_tool": dict(by_name),
        "first_write_call_index": first_write_index,
        "edit_loop_paths": edit_loops,
        "long_bash_count": long_bash,
        "cmd_not_found_install_recoveries": cmd_not_found_then_install,
        "repeated_error_loops": repeated_errors,
        "top_errors": error_strings.most_common(3),
    }


def bucketize(rows: list[dict]) -> dict[str, list[str]]:
    """Group failed tasks into named failure-mode buckets."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r.get("reward") != 0.0:
            continue
        if r.get("first_write_call_index") is None:
            buckets["NEVER_WROTE_DELIVERABLE"].append(r["trial_dir"])
        if r.get("edit_loop_paths"):
            buckets["EDIT_LOOP"].append(r["trial_dir"])
        if r.get("long_bash_count", 0) >= 1:
            buckets["HUNG_COMMAND_RISK"].append(r["trial_dir"])
        if r.get("repeated_error_loops", 0) >= 1:
            buckets["PANIC_LOOP"].append(r["trial_dir"])
        if r.get("tool_calls_total", 0) >= 100:
            buckets["TOOL_CHURN"].append(r["trial_dir"])
    return dict(buckets)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: analyze-deepseek4-trial.py <trial_dir>", file=sys.stderr)
        return 2
    trial_dir = Path(sys.argv[1]).resolve()
    if not trial_dir.is_dir():
        print(f"not a directory: {trial_dir}", file=sys.stderr)
        return 2

    pass_fail = load_pass_fail(trial_dir)
    rows: list[dict] = []
    for task_dir in sorted(trial_dir.iterdir()):
        if not task_dir.is_dir() or "__" not in task_dir.name:
            continue
        reward = pass_fail.get(task_dir.name)
        try:
            rows.append(per_task_signals(trial_dir, task_dir, reward))
        except Exception as exc:
            rows.append({"task": task_dir.name, "error": str(exc)})

    buckets = bucketize(rows)
    pass_count = sum(1 for r in rows if r.get("reward") == 1.0)
    fail_count = sum(1 for r in rows if r.get("reward") == 0.0)
    err_count = sum(1 for r in rows if r.get("reward") is None)

    print(f"# DeepSeek V4 trial analysis: {trial_dir.name}\n")
    print(f"- Total task dirs: {len(rows)}")
    print(f"- Pass: {pass_count}  Fail: {fail_count}  No reward / error: {err_count}\n")
    print("## Failure-mode buckets\n")
    for name, members in sorted(buckets.items(), key=lambda x: -len(x[1])):
        print(f"### {name} ({len(members)})")
        for m in members[:20]:
            print(f"- {m}")
        print()
    print("## Per-task signal table\n")
    print("| task | reward | tool_calls | first_write | edit_loops | long_bash | install_recoveries | error_loops |")
    print("|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: (x.get("reward") if x.get("reward") is not None else -1, x.get("task", ""))):
        print(
            f"| {r.get('task')} "
            f"| {r.get('reward')} "
            f"| {r.get('tool_calls_total')} "
            f"| {r.get('first_write_call_index')} "
            f"| {len(r.get('edit_loop_paths') or [])} "
            f"| {r.get('long_bash_count')} "
            f"| {r.get('cmd_not_found_install_recoveries')} "
            f"| {r.get('repeated_error_loops')} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
