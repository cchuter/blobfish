#!/usr/bin/env python3
"""Check peak token usage for a trial to see if it hit the compression threshold."""
import json
import sys
import os

if len(sys.argv) < 2:
    print("Usage: python3 scripts/check-token-usage.py <trial-dir>")
    sys.exit(1)

trial = sys.argv[1]
output_file = os.path.join(trial, "agent", "blobfish-output.txt")

if not os.path.exists(output_file):
    print(f"Not found: {output_file}")
    sys.exit(1)

max_input = 0
total_output = 0
turns = 0

def get_tokens(usage):
    """Extract input/output tokens from a usage dict, handling both naming conventions."""
    inp = (
        (usage.get("input_tokens") or usage.get("inputTokens") or 0)
        + (usage.get("cache_read_input_tokens") or usage.get("cacheReadInputTokens") or 0)
        + (usage.get("cache_creation_input_tokens") or usage.get("cacheCreationInputTokens") or 0)
    )
    out = usage.get("output_tokens") or usage.get("outputTokens") or 0
    return inp, out

for line in open(output_file):
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue

    # Check all places usage might live
    found = None
    for candidate in [
        msg.get("usage"),
        (msg.get("message") or {}).get("usage"),
        (msg.get("result") or {}).get("usage"),
    ]:
        if isinstance(candidate, dict) and candidate:
            found = candidate
            break

    if found:
        inp, out = get_tokens(found)
        if inp > 0:
            turns += 1
            if inp > max_input:
                max_input = inp
            total_output += out

print(f"API turns: {turns}")
print(f"Peak input tokens: {max_input:,}")
print(f"Total output tokens: {total_output:,}")
print(f"Auto-compact threshold: ~163,000")
if max_input > 163000:
    print("Hit threshold: YES — compression should have triggered")
else:
    print(f"Hit threshold: NO — {163000 - max_input:,} tokens short")
