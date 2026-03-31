# Context Compression State Injection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the agent from losing track of constraints, best solutions, and evidence when context compression fires, by re-injecting accumulated state via the SessionStart hook.

**Architecture:** PostToolUse hook already tracks evidence and output state in `/tmp/blobfish-hook/`. We enhance it to also write a structured "run state summary" file. The SessionStart hook (which already fires on compact events) reads this file and injects it as `additionalContext`, so the agent sees its accumulated knowledge after compression.

**Tech Stack:** Perl (existing hook infrastructure), shell (SessionStart hook)

**Spec:** `docs/superpowers/specs/2026-03-31-tool-input-mutation-design.md` (context compression section — this is the higher-priority item from the re-evaluation)

---

## File Structure

```
harbor/src/blobfish_harbor/templates/hooks/
├── session-start-hook.sh    # MODIFY — read + inject run state after compact
├── hook_common.pl           # MODIFY — add write_run_state() function
├── post-tool-hook.pl        # MODIFY — call write_run_state() after tracking evidence
├── pre-tool-hook.pl         # (unchanged)
├── stop-hook.pl             # (unchanged)
└── task-completed-hook.pl   # (unchanged)
```

State file: `/tmp/blobfish-hook/run_state_summary` — written by PostToolUse, read by SessionStart.

---

### Task 1: Add `write_run_state` to hook_common.pl

**Files:**
- Modify: `harbor/src/blobfish_harbor/templates/hooks/hook_common.pl`

- [ ] **Step 1: Read current hook_common.pl**

Read the file to understand existing state functions and find the right insertion point.

- [ ] **Step 2: Add the `write_run_state` function**

Add after the existing `update_evidence` function. This function gathers all accumulated state from hook state files and writes a structured summary:

```perl
sub write_run_state {
    my $summary = "";

    # 1. Timing
    # current_timing() returns ($now, $elapsed, $timeout)
    my (undef, $elapsed, $timeout) = current_timing();
    if (defined $elapsed) {
        my $remaining = $timeout - $elapsed;
        $summary .= "Time: ${elapsed}s elapsed, ${remaining}s remaining.\n";
    }

    # 2. Output written
    my @output_lines = read_lines(state_path('output_written'));
    my $output = @output_lines ? $output_lines[0] : '';
    if ($output) {
        $summary .= "Output file: $output\n";
    }

    # 3. Recent evidence (last 4 lines)
    my @evidence = read_lines(state_path('recent_evidence'));
    if (@evidence) {
        $summary .= "Recent evidence:\n";
        for my $line (@evidence) {
            $summary .= "  $line\n";
        }
    }

    # 4. Pending validation
    my $pending = read_int(state_path('pending_validation'), 0);
    if ($pending) {
        $summary .= "WARNING: Output has unvalidated changes — test before finishing.\n";
    }

    # 5. Nudge count (how many times agent saw evidence without writing)
    my $nudges = read_int(state_path('nudge_count'), 0);
    if ($nudges > 0) {
        $summary .= "Evidence seen without output write: $nudges times.\n";
    }

    write_text(state_path('run_state_summary'), $summary);
}
```

- [ ] **Step 3: Verify it compiles**

```bash
cd /Users/cchuter/work/blobfish
perl -c harbor/src/blobfish_harbor/templates/hooks/hook_common.pl
```

Expected: `hook_common.pl syntax OK`

- [ ] **Step 4: Commit**

```bash
git add harbor/src/blobfish_harbor/templates/hooks/hook_common.pl
git commit -m "hooks: add write_run_state function to track accumulated state"
```

---

### Task 2: Call `write_run_state` from PostToolUse hook

**Files:**
- Modify: `harbor/src/blobfish_harbor/templates/hooks/post-tool-hook.pl`

- [ ] **Step 1: Read current post-tool-hook.pl**

Find where evidence tracking happens and the natural place to call `write_run_state`.

- [ ] **Step 2: Add call to `write_run_state` at the end of the hook**

After all existing evidence tracking and nudge logic, add a single call:

```perl
write_run_state();
```

This goes at the very end, after all state has been updated for this tool call. Every PostToolUse invocation refreshes the summary file with current state.

- [ ] **Step 3: Verify syntax**

```bash
perl -c harbor/src/blobfish_harbor/templates/hooks/post-tool-hook.pl
```

Expected: syntax OK (or warning about missing modules — the hook loads hook_common.pl at runtime via `do`)

- [ ] **Step 4: Commit**

```bash
git add harbor/src/blobfish_harbor/templates/hooks/post-tool-hook.pl
git commit -m "hooks: write run state summary after every tool call"
```

---

### Task 3: Enhance SessionStart hook to inject run state after compact

**Files:**
- Modify: `harbor/src/blobfish_harbor/templates/hooks/session-start-hook.sh`

- [ ] **Step 1: Read current session-start-hook.sh**

Understand the current output format and additionalContext injection.

- [ ] **Step 2: Rewrite to conditionally inject run state**

The hook needs to:
1. Always emit the timeout/runtime control message (existing behavior)
2. If `/tmp/blobfish-hook/run_state_summary` exists and is non-empty, append its contents to `additionalContext`

