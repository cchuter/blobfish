# Tool Input Mutation via PreToolUse Hook

Silently rewrite bad Bash commands before they execute, fixing three mechanical patterns that waste tool calls and cause timeouts.

## Core Concept

The PreToolUse hook's `updatedInput` capability lets us rewrite tool arguments before execution. The model never sees the rewrite — the command just works. This is more reliable than prompt guidance (which MiniMax ignores) or post-hoc nudges (which come too late).

Blobfish already uses this mechanism to inject `status;` before Bash commands. This extends the same pattern with three additional rewrite rules.

## The Three Rules

### Rule 1: Auto-timeout wrapping

Commands matching known long-running patterns get wrapped with `timeout <seconds>`.

| Pattern (first token) | Timeout | Examples |
|---|---|---|
| `gcc`, `g++`, `make`, `cmake`, `cargo build`, `go build` | 120s | `gcc gpt2.c -lm` → `timeout 120 gcc gpt2.c -lm` |
| `python3`, `python`, `node`, `ruby`, `java` (running scripts) | 300s | `python3 solve.py` → `timeout 300 python3 solve.py` |
| `pip install`, `apt-get install`, `npm install` | 120s | `pip3 install rdflib` → `timeout 120 pip3 install rdflib` |

**Skip conditions:**
- Command already has `timeout`, `timed`, or `TIMED_LIMIT=` prefix
- Command is a compound (`&&`, `||`, `|`, `;` present anywhere in command) — accept that compile-then-run chains like `cd /app && gcc main.c` are skipped; these are intentional agent pipelines
- Command is interactive (`python3` with no arguments, `bash` with no arguments)
- Short-lived patterns: `python3 -c '...'` one-liners, `python3 -m pytest`

**Matching logic:** Single-token patterns match the first whitespace-delimited token (`$cmd =~ /^(gcc|g\+\+|make|cmake)\b/`). Multi-token patterns use prefix match (`$cmd =~ /^cargo\s+build\b/`, `$cmd =~ /^go\s+build\b/`).

### Rule 2: pip `--break-system-packages`

Any pip install command gets `--break-system-packages` appended.

**Matches:** `pip install`, `pip3 install`, `python3 -m pip install`, `python -m pip install`

**Skip conditions:**
- `--break-system-packages` already present
- `--user` flag present (different install mode)

**Example:** `pip3 install rdflib` → `pip3 install --break-system-packages rdflib`

### Rule 3: Probe-to-install rewrite

Pure probe commands get prepended with an install attempt.

**Matches:** `command -v <tool>`, `which <tool>`, `<tool> --version`, `type <tool>` — **only when `<tool>` is in the package mapping table below**. Unmapped tools are left alone.

**Package mapping:**

| Tool | Package |
|---|---|
| `python3` | `python3` |
| `pip`, `pip3` | `python3-pip` |
| `gcc` | `gcc` |
| `make` | `make` |
| `node` | `nodejs` |
| `npm` | `npm` |
| `rg` | `ripgrep` |
| `jq` | `jq` |
| `g++` | `g++` |

**Rewrite:** `command -v python3` → `apt-get update -qq && apt-get install -y -qq python3 2>/dev/null; command -v python3`

Uses `;` (not `&&`) before the original command so it always runs regardless of install result.

**State tracking:** Uses `state_path('installed_packages')` (consistent with existing hook state convention) — read before rewriting (skip if already installed), append after rewrite. One package per line. Also tracks `state_path('apt_updated')` to avoid re-running `apt-get update` after the first install.

## Rule Application Order

1. **pip fix** (Rule 2) — simple substitution, no structural change
2. **Probe→install** (Rule 3) — may prepend install commands
3. **Timeout wrapping** (Rule 1) — wraps the outermost command

Order matters: pip fix happens inside the command, then install gets prepended, then timeout wraps everything.

**Interaction note:** When Rule 3 fires (prepending `apt-get ... ;`), Rule 1 sees the `&&` and `;` and skips timeout wrapping. This is intentional — probe commands are fast and don't need timeouts.

## Implementation

### hook_common.pl additions

```perl
sub mutate_bash_command {
    my ($cmd) = @_;
    $cmd = inject_pip_break_system_packages($cmd);
    $cmd = rewrite_probe_to_install($cmd);
    $cmd = inject_timeout($cmd);
    return $cmd;
}
```

Three pure functions, each taking and returning a command string. Plus `is_already_installed()` and `mark_installed()` for Rule 3 state.

### pre-tool-hook.pl integration — revised control flow

The critical change: when mutation modifies the command but status injection is skipped, we must still emit the mutated command via `updatedInput`. The existing code exits silently in that case, losing the mutation.

```perl
exit 0 unless $tool_name eq 'Bash';

# Step 1: Mutate the command (new)
my $original = $command;
my $mutated = mutate_bash_command($command);
my $was_mutated = ($mutated ne $original);
$command = $mutated;

# Step 2: Status injection (existing logic, unchanged)
exit 0 if $command =~ /\bstatus\b/;
my $should_inject = ... ; # existing phase/timing logic
if ($should_inject) {
    $command = "status; $command";
    emit_permission_decision('allow', 'status + mutation', $command, $context);
    exit 0;
}

# Step 3: Emit mutation even when status injection is skipped (new)
if ($was_mutated) {
    emit_permission_decision('allow', 'command mutation', $command, undef);
    exit 0;
}

# Step 4: No changes — exit silently (existing behavior)
exit 0;
```

The status-skip check (`$command =~ /\bstatus\b/`) uses the mutated command. This is correct — if the user explicitly typed a command containing "status", we don't inject another one, but we still want the mutation applied. Since `mutate_bash_command` never introduces the word "status", this is safe.

## What Doesn't Change

- Permission decisions (deny edits to tests/verifiers)
- Status injection on first bash and final-120s
- Evidence mutation guard on Write
- PostToolUse, Stop, TaskCompleted hooks
- Simple agent (no hooks)

## Leaderboard Compliance

These are agent-side runtime optimizations — equivalent to a human developer's muscle memory of typing `timeout 120 gcc` and `pip install --break-system-packages`. No modification to tasks, verifiers, timeouts, or resource limits.

## Expected Impact

Based on autoresearch trajectory analysis:
- **Rule 1 (timeout):** Prevents HUNG_COMMAND failures (3 tasks in baseline). A hung `gcc` or `python3` that consumes the entire budget gets killed after 120-300s, freeing the agent to retry or pivot.
- **Rule 2 (pip fix):** Saves 2-3 wasted tool calls per task that needs pip install (at least 10+ tasks). Agent gets working packages on first try.
- **Rule 3 (probe→install):** Saves 5-8 tool calls per task (29-64% of budget in autoresearch observations). Agent gets tools immediately instead of probing.
