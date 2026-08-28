"""One-time (re-runnable/resumable) bulk load: mirror the six
gaiadr3.gaia_source_lite columns shitty_positional_match actually needs
(source_id, ra, dec, pmra, pmdec, phot_g_mean_mag) into a local table, so
sync.positional_fallback._gaia_healpix_pool can query local disk instead of
Gaia's rate-limited TAP+ service -- see db/migrations/0011_gaia_source_lite_mirror.sql
for why, and the project plan doc for the full rollout sequence.

gaia_source_lite has no separate bulk-download files, so this streams ESA's
public *gaia_source* (full, ~150-column) bulk CSV.gz files instead --
confirmed via https://cdn.gea.esac.esa.int/Gaia/gdr3/_catalogue_sizes.txt
(757GB compressed across ~3,386 files) -- and projects each row down to the
6 needed columns before writing anything locally. Each remote file is
streamed (curl, rate-limited | gzip-decompressed in-process) directly into
a COPY, so the ~757GB of compressed source data is never written to disk --
only the final ~150GB local table exists at rest.

Each file's CSV header is parsed to locate the needed columns by name (not
assumed positions -- verified today at source_id=3, ra=6, dec=8, pmra=14,
pmdec=16, phot_g_mean_mag=70, but this script doesn't rely on that staying
true). A file already present in gaia_source_lite_mirror_load_log is
skipped, making an interrupted run resumable without re-downloading
everything or double-loading a file (there's no uniqueness constraint on
source_id itself -- see the migration's docstring for why).

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.load_gaia_source_lite
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.load_gaia_source_lite --limit-rate 5M
"""

from __future__ import annotations

import argparse
import gzip
import io
import logging
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from urllib.request import urlopen

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BUCKET_LIST_URL = "https://gaia.eu-1.cdn77-storage.com/"
BUCKET_PREFIX = "Gaia/gdr3/gaia_source/"
FILE_BASE_URL = "https://cdn.gea.esac.esa.int/"

# gaia_source_lite's own documented columns (see db/migrations/0011's
# docstring) -- the full gaia_source CSVs carry ~150 columns; only these
# are projected into the local table.
NEEDED_COLUMNS = ["source_id", "ra", "dec", "pmra", "pmdec", "phot_g_mean_mag"]

# Conservative default so a one-time ~757GB pull never competes noticeably
# with other users of this shared machine's network link. Override with
# --limit-rate for a different cap (curl's syntax, e.g. "5M", "500K").
DEFAULT_LIMIT_RATE = "10M"

# Verified live (2026-08-27) that ESA's CDN occasionally accepts a connection
# (TCP ESTABLISHED, confirmed via lsof) and then simply stops sending data --
# no error, no close, indefinite stall at 0% CPU on both ends. Without an
# explicit abort condition a single stalled file hangs the whole multi-hour
# load forever, the same class of bug as ingest.add_star's un-timed-out
# synchronous Gaia call found earlier the same day.
#
# --speed-limit/--speed-time (abort below a sustained throughput) was tried
# first but empirically did NOT abort a live reproduction of this exact
# stall even after 3 minutes -- curl's speed measurement apparently doesn't
# reliably cover a stall during the "connected, waiting for the first byte"
# phase specifically. --max-time is the real backstop: an unconditional
# wall-clock cap on the whole request regardless of why it's stuck. Sized
# generously for a ~230MB file even at a slow rate-limit override (15 min
# comfortably covers even a ~250KB/s cap), so it essentially never fires on
# a genuinely-progressing transfer, only a stalled one.
CURL_CONNECT_TIMEOUT_SECONDS = 30
CURL_MAX_TIME_SECONDS = 900
CURL_STALL_SPEED_LIMIT_BYTES = 50_000
CURL_STALL_SPEED_TIME_SECONDS = 30

# Extra margin (beyond CURL_MAX_TIME_SECONDS) before the external `timeout`
# wrapper sends SIGTERM, and again before its --kill-after escalates to
# SIGKILL -- see _open_remote_csv_stream's docstring for why an external
# process, not curl's own flags or an in-process Python thread, is what
# actually enforces this. Not zero, so a transfer that's merely
# slow-but-progressing near the --max-time boundary isn't cut off by a
# race between curl's own timeout and the outer one.
CURL_WATCHDOG_GRACE_SECONDS = 60

