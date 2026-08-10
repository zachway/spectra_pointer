"""ESO raw archive — dbo.raw table, same TAP endpoint as eso.py's ivoa.ObsCore
but a genuinely different table: unreduced exposures straight off the
telescope, not the Phase 3 (science-portal) reduced products eso.py covers.
Confirmed live to be a real, disjoint gap: ~30k HARPS + ~9k UVES + ~1.6k
ESPRESSO raw frames alone around alpha Cen, none of which show up via
ivoa.ObsCore. Split into its own archive_code (same reasoning as
gemini_ghost/gemini_igrins/mast_jwst/dao being split from their siblings)
because the query shape, columns, and pagination watermark are unrelated to
eso.py's.

dbo.raw has no dataproduct_type/calib_level columns to isolate spectra the
way ivoa.ObsCore does -- dp_tech is the only signal, and it spans imaging,
interferometry, polarimetry, and coronagraphy alongside real spectroscopy.
Filtered to dp_tech LIKE 'ECHELLE%' OR 'SPECTRUM%' (excludes IMAGE/MOS/MXU/
IFU/INTERFEROMETRY/POLARIMETRY/CORONOGRAPHY -- IFU/MOS/MXU produce cubes or
multi-slit data, not the single-object 1D spectra this project otherwise
tracks, consistent with eso.py's own dataproduct_type='spectrum' filter).
Two instrument codes excluded despite matching that filter: GRIPS19
(591,639 rows, confirmed live every row has object='SKY MAP' -- an all-sky
background monitor, not target spectra) and APEXHET (211,442 rows, a submm
heterodyne receiver on the APEX 12m radio dish -- wrong wavelength regime
entirely, already known out of scope, see webapp/app.py's
INSTRUMENT_WAVELENGTH_RANGE_NM comment).

Old decommissioned instruments (CES, EMMI, EFOSC) report their grating/
filter setting packed into the instrument column itself (e.g. "CES/3.9",
"EMMI/2.15") -- stripped to the base instrument name, otherwise a handful
of real spectrographs fragment into dozens of near-empty "instruments".

target (not object) is used for raw_target_name -- object is frequently
overwritten by the observatory for CALIB-adjacent exposures even within
dp_cat='SCIENCE' (confirmed live: blank for a meaningful fraction of SOFI/
EFOSC/TIMMI2 rows), while target is "as given by the astronomer" per ESO's
own column description. Confirmed live this resolves the alpha-Cen-A-vs-B
problem plain position can't: raw HARPS frames report target=HD128621 (B)
distinctly from HD128620 (A), so the identifier-before-position matcher
path separates them correctly even though the two stars are too close
together, and moving too fast, for position alone to tell apart.

reduction_status is hardcoded 'raw' -- dbo.raw is unreduced exposures by
construction, no calib_level column needed (same shape as noirlab.py's own
hardcoded 'raw' for its raw-only query).

ra/dec are not masked when missing (unlike ivoa.ObsCore elsewhere) -- ESO
instead stamps a fixed sentinel, (-596.52323555, -596.52323555), confirmed
live to account for 799,630 of 799,637 physically-invalid rows (dec outside
+/-90) in this query, spanning real named targets (e.g. HD-216803, mostly
old CES-era rows) that just never got a real position recorded. Nulled out
below (along with the remaining 7 stray out-of-physical-range rows, via a
general +/-90 dec / 0-360 ra bound rather than just the exact sentinel) --
the matcher falls back to its identifier-first name match in that case,
same graceful path as any other archive with no position data at all
(feros_gavo, sophie, salt_hrs, ...).
"""

from astropy.time import Time

from sync.base import RawObservation, clean_float, make_tap_service

TAP_URL = "http://archive.eso.org/tap_obs"

# Confirmed live: real target spectra, not calibration/monitoring noise.
EXCLUDED_INSTRUMENTS = ("GRIPS19", "APEXHET")

QUERY = """
SELECT TOP {page_size} dp_id, ra, dec, mjd_obs, instrument, target, prog_id
FROM dbo.raw
WHERE dp_cat = 'SCIENCE'
AND (dp_tech LIKE 'ECHELLE%' OR dp_tech LIKE 'SPECTRUM%')
AND instrument NOT IN ({excluded})
AND mjd_obs > {last_mjd}
ORDER BY mjd_obs ASC
"""

PAGE_SIZE = 50000

DATASET_LANDING_PAGE = "https://archive.eso.org/dataset/{dp_id}"


def _clean_coords(ra, dec) -> tuple[float | None, float | None]:
    ra, dec = clean_float(ra), clean_float(dec)
    if ra is None or dec is None or not (0 <= ra <= 360) or not (-90 <= dec <= 90):
        return None, None
    return ra, dec


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    last_mjd = cursor.get("last_mjd", 0)

    tap = make_tap_service(TAP_URL)
    excluded = ", ".join(f"'{name}'" for name in EXCLUDED_INSTRUMENTS)
    query = QUERY.format(page_size=PAGE_SIZE, excluded=excluded, last_mjd=last_mjd)
    # pyvo defaults maxrec to ~20000 regardless of the ADQL TOP clause --
    # confirmed live via eso.py's own ivoa.ObsCore query, same TAP service.
    table = tap.search(query, maxrec=PAGE_SIZE).to_table()

    records = []
    max_mjd = last_mjd
    for row in table:
        mjd_obs = float(row["mjd_obs"])
        max_mjd = max(max_mjd, mjd_obs)
        dp_id = str(row["dp_id"])
        instrument = str(row["instrument"]).split("/")[0]
        ra, dec = _clean_coords(row["ra"], row["dec"])
        records.append(
            RawObservation(
                archive_obs_id=dp_id,
                archive_url=DATASET_LANDING_PAGE.format(dp_id=dp_id),
                instrument=instrument,
                obs_date=Time(mjd_obs, format="mjd").to_datetime().date(),
                program_id=str(row["prog_id"]),
                ra=ra,
                dec=dec,
                raw_target_name=str(row["target"]),
                reduction_status="raw",
            )
        )

    new_cursor = {"last_mjd": max_mjd if records else last_mjd}
    return records, new_cursor
