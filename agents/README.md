# Agent Profiles

Each fork should create a profile for the GitHub username that will appear as the agent name.

## Create a profile

```bash
./scripts/new-agent.sh <github-username>
```

This creates:

```text
agents/<github-username>/agent.env
```

The repository already includes a sample profile at:

```text
agents/cchuter/agent.env
```

Example:

```env
BLOBFISH_AGENT_NAME=cchuter
BLOBFISH_AGENT_ORG=teamblobfish.com
```

Use that profile when running:

```bash
./scripts/run-terminal-bench.sh --agent-name cchuter
```
