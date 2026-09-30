"""One-off: rewrite archive_url to a direct file download for archives whose
sync modules switched from a landing/resolver page to a file URL on
2026-09-30 -- eso, eso_raw, dao, xmm, plus two stale per-URL cases
(irsa_missions' ISO SWS/PWS links, sdss_legacy_optical's old SkyServer rows).

Each new URL is built with the same helper the sync module now uses, so a
backfilled row is byte-identical to what the next sync/reconcile would
upsert over it. That matters: sync.matcher's upsert does
`archive_url = EXCLUDED.archive_url`, so the sync modules must be deployed
(on morgan's checkout, which the weekly sync and reconcile crons run from)
no later than this backfill, or the next re-walk silently reverts rows.

Every endpoint was verified live against random samples of real holdings
before this ran (2026-09-30): eso 150/150 FITS; eso_raw 127/150 (all 23
failures 401 on 2024-26 data, i.e. still proprietary); dao 197/200 FITS
(3 x 403, 2025-26 data); xmm 117/120 tars (3 exposures with no processed
SRSPEC product); ISO SWS/PWS and SkyServer->SAS 5/5 each. No row is
checked individually here -- a link that 401s until an embargo lifts is
still the right link.

Idempotent: each archive only selects rows still carrying its *old* URL
shape, so re-running (or resuming after an interruption) only touches
what's left.

Usage (on morgan, DATABASE_URL preset):
    python3 -m scripts.backfill_direct_archive_urls [--only eso,dao] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os

import psycopg

from sync.archives import dao, eso, irsa_missions, sdss_legacy_optical, xmm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 5000


def _eso_url(archive_obs_id: str, archive_url: str) -> str:
    return eso.FILE_URL.format(dp_id=archive_obs_id)


def _dao_url(archive_obs_id: str, archive_url: str) -> str:
    return dao.file_url(archive_obs_id)


def _xmm_url(archive_obs_id: str, archive_url: str) -> str:
    observation_id, instrument, exposure_id = archive_obs_id.split("_")
    return xmm.file_url(observation_id, instrument, exposure_id)


def _irsa_url(archive_obs_id: str, archive_url: str) -> str:
    return irsa_missions.direct_url(archive_url)


def sdss_specobjid_to_url(specobjid: int) -> str:
    """Decodes an SDSS DR8+ specObjID (plate, fiber, MJD and run2d are
    bit-packed into it) into the same SAS spec-lite URL
    sdss_legacy_optical.py builds from allspec's own columns. Only for the
    658 leftover rows from that module's old SkyServer-based version, which
    stored the specObjID as archive_obs_id and a SkyServer page as
    archive_url -- none of them duplicate a current row (checked
    2026-09-30), so they're rewritten rather than deleted."""
    plate = specobjid >> 50
    fiberid = (specobjid >> 38) & 0xFFF
    mjd = ((specobjid >> 24) & 0x3FFF) + 50000
    run2d_bits = (specobjid >> 10) & 0x3FFF
    n, rest = divmod(run2d_bits, 10000)
    m, p = divmod(rest, 100)
    run2d = f"v{n + 5}_{m}_{p}"
    return sdss_legacy_optical.SPECTRUM_URL.format(run2d=run2d, plate=plate, mjd=mjd, fiberid=fiberid)


def _sdss_url(archive_obs_id: str, archive_url: str) -> str:
    return sdss_specobjid_to_url(int(archive_obs_id))


# (archive_code, old-URL LIKE pattern selecting rows still to rewrite, builder)
REWRITES = [
    ("eso", "https://archive.eso.org/dataset/%", _eso_url),
    ("eso_raw", "https://archive.eso.org/dataset/%", _eso_url),
    ("dao", "https://ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/caom2ops/datalink%", _dao_url),
    ("xmm", "https://nxsa.esac.esa.int/nxsa-web/%", _xmm_url),
    ("irsa_missions", "https://irsa.ipac.caltech.edu/data/SWS/%", _irsa_url),
    ("sdss_legacy_optical", "https://skyserver.sdss.org/%", _sdss_url),
]


def rewrite_archive(read_conn: psycopg.Connection, write_conn: psycopg.Connection,
                    archive_code: str, old_pattern: str, build, dry_run: bool) -> int:
    """Reads on read_conn's named cursor, writes+commits on write_conn --
    committing on the connection holding a named cursor closes it (see
    scripts/backfill_harpsn_reduction.py)."""
    total = 0
    with read_conn.cursor(name=f"direct_urls_{archive_code}") as read_cur:
        read_cur.execute(
            "SELECT id, archive_obs_id, archive_url FROM spectroscopy_holdings "
            "WHERE archive_code = %s AND archive_url LIKE %s",
            [archive_code, old_pattern],
        )
        while True:
            rows = read_cur.fetchmany(BATCH_SIZE)
            if not rows:
                break
            ids = [r[0] for r in rows]
            urls = [build(r[1], r[2]) for r in rows]
            if total == 0:
                logger.info("%s: e.g. %s -> %s", archive_code, rows[0][2], urls[0])
            if not dry_run:
                with write_conn.cursor() as write_cur:
                    write_cur.execute(
                        "UPDATE spectroscopy_holdings AS h SET archive_url = v.url "
                        "FROM (SELECT unnest(%s::bigint[]) AS id, unnest(%s::text[]) AS url) AS v "
                        "WHERE h.id = v.id",
                        [ids, urls],
                    )
                write_conn.commit()
            total += len(rows)
            if total % (BATCH_SIZE * 20) == 0:
                logger.info("%s: %d rows %s so far", archive_code, total, "would be rewritten" if dry_run else "rewritten")
    logger.info("%s: done, %d rows %s", archive_code, total, "would be rewritten" if dry_run else "rewritten")
    return total


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", help="comma-separated archive_codes to limit the run to")
    parser.add_argument("--dry-run", action="store_true", help="count and log examples, write nothing")
    args = parser.parse_args(argv)
    only = set(args.only.split(",")) if args.only else None

    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as read_conn, psycopg.connect(database_url) as write_conn:
        for archive_code, pattern, build in REWRITES:
            if only and archive_code not in only:
                continue
            rewrite_archive(read_conn, write_conn, archive_code, pattern, build, args.dry_run)


if __name__ == "__main__":
    main()
