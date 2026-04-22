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

    # Error-adaptive skill injection: detect the error pattern and inject targeted guidance
    my $error_msg = 'Tool failed.';
    my $output_text = $raw;
    if ($output_text =~ /command not found|No such file or directory.*bin/) {
        $error_msg = 'Tool missing. Install it now: apt-get update -qq && apt-get install -y -qq <package>. Do not probe with which/command -v — install directly.';
    } elsif ($output_text =~ /SyntaxError|IndentationError|TabError/) {
        $error_msg = 'Python syntax error. Use Edit to fix the specific line rather than rewriting the file. Check the line number in the error.';
    } elsif ($output_text =~ /error:.*expected|undefined reference|implicit declaration/) {
        $error_msg = 'Compilation error. Use Edit to fix the specific error. Do NOT rewrite the entire file — you will lose working code. Fix the first reported error only, then recompile.';
    } elsif ($output_text =~ /ModuleNotFoundError|ImportError|No module named/) {
        $error_msg = 'Missing Python module. Install it: pip3 install --break-system-packages <module-name>. Then retry.';
    } elsif ($output_text =~ /SEGV|Segmentation fault|core dumped/) {
        $error_msg = 'Segfault. Add debug prints (fprintf(stderr,...) or print(..., file=sys.stderr)) to isolate which function crashes. Do NOT rewrite the whole file.';
    } elsif ($output_text =~ /AssertionError|FAILED|assert.*False/) {
        $error_msg = 'Test assertion failed. Read the assertion message carefully — it tells you exactly what is wrong. Fix the specific logic, do not rewrite from scratch.';
    }
    if ($failures >= 2) {
        $error_msg .= ' This is failure #' . $failures . '. Do not repeat the same approach — simplify or pivot.';
    }
    emit_additional_context($event, $error_msg);
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

# Loop detection: warn if the agent is repeating the same action
my $loop_warning = detect_repetition_loop($tool_name, $file_path, $command);
if ($loop_warning ne '') {
    $msg .= " $loop_warning";
}

log_line("$event tool=$tool_name elapsed=$elapsed timeout=$timeout output_written=$output_written pending_validation=$pending_validation evidence_count=" . scalar(@recent_evidence));
emit_additional_context($event, $msg);
write_run_state();
