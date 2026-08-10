"""Cross-archive reconciliation between eso (Phase 3 reduced) and eso_raw
(dbo.raw, unreduced) -- see sync/archives/eso_raw.py's own docstring for why
these are two separate archive_codes rather than one merged fetch(): dbo.raw
and ivoa.ObsCore share no join key (no observation_id-equivalent field the
way LCO's BLKUID ties lco_floyds/lco_nres raw and reduced frames together,
see sync/archives/_lco_common.py), so there's no reliable way for a single
fetch() to emit one row per physical exposure and upgrade it via the usual
ON CONFLICT (archive_code, archive_obs_id) DO UPDATE path (sync.matcher.
_upsert_holding).

Instead, this runs as a separate step after both archives have synced: once
ESO deposits a Phase 3 reduced product for an exposure that eso_raw already
tracked as a bare raw frame, the raw row is deleted outright rather than
left sitting alongside its now-redundant reduced counterpart -- the user-
facing effect (a raw entry "becomes" its reduced counterpart once ESO
catches up) matches what LCO gets natively, just reconstructed after the
fact instead of during ingestion.

Join key is (star_id, instrument, obs_date) -- not a text/time match against
the archives' own raw_target_name/raw_ra/raw_dec, which would have to
tolerate ESO's independently-generated dp_ids (raw: `HARPS.<exposure
timestamp>`, Phase 3: `ADP.<ingestion timestamp>`, unrelated to each other)
and up to ~43s of drift between a raw frame's mjd_obs and its Phase 3
product's t_min (confirmed live). obs_date is already day-granularity in
this table (both eso.py and eso_raw.py truncate their MJD to .date() before
insert), which absorbs that drift for free in the overwhelming majority of
cases -- the only real edge case is an exposure within roughly a minute of
UTC midnight landing on different calendar days between the two records,
accepted as a rare miss (the raw row just stays around one run longer, not
silently wrong) rather than engineering around the day-precision limit of
a column already shipped this way for every other archive.

Restricted to match_status='matched' on both sides -- an eso_raw row that's
needs_review or skipped has no confirmed star_id, so it can't be reliably
tied to a specific eso row anyway (matching an unconfirmed candidate could
delete the wrong raw row, or none at all when it should).
"""

from __future__ import annotations

import logging

import psycopg

logger = logging.getLogger(__name__)


def reconcile(conn: psycopg.Connection) -> int:
    """Delete eso_raw holdings now superseded by a matched eso (Phase 3)
    counterpart. Idempotent -- safe to run every time either archive syncs,
    a no-op once already reconciled. Returns the number of rows deleted.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM spectroscopy_holdings raw
            WHERE raw.archive_code = 'eso_raw'
            AND raw.match_status = 'matched'
            AND EXISTS (
                SELECT 1 FROM spectroscopy_holdings reduced
                WHERE reduced.archive_code = 'eso'
                AND reduced.match_status = 'matched'
                AND reduced.star_id = raw.star_id
                AND reduced.instrument = raw.instrument
                AND reduced.obs_date = raw.obs_date
            )
            -- Defensive: skip_classifications has a composite FK on
            -- (archive_code, archive_obs_id) -- see db/schema.sql. It should
            -- only ever reference skipped rows, never matched ones, but this
            -- guard turns a design assumption into a hard guarantee rather
            -- than risking an FK violation mid-reconciliation.
            AND NOT EXISTS (
                SELECT 1 FROM skip_classifications sc
                WHERE sc.archive_code = raw.archive_code AND sc.archive_obs_id = raw.archive_obs_id
            )
            """
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted
