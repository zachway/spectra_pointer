"""One-off: reprocess shitty_positional_match holdings belonging to any pair
of BSC5-tracked stars that sit close enough together on sky to trigger
sync.positional_fallback.pick_best_candidate's BSC5-categorical-win blind
spot (see BSC5_DISAMBIGUATION_RATIO) -- brightness can't disambiguate
between two naked-eye stars the way it does against a Gaia candidate, since
neither has a phot_g_mean_mag at all.

Confirmed live (2026-09-16): exactly 4 such pairs exist in the whole BSC5
catalog (~9100 stars) -- Alpha Cen A/B (HR 5459/5460, ~16" apart), Alpha
Crucis/Acrux A/B (HR 4730/4731, ~4"), Gamma Leonis/Algieba A/B (HR 4057/4058,
~5"), and Zeta Ori/Alnitak A/B (HR 1948/1949, ~2"). Alpha Cen's pair alone
accounted for ~67k mismatched holdings (a majority of what looked like a
single star's observation count was actually its companion's data); the
other three pairs are smaller (tens to hundreds of rows each) but subject to
the exact same blind spot. Scope is a hard structural fact about the BSC5
catalog (found via a cheap self-join, see find_close_bsc5_pairs), not a
sample -- so this is a small, fully-bounded re-run (order 10,000s of rows
total, confirmed by survey before this script was written), safe to just
load directly rather than needing scripts.reprocess_bare_hd_numbers'
server-side-cursor paging.

Replays sync.positional_fallback.run_shitty_positional_match, so the new
BSC5_DISAMBIGUATION_RATIO guard applies: a pair member that's now
decisively closer still wins outright (unchanged from before); a record
that's genuinely ambiguous between the two demotes to
match_status='needs_review', star_id=NULL, rather than staying wrong with
false confidence. Every row already carries match_method=
'shitty_positional_match', so no unrelated holdings are touched.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.reprocess_bsc5_close_pairs
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date

import psycopg

from sync.base import RawObservation
from sync.positional_fallback import run_shitty_positional_match

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Guard against the search radius itself ever changing without this
# reprocessing job's own "close" threshold following along -- a pair of BSC5
# stars just outside this box wouldn't ever land in the same
# shitty_positional_match candidate pool in the first place, so widening
# would only ever add pairs that couldn't actually be affected.
PAIR_SEARCH_RADIUS_ARCSEC = 120.0

_CLOSE_PAIRS_SQL = """
    SELECT a.star_id, b.star_id,
           3600 * sqrt(pow((a.ra - b.ra) * cos(radians(a.dec)), 2) + pow(a.dec - b.dec, 2)) AS sep_arcsec
    FROM stars a
    JOIN stars b ON a.star_id < b.star_id
    WHERE a.source_catalog = 'bsc5' AND b.source_catalog = 'bsc5'
      AND abs(a.dec - b.dec) < %(box_deg)s
      AND abs(a.ra - b.ra) < %(box_deg)s / cos(radians(a.dec))
"""


def find_close_bsc5_pairs(conn: psycopg.Connection, radius_arcsec: float = PAIR_SEARCH_RADIUS_ARCSEC) -> list[tuple[int, int]]:
    """(star_id, star_id) for every BSC5-BSC5 pair within radius_arcsec.
    Coarse RA/Dec box pre-filter (cheap against BSC5's ~9100 rows -- no q3c
    index needed at this table size), sep_arcsec computed precisely and
    filtered in Python.
    """
    box_deg = radius_arcsec / 3600.0 * 1.5  # generous box margin before the precise filter below
    with conn.cursor() as cur:
        cur.execute(_CLOSE_PAIRS_SQL, {"box_deg": box_deg})
        rows = cur.fetchall()
    return [(a, b) for a, b, sep in rows if sep <= radius_arcsec]


def _load_shitty_holdings_for_stars(conn: psycopg.Connection, star_ids: list[int]) -> dict[str, list[RawObservation]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT archive_code, archive_obs_id, archive_url, instrument, obs_date, program_id,
                   raw_target_name, raw_ra, raw_dec, reduction_status
            FROM spectroscopy_holdings
            WHERE star_id = ANY(%s) AND match_method = 'shitty_positional_match'
            """,
            (star_ids,),
        )
        rows = cur.fetchall()

    by_archive: dict[str, list[RawObservation]] = defaultdict(list)
    for (archive_code, archive_obs_id, archive_url, instrument, obs_date, program_id,
         raw_target_name, raw_ra, raw_dec, reduction_status) in rows:
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
                reduction_status=reduction_status,
            )
        )
    return by_archive


def reprocess(conn: psycopg.Connection) -> dict:
    pairs = find_close_bsc5_pairs(conn)
    star_ids = sorted({s for pair in pairs for s in pair})
    logger.info("found %d close BSC5 pairs (%d distinct stars): %s", len(pairs), len(star_ids), pairs)
    if not star_ids:
        return {}

    by_archive = _load_shitty_holdings_for_stars(conn, star_ids)
    total_rows = sum(len(recs) for recs in by_archive.values())
    logger.info("reprocessing %d shitty_positional_match holdings across %d archives", total_rows, len(by_archive))

    counts = run_shitty_positional_match(conn, by_archive)
    logger.info("done: %s", counts)
    return counts


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        reprocess(conn)


if __name__ == "__main__":
    main()
