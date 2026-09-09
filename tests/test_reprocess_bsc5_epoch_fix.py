from datetime import date

from astropy import units as u
from astropy.coordinates import SkyCoord

from scripts.reprocess_against_new_stars import _reprocess_batch
from scripts.reprocess_bsc5_epoch_fix import find_bsc5_proximate_candidates
from tests.conftest import TEST_BSC_HR_LOW


def _insert_bsc5_star(cur, hr_number, ra, dec):
    cur.execute(
        "INSERT INTO stars (source_catalog, bsc_hr_number, ra, dec, ref_epoch, pmra, pmdec) "
        "VALUES ('bsc5', %s, %s, %s, 1991.25, 0, 0)",
        (hr_number, ra, dec),
    )


def test_bad_raw_dec_does_not_crash_the_scan(conn):
    """A present-but-out-of-range raw_dec (observed live on koa/gtc/mast/
    noirlab -- e.g. gtc's raw_dec=91.15, past 90 deg) used to crash
    SkyCoord construction for the whole batch outright, the same failure
    mode sync.matcher.match_records already guards against for MAST's
    -99.0 sentinel. Regression test: a bogus-dec row must be silently
    excluded rather than blowing up the scan, while a genuinely nearby
    row in the same archive/batch is still found.
    """
    ra0, dec0 = 40.0, 15.0
    with conn.cursor() as cur:
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW, ra0, dec0)
        cur.execute(
            "INSERT INTO spectroscopy_holdings "
            "(archive_code, archive_obs_id, archive_url, instrument, obs_date, "
            " match_method, match_status, raw_ra, raw_dec) VALUES "
            "('unit_test', 'baddec-1', 'http://example.test/baddec1', 'TESTSPEC', %s, "
            " 'positional_easy_match', 'skipped', %s, 91.15), "
            "('unit_test', 'gooddec-1', 'http://example.test/gooddec1', 'TESTSPEC', %s, "
            " 'positional_easy_match', 'skipped', %s, %s)",
            (date(2020, 1, 1), ra0, date(2020, 1, 1), ra0, dec0),
        )
    conn.commit()

    centers = SkyCoord(ra=[ra0] * u.deg, dec=[dec0] * u.deg)
    by_archive = find_bsc5_proximate_candidates(conn, centers)

    found_ids = sorted(r.archive_obs_id for r in by_archive.get("unit_test", []))
    assert found_ids == ["gooddec-1"]


def test_bad_raw_dec_star_still_gets_matched_after_filtering(conn):
    """End-to-end: the surviving (valid-dec) candidate from the same batch
    still resolves through matcher.match_records once the bad row is
    filtered out.
    """
    ra0, dec0 = 60.0, -10.0
    with conn.cursor() as cur:
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW, ra0, dec0)
        cur.execute(
            "INSERT INTO spectroscopy_holdings "
            "(archive_code, archive_obs_id, archive_url, instrument, obs_date, "
            " match_method, match_status, raw_ra, raw_dec) VALUES "
            "('unit_test', 'baddec-2', 'http://example.test/baddec2', 'TESTSPEC', %s, "
            " 'positional_easy_match', 'skipped', %s, -95.0), "
            "('unit_test', 'gooddec-2', 'http://example.test/gooddec2', 'TESTSPEC', %s, "
            " 'positional_easy_match', 'skipped', %s, %s)",
            (date(2020, 1, 1), ra0, date(2020, 1, 1), ra0, dec0),
        )
    conn.commit()

    centers = SkyCoord(ra=[ra0] * u.deg, dec=[dec0] * u.deg)
    by_archive = find_bsc5_proximate_candidates(conn, centers)
    totals = _reprocess_batch(conn, by_archive, "test")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.bsc_hr_number, h.match_status FROM spectroscopy_holdings h "
            "LEFT JOIN stars s ON s.star_id = h.star_id "
            "WHERE h.archive_code='unit_test' AND h.archive_obs_id='gooddec-2'"
        )
        hr, status = cur.fetchone()
    assert hr == TEST_BSC_HR_LOW
    assert status == "matched"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT match_status FROM spectroscopy_holdings "
            "WHERE archive_code='unit_test' AND archive_obs_id='baddec-2'"
        )
        (bad_status,) = cur.fetchone()
    assert bad_status == "skipped"
