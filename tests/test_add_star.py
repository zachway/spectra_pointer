from ingest import add_star
from tests.conftest import TEST_ID_LOW


def test_launch_gaia_job_retries_transient_failure(monkeypatch):
    """A couple of bad responses (the HTML-error-page failure mode seen live
    against Gaia's TAP+ endpoint) shouldn't kill the whole sync — retry
    should clear it once the transient blip passes."""
    monkeypatch.setattr(add_star.time, "sleep", lambda _seconds: None)

    calls = []

    def flaky_launch_job(query):
        calls.append(query)
        if len(calls) < 3:
            raise ValueError("Not a gzipped file (b'<h')")
        return "ok"

    monkeypatch.setattr(add_star.Gaia, "launch_job", flaky_launch_job)

    result = add_star._launch_gaia_job("SELECT 1")

    assert result == "ok"
    assert len(calls) == 3


def test_launch_gaia_job_raises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(add_star.time, "sleep", lambda _seconds: None)

    def always_fails(query):
        raise ValueError("Not a gzipped file (b'<h')")

    monkeypatch.setattr(add_star.Gaia, "launch_job", always_fails)

    try:
        add_star._launch_gaia_job("SELECT 1")
        assert False, "expected _launch_gaia_job to raise"
    except ValueError:
        pass


def test_add_stars_batch_falls_back_to_offline_after_gaia_tap_exhausts_retries(conn, monkeypatch):
    """A live Gaia TAP call that exhausts retries mid-batch (see
    _launch_gaia_job) shouldn't fail the whole add_stars_batch call -- it
    should fall back to the local gaia_source_lite_mirror for that chunk and
    every remaining chunk, and report gaia_degraded so a caller (sync.main's
    sync_archive) can go sticky-offline for the rest of that archive."""
    monkeypatch.setattr(add_star, "BATCH_CHUNK_SIZE", 1)

    ids = [TEST_ID_LOW + 10, TEST_ID_LOW + 11]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO gaia_source_lite_mirror (source_id, ra, dec, pmra, pmdec, phot_g_mean_mag) "
            "VALUES (%s, 10.0, 20.0, 1.0, -1.0, 12.0), (%s, 11.0, 21.0, 2.0, -2.0, 13.0)",
            tuple(ids),
        )
    conn.commit()

    online_calls = []

    def flaky_online(chunk):
        online_calls.append(list(chunk))
        raise ValueError("Not a gzipped file (b'<h')")

    monkeypatch.setattr(add_star, "_fetch_astrometry_online", flaky_online)

    try:
        result = add_star.add_stars_batch(conn, ids)

        assert result.added == 2
        assert result.gaia_degraded is True
        # BATCH_CHUNK_SIZE=1 means two chunks -- only the first should ever
        # attempt the live call; once it fails, the second skips straight
        # to the local mirror instead of retrying a TAP that just proved
        # itself unreachable.
        assert len(online_calls) == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT gaia_source_id, ra, dec, pmra, pmdec, parallax, phot_g_mean_mag "
                "FROM stars WHERE gaia_source_id = ANY(%s) ORDER BY gaia_source_id",
                (ids,),
            )
            rows = cur.fetchall()
        assert rows == [
            (ids[0], 10.0, 20.0, 1.0, -1.0, None, 12.0),
            (ids[1], 11.0, 21.0, 2.0, -2.0, None, 13.0),
        ]
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gaia_source_lite_mirror WHERE source_id = ANY(%s)", (ids,))
        conn.commit()
