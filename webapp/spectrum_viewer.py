"""Per-star spectrum fetch + parse, scoped to the 4 archives confirmed live
(this session) to have a real, directly-fetchable reduced-spectrum file with
a known wavelength/flux/uncertainty shape -- see the Spectral Access Ledger
audit. Everything else in spectroscopy_holdings either has no direct file
access, is raw data, or has an unconfirmed/nonstandard format -- deliberately
not wired up here rather than guessing at a shape.

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
"""

from __future__ import annotations

import gzip
import io

import numpy as np
import requests
from astropy.io import fits
from astropy.io.votable import parse_single_table

SUPPORTED_ARCHIVES = {"lamost", "gaia_rvs", "sdss_v_apogee", "desi"}

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # generous for these 4 -- real samples were 60KB-1MB
REQUEST_TIMEOUT_SECONDS = 20
MAX_PLOT_POINTS = 3000  # per segment -- plenty for a legible line plot, well under browser strain


class SpectrumUnavailable(Exception):
    """Raised with a user-facing message -- the route turns this into a page, not a 500."""


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
        "flux_unit": "10⁻¹⁷ erg/s/cm²/Å (DESI flux units)",
        "segments": segments,
    }


_PARSERS = {
    "lamost": _parse_lamost,
    "gaia_rvs": _parse_gaia_rvs,
    "sdss_v_apogee": _parse_sdss_v_apogee,
    "desi": _parse_desi,
}


def fetch_spectrum(holding: dict) -> dict:
    """holding needs at least archive_code, archive_url, archive_obs_id (DESI only).

    Returns {"wavelength_unit": str, "flux_unit": str, "segments": [{"label", "wavelength", "flux", "uncertainty"}, ...]}
    Raises SpectrumUnavailable with a message safe to show a user.
    """
    archive_code = holding["archive_code"]
    parser = _PARSERS.get(archive_code)
    if parser is None:
        raise SpectrumUnavailable(f"Spectrum display isn't implemented for {archive_code} yet.")
    return parser(holding)
