#!/bin/bash
# Run a list of long jobs on morgan one at a time, at low priority, without
# competing with the cron jobs. morgan has two cores and also hosts Postgres
# and NFS, so the rule is: never more than one of our jobs at once, and don't
# start the next one while the machine is busy with something else.
#
#   - Holds /tmp/spectra_sync.lock for the whole queue, so
#     scripts/weekly_sync_export.sh skips its run instead of overlapping.
#   - Each job runs under nice 15 / idle I/O class.
#   - Before each job, waits until the 1-minute load average is under
#     MAX_LOAD (default 1.5).
#   - A line starting with "reconcile-lock:" also waits for
#     /tmp/spectra_reconcile.lock, so it never overlaps the weekly/monthly
#     reconcile (use it for anything that runs the positional fallback).
#   - A failing job is logged and the queue moves on; lines that succeeded
#     are recorded in <queue file>.done, so re-running the same file resumes.
#
# The queue file has one shell command per line (blank lines and # comments
# ignored), run from the repository root with the venv active.
#
# Usage (always detached):
#   tmux new -d -s queue 'scripts/run_queue.sh ~/queue.txt >> ~/queue.log 2>&1'

QUEUE_FILE="$1"
MAX_LOAD="${MAX_LOAD:-1.5}"
if [ ! -f "$QUEUE_FILE" ]; then
    echo "usage: $0 QUEUE_FILE" >&2
    exit 2
fi
QUEUE_FILE="$(readlink -f "$QUEUE_FILE")"
DONE_FILE="$QUEUE_FILE.done"
touch "$DONE_FILE"

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source venv/bin/activate
export DATABASE_URL="postgresql:///spectra_db?host=/tmp"

COOKIE_FILE="$HOME/.goa_session_cookie"
if [ -s "$COOKIE_FILE" ]; then
    export GOA_SESSION_COOKIE
    GOA_SESSION_COOKIE="$(cat "$COOKIE_FILE")"
fi

exec 8>/tmp/spectra_sync.lock
if ! flock -n 8; then
    echo "$(date): another queue or the weekly sync holds /tmp/spectra_sync.lock, not starting"
    exit 1
fi

wait_for_quiet() {
    while awk -v max="$MAX_LOAD" '{ exit !($1 >= max) }' /proc/loadavg; do
        echo "$(date): load $(cut -d' ' -f1 /proc/loadavg) >= $MAX_LOAD, waiting"
        sleep 300
    done
}

echo "=== $(date): queue $QUEUE_FILE starting ==="
# Read on fd 7 so a job can't swallow the rest of the queue from stdin.
while IFS= read -r line <&7; do
    case "$line" in ''|'#'*) continue ;; esac
    if grep -qxF -- "$line" "$DONE_FILE"; then
        echo "$(date): already done, skipping: $line"
        continue
    fi
    wait_for_quiet
    echo "=== $(date): starting: $line ==="
    if [ "${line#reconcile-lock:}" != "$line" ]; then
        flock /tmp/spectra_reconcile.lock nice -n 15 ionice -c 3 bash -c "${line#reconcile-lock:}" 7<&- 8>&-
    else
        nice -n 15 ionice -c 3 bash -c "$line" 7<&- 8>&-
    fi
    status=$?
    echo "=== $(date): finished (status=$status): $line ==="
    if [ "$status" -eq 0 ]; then
        echo "$line" >> "$DONE_FILE"
    fi
done 7< "$QUEUE_FILE"
echo "=== $(date): queue $QUEUE_FILE finished ==="
