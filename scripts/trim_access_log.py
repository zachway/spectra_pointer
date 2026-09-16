"""Trims joy's gunicorn access.log to the last N days (default 30, matching
Cloud Logging's old default retention -- see this project's move off Cloud
Run) so raw client IPs don't accumulate on disk indefinitely now that
Google's automatic log expiry no longer applies.

Rewrites the log file in place -- reads it fully, then truncates and
rewrites the same path/inode -- rather than renaming a replacement into
place. gunicorn holds this file open for appending by file descriptor, not
by path: a rename would leave gunicorn writing into an orphaned, otherwise
unreachable inode until its next restart/reopen, silently losing every
request logged after the trim until then. This is the same "copytruncate"
strategy logrotate itself offers for exactly this reason, with the same
small race window (a line appended between the read and the truncate is
lost) -- acceptable at this project's traffic.

Run this AFTER scripts.build_access_heatmap has processed the log for a
given day (see scripts/refresh_access_heatmap.sh and
scripts/trim_access_log.sh), so a request's country is always aggregated
into access_heatmap.json before its raw IP is ever removed.

Usage:
    python3 -m scripts.trim_access_log --log-file ~/spectra_pointer_webapp/access.log
    python3 -m scripts.trim_access_log --log-file ... --keep-days 30
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

from scripts.build_access_heatmap import _GUNICORN_LOG_LINE_RE, _parse_gunicorn_timestamp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _keep_line(line: str, cutoff: datetime) -> bool:
    """A line is dropped only when it's positively identified as older than
    cutoff -- anything unparseable (blank lines, a future gunicorn
    log-format change) is kept, since this script's only job is removing
    lines proven old, not tidying the file."""
    m = _GUNICORN_LOG_LINE_RE.match(line)
    if not m:
        return True
    try:
        dt = _parse_gunicorn_timestamp(m.group("ts"))
    except ValueError:
        return True
    return dt >= cutoff


def trim(log_file: str, keep_days: int) -> tuple[int, int]:
    """Returns (lines_read, lines_removed). Leaves the file untouched (no
    write at all) when nothing needs removing."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    with open(log_file, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    kept = [line for line in lines if _keep_line(line, cutoff)]
    removed = len(lines) - len(kept)
    if removed:
        with open(log_file, "w", encoding="utf-8") as f:
            f.writelines(kept)
    return len(lines), removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--keep-days", type=int, default=30)
    args = parser.parse_args()

    total, removed = trim(args.log_file, args.keep_days)
    logger.info(
        "%s: %d lines read, %d older than %d days removed",
        args.log_file, total, removed, args.keep_days,
    )


if __name__ == "__main__":
    main()
