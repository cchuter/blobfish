#!/usr/bin/env python3
"""Check peak token usage for a trial to see if it hit the compression threshold."""
import json
import sys
import os

if len(sys.argv) < 2:
    print("Usage: python3 scripts/check-token-usage.py <trial-dir>")
    print("Example: python3 scripts/check-token-usage.py jobs/2026-03-31__12-07-36/break-filter-js-from-html__GkvWzM3")
    sys.exit(1)

trial = sys.argv[1]
output_file = os.path.join(trial, "agent", "blobfish-output.txt")

if not os.path.exists(output_file):
    print(f"Not found: {output_file}")
    sys.exit(1)

# First, show the first few lines to understand the format
print("=== First 5 lines of blobfish-output.txt ===")
with open(output_file) as f:
    for i, line in enumerate(f):
        if i >= 5:
            break
        line = line.strip()
        if line:
            try:
                msg = json.loads(line)
                print(f"Line {i}: type={msg.get('type', '?')} keys={list(msg.keys())[:8]}")
            except json.JSONDecodeError:
                print(f"Line {i}: (not JSON) {line[:100]}")
        else:
            print(f"Line {i}: (empty)")

print()

# Search for usage data in all possible locations
max_input = 0
total_output = 0
turns = 0
usage_locations = set()

for line in open(output_file):
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue

    # Check top-level usage
    usage = msg.get("usage", {})
    if usage and any(k in usage for k in ("input_tokens", "inputTokens")):
        usage_locations.add("top-level")
        inp = (
            usage.get("input_tokens", 0) or usage.get("inputTokens", 0)
            + usage.get("cache_read_input_tokens", 0) or usage.get("cacheReadInputTokens", 0)
            + usage.get("cache_creation_input_tokens", 0) or usage.get("cacheCreationInputTokens", 0)
        )
        out = usage.get("output_tokens", 0) or usage.get("outputTokens", 0)
        if inp > 0:
            turns += 1
            if inp > max_input:
                max_input = inp
            total_output += out

    # Check nested in message or result
    for key in ("message", "result", "data"):
        nested = msg.get(key, {})
        if isinstance(nested, dict):
            nested_usage = nested.get("usage", {})
            if nested_usage and any(k in nested_usage for k in ("input_tokens", "inputTokens")):
                usage_locations.add(f"nested.{key}.usage")
                inp = (
                    nested_usage.get("input_tokens", 0) or nested_usage.get("inputTokens", 0)
                    + nested_usage.get("cache_read_input_tokens", 0) or nested_usage.get("cacheReadInputTokens", 0)
                    + nested_usage.get("cache_creation_input_tokens", 0) or nested_usage.get("cacheCreationInputTokens", 0)
                )
                out = nested_usage.get("output_tokens", 0) or nested_usage.get("outputTokens", 0)
                if inp > 0:
                    turns += 1
                    if inp > max_input:
                        max_input = inp
                    total_output += out

    # Check for costUSD or modelUsage (Claude Code stream-json format)
    model_usage = msg.get("modelUsage", {})
    if model_usage:
        usage_locations.add("modelUsage")
        for model_name, mu in model_usage.items():
            inp = mu.get("inputTokens", 0) + mu.get("cacheReadInputTokens", 0) + mu.get("cacheCreationInputTokens", 0)
            out = mu.get("outputTokens", 0)
            if inp > 0:
                turns += 1
                if inp > max_input:
                    max_input = inp
                total_output += out

print(f"Usage data found at: {usage_locations or 'NOWHERE'}")
print(f"API turns: {turns}")
print(f"Peak input tokens: {max_input:,}")
print(f"Total output tokens: {total_output:,}")
print(f"Auto-compact threshold: ~163,000")
if max_input > 163000:
    print("Hit threshold: YES — compression should have triggered")
else:
    print(f"Hit threshold: NO — {163000 - max_input:,} tokens short")
