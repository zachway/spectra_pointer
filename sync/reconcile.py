"""Periodic full re-walk of archives whose cursor advances on an
observation-time field (t_min/mjd/dateobs/obsjd/start_date/...) rather than
true ingestion order.

Those archives can add or re-release a record with an OLD timestamp after
sync.main's incremental cursor has already moved past it -- reprocessing,
embargo lifting, backfilled historical data. Since insert order into the
source archive isn't guaranteed to track that timestamp column, a plain
`WHERE col > cursor` watermark permanently skips such a record. Confirmed
live for mast.py: real spectra were silently missed this way. sdss_v_apogee
has the same failure shape with a non-chronological string ID instead of a
timestamp. See db/migrations/0006_reconcile_cursor.sql for the schema story.

This module doesn't change any of those archives' fetch() implementations --
it reuses them verbatim against a second, independent cursor
(archive_sync_state.reconcile_cursor) that starts at the beginning of an
archive's history and walks forward. Once a run catches all the way up (a
page returns zero records), the cursor is reset back to the start so the
*next* scheduled run re-walks from scratch again -- a continuous rolling
re-walk, not a one-shot backfill. Re-ingesting an already-known record is a
safe no-op (sync.matcher upserts on (archive_code, archive_obs_id)), so this
never risks duplicating rows the live sync_cursor already picked up.

--max-pages-per-archive bounds how far a single invocation walks per
archive, so one cron run stays cheap and rate-limit-friendly; progress is
saved after every page, so a big archive (e.g. eso) just takes several
scheduled runs to complete one full cycle before wrapping around again.

Also runs scripts.shitty_positional_match's cheap incremental pass
(skipped_only=True) once per invocation, independent of the per-archive
cursor walk above -- a different kind of "things a plain forward-only pass
misses" problem (a positional-fallback candidate that was never attempted,
not a backfilled record an old cursor skipped), but the same "periodic
maintenance, not the live sync path" home. See
scripts.shitty_positional_match's module docstring for why skipped_only
specifically (not a full pass) is what belongs on this schedule.

Usage:
    python -m sync.reconcile                           # all at-risk archives
    python -m sync.reconcile --only eso mast
    python -m sync.reconcile --max-pages-per-archive 20  # one-off deeper pass
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from scripts import shitty_positional_match
from sync import state
from sync.main import ARCHIVES
from sync.runner import run_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Every archive whose cursor advances on an observation-time-shaped field
# (or, for sdss_v_apogee, a non-chronological string ID) -- audited by
# reading every sync/archives/*.py fetch() against this exact failure mode.
# Excludes: archives with a true ingestion-order ID, assigned at insert
# time (asiago, harpsn_tng, hermes_mercator,
# subaru_moircs, carmenes_caha); fixed/frozen data releases that don't
# backfill within a release (gaia_rvs, galah, lamost, lamost_mrs, rave,
# elodie, feros_gavo, flashheros_gavo, iacob, svo_cab, carmenes); and
# full-scan/full-window designs with no skip risk (desi, sdss_legacy_optical,
# irsa_missions, sophie, bess -- the last two have a *different* known bug,
# permanently no-op-ing once their fixed work list is exhausted, not this
# one). gemini_ghost/gemini_igrins need GOA_SESSION_COOKIE like any other
# sync.main run -- they fail (and are skipped, same as sync.main) without it.
AT_RISK_ARCHIVES = [
    "cfht_cadc",
    "chandra",
    "dao",
    "eso",
    "eso_raw",
    "gemini",
    "gemini_ghost",
    "gemini_igrins",
    "gtc",
    "ing",
    "irtf_ishell",
    "irtf_legacy",
    "irtf_spex",
    "koa",
    "lbt",
    "lco_floyds",
    "lco_nres",
    "lick",
    "mast",
    "mast_jwst",
    "naoj",
    "neid",
    "noirlab",
    "not_fies",
    "oirsa",
    "ondrejov",
    "polarbase",
    "salt_hrs",
    "sdss_v_apogee",
    "sdss_v_optical",
    "xmm",
]


def reconcile_archive(conn: psycopg.Connection, archive_code: str, fetch_fn, max_pages: int | None) -> dict:
    totals: dict[str, int] = {}
    pages = 0
    converged = False
    offline = False
    while max_pages is None or pages < max_pages:
        counts, gaia_degraded = run_sync(conn, archive_code, fetch_fn, "reconcile", offline=offline)
        if gaia_degraded and not offline:
            # Same sticky per-archive fallback as sync.main.sync_archive --
            # see there for why.
            logger.warning(
                "%s: Gaia TAP exhausted retries -- switching to the local "
                "gaia_source_lite_mirror (offline) for the rest of this archive's reconcile",
                archive_code,
            )
            offline = True
        pages += 1
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        logger.info("%s: reconcile page %d -> %s", archive_code, pages, counts)
        if sum(counts.values()) == 0:
            converged = True
            break

    if converged:
        # Reached the end of this archive's history in this run -- wrap the
        # cursor back to the start so next time this re-walks from scratch
        # instead of sitting converged forever, which would just reproduce
        # the original bug (a cursor that only ever moves forward).
        state.record_reconcile_run(conn, archive_code, {}, "success", "reached end of history, wrapped to start", 0)
        logger.info("%s: reconcile reached end of history, wrapped cursor back to start", archive_code)

    logger.info("%s: reconcile done after %d page(s) (converged=%s), totals: %s", archive_code, pages, converged, totals)
    return totals


def reconcile_shitty_positional_match(conn: psycopg.Connection) -> dict:
    """The cheap side of scripts.shitty_positional_match: only match_status=
    'skipped' rows (never yet attempted by that fallback -- see its module
    docstring for why 'skipped' alone means that), so this stays fast enough
    to run on every reconcile pass instead of the full multi-day backlog
    scan."""
    totals = shitty_positional_match.run(conn, skipped_only=True)
    logger.info("shitty_positional_match (skipped_only): %s", totals)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=sorted(AT_RISK_ARCHIVES), help="run only these archives")
    parser.add_argument(
        "--max-pages-per-archive",
        type=int,
        default=10,
        help="cap pages per archive for this run, so it stays bounded (default: 10)",
    )
    args = parser.parse_args()

    archive_codes = args.only or sorted(AT_RISK_ARCHIVES)

    failed = []
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for archive_code in archive_codes:
            logger.info("%s: starting reconcile", archive_code)
            try:
                reconcile_archive(conn, archive_code, ARCHIVES[archive_code], args.max_pages_per_archive)
            except Exception:
                logger.exception("%s: reconcile failed", archive_code)
                conn.rollback()
                failed.append(archive_code)

        try:
            reconcile_shitty_positional_match(conn)
        except Exception:
            logger.exception("shitty_positional_match (skipped_only): reconcile failed")
            conn.rollback()
            failed.append("shitty_positional_match")

    if failed:
        logger.error("reconcile failed for: %s", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
