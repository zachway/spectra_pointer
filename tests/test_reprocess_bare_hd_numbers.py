import os
from datetime import date

import psycopg
import pytest

from scripts.reprocess_bare_hd_numbers import reprocess
from tests.conftest import TEST_BSC_HR_LOW


def _insert_bsc5_star(cur, hr_number, ra, dec, name_aliases):
    cur.execute(
        "INSERT INTO stars (source_catalog, bsc_hr_number, ra, dec, ref_epoch, pmra, pmdec, name_aliases) "
        "VALUES ('bsc5', %s, %s, %s, 1991.25, 0, 0, %s)",
        (hr_number, ra, dec, name_aliases),
    )


@pytest.fixture
def read_conn(conn):
    # reprocess() reads through a named (server-side) cursor on one
    # connection while writing/committing through another -- see its own
    # docstring for why the two can't share a connection. `conn` (the
    # fixture-provided connection) is used for writes/assertions here, this
    # is the dedicated read-side connection.
    database_url = os.environ.get("DATABASE_URL", "postgresql:///spectra_test")
    connection = psycopg.connect(database_url)
    yield connection
    connection.close()


def test_bare_hd_number_holding_gets_reclaimed(conn, read_conn):
    """A holding previously stuck on shitty_positional_match (or skipped
    entirely) because its raw_target_name was a bare HD number must resolve
    to name_resolved once reprocessed, now that sync.matcher recognizes the
    bare-digit form."""
    ra0, dec0 = 220.0, -60.0
    with conn.cursor() as cur:
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW, ra0, dec0, ["HD 999999", "NAME Test Star"])
        cur.execute(
            "INSERT INTO spectroscopy_holdings "
            "(archive_code, archive_obs_id, archive_url, instrument, obs_date, program_id, "
            " match_method, match_status, raw_target_name, raw_ra, raw_dec) VALUES "
            "('unit_test', 'barehd-1', 'http://example.test/barehd1', 'echelle', %s, 'smarts', "
            " 'shitty_positional_match', 'needs_review', '999999', %s, %s)",
            (date(2015, 1, 1), ra0, dec0),
        )
    conn.commit()

    totals = reprocess(read_conn, conn)
    assert totals.get("name_matched", 0) == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.bsc_hr_number, h.match_method, h.match_status FROM spectroscopy_holdings h "
            "LEFT JOIN stars s ON s.star_id = h.star_id "
            "WHERE h.archive_code='unit_test' AND h.archive_obs_id='barehd-1'"
        )
        hr_number, method, status = cur.fetchone()
    assert hr_number == TEST_BSC_HR_LOW
    assert method == "name_resolved"
    assert status == "matched"


def test_already_name_resolved_holding_is_not_reselected(conn, read_conn):
    """The sweep's WHERE clause excludes anything already match_method =
    'name_resolved' -- confirms a holding that's already correct doesn't
    get needlessly touched again (and wouldn't show up in totals)."""
    ra0, dec0 = 100.0, 10.0
    with conn.cursor() as cur:
        _insert_bsc5_star(cur, TEST_BSC_HR_LOW, ra0, dec0, ["HD 888888"])
        cur.execute("SELECT star_id FROM stars WHERE bsc_hr_number = %s", (TEST_BSC_HR_LOW,))
        star_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO spectroscopy_holdings "
            "(star_id, archive_code, archive_obs_id, archive_url, instrument, obs_date, "
            " match_method, match_status, raw_target_name, raw_ra, raw_dec) VALUES "
            "(%s, 'unit_test', 'barehd-2', 'http://example.test/barehd2', 'echelle', %s, "
            " 'name_resolved', 'matched', '888888', %s, %s)",
            (star_id, date(2015, 1, 1), ra0, dec0),
        )
    conn.commit()

    totals = reprocess(read_conn, conn)
    assert totals == {}
