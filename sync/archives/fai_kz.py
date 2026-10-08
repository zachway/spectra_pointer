"""Fesenkov Astrophysical Institute (FAI), Kazakhstan VO — ObsCore TAP.

Found 2026-10-08 in a registry-wide SSA sweep (ivo://fai.kz/...): a GAVO
DaCHS data center at dachs.fai.kz, same software family as feros_gavo.py/
ondrejov.py. Its SSA services cap the search radius at 90 degrees, but the
same rows are in the standard ivoa.obscore table on its TAP endpoint, one
query for everything. Observed there, dataproduct_type='spectrum':

  - 'KazVO eShel'    -- 512 echelle spectra of 155 bright hot supergiants
    from the Three College Observatory (North Carolina) eShel spectrograph,
    2013-2024, 375-847 nm. Registered 2026-06, still being added to.
  - 'FAI PN Archive' -- 672 spectra of 86 planetary nebulae from FAI's AZT-8
    telescope, 1971-1998, 316-850 nm.
  - 'agn_azt8'       -- 1,055 AGN spectra. Not stellar; excluded.

Every row has an observation time (t_min, MJD), a direct application/fits
access_url and a calib_level. obs_publisher_did is unique per row (observed,
2,239 of 2,239) and is the archive_obs_id.

Positions: s_ra/s_dec are populated on every row but are not ICRS degrees as
ObsCore requires. Found by running the real sync against a scratch database
and comparing each record with the star its name resolved to:

  - 'FAI PN Archive': s_ra is in hours (range 0.31-23.53 across all 672
    rows; e.g. PN K4-46 at 23.156 against a true 347.33 deg), s_dec is
    right. Multiplied by 15 here.
  - 'KazVO eShel': unusable. Rows for one star disagree with each other and
    with the star -- some right, some in hours, many off by 180 deg in RA
    with an unrelated declination (HD 165784: stated dec +51.7, true -21.5).
    Dropped, so these rows match by name only; with the stated positions
    kept, every name match was rejected by the matcher's position check.

Target names are run together on the eShel rows ("AlpCyg", "55Cyg",
"BD+43_1168", "BS2385") -- _clean_name spaces them back into something
SIMBAD-shaped.

Small and still growing, so instead of a one-shot pull the whole selection
is re-pulled once the cursor is older than REFRESH_DAYS -- well under two
thousand rows, and re-seen rows are plain upserts.
"""

from __future__ import annotations

import re

from astropy.time import Time

from sync.base import RawObservation, clean_float, make_tap_service, reduction_status_from_calib_level

TAP_URL = "https://dachs.fai.kz/tap"

# obs_collection -> instrument label. instrument_name alone is "AZT-8" (a
# telescope) for the nebula archive and would collide with the excluded AGN
# rows, so the label is set here.
COLLECTIONS = {
    "KazVO eShel": "TCO eShel",
    "FAI PN Archive": "FAI AZT-8 (PN archive)",
}
# See "Positions" in the module docstring.
RA_IN_HOURS = {"FAI PN Archive"}
POSITION_UNUSABLE = {"KazVO eShel"}

QUERY = """
SELECT obs_publisher_did, obs_collection, access_url, target_name, s_ra, s_dec, t_min, calib_level
FROM ivoa.obscore
WHERE dataproduct_type = 'spectrum' AND access_format = 'application/fits'
  AND obs_collection IN ({collections})
""".format(collections=", ".join(f"'{name}'" for name in COLLECTIONS))

REFRESH_DAYS = 30

_GREEK_CONSTELLATION = re.compile(r"^([A-Z][a-z]{2})(\d?)([A-Z][a-zA-Z]{2})$")
_NUMBER_CONSTELLATION = re.compile(r"^(\d+)([A-Z][a-zA-Z]{2})$")
_CATALOG_NUMBER = re.compile(r"^([A-Za-z]+)(\d.*)$")
_BRIGHT_STAR = re.compile(r"^BS\s+(?=\d)")


def _clean_name(name: str) -> str:
    name = name.replace("_", " ").strip()
    match = _GREEK_CONSTELLATION.match(name)
    if match:  # "AlpCyg" -> "alp Cyg", "Omi1CMa" -> "omi01 CMa"
        letter, index, constellation = match.groups()
        return f"{letter.lower()}{index.zfill(2) if index else ''} {constellation}"
    match = _NUMBER_CONSTELLATION.match(name)
    if match:  # "55Cyg" -> "55 Cyg"
        return f"{match.group(1)} {match.group(2)}"
    match = _CATALOG_NUMBER.match(name)
    if match:  # "HD332757" -> "HD 332757"
        name = f"{match.group(1)} {match.group(2)}"
    # "BS" is the Bright Star Catalogue number, which SIMBAD files under HR.
    return _BRIGHT_STAR.sub("HR ", name)


def _is_fresh(cursor: dict) -> bool:
    synced_at = cursor.get("synced_at")
    if not synced_at:
        return False
    return (Time.now() - Time(synced_at)).jd < REFRESH_DAYS


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    if _is_fresh(cursor):
        return [], cursor

    table = make_tap_service(TAP_URL).run_sync(QUERY).to_table()

    records = []
    for row in table:
        collection = str(row["obs_collection"])
        mjd = clean_float(row["t_min"])
        ra, dec = clean_float(row["s_ra"]), clean_float(row["s_dec"])
        if collection in POSITION_UNUSABLE:
            ra = dec = None
        elif collection in RA_IN_HOURS and ra is not None:
            ra *= 15.0
        records.append(
            RawObservation(
                archive_obs_id=str(row["obs_publisher_did"]),
                archive_url=str(row["access_url"]),
                instrument=COLLECTIONS[collection],
                obs_date=Time(mjd, format="mjd").to_datetime().date() if mjd is not None else None,
                ra=ra,
                dec=dec,
                raw_target_name=_clean_name(str(row["target_name"])),
                reduction_status=reduction_status_from_calib_level(row["calib_level"]),
            )
        )

    new_cursor = {"synced_at": Time.now().isot, "row_count": len(records)}
    return records, new_cursor
