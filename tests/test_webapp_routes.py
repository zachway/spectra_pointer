import io

import pytest

from tests.conftest import WEBAPP_TEST_ARCHIVE_CODE, WEBAPP_TEST_ARCHIVE_CODE_B, WEBAPP_TEST_STAR_1, WEBAPP_TEST_STAR_2


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


def _fake_spectrum_result(continuum_normalized):
    return {
        "wavelength_unit": "Å", "flux_unit": "arbitrary", "flux_unit_family": "arbitrary",
        "flux_scale_factor": 2.0, "continuum_normalized": continuum_normalized,
        "segments": [{"label": "x", "wavelength": [1.0, 2.0], "flux": [1.0, 1.0], "uncertainty": None}],
    }


def test_spectrum_page_says_so_when_requested_continuum_fit_failed(client, webapp_module, monkeypatch):
    """A failed fit used to fall back to the median wording with no mention
    that the continuum fit (which the user had asked for) hadn't applied."""
    holding_id = _webapp_test_holding_id(webapp_module)
    monkeypatch.setattr(
        webapp_module, "_resolve_spectrum", lambda holding: {"ok": True, "result": _fake_spectrum_result(False)}
    )
    body = client.get(f"/spectrum/{holding_id}?continuum=1").get_data(as_text=True)
    assert "continuum fit was requested but failed for this spectrum" in body
    # ...and without the request, the plain median wording stays as before
    plain = client.get(f"/spectrum/{holding_id}").get_data(as_text=True)
    assert "requested but failed" not in plain
    assert "Show continuum-normalized flux" in plain


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
    assert "10.5281/zenodo.22698835" in resp.get_data(as_text=True)


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


