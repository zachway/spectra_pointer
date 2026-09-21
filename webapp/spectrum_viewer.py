"""Per-star spectrum fetch + parse, scoped to the archives confirmed live
(this session) to have a real, directly-fetchable reduced-spectrum file with
a known wavelength/flux/uncertainty shape -- see the Spectral Access Ledger
audit. Everything else in spectroscopy_holdings either has no direct file
access, is raw data, or has an unconfirmed/nonstandard format -- deliberately
not wired up here rather than guessing at a shape.

sdss_v_optical and sdss_legacy_optical both use SDSS's standard per-pixel-row
spec-file shape (COADD bintable, one row per wavelength pixel -- not a
single-row-of-arrays like lamost's COADD): loglam/flux/ivar columns, same as
every SDSS optical spectro product. sdss_v_optical's archive_url was already
confirmed live by its own sync module; sdss_legacy_optical's was fixed and
live-fetched this session (COADD columns confirmed: flux/loglam/ivar/
and_mask/or_mask/wdisp/sky/model) -- both share one parser below. Note:
sdss_legacy_optical's existing ~4.5M holdings rows still carry their old
pre-fix archive_url (a SkyServer portal page, not a file) until that
archive's sync cursor is reset and it's resynced -- this parser will work
for those rows immediately once that resync lands, no further code change
needed.

galah/spitzer_sha/iacob/mast (the general HST/IUE/EUVE/... archive) added
later after live-fetching real production rows of every product type each
handles -- see _parse_galah/_parse_spitzer_sha/_parse_iacob/_parse_mast for
the confirmed shapes. mast and spitzer_sha only cover some rows of their
archive_code (image/raw products, IRS Map, MIPS-SED are excluded), so
viewability is decided per row by is_spectrum_viewable() via _ROW_GATES,
not by archive_code alone. The earlier note below that mast is unimplemented
is superseded: it needed only a per-row product gate, not a sync change.

mast_jwst/eso/lamost_mrs/elodie added after checking real production
samples (not just one earlier one-off fetch each) -- two archives that
looked "nearly free" from format alone turned out NOT to be, and are
deliberately still unimplemented:
  - mast (the general HST/IUE/... archive_code, not mast_jwst): a real
    sampled row was an _asn.fits association/manifest file, not a
    spectrum -- access_url isn't reliably a science product for every row
    here, unlike mast_jwst's EXTRACT1D. Needs a sync-side product-type
    fix in sync/archives/mast.py before this is safe to wire up.
  - lco_floyds/lco_nres: two different real sampled rows both resolved to
    an RLEVEL=90 fallback product (PRIMARY-only, no bintable) rather than
    the clean "wavelength"/"flux"/"uncertainty" bintable a single earlier
    check found on a "-1d" product -- RLEVEL=90 appears to be the common
    case for real synced rows, not the exception, and its actual shape
    (2D rectified frame vs. something else) isn't nailed down. Skipped
    rather than guessed at.

irsa_missions is a 6-sub-collection grab-bag behind one archive_code, not
one uniform format -- confirmed live this session that only Spitzer/IRS
(both SASS and Std Stars) has the clean bintable shape; IRTF/MEarth
turned out to be a bare WCS image (no bintable at all), and
ISO/SOFIA/IRAS use other shapes not checked here. Gated on
instrument LIKE 'Spitzer/IRS%' rather than the whole archive_code.

eso.py stores archive_url as a human landing page (archive.eso.org/
dataset/{dp_id}), never a file link -- archive_obs_id is the same dp_id,
so the real file is built directly (ESO_FILE_URL below) rather than
fetching archive_url, same shape as the sdss_legacy_optical fix. A real
ESO/FEROS sample this session also had a fully-NaN ERR column (populated
WAVE/FLUX, no usable uncertainty at all for that file) -- _segment() below
treats an all-non-finite uncertainty array as absent rather than letting
it wipe out every real data point, a latent bug this caught that could
have affected any archive, not just this one. A later live sweep of all
17 real eso instrument values found the WAVE column's actual unit is NOT
always Å despite the parser previously hardcoding that label: XSHOOTER/
CRIRES/GIRAFFE report nm and SINFONI reports um (confirmed via each
column's own TUNIT, cross-checked against known instrument coverage) --
_eso_wave_to_angstrom below converts using the real per-file unit instead
of assuming. APEXHET (sub-mm heterodyne, FREQ not WAVE) and APEXHET/
EFOSC/SOFI/VIMOS's SPECTRUM-less raw/imaging products are cleanly
rejected rather than crashing with a raw KeyError, same defensive pattern
as everywhere else in this module.

rave/feros_gavo/flashheros_gavo/ondrejov/heros_ondrejov/sophie/
hermes_mercator added after re-checking real production samples for each
(not the earlier one-off audit alone). feros_gavo/flashheros_gavo/
ondrejov/heros_ondrejov share one shape -- single-HDU image, linear WCS
wavelength, genuinely no uncertainty extension at all -- one shared
parser (_parse_gavo_wcs_image). rave has a real SPECTRUM/ERROR HDU pair,
both carrying their own WCS. sophie's S1D_B extension looked like it
might be an error array but is a second, slightly different-length
channel instead (confirmed live) -- S1D_A alone is used, no uncertainty.
hermes_mercator is the odd one out even among its own DaCHS siblings:
archive_url 301-redirects to a DataLink-served VOTable, not plain FITS
(spectral/flux fields, no error field) -- handled with its own
redirect-following fetch since _fetch_bytes' size check happens before
requests would follow that redirect.

svo_cab was checked but NOT added -- a real sample from its XSL
sub-collection (one of 5 SVOCat instances behind this one archive_code)
returned "No data found for ID=320" from its own ssap.php, a genuine
access failure rather than a format question. The earlier audit's
"confirmed live" MILES fetch may not generalize to XSL/STELIB/CaT/GBS;
needs real investigation into what's actually wrong (stale ID, wrong
per-collection endpoint, ...) before this is safe to wire up.

Each archive gets its own parser below, dispatched by archive_code via
SUPPORTED_ARCHIVES. All four turned out to already store a real, directly
fetchable URL in archive_url (no DataLink/resolution hop needed) -- kept
that way rather than reconstructing paths, since archive_url is exactly the
thing sync/archives/*.py already verified live.

Memory discipline (a single gunicorn worker process handles each request):
every fetch is bounded --
MAX_DOWNLOAD_BYTES caps a plain HTTP pull (checked via Content-Length where
available, and enforced while streaming either way since a server can lie
about or omit that header); DESI's fsspec/HTTP-range path never downloads
more than the FIBERMAP extension plus one row per camera, regardless of the
per-healpix coadd file's real size (600KB-200MB+, confirmed live this
session). Arrays are downsampled to MAX_PLOT_POINTS per segment before
they ever leave this module -- nothing here holds a raw multi-thousand-point
array longer than one request.

Units are standardized where a real physical conversion exists, so several
archives' spectra can be overlaid on one plot and actually mean something --
NOT normalized/rescaled to "look similar", which would misrepresent
uncalibrated data as if it were comparable. Wavelength is always Å (mast_jwst
and irsa_missions/Spitzer are natively μm -- converted, ×1e4, real unit
conversion not a guess). Flux is converted to DESI/SDSS's own native
"1e-17 erg/s/cm²/Å" convention wherever the source unit is a real physical
one with a well-defined conversion: mast_jwst's Jy (F_ν) via the standard
F_λ = F_ν·c/λ² relation (_jy_to_flambda_1e17 below). Every result also
carries flux_unit_family -- 'erg_cm2_s_A_1e-17' for anything on that
converted/native scale, else 'arbitrary' (eso's adu, and every archive whose
FITS/VOTable carried no unit metadata at all when checked live -- lamost,
lamost_mrs, sdss_v_apogee, elodie, irsa_missions, and the whole GAVO/DaCHS
family: confirmed live these genuinely have no calibration to convert, not
a gap in this module). The webapp uses flux_unit_family to warn rather than
silently mislead when a user overlays 'arbitrary' spectra alongside
'erg_cm2_s_A_1e-17' ones.

On top of that real unit conversion, every spectrum also gets a live,
per-spectrum display scale (_apply_display_scale below: flux_scale_factor
= 1/median(|flux|)) applied before this module returns it, so the webapp
can plot every archive on one shared "Scaled Flux" y-axis instead of
needing a separate auto-scaled axis per flux_unit (an earlier version did
that; it worked, but was visually busier than wanted once several
instruments were on one plot). This is explicitly NOT a claim of physical
comparability -- it's a display convenience. A first cut used a FIXED
per-archive_code constant derived from one example spectrum instead of
computing this live; that broke for multi-instrument archives (eso's
UVES/HARPS/FEROS/... have very different typical magnitudes -- one
example's factor sent HARPS spectra to ~1e6 on the plot, confirmed live)
and was one unlucky example star away from being wrong for any archive.
Per-spectrum normalization trades away exact star-to-star relative
brightness within one archive for that robustness. flux_unit/
flux_unit_family (pre-scaling) are still returned for hover text and
transparency -- only the axis label changes.
"""

from __future__ import annotations

import bz2
import gzip
import io
import logging
import math
import re
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from astropy.io import fits
from astropy.io.votable import parse_single_table

from webapp.continuum import continuum_normalize_segment

SUPPORTED_ARCHIVES = {
    "lamost", "gaia_rvs", "sdss_v_apogee", "desi", "sdss_v_optical", "sdss_legacy_optical",
    "mast_jwst", "eso", "lamost_mrs", "elodie", "irsa_missions",
    "rave", "feros_gavo", "flashheros_gavo", "ondrejov", "heros_ondrejov", "sophie", "hermes_mercator",
    "carmenes_tac", "carmenes_reiners2018", "cfht_cadc",
    "galah", "spitzer_sha", "iacob", "mast",
    "naoj", "harpsn_tng", "hpol", "svo_cab", "gemini_ghost",
}

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # hard cap -- enforced regardless of the size hint below
REQUEST_TIMEOUT_SECONDS = 20
MAX_PLOT_POINTS = 3000  # per segment -- plenty for a legible line plot, well under browser strain

