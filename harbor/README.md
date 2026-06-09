# blobfish Harbor Adapter

Run Blobfish agents with Harbor on `terminal-bench@2.0`.

## Included agents

- `blobfish_harbor:BlobfishAgent` (generic username-driven agent)
- `blobfish_harbor:BlobfishPiAgent` (Pi coding-agent wrapper for DS4/local Anthropic endpoints)
- `blobfish_harbor:BlobfishSimpleAgent` (minimal baseline-style agent)
- `blobfish_harbor:CchuterAgent` (sample GitHub-name agent)

Both use the same core logic and support:
- `prompt_variant=auto` (default; resolves to `full` for most models, `minimax` for MiniMax models, `qwen` for Qwen models, and `deepseek` for DeepSeek models)
- `prompt_variant=full`
- `prompt_variant=slim`
- `prompt_variant=minimax`
- `prompt_variant=qwen`
- `prompt_variant=deepseek`
- `use_prompt=false`

For `prompt_variant=minimax`, `prompt_variant=qwen`, or `prompt_variant=deepseek`, `BlobfishAgent` applies both a variant-specific prompt template and project `CLAUDE.md`. Other variants use the default project `CLAUDE.md`.

`BlobfishAgent` can also target DeepSeek's Anthropic-compatible API directly
with `backend=deepseek`. This keeps the Claude Code harness and Blobfish hooks,
but sets `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`,
`ANTHROPIC_AUTH_TOKEN`, DeepSeek model aliases, and
`CLAUDE_CODE_EFFORT_LEVEL`.

`BlobfishSimpleAgent` is intentionally minimal:

- no Claude hooks
- no `.claude` project rules
- no `.claude` project skills
- default `prompt_variant=full`
- default project `CLAUDE.md`

## Install

From repository root:

```bash
uv tool install harbor-bench
uv pip install --python ~/.local/share/uv/tools/harbor/bin/python -e harbor
```

## Run

```bash
./scripts/run-terminal-bench.sh \
  --agent-name <github-username> \
  --backend claude \
  --model anthropic/claude-sonnet-4-5 \
  -k 1
```

DeepSeek API:

```bash
DEEPSEEK_API_KEY=<key> ./scripts/run-terminal-bench.sh \
  --agent-name <github-username> \
  --backend deepseek \
  --model 'deepseek-v4-pro[1m]' \
  --prompt-variant deepseek \
  --deepseek-effort max \
  -k 1 -n 1
```

## Agent import path

```text
blobfish_harbor:BlobfishAgent
```

Minimal baseline agent:

```text
blobfish_harbor:BlobfishSimpleAgent
```

Sample username agent:

```text
blobfish_harbor:CchuterAgent
```

Pi wrapper:

```text
blobfish_harbor:BlobfishPiAgent
```

The Pi wrapper installs `@earendil-works/pi-coding-agent`, writes a DS4
`anthropic-messages` model entry, runs Pi in JSON mode, and loads a small
extension that clamps `max_tokens`/thinking budget and blocks exploratory tools
after an early no-artifact gate.

You can pin Claude Code versions through Harbor agent kwargs, for example:

```bash
./scripts/run-terminal-bench.sh \
  --agent-profile simple \
  --claude-code-version 2.1.63 \
  --backend claude \
  --model minimax/minimax-m2.5 \
  -k 1 -n 1
```