@pytest.mark.parametrize("header", ["source_id", "Gaia Source ID", '"source_id"', "﻿source_id", "name"])
def test_batch_search_skips_column_header_row(client, monkeypatch, webapp_module, header):
    # Issue #225: a CSV's header row was looked up as if it were a star name.
    # SIMBAD must never be asked about it.
    def fail_if_called(names):
        raise AssertionError(f"header row sent to SIMBAD: {names}")
    monkeypatch.setattr(webapp_module,"resolve_stellar_gaia_ids_batch", fail_if_called)

    resp = client.post("/batch", data={"names": f"{header}\n{WEBAPP_TEST_STAR_1}\n{WEBAPP_TEST_STAR_2}\n"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "2 entries looked up." in body
    assert "looked like a column header and was skipped" in body
    assert "not resolved via SIMBAD" not in body


def test_batch_search_header_row_from_uploaded_file(client, monkeypatch, webapp_module):
    monkeypatch.setattr(webapp_module,"resolve_stellar_gaia_ids_batch", lambda names: {})
    resp = client.post("/batch", data={
        "file": (io.BytesIO(f"﻿source_id\r\n{WEBAPP_TEST_STAR_1}\r\n".encode("utf-8")), "ids.csv"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "1 entries looked up." in body
    assert "not resolved via SIMBAD" not in body


def test_batch_search_keeps_real_names_and_later_header_like_lines(client, monkeypatch, webapp_module):
    # Only a recognised label on the *first* line is dropped: a real star
    # name up top, or a header-like word further down, is still looked up.
    asked = []

    def fake_resolve(names):
        asked.extend(names)
        return {}
    monkeypatch.setattr(webapp_module,"resolve_stellar_gaia_ids_batch", fake_resolve)

    resp = client.post("/batch", data={"names": "Vega\nsource_id\n"})
    assert resp.status_code == 200
    assert asked == ["Vega", "source_id"]
    assert "looked like a column header" not in resp.get_data(as_text=True)


def test_batch_search_with_only_a_header_row_shows_error(client):
    resp = client.post("/batch", data={"names": "source_id\n"})
    assert resp.status_code == 200
    assert "No names or source_ids found in the upload." in resp.get_data(as_text=True)


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


def _csv_data_rows(body):
    return [line for line in body.strip().splitlines()[1:] if line]


def test_batch_search_filters_by_one_picked_archive(client):
    # star1 has 2 TESTSPEC holdings in WEBAPP_TEST_ARCHIVE_CODE and 1
    # OTHERSPEC holding in WEBAPP_TEST_ARCHIVE_CODE_B -- only the latter passes.
    resp = client.post("/batch", data={
        "names": f"{WEBAPP_TEST_STAR_1}\n",
        "adv_source": WEBAPP_TEST_ARCHIVE_CODE_B,
        "format": "csv",
    })
    rows = _csv_data_rows(resp.get_data(as_text=True))
    assert len(rows) == 1
    assert "OTHERSPEC" in rows[0]


def test_batch_search_matches_any_of_several_picks(client):
    # An archive-level pick and an instrument-level pick from a different
    # archive: holdings from either count.
    resp = client.post("/batch", data={
        "names": f"{WEBAPP_TEST_STAR_1}\n",
        "adv_source": [f"{WEBAPP_TEST_ARCHIVE_CODE}::TESTSPEC", WEBAPP_TEST_ARCHIVE_CODE_B],
        "format": "csv",
    })
    assert len(_csv_data_rows(resp.get_data(as_text=True))) == 3


def test_batch_search_instrument_pick_is_scoped_to_its_archive(client):
    resp = client.post("/batch", data={
        "names": f"{WEBAPP_TEST_STAR_1}\n",
        "adv_source": f"{WEBAPP_TEST_ARCHIVE_CODE}::TESTSPEC",
    })
    body = resp.get_data(as_text=True)
    assert "Webapp Test Archive — TESTSPEC" in body
    results_table = body.split("<table>", 1)[1].split("</table>", 1)[0]
    assert "OTHERSPEC" not in results_table


def test_batch_search_ignores_unknown_picks(client):
    resp = client.post("/batch", data={
        "names": f"{WEBAPP_TEST_STAR_1}\n",
        "adv_source": "no_such_archive::NOPE",
    })
    body = resp.get_data(as_text=True)
    assert "Matched holdings" not in body  # no valid filter, so no filtered column


def test_batch_results_offer_csv_download_above_table(client):
    resp = client.post("/batch", data={
        "names": f"{WEBAPP_TEST_STAR_1}\n{WEBAPP_TEST_STAR_2}\n",
        "adv_source": WEBAPP_TEST_ARCHIVE_CODE_B,
        "adv_reduction": "reduced",
    })
    body = resp.get_data(as_text=True)
    download = body.index('class="batch-download"')
    assert download < body.index("<th>Query</th>")
    form = body[download:body.index("</form>", download)]
    assert f"{WEBAPP_TEST_STAR_1}\n{WEBAPP_TEST_STAR_2}</textarea>" in form
    assert f'name="adv_source" value="{WEBAPP_TEST_ARCHIVE_CODE_B}"' in form
    assert 'name="adv_reduction" value="reduced"' in form
    assert 'name="format" value="csv"' in form


def test_advanced_panel_lists_picks_and_renders_on_batch_tab(client):
    resp = client.post("/batch", data={
        "names": f"{WEBAPP_TEST_STAR_1}\n",
        "adv_source": f"{WEBAPP_TEST_ARCHIVE_CODE}::TESTSPEC",
    })
    body = resp.get_data(as_text=True)
    # Rendered inside the batch tab's slot, with the pick kept in the list.
    batch_tab = body[body.index('id="tab-batch"'):body.index('id="tab-instrument"')]
    assert 'id="advanced-search"' in batch_tab
    assert f'name="adv_source" value="{WEBAPP_TEST_ARCHIVE_CODE}::TESTSPEC" form="star-form"' in batch_tab
    assert body.count('id="advanced-search"') == 1


def test_star_search_page_offers_archive_and_instrument_picks(client):
    body = client.get("/").get_data(as_text=True)
    assert f'<option value="{WEBAPP_TEST_ARCHIVE_CODE}">' in body
    assert f'<option value="{WEBAPP_TEST_ARCHIVE_CODE}::TESTSPEC">' in body


def test_star_search_csv_link_carries_every_pick(client):
    resp = client.get(
        f"/?q={WEBAPP_TEST_STAR_1}&adv_source={WEBAPP_TEST_ARCHIVE_CODE}::TESTSPEC&adv_source={WEBAPP_TEST_ARCHIVE_CODE_B}"
    )
    body = resp.get_data(as_text=True)
    assert "Showing 3 of 3 observations" in body
    assert (f"adv_source={WEBAPP_TEST_ARCHIVE_CODE}%3A%3ATESTSPEC&amp;adv_source={WEBAPP_TEST_ARCHIVE_CODE_B}"
            in body)


def test_batch_search_with_no_input_shows_error(client):
    resp = client.post("/batch", data={"names": ""})
    assert resp.status_code == 200
    assert "No names or source_ids found in the upload." in resp.get_data(as_text=True)


def test_overlap_search_archive_vs_archive_lists_only_shared_star(client):
    resp = client.get("/overlap?s=webapp_test&s=webapp_test_b")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<strong>1</strong> star with matched holdings in" in body
    assert "both <strong>Webapp Test Archive</strong> and <strong>Webapp Test Archive B</strong>" in body
    assert "TEST STAR ONE" in body
    assert "TEST STAR THREE" not in body  # only in archive B
    # star1: 2 observations in A, 1 in B
    assert "<td>2</td><td>1</td>" in body


def test_overlap_search_old_two_side_links_still_work(client):
    body = client.get("/overlap?a=webapp_test&b=webapp_test_b").get_data(as_text=True)
    assert "<strong>1</strong> star with matched holdings in" in body
    assert "TEST STAR ONE" in body


def test_overlap_search_instrument_vs_archive(client):
    resp = client.get("/overlap?s=webapp_test%3A%3ATESTSPEC&s=webapp_test_b")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Webapp Test Archive — TESTSPEC" in body
    assert "TEST STAR ONE" in body


def test_overlap_search_selects_stay_selected_and_tab_active(client):
    body = client.get("/overlap?s=webapp_test&s=webapp_test_b%3A%3AOTHERSPEC").get_data(as_text=True)
    assert '<option value="webapp_test" selected>' in body
    assert '<option value="webapp_test_b::OTHERSPEC" selected>' in body
    assert '<div id="tab-overlap" class="search-tab-panel">' in body  # not hidden
    assert '<details id="overlap-advanced">' in body  # plain two-way search leaves Advanced closed


def test_overlap_search_rejects_duplicate_unknown_and_single_values(client):
    body = client.get("/overlap?s=webapp_test&s=webapp_test").get_data(as_text=True)
    assert "Each archive or instrument can only be picked once." in body
    body = client.get("/overlap?s=webapp_test&s=not_an_archive").get_data(as_text=True)
    assert "Unknown archive or instrument" in body
    body = client.get("/overlap?s=webapp_test&s=").get_data(as_text=True)
    assert "Pick at least two archives or instruments." in body


def test_overlap_search_three_way_all_requires_every_side(client):
    body = client.get(
        "/overlap?s=webapp_test&s=webapp_test_b&s=webapp_test_c&match=all"
    ).get_data(as_text=True)
    assert "<strong>0</strong> stars with matched holdings in" in body
    assert "all of:" in body
    assert '<details id="overlap-advanced" open>' in body  # 3 sides re-opens Advanced
    assert "TEST STAR ONE" not in body and "TEST STAR THREE" not in body


def test_overlap_search_three_way_pairs_finds_any_matching_pair(client):
    body = client.get(
        "/overlap?s=webapp_test&s=webapp_test_b&s=webapp_test_c&match=pairs"
    ).get_data(as_text=True)
    assert "<strong>2</strong> stars with matched holdings in" in body
    assert "at least two of:" in body
    assert "TEST STAR ONE" in body and "TEST STAR THREE" in body
    assert "<td>2 of 3</td>" in body
    assert '<input type="radio" name="match" value="pairs" checked>' in body
    # star1 (A=2, B=1, C=0) sorts ahead of star3 (A=0, B=1, C=1) on combined count
    assert body.index("TEST STAR ONE") < body.index("TEST STAR THREE")
    assert "<td>2</td><td>1</td><td>—</td>" in body


def test_overlap_search_page_past_the_end(client):
    body = client.get("/overlap?s=webapp_test&s=webapp_test_b&page=5").get_data(as_text=True)
    assert "No results on this page." in body
    assert 'href="/overlap?s=webapp_test&amp;s=webapp_test_b&amp;match=all"' in body


def test_overlap_search_csv_export(client):
    resp = client.get("/overlap?s=webapp_test&s=webapp_test_b&format=csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    lines = resp.get_data(as_text=True).strip().splitlines()
    assert lines[0] == (
        "star_id,gaia_source_id,bsc_hr_number,known_as,ra,dec,phot_g_mean_mag,n_sides_matched,"
        "n_obs_webapp_test,n_obs_webapp_test_b"
    )
    assert len(lines) == 2
    assert f",{WEBAPP_TEST_STAR_1}," in lines[1]
    assert lines[1].endswith(",2,2,1")


def test_overlap_search_csv_export_three_way_pairs(client):
    resp = client.get("/overlap?s=webapp_test&s=webapp_test_b&s=webapp_test_c&match=pairs&format=csv")
    lines = resp.get_data(as_text=True).strip().splitlines()
    assert lines[0].endswith(",n_obs_webapp_test,n_obs_webapp_test_b,n_obs_webapp_test_c")
    assert len(lines) == 3
    assert lines[1].endswith(",2,2,1,0") and lines[2].endswith(",2,0,1,1")


def test_archive_data_releases_match_sync_modules(webapp_module):
    # Drift guard: each /status release label must still appear in the sync
    # module it describes, so bumping a module to a new release without
    # updating ARCHIVE_DATA_RELEASES fails here instead of mislabeling live.
    import pathlib
    import re
    archives_dir = pathlib.Path(__file__).resolve().parent.parent / "sync" / "archives"
    for code, release in webapp_module.ARCHIVE_DATA_RELEASES.items():
        src = (archives_dir / f"{code}.py").read_text()
        assert re.search(rf"\b{release}\b", src, re.IGNORECASE), (code, release)


def test_star_page_and_csv_show_program_id(client):
    body = client.get("/?q=TEST+STAR+ONE").get_data(as_text=True)
    assert ">Program</th>" in body
    assert "<td>TEST-PROG-0001</td>" in body
    csv_body = client.get("/?q=TEST+STAR+ONE&format=csv").get_data(as_text=True)
    assert "program_id" in csv_body.splitlines()[0]
    assert "TEST-PROG-0001" in csv_body


def test_batch_csv_export_includes_program_id(client):
    resp = client.post("/batch", data={"names": str(WEBAPP_TEST_STAR_1), "format": "csv"})
    body = resp.get_data(as_text=True)
    assert "program_id" in body.splitlines()[0]
    assert "TEST-PROG-0001" in body


def test_unmatched_radial_search_shows_program_id(client):
    query = "/?ra=10.68458&dec=41.26906&radius=1&search_unmatched=1"
    assert "<td>TEST-PROG-0001</td>" in client.get(query).get_data(as_text=True)
    csv_body = client.get(query + "&format=csv").get_data(as_text=True)
    assert "program_id" in csv_body.splitlines()[0]
    assert "TEST-PROG-0001" in csv_body


def test_advanced_panel_explains_how_min_max_ranges_match(client):
    for resp in (client.get("/"), client.post("/batch", data={"names": str(WEBAPP_TEST_STAR_1)})):
        body = resp.get_data(as_text=True)
        assert "hover/click for details" in body
        assert "published range overlaps yours at all, endpoints included" in body


def test_cmd_page_defaults_to_all_stars_preset(client):
    resp = client.get("/cmd")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "All stars with matched spectra:" in body
    assert "TEST STAR ONE" in body
    assert "GSP-Phot" in body
    # Unknown preset/view values fall back rather than erroring.
    assert "All stars with matched spectra:" in client.get("/cmd?preset=nope&view=nope").get_data(as_text=True)


def test_cmd_archive_preset_counts_stars_with_and_without_photometry(client):
    # star1 (full photometry) and star3 (no BP/RP, no GSP-Phot) are both in
    # archive B -- both counted, only star1 placeable.
    body = client.get(f"/cmd?preset=archive:{WEBAPP_TEST_ARCHIVE_CODE_B}").get_data(as_text=True)
    assert "Webapp Test Archive B:" in body
    assert "2 stars with matched spectra" in body
    assert "1 of them with the Gaia photometry" in body
    assert 'const labels = ["TEST STAR ONE"]' in body


def test_cmd_picker_is_archive_then_instrument(client):
    archive_key = f"archive:{WEBAPP_TEST_ARCHIVE_CODE}"
    inst_key = f"inst:{WEBAPP_TEST_ARCHIVE_CODE}::TESTSPEC"

    # No archive chosen: archives are listed, instruments are not, and the
    # instrument row is hidden.
    body = client.get("/cmd").get_data(as_text=True)
    assert f'<option value="{archive_key}">Webapp Test Archive</option>' in body
    assert f'<option value="{inst_key}"' not in body
    assert '<label id="cmd-inst-row" hidden>' in body

    # Archive chosen: its instruments are offered, none selected.
    body = client.get(f"/cmd?preset={archive_key}").get_data(as_text=True)
    assert "Webapp Test Archive:" in body
    assert f'<option value="{archive_key}" selected>' in body
    assert f'<option value="{inst_key}">TESTSPEC</option>' in body
    assert '<label id="cmd-inst-row">' in body

    # Archive plus one of its instruments narrows to that instrument; the
    # first dropdown still shows the archive.
    for url in (f"/cmd?preset={archive_key}&inst={inst_key}", f"/cmd?preset={inst_key}"):
        body = client.get(url).get_data(as_text=True)
        assert f'<option value="{archive_key}" selected>' in body, url
        assert f'<option value="{inst_key}" selected>TESTSPEC</option>' in body, url
        assert "preset=inst%3A" in body, url  # the CSV link is for the instrument

    # An instrument left over from a different archive is ignored.
    body = client.get(f"/cmd?preset=archive:{WEBAPP_TEST_ARCHIVE_CODE_B}&inst={inst_key}").get_data(as_text=True)
    assert "Webapp Test Archive B:" in body
    assert f'<option value="{inst_key}" selected>' not in body


def test_cmd_instrument_and_resolution_class_presets(client, webapp_module):
    presets = webapp_module._cmd_presets()
    # TESTSPEC is R ~ 80,000 at 400-700 nm for the export (see conftest).
    assert f"inst:{WEBAPP_TEST_ARCHIVE_CODE}::TESTSPEC" in presets
    for key in ("class:high:any", "class:veryhigh:any", "class:veryhigh:optical", "class:any:optical"):
        assert presets[key]["n_stars"] == 1, key
    for key in ("class:low:any", "class:medium:any", "class:any:infrared", "class:veryhigh:infrared"):
        assert key not in presets, key
    body = client.get("/cmd?preset=class:veryhigh:optical").get_data(as_text=True)
    assert "Very high resolution (R ≥ 70,000), optical (380–1,000 nm):" in body
    assert "1 star with matched spectra" in body


def test_cmd_teff_logg_view_and_parameter_filters(client):
    body = client.get("/cmd?view=kiel").get_data(as_text=True)
    assert "const teff = [5200.0]" in body
    body = client.get("/cmd?view=kiel&teff_min=5000&teff_max=5500&logg_min=4").get_data(as_text=True)
    assert "1 match your filters" in body
    assert "const teff = [5200.0]" in body
    body = client.get("/cmd?teff_min=6000").get_data(as_text=True)
    assert "0 match your filters" in body
    assert '<div id="cmd-plot">' not in body


def test_cmd_page_works_against_snapshot_without_preset_tables(spectra_data_dir, tmp_path):
    # A snapshot exported before cmd_presets/cmd_preset_stars existed must
    # not stop the app from starting (webapp.app._make_connection's
    # fallback). Own process: webapp.app opens its data source at import.
    import os
    import shutil
    import subprocess
    import sys
    for name in os.listdir(spectra_data_dir):
        if name not in ("cmd_presets.parquet", "cmd_preset_stars.parquet"):
            shutil.copy(os.path.join(spectra_data_dir, name), tmp_path / name)
    script = (
        "from webapp import app as m\n"
        "body = m.app.test_client().get('/cmd').get_data(as_text=True)\n"
        "assert 'Most-observed tracked stars:' in body, body[:2000]\n"
        "assert 'TEST STAR ONE' in body\n"
    )
    env = {**os.environ, "SPECTRA_DATA_DIR": str(tmp_path)}
    env.pop("SPECTRA_DATA_URL", None)
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-3000:]


def test_cmd_csv_export_lists_stars_with_gaia_parameters(client):
    resp = client.get("/cmd?preset=all&format=csv")
    assert resp.mimetype == "text/csv"
    lines = resp.get_data(as_text=True).splitlines()
    assert lines[0] == (
        "gaia_source_id,known_as,phot_g_mean_mag,bp_rp,abs_g_mag,teff_gspphot,logg_gspphot,mh_gspphot,n_obs"
    )
    row = next(line for line in lines if line.startswith(str(WEBAPP_TEST_STAR_1)))
    assert "5200.0" in row
    assert row.endswith(",3")  # 2 holdings in archive A + 1 in archive B
    assert len(client.get("/cmd?teff_min=6000&format=csv").get_data(as_text=True).splitlines()) == 1
