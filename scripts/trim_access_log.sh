#!/bin/bash
# Daily trim of joy's gunicorn access.log to the last 30 days, run via cron
# on morgan (same NFS-share trick as refresh_access_heatmap.sh -- see that
# script for why joy itself can't run its own crontab). Raw client IPs
# would otherwise accumulate on disk forever now that Cloud Run's automatic
# 30-day log expiry no longer applies (see scripts/trim_access_log.py).
#
# Scheduled to run after refresh_access_heatmap.sh in morgan's crontab, so
# every request has already been aggregated into access_heatmap.json
# before its raw IP is ever removed.
#
# Setup (not done by this script):
#   (crontab -l 2>/dev/null; echo "15 6 * * * $PWD/scripts/trim_access_log.sh >> $PWD/trim_access_log.log 2>&1") | crontab -

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source venv/bin/activate

echo "=== $(date): access log trim starting ==="

python3 -m scripts.trim_access_log \
    --log-file /nfs/morgan/users/way/spectra_pointer_webapp/access.log \
    --keep-days 30
status=$?

echo "=== $(date): access log trim finished (status=$status) ==="
exit "$status"
