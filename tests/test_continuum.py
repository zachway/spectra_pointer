import numpy as np
import pytest

from webapp import continuum


def test_resolution_relative_size_matches_sdss_default():
    # SDSS COADD dispersion is a fixed 1e-4 dex/pixel; mdwarf_contin's own
    # SDSS-tuned default is size=13e-4, i.e. exactly 13 of those pixels.
    loglam = np.arange(3.6, 4.0, 1e-4)
    size = continuum._resolution_relative_size(loglam)
    assert size == pytest.approx(13e-4, rel=1e-6)


def test_resolution_relative_size_scales_with_dispersion():
    coarse = np.arange(3.6, 4.0, 5e-4)
    size = continuum._resolution_relative_size(coarse)
    assert size == pytest.approx(13 * 5e-4, rel=1e-6)


def test_resolution_relative_size_rejects_degenerate_input():
    with pytest.raises(ValueError):
        continuum._resolution_relative_size(np.array([3.7, 3.7, 3.7]))


def test_continuum_normalize_segment_order_independent():
    rng = np.random.default_rng(0)
    n = 1500
    wave = np.linspace(5000, 5500, n)
    flux = 1.0 - 0.3 * np.exp(-0.5 * ((wave - 5250) / 5) ** 2) + rng.normal(0, 0.005, n)

    fit_sorted = continuum.continuum_normalize_segment(wave, flux)

    perm = rng.permutation(n)
    fit_shuffled = continuum.continuum_normalize_segment(wave[perm], flux[perm])
    resort = np.argsort(perm)

    assert np.allclose(fit_shuffled[resort], fit_sorted)


def test_continuum_normalize_segment_ignores_absorption_line():
    rng = np.random.default_rng(1)
    n = 2000
    wave = np.linspace(5000, 5500, n)
    true_continuum = np.ones(n)
    flux = true_continuum - 0.4 * np.exp(-0.5 * ((wave - 5250) / 5) ** 2) + rng.normal(0, 0.003, n)

    fit = continuum.continuum_normalize_segment(wave, flux)

    line_free = np.abs(wave - 5250) > 50
    assert np.nanmedian(np.abs(fit[line_free] - true_continuum[line_free])) < 0.01

    line_core = np.abs(wave - 5250) < 3
    assert np.nanmedian(fit[line_core]) > 0.9  # fit should not collapse into the line


def test_short_segment_fits_instead_of_failing_on_an_empty_alpha_shape():
    """Real Spitzer/IRS orders (60-110 pixels) used to raise mdwarf_contin's
    'Something has gone terribly wrong with the alpha_shape': 13 px/bin left
    ~5 binned points, too sparse for any alpha-shape triangle. The bin size
    is now capped by segment length."""
    rng = np.random.default_rng(1)
    wave = np.linspace(5.2e4, 7.5e4, 86)  # ~ an IRS order, in Å
    flux = 3.0 - 1.0 * (wave - wave.min()) / (wave.max() - wave.min()) + rng.normal(0, 0.03, wave.size)
    fit = continuum.continuum_normalize_segment(wave, flux)
    assert np.all(np.isfinite(fit)) and np.all(fit > 0)
    assert 0.7 < np.nanmedian(flux / fit) < 1.1


def test_long_segment_bin_size_is_unchanged():
    """Segments of >= ~195 pixels keep the requested 13 px/bin exactly, so
    SDSS/DESI/LAMOST-sized spectra are unaffected by the short-segment fix."""
    from mdwarf_contin.normalize import ContinuumNormalize

    rng = np.random.default_rng(0)
    wave = np.linspace(4000, 9000, 3800)
    flux = 100 + 20 * np.sin(wave / 700) + rng.normal(0, 2, wave.size)
    new = continuum.continuum_normalize_segment(wave, flux)
    loglam = np.log10(wave)
    size = continuum.DEFAULT_PIXELS_PER_BIN * float(np.nanmedian(np.diff(loglam)))
    cn = ContinuumNormalize(loglam, flux, size=size, sigma_clip=True, loglam_range=(loglam.min(), loglam.max()))
    cn.find_continuum()
    assert np.allclose(new, cn.continuum, equal_nan=True)
