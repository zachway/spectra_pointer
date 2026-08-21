import numpy as np

from webapp.spectrum_viewer import _apply_continuum_normalization, _apply_display_scale, _segment


def _synthetic_segment(rng):
    n = 2000
    wave = np.linspace(5000, 5500, n)
    true_continuum = 1.0 - 0.1 * (wave - wave.min()) / (wave.max() - wave.min())
    flux = true_continuum - 0.4 * np.exp(-0.5 * ((wave - 5250) / 5) ** 2) + rng.normal(0, 0.003, n)
    unc = np.full(n, 0.01)
    result = {"segments": [_segment("Test", wave, flux, unc)]}
    _apply_display_scale(result)
    return result


def test_continuum_normalization_replaces_flux_not_overlay():
    rng = np.random.default_rng(0)
    result = _synthetic_segment(rng)
    before = list(result["segments"][0]["flux"])

    result["continuum_normalized"] = False
    _apply_continuum_normalization(result)

    assert result["continuum_normalized"] is True
    seg = result["segments"][0]
    assert "continuum" not in seg  # replaced in place, not added as a parallel array
    after = np.array(seg["flux"])
    assert not np.allclose(after, before, equal_nan=True)


def test_continuum_normalization_centers_near_one_away_from_the_line():
    rng = np.random.default_rng(1)
    result = _synthetic_segment(rng)
    _apply_continuum_normalization(result)
    seg = result["segments"][0]
    wave = np.array(seg["wavelength"])
    flux = np.array(seg["flux"])
    line_free = np.abs(wave - 5250) > 50
    assert abs(np.nanmedian(flux[line_free]) - 1.0) < 0.05


def test_continuum_normalization_propagates_to_uncertainty():
    rng = np.random.default_rng(2)
    result = _synthetic_segment(rng)
    seg = result["segments"][0]
    before_unc = list(seg["uncertainty"])
    _apply_continuum_normalization(result)
    after_unc = np.array(seg["uncertainty"])
    assert not np.allclose(after_unc, before_unc, equal_nan=True)


def test_continuum_normalization_off_by_default_flag():
    rng = np.random.default_rng(3)
    result = _synthetic_segment(rng)
    result["continuum_normalized"] = False
    assert result["continuum_normalized"] is False
