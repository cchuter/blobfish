#!/usr/bin/perl
use strict;
use warnings;
require "/tmp/blobfish-bin/hook_common.pl";

ensure_dirs();
my $pending_validation = read_int(state_path('pending_validation'), 0) > 0;
my $completion_blocks = read_int(state_path('task_completed_blocked'), 0);
my @written = read_lines(state_path('output_written'));
my @evidence = read_lines(state_path('recent_evidence'));

if ($pending_validation && $completion_blocks < 1) {
    write_text(state_path('task_completed_blocked'), "1\n");
    my $reason = 'You appear ready to finish, but you still have unvalidated /app changes. Validate the final artifact or run the authoritative task check before completing.';
    $reason .= ' Recent evidence: ' . join(' || ', @evidence) . '.' if @evidence;
    log_line('TaskCompleted block pending_validation=1');
    emit_hook('{"decision":"block","reason":"' . json_escape($reason) . '"}');
    exit 0;
}

if (!@written && @evidence >= 2 && $completion_blocks < 2) {
    write_text(state_path('task_completed_blocked'), "2\n");
    my $reason = 'You appear ready to finish without writing a concrete /app output. Before completing, write the best current artifact or answer file you can justify from the observed evidence, then validate it.';
    log_line('TaskCompleted block missing_output_written=1');
    emit_hook('{"decision":"block","reason":"' . json_escape($reason) . '"}');
    exit 0;
}

log_line('TaskCompleted allow pending_validation=' . ($pending_validation ? 1 : 0) . ' output_written=' . scalar(@written));