# Real observed sample sizes (confirmed live), used only to decide whether to
# show a "this is a large file" warning before fetching -- NOT a substitute
# for MAX_DOWNLOAD_BYTES, which still applies regardless. None of these are
# actually heavy today; the point is that a future archive with a real
# multi-MB/multi-order product (cfht_cadc's 56MB Stokes cube, polarbase's
# 15.7MB JSON) has somewhere to register that fact rather than the route
# silently attempting a big fetch on every click -- including from a crawler
# or link-preview bot following a plain <a href>, not just a real user.
# desi is deliberately absent -- its fsspec/HTTP-range path never pulls more
# than a few KB regardless of the backing coadd file's real size (confirmed
# live this session, 600KB-200MB+ files, one row fetched in under a second).
SIZE_HINT_BYTES = {
    "lamost": 60_000,
    "lamost_mrs": 320_000,
    "gaia_rvs": 65_000,
    "sdss_v_apogee": 1_000_000,
    "sdss_v_optical": 220_000,
    "sdss_legacy_optical": 220_000,
    "mast_jwst": 680_000,
    # Not a single representative size -- eso covers many instruments with
    # very different typical file sizes (confirmed live: a FEROS spectrum
    # sample was ~3.1MB, an ESPRESSO one 58.7MB, over MAX_DOWNLOAD_BYTES
    # outright). Set high enough to reliably trigger the heavy-file warning
    # rather than pretending one small instrument's size represents them
    # all -- same root cause as the per-instrument flux-scale bug this
    # session (eso's per-archive_code granularity doesn't fit its own
    # multi-instrument reality).
    "eso": 20_000_000,
    "elodie": 480_000,
    "irsa_missions": 15_000,
    "rave": 20_000,
    "feros_gavo": 770_000,
    "flashheros_gavo": 98_000,
    "ondrejov": 20_000,
    "heros_ondrejov": 112_000,
    "sophie": 2_500_000,
    "hermes_mercator": 3_600_000,
    # VIS files run ~5.0-5.5MB (61 orders x ~3700-4100 px x 3-4 image
    # extensions), just over HEAVY_THRESHOLD_BYTES -- NIR files are smaller
    # (~2.3-2.8MB) but archive_code granularity can't distinguish the two
    # channels here (see is_heavy/SIZE_HINT_BYTES docstring), so this uses
    # the heavier VIS figure rather than understate a real VIS fetch.
    "carmenes_tac": 5_500_000,
    "carmenes_reiners2018": 5_100_000,
    # A real usable "t" (telluric-corrected) SPIRou product ran 18.4MB
    # (49 orders x 4088 px x 11 image extensions) -- the rejected product
    # types (CCF-only, raw cubes) are smaller or hit MAX_DOWNLOAD_BYTES
    # outright before this hint would even matter, so the usable case is
    # the one worth warning about.
    "cfht_cadc": 18_500_000,
    "galah": 160_000,  # 4 camera bands x ~40KB
    "spitzer_sha": 500_000,  # ~12 exposures x ~26KB per channel, 2-4 channels
    "iacob": 5_400_000,  # 2 x 335,539 float64 -- real sample, just over HEAVY_THRESHOLD_BYTES
    "mast": 500_000,  # HST x1d/x1dsum 260-430KB, cspec 40KB, aspec 1.9MB, IUE 0.7MB, EUVE 43KB
    "naoj": 225_000,  # HDS 1D primary image
    "harpsn_tng": 4_600_000,  # S1D_FLUXCAL bintable, ~4.6MB gz (older s1d_A: ~1MB)
    "hpol": 20_000,
    "svo_cab": 4_800_000,  # gbs is 4.8MB; miles/catlib/stelib/xshooter are 8KB-820KB
    "gemini_ghost": 3_900_000,  # bz2, 2.7MB blue / 3.9MB red
}

HEAVY_THRESHOLD_BYTES = 5 * 1024 * 1024


def is_heavy(archive_code: str) -> bool:
    """True if this archive's typical file is large enough to warrant asking
    before fetching, rather than just fetching on click. An archive with no
    registered hint is treated as light -- add a SIZE_HINT_BYTES entry (or
    True in a dedicated always-heavy set, if size genuinely varies per row
    rather than being roughly fixed) when wiring up a new archive whose
    files aren't reliably small."""
    hint = SIZE_HINT_BYTES.get(archive_code)
    return hint is not None and hint > HEAVY_THRESHOLD_BYTES


def size_hint_label(archive_code: str) -> str | None:
    hint = SIZE_HINT_BYTES.get(archive_code)
    if hint is None:
        return None
    return f"~{hint / (1024 * 1024):.1f} MB" if hint >= 1024 * 1024 else f"~{hint // 1024} KB"


# The common baseline every physically-calibrated archive's flux gets
# converted to -- DESI/SDSS's own native convention, chosen because it's
# already what 3 of the 18 archives report natively, not an arbitrary pick.
FLUX_UNIT_ERG_CM2_S_A = "10⁻¹⁷ erg/s/cm²/Å"
FLUX_FAMILY_ERG_CM2_S_A = "erg_cm2_s_A_1e-17"
FLUX_FAMILY_ARBITRARY = "arbitrary"

