"""AAT Archive (Data Central) -- single-object spectrographs, raw frames.

See sync/archives/_aat_common.py for the service itself. This module keeps
the exposures from spectrographs that observe one target at a time, where
the frame's OBJECT is the target's name: one record per exposure, name
first, the frame's J2000 pointing as the positional backup.

Finding the science exposures takes three queries, because the archive's
`obstype` filter reads the RUNCMD header and that header changed meaning
over the telescope's life (all observed 2026-10-08):

- 1990 to early 1994: no RUNCMD header at all, so `obstype="RUN"` returns
  nothing before 1994-03-23. Those nights are queried unfiltered.
- 1994 on: `obstype="RUN"`, no instrument filter. Through the 1990s nearly
  every frame is a "RUN" -- arcs and flats included, told apart only by
  OBJECT ("ARC FOR HD22484", "FLAT_TUNG_R") -- so this is a superset of the
  science frames, not a science filter.
- Veloce (2018-08-15 on) tags science frames RUNCMD="OBJECT" instead, which
  neither `obstype="RUN"` nor `obstype="MFOBJECT"` returns (0 of 52 on a
  night that has them), so it gets its own instrument-filtered query.

_is_science then drops what is recognisably a calibration: a RUNCMD other
than RUN/OBJECT, a zero exposure time, or an OBJECT that names a lamp, flat,
bias, dark or sky frame. That is a word list, not a classifier -- anything
it misses is left in, same as ing.py and lick.py leave their free-text
labels: it simply fails to resolve to a star downstream.

INSTRUMENTS is an allowlist keyed on the row's own lower-cased `instrument`
value. 'ucles', 'cycles2', 'uhrf', 'rgo25', 'rgo82', 'wprgo25', 'fors',
'iris2s' and 'Veloce' were observed on real rows; the other keys are taken
from the archive's own instrument dropdown and assumed to match the same
way. Left out on purpose: imagers and polarimeters (iris1i/iris2i, wfi, cfi,
tfp/ttf, sempol, hatpol, ...), integral-field and multi-object instruments
whose OBJECT is a field rather than a star (spiral, KOALA, Hector/Spector,
ldss, argo*, tdf -- AAOmega is aat_2df.py, HERMES is galah.py), and dropdown
codes not identified yet (di, f1, cps, fucles, frgo*, manech, ...). Every
skipped instrument is logged with its frame count so this list can be
extended from real sync output.

Veloce's three arms (2020: one CCD; 2024: three) are merged into one record
per exposure by _aat_common.exposures. Its OBJECT is often a bare number
("10700" for HD 10700 -- sync.matcher already reads a digits-only name as
an HD number) and sometimes a bare Gaia source_id, taken as gaia_source_id
when it agrees with the pointing (see _aat_common.gaia_source_id_from_name).

reduction_status is 'raw': the archive only holds unreduced frames.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from sync.archives import _aat_common
from sync.base import RawObservation

ARCHIVE_CODE = "aat"

# Earliest frames in the archive are 1990 (1974-1989 all return nothing).
# The unfiltered and obstype="RUN" ranges overlap by a few weeks around the
# first RUNCMD header rather than trusting one exact changeover night.
QUERIES: list[_aat_common.Query] = [
    ("1990-01-01", "1994-05-01", {}),
    ("1994-03-01", None, {"obstype": "RUN"}),
    ("2018-08-01", None, {"instrument": "Veloce"}),
]

INSTRUMENTS = {
    "ucles": "UCLES",
    "cycles": "UCLES (CYCLOPS)",
    "cycles2": "UCLES (CYCLOPS2)",
    "uhrf": "UHRF",
    "veloce": "Veloce",
    "rgo25": "RGO Spectrograph (25cm camera)",
    "rgo82": "RGO Spectrograph (82cm camera)",
    "wprgo25": "RGO Spectrograph (25cm camera, spectropolarimetry)",
    "wprgo82": "RGO Spectrograph (82cm camera, spectropolarimetry)",
    "fors": "FORS",
    "figs": "FIGS",
    "iris1s": "IRIS (spectroscopy)",
    "iris2s": "IRIS2 (spectroscopy)",
}

_SCIENCE_RUNCMDS = {None, "RUN", "OBJECT"}

# Whole words, so "ARC FOR HD22484" and "CENX_3 ARC" go but a star whose
# name merely contains the letters doesn't.
_CALIBRATION_WORD = re.compile(
    r"(?:^|[\s_\-])(arc|flat|bias|dark|sky|twilight|dome|lamp|focus|test|acquire)(?:$|[\s_\-\d])", re.IGNORECASE
)
# Lamp names, which turn up glued to other text ("FibThAr", "SimThLong").
_CALIBRATION_SUBSTRING = re.compile(r"thar|cuar|fear|tung|quartz|simth", re.IGNORECASE)


def _is_science(row: dict) -> bool:
    if (row.get("fits_header") or {}).get("RUNCMD") not in _SCIENCE_RUNCMDS:
        return False
    exposed = row.get("EXPOSED")
    if isinstance(exposed, (int, float)) and exposed <= 0:
        return False
    name = row.get("OBJECT") or ""
    return not (_CALIBRATION_WORD.search(name) or _CALIBRATION_SUBSTRING.search(name))


def to_records(rows: list[dict], url: str, night: date) -> list[RawObservation]:
    records = []
    skipped: Counter = Counter()
    for row in _aat_common.exposures(rows):
        if not _is_science(row):
            continue
        instrument = INSTRUMENTS.get(str(row.get("instrument")).lower())
        if instrument is None:
            skipped[row.get("instrument")] += 1
            continue
        name = (row.get("OBJECT") or "").strip() or None
        ra, dec = _aat_common.clean_position(row.get("ra"), row.get("dec"))
        if name is None and ra is None:
            continue
        gaia_source_id = _aat_common.gaia_source_id_from_name(name, ra, dec) if name else None
        records.append(
            RawObservation(
                archive_obs_id=str(row["aat_id"]),
                archive_url=url,
                instrument=instrument,
                obs_date=_aat_common.row_obs_date(row, night),
                program_id=(row.get("ProgramID") or "").strip() or None,
                gaia_source_id=gaia_source_id,
                ra=ra,
                dec=dec,
                raw_target_name=None if gaia_source_id else name,
                reduction_status="raw",
            )
        )
    _aat_common.log_skipped_instruments(ARCHIVE_CODE, skipped)
    return records


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    return _aat_common.fetch(cursor, QUERIES, to_records)
