import pytest

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
