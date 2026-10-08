"""IRSA space-mission stellar collections — SSA.

Six independent, historical space-mission (or airborne-mission) stellar
spectral collections behind IRSA's one shared SSA service
(irsa.ipac.caltech.edu/SSA?COLLECTION=...), same one-archive-many-
instruments shape as oirsa.py/svo_cab.py. All six observed,
2026-08-07, and share one uniform CAOM-derived SSA column schema (unlike
svo_cab.py's five independently-configured services) -- s_ra/s_dec,
target_name, tmid (MJD), access_url, access_format, calib_level, and
curation_publisherdid are the same column across every collection here.

  - spitzer_sass     -- Spitzer Atlas of Stellar Spectra: 159 stars.
  - spitzer_irs_std  -- Spitzer IRS Standard Stars: 73 stars.
  - iso_sws          -- ISO/SWS.
  - iras_lrs         -- IRAS/LRS Atlas.
  - sofia_exes       -- SOFIA/EXES: 2,580 distinct observations across
    29,212 raw per-order/per-nod FITS files -- ended 2022 (SOFIA retired),
    fully archived, still the single largest collection bundled here.
  - irtf_mearth      -- "Near-Infrared Metallicities, Radial Velocities and
    Spectral Types for 447 Nearby M Dwarfs" (Newton et al. 2014), a
    standalone IRSA table (irtf.mearth_spectra) genuinely separate from the
    IRTF CAOM2 TAP holdings already covered by irtf_spex.py/irtf_ishell.py/
    irtf_legacy.py -- flagged as a known gap in irtf_spex.py's own
    docstring, closed here. 468 stars, 498 spectra.

Three further stellar Spitzer/IRS legacy-survey tables are served not via SSA
but IRSA's TAP service (irsa.ipac.caltech.edu/TAP, schema "spitzer"), added
2026-09-18 after Spitzer's 2003-2009 cryogenic IRS mission turned out to be
almost entirely missing (see TAP_TABLES): spitzer.feps_spectra_v5 (328 solar-
type stars), spitzer.disks_sh_spectra (54 disk-hosting stars) and
spitzer.c2d_irs_spec (1,382 c2d young-stellar-object spectra). Their files are
IPAC .tbl/.dat text tables under irsa.ipac.caltech.edu/data/SPITZER/<dir>/, not
the FITS the SASS/IRS-std viewer parser expects, and none of the three tables
carries an observation date (obs_date stays None, same as the SSA collections
above). Positions and names are real, so name matching works today; positional
matching needs nominal-epoch support in the matcher (not implemented here).

Deliberately excludes every other Spitzer IRSA collection found alongside
these (spitzer_sings, spitzer_m83m33, spitzer_sage, spitzer_s5,
spitzer_5muses, spitzer_ssgss, spitzer_goals) -- confirmed extragalactic/ISM
surveys, not stellar, out of scope. (c2d was previously excluded with them;
its IRS spectra are of young stellar objects, so they are now included via TAP.)

SIZE is a real radius here too, same confirmed-live behavior as svo_cab.py
(not the usual IVOA SSA "diameter" reading): querying irtf_mearth from two
opposite points on the sky (POS=0,90 and POS=180,0) at SIZE=180, and again
at SIZE=360, all returned the identical 498-row result -- consistent only
with SIZE=180 already covering the whole sky, not one hemisphere. Four of
the six collections stay comfortably fast at that whole-sky SIZE=180 despite
their real volume (observed: spitzer_sass 4.7s, spitzer_irs_std 4.4s,
irtf_mearth ~10s, sofia_exes ~20-48s across repeat queries) -- pulled in a
single page each, no pagination needed (see WHOLE_SKY_COLLECTIONS).

iso_sws and iras_lrs are the two exceptions: SIZE=180 hard-times-out
server-side for both ("QUERY_STATUS=ERROR: TransientFault: INTERNAL_SERVER_
ERROR: Job ran but timed out", observed, reproduced twice each) even
though a 2-degree box near Orion only turns up 44/10 real hits -- these two
collections' underlying tables appear to lack a working spatial index (query
latency scales roughly linearly with SIZE, not with area or hit count:
SIZE=20 ~16-18s, SIZE=30 ~27s, SIZE=45 ~48-49s, SIZE=55 already exceeds 75s,
observed for both). SIZE=45 is the largest cell size confirmed to
reliably return within IRSA's apparent timeout window, so these two are
paginated instead via a justified sky-grid crawl (small, historical,
already-closed mission archives -- not a full-sky survey a grid crawl
wouldn't scale to): a fixed 17-cell grid (GRID_CELLS, ~50-degree spacing,
cos(dec)-scaled RA step count) with generous SIZE=45 overlap per cell.
fetch() processes exactly one (collection, cell) pair per call -- same
one-window-per-call shape as ing.py's date-window crawl -- converging after
34 calls (17 cells x 2 collections) plus one final empty call, rather than
one page per collection.

Each real observation is served across 2-3 parallel access_format rows
(a preview image/gif or image/png thumbnail alongside the real science
product) -- filtered per collection to the one real data format (see
WHOLE_SKY_COLLECTIONS/GRID_COLLECTIONS' "format" key), observed per
collection (e.g. spitzer_sass: image/gif + application/fits + text/plain,
159 each; sofia_exes: image/fits + image/png, not evenly split since EXES
emits several raw order/nod files per real observation under one shared
PublisherDID). archive_obs_id is the row's own access_url rather than
curation_publisherdid for exactly that reason -- PublisherDID groups
multiple real distinct files together for sofia_exes, but access_url is
guaranteed unique per real file across every collection here.

target_name is populated directly for iso_sws/sofia_exes/irtf_mearth
(underscore-joined on iso_sws, e.g. "HR4534_BET-LEO" -- cleaned the same
way irtf_spex.py cleans its own underscore-joined names) but is an empty
string on every spitzer_sass/spitzer_irs_std/iras_lrs row (observed)
-- for those three, the name is recovered instead from the tail of
curation_publisherdid (e.g. "ivo://irsa.ipac/spitzer_irs_std/HD 127693" ->
"HD 127693"; "ivo://irsa.ipac/iras_lrs/11210+1707" -> "11210+1707", a real
IRAS Point Source Catalog designation, not a star name -- falls through to
a harmless skip downstream like any other unresolvable identifier).

tmid (MJD) is real and populated on every sofia_exes/irtf_mearth row
(observed, 0 masked in both) but fully masked on every spitzer_sass/
spitzer_irs_std/iso_sws/iras_lrs row observed -- those four are
static, already-fully-reprocessed atlas products with no preserved
per-observation epoch in this table (dataid_date is populated instead, but
that is observed to be a data-*processing*/ingestion timestamp, e.g.
iras_lrs's dataid_date is uniformly "2025-06-04" across every row --
decades after the 1983 IRAS mission -- not a real observation date, so
deliberately not used as a stand-in). obs_date is left None for those four,
same "no real per-observation date" shape as several of svo_cab.py's five
libraries.

calib_level is real and present on every row across all six collections
(observed) -- fed through reduction_status_from_calib_level as usual;
sofia_exes in particular has a genuine mix (2/1/masked observed live),
unlike the other five which are uniformly a single value.

Added 2026-10-08, found in a registry-wide SSA sweep (all observed live):

  - sofia_flitecam -- SOFIA/FLITECAM near-IR grism spectra, whole-sky in one
    page, same schema and same per-file shape as sofia_exes. Real tmid on
    every row. Of its 5,844 FITS rows roughly half are not spectra: FLITECAM
    is also an imager and its filter frames are in the same collection with
    spec_rp of 6 (K band), 89 (Paschen alpha) or masked. Only rows with
    spec_rp >= 500 (the grisms, R ~1,100-1,300) are kept; png previews are
    dropped by format as usual.
  - iso_sws_atlas  -- "An Improved Atlas of Full-Scan Spectra from ISO/SWS":
    2,070 text tables for 817 targets under /data/ISO/SWS_Atlas/, a
    reprocessing distinct from iso_sws's files. Unlike iso_sws it returns
    whole-sky in one page and carries a real tmid on every row.
  - herschel_hifistars -- Herschel/HIFI heterodyne spectra (0.16-0.54 mm) of
    38 evolved stars: 732 FITS files (plus a gif and a text row each,
    dropped), whole-sky in one page. target_name is blank and tmid masked on
    every row, and the publisherdid tail is an obsid, so the name comes from
    the file's directory (".../spectra/IRAS_15194_5115/1342202052-...fits")
    and these match by name only.
  - sofia_forcast  -- SOFIA/FORCAST mid-IR grism spectra (5-37 um): 72,804
    distinct FITS files for 484 targets, the largest collection here. Every
    FITS row sampled is a grism product (spec_rp 110-260, 1,170
    cross-dispersed), dated, with a unique access_url, so no resolving-power
    floor is needed. Whole-sky SIZE=180 times out, so it rides the grid crawl
    with iso_sws/iras_lrs: all 17 cells timed at 56-108 s (8k-19k rows each)
    against the 150 s read timeout. Cells overlap heavily; re-seen files are
    plain upserts. Many targets are not stars (asteroids, planets, comets,
    galaxies) and fall out downstream like any other unresolvable name.
  - BRAVA (Bulge Radial Velocity Assay, CTIO Blanco/Hydra) -- not on the SSA
    path at all in practice (its SSA collection returns no rows); the catalog
    is the TAP table brava.bravacat, 8,585 M giants. Quirks, all observed:
      * No observation date and no target name in the table. The date is
        real in each file's FITS header (DATE-OBS), so it is read from there
        with a ranged request for the first header blocks -- without a date
        these records could never be matched positionally. Many rows share
        one multi-aperture file (e.g. 1004.2.ms.fits, 103 rows), so header
        dates are cached per file.
      * fits_spectra_1/_2 are HTML anchors, not paths. 173 rows have two
        observations (n_obs=2), the second file in fits_spectra_2.
      * The fixed-width ingest split the 2MASS designation across two
        columns for single-observation rows: fits_spectra_2 reads
        "x ... 183" and tmass_id "75172-1503482", i.e. 2MASS
        J18375172-1503482. Rejoined when the result has the right shape
        (SIMBAD knows only a minority of them); rows with two observations
        have no designation.
    archive_obs_id is "brava:<cntr>:<n>" (cntr is the table's own row
    counter), since a file URL is not unique per star. Paged by cntr,
    BRAVA_PAGE_SIZE rows per fetch() call, because of the per-file request.
"""

