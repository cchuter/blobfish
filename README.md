# blobfish

<p align="center">
  <img src="blobfish_bw.svg" alt="blobfish" width="300">
</p>

Open starter agent framework for Terminal-Bench.

`blobfish` is designed so anyone can fork the repo, set their agent name to their GitHub username, and run benchmark jobs with shared org identity:

- `Agent`: your GitHub username (for example, `cchuter`)
- `Agent Org`: `teamblobfish.com`


## What is included

- The Harbor agent implementation benchmark adapter:
  `harbor/src/blobfish_harbor/agent.py`
- Prompt variants Harbor setup:
  - `harbor/src/blobfish_harbor/templates/prompt.md.j2` (full)
  - `harbor/src/blobfish_harbor/templates/prompt-slim.md.j2` (slim)
- Scripts for agent identity scaffolding and benchmark runs
- A submission metadata helper to keep leaderboard fields consistent

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)
- [Docker](https://docs.docker.com/get-docker/) (Harbor runs agents inside containers)

## Quick start

1. Fork this repository.
2. Clone your fork.
3. Create your local agent profile:

```bash
./scripts/new-agent.sh <your-github-username>
```

4. Install Harbor and the Blobfish adapter:

```bash
uv tool install harbor-bench
uv pip install --python ~/.local/share/uv/tools/harbor/bin/python -e harbor
```

5. Run a sample Terminal-Bench job:

```bash
./scripts/run-terminal-bench.sh \
  --agent-name <your-github-username> \
  --backend claude \
  --model anthropic/claude-sonnet-4-5 \
  -k 1
```

Single-task quick example:

```bash
./run-blobfish.sh anthropic/claude-sonnet-4-5 fix-git
```

6. Run the full benchmark for leaderboard submission:

```bash
./run-blobfish.sh anthropic/claude-sonnet-4-5 --leaderboard
```

This runs all tasks with 5 attempts each (`-k 5`) and 4 concurrent trials. Your agent name defaults to your system username (`$USER`) and org is set to `teamblobfish.com`. To override:

```bash
./scripts/run-terminal-bench.sh \
  --agent-name <your-github-username> \
  --backend claude \
  --model anthropic/claude-sonnet-4-5 \
  -k 5 -n 4
```

## Running with a local model

You can run benchmarks against a local inference server (llama-server, vllm-mlx, etc.) instead of the Anthropic API. The server must expose an OpenAI-compatible or Anthropic-compatible `/v1/messages` endpoint.

Set `ANTHROPIC_BASE_URL` to point at your local server (or proxy) and provide a dummy API key:

```bash
ANTHROPIC_BASE_URL=http://localhost:8081 \
ANTHROPIC_API_KEY=no-key \
  ./run-blobfish.sh minimax/minimax-m2.5 fix-git
```

Full leaderboard run against a local model (`-n 1` since you're limited to one GPU):

```bash
ANTHROPIC_BASE_URL=http://localhost:8081 \
ANTHROPIC_API_KEY=no-key \
  ./scripts/run-terminal-bench.sh \
    --backend claude \
    --model minimax/minimax-m2.5 \
    -k 5 -n 1
```

When `ANTHROPIC_BASE_URL` is set, the script automatically rewrites `localhost` to `host.docker.internal`, enables Docker host networking (`network_mode=host`), and passes the env vars into the container.

## Sample GitHub-name agent: `cchuter`

This repo includes a sample username-mapped agent profile and class:

- Profile: `agents/cchuter/agent.env`
- Class: `blobfish_harbor:CchuterAgent` (same behavior as `BlobfishAgent`)

Run it directly:

```bash
./scripts/run-terminal-bench.sh \
  --agent-import-path blobfish_harbor:CchuterAgent \
  --agent-name cchuter \
  --backend claude \
  --model anthropic/claude-sonnet-4-5 \
  -k 1
```

Prompt variants:

```bash
# Full prompt (default)
./scripts/run-terminal-bench.sh --agent-name cchuter --backend claude -k 1

# Slim prompt
./scripts/run-terminal-bench.sh --agent-name cchuter --backend claude --slim-prompt -k 1

# No prompt template
./scripts/run-terminal-bench.sh --agent-name cchuter --backend claude --no-prompt -k 1

# Single task
./scripts/run-terminal-bench.sh --agent-name cchuter --backend claude -t "fix-git*" -k 1 -n 1
```

## Leaderboard metadata contract

Blobfish standardizes metadata like this:

- `agent_name`: your GitHub username
- `agent_org`: `teamblobfish.com`
- `model_name`: from your run (for example `anthropic/claude-sonnet-4-5`)
- `model_org`: model provider (for example `Anthropic`)

Generate a clean metadata payload from a Harbor job:

```bash
./scripts/prepare-submission.py \
  --job-dir jobs/<job-id> \
  --agent-name <your-github-username> \
  --agent-org teamblobfish.com
```

This writes `jobs/<job-id>/blobfish-submission.json`.

## Repository layout

```text
harbor/
  src/blobfish_harbor/      # Harbor adapter package
scripts/
  new-agent.sh              # Bootstrap local agent identity
  run-terminal-bench.sh     # Run Harbor against terminal-bench@2.0
  prepare-submission.py     # Build leaderboard metadata payload from job results
docs/
  OSS_MIGRATION_PLAN.md     # Migration checklist from private platform to OSS
agents/
  README.md                 # Fork workflow for per-user agent profiles
```

## Brand

- Project: `blobfish`
- Organization for submissions: `teamblobfish.com`
- Public website: [teamblobfish.com](http://teamblobfish.com)
