# blobfish Harbor Adapter

Run Blobfish agents with Harbor on `terminal-bench@2.0`.

## Included agents

- `blobfish_harbor:BlobfishAgent` (generic username-driven agent)
- `blobfish_harbor:CchuterAgent` (sample GitHub-name agent)

Both use the same core logic and support:
- `prompt_variant=auto` (default; resolves to `full` for most models and `minimax-m2.5` for MiniMax M2.5)
- `prompt_variant=full`
- `prompt_variant=slim`
- `prompt_variant=minimax-m2.5`
- `use_prompt=false`

For `prompt_variant=minimax-m2.5`, Blobfish applies both a MiniMax-specific prompt template and a MiniMax-specific project `CLAUDE.md`. Other variants use the default project `CLAUDE.md`.

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

Sample username agent:

```text
blobfish_harbor:CchuterAgent
```
