"""NOVA, the Argentine Virtual Observatory (FCAGLP, La Plata) — SSA.

Found 2026-10-08 in a registry-wide SSA sweep (ivo://ar.nova/...): a GAVO
DaCHS data center at nova.fcaglp.unlp.edu.ar, same software family as
feros_gavo.py/ondrejov.py. Two stellar spectral services, observed:

  - spec   -- "NOVA Spectral Catalog": 642 spectra of 391 targets, mostly
    CASLEO REOSC spectrograph, 1998-2016, optical. Planetary-nebula central
    stars, open-cluster members, bright standards. Already contains the rows
    of the two single-object services registered separately (HD 165052,
    Ferrero+ 2013, 38 rows; WR 35a, Gamen+ 2014, 29 rows -- same
    ssa_reference bibcodes), so those two services are not queried again.
  - MaGOSS -- 313 near-IR spectra of 42 Galactic O stars from Magellan/FIRE
    and Gemini/GNIRS, registered 2025-09.

SSA rather than TAP, unlike the other DaCHS archives here: this server's TAP
answers "More than one entry for table magoss.data in dc.tablemeta" for
MaGOSS (a server-side registration fault, reproduced), while its SSA services
work. DaCHS SSA accepts a query with no POS at all and returns the whole
table, so no cone or hemisphere split is needed (a POS query is capped at a
90-degree radius). The service path is case-sensitive: "MaGOSS/q/ssa", and
"magoss/q/ssa" is a 404.

Every row is image/fits with a direct accref (the archive_obs_id), a name,
and a position in ssa_location (ssa_targetpos is masked on every row).
ssa_dateObs (MJD) is real on spec (635 of 642 rows populated) but is the one
constant 59931.0 on every MaGOSS row, across two different observatories -- a
placeholder, not an observation date, so MaGOSS rows carry no obs_date and
match by name.

spec's ssa_instrument is free text typed per observing run ("Reosc DS  -
Red 080  600 l/mm", "REOSC en Dispersi n simple", "red: #580 - 600 l/mm -
Ang: 6 40'", "rds #180 (316 l/mm)", ...): dozens of spellings of one
spectrograph and its grating setup. _instrument folds those into "REOSC" and
passes anything else through (R-C Spec, Echelle/SITe2K-1, FEROS; 11 rows are
blank).

Small and recently added to, so instead of a one-shot pull both services are
re-pulled once the cursor is older than REFRESH_DAYS, same as fai_kz.py.
"""

from __future__ import annotations

import io

import requests
from astropy.io.votable import parse_single_table
from astropy.time import Time

from sync.base import RawObservation, clean_float

BASE_URL = "http://nova.fcaglp.unlp.edu.ar"

# path: the service path under BASE_URL. dated: whether ssa_dateObs is a real
# observation date there (see module docstring).
SERVICES = [
    {"path": "spec/q/ssa", "dated": True},
    {"path": "MaGOSS/q/ssa", "dated": False},
]

MAXREC = 100000
REFRESH_DAYS = 30

_session = requests.Session()


def _fetch_service_rows(path: str):
    resp = _session.get(
        f"{BASE_URL}/{path}/ssap.xml",
        params={"REQUEST": "queryData", "MAXREC": MAXREC},
        timeout=(15, 120),
    )
    resp.raise_for_status()
    return parse_single_table(io.BytesIO(resp.content)).to_table()


def _instrument(raw: str) -> str | None:
    text = " ".join(raw.split())
    lowered = text.lower()
    # "rds" = REOSC in "dispersion simple" mode; a bare "red: #580 ..." is
    # just that spectrograph's grating ("red") setting.
    if "reosc" in lowered or "dispersi" in lowered or lowered.startswith(("red", "rds")):
        return "REOSC"
    if "boller" in lowered:
        return "Boller & Chivens"
    return text or None


def _is_fresh(cursor: dict) -> bool:
    synced_at = cursor.get("synced_at")
    if not synced_at:
        return False
    return (Time.now() - Time(synced_at)).jd < REFRESH_DAYS


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    if _is_fresh(cursor):
        return [], cursor

    records = []
    for service in SERVICES:
        for row in _fetch_service_rows(service["path"]):
            if str(row["mime"]) != "image/fits":
                continue
            name = str(row["ssa_targname"]).replace("_", " ").strip()
            if not name:
                continue

            obs_date = None
            if service["dated"]:
                mjd = clean_float(row["ssa_dateObs"])
                if mjd is not None:
                    obs_date = Time(mjd, format="mjd").to_datetime().date()

            accref = str(row["accref"])
            records.append(
                RawObservation(
                    archive_obs_id=accref,
                    archive_url=accref,
                    instrument=_instrument(str(row["ssa_instrument"])),
                    obs_date=obs_date,
                    ra=clean_float(row["ssa_location"][0]),
                    dec=clean_float(row["ssa_location"][1]),
                    raw_target_name=name,
                    reduction_status="reduced",
                )
            )

    new_cursor = {"synced_at": Time.now().isot, "row_count": len(records)}
    return records, new_cursor
