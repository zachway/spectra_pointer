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
    done = {
        "tap_done": True,
        "whole_sky_done": True,
        "whole_sky_collections": sorted(irsa_missions.WHOLE_SKY_COLLECTIONS),
        "brava_done": True,
        "grid_index": len(irsa_missions.GRID_TASKS),
    }
    assert irsa_missions.fetch(done) == ([], done)


def test_pre_flitecam_cursor_pulls_only_the_new_whole_sky_collections(monkeypatch):
    pulled = []
    monkeypatch.setattr(irsa_missions, "_fetch_whole_sky", lambda names: pulled.extend(names) or ["obs"])
    old = {"tap_done": True, "whole_sky_done": True, "grid_index": len(irsa_missions.GRID_TASKS)}
    recs, cursor = irsa_missions.fetch(old)
    assert recs == ["obs"]
    assert pulled == ["sofia_flitecam", "iso_sws_atlas", "herschel_hifistars"]
    # Nothing the old cursor carried is dropped.
    assert cursor == {**old, "whole_sky_collections": sorted(irsa_missions.WHOLE_SKY_COLLECTIONS)}

    pulled.clear()
    irsa_missions.fetch({"tap_done": True})
    assert pulled == list(irsa_missions.WHOLE_SKY_COLLECTIONS)


def test_whole_sky_resolving_power_floor_drops_flitecam_filter_images(monkeypatch):
    def row(resolving_power, url):
        r = {f"col_{i}": "" for i in range(39)}
        r.update(col_0=10.0, col_1=-5.0, col_5=2, col_11=resolving_power, col_18=url, col_19="image/fits",
                 col_27="HD 1", col_31=57676.25)
        return r

    monkeypatch.setattr(irsa_missions, "_query", lambda collection, pos, size: [row(6.0, "k_band"), row(1300.0, "grism")])
    recs = irsa_missions._fetch_whole_sky(["sofia_flitecam"])
    assert [r.archive_obs_id for r in recs] == ["grism"]
    assert recs[0].instrument == "SOFIA/FLITECAM" and recs[0].obs_date.isoformat() == "2016-10-15"

    # HIFISTARS: blank target_name, so the star's directory supplies it.
    url = "https://irsa.ipac.caltech.edu/data/Herschel/HIFISTARS/spectra/IRAS_15194_5115/1342202052-final-LSB.new.fits"
    hifi = row(1e7, url)
    hifi.update(col_19="application/fits", col_27="", col_8="ivo://irsa.ipac/herschel_hifistars/1342202052-final-LSB")
    monkeypatch.setattr(irsa_missions, "_query", lambda collection, pos, size: [hifi])
    assert irsa_missions._fetch_whole_sky(["herschel_hifistars"])[0].raw_target_name == "IRAS 15194 5115"


class _BravaService:
    def __init__(self, table):
        self._table = table
        self.queries = []

    def run_sync(self, query):
        self.queries.append(query)
        return _FakeResult(self._table)


def _brava_table():
    return Table(
        {
            "cntr": [3787, 3790],
            "ra": [279.4655, 265.6659],
            "dec": [-15.0634, -38.0159],
            "fits_spectra_1": [
                '<a href="/data/BRAVA/spectra/2006_68.fits">Fits spectrum</a>',
                '<a href="/data/BRAVA/spectra/a_obj_norm_2_obj_norm.fits">Fits spectrum</a>',
            ],
            # Single-observation rows carry the first three digits of the 2MASS
            # designation here; two-observation rows carry the second file.
            "fits_spectra_2": [
                "x" + " " * 64 + "183",
                "<a href='/data/BRAVA/spectra/c_obj_norm_25_obj_norm.fits'>Fits spectrum</a>",
            ],
            "tmass_id": ["75172-1503482", ""],
        }
    )


def test_brava_page_rejoins_2mass_name_and_emits_both_observations(monkeypatch):
    service = _BravaService(_brava_table())
    monkeypatch.setattr(irsa_missions, "make_tap_service", lambda url: service)
    monkeypatch.setattr(irsa_missions, "_brava_obs_date", lambda url: None)
    monkeypatch.setattr(irsa_missions, "BRAVA_PAGE_SIZE", 2)

    recs, last_cntr, exhausted = irsa_missions._fetch_brava_page(3786)
    assert "cntr > 3786" in service.queries[0]
    assert (last_cntr, exhausted) == (3790, False)  # a full page: more may follow
    assert [r.archive_obs_id for r in recs] == ["brava:3787:1", "brava:3790:1", "brava:3790:2"]
    assert recs[0].raw_target_name == "2MASS J18375172-1503482"
    assert recs[0].archive_url == "https://irsa.ipac.caltech.edu/data/BRAVA/spectra/2006_68.fits"
    assert recs[1].raw_target_name is None and recs[2].raw_target_name is None
    assert recs[2].archive_url.endswith("/c_obj_norm_25_obj_norm.fits")


