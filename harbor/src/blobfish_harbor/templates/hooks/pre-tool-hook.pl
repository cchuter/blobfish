#!/usr/bin/perl
use strict;
use warnings;
require "/tmp/blobfish-bin/hook_common.pl";

ensure_dirs();
my $raw = read_raw_stdin();
my $tool_name = extract_json_string($raw, 'tool_name');
my $file_path = extract_json_string($raw, 'file_path');
my $command = extract_json_string($raw, 'command');
my $content = extract_json_string($raw, 'content');

my $blocked_path = denied_edit_path($tool_name, $file_path);
if ($blocked_path ne '') {
    log_line("PreToolUse deny tool=$tool_name path=$blocked_path");
    emit_permission_decision('deny', 'Do not modify tests, verifiers, or Claude settings unless the task explicitly requires it.');
    exit 0;
}

my $mutated_reason = mutated_evidence_reason($tool_name, $file_path, $content);
if ($mutated_reason ne '') {
    my $deny_count = increment_deny_count($file_path);
    log_line("PreToolUse deny_mutated_evidence tool=$tool_name path=$file_path deny_count=$deny_count");
    emit_permission_decision('deny', $mutated_reason);
    exit 0;
}

# Write-guard: block whole-file Write on existing /app/ files, force Edit instead.
# First Write to a new file is always allowed. Subsequent writes to the same path
# are blocked — the agent must use Edit for targeted changes.
if ($tool_name eq 'Write' && defined $file_path && index($file_path, '/app/') == 0) {
    my $write_count_file = state_path('write_guard_counts');
    my %counts;
    for my $line (read_lines($write_count_file)) {
        if ($line =~ /^(\d+)\t(.+)$/) { $counts{$2} = int($1); }
    }
    my $prev = $counts{$file_path} // 0;
    $counts{$file_path} = $prev + 1;
    my $text = join('', map { "$counts{$_}\t$_\n" } keys %counts);
    write_text($write_count_file, $text);

    if ($prev >= 1) {
        log_line("PreToolUse write_guard_block tool=Write path=$file_path count=$counts{$file_path}");
        emit_permission_decision(
            'deny',
            "BLOCKED: You already wrote $file_path. Use the Edit tool for targeted changes instead of rewriting the entire file. Full rewrites discard working code and introduce new bugs. Identify the specific lines that need to change and edit only those.",
        );
        exit 0;
    }
}

my $measured_reason = measured_overwrite_reason($tool_name, $file_path);
if ($measured_reason ne '') {
    log_line("PreToolUse preserve_measured_overwrite tool=$tool_name");
    emit_permission_decision(
        'allow',
        'Preserve the measured snapshot for comparison before replacement.',
        undef,
        $measured_reason,
    );
    exit 0;
}

exit 0 unless $tool_name eq 'Bash';
exit 0 if $command =~ /\bstatus\b/;

my (undef, $elapsed, $timeout) = current_timing();
my $remaining = $timeout ? ($timeout - $elapsed) : 0;
my $phase = 0;
if ($timeout > 0 && $remaining < 120) {
    $phase = 3;
}
my $last_phase = read_int(state_path('phase'), -1);
my $consecutive_failures = read_int(state_path('failures'), 0);
my $should_inject = (!-f state_path('first_bash')) || ($phase > $last_phase) || ($consecutive_failures >= 2);
exit 0 unless $should_inject;

write_text(state_path('first_bash'), "1\n");
write_text(state_path('phase'), "$phase\n");
write_text(state_path('failures'), "0\n") if $consecutive_failures >= 2;
my $new_command = "status; $command";
log_line("PreToolUse inject_status phase=$phase elapsed=$elapsed remaining=$remaining failures=$consecutive_failures");
emit_permission_decision(
    'allow',
    'Injected status checkpoint before Bash command',
    $new_command,
    'Use the status output as the authoritative budget/current-state snapshot for your next decision.',
);
