#!/usr/bin/env python3
"""Autoresearch: automated agent improvement loop."""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import subprocess
import sys
import yaml
from pathlib import Path

from trajectory import load_trajectory, extract_trajectory, to_text
from researcher import propose, evaluate
from research_log import append_entry, append_skip, last_iteration, truncate_incomplete
from trial import run_trial, read_reward, run_regression


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    return yaml.safe_load(config_path.read_text())


def read_agent_files(config: dict, project_root: str) -> dict[str, str]:
    """Read current contents of all agent files."""
    files = {}
    for rel_path in config["agent_files"]:
        full_path = os.path.join(project_root, rel_path)
        files[rel_path] = open(full_path).read()
    return files


def apply_change(change: dict, project_root: str) -> None:
    """Apply a proposed change to the agent file."""
    file_path = os.path.join(project_root, change["file"])
    if "full_content" in change:
        with open(file_path, "w") as f:
            f.write(change["full_content"])
        return
    content = open(file_path).read()
    if change["old_string"] not in content:
        raise ValueError(
            f"old_string not found in {change['file']}. "
            f"First 100 chars of old_string: {change['old_string'][:100]}"
        )
    content = content.replace(change["old_string"], change["new_string"], 1)
    with open(file_path, "w") as f:
        f.write(content)


def revert_agent_files(config: dict, project_root: str) -> None:
    """Revert all agent files to last committed state."""
    for rel_path in config["agent_files"]:
        subprocess.run(
            ["git", "checkout", "--", rel_path],
            cwd=project_root, check=True,
        )


def git_commit(message: str, files: list[str], project_root: str) -> None:
    """Stage specific files and commit."""
    subprocess.run(["git", "add"] + files, cwd=project_root, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=project_root, check=True)


def git_tag(tag: str, project_root: str) -> None:
    subprocess.run(["git", "tag", tag], cwd=project_root, check=True)


def rotate_target(config: dict, current_task: str) -> str:
    """Rotate to next failing task after a BETTER verdict."""
    tasks = config["failing_tasks"]
    idx = tasks.index(current_task) if current_task in tasks else -1
    return tasks[(idx + 1) % len(tasks)]


