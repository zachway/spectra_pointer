from tests.conftest import WEBAPP_TEST_STAR_1, WEBAPP_TEST_STAR_2


def test_search_by_name_returns_grouped_holdings(client):
    resp = client.get("/?q=TEST+STAR+ONE")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "TESTSPEC" in body
    assert "Webapp Test Archive" in body
    # Both raw holdings show up as dated rows under one grouped (archive,
    # instrument) <details> block -- the "2 observations" summary count
    # confirms _group_holdings folded them together rather than listing two
    # separate groups.
    assert "2 observations</span>" in body
    assert "2024-01-01" in body and "2024-02-01" in body


def test_search_by_source_id_returns_same_star(client):
    resp = client.get(f"/?q={WEBAPP_TEST_STAR_1}")
    assert resp.status_code == 200
    assert "TESTSPEC" in resp.get_data(as_text=True)


def test_search_by_name_is_case_and_whitespace_insensitive(client):
    resp = client.get("/?q=test   star   one")
    assert resp.status_code == 200
    assert "TESTSPEC" in resp.get_data(as_text=True)


def test_search_unresolvable_name_shows_simbad_value_error(client, monkeypatch, webapp_module):
    def _raise(name):
        raise ValueError(f"No SIMBAD match for {name!r}")
    monkeypatch.setattr(webapp_module, "resolve_gaia_source_id", _raise)
    resp = client.get("/?q=NOT+A+REAL+STAR+NAME")
    assert resp.status_code == 200
    assert "No SIMBAD match" in resp.get_data(as_text=True)


def test_search_name_when_simbad_is_down_reports_that_plainly(client, monkeypatch, webapp_module):
    from pyvo.dal import DALServiceError

    def _raise(name):
        raise DALServiceError("simbad unreachable")
    monkeypatch.setattr(webapp_module, "resolve_gaia_source_id", _raise)
    resp = client.get("/?q=SOME+UNTRACKED+NAME")
    assert resp.status_code == 200
    assert "SIMBAD is currently unavailable" in resp.get_data(as_text=True)


def test_search_unknown_source_id_reports_not_tracked(client):
    resp = client.get("/?q=123456789012345678")
    assert resp.status_code == 200
    assert "No tracked star" in resp.get_data(as_text=True)


def test_search_star_with_no_holdings_shows_empty_results(client):
    resp = client.get(f"/?q={WEBAPP_TEST_STAR_2}")
    assert resp.status_code == 200
    assert "No spectroscopy holdings found for this star yet." in resp.get_data(as_text=True)


def test_search_csv_export_contains_holding_rows(client):
    resp = client.get("/?q=TEST+STAR+ONE&format=csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    assert "test-obs-1" not in body  # archive_obs_id isn't one of the exported CSV columns
    assert "TESTSPEC" in body
    assert body.count("\n") >= 3  # header + 2 holding rows (+ trailing newline)


def test_search_blank_query_shows_blank_form(client):
    resp = client.get("/")
    assert resp.status_code == 200


def _webapp_test_holding_id(webapp_module):
    cur = webapp_module.get_cursor()
    cur.execute("SELECT id FROM spectroscopy_holdings WHERE archive_obs_id = 'test-obs-1'")
    return cur.fetchone()[0]


def test_spectrum_page_for_unimplemented_archive_shows_not_implemented_error(client, webapp_module):
    # 'webapp_test' isn't in SUPPORTED_ARCHIVES, so _resolve_spectrum takes
    # its "not implemented" branch -- exercises the page without a real
    # archive file fetch.
    holding_id = _webapp_test_holding_id(webapp_module)
    resp = client.get(f"/spectrum/{holding_id}")
    assert resp.status_code == 200
    # Jinja auto-escapes the apostrophe as &#39; in the rendered HTML.
    assert "Spectrum display isn&#39;t implemented for Webapp Test Archive yet." in resp.get_data(as_text=True)


def test_spectrum_data_json_for_unimplemented_archive(client, webapp_module):
    holding_id = _webapp_test_holding_id(webapp_module)
    resp = client.get(f"/spectrum/{holding_id}/data")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": False,
        "error": "Spectrum display isn't implemented for Webapp Test Archive yet.",
    }


def test_spectrum_page_404s_for_unknown_holding_id(client):
    resp = client.get("/spectrum/999999999")
    assert resp.status_code == 404


def test_stats_redirects_to_leaderboard(client):
    resp = client.get("/stats")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/leaderboard"


def test_citation_page_renders(client):
    resp = client.get("/citation")
    assert resp.status_code == 200
    assert "does not have a citable DOI" in resp.get_data(as_text=True)


def test_status_page_lists_test_archive_with_correct_total(client):
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Webapp Test Archive" in body
    # Both holdings are match_status='matched'/match_method='manual', which
    # ARCHIVE_STATUS_CATEGORIES has no dedicated column for -- they only
    # count toward the row total, not any of the named category columns.
    idx = body.index("Webapp Test Archive")
    row = body[idx:idx + 400]
    assert "2</td>" in row  # Total column


def test_instruments_page_lists_test_instrument(client):
    resp = client.get("/instruments")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Webapp Test Archive" in body
    assert "TESTSPEC" in body


def test_instrument_holdings_csv_streams_rows_for_archive(client):
    resp = client.get("/instrument_holdings.csv?archive_code=webapp_test")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    assert "test-obs-1" in body and "test-obs-2" in body
    assert body.count("\n") == 3  # header + 2 rows (+ trailing newline)


def test_instrument_holdings_csv_404s_for_unknown_archive(client):
    resp = client.get("/instrument_holdings.csv?archive_code=does_not_exist")
    assert resp.status_code == 404


def test_instrument_holdings_csv_400s_with_no_archive_code(client):
    resp = client.get("/instrument_holdings.csv")
    assert resp.status_code == 400


def test_batch_search_by_source_id_reports_tracked_and_untracked(client):
    resp = client.post("/batch", data={
        "names": f"{WEBAPP_TEST_STAR_1}\n{WEBAPP_TEST_STAR_2}\n999999999999999999\n",
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "3 entries looked up." in body
    # star1 has 2 holdings, star2 has 0, and the bogus id was never tracked --
    # all three distinct outcomes appear on one page.
    assert "tracked" in body
    assert "not tracked" in body


def test_batch_search_csv_export(client):
    resp = client.post("/batch", data={
        "names": f"{WEBAPP_TEST_STAR_1}\n",
        "format": "csv",
    })
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    assert "TESTSPEC" in body
    assert body.count("\n") >= 3  # header + 2 holding rows for star1


def test_batch_search_with_no_input_shows_error(client):
    resp = client.post("/batch", data={"names": ""})
    assert resp.status_code == 200
    assert "No names or source_ids found in the upload." in resp.get_data(as_text=True)
