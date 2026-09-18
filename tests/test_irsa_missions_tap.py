import pytest
from astropy.table import Table

from sync.archives import irsa_missions
from webapp import instrument_wavelengths
from webapp.spectrum_viewer import SpectrumUnavailable, _parse_irsa_missions

ARCHIVE_NAME = "IRSA Space-Mission Stellar Collections"


class _FakeResult:
    def __init__(self, table):
        self._table = table

    def to_table(self):
        return self._table


class _FakeService:
    def __init__(self, tables):
        self._tables = tables

    def run_sync(self, query):
        return _FakeResult(self._tables[query.split("FROM ")[1].strip()])


def _fake_tables():
    return {
        "spitzer.feps_spectra_v5": Table(
            {
                "name": ["HD 105", "HD 377"],
                "ra": [1.468973, 2.107247],
                "dec": [-41.75304, 6.616805],
                "irs_lo_tbl_u": ["spectra/IRS_lo_V5/HD_105_combined_spectrum.tbl", "spectra/IRS_lo_V5/HD_377_combined_spectrum.tbl"],
                "irs_hi_dat_u": ["none", "spectra/IRS_hi_V5/HD_377.dat"],
            }
        ),
        "spitzer.disks_sh_spectra": Table(
            {"object": ["1RXS_J121236.4-552037"], "ra": [183.149], "dec": [-55.341], "spectrum_tbl_u": ["spectra/1RXS_J121236.4-552037_SH.tbl"]}
        ),
        "spitzer.c2d_irs_spec": Table(
            {
                "ra": [84.87717],
                "dec": [26.37417],
                "file_name": ["./spectra/IRS_data/IRS_POINTED/RR_Tau/SPITZER_IRS_0005638400_RR_Tau.tbl"],
                "tbl_u": ["./spectra/IRS_data/IRS_POINTED/RR_Tau/SPITZER_IRS_0005638400_RR_Tau.tbl"],
            }
        ),
    }


@pytest.fixture
def records(monkeypatch):
    monkeypatch.setattr(irsa_missions, "make_tap_service", lambda url: _FakeService(_fake_tables()))
    return irsa_missions._fetch_tap_spectra()


def test_tap_records_names_urls_and_no_fabricated_date(records):
    by_url = {r.archive_url: r for r in records}
    feps = by_url[f"{irsa_missions.SPITZER_DATA_URL}/FEPS/spectra/IRS_lo_V5/HD_105_combined_spectrum.tbl"]
    assert feps.raw_target_name == "HD 105"
    assert feps.instrument == "Spitzer/IRS (FEPS)"
    assert all(r.obs_date is None for r in records)  # no real epoch in these tables
    assert all(r.archive_obs_id == r.archive_url for r in records)

    # c2d: name recovered from the file's parent directory, "./" stripped.
    c2d = by_url[f"{irsa_missions.SPITZER_DATA_URL}/C2D/spectra/IRS_data/IRS_POINTED/RR_Tau/SPITZER_IRS_0005638400_RR_Tau.tbl"]
    assert c2d.raw_target_name == "RR Tau"
    assert by_url[f"{irsa_missions.SPITZER_DATA_URL}/Disks_SH_spectra/spectra/1RXS_J121236.4-552037_SH.tbl"].raw_target_name == "1RXS J121236.4-552037"


def test_feps_none_high_res_is_skipped_but_real_one_kept(records):
    hi = [r for r in records if r.instrument == "Spitzer/IRS (FEPS high-res)"]
    assert [r.raw_target_name for r in hi] == ["HD 377"]
    assert len(records) == 5  # 2 lo + 1 hi (HD 105's "none" dropped) + 1 disks + 1 c2d


def test_fetch_runs_tap_once_then_continues_existing_cursor(monkeypatch):
    monkeypatch.setattr(irsa_missions, "make_tap_service", lambda url: _FakeService(_fake_tables()))
    # An existing cursor from before the TAP tables existed: gains tap_done,
    # keeps its whole-sky/grid progress.
    recs, cursor = irsa_missions.fetch({"whole_sky_done": True, "grid_index": 7})
    assert len(recs) == 5
    assert cursor == {"whole_sky_done": True, "grid_index": 7, "tap_done": True}

    # A finished grid stays a no-op forever after.
    done = {"tap_done": True, "whole_sky_done": True, "grid_index": len(irsa_missions.GRID_TASKS)}
    assert irsa_missions.fetch(done) == ([], done)


def test_every_tap_instrument_has_a_wavelength_range():
    for meta in irsa_missions.TAP_TABLES.values():
        for instrument in meta["files"].values():
            assert (ARCHIVE_NAME, instrument) in instrument_wavelengths.INSTRUMENT_WAVELENGTH_RANGE_NM


@pytest.mark.parametrize("instrument", ["Spitzer/IRS (FEPS)", "Spitzer/IRS (FEPS high-res)", "Spitzer/IRS (Disks SH)", "Spitzer/IRS (c2d)"])
def test_viewer_rejects_tbl_instruments_instead_of_parsing_as_fits(instrument):
    with pytest.raises(SpectrumUnavailable):
        _parse_irsa_missions({"instrument": instrument, "archive_url": "unused"})
