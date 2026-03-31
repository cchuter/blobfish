#!/usr/bin/perl
use strict;
use warnings;
require "/tmp/blobfish-bin/hook_common.pl";

ensure_dirs();
my $raw = read_raw_stdin();
my $event = extract_json_string($raw, 'hook_event_name') || 'PostToolUse';
my $tool_name = extract_json_string($raw, 'tool_name');
my $file_path = extract_json_string($raw, 'file_path');
my $command = extract_json_string($raw, 'command');
my (undef, $elapsed, $timeout) = current_timing();

if ($event eq 'PostToolUseFailure') {
    my $failures = read_int(state_path('failures'), 0) + 1;
    write_text(state_path('failures'), "$failures\n");
    log_line("$event tool=$tool_name failures=$failures elapsed=$elapsed timeout=$timeout");
    emit_additional_context($event, 'Tool failed. Do not repeat the same failing path more than twice; simplify, pivot, or write the best evidence-backed answer you have.');
    exit 0;
}

write_text(state_path('failures'), "0\n");
if ($tool_name =~ /^(Write|Edit|MultiEdit)$/ && defined $file_path && index($file_path, '/app/') == 0) {
    write_text(state_path('output_written'), "$file_path\n");
    mark_pending_validation();
}
clear_pending_validation() if looks_like_validation($tool_name, $file_path, $command);
snapshot_measured_artifact($tool_name);

my @recent_evidence = update_evidence($raw);
my $output_written = scalar(read_lines(state_path('output_written'))) ? 1 : 0;
my $pending_validation = has_pending_validation() ? 1 : 0;
my $msg = "[$elapsed"."s / $timeout"."s] " . phase_message($elapsed, $timeout, $output_written);
if ($pending_validation) {
    $msg .= ' You have unvalidated /app changes; validate the final artifact or test results before stopping.';
}
if (@recent_evidence) {
    $msg .= ' Recent evidence: ' . join(' || ', @recent_evidence) . '.';
}
if (!$output_written && @recent_evidence >= 2) {
    my $nudge = increment_nudge_count();
    if ($nudge >= 5) {
        $msg .= ' CRITICAL: You have observed ' . scalar(@recent_evidence) . ' evidence fragments across ' . $nudge . ' tool calls without writing ANY output. STOP EXPLORING. Write the required output artifact NOW with the best combination of your observed evidence. You can always overwrite it later.';
    } elsif ($nudge >= 3) {
        $msg .= ' URGENT: ' . $nudge . ' tool calls with evidence but no output written. Write the simplest exact evidence-backed artifact now before searching further.';
    } else {
        $msg .= ' You already have multiple short evidence lines. Before deeper searching, write the simplest evidence-backed candidate or output artifact you can justify from the current observations.';
    }
} elsif ($output_written) {
    reset_nudge_count();
}

log_line("$event tool=$tool_name elapsed=$elapsed timeout=$timeout output_written=$output_written pending_validation=$pending_validation evidence_count=" . scalar(@recent_evidence));
emit_additional_context($event, $msg);
write_run_state();
