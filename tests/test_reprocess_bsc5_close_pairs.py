from datetime import date

from scripts.reprocess_bsc5_close_pairs import find_close_bsc5_pairs, reprocess
from tests.conftest import TEST_BSC_HR_LOW


def _insert_bsc5_star(cur, hr_number, ra, dec):
    cur.execute(
        "INSERT INTO stars (source_catalog, bsc_hr_number, ra, dec, ref_epoch, pmra, pmdec) "
        "VALUES ('bsc5', %s, %s, %s, 1991.25, -3679.25, 473.67)",
        (hr_number, ra, dec),
    )


def test_finds_close_bsc5_pair(conn):
    ra0, dec0 = 150.0, -20.0
    with conn.cursor() as cur:
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW, ra0, dec0)
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW + 1, ra0 + 10.0 / 3600.0, dec0)  # 10" away
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW + 2, ra0 + 300.0, dec0)  # far -- not a pair
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT star_id FROM stars WHERE bsc_hr_number = %s", (TEST_BSC_HR_LOW,))
        id_a = cur.fetchone()[0]
        cur.execute("SELECT star_id FROM stars WHERE bsc_hr_number = %s", (TEST_BSC_HR_LOW + 1,))
        id_b = cur.fetchone()[0]

    pairs = find_close_bsc5_pairs(conn)
    found = {frozenset(p) for p in pairs}
    assert frozenset({id_a, id_b}) in found
    assert len(pairs) == 1  # the far star must not form a spurious pair


def test_reprocess_demotes_ambiguous_shitty_match_between_close_pair(conn):
    """End to end: a shitty_positional_match holding currently (wrongly,
    confidently) assigned to one member of a close BSC5 pair gets
    demoted to an honest star_id=NULL/needs_review once reprocessed,
    since the new BSC5_DISAMBIGUATION_RATIO guard can't tell the two apart.
    """
    ra0, dec0 = 219.9, -60.8
    with conn.cursor() as cur:
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW, ra0, dec0)
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW + 1, ra0 + 15.0 / 3600.0, dec0)  # ~15" away, like Alpha Cen A/B
        cur.execute("SELECT star_id FROM stars WHERE bsc_hr_number = %s", (TEST_BSC_HR_LOW,))
        wrong_star_id = cur.fetchone()[0]

        # A record sitting almost exactly between the two -- wrongly
        # (over-)confidently assigned to star A by the old categorical-win
        # rule before this fix.
        cur.execute(
            "INSERT INTO spectroscopy_holdings "
            "(star_id, archive_code, archive_obs_id, archive_url, instrument, obs_date, "
            " match_method, match_status, raw_target_name, raw_ra, raw_dec, theta_arcsec) VALUES "
            "(%s, 'unit_test', 'pair-1', 'http://example.test/pair1', 'echelle', %s, "
            " 'shitty_positional_match', 'needs_review', '999999', %s, %s, 7.5)",
            (wrong_star_id, date(2015, 1, 1), ra0 + 7.0 / 3600.0, dec0),
        )
    conn.commit()

    counts = reprocess(conn)
    assert counts.get("no_confident_candidate", 0) >= 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT star_id, match_status FROM spectroscopy_holdings "
            "WHERE archive_code='unit_test' AND archive_obs_id='pair-1'"
        )
        star_id, status = cur.fetchone()
    assert star_id is None
    assert status == "needs_review"