# Retries a stalled/failed file this many times (exponential backoff)
# before giving up on it -- same shape as sync.matcher._launch_gaia_job /
# ingest.add_star._launch_gaia_job's existing retry pattern for a flaky
# remote dependency.
LOAD_FILE_ATTEMPTS = 5
LOAD_FILE_BACKOFF_SECONDS = 15

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _list_remote_files() -> list[str]:
    """Every GaiaSource_*.csv.gz key under BUCKET_PREFIX, paginating past
    the bucket API's 1000-key-per-response limit via the S3-style Marker
    param."""
    keys: list[str] = []
    marker = ""
    while True:
        url = f"{BUCKET_LIST_URL}?prefix={BUCKET_PREFIX}&delimiter=/&marker={marker}"
        with urlopen(url, timeout=30) as resp:
            root = ET.fromstring(resp.read())
        page_keys = [el.text for el in root.iter(f"{_S3_NS}Key") if el.text and el.text.endswith(".csv.gz")]
        keys.extend(page_keys)
        truncated = root.findtext(f"{_S3_NS}IsTruncated") == "true"
        if not truncated or not page_keys:
            break
        marker = page_keys[-1]
    return sorted(keys)


def _already_loaded(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM gaia_source_lite_mirror_load_log")
        return {row[0] for row in cur.fetchall()}


def _open_remote_csv_stream(key: str, limit_rate: str) -> tuple[subprocess.Popen, io.TextIOWrapper]:
    """Streams one remote file: curl (rate-limited) -> in-process gzip
    decompression -> text lines. Never writes the compressed file to disk.

    The whole curl invocation is wrapped in the external `timeout` command
    (GNU coreutils), not just curl's own --max-time/--speed-limit flags.
    Verified live (2026-08-27) against morgan's curl 7.61.1 that a Python
    threading.Timer calling proc.kill() -- the first approach tried here --
    does NOT reliably fire for THIS specific call path: it passed every
    isolated reproduction (a standalone script, an instrumented inline
    version, calling _open_remote_csv_stream directly) but never once
    fired for the real stalled download inside the real run() loop, for
    reasons that were never root-caused despite extensive live debugging.
    `timeout` runs entirely outside the Python process/interpreter, so it
    can't be affected by whatever that was. --kill-after is a second
    escalation (SIGKILL) in case the process doesn't respond to timeout's
    initial SIGTERM.
    """
    url = FILE_BASE_URL + key
    proc = subprocess.Popen(
        [
            "timeout", f"--kill-after={CURL_WATCHDOG_GRACE_SECONDS}",
            str(CURL_MAX_TIME_SECONDS + CURL_WATCHDOG_GRACE_SECONDS),
            "curl", "-sL", "--limit-rate", limit_rate, "--fail",
            "--connect-timeout", str(CURL_CONNECT_TIMEOUT_SECONDS),
            "--max-time", str(CURL_MAX_TIME_SECONDS),
            "--speed-limit", str(CURL_STALL_SPEED_LIMIT_BYTES),
            "--speed-time", str(CURL_STALL_SPEED_TIME_SECONDS),
            url,
        ],
        stdout=subprocess.PIPE,
    )
    text_stream = io.TextIOWrapper(gzip.GzipFile(fileobj=proc.stdout), encoding="utf-8")
    return proc, text_stream


def _find_header_and_column_indices(text_stream: io.TextIOWrapper) -> dict[str, int]:
    """Skips the ECSV '#'-prefixed YAML metadata block and returns the
    needed columns' positions from the first plain (non-'#') line, which is
    the real CSV header -- see module docstring for why this is looked up
    by name rather than assumed."""
    for line in text_stream:
        line = line.rstrip("\n")
        if line.startswith("#"):
            continue
        header = line.split(",")
        indices = {name: header.index(name) for name in NEEDED_COLUMNS}
        return indices
    raise ValueError("reached end of stream without finding a non-'#' header line")


def _parse_value(raw: str) -> float | None:
    return float(raw) if raw else None


def _load_one_file(conn: psycopg.Connection, key: str, limit_rate: str) -> int:
    proc, text_stream = _open_remote_csv_stream(key, limit_rate)
    try:
        indices = _find_header_and_column_indices(text_stream)
        row_count = 0
        with conn.cursor() as cur:
            with cur.copy(
                "COPY gaia_source_lite_mirror (source_id, ra, dec, pmra, pmdec, phot_g_mean_mag) FROM STDIN"
            ) as copy:
                for line in text_stream:
                    fields = line.rstrip("\n").split(",")
                    row = (
                        int(fields[indices["source_id"]]),
                        float(fields[indices["ra"]]),
                        float(fields[indices["dec"]]),
                        _parse_value(fields[indices["pmra"]]),
                        _parse_value(fields[indices["pmdec"]]),
                        _parse_value(fields[indices["phot_g_mean_mag"]]),
                    )
                    copy.write_row(row)
                    row_count += 1
            cur.execute(
                "INSERT INTO gaia_source_lite_mirror_load_log (filename, row_count) VALUES (%s, %s)",
                (key, row_count),
            )
        conn.commit()
        return row_count
    finally:
        text_stream.close()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"curl exited {proc.returncode} fetching {key}")


