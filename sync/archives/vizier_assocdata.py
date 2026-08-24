"""VizieR Associated Data (CDS) -- ObsTAP, spectra pulled from journal
supplementary tables.

Found via https://cdsarc.cds.unistra.fr/assocdata/ -- a real, live ObsTAP/
ADQL service (Saada engine), not just the browse page it advertises. Real
endpoint: https://cdsarc.cds.unistra.fr/saadavizier.tap/tap (table
`obscore`, standard ObsCore columns). It aggregates images/spectra/
timeseries/SEDs that individual journal articles published as VizieR
electronic tables -- one `obs_collection` per publication (284 total carry
dataproduct_type='spectrum', 8.3M rows, observed 2026-08-24).

Ranking rule for this project: always prefer a direct link to the
originating archive; only fall back to the VizieR association when no
direct archive exists. Most of the big buckets here ARE duplicates of
archives already synced directly -- LAMOST (7.67M rows, V/153) via
lamost.py/lamost_mrs.py, HST (281k) via mast.py, AAT's J/MNRAS/413/971
(43k) is literally RAVE (rave.py), plus ESO-VLT-*/VLT/NTT (eso.py),
Keck (koa.py), Gemini (gemini*.py), TNG (harpsn_tng.py), Mercator
(hermes_mercator.py), CFHT (cfht_cadc.py), OHP (elodie.py/sophie.py),
SALT (salt_hrs.py), GTC (gtc.py), SDSS (sdss_*.py), Isaac Newton/Nordic
Optical/William Herschel Telescopes (ing.py/not_fies.py), Subaru/IRTF
(subaru_moircs.py/irtf_*.py), Lick (lick.py), CTIO/KPNO/SOAR/Blanco/Bok
(noirlab.py), Spitzer (irsa_missions.py), Ritter (ritter_prest.py), LBT
(lbt.py), Asiago/Mt. Ekar (asiago.py), FUSE (mast.py), JWST (mast_jwst.py),
XMM (xmm.py). EXCLUDED_FACILITY_NAMES below is the exact, hand-verified
(2026-08-24) list of `facility_name` literals covering all of those --
built by dumping every (facility_name, obs_collection, row count) combo for
dataproduct_type='spectrum' and checking each top bucket against this
project's own archive list, not by trusting the label alone. COROT's
177,553-row bucket (all facility_name='COROT', obs_collection='B/corot')
looked promising by count alone but turned out to be mislabeled CoRoT
photometric time-series flux, not real 1D spectra -- most rows (the
ASTERO/EXO instrument channels) have no target_name at all, but a
minority (the COROT_faint_star channel) DO carry a populated target_name
that's just a COROT internal run id (e.g. "COROT105288043"), not a
SIMBAD-resolvable star name -- confirmed live only after the first live
fetch() run surfaced one, so target_name-non-null alone was NOT a
sufficient filter here; COROT is excluded by facility_name explicitly
instead. Same issue found for facility_name='Model' (VII/102, 96 rows):
target_name is a spectral-type label ("F56V", "G04V", ...), i.e. synthetic
template spectra keyed by spectral type, not real observations of real
stars -- excluded for the same reason, not for archive overlap. ADQL here
has no LOWER()/LIKE wildcard support (observed: both error out), so
exclusion is exact literal string match, not substring -- re-run the
facility survey (see project memory project_vizier_assocdata_found.md)
before assuming a new top bucket is already covered or genuinely real.

Also restricted to em_min/em_max within [1e-7, 5e-6] meters (100nm-5000nm,
UV/optical/NIR) -- this project has no radio or far-IR archive at all, and
the long tail here is otherwise dominated by radio/mm facilities (IRAM,
JCMT, ALMA, VLA, Effelsberg, ATCA, MeerKAT, VLBA, NOEMA, Arecibo, ...)
observing molecular/atomic lines, not stellar optical/IR spectroscopy this
project tracks elsewhere. Confirmed live: with all of the above applied,
13,199 rows remain (2026-08-24) -- small single-paper collections from
telescopes/PIs with no ongoing public archive of their own (e.g. STELLA2's
Betelgeuse spectra, OAO's 188cm reflector, Jacobus Kaptein Telescope),
exactly the kind of PI-only data written off as unreachable for Palomar/
APO (see project_palomar_dead_end.md/project_apo_arc35m_dead_end.md) --
reachable here only because the journal-supplement copy is public on CDS,
not because the originating archive itself is public.

Rows with a NULL facility_name (2,722 observed) are conservatively dropped
by the NOT IN filter's three-valued-logic (NULL NOT IN (...) is NULL, which
WHERE treats as false) -- unclassifiable, safer to leave out than risk an
unverified duplicate.

calib_level is -1 (unset sentinel) on every one of the 189,958 remaining
rows (observed) -- not a real 0-3 ObsCore value, so reduction_status is
left at the 'unknown' default rather than fed through
reduction_status_from_calib_level (which would otherwise misread -1 as
"raw").

oidsaada is a real per-row unique identifier (confirmed unique, ~19-digit)
-- used both as archive_obs_id and as the pagination watermark, same
id-watermark shape as hermes_mercator.py/asiago.py, since VizieR keeps
ingesting new journal tables over time (not a frozen historical dump like
feros_gavo.py/elodie.py). access_url is a direct, already-resolved download
link (https://cdsarc.cds.unistra.fr/saadavizier/download?oid=...), no
further resolution step needed. bib_reference (the paper's bibcode) is
stored in program_id -- not a literal observing-program ID, but the closest
available field for retaining per-record provenance back to the publication.

The server's own `oidsaada > '{last_id}'` filter is unreliable at this ID's
~19-digit scale -- confirmed live 2026-08-24 (first prod run): querying
with last_id equal to the table's true maximum oidsaada got that exact same
row back every single call, cursor never advancing, an infinite loop that
had to be killed manually after 39 pages. Consistent with the comparison
being coerced through float64 somewhere server-side (exact only to ~15-16
significant digits, well under 19) so the boundary row satisfies its own
'>' filter. Fixed by re-checking every row against last_id client-side with
Python's exact arbitrary-precision int comparison and dropping anything
that doesn't genuinely satisfy it -- the server-side filter is kept too (it
still does most of the real filtering work, this is just a safety net), but
no boundary self-match can turn into an infinite loop again regardless of
why the server's own comparison misbehaves.

TOP 20000 took ~23s in testing (mostly fixed table-scan overhead from the
NOT IN literal list against the full 8.3M-row table, not per-row cost) --
at 13,199 total rows the whole catch-up fits in a single page/request
(~10-25s), PAGE_SIZE is generous headroom for future growth rather than a
real pagination need today.
"""