from __future__ import annotations

import functools
import io
import math
import re
from datetime import date

import requests
from astropy.io.votable import parse_single_table
from astropy.time import Time

from sync.base import RawObservation, clean_float, make_tap_service, reduction_status_from_calib_level

SSA_URL = "https://irsa.ipac.caltech.edu/SSA"
TAP_URL = "https://irsa.ipac.caltech.edu/TAP"
SPITZER_DATA_URL = "https://irsa.ipac.caltech.edu/data/SPITZER"

# Stellar Spitzer/IRS legacy-survey tables served via TAP (see module
# docstring). "files" maps a table column holding a relative file path to the
# instrument label that file is recorded under; a value of "none" (FEPS's
# irs_hi_dat_u when no high-res spectrum exists, observed) means no such file.
# "name_col" is the target-name column, or None when the name has to be
# recovered from the file path's parent directory (c2d, see _tap_target_name).
TAP_TABLES = {
    "spitzer.feps_spectra_v5": {
        "base": f"{SPITZER_DATA_URL}/FEPS",
        "name_col": "name",
        "files": {
            "irs_lo_tbl_u": "Spitzer/IRS (FEPS)",
            "irs_hi_dat_u": "Spitzer/IRS (FEPS high-res)",
        },
    },
    "spitzer.disks_sh_spectra": {
        "base": f"{SPITZER_DATA_URL}/Disks_SH_spectra",
        "name_col": "object",
        "files": {"spectrum_tbl_u": "Spitzer/IRS (Disks SH)"},
    },
    "spitzer.c2d_irs_spec": {
        "base": f"{SPITZER_DATA_URL}/C2D",
        "name_col": None,
        "files": {"tbl_u": "Spitzer/IRS (c2d)"},
    },
}

