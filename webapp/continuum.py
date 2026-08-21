"""Alpha-hull continuum fitting for a single spectrum segment, via mdwarf_contin.

mdwarf_contin.normalize.ContinuumNormalize's median-filter bin width (`size`)
defaults to 13e-4 dex -- a value tuned specifically to SDSS's fixed pixel
scale (1e-4 dex/pixel, confirmed against a real SDSS COADD), i.e. it is
literally "13 SDSS pixels" as an absolute constant. This project spans dozens
of archives/instruments with very different dispersions (a real CALSPEC
sample's native spacing was ~2.75e-4 dex/pixel, ~4.7x coarser), so the same
absolute constant does not represent the same smoothing scale everywhere.
_resolution_relative_size derives the bin width from each segment's own
median pixel spacing instead, reproducing mdwarf_contin's SDSS default
exactly when fed SDSS data (verified: 13e-4 / 9.9897e-5 = 13.01 pixels).

Only `size` is made resolution-relative here. `alpha`, `radius`, and
`aspect_ratio` remain mdwarf_contin's SDSS-tuned defaults -- a separate,
not-yet-scoped follow-up.

Must be called per segment (one echelle order, one arm/camera), not on a
wavelength array concatenated across segments: a real CARMENES echelle
sample (61-order TAC VIS file) showed 41 backward-stepping wavelength pixels
out of 155,357 when its per-order arrays were flattened in stored order, from
the normal small overlap between adjacent orders. Within a single segment,
wavelength is already monotonic (one spectrograph readout), so no sort is
needed there -- this module relies on that rather than re-sorting itself.
"""
from __future__ import annotations

import numpy as np
from mdwarf_contin.normalize import ContinuumNormalize

DEFAULT_PIXELS_PER_BIN = 13  # reproduces mdwarf_contin's own SDSS-tuned 13e-4 default exactly on SDSS data


def _resolution_relative_size(loglam: np.ndarray, pixels_per_bin: int = DEFAULT_PIXELS_PER_BIN) -> float:
    """Median-filter bin width (dex), derived from this segment's own pixel
    spacing rather than mdwarf_contin's SDSS-only hardcoded 13e-4."""
    diffs = np.diff(np.sort(loglam))
    diffs = diffs[diffs > 0]  # drop duplicate-wavelength pixels (arm overlaps, bad-pixel repeats)
    if len(diffs) == 0:
        raise ValueError("Segment has fewer than 2 distinct wavelength samples; cannot derive a bin size.")
    return pixels_per_bin * float(np.nanmedian(diffs))


def continuum_normalize_segment(
    wave: np.ndarray, flux: np.ndarray, *, pixels_per_bin: int = DEFAULT_PIXELS_PER_BIN
) -> np.ndarray:
    """Fit the continuum of one already-monotonic spectrum segment (one
    echelle order, one arm/camera -- not a multi-segment spectrum
    concatenated together) using mdwarf_contin's alpha-hull + local
    polynomial regression, with the median-filter bin size scaled to this
    segment's own resolution instead of a value tuned for SDSS.

    Returns the continuum evaluated at every input wavelength, in the same
    order as `wave`/`flux` (both may be reordered internally; the input
    arrays are not mutated).
    """
    wave = np.asarray(wave, dtype=float)
    flux = np.asarray(flux, dtype=float)
    finite = np.isfinite(wave) & np.isfinite(flux) & (wave > 0)
    wave, flux = wave[finite], flux[finite]
    order = np.argsort(wave)
    wave, flux = wave[order], flux[order]

    loglam = np.log10(wave)
    size = _resolution_relative_size(loglam, pixels_per_bin)

    cn = ContinuumNormalize(
        loglam, flux, size=size, sigma_clip=True, loglam_range=(loglam.min(), loglam.max())
    )
    cn.find_continuum()

    # cn.continuum is in the sorted/finite-filtered order built above --
    # unsort back to match the caller's original wave/flux order.
    continuum = np.full(len(finite), np.nan)
    continuum[np.flatnonzero(finite)[order]] = cn.continuum
    return continuum
