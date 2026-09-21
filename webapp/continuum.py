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
SHORT_SEGMENT_PIXELS = 400  # below this, fit with finer bins and sanity-check (see continuum_normalize_segment)
PIXELS_PER_BIN_DIVISOR = 40  # short segments start at n // 40 px/bin: 86 px -> 2, 107 px -> 2, 62 px -> 1


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

    # mdwarf_contin builds an alpha shape (alpha=1/0.05, in coordinates
    # normalized to the unit square) from the median-binned points. With
    # 13 pixels per bin a SHORT segment collapses to a handful of points --
    # too sparse for any triangle at that radius -- so the shape is empty
    # and mdwarf_contin raises "Something has gone terribly wrong with the
    # alpha_shape". Confirmed on real Spitzer/IRS orders (60-110 pixels
    # each): 0/20 fit at 13 px/bin. Even where a coarse fit succeeds, its
    # local regression extrapolates wildly past the last bin (a synthetic
    # 86-pixel spectrum's continuum ran to -14 against a flux of ~2), so
    # short segments start at a fine bin size scaled to their length and
    # step down on failure OR on an implausible result; measured on the real
    # orders and synthetic ones, <=2 px/bin gave zero runaway points where
    # 4-6 px/bin did not. Segments of >= SHORT_SEGMENT_PIXELS pixels --
    # every SDSS/DESI/LAMOST-sized spectrum -- take exactly the requested
    # size and are never second-guessed, so their output is unchanged.
    n = len(wave)
    short = n < SHORT_SEGMENT_PIXELS
    start = min(pixels_per_bin, max(1, n // PIXELS_PER_BIN_DIVISOR)) if short else pixels_per_bin
    ladder = sorted({p for p in (start, 8, 6, 4, 3, 2, 1) if p <= start}, reverse=True)
    last_error: Exception | None = None
    for ppb in ladder:
        try:
            size = _resolution_relative_size(loglam, ppb)
            cn = ContinuumNormalize(
                loglam, flux, size=size, sigma_clip=True, loglam_range=(loglam.min(), loglam.max())
            )
            cn.find_continuum()
        except ValueError as exc:
            last_error = exc
            continue
        if short and ppb != ladder[-1] and _implausible(cn.continuum, flux):
            last_error = ValueError(f"continuum ran away at {ppb} px/bin")
            continue
        # cn.continuum is in the sorted/finite-filtered order built above --
        # unsort back to match the caller's original wave/flux order.
        continuum = np.full(len(finite), np.nan)
        continuum[np.flatnonzero(finite)[order]] = cn.continuum
        return continuum
    raise ValueError(f"continuum fit failed at every bin size tried ({ladder}): {last_error}")


def _implausible(continuum: np.ndarray, flux: np.ndarray) -> bool:
    """True if a fitted continuum has runaway points: non-positive, or more
    than 3x / less than 0.3x the spectrum's median |flux| for more than 2% of
    pixels. Only used to reject coarse fits of short segments (see above)."""
    ok = np.isfinite(continuum) & np.isfinite(flux)
    if not ok.any():
        return True
    med = float(np.nanmedian(np.abs(flux[ok])))
    c = continuum[ok]
    bad = (c <= 0) | (c > 3 * med) | (c < 0.3 * med)
    return bool(bad.mean() > 0.02)
