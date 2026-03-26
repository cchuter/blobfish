import subprocess
import glob
import os

def run_trial(
    task: str,
    iteration: int,
    jobs_dir: str,
    backend: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    project_root: str,
) -> str:
    """Run a single Harbor trial. Returns the trial directory path.
    Raises subprocess.TimeoutExpired if iteration_timeout exceeded.
    Raises subprocess.CalledProcessError if run-terminal-bench.sh exits non-zero."""
    job_name = f"iter-{iteration}"
    cmd = [
        os.path.join(project_root, "scripts/run-terminal-bench.sh"),
        "--backend", backend,
        "--model", model,
        "-k", "1", "-n", "1",
        "-t", f"{task}*",
        "--jobs-dir", jobs_dir,
        "--job-name", job_name,
    ]
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_API_KEY"] = api_key

    subprocess.run(
        cmd, env=env, timeout=timeout,
        cwd=project_root, check=True,
    )

    # Find trial directory
    pattern = os.path.join(jobs_dir, job_name, f"{task}__*")
    matches = glob.glob(pattern)
    if not matches:
        raise FileNotFoundError(f"No trial dir found matching {pattern}")
    return matches[0]


def read_reward(trial_dir: str) -> int:
    """Read verifier/reward.txt. Returns 1 (pass) or 0 (fail).
    Returns 0 if file missing."""
    reward_path = os.path.join(trial_dir, "verifier", "reward.txt")
    try:
        return int(open(reward_path).read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def run_regression(
    tasks: list[str],
    iteration: int,
    jobs_dir: str,
    backend: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    project_root: str,
) -> dict[str, int]:
    """Run all regression tasks sequentially. Returns {task: reward}."""
    results = {}
    job_name = f"regression-{iteration}"
    for task in tasks:
        try:
            cmd = [
                os.path.join(project_root, "scripts/run-terminal-bench.sh"),
                "--backend", backend,
                "--model", model,
                "-k", "1", "-n", "1",
                "-t", f"{task}*",
                "--jobs-dir", jobs_dir,
                "--job-name", job_name,
            ]
            env = os.environ.copy()
            env["ANTHROPIC_BASE_URL"] = base_url
            env["ANTHROPIC_API_KEY"] = api_key
            subprocess.run(
                cmd, env=env, timeout=timeout // len(tasks),
                cwd=project_root, check=False,
            )
            pattern = os.path.join(jobs_dir, job_name, f"{task}__*")
            matches = glob.glob(pattern)
            results[task] = read_reward(matches[0]) if matches else 0
        except subprocess.TimeoutExpired:
            results[task] = 0
    return results
