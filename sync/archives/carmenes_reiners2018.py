"""CARMENES Reiners et al. 2018 input catalog spectra — static HTML table on
the same GTO portal as carmenes.py's DR1 and carmenes_tac.py's telluric-
corrected templates, a third, genuinely different data product.

Found on welcome.action (jsp/reinersetal2018.jsp, "The CARMENES search for
exoplanets around M dwarfs -- High-resolution optical and near-infrared
spectroscopy of 324 survey stars", Reiners et al. 2018 A&A) while auditing
whether every link on that portal was accounted for after carmenes_tac.py
shipped -- it wasn't; this page and four single-star-zip paper pages
(moralesetal2019/nortmannetal2018/trifonofetal2021, each one star) plus two
non-spectral ones (orellmiqueletal2022 = a planet transmission spectrum
.dat file, reinersetal2022 = MCMC posteriors) were still unexamined. Only
this one is a real additive archive at meaningful scale.

324 rows confirmed live (matches the paper's own "324 survey stars"), one
row per star -- unlike DR1's multi-epoch-per-star zip, this page picks one
representative epoch per star and exposes it as two direct, unauthenticated
GET links (getDataPublic.action?id=car-<timestamp>-sci-gtoc-{vis,nir}_A.fits),
no zip, no session. A real VIS sample (J00051+457) came back 5,051,520 bytes
with SPEC/CONT/SIG/WAVE image extensions (4096 x 61, one row per echelle
order); the matching NIR sample came back 2,341,440 bytes, same 4-extension
shape (4080 x 28) -- same SPEC/SIG/WAVE naming as DR1's per-epoch SERVAL
files and carmenes_tac's co-added templates, so reduction_status is hardcoded
'reduced' with the same confidence as those two. Unlike carmenes_tac.py,
CONT (blaze-normalized continuum) is also present but not used here -- SPEC/
SIG/WAVE alone already cover flux+uncertainty+wavelength.

Unlike DR1/TAC, this page's own table carries no SIMBAD-resolvable
discovery name, only the Karmn id -- but 323 of its 324 Karmn ids (all but
J11110+304, confirmed live) already appear in DR1's own table, so this
reuses carmenes._parse_dr1_table() purely for its Karmn -> discovery-name
mapping (ignoring DR1's own zip filename) rather than re-deriving a
Karmn -> position parse from scratch. The one unmatched star is dropped,
same "harmless skip" tolerance as DR1's own unresolved counter.

Two holdings per star (VIS, NIR), same shape as carmenes_tac.py. No per-
observation date is stored -- RawObservation.obs_date isn't populated here
even though the page does show a real date per epoch, since matching cares
about star identity, not epoch, at this archive's current one-row-per-star
granularity; a future pass wanting true per-epoch tracking would need to
walk the (currently discarded) date/exptime/SNR columns too. Static, closed
dataset (paper already published) -- one-shot pull via a synced_at cursor,
same pattern as carmenes.py/carmenes_tac.py.

Gaia resolution reuses ingest.add_star.resolve_stellar_gaia_ids_batch, same
as carmenes_tac.py.
"""

from urllib.parse import urljoin

import requests
from astropy.time import Time
from bs4 import BeautifulSoup

from ingest.add_star import resolve_stellar_gaia_ids_batch
from sync.archives.carmenes import _parse_dr1_table
from sync.base import RawObservation

REINERS2018_URL = "http://carmenes.cab.inta-csic.es/gto/jsp/reinersetal2018.jsp"


def _karmn_to_name() -> dict[str, str]:
    return {karmn: name for karmn, name, _ in _parse_dr1_table()}


def _parse_reiners2018_table() -> list[tuple[str, str, str]]:
    resp = requests.get(REINERS2018_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 9:
            continue
        karmn = tds[0].get_text(strip=True)
        vis_a = tds[2].find("a")
        nir_a = tds[6].find("a")
        if not karmn or vis_a is None or nir_a is None:
            continue
        rows.append((karmn, urljoin(REINERS2018_URL, vis_a["href"]), urljoin(REINERS2018_URL, nir_a["href"])))
    return rows


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    if cursor.get("synced_at"):
        return [], cursor

    rows = _parse_reiners2018_table()
    karmn_to_name = _karmn_to_name()

    names = [karmn_to_name[karmn] for karmn, _, _ in rows if karmn in karmn_to_name]
    gaia_by_name = resolve_stellar_gaia_ids_batch(names)

    records = []
    for karmn, vis_url, nir_url in rows:
        name = karmn_to_name.get(karmn)
        gaia_source_id = gaia_by_name.get(name) if name else None
        if gaia_source_id is None:
            continue
        for suffix, url, instrument in (
            ("VIS", vis_url, "CARMENES VIS"),
            ("NIR", nir_url, "CARMENES NIR"),
        ):
            records.append(
                RawObservation(
                    archive_obs_id=f"{karmn}_{suffix}",
                    archive_url=url,
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
