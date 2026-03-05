# blobfish Harbor Adapter

Run Blobfish agents with Harbor on `terminal-bench@2.0`.

## Included agents

- `blobfish_harbor:BlobfishAgent` (generic username-driven agent)
- `blobfish_harbor:CchuterAgent` (sample GitHub-name agent)

Both use the same core logic and support:
- `prompt_variant=full` (default)
- `prompt_variant=slim`
- `use_prompt=false`

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
