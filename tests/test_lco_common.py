import pytest
import requests

from sync.archives import _lco_common
from sync.archives._lco_common import _centroid


def test_centroid_handles_ra_wraparound():
    # A footprint straddling the 0/360 seam, e.g. corners at 359.9 and 0.1
    # degrees -- a plain arithmetic mean would land at 180 (opposite side
    # of the sky); the real centroid is near 0/360.
    area = {
        "coordinates": [
            [
                [359.9, -1.0],
                [0.1, -1.0],
                [0.1, 1.0],
                [359.9, 1.0],
                [359.9, -1.0],
            ]
        ]
    }
    lon, lat = _centroid(area)
    assert lon == pytest.approx(0.0, abs=1e-6) or lon == pytest.approx(360.0, abs=1e-6)
    assert lat == pytest.approx(0.0)


def test_centroid_away_from_seam_matches_arithmetic_mean():
    area = {
        "coordinates": [
            [
                [10.0, 20.0],
                [12.0, 20.0],
                [12.0, 22.0],
                [10.0, 22.0],
                [10.0, 20.0],
            ]
        ]
    }
    lon, lat = _centroid(area)
    assert lon == pytest.approx(11.0)
    assert lat == pytest.approx(21.0)


def test_centroid_none_area_returns_none():
    assert _centroid(None) is None


class _FakeLCO:
    """Stands in for archive-api.lco.global: `n` rows, some offsets poisoned."""

    def __init__(self, n, poisoned=(), all_fail=False):
        self.rows = [{"id": i} for i in range(n)]
        self.poisoned = set(poisoned)
        self.all_fail = all_fail
        self.calls = []

    def __call__(self, obstype, last_date, limit, offset=0):
        self.calls.append((limit, offset))
        window = range(offset, min(offset + limit, len(self.rows)))
        if self.all_fail or self.poisoned.intersection(window):
            resp = requests.Response()
            resp.status_code = 500
            raise requests.HTTPError("500 Server Error", response=resp)
        return [self.rows[i] for i in window]


def test_fetch_one_page_passes_clean_page_through(monkeypatch):
    fake = _FakeLCO(150)
    monkeypatch.setattr(_lco_common, "_get_frames", fake)
    rows = _lco_common._fetch_one_page("SPECTRUM", "2014-11-20")
    assert [r["id"] for r in rows] == list(range(100))
    assert fake.calls == [(100, 0)]


def test_fetch_one_page_skips_only_the_poisoned_frame(monkeypatch):
    # The live lco_floyds case: offset 76 from the stuck cursor 500s.
    fake = _FakeLCO(150, poisoned={76})
    monkeypatch.setattr(_lco_common, "_get_frames", fake)
    rows = _lco_common._fetch_one_page("SPECTRUM", "2014-11-20")
    assert [r["id"] for r in rows] == [i for i in range(100) if i != 76]


def test_fetch_one_page_short_final_page_with_poisoned_frame(monkeypatch):
    fake = _FakeLCO(33, poisoned={31})
    monkeypatch.setattr(_lco_common, "_get_frames", fake)
    rows = _lco_common._fetch_one_page("SPECTRUM", "2014-11-20")
    assert [r["id"] for r in rows] == [i for i in range(33) if i != 31]


def test_fetch_one_page_reraises_on_real_outage(monkeypatch):
    fake = _FakeLCO(150, all_fail=True)
    monkeypatch.setattr(_lco_common, "_get_frames", fake)
    with pytest.raises(requests.HTTPError):
        _lco_common._fetch_one_page("SPECTRUM", "2014-11-20")
    # Gives up after the first chunk rather than probing all 100 rows.
    assert len(fake.calls) == 1 + 1 + 10


def test_fetch_one_page_does_not_retry_client_errors(monkeypatch):
    calls = []

    def bad_request(*args, **kwargs):
        calls.append(args)
        resp = requests.Response()
        resp.status_code = 400
        raise requests.HTTPError("400 Client Error", response=resp)

    monkeypatch.setattr(_lco_common, "_get_frames", bad_request)
    with pytest.raises(requests.HTTPError):
        _lco_common._fetch_one_page("SPECTRUM", "2014-11-20")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        # Real target_name values from the live holdings table.
        ("61_Cyg_A_wcs_LL", "61 Cyg A"),
        ("sigma_Dra_bri_LL", "sigma Dra"),
        ("27_Tau_coo_LL", "27 Tau"),
        ("HD_3765_bri_LL", "HD 3765"),
        ("HD38858_bri", "HD38858"),
        ("HD49933_bri_engr", "HD49933"),
        ("alphaSco_bri_pystrat", "alphaSco"),
        ("KELT-13b_coo_LL", "KELT-13b"),
        # No acquisition suffix: only underscores change.
        ("ESO_511-30", "ESO 511-30"),
        ("asassn-14jg", "asassn-14jg"),
        ("Mrk 817", "Mrk 817"),
        # A real name that merely ends like a tag's second token stays whole.
        ("NGC1234_LL", "NGC1234 LL"),
        ("", ""),
    ],
)
def test_clean_name(raw, cleaned):
    assert _lco_common._clean_name(raw) == cleaned
