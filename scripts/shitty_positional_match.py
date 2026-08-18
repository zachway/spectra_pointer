"""One-off / re-runnable: run sync.positional_fallback.run_shitty_positional_match
against every currently skipped/needs_review holding that carries a raw
position (ra/dec) but wasn't a positional_easy_match candidate at all --
matcher.py's own 1" search found nothing at any of them.

Deliberately separate from scripts.reprocess_against_new_stars: that script
replays matcher.py's existing, confident match paths against newly-added
stars; this one runs a distinct, intentionally-lower-confidence fallback
(sync/positional_fallback.py) that always lands results in needs_review, so
it's meant to be reviewed by a human afterward, not trusted as a source of
truth the way a normal reprocess run is. Re-running is safe (already
needs_review rows just get re-evaluated), but there's no reason to run this
on a tight schedule -- unlike scripts.reprocess_against_new_stars, nothing
about *this* fallback's inputs changes between runs except newly-tracked
stars and whatever Gaia itself updates.

No per-archive/per-chunk looping here -- run_shitty_positional_match takes
every requested archive's candidates at once and paces itself by HEALPix
cell internally (see sync/positional_fallback.py's module docstring), so
archives sharing sky coverage share Gaia round trips instead of each
re-querying overlapping regions.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.shitty_positional_match
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.shitty_positional_match --only eso lick
"""

from __future__ import annotations

import argparse
import logging
import os

import psycopg

from scripts.reprocess_against_new_stars import _rows_to_records_by_archive
from sync.positional_fallback import run_shitty_positional_match

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _load_candidates(conn: psycopg.Connection, only_archives: list[str] | None) -> dict:
    query = """
        SELECT archive_code, archive_obs_id, archive_url, instrument, obs_date, program_id,
               raw_target_name, raw_ra, raw_dec
        FROM spectroscopy_holdings
        WHERE match_status IN ('skipped', 'needs_review')
          AND raw_ra IS NOT NULL AND raw_dec IS NOT NULL
    """
    params: tuple = ()
    if only_archives:
        query += " AND archive_code = ANY(%s)"
        params = (only_archives,)
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return _rows_to_records_by_archive(rows)


def run(conn: psycopg.Connection, only_archives: list[str] | None = None) -> dict:
    by_archive = _load_candidates(conn, only_archives)
    total_candidates = sum(len(v) for v in by_archive.values())
    logger.info("shitty_positional_match: %d candidates across %d archives", total_candidates, len(by_archive))

    return run_shitty_positional_match(conn, by_archive)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", metavar="ARCHIVE_CODE", help="restrict to these archive_codes")
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        totals = run(conn, only_archives=args.only)
    logger.info("done: %s", totals)


if __name__ == "__main__":
    main()
