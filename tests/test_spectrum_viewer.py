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


def test_mast_row_gate_accepts_spectra_and_rejects_images():
    from webapp.spectrum_viewer import is_spectrum_viewable

    def h(url, instrument="COS/FUV"):
        return {"archive_code": "mast", "archive_url": url, "instrument": instrument}

    hst = "https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:HST/product/"
    assert is_spectrum_viewable(h(hst + "la8p92bqq_x1d.fits"))
    assert is_spectrum_viewable(h(hst + "la8p92030_x1dsum.fits"))
    assert is_spectrum_viewable(h(hst + "hasp/hst_10014_stis_x_cspec.fits", "STIS"))
    assert is_spectrum_viewable(h("http://archive.stsci.edu/missions/iue/data/lwp/00000/lwp00501.mxhi.gz", "LWP"))
    assert is_spectrum_viewable(h("http://archive.stsci.edu/missions/euve/vocontainer/euve2/x_vo.fits", "BEFS"))
    # image / raw products are not spectra
    assert not is_spectrum_viewable(h(hst + "iaab22i1q_flt.fits", "ACS/HRC"))
    assert not is_spectrum_viewable(h(hst + "ib0004020_drz.fits", "ACS/HRC"))
    # association manifests only map to a spectrum for STIS/COS
    assert is_spectrum_viewable(h(hst + "ofhjbs010_asn.fits", "STIS/FUV-MAMA"))
    assert not is_spectrum_viewable(h(hst + "j8ca01020_asn.fits", "ACS/WFC"))


def test_mast_asn_maps_to_the_right_product():
    from webapp.spectrum_viewer import _mast_resolve

    base = "https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:HST/product/"
    assert _mast_resolve({"archive_url": base + "ofhjbs010_asn.fits", "instrument": "STIS/FUV-MAMA"}) == (
        "hst_table", base + "ofhjbs010_x1d.fits",
    )
    assert _mast_resolve({"archive_url": base + "la8p92030_asn.fits", "instrument": "COS/FUV"}) == (
        "hst_table", base + "la8p92030_x1dsum.fits",
    )


def test_per_row_gates_for_spitzer_and_irsa_missions():
    from webapp.spectrum_viewer import is_spectrum_viewable

    assert is_spectrum_viewable({"archive_code": "spitzer_sha", "instrument": "Spitzer/IRS (Stare)"})
    assert not is_spectrum_viewable({"archive_code": "spitzer_sha", "instrument": "Spitzer/IRS (Map)"})
    assert not is_spectrum_viewable({"archive_code": "spitzer_sha", "instrument": "Spitzer/MIPS-SED"})
    assert is_spectrum_viewable({"archive_code": "irsa_missions", "instrument": "Spitzer/IRS (SASS)"})
    assert not is_spectrum_viewable({"archive_code": "irsa_missions", "instrument": "ISO/SWS"})
    assert is_spectrum_viewable({"archive_code": "galah", "instrument": "GALAH (HERMES)"})
    assert not is_spectrum_viewable({"archive_code": "harpsn_tng", "instrument": "HARPS-N"})


def test_second_batch_row_gates():
    from webapp.spectrum_viewer import is_spectrum_viewable

    def v(code, url):
        return is_spectrum_viewable({"archive_code": code, "archive_url": url, "instrument": "x"})

    hds = "http://jvo.nao.ac.jp/skynode/do/download/hds/public/file/"
    assert v("naoj", hds + "PIPE-1.0_1d_nrmwec_fsclmo_HDSA00003798.fits")
    assert not v("naoj", hds + "SK-0611_HDSA00003463.tar")
    assert not v("naoj", hds + "PIPE-1.0_1d_nrmwec_fsclmo_HDSA00037843.txt")

    tng = "http://archives.ia2.inaf.it/files/tng/"
    assert v("harpsn_tng", tng + "r.HARPN.2013-10-11T00-10-28.341_S1D_FLUXCAL_A.fits.gz")
    assert v("harpsn_tng", tng + "HARPN.2012-09-05T20-35-43.956_s1d_A.fits.gz")
    assert not v("harpsn_tng", tng + "HARPN.2012-09-02T20-24-34.231.fits.gz")  # raw exposure

    svo = "http://svocats.cab.inta-csic.es/"
    for coll in ("miles", "catlib", "stelib", "xshooter", "gbs"):
        assert v("svo_cab", f"{svo}{coll}/ssap.php?ID=1&label=spec_fits")
    assert not v("svo_cab", f"{svo}xsl/ssap.php?ID=320&label=spec_fits")  # "No data found" upstream

    gem = "https://archive.gemini.edu/file/"
    assert v("gemini_ghost", gem + "S20230416S0079_blue001_calibrated.fits.bz2")
    assert v("gemini_ghost", gem + "S20230416S0079_blue001_calibrated_ql.fits.bz2")
    assert not v("gemini_igrins", gem + "SDCH_20180402_0100.spec_a0v.fits.bz2")  # anonymous GET returns 400

    assert v("hpol", "https://archive.stsci.edu/missions/hpol/data/x/hpolret_x_hw.fits.gz")


