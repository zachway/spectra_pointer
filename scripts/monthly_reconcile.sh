#!/bin/bash
# Monthly full re-walk of archives whose sync cursor advances on an
# observation-time field (see sync/reconcile.py's own docstring for why:
# short version, a MAST-style bug where an archive backfills/re-releases an
# old-dated record after the live cursor has already passed it, silently
# skipping it forever). Run via cron on the first of the month (see
# crontab -l on morgan) -- separate from scripts/weekly_sync_export.sh's
# Saturday-night live incremental sync, so a slow reconcile pass never
# delays or competes with the weekly export.
#
# --max-pages-per-archive bounds how far one run walks per archive; progress
# is saved after every page (archive_sync_state.reconcile_cursor), so a big
# archive just takes several months to complete one full cycle before
# wrapping around and starting again. sync.reconcile handles a failing
# archive without stopping the others, same as sync.main.
#
# Setup (not done by this script):
#   (crontab -l 2>/dev/null; echo "0 22 1 * * $PWD/scripts/monthly_reconcile.sh >> $PWD/monthly_reconcile.log 2>&1") | crontab -

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source venv/bin/activate
export DATABASE_URL="postgresql:///spectra_db?host=/tmp"

COOKIE_FILE="$HOME/.goa_session_cookie"
if [ -s "$COOKIE_FILE" ]; then
    export GOA_SESSION_COOKIE
    GOA_SESSION_COOKIE="$(cat "$COOKIE_FILE")"
fi

echo "=== $(date): monthly reconcile starting ==="

python3 -m sync.reconcile --max-pages-per-archive 20
reconcile_status=$?
if [ "$reconcile_status" -ne 0 ]; then
    echo "$(date): sync.reconcile exited $reconcile_status (one or more archives failed -- see above)"
fi

echo "=== $(date): monthly reconcile finished (status=$reconcile_status) ==="
exit "$reconcile_status"
