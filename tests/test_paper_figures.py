"""Star-name chooser and archive-family mapping used by scripts.paper_figures."""

import pytest

from scripts.paper_figures.common import ARCHIVE_FAMILY, LABEL_OVERRIDES, star_label


@pytest.mark.parametrize(
    "aliases, expected",
    [
        # SIMBAD common name wins over everything else.
        (["GJ 559 B", "* alf Cen B", "NAME Toliman", "HD 128621"], "Toliman"),
        # Greek Bayer designations, SIMBAD form and raw target-name form.
        (["HIP 27321", "* bet Pic", "HD 39060"], r"$\beta$ Pic"),
        (["HR 4210", "Eta Car"], r"$\eta$ Car"),
        (["TAU BOO", "HR5185"], r"$\tau$ Boo"),
        (["* alf02 Cen"], r"$\alpha$$^{2}$ Cen"),
        # An all-caps NAME is usually a raw target name: Bayer outranks it.
        (["NAME ALCAID", "* eta UMa"], r"$\eta$ UMa"),
        (["NAME ALCAID", "HD 120315"], "Alcaid"),
        # Latin-letter Bayer and Flamsteed.
        (["HD 125823", "* a Cen"], "a Cen"),
        (["TOI-1773", "*  55 Cnc", "HD 75732"], "55 Cnc"),
        # Variable-star names, then exoplanet-survey hosts, then catalogues.
        (["gj803", "V* AU Mic", "HD 197481"], "AU Mic"),
        (["HD 15082", "WASP-033", "wasp33"], "WASP-33"),
        (["HIP108859", "HD         209458"], "HD 209458"),
        (["HD 00060753", "HD_60753"], "HD 60753"),
        (["bd 284211", "BD+ 28 4211"], "BD+28 4211"),
    ],
)
def test_star_label(aliases, expected):
    assert star_label(aliases) == expected


def test_star_label_plain_text_and_fallbacks():
    assert star_label(["* bet Pic"], mathtext=False) == "beta Pic"
    assert star_label(None, gaia_source_id=123) == "Gaia DR3 123"
    assert star_label([], input_name="Some Target") == "Some Target"


def test_label_override_wins():
    sid, label = next(iter(LABEL_OVERRIDES.items()))
    assert star_label(["NAME Something Else"], star_id=sid) == label


def test_archive_family_targets_are_not_themselves_remapped():
    # A family name that is also a remapped code would be ambiguous.
    assert not set(ARCHIVE_FAMILY.values()) & set(ARCHIVE_FAMILY)
