"""One-off/periodic: backfill parallax, phot_bp_mean_mag, phot_rp_mean_mag,
has_gaia_rvs, and has_xp_continuous for stars that don't have them yet.

Two ways a star ends up missing these: added before phot_bp_mean_mag/
phot_rp_mean_mag existed as columns at all (the original, now-historical
reason this script existed as backfill_bp_rp.py), or added via
ingest.add_star.add_stars_batch(..., offline=True) -- either passed
explicitly (sync.main's --offline flag) or triggered automatically when a
live Gaia TAP call exhausted its retries mid-sync (see
add_star._fetch_astrometry_offline and AddStarsResult.gaia_degraded).
Either way, gaia_source_lite_mirror (the offline path's data source) only
carries source_id/ra/dec/pmra/pmdec/phot_g_mean_mag -- these five columns
are left NULL/False on insert and need a live Gaia TAP query to fill in,
same batched-query pattern as ingest.add_star.add_stars_batch, but scoped
to just these columns so it's safe to run against the full tracked-star
catalog (1M+ rows and growing) without re-doing any of the archive
sync/matching work.

Resumable, and safe to run on a schedule regardless of whether anything's
actually missing: only selects rows where phot_bp_mean_mag AND
phot_rp_mean_mag are still both NULL (see backfill()'s docstring for why
those two, not has_gaia_rvs/has_xp_continuous, are what identifies a
not-yet-backfilled row), so a re-run after a partial run, a Gaia TAP outage
mid-way, or simply nothing new to do all converge to a no-op fast.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.backfill_gaia_astrometry
"""

from __future__ import annotations

import logging
import os

import psycopg
from astroquery.gaia import Gaia

from sync.base import clean_float

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CHUNK_SIZE = 500

BACKFILL_QUERY = """
SELECT source_id, parallax, phot_bp_mean_mag, phot_rp_mean_mag, has_rvs, has_xp_continuous
FROM gaiadr3.gaia_source
WHERE source_id IN ({id_list})
"""


def backfill(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        # gaia_source_id IS NOT NULL matters now, not just cosmetically:
        # BSC5-sourced stars (source_catalog='bsc5') have no Gaia photometry
        # by design, so both columns are permanently NULL for them -- without
        # this filter they'd match the WHERE clause every run, and once one
        # lands in a chunk, str(None) becomes the literal text "None" in the
        # id_list below, a syntax error in the ADQL sent to Gaia's own TAP
        # service that fails the whole chunk (observed: 70 BSC5 rows
        # exist in production as of 2026-07-25, so this isn't hypothetical).
        #
        # phot_bp_mean_mag/phot_rp_mean_mag (not has_gaia_rvs/
        # has_xp_continuous) are what identify a row needing this backfill:
        # has_gaia_rvs and has_xp_continuous are NOT NULL BOOLEAN columns
        # (db/schema.sql) -- both the online and offline insert paths always
        # give them a concrete True/False, so a placeholder False from the
        # offline path is indistinguishable from a real, permanent False by
        # value alone. phot_bp_mean_mag/phot_rp_mean_mag don't have that
        # problem (NULL is never a valid magnitude), and every online-added
        # row backfills them together in the same Gaia response as parallax/
        # has_rvs/has_xp_continuous -- so "both still NULL" reliably means
        # "this row's non-mirror columns were never fetched", regardless of
        # which of the two ways (pre-column-existing legacy row, or a real
        # offline-mode insert) got it there.
        cur.execute(
            "SELECT gaia_source_id FROM stars "
            "WHERE gaia_source_id IS NOT NULL AND phot_bp_mean_mag IS NULL AND phot_rp_mean_mag IS NULL"
        )
        pending = [row[0] for row in cur.fetchall()]

    logger.info("%d stars missing parallax/bp/rp/has_rvs/has_xp_continuous", len(pending))
    updated = 0
    for i in range(0, len(pending), CHUNK_SIZE):
        chunk = pending[i : i + CHUNK_SIZE]
        id_list = ",".join(str(sid) for sid in chunk)
        job = Gaia.launch_job(BACKFILL_QUERY.format(id_list=id_list))
        table = job.get_results()

        with conn.cursor() as cur:
            for row in table:
                cur.execute(
                    "UPDATE stars SET parallax = %s, phot_bp_mean_mag = %s, phot_rp_mean_mag = %s, "
                    "has_gaia_rvs = %s, has_xp_continuous = %s WHERE gaia_source_id = %s",
                    (
                        clean_float(row["parallax"]),
                        clean_float(row["phot_bp_mean_mag"]),
                        clean_float(row["phot_rp_mean_mag"]),
                        bool(row["has_rvs"]),
                        bool(row["has_xp_continuous"]),
                        int(row["source_id"]),
                    ),
                )
        conn.commit()
        updated += len(table)
        logger.info("backfilled %d/%d", min(i + CHUNK_SIZE, len(pending)), len(pending))

    return updated


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        total = backfill(conn)
    logger.info("done, %d stars updated", total)


if __name__ == "__main__":
    main()
