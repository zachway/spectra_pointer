from datetime import date

import sync.reconcile as reconcile_module
from sync import state
from sync.base import RawObservation
from sync.reconcile import reconcile_archive, reconcile_shitty_positional_match


def _clear_sync_state(conn, archive_code):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM archive_sync_state WHERE archive_code=%s", (archive_code,))
    conn.commit()


def _obs(archive_obs_id):
    return RawObservation(
        archive_obs_id=archive_obs_id,
        archive_url=f"http://example.test/{archive_obs_id}",
        instrument="TEST",
        obs_date=date(2020, 1, 1),
        raw_target_name=None,
    )


def test_reconcile_archive_is_independent_of_sync_cursor(conn):
    _clear_sync_state(conn, "unit_test")
    state.record_run(conn, "unit_test", {"last_t_min": 100}, "success", "live sync", 5)

    def fake_fetch(cursor):
        assert cursor == {}, "reconcile must start from its own cursor, not sync_cursor's value"
        return [], {"last_t_min": 3}

    reconcile_archive(conn, "unit_test", fake_fetch, max_pages=10)

    assert state.get_cursor(conn, "unit_test") == {"last_t_min": 100}, "sync_cursor must be untouched"


def test_reconcile_archive_wraps_cursor_on_convergence(conn):
    _clear_sync_state(conn, "unit_test")

    pages = [
        ([_obs("r1")], {"last_id": 1}),
        ([], {"last_id": 1}),
    ]
    calls = []

    def fake_fetch(cursor):
        calls.append(cursor)
        return pages[len(calls) - 1]

    totals = reconcile_archive(conn, "unit_test", fake_fetch, max_pages=10)

    assert len(calls) == 2
    assert calls[0] == {}
    assert calls[1] == {"last_id": 1}
    assert totals["skipped"] == 1  # no gaia id/name -> unresolved, but still ingested
    assert state.get_reconcile_cursor(conn, "unit_test") == {}, "must wrap back to start once converged"

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM spectroscopy_holdings WHERE archive_code='unit_test' AND archive_obs_id='r1'")
        assert cur.fetchone() is not None, "the record from the one real page must still be upserted"


def test_reconcile_archive_stops_at_page_cap_without_wrapping(conn):
    _clear_sync_state(conn, "unit_test")

    pages = [
        ([_obs("p1")], {"last_id": 1}),
        ([_obs("p2")], {"last_id": 2}),
    ]
    calls = []

    def fake_fetch(cursor):
        calls.append(cursor)
        return pages[len(calls) - 1]

    reconcile_archive(conn, "unit_test", fake_fetch, max_pages=2)

    assert len(calls) == 2
    assert state.get_reconcile_cursor(conn, "unit_test") == {"last_id": 2}, (
        "hitting the page cap mid-walk must leave the cursor where it stopped, not wrap early"
    )


def test_reconcile_archive_goes_sticky_offline_after_gaia_degraded(conn, monkeypatch):
    """Same sticky per-archive fallback as sync.main.sync_archive -- see
    there. A reconcile walk can hit a degraded Gaia TAP+ just as easily as a
    live sync can."""
    _clear_sync_state(conn, "unit_test")

    pages = [{"skipped": 1}, {"skipped": 1}, {"skipped": 0}]
    run_sync_calls = []

    def fake_run_sync(conn_, archive_code, fetch_fn, cursor_kind="sync", offline=False):
        run_sync_calls.append(offline)
        counts = pages[len(run_sync_calls) - 1]
        gaia_degraded = len(run_sync_calls) == 1
        return counts, gaia_degraded

    monkeypatch.setattr(reconcile_module, "run_sync", fake_run_sync)

    reconcile_archive(conn, "unit_test", lambda cursor: (None, None), max_pages=10)

    assert run_sync_calls == [False, True, True]


def test_reconcile_shitty_positional_match_calls_skipped_only_pass(conn, monkeypatch):
    """sync.reconcile only ever runs scripts.shitty_positional_match's cheap
    incremental (skipped_only=True) pass -- see reconcile_shitty_positional_match's
    own docstring for why a full pass doesn't belong on this schedule."""
    calls = []

    def fake_run(conn_, only_archives=None, skipped_only=False):
        calls.append({"only_archives": only_archives, "skipped_only": skipped_only})
        return {"shitty_matched": 0, "no_confident_candidate": 0}

    monkeypatch.setattr(reconcile_module.shitty_positional_match, "run", fake_run)

    reconcile_shitty_positional_match(conn)

    assert calls == [{"only_archives": None, "skipped_only": True}]
