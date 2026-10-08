"""AAT Archive (Data Central) -- 2dF+AAOmega, one record per fibre.

See sync/archives/_aat_common.py for the service itself. A 2dF exposure
feeds up to ~400 fibres to the AAOmega spectrograph at once, so the frame's
own OBJECT/ra/dec only describe the field; the targets are in the row's
`fibre_table` (program fibres only -- sky, guide and parked fibres are
already left out by the archive). Each fibre of each science exposure
becomes one record at the fibre's own J2000 position, with archive_obs_id
"<aat_id>:<fibre index>". The blue and red arms of one exposure are merged
(see _aat_common.exposures), so that is one record per fibre per exposure.

A separate archive_code from aat.py because these targets run several
magnitudes fainter than the single-object spectrographs' -- one frame's own
FIBRES extension listed G~18.6-19.6 -- and sync.positional_fallback's
faintness ceiling is per archive_code.

Fibre names are whatever the observing team put in their configuration
file, sampled across 2006-2023:

- Mostly program-internal ("star1234", "G12345", "Fld_SF4_tar_0257",
  "QSO_F1_2"). Not passed on as raw_target_name: SIMBAD can't resolve them,
  and at ~10,000 fibres a night they would bury its batch resolver.
- Some programs use the Gaia source_id itself (2021 stellar-stream fields:
  400 of 400 sampled were real DR3 sources 0.01" from the fibre). Taken as
  gaia_source_id, but only when the ID's own sky position agrees with the
  fibre's (see _aat_common.gaia_source_id_from_name), since other surveys'
  object IDs are long integers too.
- Some use a catalogue designation ("2MASS00442854-7241302"). Passed on as
  raw_target_name in SIMBAD's spelling.

Everything else goes through positional matching like any nameless record.

Science exposures with an empty fibre_table are skipped (observed: 2014
"galaxy_plate" frames taken through AAOmega with a different front end).
A record at the field centre would match whatever star happens to sit
there, which was never a target.

reduction_status is 'raw': the archive only holds unreduced frames.
"""

from __future__ import annotations

import re
from datetime import date

from sync.archives import _aat_common
from sync.base import RawObservation

INSTRUMENT = "AAOmega (2dF)"

# First AAOmega science frames are 2005-12-18. "MFOBJECT" only filters when
# paired with this exact dropdown name -- see _aat_common's module docstring.
QUERIES: list[_aat_common.Query] = [("2005-12-01", None, {"instrument": "2dF+AAOMEGA", "obstype": "MFOBJECT"})]

_TWOMASS_NAME = re.compile(r"^2MASS[\s_-]*J?(\d{8}[+-]\d{7})$", re.IGNORECASE)
_CATALOGUE_NAME = re.compile(r"^(HD|HIP|HR|TYC|GJ|BD|CD|CPD)[\s_-]*[+-]?\d", re.IGNORECASE)


def _catalogue_name(name: str) -> str | None:
    twomass = _TWOMASS_NAME.match(name)
    if twomass:
        return f"2MASS J{twomass.group(1)}"
    if _CATALOGUE_NAME.match(name):
        return name.replace("_", " ")
    return None


def to_records(rows: list[dict], url: str, night: date) -> list[RawObservation]:
    records = []
    for row in _aat_common.exposures(rows):
        obs_date = _aat_common.row_obs_date(row, night)
        program_id = (row.get("ProgramID") or "").strip() or None
        for fibre in row.get("fibre_table") or []:
            ra, dec = _aat_common.clean_position(fibre.get("ra"), fibre.get("dec"))
            if ra is None or fibre.get("index") is None:
                continue
            name = str(fibre.get("name") or "").strip()
            records.append(
                RawObservation(
                    archive_obs_id=f"{row['aat_id']}:{fibre['index']}",
                    archive_url=url,
                    instrument=INSTRUMENT,
                    obs_date=obs_date,
                    program_id=program_id,
                    gaia_source_id=_aat_common.gaia_source_id_from_name(name, ra, dec),
                    ra=ra,
                    dec=dec,
                    raw_target_name=_catalogue_name(name),
                    reduction_status="raw",
                )
            )
    return records


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    return _aat_common.fetch(cursor, QUERIES, to_records)