def _apply_display_scale(result: dict) -> None:
    """Normalizes flux/uncertainty in-place (flux_scale_factor = 1 /
    median(|flux|)) so every spectrum lands at a consistent order-of-
    magnitude on the shared "Scaled Flux" axis, regardless of archive,
    instrument, or how bright the actual star is -- NOT a physical
    calibration (see flux_unit_family for what's actually comparable).

    Computed live, per spectrum, from data already sitting in memory at
    fetch time -- not a fixed per-archive constant. An earlier version used
    one fixed SCALE_FACTOR per archive_code, derived from a single example
    spectrum; that broke down two ways, both confirmed live: (1) a
    multi-instrument archive_code like eso covers UVES/HARPS/FEROS/
    ESPRESSO/... with very different typical magnitudes -- one FEROS
    example's factor, applied to a HARPS spectrum, blew it up to ~1e6 on
    the plot; (2) even for a single-instrument archive, one example star is
    a noisy estimate of "typical" when real stellar brightness varies by
    orders of magnitude star to star. Per-spectrum normalization sidesteps
    both -- no precomputed table to keep in sync with new archives/
    instruments, and no single example's brightness to get unlucky with.

    Median (not mean/max) for robustness against a few outlier pixels
    (cosmic rays, a bad column) skewing the scale; computed across all
    segments combined, not per-segment, so a multi-arm spectrum like
    DESI's B/R/Z keeps its real relative levels between arms while the
    whole spectrum normalizes together. Trades away exact star-to-star
    relative brightness within one archive (a genuinely brighter star no
    longer necessarily plots higher) for robustness -- the point of
    overlaying spectra from different archives/instruments here is shape/
    feature comparison, not absolute brightness, which most of these
    archives can't support anyway (flux_unit_family='arbitrary' for most).
    """
    all_flux = [abs(f) for seg in result["segments"] for f in seg["flux"] if f == f and f != 0]
    median = sorted(all_flux)[len(all_flux) // 2] if all_flux else None
    factor = (1.0 / median) if median else 1.0
    result["flux_scale_factor"] = factor
    if factor != 1.0:
        for seg in result["segments"]:
            seg["flux"] = [f * factor for f in seg["flux"]]
            if seg["uncertainty"] is not None:
                seg["uncertainty"] = [u * factor for u in seg["uncertainty"]]


_C_CM_PER_S = 2.99792458e10


def _jy_to_flambda_1e17(wave_angstrom: np.ndarray, flux_jy: np.ndarray) -> np.ndarray:
    """F_nu[Jy] -> F_lambda[10^-17 erg/s/cm^2/A] via F_lambda = F_nu * c / lambda^2,
    a standard physical relation (not a fitted/per-instrument calibration).
    Jy = 1e-23 erg/s/cm^2/Hz; the 1e8 cm->A and 1e17-baseline factors fold
    into the single constant below (see the module docstring for the full
    derivation)."""
    return flux_jy * (_C_CM_PER_S * 1e-15 * 1e17) / (wave_angstrom**2)


class SpectrumUnavailable(Exception):
    """Raised with a user-facing message -- the route turns this into a page, not a 500."""


# Per-IP rate limit on actual archive fetches -- MAX_DOWNLOAD_BYTES bounds
# any one fetch, but nothing else stops a script from hitting /spectrum/<id>
# in a loop across many different holdings, each a real external download.
# In-memory, per-process -- gunicorn may run several worker processes, so
# this blunts casual scripted abuse/crawlers rather than being an airtight
# global cap (a real distributed limiter would need shared state, not worth
# it for this project's traffic level). Deliberately scoped to the fetch
# itself, not page views in
# general -- viewing the "this looks heavy, load anyway?" interstitial from
# the is_heavy gate costs nothing and isn't rate-limited.
_RATE_LIMIT_WINDOW_SECONDS = 600  # 10 minutes
_RATE_LIMIT_MAX_FETCHES = 20  # per IP per window -- generous for a real person browsing holdings

_rate_limit_lock = threading.Lock()
_rate_limit_state: dict[str, list[float]] = {}


def check_rate_limit(client_ip: str) -> None:
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    with _rate_limit_lock:
        timestamps = [t for t in _rate_limit_state.get(client_ip, []) if t >= cutoff]
        if len(timestamps) >= _RATE_LIMIT_MAX_FETCHES:
            _rate_limit_state[client_ip] = timestamps
            raise SpectrumUnavailable(
                f"Too many spectrum requests from your address in the last "
                f"{_RATE_LIMIT_WINDOW_SECONDS // 60} minutes -- please wait a bit before loading more."
            )
        timestamps.append(now)
        _rate_limit_state[client_ip] = timestamps


# REQUEST_TIMEOUT_SECONDS is per socket read, so a slow-but-steady archive
# (a trickling 3.6MB hermes_mercator VOTable, say) can hold one request open
# for minutes -- long enough for the reverse proxy or browser to drop the
# connection, which the page shows as an opaque "TypeError: Failed to fetch"
# instead of our own message. This caps the whole download.
TOTAL_DOWNLOAD_DEADLINE_SECONDS = 40


def _fetch_bytes(url: str) -> bytes:
    deadline = time.monotonic() + TOTAL_DOWNLOAD_DEADLINE_SECONDS
    try:
        with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise SpectrumUnavailable(
                    f"Spectrum file is too large to display ({int(content_length):,} bytes)."
                )
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if time.monotonic() > deadline:
                    raise SpectrumUnavailable(
                        f"The archive is responding too slowly (over {TOTAL_DOWNLOAD_DEADLINE_SECONDS}s) -- try again later."
                    )
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise SpectrumUnavailable("Spectrum file exceeded the size limit while downloading.")
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise SpectrumUnavailable(f"Could not reach the archive: {exc}") from exc
    return b"".join(chunks)


def _downsample(x: np.ndarray, y: np.ndarray, max_points: int = MAX_PLOT_POINTS) -> tuple[list[float], list[float]]:
    n = len(x)
    if n <= max_points:
        return x.tolist(), y.tolist()
    step = int(np.ceil(n / max_points))
    return x[::step].tolist(), y[::step].tolist()


def _ivar_to_uncertainty(ivar: np.ndarray) -> np.ndarray:
    # np.where evaluates both branches eagerly, so 1/sqrt(0) warns even
    # though its result is discarded -- suppress rather than let a per-pixel
    # zero-ivar mask entry spam a divide-by-zero warning on every fetch.
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(ivar > 0, 1.0 / np.sqrt(ivar), np.nan)


def _finite_mask(*arrays: np.ndarray) -> np.ndarray:
    mask = np.ones(len(arrays[0]), dtype=bool)
    for arr in arrays:
        mask &= np.isfinite(arr)
    return mask


def _segment(
    label: str, wave: np.ndarray, flux: np.ndarray, unc: np.ndarray | None, max_points: int = MAX_PLOT_POINTS
) -> dict:
    # An uncertainty column can genuinely exist but be entirely NaN for a
    # given file (confirmed live: a real ESO/FEROS spectrum with a
    # populated WAVE/FLUX but a fully-empty ERR) -- requiring it finite
    # alongside wave/flux would silently drop every point. Treat an
    # all-non-finite uncertainty array as absent rather than let it wipe
    # out real data.
    if unc is not None and not np.any(np.isfinite(unc)):
        unc = None

    if unc is not None:
        mask = _finite_mask(wave, flux, unc)
    else:
        mask = _finite_mask(wave, flux)
    wave, flux = wave[mask], flux[mask]
    unc = unc[mask] if unc is not None else None

    wx, wy = _downsample(wave, flux, max_points)
    result = {"label": label, "wavelength": wx, "flux": wy, "uncertainty": None}
    if unc is not None:
        _, wu = _downsample(wave, unc, max_points)
        result["uncertainty"] = wu
    return result


def _parse_lamost(holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass  # some mirrors may already serve it decompressed -- fall through and let fits.open fail loudly if not
    with fits.open(io.BytesIO(raw)) as hdul:
        coadd = hdul["COADD"]
        wave = np.asarray(coadd.data["WAVELENGTH"][0], dtype=float)
        flux = np.asarray(coadd.data["FLUX"][0], dtype=float)
        ivar = np.asarray(coadd.data["IVAR"][0], dtype=float)
    uncertainty = _ivar_to_uncertainty(ivar)
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (pipeline flux units)",
        "segments": [_segment("LAMOST", wave, flux, uncertainty)],
    }


def _parse_gaia_rvs(holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    table = parse_single_table(io.BytesIO(raw)).to_table()
    wave = np.ma.filled(np.asarray(table["wavelength"]), np.nan).astype(float) * 10.0  # nm -> Å, matches the other 3
    flux = np.ma.filled(np.asarray(table["flux"]), np.nan).astype(float)
    flux_error = np.ma.filled(np.asarray(table["flux_error"]), np.nan).astype(float)
    return {
        "wavelength_unit": "Å",
        "flux_unit": "normalized",
        "segments": [_segment("Gaia RVS", wave, flux, flux_error)],
    }


def _parse_sdss_v_apogee(holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        header = hdul[1].header
        crval1, cdelt1, naxis1 = header["CRVAL1"], header["CDELT1"], header["NAXIS1"]
        wave = 10 ** (crval1 + np.arange(naxis1) * cdelt1)
        # Row 0 is the pipeline-combined spectrum; later rows are individual
        # visits (confirmed live this session) -- combined is what a per-star
        # viewer wants, not one arbitrary visit.
        flux = np.asarray(hdul[1].data[0], dtype=float)
        uncertainty = np.asarray(hdul[2].data[0], dtype=float)
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (apStar flux units)",
        "segments": [_segment("APOGEE (combined)", wave, flux, uncertainty)],
    }


_DESI_ARMS = ("B", "R", "Z")


def _parse_desi(holding: dict) -> dict:
    target_id = int(holding["archive_obs_id"])
    try:
        # use_fsspec + lazy_load_hdus: only header blocks plus the rows we
        # actually index get pulled over HTTP range requests -- confirmed
        # live this session to fetch one target's row out of a 187MB file in
        # under a second, nowhere near downloading the whole coadd.
        with fits.open(
            holding["archive_url"],
            use_fsspec=True,
            lazy_load_hdus=True,
            fsspec_kwargs={"block_size": 256 * 1024},
        ) as hdul:
            fibermap = hdul["FIBERMAP"]
            target_ids = fibermap.data["TARGETID"]
            matches = (target_ids == target_id).nonzero()[0]
            if len(matches) == 0:
                raise SpectrumUnavailable("This target's row was not found in its DESI coadd file.")
            row = int(matches[0])

            segments = []
            for arm in _DESI_ARMS:
                wave = np.asarray(hdul[f"{arm}_WAVELENGTH"].data, dtype=float)
                flux = np.asarray(hdul[f"{arm}_FLUX"].data[row], dtype=float)
                ivar = np.asarray(hdul[f"{arm}_IVAR"].data[row], dtype=float)
                uncertainty = _ivar_to_uncertainty(ivar)
                segments.append(_segment(f"DESI {arm}", wave, flux, uncertainty))
    except OSError as exc:
        raise SpectrumUnavailable(f"Could not reach the DESI archive: {exc}") from exc

    return {
        "wavelength_unit": "Å",
        "flux_unit": FLUX_UNIT_ERG_CM2_S_A,
        "segments": segments,
    }


def _parse_sdss_spec(label: str, holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        coadd = hdul["COADD"].data
        # Standard SDSS spec shape: COADD is one row *per pixel* (unlike
        # lamost's single-row-of-arrays COADD) -- read the columns directly,
        # no [0] indexing.
        loglam = np.asarray(coadd["loglam"], dtype=float)
        flux = np.asarray(coadd["flux"], dtype=float)
        ivar = np.asarray(coadd["ivar"], dtype=float)
    wave = 10.0**loglam
    uncertainty = _ivar_to_uncertainty(ivar)
    return {
        "wavelength_unit": "Å",
        "flux_unit": FLUX_UNIT_ERG_CM2_S_A,
        "segments": [_segment(label, wave, flux, uncertainty)],
    }


def _parse_sdss_v_optical(holding: dict) -> dict:
    return _parse_sdss_spec("SDSS-V", holding)


# Pre-BOSS (SDSS-I/II, run2d 26/103/104) legacy rows have no per-object file
# in DR20 at all -- only a per-plate spPlate-{plate}-{mjd}.fits holding every
# fiber on that plate as a 2D image (live-confirmed 2026-08-21: 59MB for one
# plate, no "spectra/lite" tree under these run2d values). scripts/
# backfill_sdss_legacy_pre_boss.py repoints those rows' archive_url at the
# spPlate file with the fiber number tacked on as a query param (confirmed
# live: a stray query string doesn't change data.sdss.org's static-file
# response) since there's no dedicated column to carry it and decoding
# specobjid's bit-packed fiberid by hand is exactly what the plate/fiberid
# columns elsewhere in this codebase were added to avoid.
_SDSS_LEGACY_SPPLATE_RE = re.compile(r"/spPlate-\d+-\d+\.fits\?fiber=(\d+)$")


def _parse_sdss_legacy_plate(url: str) -> dict:
    match = _SDSS_LEGACY_SPPLATE_RE.search(url)
    if match is None:
        raise SpectrumUnavailable("Malformed spPlate archive_url (missing fiber number).")
    fiber = int(match.group(1))
    file_url = url.split("?", 1)[0]
    try:
        # Same use_fsspec + lazy_load_hdus technique as _parse_desi -- pulls
        # only the header blocks plus the one fiber's row via HTTP range
        # requests, not the full 59MB (640-fiber) file.
        with fits.open(
            file_url,
            use_fsspec=True,
            lazy_load_hdus=True,
            fsspec_kwargs={"block_size": 256 * 1024},
        ) as hdul:
            header = hdul[0].header
            # DC-FLAG=1 log-linear dispersion, same convention as
            # _parse_desi's apStar wavelength solution -- live-confirmed
            # against a real spPlate primary header (CRVAL1/CD1_1 present,
            # no EXTNAME on any HDU here so index, not name, is how these
            # are addressed).
            wave = 10 ** (header["CRVAL1"] + np.arange(header["NAXIS1"]) * header["CD1_1"])
            row = fiber - 1  # SDSS fiberid is 1-indexed; plate array rows are 0-indexed
            flux = np.asarray(hdul[0].data[row], dtype=float)
            ivar = np.asarray(hdul[1].data[row], dtype=float)
    except (OSError, IndexError) as exc:
        raise SpectrumUnavailable(f"Could not reach the archive: {exc}") from exc
    uncertainty = _ivar_to_uncertainty(ivar)
    return {
        "wavelength_unit": "Å",
        "flux_unit": FLUX_UNIT_ERG_CM2_S_A,
        "segments": [_segment("SDSS Legacy (pre-BOSS)", wave, flux, uncertainty)],
    }


def _parse_sdss_legacy_optical(holding: dict) -> dict:
    url = holding["archive_url"]
    if "/spPlate-" in url:
        return _parse_sdss_legacy_plate(url)
    return _parse_sdss_spec("SDSS Legacy", holding)


def _parse_mast_jwst(holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        data = hdul["EXTRACT1D"].data
        wave_um = np.asarray(data["WAVELENGTH"], dtype=float)
        flux_jy = np.asarray(data["FLUX"], dtype=float)
        unc_jy = np.asarray(data["FLUX_ERROR"], dtype=float)
    # Confirmed live: WAVELENGTH is in microns and FLUX/FLUX_ERROR in Jy for
    # a real x1d product -- converted to Å / 10^-17 erg/s/cm^2/Å (a real
    # physical conversion, not a guess) so this overlays meaningfully with
    # DESI/SDSS on the same plot instead of sharing an axis in name only.
    wave = wave_um * 1e4
    flux = _jy_to_flambda_1e17(wave, flux_jy)
    uncertainty = _jy_to_flambda_1e17(wave, unc_jy)
    if wave.ndim == 1 and flux.shape == wave.shape == uncertainty.shape:
        segments = [_segment("JWST", wave, flux, uncertainty)]
    elif wave.ndim == 2 and flux.shape == wave.shape == uncertainty.shape:
        # e.g. NIRISS/SOSS: one row per spectral order/source, each a full
        # array (confirmed live: (1, 2048)) -- the old flat-array assumption
        # raised a ValueError, surfacing as an HTTP 500.
        segments = [_segment(f"JWST row {i}", wave[i], flux[i], uncertainty[i]) for i in range(min(len(wave), 40))]
        segments = [s for s in segments if s["wavelength"]]
    else:
        raise SpectrumUnavailable(
            f"This JWST product's EXTRACT1D table has an unsupported shape (wavelength {wave.shape}, flux {flux.shape})."
        )
    if not segments:
        raise SpectrumUnavailable("No usable data in this JWST product.")
    return {"wavelength_unit": "Å", "flux_unit": FLUX_UNIT_ERG_CM2_S_A, "segments": segments}


ESO_FILE_URL = "https://dataportal.eso.org/dataportal_new/file/{dp_id}"

# eso.py's SPECTRUM/WAVE column's real unit varies by instrument -- confirmed
# live across all 17 real eso instruments: HARPS/UVES/FEROS/ESPRESSO/FORS1/
# FORS2/MUSE/KMOS/NIRPS report angstrom (or no TUNIT at all, which every
# checked no-TUNIT sample's numeric range confirmed was already Å) and need
# no conversion, but XSHOOTER/CRIRES/GIRAFFE report nm and SINFONI reports um
# -- a real bug this caught: the parser used to hardcode "Å" and pass WAVE
# through unconverted, so e.g. a real XSHOOTER sample's 298.92-555.98 (nm)
# was mislabeled as 298.92-555.98 Å (off by 10x, landing in the far-UV
# instead of XSHOOTER's real optical/near-IR coverage). APEXHET/EFOSC/SOFI/
# VIMOS have no SPECTRUM extension at all in real samples (raw/imaging
# products, not 1D spectra) -- cleanly rejected below rather than crashing.
_ESO_WAVE_UNIT_TO_ANGSTROM = {
    "angstrom": 1.0,
    "angstroms": 1.0,  # STIS x1d/sx1 spell it this way
    "aa": 1.0,
    "nm": 10.0,
    "um": 1e4,
    "micron": 1e4,
    "microns": 1e4,
}


def _eso_wave_to_angstrom(wave: np.ndarray, unit: str | None) -> np.ndarray:
    if not unit:
        return wave  # no TUNIT metadata -- every real no-TUNIT sample checked was already Å
    factor = _ESO_WAVE_UNIT_TO_ANGSTROM.get(unit.strip().lower())
    if factor is None:
        raise SpectrumUnavailable(f"Unrecognized wavelength unit '{unit}' for this ESO spectrum.")
    return wave * factor


def _parse_eso(holding: dict) -> dict:
    # sync/archives/eso.py stores archive_url as a human landing page
    # (archive.eso.org/dataset/{dp_id}), not a file link -- archive_obs_id
    # is the same dp_id, confirmed live this session to resolve directly to
    # the real FITS file with zero extra API calls.
    raw = _fetch_bytes(ESO_FILE_URL.format(dp_id=holding["archive_obs_id"]))
    with fits.open(io.BytesIO(raw)) as hdul:
        if "SPECTRUM" not in hdul:
            raise SpectrumUnavailable(
                "This ESO product has no SPECTRUM extension (not a 1D extracted spectrum)."
            )
        cols = hdul["SPECTRUM"].data
        col_names = hdul["SPECTRUM"].columns.names
        if "WAVE" not in col_names:
            # apexhet (sub-mm heterodyne) reports FREQ, not WAVE -- a
            # fundamentally different axis convention (confirmed live: real
            # column set was ['FREQ', 'FLUX', 'ERR']), not a units question.
            raise SpectrumUnavailable(
                "This ESO product has no WAVE column (its SPECTRUM extension uses a "
                "different axis convention, e.g. frequency instead of wavelength)."
            )
        wave_unit = hdul["SPECTRUM"].columns["WAVE"].unit
        # Some instruments carry both a raw and a reduced-flux column
        # (FLUX_REDUCED/ERR_REDUCED) per the archive survey; a real sample
        # this session only had plain FLUX/ERR -- prefer the _REDUCED
        # variant when present, fall back otherwise rather than assume
        # either name is always there.
        flux_col = "FLUX_REDUCED" if "FLUX_REDUCED" in col_names else "FLUX"
        err_col = "ERR_REDUCED" if "ERR_REDUCED" in col_names else "ERR"
        wave = np.asarray(cols["WAVE"], dtype=float)
        flux = np.asarray(cols[flux_col], dtype=float)
        uncertainty = np.asarray(cols[err_col], dtype=float)
    # wave/flux are frequently multi-row (one row per order/exposure) rather
    # than one row per pixel -- flatten defensively, same shape risk as
    # lamost's single-row-of-arrays COADD.
    wave = wave.reshape(-1)
    flux = flux.reshape(-1)
    uncertainty = uncertainty.reshape(-1)
    wave = _eso_wave_to_angstrom(wave, wave_unit)
    return {
        "wavelength_unit": "Å",
        "flux_unit": f"ESO pipeline units ({flux_col})",
        "segments": [_segment("ESO", wave, flux, uncertainty)],
    }


def _parse_lamost_mrs(holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    with fits.open(io.BytesIO(raw)) as hdul:
        segments = []
        for label, ext in (("MRS blue", "COADD_B"), ("MRS red", "COADD_R")):
            if ext not in hdul:
                continue
            data = hdul[ext].data
            wave = np.asarray(data["WAVELENGTH"][0], dtype=float)
            flux = np.asarray(data["FLUX"][0], dtype=float)
            ivar = np.asarray(data["IVAR"][0], dtype=float)
            segments.append(_segment(label, wave, flux, _ivar_to_uncertainty(ivar)))
    if not segments:
        raise SpectrumUnavailable("Neither blue nor red MRS coadd extension was present in this file.")
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (pipeline flux units)",
        "segments": segments,
    }


def _parse_elodie(holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        header = hdul["INTENSITY"].header
        crval1, cdelt1, naxis1 = header["CRVAL1"], header["CDELT1"], header["NAXIS1"]
        wave = crval1 + np.arange(naxis1) * cdelt1  # linear, not log -- confirmed live (CTYPE1=AWAV)
        flux = np.asarray(hdul["INTENSITY"].data, dtype=float)
        uncertainty = np.asarray(hdul["NOISE"].data, dtype=float) if "NOISE" in hdul else None
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (pipeline flux units)",
        "segments": [_segment("ELODIE", wave, flux, uncertainty)],
    }


def _parse_irsa_missions(holding: dict) -> dict:
    # irsa_missions bundles 6 unrelated sub-collections behind one
    # archive_code (confirmed live this session) -- only Spitzer/IRS
    # (SASS + Std Stars) has the clean bintable shape handled here.
    # IRTF/MEarth is a bare WCS image with no bintable at all; ISO/SOFIA/
    # IRAS weren't checked and may differ again.
    instrument = holding.get("instrument") or ""
    # Exact labels, not a "Spitzer/IRS" prefix: the FEPS/Disks SH/c2d
    # instruments (added via TAP) are IPAC .tbl/.dat text tables, not this
    # FITS bintable shape.
    if instrument not in ("Spitzer/IRS (SASS)", "Spitzer/IRS (Std Stars)"):
        raise SpectrumUnavailable(
            f"Spectrum display for irsa_missions is only implemented for Spitzer/IRS "
            f"products so far, not {instrument or 'this instrument'}."
        )
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        if len(hdul) < 2:
            # Some IRS_Std files (e.g. kgiant/hd44104.coadd.lo.s19.fits) are a
            # bare 2D detector image in e-/sec, not the bintable variant --
            # confirmed live; previously an IndexError -> HTTP 500.
            raise SpectrumUnavailable("This IRS product is a 2D image, not an extracted 1D spectrum table.")
        data = hdul[1].data  # unnamed extension, confirmed live -- index, not extname
        wave_um = np.asarray(data["WAVELENGTH"], dtype=float)
        flux = np.asarray(data["FLUX"], dtype=float)
        uncertainty = np.asarray(data["ERROR"], dtype=float)
    # Confirmed live: already order-matched into one monotonic sequence
    # (unlike DESI's genuinely disjoint per-camera ranges) -- one segment.
    # Wavelength converted to Å (real unit conversion, ×1e4) so it shares an
    # x-axis with every other archive here -- but FLUX/ERROR carry no unit
    # metadata at all (confirmed live: no TUNIT, no BUNIT), so unlike
    # mast_jwst there's no calibration to convert flux by -- stays arbitrary.
    wave = wave_um * 1e4
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (pipeline flux units)",
        "segments": [_segment("Spitzer/IRS", wave, flux, uncertainty)],
    }


def _wcs_wave(header) -> np.ndarray:
    return header["CRVAL1"] + np.arange(header["NAXIS1"]) * header["CDELT1"]


def _parse_rave(holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        wave = _wcs_wave(hdul["SPECTRUM"].header)
        flux = np.asarray(hdul["SPECTRUM"].data, dtype=float)
        uncertainty = np.asarray(hdul["ERROR"].data, dtype=float) if "ERROR" in hdul else None
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (pipeline flux units)",
        "segments": [_segment("RAVE", wave, flux, uncertainty)],
    }


def _parse_gavo_wcs_image(label: str, flux_unit: str):
    """feros_gavo/flashheros_gavo/ondrejov/heros_ondrejov share one shape:
    a single PRIMARY-HDU image, linear WCS wavelength, no uncertainty
    extension at all (confirmed live -- not a parsing gap, these archives
    just don't carry one)."""

    def parser(holding: dict) -> dict:
        raw = _fetch_bytes(holding["archive_url"])
        with fits.open(io.BytesIO(raw)) as hdul:
            wave = _wcs_wave(hdul[0].header)
            flux = np.asarray(hdul[0].data, dtype=float)
        return {
            "wavelength_unit": "Å",
            "flux_unit": flux_unit,
            "segments": [_segment(label, wave, flux, None)],
        }

    return parser


def _parse_sophie(holding: dict) -> dict:
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        # S1D_B is a second, slightly different-length channel (confirmed
        # live -- not an error array, likely a second fiber) -- S1D_A alone
        # is the real object spectrum.
        wave = _wcs_wave(hdul["S1D_A"].header)
        flux = np.asarray(hdul["S1D_A"].data, dtype=float)
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (pipeline flux units)",
        "segments": [_segment("SOPHIE", wave, flux, None)],
    }


def _parse_hermes_mercator(holding: dict) -> dict:
    # archive_url 301-redirects to a DataLink-served VOTable (confirmed
    # live), not plain FITS -- requests follows redirects by default, but
    # _fetch_bytes' Content-Length check happens on the *first* response,
    # so a redirect chain could dodge the size cap; stream+redirect
    # directly here instead of going through _fetch_bytes.
    deadline = time.monotonic() + TOTAL_DOWNLOAD_DEADLINE_SECONDS
    try:
        with requests.get(holding["archive_url"], stream=True, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise SpectrumUnavailable(f"Spectrum file is too large to display ({int(content_length):,} bytes).")
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if time.monotonic() > deadline:
                    raise SpectrumUnavailable(
                        f"The archive is responding too slowly (over {TOTAL_DOWNLOAD_DEADLINE_SECONDS}s) -- try again later."
                    )
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise SpectrumUnavailable("Spectrum file exceeded the size limit while downloading.")
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise SpectrumUnavailable(f"Could not reach the archive: {exc}") from exc
    raw = b"".join(chunks)

    table = parse_single_table(io.BytesIO(raw)).to_table()
    wave = np.ma.filled(np.asarray(table["spectral"]), np.nan).astype(float)
    flux = np.ma.filled(np.asarray(table["flux"]), np.nan).astype(float)
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (pipeline flux units)",
        "segments": [_segment("HERMES", wave, flux, None)],
    }


# 61 orders (VIS) / 28-56 orders (NIR, varies by archive) x a full-resolution
# MAX_PLOT_POINTS each would be 60,000+ points sent to the browser for one
# spectrum -- keep the per-order budget small so the *total* across all
# orders lands in the same ballpark as a single-segment archive, not 60x it.
_CARMENES_ORDER_MAX_POINTS = 100


def _parse_carmenes_orders(label: str, holding: dict, wave_is_log: bool) -> dict:
    """Shared by carmenes_tac and carmenes_reiners2018 -- same SPEC/SIG/WAVE
    per-order image shape (confirmed live for both: (61, 3699) tac VIS,
    (61, 4096) reiners2018 VIS), but a real convention difference between
    them: tac's WAVE is natural log of vacuum wavelength (confirmed live,
    exp() of a raw ~8.84 gives a sensible ~6895 Å); reiners2018's WAVE is
    already linear Å (confirmed live -- exp() of a raw ~6888 overflows to
    inf, so this is NOT log-scale despite using the same column name).
    Every order gets the SAME label (not "Order N") so the webapp's legend
    collapses all of them into one entry per spectrum instead of 28-61
    near-identical ones -- see the dedup logic in webapp.app's render()."""
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        spec = np.asarray(hdul["SPEC"].data, dtype=float)
        sig = np.asarray(hdul["SIG"].data, dtype=float)
        wave = np.asarray(hdul["WAVE"].data, dtype=float)
    if wave_is_log:
        wave = np.exp(wave)
    # Several of the 61 nominal order slots are genuinely all-zero in a real
    # sample (confirmed live: orders 0-2 and 59-60 of a real tac VIS file --
    # not every physical order has usable data in every reduction) -- skip
    # those before segmenting rather than plot a degenerate flat line at
    # wave=exp(0)=1 Å.
    real_orders = [i for i in range(spec.shape[0]) if not np.all(spec[i] == 0)]
    segments = [
        _segment(label, wave[i], spec[i], sig[i], max_points=_CARMENES_ORDER_MAX_POINTS)
        for i in real_orders
    ]
    segments = [s for s in segments if s["wavelength"]]  # drop orders with no finite pixels at all
    if not segments:
        raise SpectrumUnavailable("No usable data in any echelle order for this file.")
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (pipeline flux units)",
        "segments": segments,
    }


def _parse_carmenes_tac(holding: dict) -> dict:
    label = "CARMENES NIR" if holding.get("instrument") == "CARMENES NIR" else "CARMENES VIS"
    return _parse_carmenes_orders(label, holding, wave_is_log=True)


def _parse_carmenes_reiners2018(holding: dict) -> dict:
    label = "CARMENES NIR" if holding.get("instrument") == "CARMENES NIR" else "CARMENES VIS"
    return _parse_carmenes_orders(label, holding, wave_is_log=False)


def _parse_cfht_cadc(holding: dict) -> dict:
    """cfht_cadc's archive_url is a CADC DataLink resolver, not a direct
    file -- confirmed live this session that its '#this' semantics
    resolves to genuinely different, incompatible product types depending
    on the specific observation, not just the instrument: real SPIRou
    samples came back as a well-structured per-order spectrum (FluxAB/
    WaveAB/BlazeAB, handled below), a CCF-only file (no spectrum at all),
    and once even a raw 402MB 3D detector image cube. Real ESPaDOnS
    samples were worse -- a bare, unlabeled 2D array with no WCS/unit
    metadata at all, and (confirmed live across 2 real samples) an
    *inconsistent* row count (12 vs. 28) between observations, so even
    guessing "row 0 is wavelength" isn't safe without real documentation
    this session didn't have. Rather than guess, this only handles the
    one shape confirmed live to be safe and real (SPIRou's FluxAB/WaveAB/
    BlazeAB) and cleanly rejects everything else -- meaning cfht_cadc
    support here only covers an unpredictable subset of real holdings,
    not "SPIRou" or "ESPaDOnS" as a whole. Measured against 8 real random
    matched holdings each: SPIRou 3/8 usable (the rest were CCF-only or
    over MAX_DOWNLOAD_BYTES), ESPaDOnS 0/8 (every real sample used one of
    the unsupported product types) -- ESPaDOnS support here is real in
    principle but found zero real matches in this sample; don't expect a
    meaningful hit rate for it without further investigation into which
    ESPaDOnS product type (if any) reliably carries a displayable 1D
    spectrum."""
    # archive_url is the DataLink resolver, not a file -- confirmed live
    # this session (same shape as gemini.py/dao.py's CADC archives): its
    # own response is a small VOTable listing this observation's real
    # products, the '#this'-semantics row being the actual science file.
    datalink_raw = _fetch_bytes(holding["archive_url"])
    table = parse_single_table(io.BytesIO(datalink_raw)).to_table()
    this_rows = [i for i in range(len(table)) if str(table["semantics"][i]) == "#this"]
    if not this_rows:
        raise SpectrumUnavailable("This CFHT/CADC observation has no resolvable data product.")
    file_url = str(table["access_url"][this_rows[0]])
    raw = _fetch_bytes(file_url)
    with fits.open(io.BytesIO(raw)) as hdul:
        names = {h.name for h in hdul}
        if not {"FluxAB", "WaveAB"}.issubset(names):
            raise SpectrumUnavailable(
                "This CFHT/CADC product isn't a displayable 1D spectrum -- cfht_cadc resolves to "
                "several incompatible product types per observation (raw detector frames, "
                "cross-correlation-function-only files, ...), and only the FluxAB/WaveAB extracted-"
                "spectrum shape is supported here."
            )
        wave_nm = np.asarray(hdul["WaveAB"].data, dtype=float)
        flux = np.asarray(hdul["FluxAB"].data, dtype=float)
        # BlazeAB is the instrument's blaze/response function -- dividing
        # it out gives the real extracted spectral shape rather than
        # FluxAB's raw per-order hump (confirmed live: FluxAB alone is
        # dominated by the blaze envelope, not real spectral features).
        # No error/uncertainty extension exists in this product at all
        # (confirmed live: no FluxErrAB or similar).
        if "BlazeAB" in names:
            blaze = np.asarray(hdul["BlazeAB"].data, dtype=float)
            with np.errstate(divide="ignore", invalid="ignore"):
                flux = np.where(blaze != 0, flux / blaze, np.nan)
    wave = wave_nm * 10.0  # nm -> Å (confirmed live: WaveAB has no unit header at all, but a real
    # SPIRou sample's values, ~956-2294 nm across orders, match its known near-IR coverage -- Å
    # keeps this consistent with every other archive here rather than introducing a second
    # wavelength-unit convention for just this one).
    segments = [
        _segment("CFHT/SPIRou", wave[i], flux[i], None, max_points=_CARMENES_ORDER_MAX_POINTS)
        for i in range(flux.shape[0])
    ]
    segments = [s for s in segments if s["wavelength"]]
    if not segments:
        raise SpectrumUnavailable("No usable data in any echelle order for this file.")
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (blaze-corrected pipeline flux units)",
        "segments": segments,
    }


