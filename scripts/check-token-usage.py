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

max_input = 0
total_output = 0
turns = 0

for line in open(output_file):
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
        usage = msg.get("usage", {})
        inp = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        out = usage.get("output_tokens", 0)
        if inp > 0:
            turns += 1
            if inp > max_input:
                max_input = inp
            total_output += out
    except (json.JSONDecodeError, AttributeError):
        pass

print(f"API turns: {turns}")
print(f"Peak input tokens: {max_input:,}")
print(f"Total output tokens: {total_output:,}")
print(f"Auto-compact threshold: ~163,000")
if max_input > 163000:
    print("Hit threshold: YES — compression should have triggered")
else:
    print(f"Hit threshold: NO — {163000 - max_input:,} tokens short")