# Collections whose entire catalog comes back in one whole-sky SIZE=180
# query -- see module docstring for confirmed-live timings/row counts.
WHOLE_SKY_COLLECTIONS = {
    "spitzer_sass": {"instrument": "Spitzer/IRS (SASS)", "format": "application/fits"},
    "spitzer_irs_std": {"instrument": "Spitzer/IRS (Std Stars)", "format": "application/fits"},
    "sofia_exes": {"instrument": "SOFIA/EXES", "format": "image/fits"},
    "irtf_mearth": {"instrument": "IRTF/MEarth", "format": "application/fits"},
    # min_resolving_power: FLITECAM is also an imager, and its filter frames
    # sit in the same SSA collection with spec_rp of 6-89 (or masked).
    "sofia_flitecam": {"instrument": "SOFIA/FLITECAM", "format": "image/fits", "min_resolving_power": 500},
    "iso_sws_atlas": {"instrument": "ISO/SWS (Atlas)", "format": "text/plain"},
    # name_from_url_dir: target_name is blank and curation_publisherdid ends
    # in an obsid, but each file sits in a directory named for its star.
    "herschel_hifistars": {"instrument": "Herschel/HIFI (HIFISTARS)", "format": "application/fits",
                           "name_from_url_dir": True},
}
# The collections a cursor with only the old "whole_sky_done" flag has pulled.
LEGACY_WHOLE_SKY = ("spitzer_sass", "spitzer_irs_std", "sofia_exes", "irtf_mearth")
WHOLE_SKY_QUERY_SIZE = 180