from __future__ import annotations

from astropy.time import Time

from sync.base import RawObservation, clean_float, make_tap_service

TAP_URL = "https://cdsarc.cds.unistra.fr/saadavizier.tap/tap"

PAGE_SIZE = 20000

# Hand-verified 2026-08-24 -- see module docstring. Exact facility_name
# literals only (ADQL here has no LOWER()/LIKE support to do this fuzzily).
EXCLUDED_FACILITY_NAMES = [
    "182 CM EKAR",
    "AAOmega-2dF",
    "AAT",
    "ANGLO-AUSTRALIAN TELESCOPE",
    "ASIAGO 120 cm Telescope",
    "ASIAGO T120",
    "Anglo-Australian Telescope",
    "Blanco",
    "CFHT",
    "CFHT 3.6m",
    "COROT",
    "CTIO 4.0 meter telescope",
    "ESO 1.52",
    "ESO-3P6",
    "ESO-LASILLA",
    "ESO-NTT",
    "ESO-VLT",
    "ESO-VLT-U1",
    "ESO-VLT-U2",
    "ESO-VLT-U3",
    "ESO-VLT-U4",
    "ESO/VLT",
    "ESONTTB",
    "ESOs 3.6m telescope",
    "FOS",
    "FUSE",
    "GEMINI-North",
    "GHRS",
    "GTC",
    "Gemini",
    "Gemini North",
    "Gemini-North",
    "Gemini-South",
    "HST",
    "HST/WFC3",
    "INT",
    "IRTF",
    "Isaac Newton Telescope",
    "JWST",
    "KPNO-IRAF",
    "Keck",
    "Keck I",
    "Keck II",
    "LAMOST",
    "LBT",
    "LBT-SX",
    "La Silla 3.6m",
    "Lick 3M",
    "Lick 3m",
    "Mercator",
    "Model",
    "Mt. Ekar 182 cm. Telescope",
    "NASA IRTF",
    "NOT",
    "NTT",
    "NTT La SILLA",
    "Nordic Optical Telescope",
    "OHP",
    "Ritter Observatory",
    "SALT",
    "SDSS 2.5-M",
    "SOAR",
    "SOAR 4.1M",
    "SOAR 4.1m",
    "STIS",
    "SUBARU",
    "Spitzer",
    "Subaru",
    "TNG",
    "VLT",
    "VLTI",
    "WHT",
    "WIYN3.5m",
    "William Herschel Telescope",
    "XMM-Newton",
    "bok",
    "kp4m",
    "wiyn",
]

