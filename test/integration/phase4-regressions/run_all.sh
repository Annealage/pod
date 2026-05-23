#!/usr/bin/env bash
# Run all phase-4 host-stack regression tests in order. First failure
# wins; remaining tests don't run. Re-run with single-test invocation
# if a specific test needs isolation.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

for t in \
    test_bulk_out_large.py \
    test_in_pid_after_short.py \
    test_abort_releases_ep.py \
    test_control_timeout.py
do
    echo "=== ${t} ==="
    python3 "${t}"
    echo
done

echo "All host-stack regression tests passed."
