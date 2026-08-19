"""HEROS at Ondrejov (Ondrejov 2m Perek Telescope, Czech Republic) — TAP.

Distinct from both existing HEROS-adjacent archives in this project:
flashheros_gavo.py covers Heidelberg's own Flash/Heros holdings (a
different GAVO DaCHS host, dc.g-vo.org, La Silla-era bright-star survey,
late 1990s), and ondrejov.py covers Ondrejov's CCD700 coude spectrograph
(voarchive.asu.cas.cz, a different instrument entirely). This module
covers a third, separate thing: HEROS itself was operated at Ondrejov's
own Zeiss 2m telescope for a few years under a joint-but-separate
agreement with Heidelberg, and the Ondrejov-owned (non-Heidelberg) share
of those observations was made public here, per a collaborator's tip.

Same GAVO DaCHS software family as flashheros_gavo/ondrejov/feros_gavo/
hermes_mercator, so the same shortcut applies: a plain TAP endpoint at
vos2.asu.cas.cz/tap (table heros.data), not just the SSA-style web form at
vos2.asu.cas.cz/heros/q/web/form this was found from.

Observed: `ssa_creator = "Heros Ond"`, `ssa_instrument = "Ondrejov
Zeiss 2m"` -- genuinely the Ondrejov-mounted HEROS, not a mirror of
Heidelberg's La Silla data. ssa_dateobs (MJD) spans 51770-52727
(2000-09-25 to 2003-04-15), matching "used for a few years" -- a
completely different era than flashheros_gavo.py's 1990s La Silla data,
and HEROS hasn't operated at Ondrejov since, so this dataset reads as
historical/frozen rather than still-growing. Paginated by watermark anyway
(same shape as ondrejov.py) rather than assumed one-shot, since it's the
same free correctness margin at near-zero extra cost.

Same paired-row mime quirk as feros_gavo/flashheros_gavo/ondrejov: 2,020
total rows split 1,010 image/fits (real spectra) + 1,010
application/x-votable+xml metadata siblings -- filtered via mime. All
1,010 real rows observed to have a populated ssa_location (unlike
flashheros_gavo.py's positionless data), so normal identifier-then-
position matching applies here, not name-only.

ssa_location is a plain text string ("Position ICRS <ra> <dec>"), same
format as ondrejov.py's ccd700.data -- same whitespace-split parser
applies. ssa_targname uses the same bright-star naming convention as
flashheros_gavo.py (e.g. "gamCas", "6Cep", "alpLyr").

No plain `instrument` column exists on this table (observed: querying
for one errors "No such field known") -- instrument is a per-service
constant here, same situation as flashheros_gavo.py, so it's hardcoded
below rather than read from a row.
"""

from __future__ import annotations

from astropy.time import Time

from sync.base import RawObservation, clean_float, make_tap_service

TAP_URL = "http://vos2.asu.cas.cz/tap"

QUERY = """
SELECT TOP {page_size} accref, ssa_targname, ssa_dateobs, ssa_location
FROM heros.data
WHERE mime = 'image/fits' AND ssa_dateobs > {last_dateobs}
ORDER BY ssa_dateobs ASC
"""

# Whole table is 1,010 real rows (observed) -- comfortably one page,
# but kept generous rather than hardcoded to today's exact count, same
# reasoning ondrejov.py gives for its own PAGE_SIZE.
PAGE_SIZE = 100000

INSTRUMENT = "HEROS (Ondrejov)"


def _parse_location(location: str) -> tuple[float | None, float | None]:
    # "Position ICRS <ra> <dec>" -- see module docstring.
    if not location:
        return None, None
    parts = location.split()
    if len(parts) < 2:
        return None, None
    return clean_float(parts[-2]), clean_float(parts[-1])


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    last_dateobs = cursor.get("last_dateobs", 0)
    last_accrefs = set(cursor.get("last_accrefs", []))

    tap = make_tap_service(TAP_URL)
    query = QUERY.format(page_size=PAGE_SIZE, last_dateobs=last_dateobs)
    table = tap.search(query, maxrec=PAGE_SIZE).to_table()

    records = []
    max_dateobs = last_dateobs
    max_dateobs_accrefs: set[str] = set(last_accrefs)
    for row in table:
        accref = str(row["accref"])
        dateobs = clean_float(row["ssa_dateobs"])

        # See module docstring -- guards against re-matching the exact
        # boundary row on the next page (same shape as ondrejov.py's
        # own last_accrefs guard).
        if dateobs == last_dateobs and accref in last_accrefs:
            continue

        if dateobs is not None:
            if dateobs > max_dateobs:
                max_dateobs = dateobs
                max_dateobs_accrefs = set()
            if dateobs == max_dateobs:
                max_dateobs_accrefs.add(accref)

        ra, dec = _parse_location(str(row["ssa_location"]) if row["ssa_location"] else "")
        records.append(
            RawObservation(
                archive_obs_id=accref,
                archive_url=accref,
                instrument=INSTRUMENT,
                obs_date=Time(dateobs, format="mjd").to_datetime().date() if dateobs is not None else None,
                ra=ra,
                dec=dec,
                raw_target_name=str(row["ssa_targname"]) or None,
            )
        )

    new_cursor = {"last_dateobs": max_dateobs, "last_accrefs": sorted(max_dateobs_accrefs)}
    return records, new_cursor
