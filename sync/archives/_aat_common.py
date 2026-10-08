"""Shared AAT Archive (Data Central) fetch logic for aat.py and aat_2df.py.

archives.datacentral.org.au holds every raw frame taken on the 3.9m
Anglo-Australian Telescope since 1990 (observed 2026-10-08: 1,495,279 frames
1990-2026 including calibrations, nothing earlier). It is a different service
from the Data Central TAP galah.py uses -- that one (and the registered Data
Central SSA) only carries reduced survey products, not these frames.

No documented API: the site is a React app over an unauthenticated JSON
endpoint, found by reading its bundle. `POST /api/query/` takes any of
start_date / end_date / instrument / obstype / proID / ra / dec / radius and
answers with a one-element list holding `uuid`, `count`, `next` and up to
100 `results`. Observed behaviour this module relies on:

- Dates: `[start_date, end_date)`, except start_date == end_date, which
  means that one night. A whole-archive or whole-instrument query times out
  (150s+), and so does a cone search, so the walk is one night at a time.
- Paging: re-POST `{"uuid", "next", "previous"}` with the previous page's
  `next.aat_id`. Later pages are not a fixed size (observed 100, 200, 20 for
  a 320-row query) and `next` is still set on the last one -- the stop
  condition is having `count` rows, not a null `next`.
- `obstype` filters on the frame's RUNCMD header, and what that header says
  depends on instrument and era, so no single value means "science frame"
  -- see aat.py for the three cases it has to cover. `obstype="MFOBJECT"`
  is reliable for 2dF+AAOmega, but only together with
  `instrument="2dF+AAOMEGA"` -- that dropdown name, not the raw `instrument`
  value rows carry ('aaomega', later 'AAOMEGA-2dF').
- Each row has J2000 `ra`/`dec` even where the frame's own header is B1950
  (observed on a 1998 HD 108682 frame, 2.8" from SIMBAD). They are the
  telescope pointing, though, and not always on target -- another frame of
  the same star sat ~1.6 deg away -- so position is a backup to OBJECT here,
  as everywhere else in this project.
- A 2dF row carries `fibre_table`: name, ra, dec and index for the *program*
  fibres only. Checked against a downloaded frame's own FIBRES extension
  (400 rows: 310 program, 25 sky, 8 guide, 57 unused/parked) -- the API
  returned exactly the 310, with identical coordinates.
- One exposure is several rows, one per CCD: aat_id is
  YYYYMMDD + CCD digit + 5-digit run number, and the arms of one run carry
  the same OBJECT and the same fibre list (40 of 40 multi-arm exposures in a
  sampled month). Arms are merged into one record per exposure here.

archive_url: there is no per-frame GET URL (`/api/download/` is a POST that
returns a tar.gz), so every record from one night points at
`/results/<uuid>`, the site's own results page for that night's query. The
uuid is minted per query, is not tied to a session, and one still resolved
hours later -- how long Data Central keeps them is not known.

Frames younger than 18 months are proprietary: listed here like any other,
but only the observing team can download them.

Cursor: just the next night to walk. A fetch() call returns one night's
records, skipping forward over nights with none (other instruments on the
telescope, weather) -- sync.runner treats an empty return as "caught up",
so an empty night must not be returned as-is. The walk stops INGEST_LAG_DAYS
short of today so a night isn't passed before its frames have been loaded;
neither module is in sync.reconcile's AT_RISK_ARCHIVES because one night per
page makes a full re-walk far longer than that job's page budget.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta

import astropy.units as u
import requests
from astropy_healpix import lonlat_to_healpix, neighbours

from sync.base import RawObservation

logger = logging.getLogger(__name__)

BASE_URL = "https://archives.datacentral.org.au"
QUERY_URL = f"{BASE_URL}/api/query/"

# The slowest page observed was 32s (a 200-row 2dF page, ~10MB of fibre
# tables); anything that hasn't answered in 3 minutes is one of the query
# shapes that never does.
TIMEOUT = (15, 180)

# See module docstring: don't walk a night before its frames are loaded.
INGEST_LAG_DAYS = 30

# Defensive bound on how many consecutive empty nights one fetch() call will
# step over -- about four years, far longer than any real gap inside either
# module's date range.
MAX_EMPTY_NIGHTS = 1500

ToRecords = Callable[[list[dict], str, date], list[RawObservation]]

# (first night, night to stop before or None, /api/query/ filters). A module
# can need several: no one filter means "science frame" across every
# instrument and era (see aat.py).
Query = tuple[str, "str | None", dict]

_GAIA_NAME = re.compile(r"^(?:gaia[\s_-]*(?:e?dr\d)?[\s_-]*)?(\d{17,19})$", re.IGNORECASE)

# A Gaia source_id's top bits are the nested HEALPix level-12 pixel of the
# source (source_id >> 35; same encoding sync.positional_fallback walks).
# Compared at level 10 (~3.4' pixels) plus the 8 neighbouring pixels, which
# is loose enough for any real proper motion, a fibre on a pixel edge or a
# slightly-off telescope pointing, and still makes a chance agreement for an
# unrelated integer ~1 in a million.
_GAIA_CHECK_LEVEL = 10
_GAIA_CHECK_NSIDE = 2**_GAIA_CHECK_LEVEL
_GAIA_CHECK_SHIFT = 35 + 2 * (12 - _GAIA_CHECK_LEVEL)


def _post(body: dict) -> dict | None:
    resp = requests.post(QUERY_URL, json=body, headers={"Accept": "application/json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    # Observed: a query the server can't satisfy answers 200 with a bare [].
    return data[0] if data else None


def fetch_night(night: date, filters: dict) -> tuple[list[dict], str | None]:
    """Every row for one night under `filters`, plus that query's uuid."""
    iso = night.isoformat()
    first = _post({"start_date": iso, "end_date": iso, **filters})
    if not first or not first.get("results"):
        return [], None

    uuid = first["uuid"]
    count = first.get("count") or 0
    rows = {row["aat_id"]: row for row in first["results"]}
    page = first
    while len(rows) < count and page.get("next"):
        next_id = page["next"]["aat_id"]
        page = _post({"uuid": uuid, "next": next_id, "previous": next_id})
        if not page or not page.get("results"):
            break
        before = len(rows)
        rows.update((row["aat_id"], row) for row in page["results"])
        if len(rows) == before:
            break
    return [rows[aat_id] for aat_id in sorted(rows)], uuid


