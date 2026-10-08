"""One-off/periodic: fill stars.teff_gspphot / logg_gspphot / mh_gspphot from
Gaia DR3's GSP-Phot pipeline (gaiadr3.gaia_source), for the CMD page's
Teff-logg view and parameter filters (issue #229).

These are copied exactly as Gaia publishes them -- no quality cuts, no
recalibration. GSP-Phot only has results for a subset of sources (roughly
G < 19 with usable BP/RP spectra), so a star legitimately ending up with all
three NULL is common. That's why "not looked up yet" is tracked separately,
in stars.gspphot_checked_at, rather than inferred from the values the way
scripts.backfill_gaia_astrometry does with bp/rp: every star sent to Gaia
gets gspphot_checked_at stamped whether or not parameters came back.

ingest.add_star doesn't set these columns, so this is their only writer:
new stars arrive with gspphot_checked_at NULL and get picked up by the next
run (scripts/weekly_sync_export.sh runs this after each sync). Resumable for
the same reason -- an interrupted run just leaves the rest unstamped. The
first run against an existing catalog is long (one Gaia TAP query per
CHUNK_SIZE stars); run it detached.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.backfill_gaia_gspphot
    # optional: stop after N stars (e.g. to try it out)
    DATABASE_URL=... python3 -m scripts.backfill_gaia_gspphot --limit 10000
"""

from __future__ import annotations

import argparse
import logging
import os

import psycopg

from ingest.add_star import _launch_gaia_job
from sync.base import clean_float

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Same size as ingest.add_star's BATCH_CHUNK_SIZE, and the same reason the
# query below needs an explicit TOP -- see GAIA_BATCH_QUERY's comment there.
CHUNK_SIZE = 5000

GSPPHOT_QUERY = """
SELECT TOP {top} source_id, teff_gspphot, logg_gspphot, mh_gspphot
FROM gaiadr3.gaia_source
WHERE source_id IN ({id_list})
"""


def backfill(conn: psycopg.Connection, limit: int | None = None) -> tuple[int, int]:
    """Returns (stars checked, stars that got at least one parameter)."""
    checked = 0
    with_params = 0
    # Keyset pagination on the primary key, one chunk at a time, rather than
    # loading every pending id up front -- the first run has the whole
    # catalog pending.
    last_star_id = 0
    while limit is None or checked < limit:
        want = CHUNK_SIZE if limit is None else min(CHUNK_SIZE, limit - checked)
        with conn.cursor() as cur:
            # gaia_source_id IS NOT NULL: BSC5-sourced stars have no Gaia
            # row to look up (see scripts.backfill_gaia_astrometry).
            cur.execute(
                "SELECT star_id, gaia_source_id FROM stars "
                "WHERE gaia_source_id IS NOT NULL AND gspphot_checked_at IS NULL AND star_id > %s "
                "ORDER BY star_id LIMIT %s",
                (last_star_id, want),
            )
            chunk = cur.fetchall()
        if not chunk:
            break
        last_star_id = chunk[-1][0]
        source_ids = [sid for _, sid in chunk]

        id_list = ",".join(str(sid) for sid in source_ids)
        table = _launch_gaia_job(GSPPHOT_QUERY.format(top=len(source_ids), id_list=id_list)).get_results()
        found = {
            int(row["source_id"]): (
                clean_float(row["teff_gspphot"]),
                clean_float(row["logg_gspphot"]),
                clean_float(row["mh_gspphot"]),
            )
            for row in table
        }
        values = [found.get(sid, (None, None, None)) for sid in source_ids]

        # One statement, one new row version per star: values and the
        # checked stamp together, for every star in the chunk whether or
        # not Gaia had anything for it.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE stars s SET teff_gspphot = v.teff, logg_gspphot = v.logg, mh_gspphot = v.mh, "
                "gspphot_checked_at = now() "
                "FROM unnest(%s::bigint[], %s::real[], %s::real[], %s::real[]) AS v(id, teff, logg, mh) "
                "WHERE s.gaia_source_id = v.id",
                (source_ids, [v[0] for v in values], [v[1] for v in values], [v[2] for v in values]),
            )
        conn.commit()
        checked += len(source_ids)
        with_params += sum(1 for v in values if any(x is not None for x in v))
        logger.info("checked %d stars so far, %d with GSP-Phot parameters", checked, with_params)

    return checked, with_params


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=None, help="stop after checking this many stars")
    args = parser.parse_args()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        checked, with_params = backfill(conn, limit=args.limit)
    logger.info("done, %d stars checked, %d with GSP-Phot parameters", checked, with_params)


if __name__ == "__main__":
    main()
