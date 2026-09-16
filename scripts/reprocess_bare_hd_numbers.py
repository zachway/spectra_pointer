"""One-off: reprocess spectroscopy_holdings rows whose raw_target_name is a
bare Henry Draper number (e.g. "128620", no "HD " prefix) that sync.matcher's
identifier match now recognizes against a tracked star's "HD <same digits>"
alias (see sync.matcher._lookup_star_id). Before that fix, these fell
through to position-only matching, where -- for stars close enough on sky to
another bright BSC5 star (e.g. Alpha Cen A/B, HD 128620/128621, only ~15"
apart) -- they could be silently mismatched onto the WRONG member of the
pair. Confirmed live (2026-09-16): NOIRLab's SMARTS/CHIRON program logs
plain HD numbers as its OBJECT header for exactly this reason, and a
majority of what looked like Alpha Cen A's observation count was actually
mislabeled Alpha Cen B data.

Scoped by content (raw_target_name matching a tracked star's bare HD digits
among rows that never actually resolved by name), not by archive or time
window -- unlike scripts.reprocess_skipped/reprocess_against_new_stars, this
isn't about one outage or one batch of newly-added stars. Streamed via a
named (server-side) cursor in bounded pages rather than loading the whole
matching set into Python at once -- this project's own history includes more
than one morgan OOM incident from exactly that pattern (see
sync/positional_fallback.py's PROPAGATION_ARRAY_ELEMENT_BUDGET), and the
true row count here isn't known ahead of a full-table scan.

Replays the same sync.matcher.match_records used for live syncs, so the
same identifier-before-position priority, ambiguity handling, and idempotent
ON CONFLICT (archive_code, archive_obs_id) upsert applies -- safe to
interrupt and re-run; already name_resolved rows just won't be selected
again.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.reprocess_bare_hd_numbers
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date

import psycopg

from sync import matcher
from sync.base import RawObservation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Rows fetched per server-side-cursor page -- small enough that one page's
# worth of RawObservation/matcher work stays a trivial fraction of morgan's
# RAM regardless of how many total rows this sweep turns out to touch.
PAGE_SIZE = 20_000

_CANDIDATE_ROWS_SQL = """
    WITH hd_aliases AS (
        SELECT star_id, regexp_replace(alias, '^HD\\s*', '') AS hd_digits
        FROM stars, unnest(name_aliases) AS alias
        WHERE alias ~* '^HD\\s*[0-9]+$'
    )
    SELECT h.id, h.archive_code, h.archive_obs_id, h.archive_url, h.instrument,
           h.obs_date, h.program_id, h.raw_target_name, h.raw_ra, h.raw_dec, h.reduction_status
    FROM spectroscopy_holdings h
    JOIN hd_aliases hd ON h.raw_target_name = hd.hd_digits
    WHERE h.match_method != 'name_resolved'
"""


def _row_to_record(row: tuple) -> RawObservation:
    (_id, _archive_code, archive_obs_id, archive_url, instrument, obs_date, program_id,
     raw_target_name, raw_ra, raw_dec, reduction_status) = row
    return RawObservation(
        archive_obs_id=archive_obs_id,
        archive_url=archive_url,
        instrument=instrument,
        obs_date=obs_date if isinstance(obs_date, date) else None,
        program_id=program_id,
        gaia_source_id=None,
        ra=raw_ra,
        dec=raw_dec,
        raw_target_name=raw_target_name,
        reduction_status=reduction_status,
    )


def reprocess(read_conn: psycopg.Connection, write_conn: psycopg.Connection) -> dict:
    """Two separate connections, not one: matcher.match_records commits
    write_conn's transaction several times per call, and a named (server-
    side) cursor's portal doesn't survive a commit on the same connection --
    reusing one connection for both would silently invalidate read_conn's
    cursor partway through the sweep.
    """
    totals: dict[str, int] = {}
    total_seen = 0

    # Named cursor -> server-side, streamed in itersize-sized batches instead
    # of one giant fetchall -- see module docstring.
    with read_conn.cursor(name="bare_hd_number_sweep") as cur:
        cur.itersize = PAGE_SIZE
        cur.execute(_CANDIDATE_ROWS_SQL)

        page: list[tuple] = []
        for row in cur:
            page.append(row)
            if len(page) >= PAGE_SIZE:
                totals = _process_page(write_conn, page, totals)
                total_seen += len(page)
                logger.info("bare HD number sweep: %d rows processed so far, totals=%s", total_seen, totals)
                page = []
        if page:
            totals = _process_page(write_conn, page, totals)
            total_seen += len(page)

    logger.info("bare HD number sweep: done, %d rows processed, totals=%s", total_seen, totals)
    return totals


def _process_page(conn: psycopg.Connection, page: list[tuple], totals: dict[str, int]) -> dict[str, int]:
    by_archive: dict[str, list[RawObservation]] = defaultdict(list)
    for row in page:
        by_archive[row[1]].append(_row_to_record(row))

    for archive_code, records in by_archive.items():
        counts = matcher.match_records(conn, archive_code, records)
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as read_conn, \
         psycopg.connect(os.environ["DATABASE_URL"]) as write_conn:
        totals = reprocess(read_conn, write_conn)
    logger.info("done: %s", totals)


if __name__ == "__main__":
    main()