def run_loop(config: dict, project_root: str, start_iter: int) -> None:
    """Main research loop."""
    log_path = os.path.join(os.path.dirname(__file__), "research-log.md")
    target_task = config["target_task"]
    apply_error = None
    prev_trajectory_text = None
    prev_result = None

    # Seed baseline trajectory from existing runs if available
    baseline_dirs = glob.glob(os.path.join(
        project_root, "jobs", "*", f"{target_task}__*"
    ))
    if baseline_dirs:
        baseline_dir = sorted(baseline_dirs)[-1]  # most recent
        traj_data = load_trajectory(baseline_dir)
        if traj_data:
            prev_trajectory_text = to_text(extract_trajectory(traj_data))
            prev_result = read_reward(baseline_dir)
            print(f"Loaded baseline trajectory from {baseline_dir}")

    for iteration in range(start_iter, config["max_iterations"] + 1):
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        print(f"\n{'='*60}")
        print(f"Iteration {iteration} — {timestamp}")
        print(f"Target task: {target_task}")
        print(f"{'='*60}\n")

        # --- PROPOSE ---
        print("[1/5] Proposing change...")
        agent_files = read_agent_files(config, project_root)
        research_log_text = open(log_path).read() if os.path.exists(log_path) else ""

        # Use prev trajectory or try to find a baseline
        trajectory_context = prev_trajectory_text or "(No previous trajectory available — first iteration)"

        try:
            change = propose(
                research_log=research_log_text,
                agent_files=agent_files,
                trajectory_text=trajectory_context,
                apply_error=apply_error,
                model=config["model"],
                max_tokens=config["propose_max_tokens"],
                thinking_budget=config["thinking_budget"],
            )
        except Exception as e:
            print(f"  Propose failed: {e}")
            append_skip(log_path, iteration, f"Propose API call failed: {e}")
            continue

        apply_error = None  # reset
        print(f"  Hypothesis: {change['hypothesis']}")
        print(f"  File: {change['file']}")

        # --- APPLY ---
        print("[2/5] Applying change...")
        try:
            apply_change(change, project_root)
        except ValueError as e:
            print(f"  Apply failed: {e}")
            apply_error = str(e)
            append_skip(log_path, iteration, f"Apply failed: {e}")
            continue

        # --- RUN ---
        print(f"[3/5] Running trial: {target_task}...")
        try:
            trial_dir = run_trial(
                task=target_task,
                iteration=iteration,
                jobs_dir=config["jobs_dir"],
                backend=config["harbor_backend"],
                model=config["harbor_model"],
                base_url=config["harbor_base_url"],
                api_key=config["harbor_api_key"],
                timeout=config["iteration_timeout"],
                project_root=project_root,
            )
            result = read_reward(trial_dir)
            traj_data = load_trajectory(trial_dir)
            if traj_data:
                compressed = extract_trajectory(traj_data)
                after_trajectory_text = to_text(compressed)
            else:
                after_trajectory_text = "(Trajectory not generated)"
        except subprocess.TimeoutExpired:
            print("  Trial timed out")
            result = 0
            after_trajectory_text = "(Trial timed out)"
            revert_agent_files(config, project_root)
            append_entry(log_path, {
                "iteration": iteration, "timestamp": timestamp,
                "target_task": target_task,
                "hypothesis": change["hypothesis"],
                "changed_file": change["file"],
                "diff_summary": "See propose output",
                "result": "TIMEOUT", "verdict": "NEUTRAL",
                "trajectory_analysis": "Trial timed out",
                "conclusion": "No signal", "next_direction": "Try a different approach",
            })
            git_commit(
                f"autoresearch: iteration {iteration} — TIMEOUT (log only)",
                [log_path], project_root,
            )
            continue
        except Exception as e:
            print(f"  Trial failed: {e}")
            revert_agent_files(config, project_root)
            append_entry(log_path, {
                "iteration": iteration, "timestamp": timestamp,
                "target_task": target_task,
                "hypothesis": change["hypothesis"],
                "changed_file": change["file"],
                "diff_summary": "See propose output",
                "result": "ERROR", "verdict": "NEUTRAL",
                "trajectory_analysis": f"Trial error: {e}",
                "conclusion": "No signal", "next_direction": "Investigate error",
            })
            git_commit(
                f"autoresearch: iteration {iteration} — ERROR (log only)",
                [log_path], project_root,
            )
            continue

        print(f"  Result: {'PASS' if result else 'FAIL'}")

        # --- EVALUATE ---
        print("[4/5] Evaluating change...")
        try:
            evaluation = evaluate(
                change=change,
                before_trajectory=prev_trajectory_text or "(No baseline trajectory)",
                before_result=prev_result if prev_result is not None else 0,
                after_trajectory=after_trajectory_text,
                after_result=result,
                model=config["model"],
                max_tokens=config["evaluate_max_tokens"],
                thinking_budget=config["thinking_budget"],
            )
        except Exception as e:
            print(f"  Evaluate failed: {e}")
            evaluation = {
                "verdict": "BETTER" if result == 1 else "NEUTRAL",
                "reasoning": f"Evaluate API call failed ({e}), defaulting based on pass/fail",
                "key_observations": "N/A",
                "next_direction": "Retry evaluation",
            }

        verdict = evaluation["verdict"].strip().upper()
        if verdict not in ("BETTER", "WORSE", "NEUTRAL"):
            verdict = "NEUTRAL"

        print(f"  Verdict: {verdict}")
        print(f"  Reasoning: {evaluation['reasoning']}")

        # --- LOG & GIT ---
        print("[5/5] Logging and committing...")
        entry = {
            "iteration": iteration, "timestamp": timestamp,
            "target_task": target_task,
            "hypothesis": change["hypothesis"],
            "changed_file": change["file"],
            "diff_summary": f"old_string → new_string edit" if "old_string" in change else "full rewrite",
            "result": "PASS" if result else "FAIL",
            "verdict": verdict,
            "trajectory_analysis": evaluation.get("key_observations", ""),
            "conclusion": evaluation.get("reasoning", ""),
            "next_direction": evaluation.get("next_direction", ""),
        }
        append_entry(log_path, entry)

        if verdict == "BETTER":
            commit_files = [change["file"], log_path]
            git_commit(
                f"autoresearch: iteration {iteration} — {change['hypothesis'][:72]}",
                commit_files, project_root,
            )
            target_task = rotate_target(config, target_task)
            print(f"  Rotated target to: {target_task}")
        else:
            revert_agent_files(config, project_root)
            git_commit(
                f"autoresearch: iteration {iteration} — {verdict} (log only)",
                [log_path], project_root,
            )

        # Update trajectory state for next iteration
        prev_trajectory_text = after_trajectory_text
        prev_result = result

        # --- REGRESSION GATE ---
        if iteration % config["regression_every"] == 0:
            print(f"\n{'='*60}")
            print(f"REGRESSION GATE — iteration {iteration}")
            print(f"{'='*60}\n")
            try:
                results = run_regression(
                    tasks=config["regression_tasks"],
                    iteration=iteration,
                    jobs_dir=config["jobs_dir"],
                    backend=config["harbor_backend"],
                    model=config["harbor_model"],
                    base_url=config["harbor_base_url"],
                    api_key=config["harbor_api_key"],
                    timeout=config["regression_timeout"],
                    project_root=project_root,
                )
                must_pass = {"cancel-async-tasks", "configure-git-webserver",
                             "fix-code-vulnerability", "password-recovery"}
                regressions = [t for t in must_pass if results.get(t, 0) == 0]
                if regressions:
                    print(f"  REGRESSION DETECTED: {regressions}")
                    tag_output = subprocess.run(
                        ["git", "tag", "-l", "autoresearch-gate-*"],
                        capture_output=True, text=True, cwd=project_root,
                    ).stdout.strip()
                    tags = [t for t in tag_output.split("\n") if t]
                    if tags:
                        last_tag = sorted(tags)[-1]
                        print(f"  Reverting to {last_tag}")
                        for f in config["agent_files"]:
                            subprocess.run(
                                ["git", "checkout", last_tag, "--", f],
                                cwd=project_root, check=True,
                            )
                        git_commit(
                            f"autoresearch: regression gate {iteration} — REVERTED to {last_tag}",
                            config["agent_files"] + [log_path], project_root,
                        )
                    else:
                        print("  No prior gate tag. Reverting all agent files to HEAD~1.")
                        revert_agent_files(config, project_root)
                        git_commit(
                            f"autoresearch: regression gate {iteration} — REVERTED (no prior gate)",
                            config["agent_files"] + [log_path], project_root,
                        )
                else:
                    print(f"  Gate PASSED: {results}")
                    git_tag(f"autoresearch-gate-{iteration}", project_root)
            except Exception as e:
                print(f"  Regression gate error: {e}")

    print(f"\nAutoresearch complete. {config['max_iterations']} iterations done.")


