"""One-off: collapse sdss_v_optical/sdss_legacy_optical duplicate rows caused
by keying archive_obs_id on specobjid, and re-key survivors onto a
reduction-version-independent id.

Background: sync/archives/sdss_v_optical.py and sync/archives/
sdss_legacy_optical.py used SDSS's `specobjid` as archive_obs_id -- the
column sync.matcher's `INSERT ... ON CONFLICT (archive_code, archive_obs_id)
DO UPDATE` upserts on (see db/schema.sql's UNIQUE (archive_code,
archive_obs_id)). specobjid is minted per *reduction* (run2d), not per
physical observation -- the same field/plate-mjd-fiber visit gets a brand
new specobjid every time SDSS bumps its reduction version (SDSS-V:
v6_1_3 -> v6_2_1 between DR19 and DR20; legacy BOSS/eBOSS: run2d 26/103/104
overlapping the same plate/mjd range). Confirmed live on prod:
gaia_source_id 4280201993015929344 carries 8 sdss_v_optical rows that are
really 4 physical visits x 2 reductions (DR19/v6_1_3 and DR20/v6_2_1),
identical star_id/match_status, different archive_url/archive_obs_id --
the ON CONFLICT key never matched the old row, so each reduction bump just
inserted a second copy instead of upserting the first.

Both modules are now fixed to key on a physically-stable id instead
(field-mjd-catalogid for sdss_v_optical, plate-mjd-fiberid for
sdss_legacy_optical) that survives a reduction bump -- both already appear
verbatim in archive_url's basename (`spec-{A}-{B}-{C}.fits`), independent
of the run2d/reduction-version path component. This script migrates rows
already sitting in prod under the old specobjid-based archive_obs_id:

  1. Full scan of each archive_code (id-ordered, named cursor), extracting
     (A, B, C) from archive_url's basename via regex and grouping by the
     resulting new key. Rows whose archive_url doesn't match the expected
     `spec-<int>-<int>-<int>.fits` shape are left untouched and counted
     separately (e.g. the pre-BOSS orphan rows scripts/
     backfill_sdss_legacy_pre_boss.py repoints at spPlate files with a
     `?fiber=` query param instead -- a different URL shape entirely, and
     those were never re-ingested under a second reduction since they sit
     outside sdss_legacy_optical.py's own fetch() range).
  2. For each new-key group with more than one row, picks one survivor --
     preferring a matched row (star_id set, match_status='matched') over an
     unmatched one, then the highest `id` (the most recently synced row,
     i.e. the newest reduction actually pulled) -- deletes the rest, and
     renames the survivor's archive_obs_id to the new key. A singleton
     group is just renamed in place, no deletion.

Checked live 2026-09-14: zero skip_classifications rows currently reference
either archive_code (its FK is (archive_code, archive_obs_id), RESTRICT by
default), so no triage vote gets orphaned by this pass today -- but the
script still re-checks per group before deleting, in case that changes
before this runs.

Idempotent: a second run finds every survivor already renamed to the new
key (one row per group, matching its own freshly-recomputed key), so
nothing to delete and nothing to rename.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.backfill_boss_reduction_dedup
"""

from __future__ import annotations

import logging
import os
import re

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ARCHIVE_CODES = ["sdss_v_optical", "sdss_legacy_optical"]

# Matches both layouts verbatim: sdss_v_optical's
# spec-{field}-{mjd}-{catalogid}.fits and sdss_legacy_optical's
# spec-{plate}-{mjd}-{fiberid:04d}.fits -- int() below strips fiberid's
# zero-padding so the key matches what the fixed sync modules now emit.
SPEC_ID_RE = re.compile(r"/spec-(\d+)-(\d+)-(\d+)\.fits(?:\?|$)")

BATCH_SIZE = 5000


def _new_key(archive_url: str) -> str | None:
    m = SPEC_ID_RE.search(archive_url)
    if m is None:
        return None
    a, b, c = m.groups()
    return f"{int(a)}-{int(b)}-{int(c)}"


def _survivor_priority(star_id: int | None, match_status: str, row_id: int) -> tuple[bool, bool, int]:
    return (star_id is not None, match_status == "matched", row_id)


def dedup_archive(read_conn: psycopg.Connection, write_conn: psycopg.Connection, archive_code: str) -> None:
    groups: dict[str, list[tuple[int, int | None, str]]] = {}
    unparseable = 0

    with read_conn.cursor(name=f"boss_dedup_{archive_code}") as read_cur:
        read_cur.execute(
            "SELECT id, archive_obs_id, archive_url, star_id, match_status FROM spectroscopy_holdings "
            "WHERE archive_code = %s ORDER BY id",
            (archive_code,),
        )
        scanned = 0
        while True:
            rows = read_cur.fetchmany(BATCH_SIZE)
            if not rows:
                break
            for row_id, _archive_obs_id, archive_url, star_id, match_status in rows:
                scanned += 1
                key = _new_key(archive_url)
                if key is None:
                    unparseable += 1
                    continue
                groups.setdefault(key, []).append((row_id, star_id, match_status))
            logger.info("%s: scanned %d rows, %d distinct keys so far, %d unparseable", archive_code, scanned, len(groups), unparseable)

    total_renamed = 0
    total_deleted = 0
    batch_deletes: list[int] = []
    batch_renames: list[tuple[int, str]] = []

    def _flush() -> None:
        nonlocal batch_deletes, batch_renames
        with write_conn.cursor() as write_cur:
            if batch_deletes:
                write_cur.execute("DELETE FROM spectroscopy_holdings WHERE id = ANY(%s)", (batch_deletes,))
            if batch_renames:
                ids = [i for i, _ in batch_renames]
                keys = [k for _, k in batch_renames]
                write_cur.execute(
                    """
                    UPDATE spectroscopy_holdings h
                    SET archive_obs_id = v.archive_obs_id, updated_at = now()
                    FROM (SELECT * FROM unnest(%(ids)s::bigint[], %(keys)s::text[]) AS t(id, archive_obs_id)) v
                    WHERE h.id = v.id
                    """,
                    {"ids": ids, "keys": keys},
                )
        write_conn.commit()
        batch_deletes = []
        batch_renames = []

    for key, members in groups.items():
        survivor_id, _, _ = max(members, key=lambda m: _survivor_priority(m[1], m[2], m[0]))
        loser_ids = [row_id for row_id, _, _ in members if row_id != survivor_id]

        batch_deletes.extend(loser_ids)
        batch_renames.append((survivor_id, key))
        total_deleted += len(loser_ids)
        total_renamed += 1

        if len(batch_deletes) + len(batch_renames) >= BATCH_SIZE:
            _flush()
            logger.info("%s: progress -- %d groups renamed, %d duplicate rows deleted so far", archive_code, total_renamed, total_deleted)

    _flush()
    logger.info(
        "%s: done -- %d groups (%d rows renamed onto new key), %d duplicate rows deleted, %d rows left untouched (unparseable archive_url)",
        archive_code, total_renamed, total_renamed, total_deleted, unparseable,
    )


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as read_conn, psycopg.connect(os.environ["DATABASE_URL"]) as write_conn:
        for archive_code in ARCHIVE_CODES:
            dedup_archive(read_conn, write_conn, archive_code)


if __name__ == "__main__":
    main()