def _load_one_file_with_retry(conn: psycopg.Connection, key: str, limit_rate: str) -> int:
    """Retries a stalled/failed file with backoff -- see
    LOAD_FILE_ATTEMPTS's own comment for why this is needed at all (ESA's
    CDN was observed live accepting a connection and then simply never
    sending data). A failed attempt leaves conn's transaction aborted
    (the COPY/log-insert never committed), so it's rolled back before
    retrying on the same connection."""
    last_exc: Exception | None = None
    for attempt in range(LOAD_FILE_ATTEMPTS):
        try:
            logger.info("load_gaia_source_lite: %s starting (attempt %d/%d)", key, attempt + 1, LOAD_FILE_ATTEMPTS)
            return _load_one_file(conn, key, limit_rate)
        except Exception as exc:
            last_exc = exc
            conn.rollback()
            if attempt < LOAD_FILE_ATTEMPTS - 1:
                delay = LOAD_FILE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "load_gaia_source_lite: %s failed (attempt %d/%d), retrying in %ds: %s",
                    key, attempt + 1, LOAD_FILE_ATTEMPTS, delay, exc,
                )
                time.sleep(delay)
    raise last_exc


def run(conn: psycopg.Connection, limit_rate: str) -> None:
    remote_files = _list_remote_files()
    done = _already_loaded(conn)
    pending = [f for f in remote_files if f not in done]
    logger.info(
        "load_gaia_source_lite: %d files total, %d already loaded, %d remaining",
        len(remote_files), len(done), len(pending),
    )

    skipped: list[str] = []
    for i, key in enumerate(pending, start=1):
        try:
            row_count = _load_one_file_with_retry(conn, key, limit_rate)
            logger.info("load_gaia_source_lite: [%d/%d] %s -> %d rows", i, len(pending), key, row_count)
        except Exception as exc:
            # A file that exhausts every retry is left for a later run
            # (still absent from gaia_source_lite_mirror_load_log, so a
            # rerun retries it) instead of taking down the whole ~757GB
            # load over one persistently uncooperative file -- observed
            # live (2026-08-27/28) that a single file can stall 4+
            # consecutive full retry cycles while the rest of the CDN is
            # healthy, which used to mean losing all overnight progress on
            # every other file too.
            skipped.append(key)
            logger.error("load_gaia_source_lite: [%d/%d] %s exhausted all retries, skipping: %s", i, len(pending), key, exc)

    if skipped:
        logger.warning("load_gaia_source_lite: %d file(s) skipped after exhausting retries (will retry on next run): %s", len(skipped), skipped)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit-rate", default=DEFAULT_LIMIT_RATE, help="curl --limit-rate value, e.g. '10M' (default: %(default)s)")
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        run(conn, args.limit_rate)
    logger.info("load_gaia_source_lite: done")


if __name__ == "__main__":
    main()
