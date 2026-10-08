import astropy.units as u
import numpy as np
import pytest
from astropy.table import Table
from astropy.time import Time

from sync.archives import fai_kz, nova_ar
from webapp import instrument_wavelengths


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("AlpCyg", "alp Cyg"),
        ("Omi1CMa", "omi01 CMa"),
        ("55Cyg", "55 Cyg"),
        ("HD332757", "HD 332757"),
        ("BD+43_1168", "BD+43 1168"),
        ("BS2385", "HR 2385"),
        ("BS_685", "HR 685"),
        ("PN Vy2-3", "PN Vy2-3"),
        ("IC 2003", "IC 2003"),
    ],
)
def test_fai_clean_name(raw, cleaned):
    assert fai_kz._clean_name(raw) == cleaned


class _FakeService:
    def __init__(self, table):
        self._table = table

    def run_sync(self, query):
        assert "'agn_azt8'" not in query  # the AGN collection is never asked for
        return self

    def to_table(self):
        return self._table


def test_fai_fetch_maps_rows_and_then_waits_for_the_refresh_window(monkeypatch):
    table = Table(
        {
            "obs_publisher_did": ["ivo://fai.kz/~?a.fits", "ivo://fai.kz/~?b.fits"],
            "obs_collection": ["KazVO eShel", "FAI PN Archive"],
            "access_url": ["https://dachs.fai.kz/getproduct/a.fits", "https://dachs.fai.kz/getproduct/b.fits"],
            "target_name": ["AlpCyg", "PN K4-46"],
            "s_ra": [130.36, 23.156],
            "s_dec": [-15.0, 54.747],
            "t_min": np.ma.masked_array([59071.05, 0.0], mask=[False, True]),
            "calib_level": [2, 2],
        }
    )
    monkeypatch.setattr(fai_kz, "make_tap_service", lambda url: _FakeService(table))
    recs, cursor = fai_kz.fetch({})
    assert [(r.instrument, r.raw_target_name) for r in recs] == [("TCO eShel", "alp Cyg"), ("FAI AZT-8 (PN archive)", "PN K4-46")]
    # eShel's stated positions are unusable; the nebula archive's RA is in hours.
    assert (recs[0].ra, recs[0].dec) == (None, None)
    assert (round(recs[1].ra, 2), recs[1].dec) == (347.34, 54.747)
    assert recs[0].obs_date.isoformat() == "2020-08-10" and recs[1].obs_date is None
    assert recs[0].archive_obs_id == "ivo://fai.kz/~?a.fits" and recs[0].reduction_status == "reduced"
    assert cursor["row_count"] == 2

    # Just synced: nothing to do until the refresh window passes.
    assert fai_kz.fetch(cursor) == ([], cursor)
    stale = {"synced_at": (Time.now() - (fai_kz.REFRESH_DAYS + 1) * u.day).isot}
    assert len(fai_kz.fetch(stale)[0]) == 2


@pytest.mark.parametrize(
    "raw, label",
    [
        ("Reosc DS  -  Red 080  600 l/mm", "REOSC"),
        ("REOSC en Dispersi n simple", "REOSC"),
        ("red: #580  - 600 l/mm - Ang: 6  40'", "REOSC"),
        ("rds #180 (316 l/mm) 4 50 grados", "REOSC"),
        ("Dispersion simple Red: # 270 300 l/mm", "REOSC"),
        ("BOLLER & CHIVENS red=CH260 600 L/MM", "Boller & Chivens"),
        ("FIRE-LCO", "FIRE-LCO"),
        ("  ", None),
    ],
)
def test_nova_instrument_folds_free_text(raw, label):
    assert nova_ar._instrument(raw) == label


def test_nova_fetch_drops_the_magoss_placeholder_date(monkeypatch):
    def rows(path):
        return Table(
            {
                "accref": [f"http://nova.fcaglp.unlp.edu.ar/getproduct/{path}/x.fits"],
                "mime": ["image/fits"],
                "ssa_targname": ["HD_122879"],
                "ssa_dateObs": [59931.0],
                "ssa_instrument": ["FIRE-LCO"],
                "ssa_location": [[211.6, -59.7]],
            }
        )

    monkeypatch.setattr(nova_ar, "_fetch_service_rows", rows)
    recs, cursor = nova_ar.fetch({})
    assert [r.obs_date is not None for r in recs] == [s["dated"] for s in nova_ar.SERVICES] == [True, False]
    assert recs[0].raw_target_name == "HD 122879" and (recs[0].ra, recs[0].dec) == (211.6, -59.7)
    assert len({r.archive_obs_id for r in recs}) == 2
    assert nova_ar.fetch(cursor) == ([], cursor)


def test_new_archive_instruments_have_wavelength_ranges():
    table = instrument_wavelengths.INSTRUMENT_WAVELENGTH_RANGE_NM
    for label in fai_kz.COLLECTIONS.values():
        assert ("Fesenkov Astrophysical Institute (Kazakhstan VO)", label) in table
    for label in ("REOSC", "FIRE-LCO", "GNIRS-GEMINI"):
        assert ("NOVA (Argentine Virtual Observatory)", label) in table
