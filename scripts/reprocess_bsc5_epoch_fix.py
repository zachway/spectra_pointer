"""One-off: reprocess spectroscopy_holdings rows that may have been
incorrectly left skipped/needs_review by the positional-match pre-filter
bug fixed in sync.matcher (PR #156) -- the q3c pre-filter's PM-drift
safety margin was sized off distance from GAIA_DR3_REF_EPOCH alone,
undershooting the true drift for source_catalog='bsc5' stars
(ref_epoch=1991.25) at any observation epoch past ~2003.6, so a record for
one of those 70 stars could be silently excluded from the candidate pool
before propagation ever ran.

Scoped to just those 70 stars (scripts.seed_bsc5_bright_stars.
BSC5_HR_NUMBERS_MISSING_FROM_GAIA) rather than reusing
scripts.reprocess_against_new_stars' population #1 (every skipped/
needs_review row, no filter): that full sweep already ran once against
production (2026-08-13/14, reprocess_matcher_fix.log on morgan) and took
~9 hours end to end, almost all of it sync.matcher.match_records' own
per-chunk overhead (propagation, q3c queries, upserts) applied to ~16M
rows that have nothing to do with this bug. spectroscopy_holdings.
raw_ra/raw_dec has no spatial index (only stars.ra/dec does -- see
db/schema.sql's q3c_stars_idx), so there's no way to push a "near one of
these 70 stars" filter into the SQL WHERE clause the way sync.matcher
itself does against stars. Instead this streams every skipped/
needs_review row once (per archive_code, so idx_holdings_archive_status
can still narrow the read instead of a full-table seqscan), filters to
rows within NEARBY_RADIUS_ARCSEC of any of the 70 stars' raw stored
positions via a single vectorized search_around_sky call per batch (cheap,
no external calls), and only replays that small surviving subset through
sync.matcher.match_records -- the dominant cost becomes one sequential
read instead of ~16M individual match attempts.

NEARBY_RADIUS_ARCSEC (600", matching NAME_MATCH_SANITY_RADIUS_ARCSEC's own
"generous but bounded" convention elsewhere in this codebase) is
deliberately not epoch/PM-aware -- it only needs to not miss a real
candidate; sync.matcher.match_records applies its own correct, per-epoch
radius once a record reaches it.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.reprocess_bsc5_epoch_fix
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date

import psycopg
from astropy import units as u
from astropy.coordinates import SkyCoord

from scripts.reprocess_against_new_stars import _reprocess_batch
from scripts.seed_bsc5_bright_stars import BSC5_HR_NUMBERS_MISSING_FROM_GAIA
from sync.base import RawObservation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NEARBY_RADIUS_ARCSEC = 600.0
STREAM_BATCH_SIZE = 200_000


def _load_bsc5_centers(conn: psycopg.Connection) -> SkyCoord:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ra, dec FROM stars WHERE source_catalog = 'bsc5' AND bsc_hr_number = ANY(%s)",
            (BSC5_HR_NUMBERS_MISSING_FROM_GAIA,),
        )
        rows = cur.fetchall()
    if not rows:
        raise RuntimeError("no bsc5 stars found in `stars` -- run scripts.seed_bsc5_bright_stars first")
    logger.info("%d BSC5 star centers loaded", len(rows))
    return SkyCoord(ra=[r[0] for r in rows] * u.deg, dec=[r[1] for r in rows] * u.deg)


def _archive_codes(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT archive_code FROM archives ORDER BY archive_code")
        return [r[0] for r in cur.fetchall()]


def find_bsc5_proximate_candidates(conn: psycopg.Connection, centers: SkyCoord) -> dict[str, list[RawObservation]]:
    """Every currently skipped/needs_review holding whose raw position sits
    within NEARBY_RADIUS_ARCSEC of one of the 70 BSC5 stars -- see module
    docstring for why this is a Python-side filter, not a SQL one.
    """
    by_archive: dict[str, list[RawObservation]] = defaultdict(list)
    total_scanned = 0
    total_kept = 0
    for archive_code in _archive_codes(conn):
        archive_kept = 0
        with conn.cursor(name=f"bsc5_epoch_fix_{archive_code}") as cur:
            cur.itersize = STREAM_BATCH_SIZE
            cur.execute(
                """
                SELECT archive_obs_id, archive_url, instrument, obs_date, program_id,
                       raw_target_name, raw_ra, raw_dec
                FROM spectroscopy_holdings
                WHERE archive_code = %(archive_code)s AND match_status IN ('skipped', 'needs_review')
                  AND raw_ra IS NOT NULL AND raw_dec IS NOT NULL
                  -- A present-but-bogus raw_dec (not caught by the IS NOT NULL
                  -- check above) crashes SkyCoord construction for the whole
                  -- batch outright, same failure mode sync.matcher.match_records
                  -- already guards against for MAST's -99.0 sentinel. Not
                  -- isolated to one archive: observed live in koa (3314 rows),
                  -- gtc (695, e.g. raw_dec=90.44/90.70/91.15 -- just past 90
                  -- deg, a probable coordinate-parsing bug in that archive's
                  -- own ingestion), mast (250), and noirlab (38) -- all out of
                  -- scope to fix here, this filter just keeps them from
                  -- crashing this script.
                  AND raw_dec BETWEEN -90.0 AND 90.0
                """,
                {"archive_code": archive_code},
            )
            while True:
                batch = cur.fetchmany(STREAM_BATCH_SIZE)
                if not batch:
                    break
                total_scanned += len(batch)
                targets = SkyCoord(ra=[r[6] for r in batch] * u.deg, dec=[r[7] for r in batch] * u.deg)
                # centers.search_around_sky(targets, r) -> first return indexes
                # targets (the argument), second indexes centers (self) -- the
                # reverse of what the field names suggest, same convention
                # sync.matcher documents and this script verified empirically
                # before relying on it here. We only need *which* targets got
                # a hit, not which center -- any hit is enough to warrant a
                # real (epoch-correct) match attempt via matcher.match_records.
                idx_targets, _, _, _ = centers.search_around_sky(targets, NEARBY_RADIUS_ARCSEC * u.arcsec)
                hit_idx = set(idx_targets.tolist())
                if not hit_idx:
                    continue
                for i in hit_idx:
                    archive_obs_id, archive_url, instrument, obs_date, program_id, raw_target_name, raw_ra, raw_dec = batch[i]
                    by_archive[archive_code].append(
                        RawObservation(
                            archive_obs_id=archive_obs_id,
                            archive_url=archive_url,
                            instrument=instrument,
                            obs_date=obs_date if isinstance(obs_date, date) else None,
                            program_id=program_id,
                            gaia_source_id=None,
                            ra=raw_ra,
                            dec=raw_dec,
                            raw_target_name=raw_target_name,
                        )
                    )
                archive_kept += len(hit_idx)
        if archive_kept:
            logger.info("%s: %d BSC5-proximate candidates found", archive_code, archive_kept)
        total_kept += archive_kept
    logger.info(
        "scanned %d skipped/needs_review rows with a position across all archives, "
        "%d within %g\" of a BSC5 star",
        total_scanned, total_kept, NEARBY_RADIUS_ARCSEC,
    )
    return by_archive


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        centers = _load_bsc5_centers(conn)
        by_archive = find_bsc5_proximate_candidates(conn, centers)
        totals = _reprocess_batch(conn, by_archive, "bsc5_epoch_fix")
    logger.info("done: %s", totals)


if __name__ == "__main__":
    main()
