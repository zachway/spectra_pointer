"""Spitzer Heritage Archive (IRS + MIPS-SED) -- per-AOR metadata, no file retrieval.

Spitzer's IRS spectrograph ran during the 2003-2009 cryogenic mission, so
essentially all of its spectra pre-date 2011 -- irsa_missions.py only covers a
few small curated IRS collections (SASS, IRS standard stars, FEPS, Disks SH,
c2d), not the general IRS archive (e.g. TW Cam's 2004 IRS Stare). This module
covers every IRS staring/mapping AOR plus MIPS-SED (55-95 um low-resolution
spectroscopy), found via the Heritage Archive's own search service.

There is no documented API. IRSA's TAP has no per-AOR Spitzer table, the IVOA
services (ObsCore/SSA/SIA) have no Heritage Archive collection, and
astroquery.sha no longer exists. What does work, found 2026-09-18 by
replaying the SHA web app's own search (a Firefly server): an unauthenticated
POST to sha.ipac.caltech.edu/applications/Spitzer/SHA/CmdSrv/sync?cmd=
tableSearch with a JSON `request` (id "aorByPosition", searchType POSITION),
no cookies or login needed. Undocumented and could change without notice.

Instrument filter syntax (observed): set the wanted instrument's own key to
"all" and BOTH other instruments' keys to "none" (setting them to "all", or
blank, either leaks IRAC/MIPS imaging rows in or errors with "Invalid item in
instrument filter"). MIPS-SED has no filter of its own: MIPS is queried whole
and only rows with modedisplayname == "MIPS SED" are kept (MIPS Phot/MIPS Scan
are imaging). IRS's own modes, observed: "IRS Stare", "IRS Map" (spectra,
kept) and "IRS Peakup Image" (imaging, dropped).

Pagination: no whole-sky query is possible -- radius 5 deg takes ~1s, 10 deg
~34s, 20 deg times out at 120s (observed, IRS-only). Crawled as a fixed
sky grid instead (~5 deg spacing, cos(dec)-scaled RA steps, radius 4.5 deg for
comfortable overlap), one cell per fetch() call, same one-window-per-call
shape as irsa_missions.py's GRID_TASKS. A cell that times out is split into
four half-radius sub-cells (up to MAX_SPLIT_DEPTH times) rather than failing
the whole run. AORs seen in more than one overlapping cell are de-duplicated
within a call; across calls the upsert on archive_obs_id (the AORKEY) handles
it. A finished grid stays a no-op forever after -- Spitzer is retired, nothing
new to discover.

Expected volume: 23,081 AORs in the IRSX (IRS) campaign directories of
/data/SPITZER/SHA/archive/proc/ (counted 2026-09-18), which includes IRS
peak-up imaging AORs this module drops -- so the real IRS Stare/Map count is
somewhat lower, plus a few hundred MIPS-SED. That directory tree is also the
authoritative completeness check: scripts/audit_spitzer_sha_completeness.py
compares it against the DB, and any AORKEY found there but not here is a
coverage hole. Dense fields are slow (observed 57-119s for one cell at Orion,
the Galactic centre and the LMC), so a full crawl takes hours.

Real observation dates (reqbegintime) are present on every row, unlike the
undated irsa_missions collections, so the ordinary positional matcher works.
Positions are the requested target position (RA/Dec, J2000). archive_url is
the AOR's own directory on IRSA's file server (holds raw/, bcd/ and pbcd/
subfolders per channel plus a QualityAnalysis README) -- these are per-exposure
pipeline products, not one finished combined 1D spectrum per target, so
there's no single file to point at.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime

import requests

from sync.base import RawObservation, clean_float

SEARCH_URL = "https://sha.ipac.caltech.edu/applications/Spitzer/SHA/CmdSrv/sync"
FILE_BASE_URL = "https://irsa.ipac.caltech.edu/data/SPITZER"

GRID_STEP_DEG = 5.0
CELL_RADIUS_DEG = 4.5
PAGE_SIZE = 2000
MAX_SPLIT_DEPTH = 2
REQUEST_DELAY_SEC = 0.5
READ_TIMEOUT_SEC = 150
SEARCH_ATTEMPTS = 4
RETRY_BACKOFF_SEC = 10
PROC_URL = f"{FILE_BASE_URL}/SHA/archive/proc/"

# instrument-filter key to enable -> {modedisplayname: instrument label} to keep.
QUERIES = {
    "instrumentFilter_IRS": {
        "IRS Stare": "Spitzer/IRS (Stare)",
        "IRS Map": "Spitzer/IRS (Map)",
    },
    "instrumentFilter_MIPS": {
        "MIPS SED": "Spitzer/MIPS-SED",
    },
}
ALL_FILTER_KEYS = ("instrumentFilter_IRAC", "instrumentFilter_IRS", "instrumentFilter_MIPS")


def _build_grid(step_deg: float) -> list[tuple[float, float]]:
    """cos(dec)-scaled RA/Dec grid covering the full sky -- same construction
    as irsa_missions._build_grid, kept separate since the two modules'
    step/radius choices are tuned independently."""
    cells = []
    dec = -90 + step_deg / 2
    while dec < 90:
        n_ra = max(1, round(360 * math.cos(math.radians(dec)) / step_deg))
        for i in range(n_ra):
            cells.append((i * 360.0 / n_ra, dec))
        dec += step_deg
    return cells


GRID_CELLS = _build_grid(GRID_STEP_DEG)

_session = requests.Session()


def _search(ra: float, dec: float, radius_deg: float, enabled_key: str) -> list[dict]:
    """All AOR rows for one cone, as dicts keyed by real column name. Paged
    by startIdx until a short page comes back."""
    rows: list[dict] = []
    start = 0
    while True:
        request = {
            "startIdx": start,
            "pageSize": PAGE_SIZE,
            "searchType": "POSITION",
            "tbl_id": "aor-tbl",
            "position": f"{ra};{dec};EQ_J2000",
            "positionTabs": "SingleSearch",
            "radius": str(radius_deg),
            "levelaor": "OBS_REQ",
            "level2": "LEVEL2",
            "isMatchByAOR": "false",
            "instrumentFilter": enabled_key,
            "moreOptions": "closed",
            "filterKey": "instrument",
            "id": "aorByPosition",
        }
        for key in ALL_FILTER_KEYS:
            request[key] = "all" if key == enabled_key else "none"
        for attempt in range(SEARCH_ATTEMPTS):
            resp = _session.post(
                SEARCH_URL,
                params={"cmd": "tableSearch"},
                data={"request": json.dumps(request), "cmd": "tableSearch"},
                headers={"x-requested-with": "XMLHttpRequest"},
                timeout=(15, READ_TIMEOUT_SEC),
            )
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, dict):
                break
            # A failed search comes back as a bare list, e.g.
            # [{"success": "false", "error": "..."}] (observed). The backend
            # intermittently answers a perfectly good cone with
            # "DataAccessException: Failed to retrieve data" (observed live,
            # 2026-09-21, on a 0.5 deg cone near Sz 102) -- transient, so
            # retried with a growing pause; anything else fails at once.
            if attempt < SEARCH_ATTEMPTS - 1 and "Failed to retrieve data" in str(body):
                time.sleep(RETRY_BACKOFF_SEC * (attempt + 1))
                continue
            raise RuntimeError(f"SHA search failed: {str(body)[:300]}")
        table = body["tableData"]
        names = [c["name"] for c in table["columns"]]
        # A cone with no matches comes back with "columns" but no "data" key
        # at all (observed, totalRows: 0) -- e.g. an empty IRS cone at
        # (261.8, -72.5), which crashed the first prod run at cell 44.
        page = [dict(zip(names, row)) for row in table.get("data", [])]
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE
        time.sleep(REQUEST_DELAY_SEC)


def _search_with_split(ra: float, dec: float, radius_deg: float, enabled_key: str, depth: int = 0) -> list[dict]:
    try:
        return _search(ra, dec, radius_deg, enabled_key)
    except requests.exceptions.ReadTimeout:
        if depth >= MAX_SPLIT_DEPTH:
            raise
    # Timed out: the cone is too dense for the backend's window. Four
    # half-radius sub-cones offset toward each quadrant cover the parent
    # (overlapping, de-duplicated later by AORKEY).
    half = radius_deg / 2
    rows: list[dict] = []
    for d_ra_sign, d_dec_sign in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        sub_dec = max(-90.0, min(90.0, dec + d_dec_sign * half))
        cos_dec = max(math.cos(math.radians(sub_dec)), 0.05)
        sub_ra = (ra + d_ra_sign * half / cos_dec) % 360.0
        rows.extend(_search_with_split(sub_ra, sub_dec, half, enabled_key, depth + 1))
    return rows


def _aor_folder_url(depth_of_coverage: str | None) -> str | None:
    """'/sha/archive/proc/IRSX003800/r10649856/SPITZER_S_10649856_DOC.fits'
    -> the AOR's own directory on IRSA's file server. The search returns a
    lowercase '/sha/...' path but the real directory is uppercase 'SHA'
    (observed: the lowercase form 404s, matching the web app's own plot URL)."""
    if not depth_of_coverage:
        return None
    directory = str(depth_of_coverage).rsplit("/", 1)[0]
    if not directory.startswith("/sha/"):
        return None
    return f"{FILE_BASE_URL}/SHA/{directory[len('/sha/'):]}/"


_campaign_maps: dict[str, dict[str, str]] = {}


def _campaign_map(prefix: str) -> dict[str, str]:
    """{aorkey: campaign directory} for every processed AOR under the
    /proc/<prefix>*/ campaign directories of IRSA's file tree (IRSX = IRS,
    MIPS = MIPS), built once per process on first use -- ~77 small directory
    listings for IRSX."""
    if prefix not in _campaign_maps:
        listing = _session.get(PROC_URL, timeout=(15, 120))
        listing.raise_for_status()
        campaigns = sorted(c for c in set(re.findall(r'href="([A-Z0-9]+)/"', listing.text)) if c.startswith(prefix))
        found: dict[str, str] = {}
        for campaign in campaigns:
            resp = _session.get(f"{PROC_URL}{campaign}/", timeout=(15, 120))
            resp.raise_for_status()
            for aorkey in set(re.findall(r'href="r(\d+)/"', resp.text)):
                found[aorkey] = campaign
            time.sleep(REQUEST_DELAY_SEC)
        _campaign_maps[prefix] = found
    return _campaign_maps[prefix]


def _folder_url_from_tree(aorkey: str, instrument: str) -> str | None:
    """The AOR's directory, found via the file tree instead of the search row.
    Needed because ~10% of real IRS Stare rows come back from the search with
    depthofcoverage None (observed: Beta Pictoris AOR 4888320, HD 163466,
    HD 172728) even though their processed folder exists -- dropping them
    left 1,805 IRSX AORs (mostly, but not only, legitimately dropped peak-up
    imaging and engineering requests) missing from the first full prod crawl,
    ~10% of them real Stare observations. An AOR with no folder in the tree at
    all has no products to point at and stays dropped."""
    prefix = "MIPS" if instrument.startswith("Spitzer/MIPS") else "IRSX"
    campaign = _campaign_map(prefix).get(str(aorkey))
    return f"{PROC_URL}{campaign}/r{aorkey}/" if campaign else None


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).split(".")[0], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _to_observation(row: dict, instrument: str) -> RawObservation | None:
    aorkey = row.get("reqkey")
    if not aorkey:
        return None
    url = _aor_folder_url(row.get("depthofcoverage")) or _folder_url_from_tree(aorkey, instrument)
    if url is None:
        return None
    start = _parse_date(row.get("reqbegintime"))
    return RawObservation(
        archive_obs_id=str(aorkey),
        archive_url=url,
        instrument=instrument,
        obs_date=start.date() if start else None,
        program_id=str(row["progid"]) if row.get("progid") else None,
        ra=clean_float(float(row["raj2000"])) if row.get("raj2000") else None,
        dec=clean_float(float(row["decj2000"])) if row.get("decj2000") else None,
        raw_target_name=(row.get("targetname") or "").strip() or None,
        reduction_status="reduced",
    )


