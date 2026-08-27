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
