import math
import random

import pytest
import requests

from sync.archives import spitzer_sha
from pathlib import Path

from webapp import instrument_wavelengths

DISPLAY_NAME = "Spitzer Heritage Archive (IRS + MIPS-SED)"


def _row(mode="IRS Stare", key="10649856", name="TWCam", **overrides):
    row = {
        "targetname": name,
        "raj2000": "65.19845833333332",
        "decj2000": "57.44124166666666",
        "modedisplayname": mode,
        "reqbegintime": "2004-10-03 22:47:37.208",
        "progid": "3274",
        "reqkey": key,
        "depthofcoverage": f"/sha/archive/proc/IRSX003800/r{key}/SPITZER_S_{key}_DOC.fits",
    }
    row.update(overrides)
    return row


def test_grid_covers_the_whole_sky_within_cell_radius():
    rng = random.Random(0)
    worst = 0.0
    for _ in range(3000):
        ra = rng.uniform(0, 360)
        dec = math.degrees(math.asin(rng.uniform(-1, 1)))
        d = min(_sep_deg(ra, dec, c_ra, c_dec) for c_ra, c_dec in spitzer_sha.GRID_CELLS)
        worst = max(worst, d)
    assert worst < spitzer_sha.CELL_RADIUS_DEG
    # Both poles too -- random sampling rarely lands there.
    for dec in (-90.0, 90.0):
        assert min(_sep_deg(0.0, dec, c_ra, c_dec) for c_ra, c_dec in spitzer_sha.GRID_CELLS) < spitzer_sha.CELL_RADIUS_DEG


def _sep_deg(ra1, dec1, ra2, dec2):
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def test_folder_url_uppercases_sha_and_drops_filename():
    assert (
        spitzer_sha._aor_folder_url("/sha/archive/proc/IRSX003800/r10649856/SPITZER_S_10649856_DOC.fits")
        == "https://irsa.ipac.caltech.edu/data/SPITZER/SHA/archive/proc/IRSX003800/r10649856/"
    )
    assert spitzer_sha._aor_folder_url(None) is None
    assert spitzer_sha._aor_folder_url("") is None
    assert spitzer_sha._aor_folder_url("/elsewhere/x.fits") is None


def test_observation_carries_real_date_and_no_fabrication():
    obs = spitzer_sha._to_observation(_row(), "Spitzer/IRS (Stare)")
    assert obs.archive_obs_id == "10649856"
    assert str(obs.obs_date) == "2004-10-03"
    assert obs.program_id == "3274"
    assert obs.raw_target_name == "TWCam"
    assert obs.ra == pytest.approx(65.19845833)
    assert obs.reduction_status == "reduced"


def test_row_without_url_or_aorkey_is_dropped():
    assert spitzer_sha._to_observation(_row(depthofcoverage=None), "x") is None
    assert spitzer_sha._to_observation(_row(key=None, depthofcoverage="/sha/a/b/c.fits"), "x") is None


def test_fetch_cell_keeps_spectral_modes_only_and_dedupes(monkeypatch):
    def fake_search(ra, dec, radius, enabled_key):
        if enabled_key == "instrumentFilter_IRS":
            return [_row("IRS Stare", "1"), _row("IRS Map", "2"), _row("IRS Peakup Image", "3"), _row("IRS Stare", "1")]
        return [_row("MIPS SED", "4"), _row("MIPS Phot", "5"), _row("MIPS Scan", "6")]

    monkeypatch.setattr(spitzer_sha, "_search_with_split", fake_search)
    monkeypatch.setattr(spitzer_sha, "REQUEST_DELAY_SEC", 0)
    got = {o.archive_obs_id: o.instrument for o in spitzer_sha._fetch_cell(10.0, 10.0)}
    assert got == {"1": "Spitzer/IRS (Stare)", "2": "Spitzer/IRS (Map)", "4": "Spitzer/MIPS-SED"}


def test_fetch_advances_cell_cursor_and_finished_grid_is_noop(monkeypatch):
    monkeypatch.setattr(spitzer_sha, "_fetch_cell", lambda ra, dec: [])
    records, cursor = spitzer_sha.fetch({})
    assert (records, cursor) == ([], {"cell": 1})
    done = {"cell": len(spitzer_sha.GRID_CELLS)}
    assert spitzer_sha.fetch(done) == ([], done)


def test_timeout_splits_into_four_half_radius_cones_then_gives_up(monkeypatch):
    calls = []

    def fake_search(ra, dec, radius, key):
        calls.append(radius)
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(spitzer_sha, "_search", fake_search)
    with pytest.raises(requests.exceptions.ReadTimeout):
        spitzer_sha._search_with_split(100.0, 0.0, 4.0, "instrumentFilter_IRS")
    # depth 0 tries once, splits into 4 at radius 2, each of those splits into 4 at
    # radius 1, and depth-2 cones raise instead of splitting further -- the first
    # depth-2 failure aborts the run.
    assert calls[:3] == [4.0, 2.0, 1.0]


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_empty_cone_without_a_data_key_returns_no_rows(monkeypatch):
    # Observed live: a cone with no matches has "columns" and totalRows 0 but
    # no "data" key at all (crashed the first prod run with KeyError: 'data').
    body = {"totalRows": 0, "tableData": {"columns": [{"name": "targetname"}, {"name": "reqkey"}]}}
    monkeypatch.setattr(spitzer_sha._session, "post", lambda *a, **k: _FakeResponse(body))
    assert spitzer_sha._search(261.8, -72.5, 4.5, "instrumentFilter_IRS") == []


def test_search_pages_until_a_short_page(monkeypatch):
    monkeypatch.setattr(spitzer_sha, "PAGE_SIZE", 2)
    monkeypatch.setattr(spitzer_sha, "REQUEST_DELAY_SEC", 0)
    columns = [{"name": "reqkey"}]
    pages = iter([[["1"], ["2"]], [["3"]]])
    monkeypatch.setattr(
        spitzer_sha._session, "post", lambda *a, **k: _FakeResponse({"tableData": {"columns": columns, "data": next(pages)}})
    )
    assert [r["reqkey"] for r in spitzer_sha._search(0.0, 0.0, 4.5, "instrumentFilter_IRS")] == ["1", "2", "3"]


def test_every_instrument_label_has_wavelength_and_resolving_power_entries():
    labels = {label for keep in spitzer_sha.QUERIES.values() for label in keep.values()}
    assert labels == {"Spitzer/IRS (Stare)", "Spitzer/IRS (Map)", "Spitzer/MIPS-SED"}
    # webapp.app can't be imported without a configured data source, so its two
    # hand-maintained dicts are checked against the file's own text.
    app_source = (Path(__file__).resolve().parent.parent / "webapp" / "app.py").read_text()
    for label in labels:
        assert (DISPLAY_NAME, label) in instrument_wavelengths.INSTRUMENT_WAVELENGTH_RANGE_NM
        assert f"('{DISPLAY_NAME}', '{label}'):" in app_source  # resolving-power dict
    assert f"'{DISPLAY_NAME}': 'https://" in app_source  # archive homepage dict
