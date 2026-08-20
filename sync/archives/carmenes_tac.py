"""CARMENES telluric-corrected template library (TAC) — static HTML table on
the same GTO portal as carmenes.py's DR1, but a genuinely different, additive
data product.

Found on the portal's own landing page (carmenes.cab.inta-csic.es/gto/
welcome.action, linked as "Telluric absorption corrected high S/N optical
and near-infrared template spectra of 382 M dwarf stars", Nagel, Czesla,
Kaminski et al. 2023 A&A in press) while investigating whether DR1's
whole-star-zip-only access (see the Spectral Access Ledger's Tier C note on
`carmenes`) could be improved — it can't, directly, but this sibling dataset
solves the same underlying problem with a real per-star, per-channel direct
file, no zip needed.

382 rows confirmed live at jsp/tellurics_tac.jsp (matches the paper's own
"382 M dwarf stars"), each carrying a Karmn (Carmencita) id, a SIMBAD
discovery name, and two direct getTacDataPublic.action?id=... FITS links —
one VIS (~5.3MB), one NIR (~2.7MB) — fetched live with a plain unauthenticated
GET, no session/cookie needed (unlike gemini_ghost/gemini_igrins's GOA
gate). A real VIS sample (J00051+457) came back exactly 5,466,240 bytes with
SPEC/SIG/WAVE image extensions (3699 x 61, one row per echelle order); a real
NIR sample (J00051+457) came back 2,759,040 bytes, same 3-extension shape
(1999 x 56). The archive's own readme (getTacDataPublic.action?id=
carmenes.taclibrary.readme.txt, fetched live) documents the columns
explicitly: SPEC = template flux at B-spline knots, SIG = uncertainty
estimate for those flux values, WAVE = natural log of vacuum wavelength
(needs np.exp, not a plain Angstrom/nm value) — same SPEC/SIG/WAVE naming as
DR1's own per-epoch SERVAL files, but here each star gets one direct,
already-small file per channel instead of a multi-epoch zip.

reduction_status hardcoded 'reduced' -- the readme is explicit that these are
"template spectra... constructed by co-adding all telluric corrected VIS
[or NIR] spectra of each star with serval", strictly more processed than a
single-epoch reduced spectrum, never a raw frame.

Two holdings per star (VIS, NIR) rather than one, since they're genuinely
separate instrument channels/files, not two views of the same data (compare
oirsa.py's one-archive-many-instruments shape). No per-observation date is
available -- this is a co-add across every epoch used, same tradeoff as
carmenes.py's own whole-star DR1 zip and sdss_v_apogee.py's apStar files.
Static, closed dataset (GTO ended, paper already in press) -- one-shot pull
via a synced_at cursor, same pattern as carmenes.py/rave.py.

Gaia resolution reuses ingest.add_star.resolve_stellar_gaia_ids_batch
(SIMBAD discovery-name match, batched) rather than carmenes.py's own inline
version of the same query -- the shared helper additionally checks SIMBAD's
otype so a non-stellar match can't slip through, and is the path this
project's own docstrings already point new archives at.
"""

from urllib.parse import quote, unquote

import requests
from astropy.time import Time
from bs4 import BeautifulSoup

from ingest.add_star import resolve_stellar_gaia_ids_batch
from sync.base import RawObservation

TAC_URL = "http://carmenes.cab.inta-csic.es/gto/jsp/tellurics_tac.jsp"
FILE_URL = "http://carmenes.cab.inta-csic.es/gto/getTacDataPublic.action?id={filename}"


def _extract_filename(href: str) -> str:
    # Same "id=<filename>" href shape as carmenes.py's zip links, but this
    # portal serves the query value inconsistently encoded across rows (a
    # literal "+" in some hrefs, "%2B" in others, confirmed live on the same
    # star) -- unquote first to recover the true raw filename regardless of
    # which form the page used, then re-quote once, consistently, below.
    _, _, raw = href.partition("id=")
    return unquote(raw.strip())


def _parse_tac_table() -> list[tuple[str, str, str, str]]:
    resp = requests.get(TAC_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        karmn = tds[0].get_text(strip=True)
        name = tds[1].get_text(strip=True)
        vis_a = tds[2].find("a")
        nir_a = tds[3].find("a")
        if not karmn or not name or vis_a is None or nir_a is None:
            continue
        rows.append((karmn, name, _extract_filename(vis_a["href"]), _extract_filename(nir_a["href"])))
    return rows


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    if cursor.get("synced_at"):
        return [], cursor

    rows = _parse_tac_table()
    gaia_by_name = resolve_stellar_gaia_ids_batch([name for _, name, _, _ in rows])

    records = []
    for karmn, name, vis_filename, nir_filename in rows:
        gaia_source_id = gaia_by_name.get(name)
        if gaia_source_id is None:
            continue
        for suffix, filename, instrument in (
            ("VIS", vis_filename, "CARMENES VIS"),
            ("NIR", nir_filename, "CARMENES NIR"),
        ):
            records.append(
                RawObservation(
                    archive_obs_id=f"{karmn}_{suffix}",
                    archive_url=FILE_URL.format(filename=quote(filename, safe="")),
                    instrument=instrument,
                    gaia_source_id=gaia_source_id,
                    raw_target_name=name,
                    reduction_status="reduced",
                )
            )

    new_cursor = {
        "synced_at": Time.now().isot,
        "row_count": len(records),
        "unresolved": len(rows) - len({r.raw_target_name for r in records}),
    }
    return records, new_cursor