def test_brava_date_comes_from_the_fits_header(monkeypatch):
    class _Resp:
        content = b"SIMPLE  =                    T" + b" " * 50 + b"DATE-OBS= '2008-08-25T02:42:3.233'" + b" " * 40

        def raise_for_status(self):
            pass

    seen = {}
    monkeypatch.setattr(irsa_missions._session, "get", lambda url, **kw: seen.update(kw) or _Resp())
    irsa_missions._brava_obs_date.cache_clear()
    assert irsa_missions._brava_obs_date("https://irsa.ipac.caltech.edu/x.fits").isoformat() == "2008-08-25"
    assert seen["headers"]["Range"].startswith("bytes=0-")
    irsa_missions._brava_obs_date.cache_clear()


def test_brava_pages_advance_then_finish(monkeypatch):
    pages = iter([(["a"], 500, False), (["b"], 620, True)])
    monkeypatch.setattr(irsa_missions, "_fetch_brava_page", lambda after: next(pages))
    base = {"tap_done": True, "whole_sky_done": True, "whole_sky_collections": sorted(irsa_missions.WHOLE_SKY_COLLECTIONS),
            "grid_index": 3}
    recs, cursor = irsa_missions.fetch(base)
    assert recs == ["a"] and cursor == {**base, "brava_cntr": 500}
    recs, cursor = irsa_missions.fetch(cursor)
    assert recs == ["b"] and cursor == {**base, "brava_cntr": 620, "brava_done": True}


def test_every_new_irsa_instrument_has_a_wavelength_range():
    labels = [irsa_missions.BRAVA_INSTRUMENT]
    for collections in (irsa_missions.WHOLE_SKY_COLLECTIONS, irsa_missions.GRID_COLLECTIONS):
        labels += [m["instrument"] for m in collections.values()]
    for instrument in labels:
        assert (ARCHIVE_NAME, instrument) in instrument_wavelengths.INSTRUMENT_WAVELENGTH_RANGE_NM


def test_forcast_grid_starts_where_a_finished_two_collection_cursor_stopped(monkeypatch):
    # Production's cursor finished iso_sws + iras_lrs at this index; the
    # FORCAST cells must begin exactly there, not shift the earlier ones.
    finished_before = 2 * len(irsa_missions.GRID_CELLS)
    assert irsa_missions.GRID_TASKS[finished_before] == ("sofia_forcast", 0)
    assert [name for name, _ in irsa_missions.GRID_TASKS[:finished_before:len(irsa_missions.GRID_CELLS)]] == ["iso_sws", "iras_lrs"]

    asked = []
    monkeypatch.setattr(irsa_missions, "_query", lambda collection, pos, size: asked.append(collection) or [])
    cursor = {"tap_done": True, "whole_sky_done": True, "whole_sky_collections": sorted(irsa_missions.WHOLE_SKY_COLLECTIONS),
              "brava_done": True, "grid_index": finished_before}
    _, new_cursor = irsa_missions.fetch(cursor)
    assert asked == ["sofia_forcast"] and new_cursor["grid_index"] == finished_before + 1


def test_every_tap_instrument_has_a_wavelength_range():
    for meta in irsa_missions.TAP_TABLES.values():
        for instrument in meta["files"].values():
            assert (ARCHIVE_NAME, instrument) in instrument_wavelengths.INSTRUMENT_WAVELENGTH_RANGE_NM


@pytest.mark.parametrize("instrument", ["Spitzer/IRS (FEPS)", "Spitzer/IRS (FEPS high-res)", "Spitzer/IRS (Disks SH)", "Spitzer/IRS (c2d)"])
def test_viewer_rejects_tbl_instruments_instead_of_parsing_as_fits(instrument):
    with pytest.raises(SpectrumUnavailable):
        _parse_irsa_missions({"instrument": instrument, "archive_url": "unused"})
