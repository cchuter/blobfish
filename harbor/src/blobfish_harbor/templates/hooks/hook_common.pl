our $HOOK_LOG = '/logs/agent/hooks.log';
our $STATE_DIR = '/tmp/blobfish-hook';

sub ensure_dirs {
    system('mkdir', '-p', '/logs/agent', $STATE_DIR);
}

sub state_path {
    my ($name) = @_;
    return "$STATE_DIR/$name";
}

sub log_line {
    my ($line) = @_;
    ensure_dirs();
    open my $fh, '>>', $HOOK_LOG or return;
    print {$fh} "$line\n";
    close $fh;
}

sub read_int {
    my ($path, $default) = @_;
    $default = 0 unless defined $default;
    return $default unless -f $path;
    open my $fh, '<', $path or return $default;
    local $/;
    my $text = <$fh>;
    close $fh;
    return ($text // '') =~ /(-?\d+)/ ? int($1) : $default;
}

sub write_text {
    my ($path, $text) = @_;
    open my $fh, '>', $path or return;
    print {$fh} $text;
    close $fh;
}

sub read_lines {
    my ($path) = @_;
    return () unless -f $path;
    open my $fh, '<', $path or return ();
    my @lines = <$fh>;
    close $fh;
    chomp @lines;
    return grep { defined $_ && $_ ne '' } @lines;
}

sub json_escape {
    my ($s) = @_;
    $s = '' unless defined $s;
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    return $s;
}

sub json_unescape {
    my ($s) = @_;
    $s = '' unless defined $s;
    $s =~ s/\\u([0-9a-fA-F]{4})/chr(hex($1))/eg;
    $s =~ s/\\"/"/g;
    $s =~ s/\\\\/\\/g;
    $s =~ s/\\n/\n/g;
    $s =~ s/\\r/\r/g;
    $s =~ s/\\t/\t/g;
    $s =~ s#\\/#/#g;
    return $s;
}

sub read_raw_stdin {
    local $/;
    my $raw = <STDIN>;
    return defined $raw ? $raw : '';
}

sub extract_json_string {
    my ($raw, $key) = @_;
    return '' unless defined $raw && defined $key;
    if ($raw =~ /"$key"\s*:\s*"((?:\\.|[^"\\])*)"/s) {
        return json_unescape($1);
    }
    return '';
}

sub extract_json_strings {
    my ($raw) = @_;
    my @strings;
    while ($raw =~ /"((?:\\.|[^"\\])*)"/gs) {
        push @strings, json_unescape($1);
    }
    return @strings;
}

