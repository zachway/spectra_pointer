from datetime import date

from sync import matcher
from sync.base import RawObservation
from sync.reconcile_eso_raw import reconcile


def _insert_star(cur, gaia_source_id, ra, dec):
    cur.execute(
        """
        INSERT INTO stars (gaia_source_id, ra, dec, ref_epoch, pmra, pmdec)
        VALUES (%s, %s, %s, 2016.0, 0, 0)
        ON CONFLICT (gaia_source_id) DO UPDATE SET ra = EXCLUDED.ra, dec = EXCLUDED.dec
        """,
        (gaia_source_id, ra, dec),
    )


def _matched_holding(conn, archive_code, archive_obs_id, gaia_source_id, instrument, obs_date):
    rec = RawObservation(
        archive_obs_id=archive_obs_id,
        archive_url=f"http://example.test/{archive_obs_id}",
        gaia_source_id=gaia_source_id,
        instrument=instrument,
        obs_date=obs_date,
    )
    matcher.match_records(conn, archive_code, [rec])


def _fetch_status(cur, archive_code, archive_obs_id):
    cur.execute(
        "SELECT 1 FROM spectroscopy_holdings WHERE archive_code=%s AND archive_obs_id=%s",
        (archive_code, archive_obs_id),
    )
    return cur.fetchone() is not None


def test_reconcile_deletes_superseded_raw_row(conn):
    star_id = 900000000000000010
    with conn.cursor() as cur:
        _insert_star(cur, star_id, 219.88, -60.83)
    conn.commit()

    _matched_holding(conn, "eso", "ADP.example-1", star_id, "HARPS", date(2010, 7, 28))
    _matched_holding(conn, "eso_raw", "HARPS.example-1", star_id, "HARPS", date(2010, 7, 28))

    deleted = reconcile(conn)
    assert deleted == 1

    with conn.cursor() as cur:
        assert _fetch_status(cur, "eso", "ADP.example-1") is True
        assert _fetch_status(cur, "eso_raw", "HARPS.example-1") is False


def test_reconcile_leaves_raw_only_row_alone(conn):
    star_id = 900000000000000011
    with conn.cursor() as cur:
        _insert_star(cur, star_id, 10.0, -5.0)
    conn.commit()

    # No Phase 3 counterpart exists for this raw exposure at all.
    _matched_holding(conn, "eso_raw", "HARPS.example-2", star_id, "HARPS", date(2003, 2, 20))

    deleted = reconcile(conn)
    assert deleted == 0

    with conn.cursor() as cur:
        assert _fetch_status(cur, "eso_raw", "HARPS.example-2") is True


def test_reconcile_requires_matching_instrument_and_date(conn):
    star_id = 900000000000000012
    with conn.cursor() as cur:
        _insert_star(cur, star_id, 30.0, 15.0)
    conn.commit()

    _matched_holding(conn, "eso", "ADP.example-3", star_id, "UVES", date(2015, 1, 1))
    # Same star, but a different instrument and a different night -- not the
    # same physical exposure, must survive reconciliation.
    _matched_holding(conn, "eso_raw", "HARPS.example-3", star_id, "HARPS", date(2015, 1, 2))

    deleted = reconcile(conn)
    assert deleted == 0

    with conn.cursor() as cur:
        assert _fetch_status(cur, "eso_raw", "HARPS.example-3") is True