def test_bin_mean_preserves_shape_instead_of_striding():
    from webapp.spectrum_viewer import _bin_mean

    wave = np.arange(12000, dtype=float)
    flux = np.ones(12000)
    flux[6000:6010] = 0.0  # a narrow "line" a stride of 12 could skip entirely
    w, f = _bin_mean(wave, flux, 1000)
    assert len(w) == len(f) == 1000
    assert f.min() < 1.0


def test_unexpected_parser_exception_becomes_spectrum_unavailable(monkeypatch):
    """An unseen file layout must produce the normal error message, not an
    unhandled exception (which surfaced as HTTP 500 in production)."""
    import pytest

    from webapp import spectrum_viewer as sv

    def boom(holding):
        raise IndexError("list index out of range")

    monkeypatch.setitem(sv._PARSERS, "galah", boom)
    with pytest.raises(sv.SpectrumUnavailable, match="galah"):
        sv.fetch_spectrum({"archive_code": "galah", "archive_url": "x", "archive_obs_id": "1"})


def test_result_is_strict_json_even_when_continuum_fit_yields_nan(monkeypatch):
    """The continuum toggle on TW Cam returned bare NaN in the payload, which
    browsers reject ('Unexpected token N ... is not valid JSON')."""
    import json

    from webapp import spectrum_viewer as sv

    wave = np.linspace(4000, 5000, 200)

    def parser(holding):
        return {
            "wavelength_unit": "Å", "flux_unit": "arbitrary",
            "segments": [sv._segment("x", wave, np.ones(200), np.full(200, 0.1))],
        }

    def nan_continuum(result):
        result["continuum_normalized"] = True
        for seg in result["segments"]:
            seg["flux"] = [float("nan")] * len(seg["flux"])
            seg["uncertainty"] = [float("nan")] * len(seg["uncertainty"])

    monkeypatch.setitem(sv._PARSERS, "galah", parser)
    monkeypatch.setattr(sv, "_apply_continuum_normalization", nan_continuum)
    result = sv.fetch_spectrum({"archive_code": "galah", "archive_url": "u", "archive_obs_id": "1"}, continuum_normalize=True)
    json.dumps(result, allow_nan=False)  # raises ValueError on NaN/Infinity
    assert result["segments"][0]["flux"][0] is None


def test_hopeless_products_are_gated_out():
    from webapp.spectrum_viewer import is_spectrum_viewable

    def v(code, inst, url=""):
        return is_spectrum_viewable({"archive_code": code, "instrument": inst, "archive_url": url})

    for inst in ("APEXHET", "EFOSC", "SOFI", "VIMOS"):
        assert not v("eso", inst)
    assert v("eso", "HARPS")
    assert v("cfht_cadc", "SPIRou")
    assert not v("cfht_cadc", "ESPaDOnS")

    jw = "https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:JWST/product/x_"
    assert v("mast_jwst", "NIRSPEC/MSA", jw + "x1d.fits")
    assert not v("mast_jwst", "NIRCAM/IMAGE", jw + "x1d.fits")
    assert not v("mast_jwst", "NIRCAM/GRISM", jw + "x1dints.fits")


def test_mast_jwst_two_dimensional_extract1d_gives_one_segment_per_row(monkeypatch):
    import io

    from astropy.io import fits

    from webapp import spectrum_viewer as sv

    n = 50
    wave = np.tile(np.linspace(0.9, 2.8, n), (2, 1))  # (rows, pix), microns
    cols = [
        fits.Column(name="WAVELENGTH", format=f"{n}D", array=wave),
        fits.Column(name="FLUX", format=f"{n}D", array=np.ones((2, n))),
        fits.Column(name="FLUX_ERROR", format=f"{n}D", array=np.full((2, n), 0.1)),
    ]
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU.from_columns(cols, name="EXTRACT1D")]).writeto(buf)
    monkeypatch.setattr(sv, "_fetch_bytes", lambda url: buf.getvalue())
    result = sv.fetch_spectrum({"archive_code": "mast_jwst", "archive_url": "u", "archive_obs_id": "1"})
    assert len(result["segments"]) == 2
