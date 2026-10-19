import pytest

from sync.archives import ing, mast, noirlab
from webapp import instrument_resolving_power, instrument_wavelengths


@pytest.mark.parametrize(
    "collection, instrument, obs_id, label",
    [
        ("IUE", "SWP", "swp00501", "IUE/SWP"),
        ("HUT", "FUV", "hut1", "HUT"),
        ("FUSE", "FUV", "a0010101000", "FUSE"),
        ("EUVE", "DS/S", "zeta_pup__9512201138N", "EUVE/DS/S"),
        ("TUES", "TUES", "tues1008_1", "ORFEUS/TUES"),
        ("BEFS", "BEFS", "befs1001", "ORFEUS/BEFS"),
        ("WUPPE", "ASTRO-1 WUPPE", "wup1", "ASTRO-1 WUPPE"),
        ("HST", "HRS/1", "z06a0101r", "GHRS/1"),
        ("HST", "COS", "hst_hasp_11468_cos_ngc188-41_la8p", "COS (coadd)"),
        ("HST", "COS-STIS", "hst_hsla_wray16-203--0302", "COS+STIS (coadd)"),
        ("HST", "COS/FUV", "la8p01010", "COS/FUV"),
    ],
)
def test_mast_instrument_label(collection, instrument, obs_id, label):
    assert mast.instrument_label(collection, instrument, obs_id) == label


@pytest.mark.parametrize(
    "raw, label",
    [
        ("WHT/ISIS RED ARM", "WHT/ISIS red arm"),
        (" WHT/ISIS blue arm ", "WHT/ISIS blue arm"),
        ("WHT/ISIS", "WHT/ISIS"),
    ],
)
def test_ing_folds_arm_case(raw, label):
    assert ing._instrument(raw) == label


@pytest.mark.parametrize(
    "archive, instrument, label",
    [
        ("LBT Archive", "LUCI", "LBT Archive — LUCI"),
        ("Las Cumbres Observatory — FLOYDS", "FLOYDS", "Las Cumbres Observatory — FLOYDS"),
        ("DESI", "DESI", "DESI"),
        ("SALT HRS (SAAO SSDA)", "HRS", "SALT HRS (SAAO SSDA)"),
        ("IRTF Legacy Archive", "SpeX", "IRTF Legacy Archive — SpeX"),
        # whole-word only: a short instrument mustn't vanish inside a longer word
        ("Gaia RVS", "RV", "Gaia RVS — RV"),
        ("MAST", None, "MAST — —"),
    ],
)
def test_archive_instrument_label(archive, instrument, label):
    from webapp.app import archive_instrument_label

    assert archive_instrument_label(archive, instrument) == label


def test_noirlab_no_longer_pulls_sami_imager():
    assert "sami" not in noirlab.INSTRUMENTS


def test_every_noirlab_label_has_a_resolving_power():
    for label in noirlab.INSTRUMENTS.values():
        assert ("NOIRLab Astro Data Archive", label) in instrument_resolving_power.INSTRUMENT_RESOLVING_POWER
        assert ("NOIRLab Astro Data Archive", label) in instrument_wavelengths.INSTRUMENT_WAVELENGTH_RANGE_NM
