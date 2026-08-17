from datetime import date

import pytest
from astropy.table import Table

from sync import positional_fallback
from sync.base import RawObservation
from sync.positional_fallback import (
    Candidate,
    MAG_CONTRAST_THRESHOLD_MAG,
    faintness_ceiling_mag,
    pick_best_candidate,
    run_shitty_positional_match,
)


def _cand(sep, mag, source_catalog="gaia", star_id=None, gaia_source_id=1):
    return Candidate(
        star_id=star_id, gaia_source_id=gaia_source_id, source_catalog=source_catalog,
        separation_arcsec=sep, phot_g_mean_mag=mag,
    )


def test_no_candidates_at_all():
    winner, reason = pick_best_candidate("eso", [])
    assert winner is None
    assert "no candidates" in reason


def test_bsc5_candidate_wins_regardless_of_gaia_brightness():
    # A bright BSC5 star has no phot_g_mean_mag at all (see add_bsc_star) --
    # must still win outright over a numerically "bright" Gaia candidate.
    bsc5 = _cand(sep=10.0, mag=None, source_catalog="bsc5", star_id=1)
    gaia = _cand(sep=5.0, mag=8.0, source_catalog="gaia", star_id=2)
    winner, reason = pick_best_candidate("eso", [gaia, bsc5])
    assert winner is bsc5
    assert "bsc5" in reason


def test_sole_candidate_within_ceiling_accepted():
    winner, reason = pick_best_candidate("koa", [_cand(sep=8.0, mag=15.0)])
    assert winner is not None
    assert winner.phot_g_mean_mag == 15.0


def test_sole_candidate_fainter_than_ceiling_rejected():
    # koa's ceiling is 20.4 -- a lone G=21 candidate should not be trusted
    # just because it's the only thing there.
    winner, reason = pick_best_candidate("koa", [_cand(sep=8.0, mag=21.0)])
    assert winner is None
    assert "ceiling" in reason


def test_unknown_archive_uses_default_ceiling():
    from sync.positional_fallback import DEFAULT_FAINTNESS_CEILING_MAG
    assert faintness_ceiling_mag("some_new_archive") == DEFAULT_FAINTNESS_CEILING_MAG


def test_clear_brightness_contrast_wins():
    bright = _cand(sep=10.0, mag=10.0, gaia_source_id=1)
    faint = _cand(sep=12.0, mag=10.0 + MAG_CONTRAST_THRESHOLD_MAG + 0.5, gaia_source_id=2)
    winner, reason = pick_best_candidate("eso", [faint, bright])
    assert winner is bright
    assert "brightness winner" in reason


def test_insufficient_contrast_rejected():
    a = _cand(sep=10.0, mag=10.0, gaia_source_id=1)
    b = _cand(sep=12.0, mag=10.0 + MAG_CONTRAST_THRESHOLD_MAG - 0.5, gaia_source_id=2)
    winner, reason = pick_best_candidate("eso", [a, b])
    assert winner is None
    assert "mag ahead" in reason


def test_proximity_override_blocks_bright_but_far_winner():
    # A much closer, modestly fainter candidate should block a brighter but
    # far-away one from winning outright -- the transient/crowded-field
    # safeguard (see module docstring point 5).
    bright_far = _cand(sep=60.0, mag=10.0, gaia_source_id=1)
    close_faint = _cand(sep=5.0, mag=10.0 + MAG_CONTRAST_THRESHOLD_MAG + 0.5, gaia_source_id=2)
    winner, reason = pick_best_candidate("eso", [bright_far, close_faint])
    assert winner is None
    assert "closer" in reason


def test_close_bright_winner_not_blocked_by_distant_fainter_candidate():
    close_bright = _cand(sep=5.0, mag=10.0, gaia_source_id=1)
    far_faint = _cand(sep=50.0, mag=10.0 + MAG_CONTRAST_THRESHOLD_MAG + 0.5, gaia_source_id=2)
    winner, reason = pick_best_candidate("eso", [close_bright, far_faint])
    assert winner is close_bright


