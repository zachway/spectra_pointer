"""Ritter Observatory Public Archive / PREST (University of Toledo) --
static HTML index, no TAP/API, one-shot pull of a frozen historical dataset.

https://astro1.panet.utoledo.edu/~wwritter/archive/ -- PREST = "Program for
Research and Education with Small Telescopes" (NSF-funded), a personal
faculty page (Nancy Morrison is the listed document custodian) reflecting a
real institutional dataset from the University of Toledo's 1.06-m Ritter
Observatory telescope. A real permanence risk, unlike an institutional
archive: this is hosted on a personal ~username faculty page with no known
mirror, so it could vanish if the account/page goes down.

Real echelle/LDS spectra, wavelength-calibrated (bias-subtracted,
flat-fielded, extracted -- per the homepage's own description of its data
processing), never raw 2D frames -- 'reduced' is the right side of this
project's raw/reduced bucket. Two instruments share the same 1.06-m
telescope/fiber feed and are distinguished live via each FITS header's own
NAXIS2 rather than the HTML page text's inconsistent "(LDS ...)"
annotations (confirmed live: a multi-order echelle file has NAXIS2=9, a
single-order LDS file has no NAXIS2 key at all): NAXIS2 present ->
"Ritter Echelle" (telescope.html describes ~9-21 useful orders across two
CCD camera generations, R~13,000-26,000 around H-alpha), NAXIS2 absent ->
"Ritter LDS" (Low-Dispersion Spectrograph, single order).

Frozen, historical dataset (confirmed live): despite the homepage's own
"New spectra are continually being added" banner, every per-file
Last-Modified header sampled falls in the 2011-2016 reprocessing-pass
window, and the underlying observations themselves span 1994-2007 -- one
full pull is enough forever, same reasoning elodie.py gives for its own
decommissioned-instrument one-shot pull.

No TAP/SSA/API of any kind, and no directory listing either (FITS-spectra/
itself 403s, confirmed live -- Apache with indexing off). The only way in
is two static top-level index pages, both confirmed live and unioned here
since neither alone is complete:
  - HDsearch.html: an HD-number-sorted index, ~70 unique per-star/per-group
    page links (some HD numbers point at the same shared group page, e.g.
    several different "normal OB star" HDs all link to NormOB/NormOB.html).
  - PREST-archive.html: the hand-curated homepage listing, organized by
    science category (telluric standards, RV standards, novae, Cepheids,
    active stars, SPB stars, binaries, shell stars, solar system objects)
    -- covers a handful of pages HDsearch.html's HD-indexed view misses
    entirely (e.g. betaVir, betaLib, epsPeg, AGDra, 73Leo).
Both are hand-maintained static HTML (BBEdit-authored, "Page last updated"
footers) with real, confirmed-live rot: 4 of the 92 unioned links 404
(dead/typo'd hrefs -- a literal "NormOB.html.html" double-extension typo,
and a "rhoCas/rhoCas.html" case mismatch where only "RhoCas/rhocas.html"
actually resolves) -- skipped rather than treated as fatal.

Solar-system objects (confirmed live under PREST-archive.html's own
"Solar System Objects" heading: Jupiter, Callisto, Ganymede, Io, Mars,
Moon, Venus, plus Comet C/2000 WM1, Comet Hale-Bopp, and Comet Hyakutake)
are excluded by directory name, per this project's stars-only scope --
along with tellstds/tellstds.html, confirmed live to be *artificial*
(synthesized) telluric-line-removal templates, not real observations of
anything.

Per-star-group HTML pages carry no ra/dec/date at all (confirmed live,
every page inspected -- just FITS filenames, in the form
YYYYMMDD.NNN.fits or, confirmed live on roughly half of the ~2,200 files,
an older 6-digit YYMMDD.NNN.fits) -- ra/dec/date/target all have to come
from each FITS header instead, one GET per file. Fetched via an HTTP Range
request (bytes=0-11519, 4 FITS 2880-byte blocks) rather than a full
download -- confirmed live the real header always ends well within the
first 2 blocks (5,760 bytes) on every file sampled, so 4 blocks leaves
comfortable margin while still cutting each request to about a quarter of
the ~48-52KB full file size, a meaningfully lighter footprint on a small
personal page being crawled for ~2,200 files in one pass.

FITS header fields (confirmed live across files spanning 1994-2007):
OBJECT (e.g. "alpha Leo", "35 Ari", "BD +63 1964" -- a real catalog name,
not always matching the page's own grouping label: NormOB.html's own
"HD 16908" section header turned out to hold a file whose OBJECT is
"35 Ari", the star's Flamsteed name instead -- OBJECT is trusted over the
page text for this reason). RA/DEC are sexagesimal strings
(" 10:08:08.0" / "+11:59:22", confirmed negative Dec uses a literal '-'
and positive Dec sometimes omits any sign at all, e.g. "27:40:55.00" --
SkyCoord parses both natively). DATE-OBS is inconsistent across the
archive's history: modern ISO "YYYY-MM-DD" on later files but a legacy
"DD/MM/YY" two-digit-year form on others (confirmed live, e.g.
"22/12/94") -- both parsed explicitly rather than assuming one format.
UT/EPOCH are not used: UT is a plain time-of-night with no bearing on
RawObservation.obs_date (a date, not a datetime), and EPOCH is the
telescope's own apparent-place equinox-of-date bookkeeping, not treated as
a proper-motion signal here -- ra/dec are used as reported, the same
face-value convention every other scraper module in this project follows
for its own catalog coordinates.

At least one real file (FITS-spectra/22Vul/20000810.018.fits, confirmed
live) has a DATE-OBS card present but with a blank, unquoted value --
invalid FITS, and astropy.io.fits.Card raises (VerifyError) rather than
returning something merely falsy when that specific card's value is
touched. _safe_get treats any such malformed card as an ordinary missing
field instead of failing the whole record, and obs_date falls back to the
date embedded in the file's own name (the archive's own stated
YYYYMMDD.NNN.fits / YYMMDD.NNN.fits convention, confirmed live reliable
on all but 6 of the ~2,200 stellar files) when DATE-OBS is missing or
unparsable either way.

Duplicate anchors happen on a handful of real pages (confirmed live, e.g.
alpha Leo's own page links "20050412.018.fits" twice, once per download-
button target) -- deduped by resolved absolute URL across the whole
crawl, not just within a page.

Confirmed live full crawl: 88 of the 92 unioned star/group page links
resolve (the other 4 404, see above), 77 of those aren't solar-system/
template pages, and those 77 carry 2,218 candidate stellar FITS links
across ~175 individually named stars (several group pages -- NormOB,
NormKM, NormFG, NormA, betaCepheiStars, ShellStars -- each hold multiple
different named stars) -- matching this project's own prior research
estimate of "low thousands across ~175 stars". A real fetch({}) run confirmed 2,201 of those 2,218 actually ingest
(17 individually 404 despite being linked from a live page, same
static-site rot as the 4 dead page links above) in ~325s, and confirmed
a second fetch() call with the returned cursor is an instant no-op
(0 records).

One-shot pull, same `synced_at` no-op convention as elodie.py/iacob.py --
appropriate for a dataset with no evidence of ongoing growth (see above).
A modest per-request delay is used throughout (index pages, per-star-group
pages, and per-file header requests all go through the same rate-limited
session) out of respect for what this is: a small personal academic page,
not a funded institutional API.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from urllib.parse import urljoin

import astropy.units as u
import requests
from astropy.coordinates import SkyCoord
from astropy.io import fits

from sync.base import RawObservation

BASE_URL = "https://astro1.panet.utoledo.edu/~wwritter/archive/"
INDEX_PAGES = ["PREST-archive.html", "HDsearch.html"]

# Confirmed live under PREST-archive.html's own "Solar System Objects"
# heading, plus the synthetic telluric-template page -- excluded by
# per-star-group directory name (see module docstring).
EXCLUDED_DIRS = {
    "Jupiter", "Callisto", "Ganymede", "Io", "Mars", "Moon", "Venus",
    "C2000WM1", "Hale-Bopp", "Hyakutake", "tellstds",
}

TIMEOUT = (15, 60)
REQUEST_DELAY = 0.1  # seconds before every request -- a small personal page, not an API
HEADER_RANGE = "bytes=0-11519"  # 4 FITS blocks; real headers end within 2 (see module docstring)

_PAGE_LINK_RE = re.compile(r'href="(FITS-spectra/[^"#]+\.html)"')
_FITS_LINK_RE = re.compile(r'href="([^"]+\.fits)"', re.I)

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SpectraPointer/1.0)"})


def _get(url: str, **kwargs) -> requests.Response | None:
    """GET url, returning None (not raising) on a 404 -- real, confirmed-live
    dead links exist among both the star/group pages and (rarely) their
    own FITS links -- see module docstring."""
    time.sleep(REQUEST_DELAY)
    response = _session.get(url, timeout=TIMEOUT, **kwargs)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response


def _find_star_pages() -> list[str]:
    """Unions both index pages' star/group page links -- neither alone is
    complete (see module docstring) -- then drops non-stellar directories."""
    pages: set[str] = set()
    for index_page in INDEX_PAGES:
        response = _get(urljoin(BASE_URL, index_page))
        if response is None:
            continue
        for href in _PAGE_LINK_RE.findall(response.text):
            if href.split("/")[1] not in EXCLUDED_DIRS:
                pages.add(href)
    return sorted(pages)


def _find_fits_urls(page_relpath: str) -> list[str]:
    page_url = urljoin(BASE_URL, page_relpath)
    response = _get(page_url)
    if response is None:
        return []
    return [urljoin(page_url, href.strip()) for href in _FITS_LINK_RE.findall(response.text)]


def _parse_date_obs(raw: str) -> date | None:
    """Handles both DATE-OBS conventions confirmed live on this archive --
    modern ISO "YYYY-MM-DD" and a legacy two-digit-year "DD/MM/YY"."""
    raw = raw.strip()
    if not raw:
        return None
    if "-" in raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    if "/" in raw:
        try:
            day, month, yy = (int(part) for part in raw.split("/"))
        except ValueError:
            return None
        year = 1900 + yy if yy >= 50 else 2000 + yy  # this archive's observations span 1994-2007
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _parse_coords(ra_str: str, dec_str: str) -> tuple[float, float] | tuple[None, None]:
    ra_str = " ".join(ra_str.split())
    dec_str = " ".join(dec_str.split())
    if not ra_str or not dec_str:
        return None, None
    try:
        coord = SkyCoord(ra=ra_str, dec=dec_str, unit=(u.hourangle, u.deg))
    except ValueError:
        return None, None
    return coord.ra.deg, coord.dec.deg


# "YYYYMMDD.NNN.fits" (confirmed live, the majority) or the older
# "YYMMDD.NNN.fits" (confirmed live on ~half the archive) -- used as a
# fallback obs_date source, see _safe_get's docstring below.
_FILENAME_DATE_RE = re.compile(r"(\d{6}|\d{8})\.\d+\.fits$", re.I)


def _parse_date_from_filename(fits_url: str) -> date | None:
    match = _FILENAME_DATE_RE.search(fits_url)
    if match is None:
        return None
    digits = match.group(1)
    try:
        if len(digits) == 8:
            return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        yy, month, day = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
        year = 1900 + yy if yy >= 50 else 2000 + yy  # this archive's observations span 1994-2007
        return date(year, month, day)
    except ValueError:
        return None


def _safe_get(header: fits.Header, key: str):
    """header.get() still raises (not just returns None) on a card whose
    *value* -- not just its presence -- is malformed, confirmed live: one
    real file (FITS-spectra/22Vul/20000810.018.fits) has a DATE-OBS card
    present with a blank, unquoted, non-numeric value, which
    astropy.io.fits.Card refuses to parse at all (VerifyError) rather than
    silently returning something falsy. Treated as an ordinary missing
    field here rather than failing the whole record -- a lone bad card on
    an otherwise-good file shouldn't drop the file's other real fields,
    same reasoning _parse_date_from_filename's DATE-OBS fallback exists for."""
    try:
        return header.get(key)
    except Exception:
        return None


