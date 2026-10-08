from concurrent.futures import ThreadPoolExecutor

import numpy as np

from scripts import backfill_gaia_from_flatiron as flatiron
from tests.conftest import TEST_ID_LOW

# In the mirror file, with GSP-Phot and astrometry values.
FULL_ID = TEST_ID_LOW + 40
# In the mirror file, but every value blank there -- must still be stamped.
BLANK_ID = TEST_ID_LOW + 41
# Not in the mirror file at all -- stamped as checked, astrometry untouched.
ABSENT_ID = TEST_ID_LOW + 42
# GSP-Phot already checked and BP present: nothing pending, must not change.
DONE_ID = TEST_ID_LOW + 43

PIXEL = TEST_ID_LOW >> 43
FILES = [(PIXEL, PIXEL, "GaiaSource_test.hdf5"), (PIXEL + 1, PIXEL + 5, "GaiaSource_other.hdf5")]


def _file_data():
    nan = np.nan
    return {
        # Sorted by source_id, with neighbours the catalog doesn't track.
        "source_id": np.array([FULL_ID - 1, FULL_ID, BLANK_ID, DONE_ID, DONE_ID + 7], dtype=np.int64),
        "teff_gspphot": np.array([3000.0, 5777.0, nan, 9999.0, 4000.0], dtype=np.float32),
        "logg_gspphot": np.array([5.0, 4.44, nan, 1.0, 4.5], dtype=np.float32),
        "mh_gspphot": np.array([0.0, -0.1, nan, 0.5, 0.2], dtype=np.float32),
        "parallax": np.array([1.0, 12.5, nan, 3.0, 4.0], dtype=np.float64),
        "phot_bp_mean_mag": np.array([10.0, 11.5, nan, 9.0, 8.0], dtype=np.float32),
        "phot_rp_mean_mag": np.array([9.0, 10.5, nan, 8.0, 7.0], dtype=np.float32),
        "has_rvs": np.array([0, 1, 0, 1, 0], dtype=np.int32),
        "has_xp_continuous": np.array([1, 1, 0, 1, 0], dtype=np.int32),
    }


def _row(conn, source_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT teff_gspphot, logg_gspphot, mh_gspphot, gspphot_checked_at IS NOT NULL, "
            "parallax, phot_bp_mean_mag, phot_rp_mean_mag, has_gaia_rvs, has_xp_continuous "
            "FROM stars WHERE gaia_source_id = %s",
            (source_id,),
        )
        return cur.fetchone()


def test_parse_listing_reads_pixel_ranges_from_file_names():
    html = (
        '<a href="../">../</a>'
        '<a href="GaiaSource_527233-528422.hdf5">x</a> 298.2MB'
        '<a href="GaiaSource_000000-003111.hdf5">x</a>'
        '<a href="README.txt">x</a>'
    )
    assert flatiron.parse_listing(html) == [
        (0, 3111, "GaiaSource_000000-003111.hdf5"),
        (527233, 528422, "GaiaSource_527233-528422.hdf5"),
    ]


def test_backfill_fills_pending_stars_from_file_data_and_is_a_noop_on_rerun(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stars (gaia_source_id, ra, dec) VALUES (%s, 10.0, 20.0), (%s, 11.0, 21.0), (%s, 12.0, 22.0)",
            (FULL_ID, BLANK_ID, ABSENT_ID),
        )
        cur.execute(
            "INSERT INTO stars (gaia_source_id, ra, dec, teff_gspphot, gspphot_checked_at, phot_bp_mean_mag) "
            "VALUES (%s, 13.0, 23.0, 4321.0, now(), 7.5)",
            (DONE_ID,),
        )
        # Anything else pending in this shared test DB would otherwise be
        # swept up as a star the (fake) file doesn't list.
        cur.execute(
            "UPDATE stars SET gspphot_checked_at = now() WHERE gspphot_checked_at IS NULL "
            "AND gaia_source_id NOT IN (%s, %s, %s)",
            (FULL_ID, BLANK_ID, ABSENT_ID),
        )
    conn.commit()

    calls = []

    def fake_fetch(name, columns):
        calls.append((name, columns))
        return _file_data()

    with ThreadPoolExecutor(max_workers=1) as executor:
        totals = flatiron.backfill(conn, FILES, workers=1, fetch=fake_fetch, executor=executor)

        # Only the file whose pixel range holds pending stars is fetched.
        assert calls == [("GaiaSource_test.hdf5", flatiron.GSPPHOT_COLUMNS + flatiron.ASTROMETRY_COLUMNS)]
        assert totals["files"] == 1 and totals["files_failed"] == 0
        assert totals["gspphot_checked"] == 3

        teff, logg, mh, checked, plx, bp, rp, rvs, xp = _row(conn, FULL_ID)
        assert (round(teff), round(logg, 2), round(mh, 1), checked) == (5777, 4.44, -0.1, True)
        assert (plx, bp, rp, rvs, xp) == (12.5, 11.5, 10.5, True, True)
        assert _row(conn, BLANK_ID) == (None, None, None, True, None, None, None, False, False)
        assert _row(conn, ABSENT_ID) == (None, None, None, True, None, None, None, False, False)
        assert _row(conn, DONE_ID)[:4] == (4321.0, None, None, True)
        assert _row(conn, DONE_ID)[5] == 7.5

        # GSP-Phot is done for all three; only astrometry is still looked
        # for, for the stars whose BP and RP stayed blank.
        calls.clear()
        again = flatiron.backfill(conn, FILES, workers=1, fetch=fake_fetch, executor=executor)
        assert again["gspphot_checked"] == 0
        assert [columns for _, columns in calls] == [flatiron.ASTROMETRY_COLUMNS]


def test_a_file_that_cannot_be_fetched_leaves_its_stars_pending(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO stars (gaia_source_id, ra, dec) VALUES (%s, 10.0, 20.0)", (FULL_ID,))
    conn.commit()

    def failing_fetch(name, columns):
        raise RuntimeError("mirror unreachable")

    with ThreadPoolExecutor(max_workers=1) as executor:
        totals = flatiron.backfill(conn, FILES[:1], workers=1, fetch=failing_fetch, executor=executor)

    assert totals["files_failed"] == 1 and totals["files"] == 0
    assert _row(conn, FULL_ID)[3] is False
