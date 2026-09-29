#!/bin/bash
# Daily refresh of the "Who's using The Spectra Pointer?" map on /info, run
# via cron on morgan (see crontab -l on morgan). joy itself has no crontab
# available to this account, but morgan and joy share an NFS home
# directory, so this runs on morgan and reads gunicorn's live access.log on
# joy directly through that shared mount -- no copying needed, same as
# scripts/build_access_heatmap.py's docstring describes for --source
# local-log. Incremental: each run only picks up log entries newer than the
# previous run's watermark in access_heatmap.json.
#
# Setup (not done by this script):
#   (crontab -l 2>/dev/null; echo "0 6 * * * $PWD/scripts/refresh_access_heatmap.sh >> $PWD/refresh_access_heatmap.log 2>&1") | crontab -

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source venv/bin/activate

echo "=== $(date): access heatmap refresh starting ==="

python3 -m scripts.build_access_heatmap \
    --out-dir ~/public_html/spectra_data \
    --source local-log \
    --log-file /nfs/morgan/users/way/spectra_pointer_webapp/access.log
status=$?

echo "=== $(date): access heatmap refresh finished (status=$status) ==="
exit "$status"