sub current_timing {
    my $now = time;
    my $start = int($ENV{TASK_START_EPOCH} // $now);
    my $timeout = int($ENV{TASK_TIMEOUT_SECS} // 0);
    return ($now, $now - $start, $timeout);
}

sub strong_tokens {
    my (@lines) = @_;
    my %seen;
    my @tokens;
    for my $line (@lines) {
        while ($line =~ /([A-Z0-9]{8,})/g) {
            my $token = $1;
            next unless $token =~ /[A-Z]/ && $token =~ /\d/;
            next if $seen{$token}++;
            push @tokens, $token;
            return @tokens if @tokens >= 4;
        }
    }
    return @tokens;
}

sub denied_edit_path {
    my ($tool_name, $file_path) = @_;
    return '' unless $tool_name =~ /^(Write|Edit|MultiEdit)$/;
    return '' unless defined $file_path && $file_path ne '';
    for my $part ('/tests/', '/verifier/', '/.claude/', '/CLAUDE.md') {
        return $file_path if index($file_path, $part) >= 0;
    }
    return '';
}

sub increment_deny_count {
    my ($file_path) = @_;
    my $count_file = state_path('mutation_deny_count');
    my @lines = read_lines($count_file);
    my %counts;
    for my $line (@lines) {
        if ($line =~ /^(\d+)\t(.+)$/) { $counts{$2} = int($1); }
    }
    $counts{$file_path} = ($counts{$file_path} // 0) + 1;
    my $text = join('', map { "$counts{$_}\t$_\n" } keys %counts);
    write_text($count_file, $text);
    return $counts{$file_path};
}

sub get_deny_count {
    my ($file_path) = @_;
    for my $line (read_lines(state_path('mutation_deny_count'))) {
        return int($1) if $line =~ /^(\d+)\t(.+)$/ && $2 eq $file_path;
    }
    return 0;
}

sub increment_nudge_count {
    my $path = state_path('nudge_count');
    my $count = read_int($path, 0) + 1;
    write_text($path, "$count\n");
    return $count;
}

sub reset_nudge_count {
    write_text(state_path('nudge_count'), "0\n");
}

sub mutated_evidence_reason {
    my ($tool_name, $file_path, $content) = @_;
    return '' unless $tool_name eq 'Write';
    return '' unless defined $file_path && index($file_path, '/app/') == 0;
    return '' if get_deny_count($file_path) >= 2;
    return '' unless defined $content;
    my @candidates = grep { /[A-Z]/ && /\d/ } ($content =~ /([A-Z0-9]{8,})/g);
    return '' unless @candidates == 1;
    my $candidate = $candidates[0];
    my @tokens = grep { length($_) >= 10 } strong_tokens(read_lines(state_path('recent_evidence')));
    return '' unless @tokens >= 2;
    my @missing = grep { index($candidate, $_) < 0 } @tokens;
    return '' unless @missing;
    my $limit = @tokens > 3 ? 2 : $#tokens;
    my $summary = join(' || ', @tokens[0 .. $limit]);
    return "The content you are writing mutates or drops exact observed token fragments. Preserve observed evidence exactly before writing. Recent exact tokens: $summary.";
}

sub measured_overwrite_reason {
    my ($tool_name, $file_path) = @_;
    return '' unless $tool_name eq 'Write';
    return '' unless defined $file_path && index($file_path, '/app/') == 0;
    my @measured_path = read_lines(state_path('measured_path'));
    return '' unless @measured_path && $measured_path[0] eq $file_path;
    my @notice = read_lines(state_path('measured_notice'));
    return '' if @notice && $notice[0] eq $file_path;
    write_text(state_path('measured_notice'), "$file_path\n");
    my @backup = read_lines(state_path('measured_backup'));
    my $backup = @backup ? $backup[0] : 'the measured artifact snapshot';
    return "You are overwriting a previously measured /app artifact at $file_path. Preserve the measured version for comparison before replacement. A snapshot is available at $backup. If the new variant regresses, restore the measured version instead of throwing it away.";
}

sub emit_hook {
    my ($json) = @_;
    print $json;
}

sub emit_permission_decision {
    my ($decision, $reason, $updated_command, $additional) = @_;
    my $json = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"' . json_escape($decision) . '","permissionDecisionReason":"' . json_escape($reason) . '"';
    if (defined $updated_command) {
        $json .= ',"updatedInput":{"command":"' . json_escape($updated_command) . '"}';
    }
    if (defined $additional) {
        $json .= ',"additionalContext":"' . json_escape($additional) . '"';
    }
    $json .= '}}';
    emit_hook($json);
}

sub emit_additional_context {
    my ($event, $msg) = @_;
    emit_hook('{"hookSpecificOutput":{"hookEventName":"' . json_escape($event) . '","additionalContext":"' . json_escape($msg) . '"}}');
}

sub phase_message {
    my ($elapsed, $timeout, $output_written) = @_;
    return 'Preserve observed evidence exactly. If you have a plausible answer, write it now.' if $timeout <= 0;
    my $remaining = $timeout - $elapsed;
    my $pct = int(($elapsed * 100) / $timeout);
    return 'FINAL 120s: STOP exploring. Write the best current valid artifact, run the closest authoritative verification, and ship.' if $remaining < 120;
    return 'If you have a plausible evidence-backed answer, write it now; you can overwrite it later.' unless $output_written;
    return 'Keep work concise and preserve observed evidence exactly.';
}

sub salient_evidence_lines {
    my ($raw) = @_;
    my @strings = extract_json_strings($raw);
    my %seen;
    my @lines;
    STRING:
    for my $text (@strings) {
        next if length($text) > 6000;
        for my $line (split /\n/, $text) {
            $line =~ s/^\s+//;
            $line =~ s/\s+$//;
            next if $line eq '';
            next if length($line) > 120;
            next if $line =~ /^(Elapsed:|Remaining:|=== \/tmp\/run_state\.md ===|# Run state|- Goal:|- Best known result:|- Next step:|Exit code \d+|No matches found)$/;
            # Skip Claude Code internal metadata (session IDs, paths, config)
            next if $line =~ /session_id|transcript_path|permission_mode|hook_event|hook_id|claude_code_version|apiKeySource|fast_mode_state|output_style|mcp_servers/;
            next unless $line =~ /(PASSWORD=|[A-Z0-9]{8,}|launchcode|\/(app|logs)\/|\b(pass|fail|score|wins?|matches?|error|timeout|constraint)\b)/i;
            next if $seen{$line}++;
            push @lines, $line;
            last STRING if @lines >= 4;
        }
    }
    return @lines;
}

sub update_evidence {
    my ($raw) = @_;
    my @new_lines = salient_evidence_lines($raw);
    my @existing = read_lines(state_path('recent_evidence'));
    my %seen;
    my @merged = grep { !$seen{$_}++ } (@existing, @new_lines);
    @merged = @merged[-4 .. -1] if @merged > 4;
    write_text(state_path('recent_evidence'), join("\n", @merged) . "\n") if @merged;
    return @merged;
}

sub write_run_state {
    my $summary = "";

    # 1. Timing — current_timing() returns ($now, $elapsed, $timeout)
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

sub mark_pending_validation {
    write_text(state_path('pending_validation'), "1\n");
    write_text(state_path('stop_blocked'), "0\n");
    write_text(state_path('task_completed_blocked'), "0\n");
}

sub clear_pending_validation {
    write_text(state_path('pending_validation'), "0\n");
    write_text(state_path('stop_blocked'), "0\n");
    write_text(state_path('task_completed_blocked'), "0\n");
}

sub has_pending_validation {
    return read_int(state_path('pending_validation'), 0) > 0;
}

sub looks_like_validation {
    my ($tool_name, $file_path, $command) = @_;
    if ($tool_name eq 'Read') {
        return defined $file_path && index($file_path, '/app/') == 0;
    }
    return 0 unless $tool_name eq 'Bash';
    return 0 unless defined $command;
    my $lower = lc($command);
    for my $marker ('/tests/', 'pytest', 'unittest', 'cargo test', 'go test', 'npm test', 'pnpm test', 'yarn test', 'bun test', 'ctest', 'make test', 'verify', 'sed -n ', 'grep ') {
        return 1 if index($lower, $marker) >= 0;
    }
    return 0;
}

sub snapshot_measured_artifact {
    my ($tool_name) = @_;
    return unless $tool_name eq 'Bash';
    my @paths = read_lines(state_path('output_written'));
    return unless @paths;
    my $path = $paths[0];
    return unless defined $path && -f $path;
    open my $fh, '<', $path or return;
    local $/;
    my $content = <$fh>;
    close $fh;
    my $snapshot = state_path('measured_output.snapshot');
    write_text($snapshot, defined $content ? $content : '');
    write_text(state_path('measured_path'), "$path\n");
    write_text(state_path('measured_backup'), "$snapshot\n");
}

1;
