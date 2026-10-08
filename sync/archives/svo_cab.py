"""SVO CAB stellar libraries (Spanish Virtual Observatory, CAB/INTA-CSIC) — SSA.

Ten small, curated, empirical stellar spectral libraries on one SVOCat SSA
stack, plus GAUDI on SVO's older sdc.cab.inta-csic.es host. The original five
(2026-08-07) are described first; the five added 2026-10-08 and GAUDI are
described under "Added 2026-10-08" below.

Five small, curated, empirical stellar spectral libraries hosted on the same
DaCHS-flavored SVOCat SSA stack at svo2.cab.inta-csic.es/vocats, same
one-archive-many-instruments shape as oirsa.py -- one shared query function
parameterized by sub-collection path, each row labeled by which library it
came from. Observed, 2026-08-07:

  - MILES     (v3/miles)     -- 985 bright reference stars.
  - STELIB    (v2/stelib)    -- 256 stars.
  - XSL       (v3/xshooter)  -- 912 stars (X-Shooter Spectral Library).
  - CaT       (v2/catlib)    -- 696 stars (Ca II Triplet calibration lib).
  - GBS       (gbs)          -- 241 stars (Gaia FGK Benchmark Stars) -- note
    the different path shape observed: "vocats/gbs/ssap.php", not
    "vocats/v3/gbs/...".

This host 302-redirects every request to svocats.cab.inta-csic.es -- a
bare `curl` without `-L` silently returns an apparently-empty-but-200
response, easy to mistake for "no data here". `requests` follows
redirects by default so this is a non-issue for this module, but worth
noting for anyone testing the endpoint manually.

These are SSA cone-search services, not TAP, but each one's own search-form
help text says "Maximum Search Radius allowed: 180 degrees" -- a genuine
radius, not the IVOA SSA spec's usual "diameter" reading, observed by
querying MILES from two different POS centers (equatorial and polar) with
SIZE=180: identical ~985-row result both times, which a true hemisphere-only
diameter reading could not produce for an all-sky reference-star library.
So a single SIZE=180 query from any POS (0,0 used here) pulls each library's
*entire* catalog in one page -- no pagination, no sky-grid crawl needed at
all, unlike irsa_missions.py's iso_sws/iras_lrs (this project initially
planned for a grid crawl here per the age-old cone-search-only assumption,
but the confirmed-live radius behavior made that unnecessary).

Column names are NOT uniform across the five services (each is configured
independently in SVOCat) -- observed, per collection: the per-row
target-name field is "objname" (MILES, CaT), "name" (STELIB, XSL), or "star"
(GBS); see COLLECTIONS below for the explicit per-collection mapping used
instead of any generic utype-sniffing. Position is uniform, though: every
service exposes a "TargetPos" field (SSA Target.Pos, [ra, dec] in degrees),
observed to be populated on 100% of rows across all five (zero masked
positions in any of them).

No real per-observation date on four of the five: these are static, one-shot
curated libraries (each star observed once, long ago, for the reference
compilation), same "no observation date" shape as feros_gavo.py/rave.py --
observed, no Epoch/mjd-shaped field exists at all for MILES, STELIB,
CaT, or GBS. XSL is the one exception: it carries a real "Epoch" field (MJD)
-- populated on 245 of 912 fits-format rows observed (masked on the
rest, presumably for spectra ingested from the original ESO archive without
a preserved observation date) -- read via clean_float, left None when masked
rather than guessed.

Each real spectrum is served in three parallel formats (VOTable/ASCII/FITS,
"SpecFmt" field) sharing one "AssocID" -- filtered to SpecFmt=='application/
fits' to get one row per real object rather than tripling every count.
CaT additionally serves a paired *error* spectrum under its own AssocID for
every real spectrum (observed: exactly 696 "spec_fits" + 696
"errsp_fits" rows, both formatted application/fits) -- excluded via a
substring check on SpecURL's "errsp"/"spec" label, the same kind of
mime/label-based dedup feros_gavo.py uses for its own paired VOTable/FITS
rows.

archive_url points at "SpecURL" (the real per-row spectrum file link, e.g.
".../miles/ssap.php?ID=0685&label=spec_fits"), not "access_url" (a DataLink
descriptor wrapper around the same file, one extra hop for no benefit here).

No native Gaia column on any of these five (positional/name matching only,
same as most archives here). archive_obs_id is "<collection>:<AssocID>" --
AssocID alone is only unique within one library's own table, not across all
five sharing this one archive_code.

Final, static datasets (none of these libraries has grown since its original
publication) -- one full pull per collection is enough forever, so fetch() is
a no-op once the cursor lists every collection as done, same shape as
rave.py/feros_gavo.py. The cursor tracks collections individually
("collections_done") so that adding one here pulls only the new one; a cursor
from before that key existed ("synced_at" alone) means the original five.

Added 2026-10-08, found in a registry-wide SSA sweep (all observed live):

  - SpeX Prism Library (v2/spex)   -- 475 M/L/T dwarfs (556 epoch rows).
  - Yee 2017          (v2/yee2017) -- 404 Keck/HIRES library spectra
    (Empirical SpecMatch); names come from "sbname" (a SIMBAD identifier,
    e.g. "V* BR Psc"), falling back to "source_name"/"cps_name" -- cps_name
    alone is often a bare HD number ("222038").
  - Chiu 2006         (v2/chiu06)  -- 188 L/T dwarf spectra. TargetPos is
    [0, 0] on its rows (a placeholder, not a position) -- read as no position.
  - NIRSPEC BDSS low-res (v2/bdsslow) -- 53 brown-dwarf spectra.
  - UVES M subdwarfs  (v2/uves)    -- 21 spectra.

Only Yee 2017 serves FITS; the other four serve each spectrum as a VOTable and
a plain-text row only (no application/fits row at all), so their "format" is
text/plain. SpeX carries a real observation date as text ("2007 Oct 12").

GAUDI (sdc.cab.inta-csic.es/gaudivo) is the ground-based preparatory archive
for CoRoT's asteroseismology targets: 2,607 FITS spectra of 1,666 stars from
ELODIE, FEROS, SARG, CORALIE and an "Echelle" instrument, with a real
observation date and position on every row. Its SSA response is not a valid
VOTable (a PARAM declares datatype="long" with value "Multiformat", which
astropy refuses to parse), so its rows are read with a plain <TR>/<TD> scan
instead -- see _parse_gaudi.

webapp's INSTRUMENT_RESOLVING_POWER/INSTRUMENT_WAVELENGTH_RANGE_NM
deliberately have no entry for Gaia FGK Benchmark Stars -- its own per-row
"instrument" field (visible in the raw SSA response, e.g. "ESPaDOnS_tauCet",
"HARPS.Archive_tauCet", "NARVAL_tauCet") shows this collection is itself a
compilation of high-resolution spectra pulled from several different
underlying spectrographs, not one instrument with one citable resolving
power -- same reasoning several other archives here give for omitting a
mixed/uncertain entry rather than guessing a single number.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime

import numpy as np
import requests
from astropy.io.votable import parse_single_table
from astropy.time import Time

from sync.base import RawObservation, clean_float

BASE_URL = "http://svo2.cab.inta-csic.es/vocats"

# path: relative to BASE_URL, e.g. "v3/miles" -> .../vocats/v3/miles/ssap.php
# name_field: which column carries the star's name in this collection's own
#   SSA response (not uniform across the five, observed -- see module
#   docstring).
#   A tuple means "first non-blank of these" (Yee 2017).
# date_field: only set for XSL, the one collection with a real Epoch column.
# text_date_field: SpeX's "2007 Oct 12"-style text date.
# exclude_label_substr: only set for CaT, to drop its paired error spectra.
# format: the one SpecFmt kept per spectrum; application/fits unless the
#   collection serves no FITS at all (see module docstring).
COLLECTIONS = [
    {"path": "v3/miles", "instrument": "MILES", "name_field": "objname"},
    {"path": "v2/stelib", "instrument": "STELIB", "name_field": "name"},
    {"path": "v3/xshooter", "instrument": "XSL", "name_field": "name", "date_field": "Epoch"},
    {"path": "v2/catlib", "instrument": "CaT", "name_field": "objname", "exclude_label_substr": "errsp"},
    {"path": "gbs", "instrument": "Gaia FGK Benchmark Stars", "name_field": "star"},
    {"path": "v2/spex", "instrument": "SpeX Prism Library", "name_field": "name", "format": "text/plain",
     "text_date_field": "dateobs"},
    {"path": "v2/yee2017", "instrument": "Keck/HIRES (Yee 2017)", "name_field": ("sbname", "source_name", "cps_name")},
    {"path": "v2/chiu06", "instrument": "Chiu 2006 L/T dwarfs", "name_field": "name", "format": "text/plain"},
    {"path": "v2/bdsslow", "instrument": "Keck/NIRSPEC (BDSS)", "name_field": "name", "format": "text/plain"},
    {"path": "v2/uves", "instrument": "VLT/UVES (M subdwarfs)", "name_field": "name", "format": "text/plain"},
]
# The collections a pre-"collections_done" cursor had already pulled.
LEGACY_COLLECTIONS = ("v3/miles", "v2/stelib", "v3/xshooter", "v2/catlib", "gbs")

GAUDI_URL = "http://sdc.cab.inta-csic.es/gaudivo/ssap/gaudi_ssap.jsp"
GAUDI_KEY = "gaudi"

# Observed (see module docstring): a radius, not a diameter -- 180
# pulls each library's whole catalog in one page, from any center.
QUERY_SIZE = 180

_session = requests.Session()


def _fetch_collection_rows(path: str) -> np.ma.MaskedArray:
    url = f"{BASE_URL}/{path}/ssap.php"
    resp = _session.get(
        url,
        params={"POS": "0,0", "SIZE": QUERY_SIZE, "REQUEST": "queryData"},
        timeout=(15, 120),
    )
    resp.raise_for_status()
    return parse_single_table(io.BytesIO(resp.content)).array


def _text(value) -> str:
    return "" if np.ma.is_masked(value) else str(value).strip()


def _name(row, name_field) -> str:
    fields = (name_field,) if isinstance(name_field, str) else name_field
    for field in fields:
        name = _text(row[field])
        if name:
            return name
    return ""


def _text_date(value) -> date | None:
    try:
        return datetime.strptime(" ".join(_text(value).split()), "%Y %b %d").date()
    except ValueError:
        return None


def _collection_records(coll: dict) -> list[RawObservation]:
    records = []
    seen = set()
    for row in _fetch_collection_rows(coll["path"]):
        if str(row["SpecFmt"]) != coll.get("format", "application/fits"):
            continue
        exclude_substr = coll.get("exclude_label_substr")
        if exclude_substr and exclude_substr in str(row["SpecURL"]):
            continue

        name = _name(row, coll["name_field"])
        if not name:
            continue

        ra, dec = clean_float(row["TargetPos"][0]), clean_float(row["TargetPos"][1])
        if ra == 0 and dec == 0:
            # Chiu 2006's placeholder, not a real position.
            ra = dec = None

        obs_date = None
        date_field = coll.get("date_field")
        if date_field:
            mjd = clean_float(row[date_field])
            if mjd is not None:
                obs_date = Time(mjd, format="mjd").to_datetime().date()
        elif coll.get("text_date_field"):
            obs_date = _text_date(row[coll["text_date_field"]])

        # SpeX lists one row per epoch of a star, but its AssocID and
        # SpecURL are keyed by star name alone, so a star's later epochs
        # point at the same single file as its first (observed: 556 rows,
        # 475 distinct URLs). Kept once.
        archive_obs_id = f"{coll['path']}:{row['AssocID']}"
        if archive_obs_id in seen:
            continue
        seen.add(archive_obs_id)

        records.append(
            RawObservation(
                archive_obs_id=archive_obs_id,
                archive_url=str(row["SpecURL"]),
                instrument=coll["instrument"],
                obs_date=obs_date,
                ra=ra,
                dec=dec,
                raw_target_name=name.replace("_", " "),
            )
        )
    return records


_GAUDI_ROW = re.compile(r"<TR>(.*?)</TR>", re.S)
_GAUDI_CELL = re.compile(r"<TD>(.*?)</TD>", re.S)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def _parse_gaudi(text: str) -> list[RawObservation]:
    # Cell order observed: id, title, url, format, target, instrument,
    # "ra dec", size, spectral location, width, date, time, exposure.
    records = []
    for row in _GAUDI_ROW.findall(text):
        cells = [_CDATA.sub(r"\1", cell).strip() for cell in _GAUDI_CELL.findall(row)]
        if len(cells) < 11 or cells[3] != "fits" or not cells[4]:
            continue
        try:
            ra, dec = (float(v) for v in cells[6].split())
        except ValueError:
            ra = dec = None
        try:
            obs_date = date.fromisoformat(cells[10])
        except ValueError:
            obs_date = None
        records.append(
            RawObservation(
                archive_obs_id=f"{GAUDI_KEY}:{cells[0]}",
                archive_url=cells[2],
                instrument=f"GAUDI ({cells[5]})" if cells[5] else "GAUDI",
                obs_date=obs_date,
                ra=ra,
                dec=dec,
                raw_target_name=cells[4],
                reduction_status="reduced",
            )
        )
    return records


def _fetch_gaudi() -> list[RawObservation]:
    resp = _session.get(
        GAUDI_URL,
        params={"POS": "0,0", "SIZE": QUERY_SIZE, "REQUEST": "queryData"},
        timeout=(15, 120),
    )
    resp.raise_for_status()
    return _parse_gaudi(resp.text)


def _collections_done(cursor: dict) -> set[str]:
    if "collections_done" in cursor:
        return set(cursor["collections_done"])
    return set(LEGACY_COLLECTIONS) if cursor.get("synced_at") else set()


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    already_done = _collections_done(cursor)
    done = set(already_done)

    records = []
    for coll in COLLECTIONS:
        if coll["path"] not in done:
            records.extend(_collection_records(coll))
            done.add(coll["path"])
    if GAUDI_KEY not in done:
        records.extend(_fetch_gaudi())
        done.add(GAUDI_KEY)

    if done == already_done:
        return [], cursor

    new_cursor = {"synced_at": Time.now().isot, "row_count": len(records), "collections_done": sorted(done)}
    return records, new_cursor
