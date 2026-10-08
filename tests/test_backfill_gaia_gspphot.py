from astropy.table import MaskedColumn, Table

from scripts import backfill_gaia_gspphot
from tests.conftest import TEST_ID_LOW

# Has GSP-Phot results in Gaia.
WITH_PARAMS_ID = TEST_ID_LOW + 30
# In Gaia, but GSP-Phot published nothing for it -- must still be stamped as
# checked, or it would be re-queried on every run.
NO_PARAMS_ID = TEST_ID_LOW + 31
# Already checked on an earlier run -- must not be sent to Gaia again.
ALREADY_CHECKED_ID = TEST_ID_LOW + 32


class _FakeJob:
    def get_results(self):
        return Table({
            "source_id": [WITH_PARAMS_ID, NO_PARAMS_ID],
            "teff_gspphot": MaskedColumn([5777.0, 0.0], mask=[False, True]),
            "logg_gspphot": MaskedColumn([4.44, 0.0], mask=[False, True]),
            "mh_gspphot": MaskedColumn([-0.1, 0.0], mask=[False, True]),
        })


def _insert_pending_pair(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stars (gaia_source_id, ra, dec) VALUES (%s, 10.0, 20.0), (%s, 11.0, 21.0)",
            (WITH_PARAMS_ID, NO_PARAMS_ID),
        )
        # Anything else pending in this shared test DB would otherwise be
        # swept into the same (faked) Gaia query.
        cur.execute(
            "UPDATE stars SET gspphot_checked_at = now() WHERE gspphot_checked_at IS NULL "
            "AND gaia_source_id NOT IN (%s, %s)",
            (WITH_PARAMS_ID, NO_PARAMS_ID),
        )
    conn.commit()


def _gspphot(conn, source_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT teff_gspphot, logg_gspphot, mh_gspphot, gspphot_checked_at IS NOT NULL "
            "FROM stars WHERE gaia_source_id = %s",
            (source_id,),
        )
        return cur.fetchone()


def test_backfill_stamps_every_checked_star_and_is_a_noop_on_rerun(conn, monkeypatch):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stars (gaia_source_id, ra, dec, teff_gspphot, gspphot_checked_at) "
            "VALUES (%s, 12.0, 22.0, 4000.0, now())",
            (ALREADY_CHECKED_ID,),
        )
    _insert_pending_pair(conn)

    queries = []

    def fake_launch(query):
        queries.append(query)
        return _FakeJob()

    monkeypatch.setattr(backfill_gaia_gspphot, "_launch_gaia_job", fake_launch)

    assert backfill_gaia_gspphot.backfill(conn) == (2, 1)
    assert len(queries) == 1
    assert str(WITH_PARAMS_ID) in queries[0] and str(NO_PARAMS_ID) in queries[0]
    assert str(ALREADY_CHECKED_ID) not in queries[0]
    assert "TOP 2 " in queries[0]

    teff, logg, mh, checked = _gspphot(conn, WITH_PARAMS_ID)
    assert (round(teff), round(logg, 2), round(mh, 1), checked) == (5777, 4.44, -0.1, True)
    assert _gspphot(conn, NO_PARAMS_ID) == (None, None, None, True)
    assert _gspphot(conn, ALREADY_CHECKED_ID)[0] == 4000.0

    assert backfill_gaia_gspphot.backfill(conn) == (0, 0)
    assert len(queries) == 1


def test_backfill_limit_stops_early(conn, monkeypatch):
    _insert_pending_pair(conn)
    monkeypatch.setattr(backfill_gaia_gspphot, "_launch_gaia_job", lambda query: _FakeJob())

    assert backfill_gaia_gspphot.backfill(conn, limit=1)[0] == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM stars WHERE gaia_source_id IN (%s, %s) AND gspphot_checked_at IS NULL",
            (WITH_PARAMS_ID, NO_PARAMS_ID),
        )
        assert cur.fetchone()[0] == 1
