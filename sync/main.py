"""Production entry point: run every implemented archive sync to convergence.

Usage:
    python -m sync.main                              # all implemented archives
    python -m sync.main --only rave galah             # just these
    python -m sync.main --max-pages-per-archive 2      # cap pages, for debugging

Each archive's fetch() is called repeatedly until it returns no new records.
Paginated archives (eso, desi, sdss_v_optical's MJD watermark, ...) converge
to their current edge; static/gated archives (rave, galah, sdss_v_apogee's
one-shot pulls, carmenes) short-circuit to a no-op after their first run via
their own cursor. One archive failing doesn't stop the others — the error is
logged, the connection is rolled back so the failure can't poison the next
archive's transaction, and the driver moves on. Not-yet-implemented archives
(weave, four_most) aren't registered here at all — they have no public data.

gemini_ghost and gemini_igrins both need a GOA_SESSION_COOKIE env var (see
sync/archives/_goa_common.py) -- morgan is headless, so this means logging
into archive.gemini.edu in a browser elsewhere and copying the session
cookie value over by hand before including either in --only. Without it,
those archives fail (handled the same as any other failure) rather than
blocking the rest.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from sync.archives import (
    asiago,
    bess,
    carmenes,
    carmenes_caha,
    carmenes_reiners2018,
    carmenes_tac,
    cfht_cadc,
    chandra,
    dao,
    desi,
    elodie,
    eso,
    eso_raw,
    feros_gavo,
    flashheros_gavo,
    gaia_rvs,
    galah,
    gemini,
    gemini_ghost,
    gemini_igrins,
    gtc,
    harpsn_tng,
    hermes_mercator,
    hpol,
    heros_ondrejov,
    iacob,
    ing,
    irsa_missions,
    irtf_ishell,
    irtf_legacy,
    irtf_spex,
    koa,
    lamost,
    lamost_mrs,
    lbt,
    lco_floyds,
    lco_nres,
    lick,
    mast,
    mast_jwst,
    naoj,
    neid,
    noirlab,
    not_fies,
    oirsa,
    ondrejov,
    polarbase,
    rave,
    ritter_prest,
    salt_hrs,
    sdss_legacy_optical,
    sdss_v_apogee,
    sdss_v_optical,
    sophie,
    subaru_moircs,
    svo_cab,
    vizier_assocdata,
    xmm,
)
from sync.reconcile_eso_raw import reconcile as reconcile_eso_raw
from sync.runner import run_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ARCHIVES = {
    "rave": rave.fetch,
    "galah": galah.fetch,
    "eso": eso.fetch,
    "eso_raw": eso_raw.fetch,
    "cfht_cadc": cfht_cadc.fetch,
    "chandra": chandra.fetch,
    "dao": dao.fetch,
    "gemini": gemini.fetch,
    "gemini_ghost": gemini_ghost.fetch,
    "gemini_igrins": gemini_igrins.fetch,
    "koa": koa.fetch,
    "lamost": lamost.fetch,
    "lamost_mrs": lamost_mrs.fetch,
    "lbt": lbt.fetch,
    "lick": lick.fetch,
    "mast": mast.fetch,
    "mast_jwst": mast_jwst.fetch,
    "noirlab": noirlab.fetch,
    "sdss_v_apogee": sdss_v_apogee.fetch,
    "sdss_v_optical": sdss_v_optical.fetch,
    "sdss_legacy_optical": sdss_legacy_optical.fetch,
    "carmenes": carmenes.fetch,
    "carmenes_caha": carmenes_caha.fetch,
    "carmenes_reiners2018": carmenes_reiners2018.fetch,
    "carmenes_tac": carmenes_tac.fetch,
    "desi": desi.fetch,
    "feros_gavo": feros_gavo.fetch,
    "flashheros_gavo": flashheros_gavo.fetch,
    "asiago": asiago.fetch,
    "harpsn_tng": harpsn_tng.fetch,
    "elodie": elodie.fetch,
    "sophie": sophie.fetch,
    "salt_hrs": salt_hrs.fetch,
    "ing": ing.fetch,
    "naoj": naoj.fetch,
    "neid": neid.fetch,
    "not_fies": not_fies.fetch,
    "oirsa": oirsa.fetch,
    "gtc": gtc.fetch,
    "hermes_mercator": hermes_mercator.fetch,
    "hpol": hpol.fetch,
    "bess": bess.fetch,
    "gaia_rvs": gaia_rvs.fetch,
    "irtf_spex": irtf_spex.fetch,
    "irtf_ishell": irtf_ishell.fetch,
    "irtf_legacy": irtf_legacy.fetch,
    "lco_floyds": lco_floyds.fetch,
    "lco_nres": lco_nres.fetch,
    "ondrejov": ondrejov.fetch,
    "polarbase": polarbase.fetch,
    "svo_cab": svo_cab.fetch,
    "irsa_missions": irsa_missions.fetch,
    "iacob": iacob.fetch,
    "ritter_prest": ritter_prest.fetch,
    "subaru_moircs": subaru_moircs.fetch,
    "xmm": xmm.fetch,
    "heros_ondrejov": heros_ondrejov.fetch,
    "vizier_assocdata": vizier_assocdata.fetch,
}


def sync_archive(
    conn: psycopg.Connection,
    archive_code: str,
    fetch_fn,
    max_pages: int | None = None,
    offline: bool = False,
) -> dict:
    totals: dict[str, int] = {}
    pages = 0
    while max_pages is None or pages < max_pages:
        counts, gaia_degraded = run_sync(conn, archive_code, fetch_fn, offline=offline)
        if gaia_degraded and not offline:
            # A live Gaia TAP call for this page just exhausted its retries
            # and fell back to the local mirror once (see
            # ingest.add_star.add_stars_batch) -- a TAP that's degraded
            # right now is very unlikely to recover a page or two later, so
            # go offline for the rest of this archive's pages instead of
            # paying a multi-minute retry-then-fail cost on every one of
            # them. Per-archive, not global or persisted: the next archive
            # (or the next `sync.main` run) starts back online and gets its
            # own chance to prove Gaia is actually reachable.
            logger.warning(
                "%s: Gaia TAP exhausted retries -- switching to the local "
                "gaia_source_lite_mirror (offline) for the rest of this archive's sync",
                archive_code,
            )
            offline = True
        pages += 1
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        logger.info("%s: page %d -> %s", archive_code, pages, counts)
        if sum(counts.values()) == 0:
            break
    logger.info("%s: done after %d page(s), totals: %s", archive_code, pages, totals)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=sorted(ARCHIVES), help="run only these archives")
    parser.add_argument("--max-pages-per-archive", type=int, default=None, help="cap pages per archive (debugging)")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip live Gaia TAP astrometry lookups for new stars, using the local "
        "gaia_source_lite_mirror instead (see ingest.add_star._fetch_astrometry_offline). "
        "Every archive already does this automatically for the rest of its own run once "
        "its Gaia TAP calls start exhausting retries -- pass this to start every archive "
        "that way from the first page, e.g. when Gaia's TAP+ service is known to already "
        "be down.",
    )
    args = parser.parse_args()

    archive_codes = args.only or sorted(ARCHIVES)

    failed = []
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for archive_code in archive_codes:
            logger.info("%s: starting", archive_code)
            try:
                sync_archive(conn, archive_code, ARCHIVES[archive_code], args.max_pages_per_archive, offline=args.offline)
            except Exception:
                logger.exception("%s: failed", archive_code)
                conn.rollback()
                failed.append(archive_code)

        # eso/eso_raw have no shared join key to upgrade a raw row to
        # reduced in place during fetch() itself (see
        # sync/reconcile_eso_raw.py) -- run as a separate pass instead,
        # once both archives have synced whatever they're going to this run.
        if "eso" in archive_codes or "eso_raw" in archive_codes:
            try:
                reconciled = reconcile_eso_raw(conn)
                if reconciled:
                    logger.info("eso_raw: reconciled %d row(s) superseded by a Phase 3 reduced product", reconciled)
            except Exception:
                logger.exception("eso_raw: reconciliation failed")
                conn.rollback()

    if failed:
        logger.error("archives failed: %s", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