def _bin_mean(wave: np.ndarray, flux: np.ndarray, n_bins: int):
    """Bin-averages a very finely sampled spectrum down to n_bins points.
    _downsample's plain stride would alias a spectrum with hundreds of
    thousands of pixels (iacob's HERMES spectra: 335,539 points at
    0.0156 Å) and silently drop narrow lines; averaging keeps the shape."""
    n = len(wave)
    if n <= n_bins:
        return wave, flux
    edges = np.linspace(0, n, n_bins + 1).astype(int)
    with np.errstate(invalid="ignore"):
        w = np.array([np.nanmean(wave[i:j]) for i, j in zip(edges[:-1], edges[1:])])
        f = np.array([np.nanmean(flux[i:j]) for i, j in zip(edges[:-1], edges[1:])])
    return w, f


def _parse_galah(holding: dict) -> dict:
    """GALAH DR4: archive_url is Data Central's slink for FILT=B only (one of
    four HERMES cameras). The same URL with FILT=G/R/I serves the other three
    bands, so all four are fetched (in parallel) for the full ~4700-7900 Å
    coverage. Confirmed live: each is a 4096-pixel, ~40KB 1D primary-HDU
    image, continuum-normalized (median ~1), with NO uncertainty array. The
    WCS is CTYPE=LINEAR with CDELT1=1.0 and the real Å/pixel step in PC1_1
    (0.046-0.074) -- reading CDELT1 alone (as _wcs_wave does) would stretch a
    190 Å band to 4096 Å, so PC1_1*CDELT1 is used. Data Central returns
    sporadic HTTP 500s for individual bands (confirmed live, per band per
    star), so a failed band is skipped, not fatal."""
    base = holding["archive_url"]
    if "FILT=B" not in base:
        raise SpectrumUnavailable("Unexpected GALAH archive_url shape.")

    def fetch_band(band: str):
        try:
            raw = _fetch_bytes(base.replace("FILT=B", f"FILT={band}"))
            with fits.open(io.BytesIO(raw)) as hdul:
                header, data = hdul[0].header, np.asarray(hdul[0].data, dtype=float)
        except (SpectrumUnavailable, OSError, ValueError):
            return None
        step = header.get("PC1_1", 1.0) * header.get("CDELT1", 1.0)
        wave = header["CRVAL1"] + np.arange(len(data)) * step
        return _segment(f"GALAH {band}", wave, data, None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        segments = [s for s in pool.map(fetch_band, ("B", "G", "R", "I")) if s is not None and s["wavelength"]]
    if not segments:
        raise SpectrumUnavailable(
            "Data Central returned no usable band for this GALAH spectrum (it fails intermittently -- try again)."
        )
    return {
        "wavelength_unit": "Å",
        "flux_unit": "continuum-normalized (dimensionless)",
        "segments": segments,
    }


def _parse_iacob(holding: dict) -> dict:
    """IACOB (HERMES/Mercator + FIES/NOT): a single 2 x N primary image, N up
    to ~335k pixels at 0.0156 Å. Confirmed live: row 0 is the continuum-
    normalized flux (~1) and row 1 the un-normalized ADU flux; CRVAL1/CDELT1
    give a linear wavelength axis. Row 0 is shown -- these are OB-star
    spectra used for line profiles. No uncertainty array."""
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        header, data = hdul[0].header, hdul[0].data
        if data is None or data.ndim != 2 or data.shape[0] < 1:
            raise SpectrumUnavailable("Unexpected IACOB file shape (expected a 2 x N image).")
        wave = header["CRVAL1"] + np.arange(data.shape[1]) * header["CDELT1"]
        flux = np.asarray(data[0], dtype=float)
    wave, flux = _bin_mean(wave, flux, 6000)
    return {
        "wavelength_unit": "Å",
        "flux_unit": "continuum-normalized (dimensionless)",
        "segments": [_segment("IACOB", wave, flux, None, max_points=6000)],
    }


_SPITZER_LINK_RE = re.compile(r'href="([^"?/][^"?]*)"')
SPITZER_MAX_FILES_PER_CHANNEL = 12


def _spitzer_list(url: str) -> list[str]:
    return _SPITZER_LINK_RE.findall(_fetch_bytes(url).decode("utf-8", "replace"))


def _parse_spitzer_sha(holding: dict) -> dict:
    """Spitzer Heritage Archive IRS Stare: archive_url is an AOR directory
    (see sync/archives/spitzer_sha.py), not a file. Confirmed live: each
    ch{0,1,2,3}/bcd/ folder holds one extracted 1D SPITZER_S*_spect.fits per
    exposure (bintable ORDER/WAVELENGTH[um]/FLUX_DENSITY[Jy]/ERROR[Jy]/
    BIT_FLAG, ~26KB each). There is no combined per-target 1D product (pbcd/
    only has 2D images), so the exposures are median-combined per (channel,
    order) onto the longest exposure's wavelength grid -- an approximation
    for display, not the SSC's own coadd; the error is the mean per-exposure
    error / sqrt(N). Jy -> F_lambda is a real physical conversion, so these
    overlay with the other erg/s/cm^2/Å archives."""
    base = holding["archive_url"].rstrip("/") + "/"
    channels = [n for n in _spitzer_list(base) if re.fullmatch(r"ch\d/", n)]
    if not channels:
        raise SpectrumUnavailable("No IRS channel folders found for this Spitzer AOR.")

    def list_channel(ch: str):
        try:
            files = [n for n in _spitzer_list(f"{base}{ch}bcd/") if n.endswith("_spect.fits")]
        except SpectrumUnavailable:
            return ch, []
        return ch, [f"{base}{ch}bcd/{n}" for n in sorted(files)[:SPITZER_MAX_FILES_PER_CHANNEL]]

    with ThreadPoolExecutor(max_workers=4) as pool:
        per_channel = list(pool.map(list_channel, channels))

    def load(item):
        ch, url = item
        try:
            with fits.open(io.BytesIO(_fetch_bytes(url))) as hdul:
                d = hdul[1].data
                order = np.asarray(d["ORDER"])
                wave = np.asarray(d["WAVELENGTH"], dtype=float)
                flux = np.asarray(d["FLUX_DENSITY"], dtype=float)
                err = np.asarray(d["ERROR"], dtype=float)
                bad = np.asarray(d["BIT_FLAG"]) != 0
        except (SpectrumUnavailable, OSError, KeyError, ValueError):
            return []
        flux, err = np.where(bad, np.nan, flux), np.where(bad, np.nan, err)
        return [(ch.strip("/"), int(o), wave[order == o], flux[order == o], err[order == o]) for o in np.unique(order)]

    items = [(ch, url) for ch, urls in per_channel for url in urls]
    if not items:
        raise SpectrumUnavailable("No extracted IRS spectra (*_spect.fits) found for this Spitzer AOR.")
    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = [piece for pieces in pool.map(load, items) for piece in pieces]

    groups: dict[tuple[str, int], list] = {}
    for ch, order, wave, flux, err in loaded:
        groups.setdefault((ch, order), []).append((wave, flux, err))

    segments = []
    for (ch, order), members in sorted(groups.items()):
        ref_wave = np.sort(max((m[0] for m in members), key=lambda w: np.ptp(w) if len(w) else 0))
        if len(ref_wave) < 2:
            continue
        fluxes, errs = [], []
        for wave, flux, err in members:
            if len(wave) < 2:
                continue
            o = np.argsort(wave)
            fluxes.append(np.interp(ref_wave, wave[o], flux[o], left=np.nan, right=np.nan))
            errs.append(np.interp(ref_wave, wave[o], err[o], left=np.nan, right=np.nan))
        if not fluxes:
            continue
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns where no exposure overlaps
            n = np.sum(np.isfinite(fluxes), axis=0)
            flux = np.nanmedian(np.array(fluxes), axis=0)
            unc = np.nanmean(np.array(errs), axis=0) / np.sqrt(np.maximum(n, 1))
        wave_a = ref_wave * 1e4  # um -> Å
        flux = _jy_to_flambda_1e17(wave_a, flux)
        unc = _jy_to_flambda_1e17(wave_a, unc)
        seg = _segment(f"IRS {ch} order {order} (median of {len(fluxes)})", wave_a, flux, unc)
        if seg["wavelength"]:
            segments.append(seg)
    if not segments:
        raise SpectrumUnavailable("Every extracted IRS spectrum for this AOR was empty after masking.")
    return {"wavelength_unit": "Å", "flux_unit": FLUX_UNIT_ERG_CM2_S_A, "segments": segments}


def _mast_resolve(holding: dict):
    """Returns (kind, file_url) for a mast holding whose product is a
    displayable 1D spectrum, else None. mast bundles image products (flt/drz/
    c0f/raw/...: ~100k of 440k rows) with spectra, so this gates per row by
    the URL's product suffix, confirmed live against a real sample of each:
      hst_table  -- HST *_x1d / *_x1dsum / *_sx1 (COS/STIS): SCI bintable, one
                    row per segment/order, WAVELENGTH[Å]/FLUX/ERROR
      hasp       -- HASP/HSLA *_cspec / *_aspec coadds: SCI bintable, one row
      wavflux    -- archive.stsci.edu vocontainer *_vo.fits (EUVE/BEFS/WUPPE/
                    HUT) and TUES *_spectrum.fits.gz: WAVE/FLUX (no error)
      iue        -- IUE *.mxhi.gz: MEHI echelle orders, ABS_CAL flux
    HST _asn.fits rows (an association manifest, not a spectrum) are mapped to
    the association's product: STIS -> <root>_x1d.fits, COS -> <root>_x1dsum.fits
    (same rootname)."""
    url = holding.get("archive_url") or ""
    instrument = (holding.get("instrument") or "").upper()
    if re.search(r"_(x1d|x1dsum|sx1)\.fits$", url):
        return "hst_table", url
    if re.search(r"_(cspec|aspec)\.fits$", url):
        return "hasp", url
    if url.endswith("_vo.fits"):
        return "wavflux", url
    if url.endswith("_spectrum.fits.gz"):
        return "tues", url
    if re.search(r"\.mxhi\.gz$", url):
        return "iue", url
    if url.endswith("_asn.fits") and "mast:HST/" in url:
        if instrument.startswith("STIS"):
            return "hst_table", url[: -len("_asn.fits")] + "_x1d.fits"
        if instrument.startswith("COS"):
            return "hst_table", url[: -len("_asn.fits")] + "_x1dsum.fits"
    return None


def _is_erg_flux_unit(unit: str | None) -> bool:
    """True if a table column's TUNIT says erg/s/cm^2/Å (any spelling)."""
    u = (unit or "").lower().replace(" ", "")
    return "erg" in u and ("angstrom" in u or u.endswith("/a") or "aa" in u)


def _mast_table_segments(hdu, labels, wave_names, flux_names, err_names, prefix):
    names = {n.upper(): n for n in hdu.columns.names}
    pick = lambda options: next((names[o] for o in options if o in names), None)
    wcol, fcol, ecol = pick(wave_names), pick(flux_names), pick(err_names)
    if not wcol or not fcol:
        raise SpectrumUnavailable("This product has no wavelength/flux table columns.")
    wave_unit = hdu.columns[wcol].unit
    physical = _is_erg_flux_unit(hdu.columns[fcol].unit)
    scale = 1e17 if physical else 1.0
    data = hdu.data
    segments = []
    for i in range(len(data)):
        wave = _eso_wave_to_angstrom(np.asarray(data[wcol][i], dtype=float).reshape(-1), wave_unit)
        flux = np.asarray(data[fcol][i], dtype=float).reshape(-1) * scale
        unc = np.asarray(data[ecol][i], dtype=float).reshape(-1) * scale if ecol else None
        # COS/STIS x1d pad unused detector regions with zero/negative
        # wavelengths (confirmed live: a COS x1d reported a -28 Å minimum);
        # 50 Å is below every real product handled here (EUVE reaches ~70 Å)
        valid = wave > 50
        wave, flux = wave[valid], flux[valid]
        unc = unc[valid] if unc is not None else None
        seg = _segment(f"{prefix} {labels[i]}".strip(), wave, flux, unc)
        if seg["wavelength"]:
            segments.append(seg)
    return segments, physical


def _parse_mast(holding: dict) -> dict:
    resolved = _mast_resolve(holding)
    if resolved is None:
        raise SpectrumUnavailable(
            "This MAST product isn't a 1D spectrum file (it's an image, raw frame, or "
            "manifest); spectrum display covers HST COS/STIS x1d products, HASP/HSLA "
            "coadds, EUVE/BEFS/WUPPE/HUT/TUES spectra and IUE high-resolution."
        )
    kind, url = resolved
    instrument = holding.get("instrument") or ""
    raw = _fetch_bytes(url)
    if url.endswith(".gz"):
        raw = gzip.decompress(raw)
    with fits.open(io.BytesIO(raw)) as hdul:
        if kind == "hst_table":
            hdu = hdul["SCI"] if "SCI" in hdul else hdul[1]
            cols = hdu.columns.names
            labels = [
                str(hdu.data["SEGMENT"][i]).strip() if "SEGMENT" in cols
                else f"order {hdu.data['SPORDER'][i]}" if "SPORDER" in cols else str(i)
                for i in range(len(hdu.data))
            ]
            segments, physical = _mast_table_segments(
                hdu, labels, ("WAVELENGTH",), ("FLUX",), ("ERROR",), f"HST {instrument}"
            )
        elif kind == "hasp":
            segments, physical = _mast_table_segments(
                hdul["SCI"], ["coadd"], ("WAVELENGTH",), ("FLUX",), ("ERROR",), f"HST {instrument}"
            )
        elif kind == "wavflux":
            segments, physical = _mast_table_segments(
                hdul[1], [""], ("WAVE", "WAVELENGTH"), ("FLUX",), ("ERROR", "ERR", "SIGMA"), instrument
            )
        elif kind == "tues":
            # TUES (ORFEUS echelle): one ORDER_nn table per order; ENERGY_FLUX
            # is erg/cm^2/s/Å and RELATIVE_ERROR a fractional error.
            segments = []
            for hdu in hdul[1:]:
                if not hdu.name.startswith("ORDER_") or hdu.data is None:
                    continue
                flux = np.asarray(hdu.data["ENERGY_FLUX"], dtype=float)
                unc = np.abs(flux) * np.asarray(hdu.data["RELATIVE_ERROR"], dtype=float)
                seg = _segment(
                    f"TUES {hdu.name.replace('_', ' ').lower()}",
                    np.asarray(hdu.data["WAVELENGTH"], dtype=float), flux * 1e17, unc * 1e17,
                )
                if seg["wavelength"]:
                    segments.append(seg)
            physical = True
        else:  # iue
            hdu = hdul["MEHI"] if "MEHI" in hdul else hdul[1]
            segments = []
            for row in hdu.data:
                n = int(row["NPOINTS"])
                wave = float(row["WAVELENGTH"]) + float(row["DELTAW"]) * np.arange(n)
                flux = np.asarray(row["ABS_CAL"], dtype=float)[:n] * 1e17
                # QUALITY != 0 flags saturated/bad/extrapolated pixels
                flux = np.where(np.asarray(row["QUALITY"])[:n] == 0, flux, np.nan)
                seg = _segment(f"IUE {instrument} order {int(row['ORDER'])}", wave, flux, None)
                if seg["wavelength"]:
                    segments.append(seg)
            physical = True
    if not segments:
        raise SpectrumUnavailable("No usable data in this MAST spectrum file.")
    return {
        "wavelength_unit": "Å",
        "flux_unit": FLUX_UNIT_ERG_CM2_S_A if physical else "arbitrary (pipeline flux units)",
        "segments": segments,
    }


def _maybe_decompress(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:3] == b"BZh":
        return bz2.decompress(raw)
    return raw


def _linear_wave(header, n: int) -> np.ndarray:
    crpix = header.get("CRPIX1", 1.0)
    return header["CRVAL1"] + (np.arange(n) + 1 - crpix) * header["CDELT1"]


_NAOJ_HDS_RE = re.compile(r"/PIPE-[\d.]+_1d_nrmwec_[^/]*\.fits$")
_HARPSN_S1D_RE = re.compile(r"_S1D_FLUXCAL_A\.fits\.gz$|_s1d_A\.fits\.gz$")
_HPOL_RE = re.compile(r"_hw\.fits\.gz$")
_SVO_COLLECTIONS = ("miles", "catlib", "stelib", "xshooter", "gbs")
_GHOST_RE = re.compile(r"_calibrated(_ql)?\.fits\.bz2$")


def _parse_naoj(holding: dict) -> dict:
    """Subaru/HDS pipeline product (PIPE-*_1d_nrmwec_*): a continuum-normalized
    1D primary-HDU image with a linear WCS (CRVAL1/CDELT1, ~0.026 Å/px,
    ~50k px), confirmed live; no uncertainty array. naoj's other rows (raw
    .tar bundles, .txt) are not displayable -- see _ROW_GATES."""
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        flux = np.asarray(hdul[0].data, dtype=float).reshape(-1)
        wave = _linear_wave(hdul[0].header, len(flux))
    wave, flux = _bin_mean(wave, flux, 8000)
    return {
        "wavelength_unit": "Å",
        "flux_unit": "continuum-normalized (dimensionless)",
        "segments": [_segment("Subaru/HDS", wave, flux, None, max_points=8000)],
    }


def _parse_harpsn_tng(holding: dict) -> dict:
    """HARPS-N (TNG/IA2). Two real reduced products, both confirmed live:
    *_S1D_FLUXCAL_A.fits.gz (bintable wavelength/flux_cal/error_cal, ~212k
    rows, 4.6MB) and older *_s1d_A.fits.gz (1D 'Relative Flux' image, linear
    Å WCS, ~304k px). Both are bin-averaged to keep line shapes at 8000 pts.
    Raw exposures (no S1D in the name) are excluded by the row gate."""
    raw = _maybe_decompress(_fetch_bytes(holding["archive_url"]))
    with fits.open(io.BytesIO(raw)) as hdul:
        if len(hdul) > 1 and "flux_cal" in (getattr(hdul[1], "columns", None) and hdul[1].columns.names or []):
            d = hdul[1].data
            wave = np.asarray(d["wavelength"], dtype=float)
            flux = np.asarray(d["flux_cal"], dtype=float)
            unc = np.asarray(d["error_cal"], dtype=float)
            unit = "arbitrary (flux-calibrated pipeline units)"
        else:
            flux = np.asarray(hdul[0].data, dtype=float).reshape(-1)
            wave = _linear_wave(hdul[0].header, len(flux))
            unc, unit = None, "relative flux"
    binned_wave, binned_flux = _bin_mean(wave, flux, 8000)
    binned_unc = _bin_mean(wave, unc, 8000)[1] if unc is not None else None
    return {
        "wavelength_unit": "Å",
        "flux_unit": unit,
        "segments": [_segment("HARPS-N", binned_wave, binned_flux, binned_unc, max_points=8000)],
    }


def _parse_hpol(holding: dict) -> dict:
    """HPOL spectropolarimeter (STScI mirror): the 1D flux is the PRIMARY image
    (linear WCS, BUNIT erg/cm^2/s/Å -- a real calibrated flux); Q/U live in a
    POLARIMETRY table and are not plotted. No flux uncertainty."""
    raw = _maybe_decompress(_fetch_bytes(holding["archive_url"]))
    with fits.open(io.BytesIO(raw)) as hdul:
        flux = np.asarray(hdul[0].data, dtype=float).reshape(-1)
        wave = _linear_wave(hdul[0].header, len(flux))
        physical = _is_erg_flux_unit(hdul[0].header.get("BUNIT"))
    if physical:
        flux = flux * 1e17
    return {
        "wavelength_unit": "Å",
        "flux_unit": FLUX_UNIT_ERG_CM2_S_A if physical else "arbitrary (pipeline flux units)",
        "segments": [_segment("HPOL", wave, flux, None)],
    }


def _svo_collection(url: str) -> str | None:
    m = re.search(r"svocats\.cab\.inta-csic\.es/([a-z0-9_]+)/ssap\.php", url or "")
    return m.group(1) if m and m.group(1) in _SVO_COLLECTIONS else None


def _parse_svo_cab(holding: dict) -> dict:
    """SVO Cluster/CAB spectral libraries (one archive_code, 5 collections
    with 3 different shapes, each confirmed live): miles/catlib/stelib are a
    1D (or 1xN) flux image with linear Å WCS; xshooter is natural-log
    wavelength (CRVAL1=ln λ; 2990-10200 Å matches XShooter's coverage) with
    an ERRS extension; gbs is a normalized 600k-px spectrum with a SIGMA
    extension whose axis is in nm (300-900). The XSL sub-collection that once
    returned 'No data found' is not in _SVO_COLLECTIONS. Flux is treated as
    arbitrary even where a BUNIT says erg (library normalizations vary)."""
    coll = _svo_collection(holding["archive_url"])
    if coll is None:
        raise SpectrumUnavailable("This SVO collection isn't supported for display.")
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
        header = hdul[0].header
        flux = np.asarray(hdul[0].data, dtype=float).reshape(-1)
        n = len(flux)
        unc = None
        if coll == "xshooter":
            wave = np.exp(header["CRVAL1"] + np.arange(n) * header["CDELT1"])
            if "ERRS" in hdul:
                unc = np.asarray(hdul["ERRS"].data, dtype=float).reshape(-1)
        elif coll == "gbs":
            wave = _linear_wave(header, n) * 10.0  # nm -> Å
            if "SIGMA" in hdul:
                unc = np.asarray(hdul["SIGMA"].data, dtype=float).reshape(-1)
        else:
            wave = _linear_wave(header, n)
    if n > 8000:
        w, f = _bin_mean(wave, flux, 8000)
        unc = _bin_mean(wave, unc, 8000)[1] if unc is not None else None
        wave, flux = w, f
    return {
        "wavelength_unit": "Å",
        "flux_unit": "arbitrary (library-normalized flux)",
        "segments": [_segment(f"SVO {coll}", wave, flux, unc, max_points=8000)],
    }


def _parse_gemini_ghost(holding: dict) -> dict:
    """Gemini/GHOST reduced (calibrated) echelle, bz2 MEF, confirmed live to
    download anonymously (the audit's earlier auth doubt did not hold): SCI
    (orders, pix, 2), VAR, WAVL (orders, pix). Extensions are read by index
    because 'SCI' also names the primary HDU. The trailing axis of 2 is two
    slit positions; index 0 is the science target (median ~60x brighter than
    index 1 on a real bright star, i.e. the other is sky/secondary) and is
    the one plotted. WAVL is Å despite its BUNIT string (3474-5439 Å blue,
    5209-10610 Å red); flux is W m^-2 nm^-1 (x100 -> erg/s/cm^2/Å), a real
    physical unit."""
    raw = _maybe_decompress(_fetch_bytes(holding["archive_url"]))
    with fits.open(io.BytesIO(raw)) as hdul:
        if ("AWAV", 1) in [(h.name, h.ver) for h in hdul]:
            # Second real layout (confirmed live on prod, e.g. a 2023 blue
            # HD 103295 file): per-slit SCI/VAR/DQ/AWAV extensions (versions
            # 1 and 2), each (orders, pix). SCI is in electrons -- NOT flux
            # calibrated -- and AWAV in nm (347-544 for blue). Slit 1 is
            # plotted (much higher median than slit 2 in the checked file).
            sci, var, awav = hdul["SCI", 1].data, hdul["VAR", 1].data, hdul["AWAV", 1].data
            dq = hdul["DQ", 1].data if ("DQ", 1) in [(h.name, h.ver) for h in hdul] else None
            segments = []
            for i in range(sci.shape[0]):
                flux = np.asarray(sci[i], dtype=float)
                if dq is not None:
                    flux = np.where(np.asarray(dq[i]) == 0, flux, np.nan)
                unc = np.sqrt(np.clip(np.asarray(var[i], dtype=float), 0, None))
                seg = _segment(f"GHOST order {i}", np.asarray(awav[i], dtype=float) * 10.0, flux, unc)
                if seg["wavelength"]:
                    segments.append(seg)
            if not segments:
                raise SpectrumUnavailable("No usable orders in this GHOST file.")
            return {"wavelength_unit": "Å", "flux_unit": "arbitrary (extracted electrons, not flux-calibrated)", "segments": segments}
        sci, var, wavl = hdul[1].data, hdul[2].data, hdul[3].data
    if sci is None or wavl is None or sci.ndim != 3:
        raise SpectrumUnavailable("Unexpected GHOST file layout.")
    segments = []
    for i in range(sci.shape[0]):
        flux = np.asarray(sci[i, :, 0], dtype=float) * 100.0 * 1e17
        unc = np.sqrt(np.clip(np.asarray(var[i, :, 0], dtype=float), 0, None)) * 100.0 * 1e17
        seg = _segment(f"GHOST order {i}", np.asarray(wavl[i], dtype=float), flux, unc)
        if seg["wavelength"]:
            segments.append(seg)
    if not segments:
        raise SpectrumUnavailable("No usable orders in this GHOST file.")
    return {"wavelength_unit": "Å", "flux_unit": FLUX_UNIT_ERG_CM2_S_A, "segments": segments}


# Archives where SUPPORTED_ARCHIVES is necessary but not sufficient: only some
# rows carry a displayable product. Each gate takes the holding dict.
_ROW_GATES = {
    # Instruments whose ESO products have no 1D SPECTRUM extension at all
    # (raw/imaging/heterodyne) -- confirmed live in the spot-check sweep.
    "eso": lambda h: (h.get("instrument") or "") not in ("APEXHET", "EFOSC", "SOFI", "VIMOS"),
    # Only SPIRou has ever produced a displayable product here; the sweep
    # found all 14 other CFHT instruments resolve to non-spectrum products.
    "cfht_cadc": lambda h: (h.get("instrument") or "") == "SPIRou",
    # Only plain _x1d.fits from a spectroscopic mode: the image/target-acq
    # instruments and _x1dints/_c1d/_s2d/_cal products aren't 1D spectra.
    "mast_jwst": lambda h: (h.get("archive_url") or "").endswith("_x1d.fits")
    and not (h.get("instrument") or "").endswith(("IMAGE", "TARGACQ")),
    "mast": lambda h: _mast_resolve(h) is not None,
    "irsa_missions": lambda h: (h.get("instrument") or "") in ("Spitzer/IRS (SASS)", "Spitzer/IRS (Std Stars)"),
    "spitzer_sha": lambda h: (h.get("instrument") or "") == "Spitzer/IRS (Stare)",
    "naoj": lambda h: bool(_NAOJ_HDS_RE.search(h.get("archive_url") or "")),
    "harpsn_tng": lambda h: bool(_HARPSN_S1D_RE.search(h.get("archive_url") or "")),
    "hpol": lambda h: bool(_HPOL_RE.search(h.get("archive_url") or "")),
    "svo_cab": lambda h: _svo_collection(h.get("archive_url") or "") is not None,
    "gemini_ghost": lambda h: bool(_GHOST_RE.search(h.get("archive_url") or "")),
}


def is_spectrum_viewable(holding: dict) -> bool:
    """True if this specific holding can be shown: its archive is supported
    and, for archives that bundle non-spectrum products, the row passes its
    per-row gate."""
    if holding["archive_code"] not in SUPPORTED_ARCHIVES:
        return False
    gate = _ROW_GATES.get(holding["archive_code"])
    return gate(holding) if gate else True


_PARSERS = {
    "lamost": _parse_lamost,
    "gaia_rvs": _parse_gaia_rvs,
    "sdss_v_apogee": _parse_sdss_v_apogee,
    "desi": _parse_desi,
    "sdss_v_optical": _parse_sdss_v_optical,
    "sdss_legacy_optical": _parse_sdss_legacy_optical,
    "mast_jwst": _parse_mast_jwst,
    "eso": _parse_eso,
    "lamost_mrs": _parse_lamost_mrs,
    "elodie": _parse_elodie,
    "irsa_missions": _parse_irsa_missions,
    "rave": _parse_rave,
    "feros_gavo": _parse_gavo_wcs_image("FEROS", "arbitrary (pipeline flux units)"),
    "flashheros_gavo": _parse_gavo_wcs_image("Flash/Heros", "arbitrary (pipeline flux units)"),
    "ondrejov": _parse_gavo_wcs_image("Ondrejov", "ADU (uncalibrated counts)"),
    "heros_ondrejov": _parse_gavo_wcs_image("HEROS (Ondrejov)", "arbitrary (pipeline flux units)"),
    "sophie": _parse_sophie,
    "hermes_mercator": _parse_hermes_mercator,
    "carmenes_tac": _parse_carmenes_tac,
    "carmenes_reiners2018": _parse_carmenes_reiners2018,
    "cfht_cadc": _parse_cfht_cadc,
    "galah": _parse_galah,
    "spitzer_sha": _parse_spitzer_sha,
    "iacob": _parse_iacob,
    "mast": _parse_mast,
    "naoj": _parse_naoj,
    "harpsn_tng": _parse_harpsn_tng,
    "hpol": _parse_hpol,
    "svo_cab": _parse_svo_cab,
    "gemini_ghost": _parse_gemini_ghost,
}


def _apply_continuum_normalization(result: dict) -> None:
    """Replaces each segment's "flux"/"uncertainty" with flux/continuum and
    uncertainty/continuum -- an actual continuum normalization (features as
    deviations around ~1), not an overlay on top of the existing "Scaled
    Flux" values. Fit via mdwarf_contin's alpha-hull + local polynomial
    regression (see webapp/continuum.py), computed AFTER _apply_display_scale
    -- a constant scale factor applied to flux before the fit would cancel
    out in the flux/continuum ratio either way, so the two don't interact;
    keeping the fit after just means there's only one number system in the
    response to reason about.

    Runs per segment, on the already-downsampled wavelength/flux (still
    monotonic within one segment -- see continuum.py's own docstring for why
    that matters and can't be skipped by concatenating segments first).
    One segment's fit failing (too few points, a degenerate alpha shape,
    continuum touching zero, ...) shouldn't take down the rest of an
    otherwise-displayable spectrum -- caught per segment, that segment's
    flux/uncertainty are left as the pre-normalization (median-scaled)
    values rather than raising. result["continuum_normalized"] is only set
    True if at least one segment actually normalized, so the webapp can tell
    "you asked for this and it worked" from "it silently didn't apply".
    """
    result["continuum_normalized"] = False
    for seg in result["segments"]:
        wave = np.array(seg["wavelength"])
        flux = np.array(seg["flux"])
        try:
            continuum = continuum_normalize_segment(wave, flux)
        except Exception:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            normalized_flux = np.where(continuum > 0, flux / continuum, np.nan)
        if not np.any(np.isfinite(normalized_flux)):
            continue
        seg["flux"] = normalized_flux.tolist()
        if seg["uncertainty"] is not None:
            unc = np.array(seg["uncertainty"])
            with np.errstate(divide="ignore", invalid="ignore"):
                seg["uncertainty"] = np.where(continuum > 0, unc / continuum, np.nan).tolist()
        result["continuum_normalized"] = True


def _nan_to_none(result: dict) -> None:
    """Replaces non-finite flux/uncertainty values with None, in place. The
    continuum fit (and any flux/0) leaves NaN behind, and Flask's jsonify
    writes that as a bare `NaN` -- not valid JSON -- so the browser's
    r.json() threw 'Unexpected token N' (confirmed live on TW Cam with the
    continuum toggle on). None serializes to null, which Plotly draws as a
    gap."""
    def clean(values):
        return [v if v is not None and math.isfinite(v) else None for v in values]

    for seg in result["segments"]:
        seg["flux"] = clean(seg["flux"])
        if seg["uncertainty"] is not None:
            seg["uncertainty"] = clean(seg["uncertainty"])


def fetch_spectrum(holding: dict, continuum_normalize: bool = False) -> dict:
    """holding needs at least archive_code, archive_url, archive_obs_id (DESI only).

    Returns {"wavelength_unit": str, "flux_unit": str, "flux_unit_family": str,
    "flux_scale_factor": float, "continuum_normalized": bool, "segments":
    [{"label", "wavelength", "flux", "uncertainty"}, ...]} -- "flux" and
    "uncertainty" here are ALREADY scaled by flux_scale_factor (see
    _apply_display_scale above); flux_unit/flux_unit_family describe the
    *original*, pre-scaling unit, kept for hover text and scientific
    transparency, not for axis labeling -- the webapp always labels the
    shared y-axis "Scaled Flux" regardless of archive UNLESS
    continuum_normalized is True, in which case "flux"/"uncertainty" have
    been further divided by a fitted continuum (see
    _apply_continuum_normalization) -- dimensionless, ~1 baseline, features
    as deviations from it, not the same axis at all. Only happens when
    continuum_normalize=True is passed AND the fit actually succeeded for at
    least one segment. Opt-in, off-by-default: costs real per-request
    compute (~0.1-0.5s/segment, see the PR that added this), unlike the
    median scale factor above which is effectively free.

    flux_unit_family is derived here, centrally, rather than set per-parser
    -- every parser reporting FLUX_UNIT_ERG_CM2_S_A already means "converted
    to (or natively in) that real physical scale", so there's exactly one
    place that needs to agree with the constant, not one per archive.
    """
    archive_code = holding["archive_code"]
    parser = _PARSERS.get(archive_code)
    if parser is None:
        raise SpectrumUnavailable(f"Spectrum display isn't implemented for {archive_code} yet.")
    try:
        result = parser(holding)
    except SpectrumUnavailable:
        raise
    except Exception as exc:
        # Every parser was written against the shapes seen when it was
        # checked; an unseen variant (a JWST product with 2D columns, an IRS
        # file with no table, ...) used to escape as an unhandled exception ->
        # HTTP 500 and an unreadable page. Turn any such failure into the
        # normal "couldn't display this one" message instead, and log it so
        # the shape can be added properly.
        logging.getLogger(__name__).warning(
            "spectrum parse failed for %s holding %s: %r", archive_code, holding.get("archive_obs_id"), exc
        )
        raise SpectrumUnavailable(
            f"Couldn't read this {archive_code} product ({type(exc).__name__}) -- its file layout isn't one this viewer handles yet."
        ) from exc
    result["flux_unit_family"] = (
        FLUX_FAMILY_ERG_CM2_S_A if result["flux_unit"] == FLUX_UNIT_ERG_CM2_S_A else FLUX_FAMILY_ARBITRARY
    )
    result["continuum_normalized"] = False
    _apply_display_scale(result)
    if continuum_normalize:
        _apply_continuum_normalization(result)
    _nan_to_none(result)
    return result
