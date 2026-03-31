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

# First pass: dump a sample usage block so we can see the actual format
sample_shown = False

for line in open(output_file):
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue

    # Walk the entire JSON tree looking for any dict with token-related keys
    def find_usage(obj, path=""):
        global max_input, total_output, turns, sample_shown
        if isinstance(obj, dict):
            # Check if this dict has token keys
            token_keys = [k for k in obj if "token" in k.lower()]
            if token_keys and not sample_shown:
                print(f"=== Sample usage at {path or 'root'} ===")
                print(json.dumps({k: obj[k] for k in token_keys}, indent=2))
                print()
                sample_shown = True

            if token_keys:
                inp = 0
                for k in obj:
                    kl = k.lower()
                    if "input_token" in kl or "inputtoken" in kl:
                        inp += (obj[k] or 0)
                out = 0
                for k in obj:
                    kl = k.lower()
                    if "output_token" in kl or "outputtoken" in kl:
                        out += (obj[k] or 0)
                if inp > 0:
                    turns += 1
                    if inp > max_input:
                        max_input = inp
                    total_output += out

            for k, v in obj.items():
                find_usage(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                find_usage(v, f"{path}[{i}]")

    find_usage(msg)

print(f"API turns: {turns}")
print(f"Peak input tokens: {max_input:,}")
print(f"Total output tokens: {total_output:,}")
print(f"Auto-compact threshold: ~163,000")
if max_input > 163000:
    print("Hit threshold: YES — compression should have triggered")
else:
    print(f"Hit threshold: NO — {163000 - max_input:,} tokens short")
