# blobfish Harbor Adapter

Run Blobfish agents with Harbor on `terminal-bench@2.0`.

## Included agents

- `blobfish_harbor:BlobfishAgent` (generic username-driven agent)
- `blobfish_harbor:BlobfishSimpleAgent` (minimal baseline-style agent)
- `blobfish_harbor:CchuterAgent` (sample GitHub-name agent)

Both use the same core logic and support:
- `prompt_variant=auto` (default; resolves to `full` for most models, `minimax` for MiniMax models, and `qwen` for Qwen models)
- `prompt_variant=full`
- `prompt_variant=slim`
- `prompt_variant=minimax`
- `prompt_variant=qwen`
- `use_prompt=false`

For `prompt_variant=minimax` or `prompt_variant=qwen`, `BlobfishAgent` applies both a variant-specific prompt template and project `CLAUDE.md`. Other variants use the default project `CLAUDE.md`.

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

You can pin Claude Code versions through Harbor agent kwargs, for example:

```bash
./scripts/run-terminal-bench.sh \
  --agent-profile simple \
  --claude-code-version 2.1.63 \
  --backend claude \
  --model minimax/minimax-m2.5 \
  -k 1 -n 1
```