IRSA_URL = "https://irsa.ipac.caltech.edu"
BRAVA_INSTRUMENT = "CTIO Blanco/Hydra (BRAVA)"
BRAVA_PAGE_SIZE = 500
# DATE-OBS sits within the first block or two of every header sampled; ten
# blocks leaves a wide margin without fetching a ~1MB multi-aperture file.
BRAVA_HEADER_BYTES = 28800

# Collections that time out server-side at that SIZE and are crawled via
# GRID_CELLS instead -- see module docstring.
GRID_COLLECTIONS = {
    "iso_sws": {"instrument": "ISO/SWS", "format": "text/plain"},
    "iras_lrs": {"instrument": "IRAS/LRS", "format": "text/plain"},
    # Must stay last: GRID_TASKS is positional, and a cursor that finished
    # the two collections above resumes exactly where this one starts.
    "sofia_forcast": {"instrument": "SOFIA/FORCAST", "format": "application/fits"},
}
GRID_CELL_SIZE = 45
GRID_STEP_DEG = 50


def _build_grid(step_deg: float) -> list[tuple[float, float]]:
    """cos(dec)-scaled RA/Dec grid covering the full sky -- fewer RA steps
    near the poles, where a fixed-degree RA step packs cells much closer
    together on the sky than at the equator."""
    cells = []
    dec = -90 + step_deg / 2
    while dec < 90:
        n_ra = max(1, round(360 * math.cos(math.radians(dec)) / step_deg))
        for i in range(n_ra):
            cells.append((i * 360.0 / n_ra, dec))
        dec += step_deg
    return cells


GRID_CELLS = _build_grid(GRID_STEP_DEG)
# Flat (collection, cell_index) work list the grid crawl advances through
# one entry per fetch() call, same one-window-per-call shape as ing.py.
GRID_TASKS = [(name, i) for name in GRID_COLLECTIONS for i in range(len(GRID_CELLS))]

_session = requests.Session()


def _query(collection: str, pos: tuple[float, float], size: float):
    resp = _session.get(
        SSA_URL,
        params={
            "COLLECTION": collection,
            "POS": f"{pos[0]},{pos[1]}",
            "SIZE": size,
            "REQUEST": "queryData",
        },
        timeout=(15, 150),
    )
    resp.raise_for_status()
    return parse_single_table(io.BytesIO(resp.content)).array


def _clean_publisher_did_name(publisher_did: str) -> str:
    """Recovers a target name from curation_publisherdid's own tail segment
    for the collections whose target_name column is blank (see module
    docstring). spitzer_sass wraps its real name in a "sass_<name>_matched"
    convention (observed, e.g. ".../spitzer_sass/sass_HD152386_matched")
    -- stripped here so "HD152386" actually lines up with SIMBAD-style
    aliases, same reasoning irtf_spex.py gives for stripping its own
    appended "_AV=..." suffix. spitzer_irs_std/iras_lrs tails are already
    the bare identifier (e.g. "HD 127693", "11210+1707") and pass through
    unchanged."""
    name = publisher_did.rsplit("/", 1)[-1]
    if name.startswith("sass_"):
        name = name[len("sass_") :]
    if name.endswith("_matched"):
        name = name[: -len("_matched")]
    return name


# IRSA's SSA still hands out ISO SWS/PWS access_urls under /data/SWS/,
# which now redirects to the ISO collection's index page; the same files
# live under /data/ISO/SWS/ (verified 2026-09-30). archive_obs_id keeps the
# SSA's own URL (it's the upsert key); only the clickable link is fixed.
_STALE_SWS_PREFIX = "https://irsa.ipac.caltech.edu/data/SWS/"
_CURRENT_SWS_PREFIX = "https://irsa.ipac.caltech.edu/data/ISO/SWS/"


