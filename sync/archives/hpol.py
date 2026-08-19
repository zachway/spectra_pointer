"""HPOL -- Wisconsin H-alpha/HPOL spectropolarimeter archive (STScI), decommissioned 2004.

Not present in MAST's modern VO-TAP obscore table alongside its siblings
IUE/WUPPE/EUVE/... that mast.py already covers there (observed:
obs_collection='HPOL' returns 0 rows against mast.stsci.edu/vo-tap/api/
v0.1/caom) -- needs its own scraper against the legacy search.php form.

Metadata: a plain POST to archive.stsci.edu/hpol/search.php with every
filter field blank returns the *entire* archive in one response when
outputformat=CSV and max_records is set past the true total -- 4,836
data rows (matching the archive's own "N rows displayed, but
4836 are available" banner text seen at the form's default max_records=
5001), so no incremental pagination is needed for the metadata pull
itself, same one-shot-bulk-dump shape as elodie.py/rave.py. The form's
own JS (preprocess() in /javascript/js.js) just serializes whatever's
already sitting in the "Search Output Columns" multi-select into a hidden
selectedColumnsCsv field before submit -- reproduced directly here
instead of needing a real browser, using the columns the form ships
pre-populated with in its HTML (see OUTPUT_COLUMNS below). RA/Dec come
back space-separated sexagesimal ("00 06 26.400"/"+64 11 46.01"), parsed
via SkyCoord same as bess.py. Obs date is "YYYY-MM-DD HH:MM:SS" (older
rows carry a real time-of-day; many later rows carry a literal
"00:00:00" sentinel for unknown time-of-day, date still real either way)
-- only .date() is used. The server's own CSV output isn't properly
comma-escaped: a handful of Category values contain a literal comma
("ASTEROIDS, ETC.", observed, 6 of 4,836 rows) which shifts every
field after it -- harmless here since only the first 5 columns (Data ID,
Target Name, RA, DEC, Obs Start Time) are ever read, all of which sit
safely before the ragged tail, so a plain split(",") is used instead of
a real CSV parser.

Download URL: NOT a single deterministic pattern across the whole
archive, despite an initial spot-check (HEAD on one hpolret_{id}_hw.fits.gz
URL) suggesting one. HPOL's detector was upgraded from a Reticon to a CCD
partway through the mission, and the real filename prefix is "hpolret_"
for some ids and "hpolccd_" for others. Observed this is NOT
reliably derivable from the Data ID's own shape or date: e.g.
"10-cas_19971019b" (has a blue/red channel-split "b" suffix, CCD era) is
hpolccd_, but "mars_19921027b"/"venus_19910529b" (the same "b"-suffix
shape, nearby years) are hpolret_ -- and the two eras genuinely overlap
(1991-1992 has both, observed on real rows from each). So this
resolves each id's real filename off its own Apache directory listing at
missions/hpol/data/{data_id}/ instead of guessing (observed,
always exactly 2 files per id across a random 30-id sample -- {prefix}_
{data_id}_hw.fits.gz + a companion .lis.gz, prefix always one of
hpolret/hpolccd) -- one extra GET per record (~280ms observed), which is
why this is paginated in bounded per-call batches (see BATCH_SIZE) even
though the metadata pull itself is a single one-shot request. Falls back
to the directory-listing URL itself (always real, always live, just
without a resolved filename) on the rare chance a listing doesn't parse
as expected -- never fabricates a filename.

reduction_status intentionally left unset -- no calib_level-style column
here and no established fact yet about whether hpolret_*_hw.fits.gz /
hpolccd_*_hw.fits.gz are raw or reduced products, unlike e.g. bess.py's
documented FITS-format-checked upload requirement.
"""

from __future__ import annotations

from datetime import date, datetime

import astropy.units as u
import requests
from astropy.coordinates import SkyCoord
from bs4 import BeautifulSoup

from sync.base import RawObservation

SEARCH_URL = "https://archive.stsci.edu/hpol/search.php"
DATA_DIR_URL = "https://archive.stsci.edu/missions/hpol/data/{data_id}/"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; spectra-pointer-sync/1.0)"}

