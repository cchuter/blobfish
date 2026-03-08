"""Blobfish Harbor adapter for Terminal-Bench runs.

Open-source Harbor adapter with support for prompt variants and
backend/model selection.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
from harbor.models.agent.context import AgentContext
from harbor.models.trial.result import AgentInfo, ModelInfo

DEFAULT_AGENT_ORG = "teamblobfish.com"
TEMPLATES_DIR = Path(__file__).parent / "templates"


class BlobfishAgent(BaseInstalledAgent):
    """Harbor agent that runs Claude or Codex in headless mode for benchmarks."""

    def __init__(
        self,
        backend: str = "claude",
        agent_name: str | None = None,
        agent_org: str = DEFAULT_AGENT_ORG,
        routing_table: str | Path | None = None,
        default_model: str | None = None,
        codex_model: str = "gpt-5.3-codex",
        openai_base_url: str | None = None,
        openai_api_key: str | None = None,
        reasoning_effort: str | None = "high",
        max_thinking_tokens: int | None = None,
        use_prompt: bool = True,
        prompt_variant: str = "auto",
        *args,
        **kwargs,
    ):
        requested_prompt_variant = _normalize_prompt_variant(prompt_variant)
        resolved_prompt_variant = _resolve_prompt_variant(
            requested_prompt_variant,
            kwargs.get("model_name") or default_model,
        )
        if not use_prompt or str(use_prompt).lower() == "false":
            kwargs["prompt_template_path"] = None
        else:
            kwargs.setdefault("prompt_template_path", _prompt_template_path(resolved_prompt_variant))

        super().__init__(*args, **kwargs)

        self._agent_name = _resolve_agent_name(agent_name)
        self._agent_org = (agent_org or DEFAULT_AGENT_ORG).strip() or DEFAULT_AGENT_ORG

        self._default_backend = (backend or "claude").strip().lower()
        if self._default_backend not in {"claude", "codex"}:
            raise ValueError("backend must be one of: claude, codex")
        self._prompt_variant = requested_prompt_variant

        self._default_model_selector = (default_model or "").strip() or None
        self._codex_model = (codex_model or "").strip() or None
        self._reasoning_effort = reasoning_effort
        self._max_thinking_tokens = max_thinking_tokens

        self._routing: dict[str, dict] = {}
        if routing_table:
            path = Path(routing_table)
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    if isinstance(data, dict):
                        self._routing = data
                except json.JSONDecodeError:
                    self._routing = {}

        self._openai_base_url = (openai_base_url or "").strip() or None
        self._openai_api_key = (openai_api_key or "").strip() or None

    @staticmethod
    def name() -> str:
        return "blobfish"

    @property
    def _install_agent_template_path(self) -> Path:
        backend, _ = self._resolve_backend_and_model()
        if backend == "codex":
            return TEMPLATES_DIR / "install-codex.sh.j2"
        return TEMPLATES_DIR / "install-claude.sh.j2"

    def to_agent_info(self) -> AgentInfo:
        model_info = None
        if self._parsed_model_name and self._parsed_model_provider:
            model_info = ModelInfo(
                name=self._parsed_model_name,
                provider=self._parsed_model_provider,
            )
        return AgentInfo(
            name=self._agent_name,
            version=self.version() or "unknown",
            model_info=model_info,
        )

    def _get_task_name(self) -> str | None:
        trial_dir_name = self.logs_dir.parent.name
        if "__" in trial_dir_name:
            return trial_dir_name.rsplit("__", 1)[0]
        return None

    def _resolve_backend_and_model(self) -> tuple[str, str | None]:
        backend = self._default_backend
        model_name = self.model_name

        if model_name and "codex" in model_name.lower():
            backend = "codex"

        backend, model_name = _apply_selector(
            self._default_model_selector,
            backend=backend,
            model_name=model_name,
        )

        task_name = self._get_task_name()
        route = self._routing.get(task_name, {}) if task_name else {}
        if isinstance(route, dict):
            selectors = [route.get("backend"), route.get("model_name"), route.get("model")]
            for selector in selectors:
                backend, model_name = _apply_selector(
                    selector,
                    backend=backend,
                    model_name=model_name,
                )

        if _looks_incompatible_model_for_backend(model_name, backend):
            model_name = None

        if backend == "codex" and not model_name and self._codex_model:
            model_name = self._codex_model

        return backend, model_name

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        escaped = shlex.quote(instruction)
        backend, model_name = self._resolve_backend_and_model()
        if backend == "codex":
            return self._create_codex_run_commands(escaped, model_name=model_name)
        return self._create_claude_run_commands(escaped, model_name=model_name)

    def _create_claude_run_commands(
        self, escaped_instruction: str, model_name: str | None = None
    ) -> list[ExecInput]:
        env: dict[str, str] = {
            "BLOBFISH_AGENT_NAME": self._agent_name,
            "BLOBFISH_AGENT_ORG": self._agent_org,
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        }

        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        if base_url:
            env["ANTHROPIC_BASE_URL"] = _rewrite_localhost_for_docker(base_url) or base_url

        if not env.get("ANTHROPIC_API_KEY") and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            token = _read_oauth_token()
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token

        env = {k: v for k, v in env.items() if v}

        if model_name:
            selected_model = model_name.split("/", 1)[1] if "/" in model_name else model_name
            env["ANTHROPIC_MODEL"] = selected_model

        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["IS_SANDBOX"] = "1"
        env["FORCE_AUTO_BACKGROUND_TASKS"] = "1"
        env["ENABLE_BACKGROUND_TASKS"] = "1"
        env["CLAUDE_CONFIG_DIR"] = "/logs/agent/sessions"

        if self._max_thinking_tokens is not None:
            env["MAX_THINKING_TOKENS"] = str(self._max_thinking_tokens)
        elif "MAX_THINKING_TOKENS" in os.environ:
            env["MAX_THINKING_TOKENS"] = os.environ["MAX_THINKING_TOKENS"]
        else:
            env["MAX_THINKING_TOKENS"] = "10000"

        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = os.environ.get(
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "48000"
        )
        env["TASK_START_EPOCH"] = str(int(time.time()))
        task_timeout_sec = _resolve_task_timeout_sec(self._get_task_name())
        if task_timeout_sec is not None:
            env["TASK_TIMEOUT_SECS"] = str(task_timeout_sec)
        existing_path = os.environ.get("PATH", "")
        env["PATH"] = f"/tmp/blobfish-bin:{existing_path}" if existing_path else "/tmp/blobfish-bin"
        prompt_variant = _resolve_prompt_variant(self._prompt_variant, model_name)
        claude_md_text = _project_claude_md(prompt_variant)
        claude_md = shlex.quote(claude_md_text)

        _legacy_setup_cmd = (
            "mkdir -p $CLAUDE_CONFIG_DIR/debug $CLAUDE_CONFIG_DIR/projects/-app "
            "$CLAUDE_CONFIG_DIR/shell-snapshots $CLAUDE_CONFIG_DIR/statsig "
            "$CLAUDE_CONFIG_DIR/todos /tmp/blobfish-bin && "
            "if [ -d ~/.claude/skills ]; then "
            "cp -r ~/.claude/skills $CLAUDE_CONFIG_DIR/skills 2>/dev/null || true; "
            "fi && "
            f"printf %s {claude_md} > $CLAUDE_CONFIG_DIR/projects/-app/CLAUDE.md && "
            "cat > /tmp/blobfish-bin/timed <<'EOF'\n"
            "#!/bin/sh\n"
            "s=$(date +%s)\n"
            "timeout \"${TIMED_LIMIT:-120}\" \"$@\"\n"
            "rc=$?\n"
            "if [ \"$rc\" -eq 124 ]; then\n"
            "  echo \"[TIMING] KILLED after $(($(date +%s)-s))s\"\n"
            "else\n"
            "  echo \"[TIMING] $(($(date +%s)-s))s (exit $rc)\"\n"
            "fi\n"
            "exit \"$rc\"\n"
            "EOF\n"
            "chmod +x /tmp/blobfish-bin/timed && "
            "cat > /tmp/run_state.md <<'EOF'\n"
            "# Run state\n"
            "- Goal:\n"
            "- Best known result:\n"
            "- Next step:\n"
            "EOF\n"
            "cat > /tmp/blobfish-bin/status <<'EOF'\n"
            "#!/bin/sh\n"
            "now=$(date +%s)\n"
            "start=${TASK_START_EPOCH:-$now}\n"
            "timeout=${TASK_TIMEOUT_SECS:-unknown}\n"
            "elapsed=$((now-start))\n"
            "echo \"Elapsed: ${elapsed}s\"\n"
            "if [ \"$timeout\" != \"unknown\" ]; then\n"
            "  remaining=$((timeout-elapsed))\n"
            "  echo \"Remaining: ${remaining}s of ${timeout}s\"\n"
            "fi\n"
            "if [ -f /tmp/run_state.md ]; then\n"
            "  echo \"=== /tmp/run_state.md ===\"\n"
            "  cat /tmp/run_state.md\n"
            "fi\n"
            "EOF\n"
            "chmod +x /tmp/blobfish-bin/status && "
            "cat > /tmp/blobfish-bin/session-start-hook <<'EOF'\n"
            "#!/usr/bin/perl\n"
            "use strict;\n"
            "use warnings;\n"
            "use JSON::PP qw(encode_json);\n"
            "use File::Path qw(make_path);\n"
            "\n"
            "my $hook_log = '/logs/agent/hooks.log';\n"
            "my $timeout = $ENV{TASK_TIMEOUT_SECS} // 'unknown';\n"
            "my $msg = \"Runtime control: actual task timeout is ${timeout}s. Hook budget reminders are authoritative. Preserve observed evidence exactly; do not mutate observed strings to satisfy heuristics. If you have a plausible evidence-backed answer, write the required output artifact immediately; you can overwrite it later.\";\n"
            "make_path('/logs/agent');\n"
            "open my $fh, '>>', $hook_log;\n"
            "print {$fh} \"SessionStart timeout=$timeout\\n\";\n"
            "close $fh;\n"
            "print encode_json({ hookSpecificOutput => { hookEventName => 'SessionStart', additionalContext => $msg } });\n"
            "EOF\n"
            "chmod +x /tmp/blobfish-bin/session-start-hook && "
            "mkdir -p /tmp/blobfish-hook && "
            "cat > /tmp/blobfish-bin/pre-tool-hook <<'EOF'\n"
            "#!/usr/bin/perl\n"
            "use strict;\n"
            "use warnings;\n"
            "use JSON::PP qw(decode_json encode_json);\n"
            "use File::Path qw(make_path);\n"
            "\n"
            "my $hook_log = '/logs/agent/hooks.log';\n"
            "my $state_dir = '/tmp/blobfish-hook';\n"
            "my $phase_file = \"$state_dir/phase\";\n"
            "my $fail_file = \"$state_dir/failures\";\n"
            "my $first_file = \"$state_dir/first_bash\";\n"
            "my $evidence_file = \"$state_dir/recent_evidence\";\n"
            "my $measured_path_file = \"$state_dir/measured_path\";\n"
            "my $measured_backup_file = \"$state_dir/measured_backup\";\n"
            "my $measured_notice_file = \"$state_dir/measured_notice\";\n"
            "\n"
            "sub read_int {\n"
            "    my ($path, $default) = @_;\n"
            "    $default //= 0;\n"
            "    return $default unless -f $path;\n"
            "    open my $fh, '<', $path or return $default;\n"
            "    my $text = <$fh>;\n"
            "    close $fh;\n"
            "    return ($text // '') =~ /(-?\\d+)/ ? int($1) : $default;\n"
            "}\n"
            "\n"
            "sub write_text {\n"
            "    my ($path, $text) = @_;\n"
            "    open my $fh, '>', $path or die \"write $path: $!\";\n"
            "    print {$fh} $text;\n"
            "    close $fh;\n"
            "}\n"
            "\n"
            "sub read_lines {\n"
            "    my ($path) = @_;\n"
            "    return () unless -f $path;\n"
            "    open my $fh, '<', $path or return ();\n"
            "    my @lines = <$fh>;\n"
            "    close $fh;\n"
            "    chomp @lines;\n"
            "    return grep { defined $_ && $_ ne '' } @lines;\n"
            "}\n"
            "\n"
            "sub strong_tokens {\n"
            "    my (@lines) = @_;\n"
            "    my %seen;\n"
            "    my @tokens;\n"
            "    for my $line (@lines) {\n"
            "        while ($line =~ /([A-Z0-9]{8,})/g) {\n"
            "            my $token = $1;\n"
            "            next unless $token =~ /[A-Z]/ && $token =~ /\\d/;\n"
            "            next if $seen{$token}++;\n"
            "            push @tokens, $token;\n"
            "            return @tokens if @tokens >= 4;\n"
            "        }\n"
            "    }\n"
            "    return @tokens;\n"
            "}\n"
            "\n"
            "sub denied_edit_path {\n"
            "    my ($tool_name, $tool_input) = @_;\n"
            "    return '' unless $tool_name =~ /^(Write|Edit|MultiEdit)$/;\n"
            "    my $path = $tool_input->{file_path};\n"
            "    return '' unless defined $path;\n"
            "    for my $part ('/tests/', '/verifier/', '/.claude/', '/CLAUDE.md') {\n"
            "        return $path if index($path, $part) >= 0;\n"
            "    }\n"
            "    return '';\n"
            "}\n"
            "\n"
            "sub mutated_evidence_reason {\n"
            "    my ($tool_name, $tool_input) = @_;\n"
            "    return '' unless $tool_name eq 'Write';\n"
            "    my $path = $tool_input->{file_path} // '';\n"
            "    return '' unless index($path, '/app/') == 0;\n"
            "    my $content = $tool_input->{content} // '';\n"
            "    my @candidates = grep { /[A-Z]/ && /\\d/ } ($content =~ /([A-Z0-9]{8,})/g);\n"
            "    return '' unless @candidates == 1;\n"
            "    my $candidate = $candidates[0];\n"
            "    my @tokens = grep { length($_) >= 10 } strong_tokens(read_lines($evidence_file));\n"
            "    return '' unless @tokens >= 2;\n"
            "    my @missing = grep { index($candidate, $_) < 0 } @tokens;\n"
            "    return '' unless @missing;\n"
            "    my $token_summary = join(' || ', @tokens[0 .. (@tokens > 2 ? 2 : $#tokens)]);\n"
            "    return \"The content you are writing mutates or drops exact observed token fragments. Preserve observed evidence exactly before writing. Recent exact tokens: $token_summary.\";\n"
            "}\n"
            "\n"
            "sub measured_overwrite_reason {\n"
            "    my ($tool_name, $tool_input) = @_;\n"
            "    return '' unless $tool_name eq 'Write';\n"
            "    my $path = $tool_input->{file_path} // '';\n"
            "    return '' unless index($path, '/app/') == 0;\n"
            "    my @measured_path = read_lines($measured_path_file);\n"
            "    return '' unless @measured_path && $measured_path[0] eq $path;\n"
            "    my @notice = read_lines($measured_notice_file);\n"
            "    return '' if @notice && $notice[0] eq $path;\n"
            "    write_text($measured_notice_file, \"$path\\n\");\n"
            "    my @backup = read_lines($measured_backup_file);\n"
            "    my $backup = @backup ? $backup[0] : 'the measured artifact snapshot';\n"
            "    return \"You are overwriting a previously measured /app artifact at $path. Preserve the measured version before replacement. A snapshot is available at $backup. If the new variant regresses, restore the measured version instead of throwing it away.\";\n"
            "}\n"
            "\n"
            "my $raw = do { local $/; <STDIN> };\n"
            "my $payload = length($raw) ? decode_json($raw) : {};\n"
            "my $tool_name = $payload->{tool_name} // '';\n"
            "my $tool_input = ref($payload->{tool_input}) eq 'HASH' ? $payload->{tool_input} : {};\n"
            "my $blocked_path = denied_edit_path($tool_name, $tool_input);\n"
            "if ($blocked_path ne '') {\n"
            "    make_path('/logs/agent');\n"
            "    open my $fh, '>>', $hook_log;\n"
            "    print {$fh} \"PreToolUse deny tool=$tool_name path=$blocked_path\\n\";\n"
            "    close $fh;\n"
            "    print encode_json({ hookSpecificOutput => { hookEventName => 'PreToolUse', permissionDecision => 'deny', permissionDecisionReason => 'Do not modify tests, verifiers, or Claude settings unless the task explicitly requires it.' } });\n"
            "    exit 0;\n"
            "}\n"
            "my $mutated_reason = mutated_evidence_reason($tool_name, $tool_input);\n"
            "if ($mutated_reason ne '') {\n"
            "    make_path('/logs/agent');\n"
            "    open my $fh, '>>', $hook_log;\n"
            "    print {$fh} \"PreToolUse deny_mutated_evidence tool=$tool_name\\n\";\n"
            "    close $fh;\n"
            "    print encode_json({ hookSpecificOutput => { hookEventName => 'PreToolUse', permissionDecision => 'deny', permissionDecisionReason => $mutated_reason } });\n"
            "    exit 0;\n"
            "}\n"
            "my $measured_reason = measured_overwrite_reason($tool_name, $tool_input);\n"
            "if ($measured_reason ne '') {\n"
            "    make_path('/logs/agent');\n"
            "    open my $fh, '>>', $hook_log;\n"
            "    print {$fh} \"PreToolUse deny_measured_overwrite tool=$tool_name\\n\";\n"
            "    close $fh;\n"
            "    print encode_json({ hookSpecificOutput => { hookEventName => 'PreToolUse', permissionDecision => 'deny', permissionDecisionReason => $measured_reason } });\n"
            "    exit 0;\n"
            "}\n"
            "exit 0 unless $tool_name eq 'Bash';\n"
            "my $command = $tool_input->{command} // '';\n"
            "exit 0 if $command =~ /\\bstatus\\b/;\n"
            "my $now = time;\n"
            "my $start = int($ENV{TASK_START_EPOCH} // $now);\n"
            "my $timeout = int($ENV{TASK_TIMEOUT_SECS} // 0);\n"
            "my $elapsed = $now - $start;\n"
            "my $remaining = $timeout ? ($timeout - $elapsed) : 0;\n"
            "my $phase = 0;\n"
            "if ($timeout > 0) {\n"
            "    my $pct = int(($elapsed * 100) / $timeout);\n"
            "    if ($remaining < 120) {\n"
            "        $phase = 3;\n"
            "    } elsif ($pct >= 75) {\n"
            "        $phase = 2;\n"
            "    } elsif ($pct >= 50) {\n"
            "        $phase = 1;\n"
            "    }\n"
            "}\n"
            "my $last_phase = read_int($phase_file, -1);\n"
            "my $consecutive_failures = read_int($fail_file, 0);\n"
            "my $should_inject = (!-f $first_file) || ($phase > $last_phase) || ($consecutive_failures >= 2);\n"
            "exit 0 unless $should_inject;\n"
            "write_text($first_file, \"1\\n\");\n"
            "write_text($phase_file, \"$phase\\n\");\n"
            "write_text($fail_file, \"0\\n\") if $consecutive_failures >= 2;\n"
            "my $new_command = \"status; $command\";\n"
            "make_path('/logs/agent');\n"
            "open my $fh, '>>', $hook_log;\n"
            "print {$fh} \"PreToolUse inject_status phase=$phase elapsed=$elapsed remaining=$remaining failures=$consecutive_failures\\n\";\n"
            "close $fh;\n"
            "print encode_json({ hookSpecificOutput => { hookEventName => 'PreToolUse', permissionDecision => 'allow', permissionDecisionReason => 'Injected status checkpoint before Bash command', updatedInput => { command => $new_command }, additionalContext => 'Use the status output as the authoritative budget/current-state snapshot for your next decision.' } });\n"
            "EOF\n"
            "chmod +x /tmp/blobfish-bin/pre-tool-hook && "
            "cat > /tmp/blobfish-bin/post-tool-hook <<'EOF'\n"
            "#!/usr/bin/perl\n"
            "use strict;\n"
            "use warnings;\n"
            "use JSON::PP qw(decode_json encode_json);\n"
            "use File::Path qw(make_path);\n"
            "\n"
            "my $hook_log = '/logs/agent/hooks.log';\n"
            "my $state_dir = '/tmp/blobfish-hook';\n"
            "my $fail_file = \"$state_dir/failures\";\n"
            "my $output_file = \"$state_dir/output_written\";\n"
            "my $pending_validation_file = \"$state_dir/pending_validation\";\n"
            "my $stop_block_file = \"$state_dir/stop_blocked\";\n"
            "my $evidence_file = \"$state_dir/recent_evidence\";\n"
            "my $measured_path_file = \"$state_dir/measured_path\";\n"
            "my $measured_backup_file = \"$state_dir/measured_backup\";\n"
            "my $measured_notice_file = \"$state_dir/measured_notice\";\n"
            "my $measured_snapshot = \"$state_dir/measured_output.snapshot\";\n"
            "my @validation_markers = ('/tests/', 'pytest', 'unittest', 'cargo test', 'go test', 'npm test', 'pnpm test', 'yarn test', 'bun test', 'ctest', 'make test', 'verify', 'cat /app/', 'sed -n ', 'grep ');\n"
            "\n"
            "sub read_int {\n"
            "    my ($path, $default) = @_;\n"
            "    $default //= 0;\n"
            "    return $default unless -f $path;\n"
            "    open my $fh, '<', $path or return $default;\n"
            "    my $text = <$fh>;\n"
            "    close $fh;\n"
            "    return ($text // '') =~ /(-?\\d+)/ ? int($1) : $default;\n"
            "}\n"
            "\n"
            "sub write_text {\n"
            "    my ($path, $text) = @_;\n"
            "    open my $fh, '>', $path or die \"write $path: $!\";\n"
            "    print {$fh} $text;\n"
            "    close $fh;\n"
            "}\n"
            "\n"
            "sub read_lines {\n"
            "    my ($path) = @_;\n"
            "    return () unless -f $path;\n"
            "    open my $fh, '<', $path or return ();\n"
            "    my @lines = <$fh>;\n"
            "    close $fh;\n"
            "    chomp @lines;\n"
            "    return grep { defined $_ && $_ ne '' } @lines;\n"
            "}\n"
            "\n"
            "sub mark_pending_validation {\n"
            "    write_text($pending_validation_file, \"1\\n\");\n"
            "    write_text($stop_block_file, \"0\\n\");\n"
            "}\n"
            "\n"
            "sub clear_pending_validation {\n"
            "    write_text($pending_validation_file, \"0\\n\");\n"
            "    write_text($stop_block_file, \"0\\n\");\n"
            "}\n"
            "\n"
            "sub snapshot_measured_artifact {\n"
            "    my ($tool_name) = @_;\n"
            "    return unless $tool_name eq 'Bash';\n"
            "    return unless -f $output_file;\n"
            "    my @paths = read_lines($output_file);\n"
            "    return unless @paths;\n"
            "    my $path = $paths[0];\n"
            "    return unless defined $path && $path ne '' && -f $path;\n"
            "    open my $in, '<', $path or return;\n"
            "    local $/;\n"
            "    my $content = <$in>;\n"
            "    close $in;\n"
            "    write_text($measured_snapshot, $content // '');\n"
            "    write_text($measured_path_file, \"$path\\n\");\n"
            "    write_text($measured_backup_file, \"$measured_snapshot\\n\");\n"
            "    write_text($measured_notice_file, \"\");\n"
            "}\n"
            "\n"
            "sub has_pending_validation {\n"
            "    return read_int($pending_validation_file, 0) > 0;\n"
            "}\n"
            "\n"
            "sub looks_like_validation {\n"
            "    my ($tool_name, $tool_input) = @_;\n"
            "    if ($tool_name eq 'Read') {\n"
            "        my $file_path = $tool_input->{file_path} // '';\n"
            "        return index($file_path, '/app/') == 0;\n"
            "    }\n"
            "    return 0 unless $tool_name eq 'Bash';\n"
            "    my $command = lc($tool_input->{command} // '');\n"
            "    for my $marker (@validation_markers) {\n"
            "        return 1 if index($command, $marker) >= 0;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
            "\n"
            "sub phase_message {\n"
            "    my ($elapsed, $timeout, $output_written) = @_;\n"
            "    return 'Preserve observed evidence exactly. If you have a plausible answer, write it now.' if $timeout <= 0;\n"
            "    my $remaining = $timeout - $elapsed;\n"
            "    my $pct = int(($elapsed * 100) / $timeout);\n"
            "    return 'FINAL: <120s left. Write your best answer now and verify.' if $remaining < 120;\n"
            "    return '75%+ elapsed. Finalize your best solution and write required outputs before time runs out.' if $pct >= 75;\n"
            "    return '50%+ elapsed. Simplify if not on track. Preserve exact evidence and write your best plausible answer early.' if $pct >= 50;\n"
            "    return 'If you have a plausible evidence-backed answer, write it now; you can overwrite it later.' unless $output_written;\n"
            "    return 'Keep work concise and preserve observed evidence exactly.';\n"
            "}\n"
            "\n"
            "sub collect_strings {\n"
            "    my ($node, $out) = @_;\n"
            "    return unless defined $node;\n"
            "    if (!ref $node) {\n"
            "        push @$out, $node if $node ne '';\n"
            "        return;\n"
            "    }\n"
            "    if (ref($node) eq 'ARRAY') {\n"
            "        collect_strings($_, $out) for @$node;\n"
            "        return;\n"
            "    }\n"
            "    if (ref($node) eq 'HASH') {\n"
            "        collect_strings($node->{$_}, $out) for keys %$node;\n"
            "    }\n"
            "}\n"
            "\n"
            "sub salient_evidence_lines {\n"
            "    my ($tool_response) = @_;\n"
            "    my @strings;\n"
            "    collect_strings($tool_response, \\@strings);\n"
            "    my %seen;\n"
            "    my @lines;\n"
            "    STRING: for my $text (@strings) {\n"
            "        next if length($text) > 6000;\n"
            "        for my $line (split /\\n/, $text) {\n"
            "            $line =~ s/^\\s+//;\n"
            "            $line =~ s/\\s+$//;\n"
            "            next if $line eq '';\n"
            "            next if length($line) > 120;\n"
            "            next if $line =~ /^(Elapsed:|Remaining:|=== \\/tmp\\/run_state\\.md ===|# Run state|- Goal:|- Best known result:|- Next step:|Exit code \\d+|No matches found)$/;\n"
            "            next unless $line =~ /(PASSWORD=|[A-Z0-9]{8,}|launchcode|\\/(app|logs)\\/|\\b(pass|fail|score|wins?|matches?|error|timeout|constraint)\\b)/i;\n"
            "            next if $seen{$line}++;\n"
            "            push @lines, $line;\n"
            "            last STRING if @lines >= 4;\n"
            "        }\n"
            "    }\n"
            "    return @lines;\n"
            "}\n"
            "\n"
            "sub update_evidence {\n"
            "    my ($tool_response) = @_;\n"
            "    my @new_lines = salient_evidence_lines($tool_response);\n"
            "    return () unless @new_lines;\n"
            "    my @existing = read_lines($evidence_file);\n"
            "    my %seen;\n"
            "    my @merged = grep { !$seen{$_}++ } (@existing, @new_lines);\n"
            "    @merged = @merged[-4 .. -1] if @merged > 4;\n"
            "    write_text($evidence_file, join(\"\\n\", @merged) . \"\\n\");\n"
            "    return @merged;\n"
            "}\n"
            "\n"
            "my $raw = do { local $/; <STDIN> };\n"
            "my $payload = length($raw) ? decode_json($raw) : {};\n"
            "my $event = $payload->{hook_event_name} // 'PostToolUse';\n"
            "my $tool_name = $payload->{tool_name} // '';\n"
            "my $tool_input = ref($payload->{tool_input}) eq 'HASH' ? $payload->{tool_input} : {};\n"
            "my $tool_response = $payload->{tool_response};\n"
            "my $now = time;\n"
            "my $start = int($ENV{TASK_START_EPOCH} // $now);\n"
            "my $timeout = int($ENV{TASK_TIMEOUT_SECS} // 0);\n"
            "my $elapsed = $now - $start;\n"
            "make_path('/logs/agent');\n"
            "if ($event eq 'PostToolUseFailure') {\n"
            "    my $failures = read_int($fail_file, 0) + 1;\n"
            "    write_text($fail_file, \"$failures\\n\");\n"
            "    my $msg = 'Tool failed. Do not repeat the same failing path more than twice; simplify, pivot, or write the best evidence-backed answer you have.';\n"
            "    open my $fh, '>>', $hook_log;\n"
            "    print {$fh} \"$event tool=$tool_name failures=$failures elapsed=$elapsed timeout=$timeout\\n\";\n"
            "    close $fh;\n"
            "    print encode_json({ hookSpecificOutput => { hookEventName => $event, additionalContext => $msg } });\n"
            "    exit 0;\n"
            "}\n"
            "write_text($fail_file, \"0\\n\");\n"
            "if ($tool_name =~ /^(Write|Edit|MultiEdit)$/) {\n"
            "    my $file_path = $tool_input->{file_path} // '';\n"
            "    if (index($file_path, '/app/') == 0) {\n"
            "        write_text($output_file, \"$file_path\\n\");\n"
            "        mark_pending_validation();\n"
            "    }\n"
            "}\n"
            "clear_pending_validation() if looks_like_validation($tool_name, $tool_input);\n"
            "snapshot_measured_artifact($tool_name);\n"
            "my @recent_evidence = update_evidence($tool_response);\n"
            "my $output_written = -f $output_file ? 1 : 0;\n"
            "my $pending_validation = has_pending_validation() ? 1 : 0;\n"
            "my $msg = \"[${elapsed}s / ${timeout}s] \" . phase_message($elapsed, $timeout, $output_written);\n"
            "if ($pending_validation) {\n"
            "    $msg .= ' You have unvalidated /app changes; validate the final artifact or test results before stopping.';\n"
            "}\n"
            "if (@recent_evidence) {\n"
            "    $msg .= ' Recent evidence: ' . join(' || ', @recent_evidence) . '.';\n"
            "}\n"
            "if (!$output_written && @recent_evidence >= 2) {\n"
            "    $msg .= ' You have multiple short evidence lines already. Before deeper searching, form the simplest exact candidate from the observed lines and write it now. Prefer exact concatenation or exact observed overlap only; do not alter observed characters to satisfy heuristics.';\n"
            "}\n"
            "open my $fh, '>>', $hook_log;\n"
            "print {$fh} \"$event tool=$tool_name elapsed=$elapsed timeout=$timeout output_written=$output_written pending_validation=$pending_validation evidence_count=\" . scalar(@recent_evidence) . \"\\n\";\n"
            "close $fh;\n"
            "print encode_json({ hookSpecificOutput => { hookEventName => $event, additionalContext => $msg } });\n"
            "EOF\n"
            "chmod +x /tmp/blobfish-bin/post-tool-hook && "
            "cat > /tmp/blobfish-bin/stop-hook <<'EOF'\n"
            "#!/usr/bin/perl\n"
            "use strict;\n"
            "use warnings;\n"
            "use JSON::PP qw(encode_json);\n"
            "use File::Path qw(make_path);\n"
            "\n"
            "my $hook_log = '/logs/agent/hooks.log';\n"
            "my $state_dir = '/tmp/blobfish-hook';\n"
            "my $pending_validation_file = \"$state_dir/pending_validation\";\n"
            "my $stop_block_file = \"$state_dir/stop_blocked\";\n"
            "my $evidence_file = \"$state_dir/recent_evidence\";\n"
            "\n"
            "sub read_int {\n"
            "    my ($path, $default) = @_;\n"
            "    $default //= 0;\n"
            "    return $default unless -f $path;\n"
            "    open my $fh, '<', $path or return $default;\n"
            "    my $text = <$fh>;\n"
            "    close $fh;\n"
            "    return ($text // '') =~ /(-?\\d+)/ ? int($1) : $default;\n"
            "}\n"
            "\n"
            "sub write_text {\n"
            "    my ($path, $text) = @_;\n"
            "    open my $fh, '>', $path or die \"write $path: $!\";\n"
            "    print {$fh} $text;\n"
            "    close $fh;\n"
            "}\n"
            "\n"
            "sub read_lines {\n"
            "    my ($path) = @_;\n"
            "    return () unless -f $path;\n"
            "    open my $fh, '<', $path or return ();\n"
            "    my @lines = <$fh>;\n"
            "    close $fh;\n"
            "    chomp @lines;\n"
            "    return grep { defined $_ && $_ ne '' } @lines;\n"
            "}\n"
            "\n"
            "my $pending_validation = read_int($pending_validation_file, 0) > 0;\n"
            "my $stop_blocks = read_int($stop_block_file, 0);\n"
            "my @evidence = read_lines($evidence_file);\n"
            "make_path('/logs/agent');\n"
            "if ($pending_validation && $stop_blocks < 1) {\n"
            "    write_text($stop_block_file, \"1\\n\");\n"
            "    my $reason = 'You changed /app files since your last validation. Before stopping, run a direct validation step such as reading the final artifact or running the authoritative task test path. Preserve observed evidence exactly and do not drop characters from observed strings to satisfy heuristics.';\n"
            "    $reason .= ' Recent evidence: ' . join(' || ', @evidence) . '.' if @evidence;\n"
            "    open my $fh, '>>', $hook_log;\n"
            "    print {$fh} \"Stop block pending_validation=1\\n\";\n"
            "    close $fh;\n"
            "    print encode_json({ decision => 'block', reason => $reason });\n"
            "    exit 0;\n"
            "}\n"
            "open my $fh, '>>', $hook_log;\n"
            "print {$fh} \"Stop allow pending_validation=\" . ($pending_validation ? 1 : 0) . \" stop_blocks=$stop_blocks\\n\";\n"
            "close $fh;\n"
            "EOF\n"
            "chmod +x /tmp/blobfish-bin/stop-hook && "
            "cat > $CLAUDE_CONFIG_DIR/settings.json <<'EOF'\n"
            '{\n'
            '  "hooks": {\n'
            '    "SessionStart": [\n'
            '      {\n'
            '        "matcher": "startup|resume|clear|compact",\n'
            '        "hooks": [\n'
            '          {\n'
            '            "type": "command",\n'
            '            "command": "/tmp/blobfish-bin/session-start-hook"\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ],\n'
            '    "PreToolUse": [\n'
            '      {\n'
            '        "matcher": "",\n'
            '        "hooks": [\n'
            '          {\n'
            '            "type": "command",\n'
            '            "command": "/tmp/blobfish-bin/pre-tool-hook"\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ],\n'
            '    "PostToolUse": [\n'
            '      {\n'
            '        "matcher": "",\n'
            '        "hooks": [\n'
            '          {\n'
            '            "type": "command",\n'
            '            "command": "/tmp/blobfish-bin/post-tool-hook"\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ],\n'
            '    "PostToolUseFailure": [\n'
            '      {\n'
            '        "matcher": "",\n'
            '        "hooks": [\n'
            '          {\n'
            '            "type": "command",\n'
            '            "command": "/tmp/blobfish-bin/post-tool-hook"\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ],\n'
            '    "Stop": [\n'
            '      {\n'
            '        "matcher": "",\n'
            '        "hooks": [\n'
            '          {\n'
            '            "type": "command",\n'
            '            "command": "/tmp/blobfish-bin/stop-hook"\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ]\n'
            '  }\n'
            '}\n'
            "EOF\n"
            "{ echo '=== SYSTEM ===' && uname -a && "
            "cat /etc/os-release 2>/dev/null | head -3; "
            "echo '=== TOOLS ===' && "
            "command -v python3 python gcc g++ make cmake node npm cargo rustc go java javac timed status; "
            "echo '=== /app ===' && ls /app; "
            "git -C /app log --oneline -3 2>/dev/null; "
            "echo '=== orient.txt written ==='; "
            "echo '=== STATUS ==='; status; } > /tmp/orient.txt 2>&1"
        )

        # The template-backed setup below is the only live Claude hook path.
        # Keep hooks portable: shell + module-free Perl only.
        setup_cmd = _claude_setup_cmd(
            claude_md=claude_md_text,
            session_start_hook=_hook_template_text("session-start-hook.sh"),
            hook_common_pl=_hook_template_text("hook_common.pl"),
            pre_tool_hook=_hook_template_text("pre-tool-hook.pl"),
            post_tool_hook=_hook_template_text("post-tool-hook.pl"),
            stop_hook=_hook_template_text("stop-hook.pl"),
            task_completed_hook=_hook_template_text("task-completed-hook.pl"),
            constraint_rule=_project_rule_text("constraint-first-debugging.md"),
            constraint_skill=_project_skill_text("constraint-first-debugging/SKILL.md"),
            deadline_rule=_project_rule_text("deadline-aware-delivery.md"),
            deadline_skill=_project_skill_text("deadline-aware-delivery/SKILL.md"),
        )

        run_cmd = (
            "export PATH=\"/tmp/blobfish-bin:$HOME/.local/bin:$PATH\" && "
            "claude --verbose --output-format stream-json "
            "--permission-mode bypassPermissions "
            f"-p {escaped_instruction} 2>&1 </dev/null | tee /logs/agent/blobfish-output.txt"
        )

        return [ExecInput(command=setup_cmd, env=env), ExecInput(command=run_cmd, env=env)]

    def _create_codex_run_commands(
        self, escaped_instruction: str, model_name: str | None = None
    ) -> list[ExecInput]:
        env: dict[str, str] = {
            "BLOBFISH_AGENT_NAME": self._agent_name,
            "BLOBFISH_AGENT_ORG": self._agent_org,
            "CODEX_HOME": "/root/.codex",
        }

        openai_base_url = self._openai_base_url or os.environ.get("OPENAI_BASE_URL")
        openai_base_url = _rewrite_localhost_for_docker(openai_base_url)
        if openai_base_url:
            env["OPENAI_BASE_URL"] = openai_base_url

        env["_CODEX_AUTH_B64"] = base64.b64encode(
            _read_codex_auth(
                openai_base_url=openai_base_url,
                explicit_api_key=self._openai_api_key,
            ).encode()
        ).decode()

        setup_cmd = (
            'mkdir -p /tmp/codex-secrets "$CODEX_HOME"\n'
            'echo "$_CODEX_AUTH_B64" | base64 -d > /tmp/codex-secrets/auth.json\n'
            'ln -sf /tmp/codex-secrets/auth.json "$CODEX_HOME/auth.json"'
        )

        model_flag = ""
        if model_name:
            model = model_name
            if model.startswith("openai/"):
                model = model.split("/", 1)[1]
            model_flag = f"--model {shlex.quote(model)} "

        reasoning_flag = (
            f"-c model_reasoning_effort={self._reasoning_effort} "
            if self._reasoning_effort
            else ""
        )

        run_cmd = (
            'trap \'rm -rf /tmp/codex-secrets "$CODEX_HOME/auth.json"\' EXIT TERM INT; '
            "codex exec --json "
            "--dangerously-bypass-approvals-and-sandbox "
            "--skip-git-repo-check "
            f"{model_flag}"
            "--enable unified_exec "
            f"{reasoning_flag}"
            "-- "
            f"{escaped_instruction} 2>&1 </dev/null | tee /logs/agent/blobfish-output.txt"
        )

        return [ExecInput(command=setup_cmd, env=env), ExecInput(command=run_cmd, env=env)]

    def populate_context_post_run(self, context: AgentContext) -> None:
        output_path = self.logs_dir / "command-0" / "stdout.txt"
        alt_path = self.logs_dir / "command-1" / "stdout.txt"
        if alt_path.exists():
            output_path = alt_path

        total_input = 0
        total_output = 0
        total_cache = 0
        found_usage = False

        if output_path.exists():
            for line in output_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = None
                if isinstance(event, dict):
                    msg = event.get("message")
                    if isinstance(msg, dict):
                        usage = msg.get("usage")
                if isinstance(usage, dict):
                    found_usage = True
                    total_input += usage.get("input_tokens", 0)
                    total_output += usage.get("output_tokens", 0)
                    total_cache += usage.get("cache_read_input_tokens", 0)

                usage = event.get("usage") if isinstance(event, dict) else None
                if isinstance(usage, dict) and "input_tokens" in usage:
                    found_usage = True
                    total_input += usage.get("input_tokens", 0)
                    total_output += usage.get("output_tokens", 0)

        if found_usage:
            context.n_input_tokens = total_input + total_cache
            context.n_output_tokens = total_output
            context.n_cache_tokens = total_cache


class CchuterAgent(BlobfishAgent):
    """Sample GitHub-username agent that matches BlobfishAgent behavior."""

    @staticmethod
    def name() -> str:
        return "cchuter"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("agent_name", "cchuter")
        kwargs.setdefault("agent_org", DEFAULT_AGENT_ORG)
        super().__init__(*args, **kwargs)


def _resolve_agent_name(explicit_name: str | None) -> str:
    candidate = (
        explicit_name
        or os.environ.get("BLOBFISH_AGENT_NAME")
        or os.environ.get("GITHUB_ACTOR")
        or os.environ.get("USER")
        or "blobfish"
    )
    candidate = candidate.strip().lower()
    candidate = re.sub(r"[^a-z0-9-]", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
    if not candidate:
        return "blobfish"
    return candidate[:39]


def _normalize_prompt_variant(prompt_variant: str | None) -> str:
    value = (prompt_variant or "auto").strip().lower()
    if not value:
        value = "auto"
    aliases = {
        "minimax": "minimax-m2.5",
        "minimax-m25": "minimax-m2.5",
        "minimax_m25": "minimax-m2.5",
    }
    value = aliases.get(value, value)
    if value not in {"auto", "full", "slim", "minimax-m2.5"}:
        raise ValueError("prompt_variant must be one of: auto, full, slim, minimax-m2.5")
    return value


def _resolve_prompt_variant(prompt_variant: str, model_name: str | None) -> str:
    if prompt_variant != "auto":
        return prompt_variant
    if _is_minimax_m25(model_name):
        return "minimax-m2.5"
    return "full"


def _is_minimax_m25(model_name: str | None) -> bool:
    if not model_name:
        return False
    low = model_name.lower()
    return "minimax-m2.5" in low


def _prompt_template_path(prompt_variant: str) -> Path:
    if prompt_variant == "slim":
        return TEMPLATES_DIR / "prompt-slim.md.j2"
    if prompt_variant == "minimax-m2.5":
        return TEMPLATES_DIR / "prompt-minimax-m25.md.j2"
    return TEMPLATES_DIR / "prompt.md.j2"


def _project_claude_md(prompt_variant: str) -> str:
    if prompt_variant == "minimax-m2.5":
        path = TEMPLATES_DIR / "claude-project-minimax-m25.md"
    else:
        path = TEMPLATES_DIR / "claude-project-default.md"
    return path.read_text()


def _hook_template_text(name: str) -> str:
    return (TEMPLATES_DIR / "hooks" / name).read_text()


def _project_rule_text(name: str) -> str:
    return (TEMPLATES_DIR / "project-rules" / name).read_text()


def _project_skill_text(path: str) -> str:
    return (TEMPLATES_DIR / "project-skills" / path).read_text()


def _claude_settings_json() -> str:
    settings = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/session-start-hook"}],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/pre-tool-hook"}],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/post-tool-hook"}],
                }
            ],
            "PostToolUseFailure": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/post-tool-hook"}],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/stop-hook"}],
                }
            ],
            "TaskCompleted": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/task-completed-hook"}],
                }
            ],
        }
    }
    return json.dumps(settings, indent=2)


def _claude_setup_cmd(
    *,
    claude_md: str,
    session_start_hook: str,
    hook_common_pl: str,
    pre_tool_hook: str,
    post_tool_hook: str,
    stop_hook: str,
    task_completed_hook: str,
    constraint_rule: str,
    constraint_skill: str,
    deadline_rule: str,
    deadline_skill: str,
) -> str:
    timed_script = """#!/bin/sh
