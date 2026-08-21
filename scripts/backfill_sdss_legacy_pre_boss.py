"""One-off: repoint sdss_legacy_optical's pre-BOSS (SDSS-I/II) rows at a real
DR20 file instead of the dead dr19.sdss.org SkyServer object-explorer page.

Background: sync/archives/sdss_legacy_optical.py's own fix (commit 5b32984)
only reaches instrument='boss' rows with mjd >= FIRST_MJD (55176, ~Dec 2009)
-- that's the true floor of what DR20's allspec catalog even has tagged
'boss'. Everything dated earlier is a leftover from a since-abandoned first-
cut sync (SkyServer's SqlSearch API, blocked by a 403 after ~900K rows, see
that module's docstring) and is permanently outside the current fetch()'s
query -- no cursor reset touches it. Confirmed live 2026-08-21 against a
production sample: every row dated before ~Dec 2009 still carries the dead
dr19.sdss.org link (HTTP 403/text-html, not a timeout).

Also confirmed live 2026-08-21: DR20 has no per-object file at all for these
pre-BOSS reductions (only 3 legacy run2d values exist -- 26, 103, 104 -- none
with a "spectra/lite" tree). The only real product is a per-*plate* file,
spPlate-{plate:04d}-{mjd}.fits (every fiber on that plate as a 2D image, 59MB
for one real sampled plate). webapp/spectrum_viewer.py's
_parse_sdss_legacy_plate reads a single fiber's row out of that file via an
HTTP range fetch (same use_fsspec technique as _parse_desi) rather than
downloading the whole thing -- this script just needs to point archive_url at
the right spPlate file, with the fiber number appended as a query param
(confirmed live: a stray query string doesn't change data.sdss.org's static-
file response, so this survives the actual GET while still being parseable
back out).

plate/mjd/fiberid aren't stored in any dedicated column for these orphaned
rows (spectroscopy_holdings has no archive-specific columns at all) -- they're
recovered directly from the existing dead archive_url's query string
(?plateid=X&mjd=Y&fiberid=Z), avoiding the specobjid bit-unpacking the current
sync module's own docstring says it deliberately avoids.

run2d isn't recoverable from the old URL at all (SkyServer's object-explorer
link never carried it) -- there's no shortcut around checking live which of
the 3 known legacy run2d values (26, 103, 104) actually has a given
plate/mjd's spPlate file. Checked once per unique (plate, mjd) pair via a
HEAD request and cached (typically a few thousand unique pairs across
millions of per-fiber rows, not one probe per row). Preference order 104 ->
103 -> 26 (newest reprocessing first, since a later run2d is a strict
reprocessing of the same raw data when it exists) -- rows whose plate/mjd
isn't found under any of the three are left untouched and counted separately,
never guessed at.

Same two-connection shape as scripts/backfill_harpsn_reduction.py (named
read cursor + separate write connection, since committing on the same
connection that holds a named cursor implicitly closes it). Idempotent: only
selects rows still pointing at dr19.sdss.org, so safe to re-run (e.g. after
extending run2d coverage or fixing a row this pass couldn't resolve).

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.backfill_sdss_legacy_pre_boss
"""

from __future__ import annotations

import logging
import os
import re
import time

import psycopg
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OLD_URL_RE = re.compile(r"plateid=(\d+)&mjd=(\d+)&fiberid=(\d+)")

# Newest-reprocessing-first -- a later run2d is a reprocessing of the same
# raw data when it exists for a given plate/mjd, not an independent product.
RUN2D_CANDIDATES = ["104", "103", "26"]

SPPLATE_URL = "https://data.sdss.org/sas/dr20/spectro/sdss/redux/{run2d}/{plate:04d}/spPlate-{plate:04d}-{mjd}.fits"

HEAD_TIMEOUT_SECONDS = 20
# Static-file server (nginx), not a rate-limited dynamic API like SkyServer's
# SqlSearch (see sync/archives/sdss_legacy_optical.py's docstring on that) --
# a small courtesy delay between distinct new plate/mjd probes is enough,
# no need for SkyServer-style backoff handling.
PROBE_DELAY_SECONDS = 0.05

_run2d_cache: dict[tuple[int, int], str | None] = {}


def _find_run2d(session: requests.Session, plate: int, mjd: int) -> str | None:
    key = (plate, mjd)
    if key in _run2d_cache:
        return _run2d_cache[key]
    found = None
    for run2d in RUN2D_CANDIDATES:
        url = SPPLATE_URL.format(run2d=run2d, plate=plate, mjd=mjd)
        try:
            resp = session.head(url, timeout=HEAD_TIMEOUT_SECONDS, allow_redirects=True)
            if resp.status_code == 200:
                found = run2d
                break
        except requests.RequestException:
            pass
        time.sleep(PROBE_DELAY_SECONDS)
    _run2d_cache[key] = found
    return found


def upgrade_rows(read_conn: psycopg.Connection, write_conn: psycopg.Connection) -> None:
    session = requests.Session()
    batch_size = 2000
    total_fixed = 0
    total_unresolved = 0
    total_unparseable = 0

    with read_conn.cursor(name="sdss_legacy_pre_boss_rows") as read_cur:
        read_cur.execute(
            "SELECT id, archive_url FROM spectroscopy_holdings "
            "WHERE archive_code = 'sdss_legacy_optical' AND archive_url LIKE 'https://dr19.sdss.org/%'"
        )
        while True:
            rows = read_cur.fetchmany(batch_size)
            if not rows:
                break

            ids, new_urls = [], []
            for row_id, archive_url in rows:
                m = OLD_URL_RE.search(archive_url)
                if m is None:
                    total_unparseable += 1
                    continue
                plate, mjd, fiberid = int(m.group(1)), int(m.group(2)), int(m.group(3))
                run2d = _find_run2d(session, plate, mjd)
                if run2d is None:
                    total_unresolved += 1
                    continue
                spplate_url = SPPLATE_URL.format(run2d=run2d, plate=plate, mjd=mjd)
                ids.append(row_id)
                new_urls.append(f"{spplate_url}?fiber={fiberid}")
                total_fixed += 1

            if ids:
                with write_conn.cursor() as write_cur:
                    write_cur.execute(
                        """
                        UPDATE spectroscopy_holdings h
                        SET archive_url = v.archive_url, updated_at = now()
                        FROM (SELECT * FROM unnest(%(ids)s::bigint[], %(urls)s::text[]) AS t(id, archive_url)) v
                        WHERE h.id = v.id
                        """,
                        {"ids": ids, "urls": new_urls},
                    )
                write_conn.commit()

            logger.info(
                "progress: %d fixed, %d unresolved (no matching run2d), %d unparseable url, %d unique plate/mjd probed",
                total_fixed, total_unresolved, total_unparseable, len(_run2d_cache),
            )

    logger.info(
        "done: %d rows fixed, %d rows left unresolved (plate/mjd not found under run2d %s), %d rows had an unparseable archive_url",
        total_fixed, total_unresolved, RUN2D_CANDIDATES, total_unparseable,
    )


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as read_conn, psycopg.connect(os.environ["DATABASE_URL"]) as write_conn:
        upgrade_rows(read_conn, write_conn)


if __name__ == "__main__":
    main()
