#!/bin/bash
# Weekly reconcile of the archives most prone to records going public late
# (proprietary/embargo periods ending on old-dated observations) AND cheap to
# re-walk. Chosen from the 2026-09-01 monthly run: gemini_ghost, mast_jwst,
# neid, koa, irtf_spex, xmm, irtf_ishell all turned up hundreds of records
# with observation dates well before the live cursor; total cost is roughly
# 70 minutes. The heavy archives (eso, eso_raw, noirlab, polarbase,
# sdss_v_optical) stay on scripts/monthly_reconcile.sh only.
#
# Shares archive_sync_state.reconcile_cursor with the monthly job, so this
# just advances the same rolling re-walk faster. Uses flock so it can never
# overlap the monthly run or itself (koa's 2026-09-01 failure was a deadlock
# against a concurrent writer).
#
# Setup (not done by this script; pick a day clear of the 1st):
#   (crontab -l 2>/dev/null; echo "0 3 * * 3 $PWD/scripts/weekly_reconcile.sh >> $PWD/weekly_reconcile.log 2>&1") | crontab -

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source venv/bin/activate
export DATABASE_URL="postgresql:///spectra_db?host=/tmp"

COOKIE_FILE="$HOME/.goa_session_cookie"
if [ -s "$COOKIE_FILE" ]; then
    export GOA_SESSION_COOKIE
    GOA_SESSION_COOKIE="$(cat "$COOKIE_FILE")"
fi

exec 9>/tmp/spectra_reconcile.lock
if ! flock -n 9; then
    echo "$(date): another reconcile run holds the lock, skipping"
    exit 0
fi

echo "=== $(date): weekly reconcile starting ==="
python3 -m sync.reconcile --max-pages-per-archive 20 --only \
    mast_jwst gemini_ghost gemini_igrins gemini neid irtf_spex irtf_ishell xmm chandra lick koa
status=$?
echo "=== $(date): weekly reconcile finished (status=$status) ==="
exit "$status"
