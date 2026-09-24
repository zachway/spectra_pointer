"""One-off: backfill raw_ra/raw_dec on direct_gaia_column holdings.

Until 2026-09-24 every direct-Gaia-column archive module except lbt built
its RawObservation with gaia_source_id but no ra/dec -- several (lamost,
lamost_mrs) even SELECTed the position and then dropped it. sync.matcher
stores rec.ra/rec.dec as raw_ra/raw_dec verbatim, so ~27.7M matched rows
(lamost 11.3M, desi 5.2M, lamost_mrs 4.9M, sdss_v_optical 3.6M, galah 1.1M,
gaia_rvs 1.0M, sdss_v_apogee 657k, rave 517k) had no archive-reported
position at all. Visible symptom: the search page's "search unmatched
records" radius mode reads spectroscopy_holdings_by_position.parquet, which
only carries rows with a position, so a star's LAMOST/APOGEE/... records
never showed up there even at 0" separation.

The module fixes make every future sync store the position. This re-walks
each archive's own (now fixed) fetch() from the start and fills in
raw_ra/raw_dec only -- no re-matching, no upsert of any other column, and
never overwrites a position already on file (WHERE raw_ra IS NULL). Most of
these archives aren't in sync.reconcile's rolling re-walk (frozen releases
/ full-scan designs, see AT_RISK_ARCHIVES there), so it would never pick
this up on its own.

carmenes/carmenes_tac/carmenes_reiners2018 are deliberately not here: their
source tables carry no coordinates at all (Gaia id comes from resolving the
star's name through SIMBAD), so there is no archive-reported position to
store.

Resumable: each archive's fetch() cursor is saved to --state-file after
every page, and an archive that finished is skipped on re-run.

Usage (on morgan, in tmux -- lamost alone is ~11M rows at ~500 rows/s):
    python3 -m scripts.backfill_direct_raw_positions
    python3 -m scripts.backfill_direct_raw_positions --only lamost_mrs rave
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os

import psycopg

from sync.archives import desi, gaia_rvs, galah, lamost, lamost_mrs, rave, sdss_v_apogee, sdss_v_optical

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Smallest first, so a quick run proves the path end to end before the long
# LAMOST walk.
ARCHIVES = {
    "rave": rave.fetch,
    "sdss_v_apogee": sdss_v_apogee.fetch,
    "gaia_rvs": gaia_rvs.fetch,
    "galah": galah.fetch,
    "sdss_v_optical": sdss_v_optical.fetch,
    "lamost_mrs": lamost_mrs.fetch,
    "desi": desi.fetch,
    "lamost": lamost.fetch,
}

UPDATE_CHUNK_SIZE = 50_000

DEFAULT_STATE_FILE = os.path.expanduser("~/.cache/spectra_pointer/backfill_direct_raw_positions.json")


def _load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _apply(conn: psycopg.Connection, archive_code: str, positions: list[tuple[str, float, float]]) -> int:
    updated = 0
    for start in range(0, len(positions), UPDATE_CHUNK_SIZE):
        chunk = positions[start : start + UPDATE_CHUNK_SIZE]
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE IF NOT EXISTS backfill_pos "
                "(archive_obs_id TEXT, ra DOUBLE PRECISION, dec DOUBLE PRECISION) ON COMMIT DELETE ROWS"
            )
            buf = io.StringIO()
            for obs_id, ra, dec in chunk:
                buf.write(f"{obs_id}\t{ra!r}\t{dec!r}\n")
            with cur.copy("COPY backfill_pos (archive_obs_id, ra, dec) FROM STDIN") as copy:
                copy.write(buf.getvalue())
            cur.execute(
                """
                UPDATE spectroscopy_holdings h
                SET raw_ra = b.ra, raw_dec = b.dec
                FROM backfill_pos b
                WHERE h.archive_code = %s AND h.archive_obs_id = b.archive_obs_id
                  AND h.match_method = 'direct_gaia_column' AND h.raw_ra IS NULL
                """,
                (archive_code,),
            )
            updated += cur.rowcount
        conn.commit()
    return updated


def backfill_archive(conn: psycopg.Connection, archive_code: str, fetch_fn, state: dict, state_file: str) -> None:
    entry = state.setdefault(archive_code, {"cursor": {}, "done": False, "updated": 0})
    if entry["done"]:
        logger.info("%s: already done (%d rows updated), skipping", archive_code, entry["updated"])
        return

    cursor = entry["cursor"]
    while True:
        records, new_cursor = fetch_fn(cursor)
        positions = [(r.archive_obs_id, r.ra, r.dec) for r in records if r.ra is not None and r.dec is not None]
        n = _apply(conn, archive_code, positions)
        entry["updated"] += n
        logger.info(
            "%s: page of %d records (%d with position) -> %d rows updated, %d total; cursor %s",
            archive_code, len(records), len(positions), n, entry["updated"], new_cursor,
        )
        # Same termination rule as sync.main: an empty page means caught up;
        # an unchanged cursor (one-shot archives like rave) means no further
        # pages exist either.
        if not records or new_cursor == cursor:
            entry["done"] = True
            _save_state(state_file, state)
            break
        cursor = new_cursor
        entry["cursor"] = cursor
        _save_state(state_file, state)

    logger.info("%s: done, %d rows updated", archive_code, entry["updated"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=sorted(ARCHIVES), help="restrict to these archive_codes")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    args = parser.parse_args()

    state = _load_state(args.state_file)
    targets = args.only or list(ARCHIVES)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for archive_code in targets:
            backfill_archive(conn, archive_code, ARCHIVES[archive_code], state, args.state_file)


if __name__ == "__main__":
    main()