def direct_url(access_url: str) -> str:
    if access_url.startswith(_STALE_SWS_PREFIX):
        return _CURRENT_SWS_PREFIX + access_url[len(_STALE_SWS_PREFIX):]
    return access_url


def _to_observation(row, instrument: str) -> RawObservation:
    # Indexed by position (col_N), not by real field name, because astropy's
    # parse_single_table(...).array observed to fall back to synthetic
    # col_N dtype names for this service's VOTable response -- table.fields[i]
    # .name correctly reports the real names (s_ra, target_name, access_url,
    # ...) at these same positions, but arr.dtype.names does not carry them
    # through. Positions observed and stable across all 6 collections
    # (same shared CAOM-derived schema, see module docstring).
    access_url = str(row["col_18"])
    target_name = str(row["col_27"]).strip()
    if not target_name:
        target_name = _clean_publisher_did_name(str(row["col_8"]))

    tmid = clean_float(row["col_31"])
    obs_date = Time(tmid, format="mjd").to_datetime().date() if tmid is not None else None

    return RawObservation(
        archive_obs_id=access_url,
        archive_url=direct_url(access_url),
        instrument=instrument,
        obs_date=obs_date,
        ra=clean_float(row["col_0"]),
        dec=clean_float(row["col_1"]),
        raw_target_name=target_name.replace("_", " "),
        reduction_status=reduction_status_from_calib_level(row["col_5"]),
    )


def _tap_target_name(row, name_col) -> str:
    if name_col is not None:
        return str(row[name_col]).strip()
    # c2d: no name column -- the real target name is the file's parent
    # directory (e.g. ".../IRS_POINTED/RR_Tau/SPITZER_IRS_..._RR_Tau.tbl" ->
    # "RR_Tau"), observed on the first rows sampled.
    return str(row["file_name"]).rstrip("/").split("/")[-2]


def _fetch_tap_spectra() -> list[RawObservation]:
    service = make_tap_service(TAP_URL)
    records = []
    for table, meta in TAP_TABLES.items():
        rows = service.run_sync(f"SELECT * FROM {table}").to_table()
        for row in rows:
            name = _tap_target_name(row, meta["name_col"]).replace("_", " ")
            ra, dec = clean_float(row["ra"]), clean_float(row["dec"])
            for column, instrument in meta["files"].items():
                path = str(row[column]).strip()
                if not path or path.lower() == "none":
                    continue
                url = f"{meta['base']}/{path.removeprefix('./')}"
                records.append(
                    RawObservation(
                        archive_obs_id=url,
                        archive_url=url,
                        instrument=instrument,
                        ra=ra,
                        dec=dec,
                        raw_target_name=name,
                        reduction_status="reduced",
                    )
                )
    return records


def _fetch_whole_sky(collections) -> list[RawObservation]:
    records = []
    for collection in collections:
        meta = WHOLE_SKY_COLLECTIONS[collection]
        rows = _query(collection, pos=(180, 0), size=WHOLE_SKY_QUERY_SIZE)
        min_rp = meta.get("min_resolving_power")
        for row in rows:
            if str(row["col_19"]) != meta["format"]:
                continue
            if min_rp is not None:
                # col_11 is spec_rp, same positional indexing as _to_observation.
                resolving_power = clean_float(row["col_11"])
                if resolving_power is None or resolving_power < min_rp:
                    continue
            record = _to_observation(row, meta["instrument"])
            if meta.get("name_from_url_dir"):
                record.raw_target_name = record.archive_obs_id.rstrip("/").split("/")[-2].replace("_", " ")
            records.append(record)
    return records


def _whole_sky_done(cursor: dict) -> set[str]:
    if "whole_sky_collections" in cursor:
        return set(cursor["whole_sky_collections"])
    return set(LEGACY_WHOLE_SKY) if cursor.get("whole_sky_done") else set()


_HREF = re.compile(r"""href=["']([^"']+)["']""")
_DATE_OBS = re.compile(rb"DATE-OBS= '(\d{4}-\d{2}-\d{2})")
_TMASS_PREFIX = re.compile(r"^x\s+(\d{3})$")
_TMASS_DESIGNATION = re.compile(r"^\d{8}[+-]\d{7}$")


