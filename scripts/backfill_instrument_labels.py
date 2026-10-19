"""One-off: bring holdings already in prod in line with the instrument-label
cleanup of 2026-10-19 (see the sync/archives/* modules it touched).

The sync modules now emit clearer, consistent instrument labels -- this
relabels the rows synced before that, and removes two sets of rows that
were never stellar spectra:

  * Relabels (UPDATE ... SET instrument): case-only duplicates folded
    together (ING's "WHT/ISIS RED ARM" -> "WHT/ISIS red arm", Asiago's
    "echelle hi-res Spectrograph"), ESO raw's commissioning-era "SHOOT" ->
    "XSHOOTER", MAST's mission-less labels (IUE/EUVE/ORFEUS prefixes, GHRS,
    HASP/HSLA coadds, and "FUV" split into HUT vs FUSE by archive_url),
    NOIRLab's lowercase API codes, IACOB's telescope-not-instrument labels,
    Lick, NOVA, Gemini, OIRSA -- and 43 eso rows whose instrument came back
    blank, which ESO's own TAP now reports as GIRAFFE (checked 2026-10-19).
  * Deletes: noirlab "sami" (SOAR Adaptive Module Imager -- an imager,
    31,558 rows) and eso "APEXHET" (APEX submm heterodyne receiver, 253
    rows; eso_raw already excluded it). Both are now excluded at sync time.
    Rows a skip_classifications vote still references are left in place
    (its FK is RESTRICT) and counted instead.

Dry run by default (prints per-step row counts, rolls back); --apply
commits. Idempotent: a second run finds nothing left to change.

After applying: re-run scripts.export_to_parquet so the precomputed
instruments/leaderboard tables pick up the new labels, then redeploy the
webapp (its wavelength/R lookups are keyed on the new labels).

Usage:
    DATABASE_URL=... python3 -m scripts.backfill_instrument_labels [--apply]
"""

from __future__ import annotations

import argparse
import logging
import os

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# (archive_code, old instrument, new instrument)
RELABELS = [
    ("ing", "WHT/ISIS RED ARM", "WHT/ISIS red arm"),
    ("ing", "WHT/ISIS BLUE ARM", "WHT/ISIS blue arm"),
    ("asiago", "echelle hi-res Spectrograph", "Echelle Hi-Res Spectrograph"),
    ("eso_raw", "SHOOT", "XSHOOTER"),
    ("eso", "", "GIRAFFE"),
    ("mast", "SWP", "IUE/SWP"),
    ("mast", "LWP", "IUE/LWP"),
    ("mast", "LWR", "IUE/LWR"),
    ("mast", "DS/S", "EUVE/DS/S"),
    ("mast", "HRS/1", "GHRS/1"),
    ("mast", "HRS/2", "GHRS/2"),
    ("mast", "BEFS", "ORFEUS/BEFS"),
    ("mast", "TUES", "ORFEUS/TUES"),
    ("mast", "COS", "COS (coadd)"),
    ("mast", "STIS", "STIS (coadd)"),
    ("mast", "COS-STIS", "COS+STIS (coadd)"),
    ("noirlab", "goodman", "Goodman"),
    ("noirlab", "ghts_blue", "Goodman (blue camera)"),
    ("noirlab", "ghts_red", "Goodman (red camera)"),
    ("noirlab", "chiron", "CHIRON"),
    ("noirlab", "echelle", "CTIO 1.5m Echelle"),
    ("noirlab", "kosmos", "KOSMOS"),
    ("noirlab", "arcoiris", "ARCoIRIS"),
    ("noirlab", "triplespec", "TripleSpec"),
    ("noirlab", "cosmos", "COSMOS"),
    ("iacob", "NOT", "FIES (NOT)"),
    ("iacob", "MERCATOR", "HERMES (Mercator)"),
    ("lick", "Lick shane", "Shane 3-m"),
    ("lick", "Lick APF", "APF (Levy)"),
    ("nova_ar", "FIRE-LCO", "FIRE (Magellan)"),
    ("nova_ar", "GNIRS-GEMINI", "GNIRS (Gemini)"),
    ("gemini", "michelle", "Michelle"),
    ("gemini", "TReCS", "T-ReCS"),
    ("gemini", "F2", "Flamingos-2"),
    ("oirsa", "echelle", "CfA Digital Speedometers (echelle)"),
]

# MAST's "FUV" covers two missions; archive_url's path says which.
FUV_SPLIT = [("%/missions/hut/%", "HUT"), ("%/vospectra/fuse%", "FUSE")]

DELETES = [("noirlab", "sami"), ("eso", "APEXHET")]


def run(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for archive_code, old, new in RELABELS:
            # coalesce: the blank eso rows may be '' or NULL.
            cur.execute(
                "UPDATE spectroscopy_holdings SET instrument = %s, updated_at = now() "
                "WHERE archive_code = %s AND coalesce(instrument, '') = %s",
                (new, archive_code, old),
            )
            logger.info("%s: %r -> %r: %d rows", archive_code, old, new, cur.rowcount)

        for pattern, new in FUV_SPLIT:
            cur.execute(
                "UPDATE spectroscopy_holdings SET instrument = %s, updated_at = now() "
                "WHERE archive_code = 'mast' AND instrument = 'FUV' AND archive_url LIKE %s",
                (new, pattern),
            )
            logger.info("mast: 'FUV' (%s) -> %r: %d rows", pattern, new, cur.rowcount)
        cur.execute("SELECT count(*) FROM spectroscopy_holdings WHERE archive_code = 'mast' AND instrument = 'FUV'")
        leftover = cur.fetchone()[0]
        if leftover:
            logger.warning("mast: %d 'FUV' rows matched neither HUT nor FUSE URL shape; left as-is", leftover)

        for archive_code, instrument in DELETES:
            cur.execute(
                "SELECT count(*) FROM spectroscopy_holdings h WHERE h.archive_code = %s AND h.instrument = %s "
                "AND EXISTS (SELECT 1 FROM skip_classifications s "
                "WHERE s.archive_code = h.archive_code AND s.archive_obs_id = h.archive_obs_id)",
                (archive_code, instrument),
            )
            kept = cur.fetchone()[0]
            cur.execute(
                "DELETE FROM spectroscopy_holdings h WHERE h.archive_code = %s AND h.instrument = %s "
                "AND NOT EXISTS (SELECT 1 FROM skip_classifications s "
                "WHERE s.archive_code = h.archive_code AND s.archive_obs_id = h.archive_obs_id)",
                (archive_code, instrument),
            )
            logger.info("%s: deleted %d %r rows (%d kept: referenced by triage votes)",
                        archive_code, cur.rowcount, instrument, kept)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        run(conn)
        if args.apply:
            conn.commit()
            logger.info("committed")
        else:
            conn.rollback()
            logger.info("dry run -- rolled back; pass --apply to commit")


if __name__ == "__main__":
    main()
