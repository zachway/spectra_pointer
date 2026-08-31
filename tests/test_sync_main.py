import sync.main as sync_main_module
from sync.main import sync_archive


def test_sync_archive_goes_sticky_offline_after_gaia_degraded(conn, monkeypatch):
    """Once one page's Gaia TAP astrometry lookup exhausts its retries
    (ingest.add_star.AddStarsResult.gaia_degraded, surfaced here via
    sync.runner.run_sync's second return value), every later page of the
    same archive's sync should start offline too instead of paying the same
    multi-minute retry-then-fail cost again on every remaining page -- see
    sync.main.sync_archive's docstring comment on the switch."""
    pages = [
        {"skipped": 1},
        {"skipped": 1},
        {"skipped": 0},
    ]
    run_sync_calls = []

    def fake_run_sync(conn_, archive_code, fetch_fn, offline=False):
        run_sync_calls.append(offline)
        counts = pages[len(run_sync_calls) - 1]
        gaia_degraded = len(run_sync_calls) == 1  # only the first page degrades
        return counts, gaia_degraded

    monkeypatch.setattr(sync_main_module, "run_sync", fake_run_sync)

    totals = sync_archive(conn, "unit_test", lambda cursor: (None, None), max_pages=10)

    assert run_sync_calls == [False, True, True], (
        "must start online, then stay offline for every page after the first degraded one"
    )
    assert totals == {"skipped": 2}


def test_sync_archive_manual_offline_override_starts_offline_from_page_one(conn, monkeypatch):
    run_sync_calls = []

    def fake_run_sync(conn_, archive_code, fetch_fn, offline=False):
        run_sync_calls.append(offline)
        return {"skipped": 0}, False

    monkeypatch.setattr(sync_main_module, "run_sync", fake_run_sync)

    sync_archive(conn, "unit_test", lambda cursor: (None, None), max_pages=10, offline=True)

    assert run_sync_calls == [True]
