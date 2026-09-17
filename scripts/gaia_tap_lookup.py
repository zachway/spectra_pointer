"""Look up Gaia DR3 astrometry/photometry for an arbitrary list of
source_ids via an authenticated Gaia TAP+ upload cross-match, rather than
pulling from either of this project's own local Gaia tables:
gaia_source_lite_mirror only carries source_id/ra/dec/pmra/pmdec/
phot_g_mean_mag (see db/schema.sql), and stars has parallax/phot_bp_mean_mag/
phot_rp_mean_mag but no radial_velocity or pm at all -- neither has every
column this script fetches. Logging in (rather than querying anonymously,
as backfill_gaia_astrometry.py does) raises the TAP+ server's per-query and
upload-table size limits enough to make a real LEFT JOIN over the whole
input list practical instead of chunking into hundreds of small IN (...)
queries.

Credentials are only ever read interactively (getpass) or from
GAIA_TAP_USER/GAIA_TAP_PASSWORD -- never as a CLI argument, so they can't
leak into shell history or `ps`.

Usage:
    python3 -m scripts.gaia_tap_lookup --input source_ids.txt --output out.csv
"""

from __future__ import annotations

import argparse
import csv
import getpass
import logging
import os
import math
import tempfile

import numpy as np
from astropy.table import Table
from astroquery.gaia import Gaia

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CHUNK_SIZE = 200_000

# Column order matches exactly what was asked for, bp/rp order included.
OUTPUT_COLUMNS = [
    "ra", "dec", "source_id", "pmra", "pm", "pmdec", "parallax",
    "radial_velocity", "phot_g_mean_mag", "phot_rp_mean_mag", "phot_bp_mean_mag",
]

CROSSMATCH_QUERY = """
SELECT
    up.source_id AS up_source_id,
    gs.ra, gs.dec, gs.source_id, gs.pmra, gs.pm, gs.pmdec, gs.parallax,
    gs.radial_velocity, gs.phot_g_mean_mag, gs.phot_rp_mean_mag, gs.phot_bp_mean_mag
FROM tap_upload.chunk_ids AS up
LEFT OUTER JOIN gaiadr3.gaia_source AS gs
ON up.source_id = gs.source_id
"""


def _clean(value):
    """TAP results mask NULLs as np.ma.masked (occasionally NaN for floats)
    rather than None -- normalize both to "" for a clean CSV cell."""
    if value is np.ma.masked:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return value


def read_source_ids(path: str) -> list[int]:
    ids = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                ids.append(int(line))
    return ids


def login() -> None:
    user = os.environ.get("GAIA_TAP_USER") or input("Gaia archive username: ")
    password = os.environ.get("GAIA_TAP_PASSWORD") or getpass.getpass("Gaia archive password: ")
    Gaia.login(user=user, password=password)


def fetch_chunk(chunk: list[int]) -> Table:
    table = Table({"source_id": chunk})
    with tempfile.NamedTemporaryFile(suffix=".xml") as f:
        table.write(f.name, format="votable", overwrite=True)
        job = Gaia.launch_job_async(
            CROSSMATCH_QUERY,
            upload_resource=f.name,
            upload_table_name="chunk_ids",
        )
        return job.get_results()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="text file of gaia source_ids, one per line")
    parser.add_argument("--output", required=True, help="CSV path to write results to")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    args = parser.parse_args()

    source_ids = read_source_ids(args.input)
    logger.info("%d source_ids to look up", len(source_ids))

    login()
    Gaia.ROW_LIMIT = -1  # default TAP result cap is 2000 rows; we need all of them

    failed_chunks: list[list[int]] = []
    try:
        with open(args.output, "w", newline="") as out_f:
            writer = csv.writer(out_f)
            writer.writerow(OUTPUT_COLUMNS)
            for i in range(0, len(source_ids), args.chunk_size):
                chunk = source_ids[i : i + args.chunk_size]
                logger.info("fetching rows %d-%d of %d", i, i + len(chunk), len(source_ids))
                try:
                    result = fetch_chunk(chunk)
                except Exception:
                    logger.exception("chunk starting at row %d failed, skipping for now", i)
                    failed_chunks.append(chunk)
                    continue
                # LEFT JOIN means a source_id with no gaiadr3.gaia_source match
                # comes back with every gs.* column masked/NULL -- write it as
                # the original up_source_id with blanks rather than dropping it.
                for row in result:
                    writer.writerow([
                        _clean(row["ra"]),
                        _clean(row["dec"]),
                        row["up_source_id"],
                        _clean(row["pmra"]),
                        _clean(row["pm"]),
                        _clean(row["pmdec"]),
                        _clean(row["parallax"]),
                        _clean(row["radial_velocity"]),
                        _clean(row["phot_g_mean_mag"]),
                        _clean(row["phot_rp_mean_mag"]),
                        _clean(row["phot_bp_mean_mag"]),
                    ])
                out_f.flush()
    finally:
        Gaia.logout()

    if failed_chunks:
        failed_path = args.output + ".failed_ids.txt"
        with open(failed_path, "w") as f:
            for chunk in failed_chunks:
                for sid in chunk:
                    f.write(f"{sid}\n")
        logger.warning(
            "%d chunk(s) failed (%d ids) -- see %s; rerun with --input %s to retry just those",
            len(failed_chunks), sum(len(c) for c in failed_chunks), failed_path, failed_path,
        )

    logger.info("done, wrote %s", args.output)


if __name__ == "__main__":
    main()