def _brava_file_url(cell) -> str | None:
    match = _HREF.search(str(cell))
    return IRSA_URL + match.group(1) if match else None


@functools.lru_cache(maxsize=None)
def _brava_obs_date(url: str) -> date | None:
    try:
        resp = _session.get(url, headers={"Range": f"bytes=0-{BRAVA_HEADER_BYTES - 1}"}, timeout=(15, 60))
        resp.raise_for_status()
    except requests.RequestException:
        return None
    match = _DATE_OBS.search(resp.content[:BRAVA_HEADER_BYTES])
    try:
        return date.fromisoformat(match.group(1).decode()) if match else None
    except ValueError:
        return None


def _brava_name(row) -> str | None:
    prefix = _TMASS_PREFIX.match(str(row["fits_spectra_2"]).strip())
    if not prefix:
        return None
    designation = prefix.group(1) + str(row["tmass_id"]).strip()
    return f"2MASS J{designation}" if _TMASS_DESIGNATION.match(designation) else None


def _fetch_brava_page(after_cntr: int) -> tuple[list[RawObservation], int, bool]:
    """One page of brava.bravacat past `after_cntr`; returns (records, last
    cntr seen, whether the table is exhausted)."""
    service = make_tap_service(TAP_URL)
    rows = service.run_sync(
        f"SELECT TOP {BRAVA_PAGE_SIZE} cntr, ra, dec, fits_spectra_1, fits_spectra_2, tmass_id "
        f"FROM brava.bravacat WHERE cntr > {int(after_cntr)} ORDER BY cntr"
    ).to_table()

    records = []
    last_cntr = after_cntr
    for row in rows:
        last_cntr = int(row["cntr"])
        ra, dec = clean_float(row["ra"]), clean_float(row["dec"])
        name = _brava_name(row)
        for n, column in enumerate(("fits_spectra_1", "fits_spectra_2"), start=1):
            url = _brava_file_url(row[column])
            if url is None:
                continue
            records.append(
                RawObservation(
                    archive_obs_id=f"brava:{last_cntr}:{n}",
                    archive_url=url,
                    instrument=BRAVA_INSTRUMENT,
                    obs_date=_brava_obs_date(url),
                    ra=ra,
                    dec=dec,
                    raw_target_name=name,
                    reduction_status="reduced",
                )
            )
    return records, last_cntr, len(rows) < BRAVA_PAGE_SIZE


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    if not cursor.get("tap_done"):
        # Runs once, ahead of everything else -- including for an existing
        # cursor from before the TAP tables were added (whole_sky_done/
        # grid_index already set), which just gains tap_done and carries on.
        new_cursor = dict(cursor)
        new_cursor["tap_done"] = True
        return _fetch_tap_spectra(), new_cursor

    done = _whole_sky_done(cursor)
    pending = [name for name in WHOLE_SKY_COLLECTIONS if name not in done]
    if pending:
        # Only the collections this cursor hasn't pulled yet -- for a cursor
        # from before sofia_flitecam/iso_sws_atlas were added, just those two.
        records = _fetch_whole_sky(pending)
        new_cursor = dict(cursor)
        new_cursor["whole_sky_done"] = True
        new_cursor["whole_sky_collections"] = sorted(WHOLE_SKY_COLLECTIONS)
        new_cursor.setdefault("grid_index", 0)
        return records, new_cursor

    if not cursor.get("brava_done"):
        records, last_cntr, exhausted = _fetch_brava_page(cursor.get("brava_cntr", 0))
        new_cursor = dict(cursor)
        new_cursor["brava_cntr"] = last_cntr
        if exhausted:
            new_cursor["brava_done"] = True
        return records, new_cursor

    grid_index = cursor.get("grid_index", 0)
    if grid_index >= len(GRID_TASKS):
        # Grid fully crawled -- stays a no-op forever after, these are all
        # closed/static historical archives with nothing new to discover.
        return [], cursor

    collection, cell_index = GRID_TASKS[grid_index]
    meta = GRID_COLLECTIONS[collection]
    pos = GRID_CELLS[cell_index]

    rows = _query(collection, pos=pos, size=GRID_CELL_SIZE)
    records = [_to_observation(row, meta["instrument"]) for row in rows if str(row["col_19"]) == meta["format"]]

    new_cursor = dict(cursor)
    new_cursor["grid_index"] = grid_index + 1
    return records, new_cursor