# Reproduces the form's default-populated "Search Output Columns" list
# (see module docstring) -- what the page's own JS would serialize into
# selectedColumnsCsv on a real browser submit. Only the first 5 (through
# hpol_obs_start_time) are actually read below; the rest are harmless to
# ask for and keep this close to what a real form submission would send.
OUTPUT_COLUMNS = (
    "Mark,hpol_data_id,hpol_target_name,hpol_ra2000,hpol_dec2000,"
    "hpol_obs_start_time,hpol_type,hpol_sptype,hpol_exp_time,"
    "hpol_posangle,hpol_observatory,hpol_ref,hpol_category,hpol_wuppe,ang_sep"
)

# Comfortably past the observed total (4,836) -- the largest option
# the form itself offers (50001), so a full pull works with no further
# pagination on the metadata side even if a few more rows were ever added
# to this decommissioned archive.
MAX_RECORDS = "50001"

# Bounds each fetch() call's real wall-clock cost: the metadata pull
# itself is one request, but resolving each record's real download
# filename needs its own directory-listing GET (~280ms observed each) --
# see module docstring. 200/call keeps a single call to roughly a minute.
BATCH_SIZE = 200

TIMEOUT = (15, 90)

_session = requests.Session()


def _fetch_all_rows() -> list[dict]:
    data = {
        "target": "",
        "resolver": "Resolve",
        "radius": "3.0",
        "ra": "",
        "dec": "",
        "equinox": "J2000",
        "coordformat": "sex",
        "hpol_category[]": "",
        "hpol_data_id": "",
        "hpol_obs_start_time": "",
        "hpol_exp_time": "",
        "extra_column_name_1": "hpol_data_id",
        "extra_column_value_1": "",
        "extra_column_name_2": "hpol_data_id",
        "extra_column_value_2": "",
        "selectedColumnsCsv": OUTPUT_COLUMNS,
        "ordercolumn1": "hpol_data_id",
        "ordercolumn2": "",
        "ordercolumn3": "",
        "outputformat": "CSV",
        "max_records": MAX_RECORDS,
        "max_rpp": "5000",
        "action": "Search",
    }
    response = _session.post(SEARCH_URL, data=data, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()

    lines = response.text.splitlines()
    rows = []
    # lines[0] is the column-header row, lines[1] is a type-declaration
    # row (e.g. "lstring,ustring,ra,dec,datetime,..."); both skipped.
    for line in lines[2:]:
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 5:
            continue
        rows.append(
            {
                "data_id": parts[0].strip(),
                "target_name": parts[1].strip(),
                "ra": parts[2].strip(),
                "dec": parts[3].strip(),
                "obs_time": parts[4].strip(),
            }
        )
    return rows


def _parse_coords(ra_str: str, dec_str: str) -> tuple[float, float] | tuple[None, None]:
    if not ra_str or not dec_str:
        return None, None
    try:
        coord = SkyCoord(ra=ra_str, dec=dec_str, unit=(u.hourangle, u.deg))
    except ValueError:
        return None, None
    return coord.ra.deg, coord.dec.deg


def _parse_date(obs_time: str) -> date | None:
    try:
        return datetime.strptime(obs_time, "%Y-%m-%d %H:%M:%S").date()
    except ValueError:
        return None


def _resolve_archive_url(data_id: str) -> str:
    listing_url = DATA_DIR_URL.format(data_id=data_id)
    try:
        response = _session.get(listing_url, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        return listing_url
    soup = BeautifulSoup(response.text, "html.parser")
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if href.endswith("_hw.fits.gz") and (href.startswith("hpolret_") or href.startswith("hpolccd_")):
            return listing_url + href
    return listing_url


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    if cursor.get("synced_at"):
        return [], cursor

    rows = cursor.get("rows")
    if rows is None:
        rows = _fetch_all_rows()
        idx = 0
    else:
        idx = cursor.get("idx", 0)

    batch = rows[idx : idx + BATCH_SIZE]
    records = []
    for row in batch:
        ra, dec = _parse_coords(row["ra"], row["dec"])
        records.append(
            RawObservation(
                archive_obs_id=row["data_id"],
                archive_url=_resolve_archive_url(row["data_id"]),
                instrument="HPOL",
                obs_date=_parse_date(row["obs_time"]),
                ra=ra,
                dec=dec,
                raw_target_name=row["target_name"] or None,
            )
        )

    idx += len(batch)
    if idx >= len(rows):
        new_cursor = {"synced_at": datetime.now().isoformat(), "row_count": len(rows)}
    else:
        new_cursor = {"rows": rows, "idx": idx}

    return records, new_cursor
