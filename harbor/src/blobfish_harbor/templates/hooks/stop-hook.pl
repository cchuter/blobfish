#!/usr/bin/perl
use strict;
use warnings;
require "/tmp/blobfish-bin/hook_common.pl";

ensure_dirs();
my $pending_validation = read_int(state_path('pending_validation'), 0) > 0;
my $stop_blocks = read_int(state_path('stop_blocked'), 0);
my @evidence = read_lines(state_path('recent_evidence'));

if ($pending_validation && $stop_blocks < 1) {
    write_text(state_path('stop_blocked'), "1\n");
    my $reason = 'You changed /app files since your last validation. Before stopping, run a direct validation step such as reading the final artifact or running the authoritative task test path.';
    $reason .= ' Recent evidence: ' . join(' || ', @evidence) . '.' if @evidence;
    log_line('Stop block pending_validation=1');
    emit_hook('{"decision":"block","reason":"' . json_escape($reason) . '"}');
    exit 0;
}

log_line('Stop allow pending_validation=' . ($pending_validation ? 1 : 0) . " stop_blocks=$stop_blocks");