def _fetch_record(fits_url: str) -> RawObservation | None:
    response = _get(fits_url, headers={"Range": HEADER_RANGE})
    if response is None:
        return None
    data = response.content
    header = fits.Header.fromstring(data[: (len(data) // 80) * 80], sep="")

    object_name = str(_safe_get(header, "OBJECT") or "").strip() or None
    ra_raw, dec_raw = _safe_get(header, "RA"), _safe_get(header, "DEC")
    ra, dec = _parse_coords(str(ra_raw), str(dec_raw)) if ra_raw and dec_raw else (None, None)
    obs_date = _parse_date_obs(str(_safe_get(header, "DATE-OBS") or ""))
    if obs_date is None:
        obs_date = _parse_date_from_filename(fits_url)
    instrument = "Ritter Echelle" if _safe_get(header, "NAXIS2") else "Ritter LDS"

    return RawObservation(
        archive_obs_id=fits_url[len(BASE_URL):],
        archive_url=fits_url,
        instrument=instrument,
        obs_date=obs_date,
        ra=ra,
        dec=dec,
        raw_target_name=object_name,
        reduction_status="reduced",
    )


def fetch(cursor: dict) -> tuple[list[RawObservation], dict]:
    if cursor.get("synced_at"):
        return [], cursor

    fits_urls: list[str] = []
    seen: set[str] = set()
    for page in _find_star_pages():
        for url in _find_fits_urls(page):
            if url not in seen:
                seen.add(url)
                fits_urls.append(url)

    records = []
    for fits_url in fits_urls:
        record = _fetch_record(fits_url)
        if record is not None:
            records.append(record)

    new_cursor = {"synced_at": datetime.now().isoformat(), "row_count": len(records)}
    return records, new_cursor
