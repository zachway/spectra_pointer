from astropy.table import Table

from scripts import backfill_gaia_astrometry
from tests.conftest import TEST_ID_LOW

# A star with valid ra/dec/pmra/pmdec but no parallax/bp/rp/rvs/xp yet --
# exactly the shape ingest.add_star._fetch_astrometry_offline produces.
OFFLINE_STAR_ID = TEST_ID_LOW + 20

# Already fully backfilled -- must be left alone (and must not appear in
# the id_list sent to Gaia at all, since backfill() only selects rows still
# missing bp/rp).
COMPLETE_STAR_ID = TEST_ID_LOW + 21

# gaia_source_id IS NULL (a BSC5 star) -- must never be selected, since a
# NULL landing in the comma-joined id_list would be a syntax error against
# Gaia's own TAP service.
BSC5_HR_NUMBER = 999999


class _FakeBackfillJob:
    def __init__(self, requested_ids):
        self.requested_ids = requested_ids

    def get_results(self):
        return Table({
            "source_id": [OFFLINE_STAR_ID],
            "parallax": [12.5],
            "phot_bp_mean_mag": [11.2],
            "phot_rp_mean_mag": [10.1],
            "has_rvs": [True],
            "has_xp_continuous": [True],
        })


def test_backfill_fills_missing_columns_and_skips_complete_or_gaia_less_stars(conn, monkeypatch):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stars (gaia_source_id, ra, dec, ref_epoch, pmra, pmdec, phot_g_mean_mag) "
            "VALUES (%s, 10.0, 20.0, 2016.0, 1.0, -1.0, 12.0)",
            (OFFLINE_STAR_ID,),
        )
        cur.execute(
            "INSERT INTO stars (gaia_source_id, ra, dec, ref_epoch, phot_g_mean_mag, "
            "parallax, phot_bp_mean_mag, phot_rp_mean_mag, has_gaia_rvs, has_xp_continuous) "
            "VALUES (%s, 30.0, 40.0, 2016.0, 13.0, 5.0, 13.5, 12.6, true, false)",
            (COMPLETE_STAR_ID,),
        )
        cur.execute("DELETE FROM stars WHERE source_catalog = 'bsc5' AND bsc_hr_number = %s", (BSC5_HR_NUMBER,))
        cur.execute(
            "INSERT INTO stars (source_catalog, bsc_hr_number, ra, dec, ref_epoch) "
            "VALUES ('bsc5', %s, 50.0, 60.0, 1991.25)",
            (BSC5_HR_NUMBER,),
        )
    conn.commit()

    calls = []

    def fake_launch_job(query):
        calls.append(query)
        assert str(BSC5_HR_NUMBER) not in query, "a NULL gaia_source_id must never reach the id_list"
        assert str(COMPLETE_STAR_ID) not in query, "a star with bp/rp already set must not be re-fetched"
        assert str(OFFLINE_STAR_ID) in query
        return _FakeBackfillJob([OFFLINE_STAR_ID])

    monkeypatch.setattr(backfill_gaia_astrometry.Gaia, "launch_job", fake_launch_job)

    try:
        updated = backfill_gaia_astrometry.backfill(conn)

        assert updated == 1
        assert len(calls) == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT parallax, phot_bp_mean_mag, phot_rp_mean_mag, has_gaia_rvs, has_xp_continuous "
                "FROM stars WHERE gaia_source_id = %s",
                (OFFLINE_STAR_ID,),
            )
            row = cur.fetchone()
        assert row == (12.5, 11.2, 10.1, True, True)
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM stars WHERE source_catalog = 'bsc5' AND bsc_hr_number = %s", (BSC5_HR_NUMBER,))
        conn.commit()