def main():
    parser = argparse.ArgumentParser(description="Autoresearch for Blobfish")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last completed iteration")
    parser.add_argument("--regression-only", action="store_true",
                        help="Run regression gate only")
    args = parser.parse_args()

    config = load_config()
    project_root = str(Path(__file__).resolve().parent.parent.parent)
    log_path = os.path.join(os.path.dirname(__file__), "research-log.md")

    if args.regression_only:
        results = run_regression(
            tasks=config["regression_tasks"],
            iteration=0,
            jobs_dir=config["jobs_dir"],
            backend=config["harbor_backend"],
            model=config["harbor_model"],
            base_url=config["harbor_base_url"],
            api_key=config["harbor_api_key"],
            timeout=config["regression_timeout"],
            project_root=project_root,
        )
        print("Regression results:")
        for task, reward in sorted(results.items()):
            print(f"  {task}: {'PASS' if reward else 'FAIL'}")
        return

    start_iter = 1
    if args.resume:
        revert_agent_files(config, project_root)
        truncate_incomplete(log_path)
        start_iter = last_iteration(log_path) + 1
        print(f"Resuming from iteration {start_iter}")

    # Create branch if not on one
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=project_root,
    ).stdout.strip()
    expected_branch = f"autoresearch/{config['target_task']}"
    if branch != expected_branch:
        subprocess.run(
            ["git", "checkout", "-b", expected_branch],
            cwd=project_root, check=False,  # may already exist
        )
        subprocess.run(
            ["git", "checkout", expected_branch],
            cwd=project_root, check=True,
        )

    run_loop(config, project_root, start_iter)


if __name__ == "__main__":
    main()
