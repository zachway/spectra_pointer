from scripts import shitty_positional_match
from scripts.shitty_positional_match import _page_candidates_by_cell


def test_page_never_splits_a_single_cell():
    by_cell = {
        1: [("a", "1"), ("a", "2"), ("a", "3")],
        2: [("a", "4"), ("a", "5")],
    }
    pages = list(_page_candidates_by_cell(dict(by_cell), page_size=3))
    assert sum(len(p) for p in pages) == 5
    for cell, keys in by_cell.items():
        matches = [p for p in pages if all(k in p for k in keys)]
        assert len(matches) == 1, f"cell {cell} split across pages"


def test_dense_cell_gets_its_own_oversized_page_rather_than_split():
    by_cell = {1: [("a", str(i)) for i in range(10)]}
    pages = list(_page_candidates_by_cell(dict(by_cell), page_size=3))
    assert len(pages) == 1
    assert len(pages[0]) == 10


def test_page_boundaries_respect_page_size_across_cells():
    by_cell = {1: [("a", "1")], 2: [("a", "2")], 3: [("a", "3")], 4: [("a", "4")]}
    pages = list(_page_candidates_by_cell(dict(by_cell), page_size=2))
    assert len(pages) == 2
    assert all(len(p) <= 2 for p in pages)


def test_run_pages_and_aggregates_totals_across_pages(conn, monkeypatch):
    # Two records at very different sky positions -- guaranteed different
    # HEALPix cells at any reasonable level -- and page_size forced to 1 so
    # each candidate is guaranteed its own page.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO spectroscopy_holdings
                (archive_code, archive_obs_id, archive_url, raw_ra, raw_dec, match_method, match_status)
            VALUES
                ('unit_test', 'page-1', 'http://example.test/page-1', 10.0, 10.0, 'positional_easy_match', 'skipped'),
                ('unit_test', 'page-2', 'http://example.test/page-2', 200.0, -60.0, 'positional_easy_match', 'skipped')
            """
        )
    conn.commit()

    monkeypatch.setattr(shitty_positional_match, "CANDIDATE_PAGE_SIZE", 1)

    calls = []

    def fake_run_shitty_positional_match(conn, by_archive):
        calls.append({k: [r.archive_obs_id for r in v] for k, v in by_archive.items()})
        return {"shitty_matched": sum(len(v) for v in by_archive.values())}

    monkeypatch.setattr(shitty_positional_match, "run_shitty_positional_match", fake_run_shitty_positional_match)

    totals = shitty_positional_match.run(conn, only_archives=["unit_test"])

    assert len(calls) == 2  # each candidate got its own page
    seen_ids = {obs_id for call in calls for obs_ids in call.values() for obs_id in obs_ids}
    assert seen_ids == {"page-1", "page-2"}
    assert totals == {"shitty_matched": 2}


def test_skipped_only_excludes_needs_review_rows(conn, monkeypatch):
    """skipped_only=True must only pick up match_status='skipped' -- this
    fallback always moves a row's status to 'needs_review' once it's
    touched it at all (win or lose, see sync.positional_fallback._process_cell),
    so 'skipped' unambiguously means never yet attempted. A 'needs_review'
    row (already attempted before, by this fallback or a human) must be left
    alone by the cheap incremental pass -- only the full (skipped_only=False)
    pass re-checks those."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO spectroscopy_holdings
                (archive_code, archive_obs_id, archive_url, raw_ra, raw_dec, match_method, match_status)
            VALUES
                ('unit_test', 'never-attempted', 'http://example.test/never-attempted', 10.0, 10.0,
                 'positional_easy_match', 'skipped'),
                ('unit_test', 'already-attempted', 'http://example.test/already-attempted', 200.0, -60.0,
                 'shitty_positional_match', 'needs_review')
            """
        )
    conn.commit()

    calls = []

    def fake_run_shitty_positional_match(conn, by_archive):
        calls.append({k: [r.archive_obs_id for r in v] for k, v in by_archive.items()})
        return {"shitty_matched": sum(len(v) for v in by_archive.values())}

    monkeypatch.setattr(shitty_positional_match, "run_shitty_positional_match", fake_run_shitty_positional_match)

    shitty_positional_match.run(conn, only_archives=["unit_test"], skipped_only=True)

    seen_ids = {obs_id for call in calls for obs_ids in call.values() for obs_id in obs_ids}
    assert seen_ids == {"never-attempted"}, "skipped_only must not re-touch an already-attempted needs_review row"


def test_full_pass_includes_both_skipped_and_needs_review_rows(conn, monkeypatch):
    """The default (skipped_only=False) full pass is the one that's allowed
    to re-check rows this fallback already attempted before -- e.g. because
    a newly-tracked star (from some other archive) might now make an old
    miss matchable."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO spectroscopy_holdings
                (archive_code, archive_obs_id, archive_url, raw_ra, raw_dec, match_method, match_status)
            VALUES
                ('unit_test', 'never-attempted-2', 'http://example.test/never-attempted-2', 10.0, 10.0,
                 'positional_easy_match', 'skipped'),
                ('unit_test', 'already-attempted-2', 'http://example.test/already-attempted-2', 200.0, -60.0,
                 'shitty_positional_match', 'needs_review')
            """
        )
    conn.commit()

    calls = []

    def fake_run_shitty_positional_match(conn, by_archive):
        calls.append({k: [r.archive_obs_id for r in v] for k, v in by_archive.items()})
        return {"shitty_matched": sum(len(v) for v in by_archive.values())}

    monkeypatch.setattr(shitty_positional_match, "run_shitty_positional_match", fake_run_shitty_positional_match)

    shitty_positional_match.run(conn, only_archives=["unit_test"])

    seen_ids = {obs_id for call in calls for obs_ids in call.values() for obs_id in obs_ids}
    assert seen_ids == {"never-attempted-2", "already-attempted-2"}