```sh
#!/bin/sh
hook_log=/logs/agent/hooks.log
timeout=${TASK_TIMEOUT_SECS:-unknown}
state_file=/tmp/blobfish-hook/run_state_summary

mkdir -p /logs/agent
printf 'SessionStart timeout=%s\n' "$timeout" >> "$hook_log"

# Build the additionalContext string
context="Runtime control: actual task timeout is ${timeout}s. Hook budget reminders are authoritative. Preserve observed evidence exactly; do not mutate observed strings to satisfy heuristics. If you have a plausible evidence-backed answer, write the required output artifact immediately; you can overwrite it later."

# Append run state if it exists (post-compression recovery)
if [ -s "$state_file" ]; then
    # Read up to 1KB to prevent bloating context window
    state=$(head -c 1024 "$state_file")
    # Use Perl for robust JSON escaping (matches hook_common.pl json_escape)
    state=$(printf '%s' "$state" | perl -pe 's/\\/\\\\/g; s/"/\\"/g; s/\n/\\n/g; s/\r/\\r/g; s/\t/\\t/g')
    context="${context}\\n\\nRun state recovered after context compression:\\n${state}"
    printf 'SessionStart injected run_state_summary\n' >> "$hook_log"
fi

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}' "$context"
```

Key points:
- The `sed` + `tr` pipeline escapes the state for JSON embedding (backslashes, quotes, tabs, newlines)
- State is appended with a clear label "Run state recovered after context compression" so the agent knows this is recovered context
- The file is not deleted after reading — it persists for subsequent compactions

- [ ] **Step 3: Test the hook locally**

```bash
# Simulate the hook with state present
mkdir -p /tmp/blobfish-hook
printf 'Time: 450s elapsed, 450s remaining.\nOutput file: /app/solution.py\nRecent evidence:\n  test passed: 3/5\n' > /tmp/blobfish-hook/run_state_summary
TASK_TIMEOUT_SECS=900 sh harbor/src/blobfish_harbor/templates/hooks/session-start-hook.sh | python3 -m json.tool
```

Expected: Valid JSON with `additionalContext` containing both the runtime control message and the run state.

```bash
# Test without state (fresh start)
rm -f /tmp/blobfish-hook/run_state_summary
TASK_TIMEOUT_SECS=900 sh harbor/src/blobfish_harbor/templates/hooks/session-start-hook.sh | python3 -m json.tool
```

Expected: Valid JSON with only the runtime control message (no run state section).

- [ ] **Step 4: Cleanup test artifacts**

```bash
rm -rf /tmp/blobfish-hook/run_state_summary
```

- [ ] **Step 5: Commit**

```bash
git add harbor/src/blobfish_harbor/templates/hooks/session-start-hook.sh
git commit -m "hooks: inject accumulated run state after context compression"
```

---

### Task 4: Integration test with a real task

Run a task that's likely to trigger context compression (high tool-call count) and verify the state survives.

**Prerequisites:**
- llama-server + cache proxy running at localhost:8081
- Docker running

- [ ] **Step 1: Run a task that generates many tool calls**

```bash
ANTHROPIC_BASE_URL=http://localhost:8081 ANTHROPIC_API_KEY=no-key \
  ./scripts/run-terminal-bench.sh \
  --backend claude --model minimax/minimax-m2.5 \
  -k 1 -n 1 -t "break-filter-js-from-html*" \
  --jobs-dir /tmp/test-compact-hooks \
  --job-name test-1
```

This task typically uses 40-80 tool calls (enough to trigger compression).

- [ ] **Step 2: Check if run_state_summary was written**

```bash
# Find the trial dir
trial=$(find /tmp/test-compact-hooks/test-1 -maxdepth 1 -type d -name "break-filter*" | head -1)
echo "Trial: $trial"

# Check hooks log for state injection
grep "run_state_summary" "$trial/agent/sessions/*/hooks.log" 2>/dev/null || echo "No injection found (may not have triggered compact)"
```

- [ ] **Step 3: Check the trajectory for compression events**

```bash
python3 -c "
import json
traj = json.load(open('$(find /tmp/test-compact-hooks/test-1 -name trajectory.json | head -1)'))
for i, step in enumerate(traj['steps']):
    msg = step.get('message', '')
    if 'compact' in str(msg).lower() or 'recovered' in str(msg).lower():
        print(f'Step {i}: {str(msg)[:200]}')
print(f'Total steps: {len(traj[\"steps\"])}')
"
```

- [ ] **Step 4: Verify no regression on a passing task**

```bash
ANTHROPIC_BASE_URL=http://localhost:8081 ANTHROPIC_API_KEY=no-key \
  ./scripts/run-terminal-bench.sh \
  --backend claude --model minimax/minimax-m2.5 \
  -k 1 -n 1 -t "largest-eigenval*" \
  --jobs-dir /tmp/test-compact-hooks \
  --job-name test-2

cat /tmp/test-compact-hooks/test-2/largest-eigenval__*/verifier/reward.txt
```

Expected: `1` (still passes).
