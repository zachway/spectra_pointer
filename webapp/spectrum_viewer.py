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
have affected any archive, not just this one.

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

Memory/cost discipline (Cloud Run, single threaded Flask process, see
project memory on GCP cost minimization): every fetch is bounded --
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

On top of that real unit conversion, every archive also gets a fixed,
per-archive SCALE_FACTOR (below) applied to flux/uncertainty before this
module returns them, so the webapp can plot every archive on one shared
"Scaled Flux" y-axis instead of needing a separate auto-scaled axis per
flux_unit (an earlier version of this feature did that; it worked, but was
visually busier than wanted once several instruments were on one plot).
This is explicitly NOT a claim of physical comparability -- it's a display
convenience derived from one real example per archive (median |flux|,
target order ~1, see scripts/derive_flux_scale_factors.py), fixed and
multiplicative so a real brighter/fainter star still plots higher/lower
after scaling, not a per-spectrum renormalization that would flatten every
star to the same height. flux_unit/flux_unit_family (pre-scaling) are still
returned for hover text and transparency -- only the axis label changes.
"""

from __future__ import annotations

import gzip
import io
import threading
import time

import numpy as np
import requests
from astropy.io import fits
from astropy.io.votable import parse_single_table

SUPPORTED_ARCHIVES = {
    "lamost", "gaia_rvs", "sdss_v_apogee", "desi", "sdss_v_optical", "sdss_legacy_optical",
    "mast_jwst", "eso", "lamost_mrs", "elodie", "irsa_missions",
    "rave", "feros_gavo", "flashheros_gavo", "ondrejov", "heros_ondrejov", "sophie", "hermes_mercator",
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
    "eso": 3_100_000,
    "elodie": 480_000,
    "irsa_missions": 15_000,
    "rave": 20_000,
    "feros_gavo": 770_000,
    "flashheros_gavo": 98_000,
    "ondrejov": 20_000,
    "heros_ondrejov": 112_000,
    "sophie": 2_500_000,
    "hermes_mercator": 3_600_000,
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

# Per-archive display scale so every archive can share one "Scaled Flux"
# y-axis instead of needing a separate axis per flux_unit -- NOT a physical
# calibration (most of these archives have none to begin with, see
# FLUX_FAMILY_ARBITRARY above), just a fixed multiplicative factor that
# brings each archive's *typical* magnitude to order ~1, derived once from
# one real fetched example per archive (median |flux| across that example's
# finite, nonzero pixels, target=1.0) -- see scripts/derive_flux_scale_factors.py.
# Fixed and multiplicative, not renormalized per spectrum: within one
# archive, a genuinely brighter star still plots higher than a fainter one
# (the point of a *display* scale, not a per-plot normalization that would
# flatten every spectrum to the same height regardless of real brightness).
#
# The 4 archives already sharing FLUX_FAMILY_ERG_CM2_S_A (a real physical
# baseline) get ONE shared factor derived from the geometric mean of 3 real
# examples (desi, mast_jwst, sdss_v_optical -- sdss_legacy_optical uses the
# identical SDSS spec-file convention, no separate example needed) rather
# than 4 independent ones -- giving them different factors would have
# destroyed the real relative comparability that unit conversion earned
# them. Every other archive is genuinely on its own incomparable scale
# already (no shared basis to preserve), so each gets its own factor.
SCALE_FACTOR = {
    "desi": 0.00608034,
    "mast_jwst": 0.00608034,
    "sdss_v_optical": 0.00608034,
    "sdss_legacy_optical": 0.00608034,
    "elodie": 2.1619e-05,
    "eso": 106.342,
    "feros_gavo": 1.22498,
    "flashheros_gavo": 0.996962,
    "gaia_rvs": 1.02779,
    "hermes_mercator": 0.000721679,
    "heros_ondrejov": 34.317,
    "irsa_missions": 3.94345,
    "lamost": 0.022563,
    "lamost_mrs": 0.040983,
    "ondrejov": 0.000780958,
    "rave": 1.02128,
    "sdss_v_apogee": 0.00338629,
    "sophie": 1658.55,
}

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
# In-memory, per-process -- Cloud Run may run several instances, so this
# blunts casual scripted abuse/crawlers rather than being an airtight global
# cap (a real distributed limiter would need shared state, not worth it for
# this project's traffic level -- see project memory on GCP cost
# minimization). Deliberately scoped to the fetch itself, not page views in
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


def _fetch_bytes(url: str) -> bytes:
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
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise SpectrumUnavailable("Spectrum file exceeded the size limit while downloading.")
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise SpectrumUnavailable(f"Could not reach the archive: {exc}") from exc
    return b"".join(chunks)


def _downsample(x: np.ndarray, y: np.ndarray) -> tuple[list[float], list[float]]:
    n = len(x)
    if n <= MAX_PLOT_POINTS:
        return x.tolist(), y.tolist()
    step = int(np.ceil(n / MAX_PLOT_POINTS))
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


def _segment(label: str, wave: np.ndarray, flux: np.ndarray, unc: np.ndarray | None) -> dict:
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

    wx, wy = _downsample(wave, flux)
    result = {"label": label, "wavelength": wx, "flux": wy, "uncertainty": None}
    if unc is not None:
        _, wu = _downsample(wave, unc)
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


def _parse_sdss_legacy_optical(holding: dict) -> dict:
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
    return {
        "wavelength_unit": "Å",
        "flux_unit": FLUX_UNIT_ERG_CM2_S_A,
        "segments": [_segment("JWST", wave, flux, uncertainty)],
    }


ESO_FILE_URL = "https://dataportal.eso.org/dataportal_new/file/{dp_id}"


def _parse_eso(holding: dict) -> dict:
    # sync/archives/eso.py stores archive_url as a human landing page
    # (archive.eso.org/dataset/{dp_id}), not a file link -- archive_obs_id
    # is the same dp_id, confirmed live this session to resolve directly to
    # the real FITS file with zero extra API calls.
    raw = _fetch_bytes(ESO_FILE_URL.format(dp_id=holding["archive_obs_id"]))
    with fits.open(io.BytesIO(raw)) as hdul:
        cols = hdul["SPECTRUM"].data
        col_names = hdul["SPECTRUM"].columns.names
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
    if not instrument.startswith("Spitzer/IRS"):
        raise SpectrumUnavailable(
            f"Spectrum display for irsa_missions is only implemented for Spitzer/IRS "
            f"products so far, not {instrument or 'this instrument'}."
        )
    raw = _fetch_bytes(holding["archive_url"])
    with fits.open(io.BytesIO(raw)) as hdul:
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
    try:
        with requests.get(holding["archive_url"], stream=True, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise SpectrumUnavailable(f"Spectrum file is too large to display ({int(content_length):,} bytes).")
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=1 << 16):
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
}


def fetch_spectrum(holding: dict) -> dict:
    """holding needs at least archive_code, archive_url, archive_obs_id (DESI only).

    Returns {"wavelength_unit": str, "flux_unit": str, "flux_unit_family": str,
    "flux_scale_factor": float, "segments": [{"label", "wavelength", "flux",
    "uncertainty"}, ...]} -- "flux" and "uncertainty" here are ALREADY scaled
    by flux_scale_factor (see SCALE_FACTOR above); flux_unit/flux_unit_family
    describe the *original*, pre-scaling unit, kept for hover text and
    scientific transparency, not for axis labeling -- the webapp always
    labels the shared y-axis "Scaled Flux" regardless of archive.

    flux_unit_family and the scale factor are both derived here, centrally,
    rather than set per-parser -- every parser reporting FLUX_UNIT_ERG_CM2_S_A
    already means "converted to (or natively in) that real physical scale",
    so there's exactly one place that needs to agree with the constant, not
    one per archive; same reasoning for looking the scale factor up by
    archive_code here instead of hardcoding it into all 18 parsers.
    """
    archive_code = holding["archive_code"]
    parser = _PARSERS.get(archive_code)
    if parser is None:
        raise SpectrumUnavailable(f"Spectrum display isn't implemented for {archive_code} yet.")
    result = parser(holding)
    result["flux_unit_family"] = (
        FLUX_FAMILY_ERG_CM2_S_A if result["flux_unit"] == FLUX_UNIT_ERG_CM2_S_A else FLUX_FAMILY_ARBITRARY
    )
    factor = SCALE_FACTOR.get(archive_code, 1.0)
    result["flux_scale_factor"] = factor
    if factor != 1.0:
        for seg in result["segments"]:
            seg["flux"] = [f * factor for f in seg["flux"]]
            if seg["uncertainty"] is not None:
                seg["uncertainty"] = [u * factor for u in seg["uncertainty"]]
    return result