def results_url(uuid: str) -> str:
    return f"{BASE_URL}/results/{uuid}"


def exposures(rows: list[dict]) -> list[dict]:
    """One row per exposure -- the lowest-aat_id arm of each run. OBJECT is
    part of the key so two runs that happen to share a run number are never
    folded together."""
    best: dict[tuple, dict] = {}
    for row in sorted(rows, key=lambda r: r["aat_id"]):
        key = (row["aat_id"] % 100_000, str(row.get("instrument")).lower(), row.get("OBJECT"))
        best.setdefault(key, row)
    return list(best.values())


def clean_position(ra, dec) -> tuple[float, float] | tuple[None, None]:
    try:
        ra, dec = float(ra), float(dec)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(ra) and math.isfinite(dec)) or not (0 <= ra < 360 and -90 <= dec <= 90):
        return None, None
    return ra, dec


def row_obs_date(row: dict, night: date) -> date:
    try:
        return date.fromisoformat(row["obs_date"])
    except (KeyError, TypeError, ValueError):
        return night


def log_skipped_instruments(archive_code: str, skipped: Counter) -> None:
    if skipped:
        logger.info("%s: skipped science frames from untracked instruments: %s", archive_code, dict(skipped))


def gaia_source_id_from_name(name: str, ra: float | None, dec: float | None) -> int | None:
    """A target named by its bare Gaia source_id (observed on 2021 2dF
    stellar-stream fibres and on 2025 Veloce frames), accepted only when the
    ID's own sky position agrees with the record's -- other surveys' object
    IDs are long integers too."""
    match = _GAIA_NAME.match(name)
    if not match or ra is None or dec is None:
        return None
    source_id = int(match.group(1))
    cell = int(lonlat_to_healpix(ra * u.deg, dec * u.deg, _GAIA_CHECK_NSIDE, order="nested"))
    nearby = {cell, *(int(c) for c in neighbours(cell, _GAIA_CHECK_NSIDE, order="nested"))}
    return source_id if (source_id >> _GAIA_CHECK_SHIFT) in nearby else None


def _night_records(night: date, queries: list[Query], to_records: ToRecords) -> list[RawObservation]:
    by_id: dict[str, RawObservation] = {}
    for start, end, filters in queries:
        if night < date.fromisoformat(start) or (end is not None and night >= date.fromisoformat(end)):
            continue
        rows, uuid = fetch_night(night, filters)
        if not rows:
            continue
        for record in to_records(rows, results_url(uuid), night):
            by_id.setdefault(record.archive_obs_id, record)
    return list(by_id.values())


def fetch(cursor: dict, queries: list[Query], to_records: ToRecords) -> tuple[list[RawObservation], dict]:
    night = date.fromisoformat(cursor.get("next_date", min(start for start, _, _ in queries)))
    last_night = date.today() - timedelta(days=INGEST_LAG_DAYS)

    records: list[RawObservation] = []
    for _ in range(MAX_EMPTY_NIGHTS):
        if night > last_night:
            break
        records = _night_records(night, queries, to_records)
        night += timedelta(days=1)
        if records:
            break
    else:
        logger.warning("aat: stepped over MAX_EMPTY_NIGHTS (%d) empty nights, now at %s", MAX_EMPTY_NIGHTS, night)

    return records, {"next_date": night.isoformat()}
