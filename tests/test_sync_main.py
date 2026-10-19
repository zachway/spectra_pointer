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


def test_sync_archive_retries_a_transient_page_error(conn, monkeypatch):
    # ing/noirlab, 2026-10-17: one read timeout failed the whole weekly sync
    # for that archive -- sync.main had no page retry, only sync.reconcile did.
    import requests

    import sync.retry as retry_module

    monkeypatch.setattr(retry_module, "RETRY_BACKOFF_SECONDS", 0)
    calls = []

    def flaky_run_sync(conn_, archive_code, fetch_fn, offline=False):
        calls.append(offline)
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("slow")
        return {"skipped": 0}, False

    monkeypatch.setattr(sync_main_module, "run_sync", flaky_run_sync)

    sync_archive(conn, "unit_test", lambda cursor: (None, None), max_pages=1)

    assert len(calls) == 2, "the failed page must be retried once"


def _archives_synced_by_main(monkeypatch, argv):
    synced = []
    monkeypatch.setattr(
        sync_main_module, "sync_archive", lambda conn, code, fetch_fn, max_pages, offline=False: synced.append(code)
    )
    monkeypatch.setattr(sync_main_module, "reconcile_eso_raw", lambda conn: 0)
    monkeypatch.setattr("sys.argv", ["sync.main", *argv])
    sync_main_module.main()
    return synced


def test_a_run_with_no_only_skips_paused_archives(conn, monkeypatch):
    monkeypatch.setattr(sync_main_module, "PAUSED_ARCHIVES", {"aat": "waiting"})
    synced = _archives_synced_by_main(monkeypatch, [])
    assert "aat" not in synced
    assert "aat_2df" in synced and "eso" in synced


def test_only_still_runs_a_paused_archive(conn, monkeypatch):
    monkeypatch.setattr(sync_main_module, "PAUSED_ARCHIVES", {"aat": "waiting"})
    assert _archives_synced_by_main(monkeypatch, ["--only", "aat"]) == ["aat"]


def test_the_aat_archives_are_paused():
    # Not to be crawled by the weekly cron until Data Central replies --
    # see PAUSED_ARCHIVES. Delete this test with those entries.
    assert {"aat", "aat_2df"} <= set(sync_main_module.PAUSED_ARCHIVES)
    assert not {"aat", "aat_2df"} & set(sync_main_module.default_archive_codes())
