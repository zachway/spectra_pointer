"""Bulk backfill of Gaia DR3 columns on stars from the Flatiron Institute's
public mirror of the DR3 files, with no queries to the Gaia archive itself.

Fills, for every star still missing them:

  - teff_gspphot / logg_gspphot / mh_gspphot (+ gspphot_checked_at), the same
    columns scripts.backfill_gaia_gspphot fills from Gaia TAP;
  - parallax / phot_bp_mean_mag / phot_rp_mean_mag / has_gaia_rvs /
    has_xp_continuous, the columns an --offline sync leaves blank and
    scripts.backfill_gaia_astrometry fills from Gaia TAP.

Why this exists: the Gaia archive rate-limits for fair use, and a first
GSP-Phot pass over the whole catalog is ~3,500 TAP queries. The mirror at
https://sdsc-users.flatironinstitute.org/~gaia/dr3/ carries gaia_source as
3,386 HDF5 files, one per run of HEALPix level-8 pixels (the pixel is
source_id >> 43, and the range is in the file name), each ~300 MB with one
uncompressed, contiguous dataset per column and rows sorted by source_id. So
a column can be fetched on its own with one HTTP range request: this reads
~10-25 MB per file instead of the whole thing, ~7 s per file.

Only files whose pixel range contains a pending star are touched, and only
the columns those stars need. Each file is one transaction, so an
interrupted run just leaves the remaining stars pending; re-running resumes.
A file that can't be fetched after retries is logged and skipped, and the
script then exits non-zero.

This is the bulk tool, meant to be run by hand (detached) after a large
load. The two TAP scripts stay in scripts/weekly_sync_export.sh for the
handful of stars a normal week adds. Note the astrometry half re-selects
stars whose BP and RP are genuinely absent in Gaia on every run, same as
scripts.backfill_gaia_astrometry.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.backfill_gaia_from_flatiron
    # try it on a few files first
    DATABASE_URL=... python3 -m scripts.backfill_gaia_from_flatiron --limit-files 5
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

import numpy as np
import psycopg
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MIRROR_URL = "https://sdsc-users.flatironinstitute.org/~gaia/dr3/hdf5/GaiaSource/"
_FILE_RE = re.compile(r'href="(GaiaSource_(\d{6})-(\d{6})\.hdf5)"')

# HEALPix level-8 pixel = source_id >> 43 (Gaia DR3 source_id encoding).
_HEALPIX8_SHIFT = 43

GSPPHOT_COLUMNS = ("teff_gspphot", "logg_gspphot", "mh_gspphot")
ASTROMETRY_COLUMNS = ("parallax", "phot_bp_mean_mag", "phot_rp_mean_mag", "has_rvs", "has_xp_continuous")

# The HDF5 metadata needed to locate a few datasets is ~12 small reads at
# this block size; larger blocks only fetch more bytes for the same reads.
_METADATA_BLOCK_SIZE = 65536
FETCH_ATTEMPTS = 4
DEFAULT_WORKERS = 3


def parse_listing(html: str) -> list[tuple[int, int, str]]:
    """(first pixel, last pixel, file name) for every file in the mirror's
    directory listing, both pixels inclusive."""
    return sorted((int(lo), int(hi), name) for name, lo, hi in _FILE_RE.findall(html))


def fetch_columns(file_name: str, columns: tuple[str, ...]) -> dict[str, np.ndarray]:
    """source_id plus the named columns of one mirror file, by exact byte
    range. Runs in a worker process."""
    import fsspec
    import h5py

    url = MIRROR_URL + file_name
    wanted = ("source_id",) + tuple(columns)
    last_exc: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            fs = fsspec.filesystem("https")
            layout = {}
            with fs.open(url, "rb", block_size=_METADATA_BLOCK_SIZE, cache_type="blockcache") as raw:
                with h5py.File(raw, "r") as h5:
                    for name in wanted:
                        dataset = h5[name]
                        offset = dataset.id.get_offset()
                        if offset is None or dataset.chunks is not None:
                            # Not the contiguous layout this relies on --
                            # read it through h5py instead (slower, correct).
                            layout[name] = dataset[...]
                        else:
                            layout[name] = (offset, dataset.id.get_storage_size(), dataset.dtype, dataset.shape)
            out = {}
            for name, where in layout.items():
                if isinstance(where, np.ndarray):
                    out[name] = where
                    continue
                offset, size, dtype, shape = where
                out[name] = np.frombuffer(fs.cat_file(url, start=offset, end=offset + size), dtype=dtype).reshape(shape)
            return out
        except Exception as exc:  # network errors surface as many types
            last_exc = exc
            if attempt < FETCH_ATTEMPTS - 1:
                time.sleep(5 * 2**attempt)
    raise RuntimeError(f"{file_name}: giving up after {FETCH_ATTEMPTS} attempts") from last_exc


def _nullable(values: np.ndarray) -> list[float | None]:
    return [None if v != v else float(v) for v in values]


def apply_file(
    conn: psycopg.Connection, data: dict[str, np.ndarray], gsp_ids: np.ndarray, astro_ids: np.ndarray,
) -> tuple[int, int]:
    """Write one file's values for the pending stars in its pixel range, in
    one transaction. Returns (stars stamped as GSP-Phot checked, stars given
    astrometry)."""
    file_ids = data["source_id"]

    def locate(ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        idx = np.minimum(np.searchsorted(file_ids, ids), len(file_ids) - 1)
        return idx, file_ids[idx] == ids

    with conn.cursor() as cur:
        if len(gsp_ids):
            idx, found = locate(gsp_ids)
            columns = []
            for name in GSPPHOT_COLUMNS:
                # A star the file doesn't list still gets stamped, with
                # NULLs, so it isn't looked up again on every run.
                values = np.where(found, data[name][idx], np.nan)
                columns.append(_nullable(values))
            cur.execute(
                "UPDATE stars s SET teff_gspphot = v.teff, logg_gspphot = v.logg, mh_gspphot = v.mh, "
                "gspphot_checked_at = now() "
                "FROM unnest(%s::bigint[], %s::real[], %s::real[], %s::real[]) AS v(id, teff, logg, mh) "
                "WHERE s.gaia_source_id = v.id",
                (gsp_ids.tolist(), *columns),
            )
        astro_done = 0
        if len(astro_ids):
            idx, found = locate(astro_ids)
            idx = idx[found]
            astro_done = len(idx)
            cur.execute(
                "UPDATE stars s SET parallax = v.plx, phot_bp_mean_mag = v.bp, phot_rp_mean_mag = v.rp, "
                "has_gaia_rvs = v.rvs, has_xp_continuous = v.xp "
                "FROM unnest(%s::bigint[], %s::real[], %s::real[], %s::real[], %s::boolean[], %s::boolean[]) "
                "AS v(id, plx, bp, rp, rvs, xp) WHERE s.gaia_source_id = v.id",
                (
                    astro_ids[found].tolist(),
                    _nullable(data["parallax"][idx]),
                    _nullable(data["phot_bp_mean_mag"][idx]),
                    _nullable(data["phot_rp_mean_mag"][idx]),
                    (data["has_rvs"][idx] == 1).tolist(),
                    (data["has_xp_continuous"][idx] == 1).tolist(),
                ),
            )
    conn.commit()
    return len(gsp_ids), astro_done


def _pending_ids(conn: psycopg.Connection, where: str) -> np.ndarray:
    # Server-side cursor straight into an int64 array: the first run has
    # most of the catalog pending, too many rows to hold as Python tuples.
    with conn.cursor(name="flatiron_pending") as cur:
        cur.itersize = 100_000
        cur.execute(f"SELECT gaia_source_id FROM stars WHERE gaia_source_id IS NOT NULL AND {where}")
        ids = np.fromiter((row[0] for row in cur), dtype=np.int64)
    conn.commit()
    ids.sort()
    return ids


def _in_range(ids: np.ndarray, lo: int, hi: int) -> np.ndarray:
    return ids[np.searchsorted(ids, lo << _HEALPIX8_SHIFT) : np.searchsorted(ids, (hi + 1) << _HEALPIX8_SHIFT)]


def backfill(
    conn: psycopg.Connection,
    files: list[tuple[int, int, str]],
    workers: int = DEFAULT_WORKERS,
    limit_files: int | None = None,
    fetch=fetch_columns,
    executor=None,
) -> dict[str, int]:
    gsp_pending = _pending_ids(conn, "gspphot_checked_at IS NULL")
    # Same test scripts.backfill_gaia_astrometry uses -- see its comment on
    # why BP and RP both NULL, not the boolean flags, marks a pending row.
    astro_pending = _pending_ids(conn, "phot_bp_mean_mag IS NULL AND phot_rp_mean_mag IS NULL")
    logger.info("%d stars pending GSP-Phot, %d pending astrometry", len(gsp_pending), len(astro_pending))

    todo = []
    for lo, hi, name in files:
        gsp_ids, astro_ids = _in_range(gsp_pending, lo, hi), _in_range(astro_pending, lo, hi)
        if len(gsp_ids) or len(astro_ids):
            columns = (GSPPHOT_COLUMNS if len(gsp_ids) else ()) + (ASTROMETRY_COLUMNS if len(astro_ids) else ())
            todo.append((name, columns, gsp_ids, astro_ids))
    if limit_files is not None:
        todo = todo[:limit_files]
    logger.info("%d of %d mirror files hold pending stars", len(todo), len(files))

    totals = {"files": 0, "files_failed": 0, "gspphot_checked": 0, "astrometry_filled": 0}
    own_executor = executor is None
    if own_executor:
        # spawn, not fork: fsspec's event loop thread doesn't survive a fork.
        executor = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
    try:
        queue = iter(todo)
        in_flight = {}

        # At most `workers` files are fetched or waiting to be written at a
        # time, so a slow database never piles fetched files up in memory.
        def submit_next() -> None:
            item = next(queue, None)
            if item is not None:
                in_flight[executor.submit(fetch, item[0], item[1])] = item

        for _ in range(workers):
            submit_next()
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                name, _columns, gsp_ids, astro_ids = in_flight.pop(future)
                try:
                    gsp_done, astro_done = apply_file(conn, future.result(), gsp_ids, astro_ids)
                except Exception:
                    logger.exception("%s: failed, its stars stay pending", name)
                    conn.rollback()
                    totals["files_failed"] += 1
                else:
                    totals["files"] += 1
                    totals["gspphot_checked"] += gsp_done
                    totals["astrometry_filled"] += astro_done
                    if totals["files"] % 25 == 0:
                        logger.info("%d/%d files: %s", totals["files"] + totals["files_failed"], len(todo), totals)
                submit_next()
    finally:
        if own_executor:
            executor.shutdown()
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="files fetched at once")
    parser.add_argument("--limit-files", type=int, default=None, help="stop after this many files (to try it out)")
    args = parser.parse_args()

    response = requests.get(MIRROR_URL, timeout=120)
    response.raise_for_status()
    files = parse_listing(response.text)
    if not files:
        raise SystemExit(f"no GaiaSource files found in the listing at {MIRROR_URL}")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        totals = backfill(conn, files, workers=args.workers, limit_files=args.limit_files)
    logger.info("done: %s", totals)
    if totals["files_failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
