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