_EXCLUDED_LITERAL = ", ".join("'" + name.replace("'", "''") + "'" for name in EXCLUDED_FACILITY_NAMES)

QUERY = f"""
SELECT TOP {{page_size}} oidsaada, target_name, s_ra, s_dec, t_min,
       instrument_name, bib_reference, access_url
FROM obscore
WHERE dataproduct_type = 'spectrum'
  AND target_name IS NOT NULL AND target_name != ''
  AND em_min IS NOT NULL AND em_max IS NOT NULL
  AND em_min >= 1e-7 AND em_max <= 5e-6
  AND facility_name NOT IN ({_EXCLUDED_LITERAL})
  AND oidsaada > '{{last_id}}'
ORDER BY oidsaada ASC
"""


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    last_id = cursor.get("last_id", "0")
    last_id_int = int(last_id)

    tap = make_tap_service(TAP_URL)
    query = QUERY.format(page_size=PAGE_SIZE, last_id=last_id)
    table = tap.search(query, maxrec=PAGE_SIZE).to_table()

    records = []
    max_id_int = last_id_int
    for row in table:
        oid = str(row["oidsaada"]).strip()
        oid_int = int(oid)

        # The server's own `oidsaada > '{last_id}'` filter is unreliable at
        # this ID's ~19-digit scale (observed live 2026-08-24: querying with
        # last_id equal to the table's true maximum oidsaada gets that same
        # row back forever, cursor never advancing -- consistent with the
        # comparison being coerced through float64 somewhere server-side,
        # which is only exact to ~15-16 significant digits, well under 19).
        # Re-checking with Python's exact arbitrary-precision int comparison
        # drops any row the server incorrectly included, so a boundary
        # self-match can never turn into an infinite loop regardless of why
        # the server's filter misbehaves.
        if oid_int <= last_id_int:
            continue
        if oid_int > max_id_int:
            max_id_int = oid_int

        t_min = clean_float(row["t_min"])
        obs_date = Time(t_min, format="mjd").to_datetime().date() if t_min is not None else None

        instrument = str(row["instrument_name"]).strip() or None
        bib_reference = str(row["bib_reference"]).strip() or None

        records.append(
            RawObservation(
                archive_obs_id=oid,
                archive_url=str(row["access_url"]),
                instrument=instrument,
                obs_date=obs_date,
                program_id=bib_reference,
                ra=clean_float(row["s_ra"]),
                dec=clean_float(row["s_dec"]),
                raw_target_name=str(row["target_name"]).strip() or None,
            )
        )

    new_cursor = {"last_id": str(max_id_int)}
    return records, new_cursor