s=$(date +%s)
timeout "${TIMED_LIMIT:-120}" "$@"
rc=$?
if [ "$rc" -eq 124 ]; then
  echo "[TIMING] KILLED after $(($(date +%s)-s))s"
else
  echo "[TIMING] $(($(date +%s)-s))s (exit $rc)"
fi
exit "$rc"
"""
    run_state = """# Run state
- Goal:
- Best known result:
- Next step:
"""
    status_script = """#!/bin/sh
now=$(date +%s)
start=${TASK_START_EPOCH:-$now}
timeout=${TASK_TIMEOUT_SECS:-unknown}
elapsed=$((now-start))
echo "Elapsed: ${elapsed}s"
if [ "$timeout" != "unknown" ]; then
  remaining=$((timeout-elapsed))
  echo "Remaining: ${remaining}s of ${timeout}s"
fi
if [ -f /tmp/run_state.md ]; then
  echo "=== /tmp/run_state.md ==="
  cat /tmp/run_state.md
fi
"""
    return (
        "mkdir -p $CLAUDE_CONFIG_DIR/debug $CLAUDE_CONFIG_DIR/projects/-app "
        "$CLAUDE_CONFIG_DIR/shell-snapshots $CLAUDE_CONFIG_DIR/statsig "
        "$CLAUDE_CONFIG_DIR/todos /tmp/blobfish-bin /tmp/blobfish-hook "
        "/app/.claude/rules /app/.claude/skills/constraint-first-debugging "
        "/app/.claude/skills/deadline-aware-delivery && "
        "if [ -d ~/.claude/skills ]; then "
        "cp -r ~/.claude/skills $CLAUDE_CONFIG_DIR/skills 2>/dev/null || true; "
        "fi && "
        f"printf %s {shlex.quote(claude_md)} > $CLAUDE_CONFIG_DIR/projects/-app/CLAUDE.md && "
        f"printf %s {shlex.quote(constraint_rule)} > /app/.claude/rules/constraint-first-debugging.md && "
        f"printf %s {shlex.quote(constraint_skill)} > /app/.claude/skills/constraint-first-debugging/SKILL.md && "
        f"printf %s {shlex.quote(deadline_rule)} > /app/.claude/rules/deadline-aware-delivery.md && "
        f"printf %s {shlex.quote(deadline_skill)} > /app/.claude/skills/deadline-aware-delivery/SKILL.md && "
        f"printf %s {shlex.quote(timed_script)} > /tmp/blobfish-bin/timed && "
        "chmod +x /tmp/blobfish-bin/timed && "
        f"printf %s {shlex.quote(run_state)} > /tmp/run_state.md && "
        f"printf %s {shlex.quote(status_script)} > /tmp/blobfish-bin/status && "
        "chmod +x /tmp/blobfish-bin/status && "
        f"printf %s {shlex.quote(session_start_hook)} > /tmp/blobfish-bin/session-start-hook && "
        "chmod +x /tmp/blobfish-bin/session-start-hook && "
        f"printf %s {shlex.quote(hook_common_pl)} > /tmp/blobfish-bin/hook_common.pl && "
        f"printf %s {shlex.quote(pre_tool_hook)} > /tmp/blobfish-bin/pre-tool-hook && "
        f"printf %s {shlex.quote(post_tool_hook)} > /tmp/blobfish-bin/post-tool-hook && "
        f"printf %s {shlex.quote(stop_hook)} > /tmp/blobfish-bin/stop-hook && "
        f"printf %s {shlex.quote(task_completed_hook)} > /tmp/blobfish-bin/task-completed-hook && "
        "chmod +x /tmp/blobfish-bin/pre-tool-hook /tmp/blobfish-bin/post-tool-hook /tmp/blobfish-bin/stop-hook /tmp/blobfish-bin/task-completed-hook && "
        f"printf %s {shlex.quote(_claude_settings_json())} > $CLAUDE_CONFIG_DIR/settings.json && "
        "{ echo '=== SYSTEM ===' && uname -a && "
        "cat /etc/os-release 2>/dev/null | head -3; "
        "echo '=== TOOLS ===' && "
        "command -v python3 python gcc g++ make cmake node npm cargo rustc go java javac timed status perl; "
        "echo '=== /app ===' && ls /app; "
        "git -C /app log --oneline -3 2>/dev/null; "
        "echo '=== orient.txt written ==='; "
        "echo '=== STATUS ==='; status; } > /tmp/orient.txt 2>&1"
    )


def _resolve_task_timeout_sec(task_name: str | None) -> int | None:
    if not task_name:
        return None

    task_tomls = sorted(
        (Path.home() / ".cache" / "harbor" / "tasks").glob(f"*/{task_name}/task.toml"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for task_toml in task_tomls:
        try:
            data = tomllib.loads(task_toml.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        timeout = data.get("agent", {}).get("timeout_sec")
        if timeout is None:
            continue
        try:
            return int(float(timeout))
        except (TypeError, ValueError):
            continue
    return None


def _read_oauth_token() -> str | None:
    creds_path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(creds_path.read_text())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_codex_auth(
    openai_base_url: str | None = None,
    explicit_api_key: str | None = None,
) -> str:
    if openai_base_url:
        api_key = explicit_api_key or os.environ.get("OPENAI_API_KEY") or "lm-studio"
        return json.dumps({"OPENAI_API_KEY": api_key})

    codex_auth_path = Path.home() / ".codex" / "auth.json"
    try:
        data = json.loads(codex_auth_path.read_text())
        if data.get("tokens") or data.get("auth_mode") == "chatgpt":
            return json.dumps(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    api_key = os.environ.get("OPENAI_API_KEY", "")
    return json.dumps({"OPENAI_API_KEY": api_key})


def _apply_selector(
    selector: object, backend: str, model_name: str | None
) -> tuple[str, str | None]:
    if not isinstance(selector, str):
        return backend, model_name
    value = selector.strip()
    if not value:
        return backend, model_name

    low = value.lower()
    if low in {"claude", "codex"}:
        return low, model_name

    inferred = _infer_backend_from_model(value)
    return inferred or backend, value


def _infer_backend_from_model(model_name: str) -> str | None:
    low = model_name.lower()
    if "codex" in low or low.startswith("openai/"):
        return "codex"
    if "claude" in low or low.startswith("anthropic/"):
        return "claude"
    return None


def _looks_incompatible_model_for_backend(model_name: str | None, backend: str) -> bool:
    if not model_name:
        return False
    low = model_name.lower()
    if backend == "claude":
        return "codex" in low or low.startswith("openai/")
    if backend == "codex":
        return "claude" in low or low.startswith("anthropic/")
    return False


def _rewrite_localhost_for_docker(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return url

    host_gateway = os.environ.get("BLOBFISH_DOCKER_HOST_GATEWAY", "host.docker.internal").strip()
    if not host_gateway:
        return url

    netloc = parsed.netloc.replace(parsed.hostname, host_gateway, 1)
    return urlunparse(parsed._replace(netloc=netloc))
