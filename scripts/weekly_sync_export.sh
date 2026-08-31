#!/bin/bash
# Weekly full archive sync + Parquet export, run via cron every Saturday
# night (see crontab -l on morgan). sync.main exits 1 if any individual
# archive fails -- gemini_ghost/gemini_igrins in particular fail every run
# unless GOA_SESSION_COOKIE is set, since morgan is headless and that cookie
# can only be obtained by logging into archive.gemini.edu in a browser
# elsewhere (see sync/main.py's docstring and sync/archives/_goa_common.py).
# sync.main already handles a failing archive without stopping the others,
# so this wrapper doesn't let sync's exit code skip the export step either.
#
# To include gemini_ghost/gemini_igrins in the weekly run, copy a fresh
# session cookie value into ~/.goa_session_cookie (no trailing newline). If
# that file is absent or empty, those two archives are just skipped for the
# week like any other transient failure -- nothing needs to be edited here.
#
# Setup (not done by this script):
#   (crontab -l 2>/dev/null; echo "0 23 * * 6 $PWD/scripts/weekly_sync_export.sh >> $PWD/weekly_sync_export.log 2>&1") | crontab -

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source venv/bin/activate
export DATABASE_URL="postgresql:///spectra_db?host=/tmp"

COOKIE_FILE="$HOME/.goa_session_cookie"
if [ -s "$COOKIE_FILE" ]; then
    export GOA_SESSION_COOKIE
    GOA_SESSION_COOKIE="$(cat "$COOKIE_FILE")"
fi

echo "=== $(date): weekly sync+export starting ==="

python3 -m sync.main
sync_status=$?
if [ "$sync_status" -ne 0 ]; then
    echo "$(date): sync.main exited $sync_status (one or more archives failed -- see above)"
fi

# Fills in parallax/phot_bp_mean_mag/phot_rp_mean_mag/has_gaia_rvs/
# has_xp_continuous for any star the run above just added via the
# automatic offline fallback (a Gaia TAP timeout mid-sync -- see
# sync.main.sync_archive and ingest.add_star.AddStarsResult.gaia_degraded).
# Runs even if sync.main above failed for some archives: this only touches
# stars already in the table, independent of which archives succeeded, and
# is a no-op (fast) if nothing needs it. Before the export below, so a
# star backfilled this run shows up complete in this week's snapshot
# instead of a week late.
python3 -m scripts.backfill_gaia_astrometry
backfill_status=$?
if [ "$backfill_status" -ne 0 ]; then
    echo "$(date): scripts.backfill_gaia_astrometry exited $backfill_status"
fi

python3 -m scripts.export_to_parquet --out-dir ~/public_html/spectra_data
export_status=$?

echo "=== $(date): weekly sync+export finished (sync=$sync_status backfill=$backfill_status export=$export_status) ==="
exit "$export_status"