# Integration test: exercises the one path the pure pick_best_candidate unit
# tests above can't reach -- a live-Gaia hit with no existing `stars` row at
# all gets registered as a new star (via ingest.add_star.add_stars_batch)
# before the holding is upserted. Both live Gaia calls involved (the upload
# cross-match here, and add_stars_batch's own batch astrometry lookup) are
# mocked, following the same monkeypatch.setattr(...Gaia, "launch_job", ...)
# pattern test_add_star.py already uses.

NEW_TEST_GAIA_ID = 900000000000500001


class _FakeXmatchJob:
    def get_results(self):
        # ra/dec placed exactly 5" north of the test record's (50.0, 20.0)
        # position, zero proper motion -- separation stays 5" regardless of
        # which epoch it gets propagated to.
        return Table({
            "rec_id": [0],
            "source_id": [NEW_TEST_GAIA_ID],
            "ra": [50.0], "dec": [20.0 + 5.0 / 3600.0],
            "pmra": [0.0], "pmdec": [0.0],
            "phot_g_mean_mag": [10.0],
        })


class _FakeAstrometryJob:
    def get_results(self):
        return Table({
            "source_id": [NEW_TEST_GAIA_ID],
            "ra": [50.001], "dec": [20.001], "ref_epoch": [2016.0],
            "pmra": [0.0], "pmdec": [0.0], "parallax": [10.0],
            "phot_g_mean_mag": [10.0], "phot_bp_mean_mag": [10.5], "phot_rp_mean_mag": [9.5],
            "has_rvs": [False], "has_xp_continuous": [False],
        })


def test_run_shitty_positional_match_discovers_untracked_gaia_star(conn, monkeypatch):
    monkeypatch.setattr(
        positional_fallback, "_launch_gaia_upload_job",
        lambda query, upload, name: _FakeXmatchJob(),
    )
    from ingest import add_star as add_star_module
    monkeypatch.setattr(add_star_module.Gaia, "launch_job", lambda query: _FakeAstrometryJob())

    rec = RawObservation(
        archive_obs_id="shitty-2", archive_url="http://example.test/shitty-2",
        ra=50.0, dec=20.0, obs_date=date(2020, 1, 1),
    )
    counts = run_shitty_positional_match(conn, "unit_test", [rec])
    assert counts["shitty_matched"] == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.gaia_source_id, h.match_method, h.match_status, h.theta_arcsec "
            "FROM spectroscopy_holdings h JOIN stars s ON s.star_id = h.star_id "
            "WHERE h.archive_code='unit_test' AND h.archive_obs_id='shitty-2'"
        )
        gaia_source_id, method, status, theta = cur.fetchone()
    assert gaia_source_id == NEW_TEST_GAIA_ID
    assert method == "shitty_positional_match"
    assert status == "needs_review"  # never 'matched', by design
    assert theta == pytest.approx(5.0)


def test_gaia_queries_are_bucketed_by_decade_not_worst_case_for_whole_chunk(conn, monkeypatch):
    """A chunk spanning two decades should issue one Gaia query per decade
    bucket, each padded only for its own records' epoch spread -- not one
    query padded for the single oldest record in the whole chunk.
    """
    calls = []

    def fake_cone_search(records, radius_arcsec, max_years):
        calls.append((len(records), max_years))
        return {}

    monkeypatch.setattr(positional_fallback, "_gaia_cone_search_batch", fake_cone_search)

    recent = RawObservation(
        archive_obs_id="bucket-recent", archive_url="http://example.test/bucket-recent",
        ra=50.0, dec=20.0, obs_date=date(2020, 1, 1),
    )
    old = RawObservation(
        archive_obs_id="bucket-old", archive_url="http://example.test/bucket-old",
        ra=60.0, dec=30.0, obs_date=date(1986, 1, 1),
    )
    run_shitty_positional_match(conn, "unit_test", [recent, old])

    assert len(calls) == 2, "expected one Gaia query per decade bucket, not one for the whole chunk"
    sizes_and_years = sorted(calls, key=lambda c: c[1])
    (_, small_max_years), (_, big_max_years) = sizes_and_years
    # The recent (2020) record's own padding should be small (~4 years from
    # Gaia's 2016.0 epoch), nowhere near the old (1986) record's ~30 years --
    # the whole point of bucketing is that the recent record's query isn't
    # forced to pay for the old one's much wider drift budget.
    assert small_max_years < 10
    assert big_max_years > 25
