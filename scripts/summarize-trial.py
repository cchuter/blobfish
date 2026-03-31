#!/usr/bin/env python3
"""Summarize a trial: pass/fail, diagnostics, and potential failure reasons."""
import json
import sys
import os
import glob

def find_trial(path):
    """Resolve a trial dir from a job dir, task glob, or direct trial path."""
    if os.path.isfile(os.path.join(path, "result.json")) and os.path.isdir(os.path.join(path, "agent")):
        return path  # direct trial dir
    # Maybe it's a job dir — find the most recent trial
    trials = sorted(glob.glob(os.path.join(path, "*__*")), key=os.path.getmtime)
    if trials:
        return trials[-1]
    # Maybe it's a pattern
    trials = sorted(glob.glob(path), key=os.path.getmtime)
    if trials:
        return trials[-1]
    return path

def main():
    if len(sys.argv) < 2:
        # Auto-detect: find the most recent job, then its most recent trial
        jobs = sorted(glob.glob("jobs/*"), key=os.path.getmtime)
        if not jobs:
            print("Usage: python3 scripts/summarize-trial.py <trial-dir|job-dir>")
            sys.exit(1)
        trial = find_trial(jobs[-1])
        print(f"Auto-detected: {trial}\n")
    else:
        trial = find_trial(sys.argv[1])

    task_name = os.path.basename(trial).rsplit("__", 1)[0] if "__" in os.path.basename(trial) else os.path.basename(trial)

    # === RESULT ===
    reward = "?"
    reward_path = os.path.join(trial, "verifier", "reward.txt")
    if os.path.exists(reward_path):
        reward = open(reward_path).read().strip()

    exception = None
    exc_path = os.path.join(trial, "exception.txt")
    if os.path.exists(exc_path):
        lines = open(exc_path).readlines()
        exception = lines[-1].strip() if lines else None

    status = "PASS" if reward == "1" else "FAIL"
    if exception and "TimeoutError" in exception:
        status = "TIMEOUT"
    elif exception and reward != "1":
        status = "ERROR"

    print(f"{'='*60}")
    print(f"Task:   {task_name}")
    print(f"Trial:  {os.path.basename(trial)}")
    print(f"Result: {status} (reward={reward})")
    if exception:
        print(f"Error:  {exception}")
    print(f"{'='*60}")

    # === TRAJECTORY DIAGNOSTICS ===
    traj_path = os.path.join(trial, "agent", "trajectory.json")
    if os.path.exists(traj_path):
        traj = json.load(open(traj_path))
        steps = traj.get("steps", [])
        total_steps = len(steps)

        tool_calls = []
        writes = []
        bash_cmds = []
        errors = []

        for step in steps:
            for tc in step.get("tool_calls", []):
                name = tc.get("function_name", "?")
                args = tc.get("arguments", {})
                tool_calls.append(name)

                if name == "Bash":
                    cmd = args.get("command", "")[:120]
                    bash_cmds.append(cmd)
                elif name == "Write":
                    path = args.get("file_path", "?")
                    writes.append(path)

            obs = step.get("observation", {})
            if obs:
                for result in obs.get("results", []):
                    content = result.get("content", "")
                    if any(x in content.lower() for x in ["error", "traceback", "segfault", "killed", "no such file"]):
                        # Extract first error-like line
                        for line in content.split("\n"):
                            if any(x in line.lower() for x in ["error", "traceback", "segfault", "killed"]):
                                errors.append(line.strip()[:120])
                                break

        # Tool call summary
        from collections import Counter
        tc_counts = Counter(tool_calls)

        print(f"\n--- Trajectory ---")
        print(f"Steps: {total_steps}  |  Tool calls: {len(tool_calls)}  |  Errors seen: {len(errors)}")
        print(f"Tool breakdown: {', '.join(f'{t}={c}' for t, c in tc_counts.most_common())}")

        # Write targets
        if writes:
            write_counts = Counter(writes)
            print(f"Files written: {', '.join(f'{p} (x{c})' for p, c in write_counts.most_common(5))}")

        # First and last bash commands
        if bash_cmds:
            print(f"\n--- Bash Commands ({len(bash_cmds)} total) ---")
            print(f"First: {bash_cmds[0]}")
            if len(bash_cmds) > 1:
                print(f"Last:  {bash_cmds[-1]}")

        # Errors
        if errors:
            print(f"\n--- Errors Observed ({len(errors)}) ---")
            seen = set()
            for e in errors[:8]:
                if e not in seen:
                    print(f"  {e}")
                    seen.add(e)

    # === VERIFIER OUTPUT ===
    verifier_stdout = os.path.join(trial, "verifier", "test-stdout.txt")
    if os.path.exists(verifier_stdout):
        content = open(verifier_stdout).read().strip()
        if content:
            # Show last 10 lines
            lines = content.split("\n")
            print(f"\n--- Verifier Output (last {min(10, len(lines))} lines) ---")
            for line in lines[-10:]:
                print(f"  {line}")

    # === TOKEN USAGE ===
    result_path = os.path.join(trial, "result.json")
    if os.path.exists(result_path):
        r = json.load(open(result_path))
        ar = r.get("agent_result", {})
        inp = ar.get("n_input_tokens", 0)
        cache = ar.get("n_cache_tokens", 0)
        out = ar.get("n_output_tokens", 0)
        if inp:
            print(f"\n--- Token Usage ---")
            print(f"Input: {inp:,}  |  Cache: {cache:,}  |  Output: {out:,}")

    # === TIMING ===
    if os.path.exists(result_path):
        r = json.load(open(result_path))
        started = r.get("started_at", "")
        finished = r.get("finished_at", "")
        if started and finished:
            from datetime import datetime
            try:
                fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
                s = datetime.strptime(started, fmt)
                f = datetime.strptime(finished, fmt)
                duration = f - s
                print(f"\n--- Timing ---")
                print(f"Duration: {duration}")
            except ValueError:
                pass

    # === DIAGNOSIS ===
    print(f"\n--- Potential Issues ---")
    issues = []

    if status == "TIMEOUT":
        if tool_calls:
            bash_count = tc_counts.get("Bash", 0)
            write_count = tc_counts.get("Write", 0)
            if write_count > 5:
                issues.append(f"REWRITE_LOOP: {write_count} Write calls — agent may be rewriting files blindly")
            if bash_count > 0 and bash_count / len(tool_calls) < 0.3:
                issues.append("LOW_BASH_RATIO: Agent spent most calls on Read/Write, not running/testing code")
            if len(tool_calls) < 10:
                issues.append("HUNG_COMMAND: Few tool calls before timeout — a command likely hung")
            elif len(tool_calls) > 60:
                issues.append("EXPLORATION_LOOP: Many tool calls — agent may be stuck in a search/retry loop")
        else:
            issues.append("HUNG_COMMAND: No tool calls recorded — agent may have hung on first command")

    if status == "FAIL" and not exception:
        if writes:
            write_counts = Counter(writes)
            max_writes = write_counts.most_common(1)[0][1] if write_counts else 0
            if max_writes > 4:
                issues.append(f"REWRITE_LOOP: Wrote same file {max_writes} times — blind rewriting without new info")
        if errors:
            issues.append(f"RUNTIME_ERRORS: {len(errors)} errors observed during execution")
        if not writes:
            issues.append("NO_OUTPUT: Agent never wrote any files")

    if status == "ERROR":
        issues.append(f"INFRASTRUCTURE: {exception}")

    if not issues:
        if status == "PASS":
            issues.append("None — task passed successfully")
        else:
            issues.append("No obvious pattern — may be a capability limit or subtle bug")

    for issue in issues:
        print(f"  * {issue}")

    print()


if __name__ == "__main__":
    main()