def _fetch_cell(ra: float, dec: float) -> list[RawObservation]:
    by_aorkey: dict[str, RawObservation] = {}
    for enabled_key, keep in QUERIES.items():
        for row in _search_with_split(ra, dec, CELL_RADIUS_DEG, enabled_key):
            instrument = keep.get(row.get("modedisplayname"))
            if instrument is None:
                continue
            obs = _to_observation(row, instrument)
            if obs is not None:
                by_aorkey[obs.archive_obs_id] = obs
        time.sleep(REQUEST_DELAY_SEC)
    return list(by_aorkey.values())


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    cell_index = cursor.get("cell", 0)
    if cell_index >= len(GRID_CELLS):
        return [], cursor

    # Walks forward past empty cells within this one call, returning only
    # once a cell has records (or the grid ends). sync.main.sync_archive
    # stops its page loop as soon as a page's match counts sum to zero, so
    # returning an empty page for an empty sky cell would end the whole run
    # as if the archive had converged -- observed on prod: the first resumed
    # run stopped at empty cell 44 after one page, with 1,600+ cells left.
    while cell_index < len(GRID_CELLS):
        ra, dec = GRID_CELLS[cell_index]
        records = _fetch_cell(ra, dec)
        cell_index += 1
        if records:
            return records, {"cell": cell_index}
    return [], {"cell": cell_index}
