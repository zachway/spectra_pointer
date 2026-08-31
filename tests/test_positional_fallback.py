from datetime import date

import pytest
from astropy.table import Table

from sync import positional_fallback
from sync.base import RawObservation
from sync.positional_fallback import (
    Candidate,
    EPOCH_PROPAGATION_CHUNK_SIZE,
    GAIA_HEALPIX_LEVEL,
    MAG_CONTRAST_THRESHOLD_MAG,
    PROPAGATION_ARRAY_ELEMENT_BUDGET,
    _epoch_chunk_size,
    _healpix_cell,
    _healpix_cell_and_ring,
    _healpix_source_id_range,
    faintness_ceiling_mag,
    pick_best_candidate,
    run_shitty_positional_match,
)


def _insert_star(cur, gaia_source_id, ra, dec):
    cur.execute(
        """
        INSERT INTO stars (gaia_source_id, ra, dec, ref_epoch, pmra, pmdec)
        VALUES (%s, %s, %s, 2016.0, 0, 0)
        ON CONFLICT (gaia_source_id) DO UPDATE SET ra = EXCLUDED.ra, dec = EXCLUDED.dec
        """,
        (gaia_source_id, ra, dec),
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


# -- HEALPix helpers -----------------------------------------------------
# See sync/positional_fallback.py's module docstring: source_id's high bits
# encode a nested-scheme HEALPix pixel per ESA's own documentation
# (GAIA-C3-TN-ARI-BAS-020), so these are checked against that encoding
# directly rather than against any live Gaia data.

def test_healpix_source_id_range_matches_documented_encoding():
    shift = 35 + 2 * (12 - GAIA_HEALPIX_LEVEL)
    lo, hi = _healpix_source_id_range(0)
    assert lo == 0
    assert hi == (1 << shift) - 1

    lo2, hi2 = _healpix_source_id_range(1)
    assert lo2 == hi + 1  # pixel 1's range starts right after pixel 0's ends


def test_healpix_cell_and_ring_includes_self_and_neighbours():
    pix = _healpix_cell(200.0, 30.0)
    ring = _healpix_cell_and_ring(pix)
    assert pix in ring
    assert len(ring) >= 5  # itself + most of a ring of 8 (poles can have fewer)


def test_healpix_cell_is_stable_for_nearby_points():
    # Two points a few arcsec apart, nowhere near a pixel boundary, must
    # land in the same cell -- this is the whole basis for batching Gaia
    # round trips by cell instead of by record.
    assert _healpix_cell(150.0, 10.0) == _healpix_cell(150.0001, 10.0001)


# -- Integration test: exercises the one path the pure pick_best_candidate
# unit tests above can't reach -- a live-Gaia hit with no existing `stars`
# row at all gets registered as a new star (via ingest.add_star.
# add_stars_batch) before the holding is upserted. Both live Gaia calls
# involved (the HEALPix pool fetch here, and add_stars_batch's own batch
# astrometry lookup) are mocked, following the same
# monkeypatch.setattr(...Gaia, "launch_job", ...) pattern test_add_star.py
# already uses.

NEW_TEST_GAIA_ID = 900000000000500001


class _FakeHealpixPoolJob:
    def get_results(self):
        # ra/dec placed exactly 5" north of the test record's (50.0, 20.0)
        # position, zero proper motion -- separation stays 5" regardless of
        # which epoch it gets propagated to.
        return Table({
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
        positional_fallback, "_launch_gaia_job",
        lambda query: _FakeHealpixPoolJob(),
    )
    from ingest import add_star as add_star_module
    monkeypatch.setattr(add_star_module.Gaia, "launch_job", lambda query: _FakeAstrometryJob())

    rec = RawObservation(
        archive_obs_id="shitty-2", archive_url="http://example.test/shitty-2",
        ra=50.0, dec=20.0, obs_date=date(2020, 1, 1),
    )
    counts = run_shitty_positional_match(conn, {"unit_test": [rec]})
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


class _FakePhaseJob:
    """Simulates a real astroquery Job's phase transitions for testing
    _launch_gaia_job's polling loop without touching the network."""

    def __init__(self, phases, jobid="fake-job-1"):
        self._phases = list(phases)
        self.jobid = jobid
        self._phase = "EXECUTING"

    def is_finished(self):
        return self._phase in ("ERROR", "ABORTED", "COMPLETED")

    def get_phase(self, update=False):
        if update and self._phases:
            self._phase = self._phases.pop(0)
        return self._phase

    def get_results(self):
        return Table({"source_id": [], "ra": [], "dec": [], "pmra": [], "pmdec": [], "phot_g_mean_mag": []})


def test_launch_gaia_job_polls_until_completed(monkeypatch):
    job = _FakePhaseJob(["QUEUED", "EXECUTING", "COMPLETED"])
    monkeypatch.setattr(
        positional_fallback.Gaia, "launch_job_async",
        lambda query, background: job,
    )
    sleeps = []
    monkeypatch.setattr(positional_fallback.time, "sleep", lambda s: sleeps.append(s))

    result = positional_fallback._launch_gaia_job("SELECT 1")

    assert result is job
    assert len(sleeps) == 2  # polled between QUEUED->EXECUTING and EXECUTING->COMPLETED


def test_launch_gaia_job_raises_on_error_phase(monkeypatch):
    job = _FakePhaseJob(["QUEUED", "ERROR"])
    monkeypatch.setattr(
        positional_fallback.Gaia, "launch_job_async",
        lambda query, background: job,
    )
    monkeypatch.setattr(positional_fallback.time, "sleep", lambda s: None)
    monkeypatch.setattr(positional_fallback, "GAIA_LAUNCH_JOB_ATTEMPTS", 1)

    with pytest.raises(RuntimeError, match="ERROR"):
        positional_fallback._launch_gaia_job("SELECT 1")


# -- Cell-batching behavior -----------------------------------------------
# Replaces the old chunk/epoch/sky-bucket tests: the unit of Gaia work is now
# a HEALPix cell, shared across every archive's records that land in it, not
# a per-archive per-decade per-RA/Dec-grid batch.

def test_records_far_apart_on_sky_use_separate_healpix_pool_queries(conn, monkeypatch):
    calls = []

    def fake_pool(pixels):
        calls.append(tuple(pixels))
        return []

    monkeypatch.setattr(positional_fallback, "_gaia_healpix_pool", fake_pool)

    a = RawObservation(
        archive_obs_id="cell-a", archive_url="http://example.test/cell-a",
        ra=10.0, dec=10.0, obs_date=date(2020, 1, 1),
    )
    b = RawObservation(
        archive_obs_id="cell-b", archive_url="http://example.test/cell-b",
        ra=200.0, dec=-40.0, obs_date=date(2020, 1, 1),  # far side of the sky
    )
    run_shitty_positional_match(conn, {"unit_test": [a, b]})

    assert len(calls) == 2, "records in unrelated HEALPix cells should not share a pool fetch"


def test_nearby_records_share_one_healpix_pool_query(conn, monkeypatch):
    calls = []

    def fake_pool(pixels):
        calls.append(pixels)
        return []

    monkeypatch.setattr(positional_fallback, "_gaia_healpix_pool", fake_pool)

    a = RawObservation(
        archive_obs_id="near-a", archive_url="http://example.test/near-a",
        ra=10.05, dec=10.05, obs_date=date(2020, 1, 1),
    )
    b = RawObservation(
        archive_obs_id="near-b", archive_url="http://example.test/near-b",
        ra=10.06, dec=10.06, obs_date=date(2020, 3, 1),  # different epoch, same cell
    )
    run_shitty_positional_match(conn, {"unit_test": [a, b]})

    assert len(calls) == 1, "records a few arcsec apart should land in the same HEALPix cell"


def test_records_from_different_archives_in_same_cell_share_one_query(conn, monkeypatch):
    calls = []

    def fake_pool(pixels):
        calls.append(pixels)
        return []

    monkeypatch.setattr(positional_fallback, "_gaia_healpix_pool", fake_pool)

    a = RawObservation(
        archive_obs_id="arc-a", archive_url="http://example.test/arc-a",
        ra=10.05, dec=10.05, obs_date=date(2020, 1, 1),
    )
    b = RawObservation(
        archive_obs_id="arc-b", archive_url="http://example.test/arc-b",
        ra=10.06, dec=10.06, obs_date=date(2020, 1, 1),
    )
    run_shitty_positional_match(conn, {"unit_test": [a], "eso_raw": [b]})

    assert len(calls) == 1, "a HEALPix cell's pool fetch should be shared across archives"


def test_all_cells_processed_when_more_cells_than_fetch_concurrency(conn, monkeypatch):
    """Fetches are pipelined GAIA_FETCH_CONCURRENCY at a time (see
    run_shitty_positional_match) -- with more occupied cells than that
    window, every cell must still get fetched and committed, not just the
    first batch submitted up front."""
    import threading

    calls = []
    lock = threading.Lock()

    def fake_pool(pixels):
        with lock:
            calls.append(tuple(pixels))
        return []

    monkeypatch.setattr(positional_fallback, "_gaia_healpix_pool", fake_pool)
    monkeypatch.setattr(positional_fallback, "GAIA_FETCH_CONCURRENCY", 2)

    # 5 widely separated records -> 5 distinct HEALPix cells, well beyond
    # the concurrency window of 2.
    records = [
        RawObservation(
            archive_obs_id=f"pipe-{i}", archive_url=f"http://example.test/pipe-{i}",
            ra=ra, dec=dec, obs_date=date(2020, 1, 1),
        )
        for i, (ra, dec) in enumerate([(10.0, 10.0), (100.0, -30.0), (190.0, 50.0), (280.0, -60.0), (350.0, 5.0)])
    ]
    counts = run_shitty_positional_match(conn, {"unit_test": records})

    assert len(calls) == 5, "every occupied cell should be fetched, not just the first concurrency window"
    assert counts["no_confident_candidate"] == 5, "every record should still be processed and committed"


def test_epoch_chunk_boundary_preserves_per_record_proper_motion(conn, monkeypatch):
    """_process_cell propagates candidates EPOCH_PROPAGATION_CHUNK_SIZE
    epochs at a time (see matcher.propagate_many_epochs) instead of all at
    once. Forced to 1 here so two records at different epochs land in
    separate chunks -- each must still be matched against the candidate
    propagated to *its own* epoch, not a neighboring chunk's position."""
    monkeypatch.setattr(positional_fallback, "EPOCH_PROPAGATION_CHUNK_SIZE", 1)

    import warnings

    from astropy import units as u
    from astropy.coordinates import SkyCoord
    from astropy.table import Table
    from astropy.time import Time
    from erfa import ErfaWarning

    from sync import matcher

    # A fast mover -- 5000 mas/yr pmra_cosdec from a 2016.0 (Gaia DR3) ref
    # epoch -- so a record correctly propagated to its own observation epoch
    # lands well inside SHITTY_MATCH_RADIUS_ARCSEC (60"), while a record
    # matched against the WRONG epoch's propagated position would miss by
    # several arcsec per year of epoch error -- easily enough to fall
    # outside the radius and catch a chunk-boundary regression.
    source_id = 900000000000000090
    ref_ra, ref_dec = 10.0, 10.0
    pmra, pmdec = 5000.0, 0.0

    def fake_pool(pixels):
        return [(source_id, ref_ra, ref_dec, pmra, pmdec, 12.0)]

    monkeypatch.setattr(positional_fallback, "_gaia_healpix_pool", fake_pool)

    # This is a live-Gaia-only (not yet tracked) match, so _process_cell also
    # calls ingest.add_star.add_stars_batch to register the star -- that does
    # its own separate (real, un-mocked by default) Gaia astrometry lookup,
    # same as test_run_shitty_positional_match_discovers_untracked_gaia_star
    # above.
    class _FakeAstrometryJob:
        def get_results(self):
            return Table({
                "source_id": [source_id],
                "ra": [ref_ra], "dec": [ref_dec], "ref_epoch": [2016.0],
                "pmra": [pmra], "pmdec": [pmdec], "parallax": [10.0],
                "phot_g_mean_mag": [12.0], "phot_bp_mean_mag": [12.5], "phot_rp_mean_mag": [11.5],
                "has_rvs": [False], "has_xp_continuous": [False],
            })

    from ingest import add_star as add_star_module
    monkeypatch.setattr(add_star_module.Gaia, "launch_job", lambda query: _FakeAstrometryJob())

    def propagate_to(obs_date_) -> tuple[float, float]:
        jyear = matcher._to_jyear(obs_date_)
        coord = SkyCoord(
            ra=ref_ra * u.deg, dec=ref_dec * u.deg,
            pm_ra_cosdec=pmra * u.mas / u.yr, pm_dec=pmdec * u.mas / u.yr,
            obstime=Time(2016.0, format="jyear"), frame="icrs",
        )
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ErfaWarning, message=".*distance overridden.*")
            moved = coord.apply_space_motion(new_obstime=Time(jyear, format="jyear"))
        return moved.ra.deg, moved.dec.deg

    date_a, date_b = date(2018, 1, 1), date(2022, 1, 1)
    ra_a, dec_a = propagate_to(date_a)
    ra_b, dec_b = propagate_to(date_b)

    rec_a = RawObservation(
        archive_obs_id="chunk-epoch-a", archive_url="http://example.test/chunk-epoch-a",
        ra=ra_a, dec=dec_a, obs_date=date_a,
    )
    rec_b = RawObservation(
        archive_obs_id="chunk-epoch-b", archive_url="http://example.test/chunk-epoch-b",
        ra=ra_b, dec=dec_b, obs_date=date_b,
    )

    counts = run_shitty_positional_match(conn, {"unit_test": [rec_a, rec_b]})
    assert counts["shitty_matched"] == 2

    with conn.cursor() as cur:
        cur.execute(
            "SELECT h.archive_obs_id, s.gaia_source_id FROM spectroscopy_holdings h "
            "LEFT JOIN stars s ON s.star_id = h.star_id "
            "WHERE h.archive_code='unit_test' AND h.archive_obs_id IN ('chunk-epoch-a','chunk-epoch-b')"
        )
        rows = dict(cur.fetchall())

    assert rows["chunk-epoch-a"] == source_id
    assert rows["chunk-epoch-b"] == source_id


def test_multiple_tracked_candidates_across_epochs_does_not_crash(conn, monkeypatch):
    """Reproduces a real crash found live on morgan (2026-08-27): slicing
    matcher.propagate_many_epochs's broadcast SkyCoord per epoch and passing
    it through search_around_sky raised a raw astropy IndexError from a
    non-materialized obstime array -- but only once 2+ candidates were
    involved (a single-candidate cell, like
    test_epoch_chunk_boundary_preserves_per_record_proper_motion above,
    never exercised the fancy-indexing path that triggered it). Two tracked
    stars in the same field plus two distinct observation epochs reproduces
    the exact shape that crashed."""
    with conn.cursor() as cur:
        _insert_star(cur, 900000000000000091, 10.0, 10.0)
        _insert_star(cur, 900000000000000092, 10.01, 10.01)  # ~51" away -- a second candidate in the same field
    conn.commit()

    monkeypatch.setattr(positional_fallback, "_gaia_healpix_pool", lambda pixels: [])

    rec_a = RawObservation(
        archive_obs_id="multi-cand-a", archive_url="http://example.test/multi-cand-a",
        ra=10.0, dec=10.0, obs_date=date(2018, 1, 1),
    )
    rec_b = RawObservation(
        archive_obs_id="multi-cand-b", archive_url="http://example.test/multi-cand-b",
        ra=10.0, dec=10.0, obs_date=date(2022, 1, 1),
    )

    counts = run_shitty_positional_match(conn, {"unit_test": [rec_a, rec_b]})
    assert counts["shitty_matched"] + counts["no_confident_candidate"] == 2


def test_epoch_chunk_size_shrinks_for_large_candidate_pools():
    """Reproduces the shape of a real incident: a live morgan run (2026-08-27)
    hit a dense/galactic-plane cell whose Gaia candidate pool held tens of
    thousands of sources, and the flat EPOCH_PROPAGATION_CHUNK_SIZE=2000
    alone produced a single propagated-array allocation that spiked RSS from
    ~6GB to ~23GB in under a minute -- had to be killed before OOM-killing
    the host. _epoch_chunk_size must shrink well below the flat cap once
    candidate_count is large enough that candidate_count * cap would exceed
    PROPAGATION_ARRAY_ELEMENT_BUDGET."""
    # Small pool: budget doesn't bind, flat cap wins.
    assert _epoch_chunk_size(1) == EPOCH_PROPAGATION_CHUNK_SIZE
    assert _epoch_chunk_size(10) == EPOCH_PROPAGATION_CHUNK_SIZE

    # Large pool (the real incident's shape): budget must bind and the
    # resulting element count must stay within it.
    large_pool = 40_000
    chunk_size = _epoch_chunk_size(large_pool)
    assert chunk_size < EPOCH_PROPAGATION_CHUNK_SIZE
    assert large_pool * chunk_size <= PROPAGATION_ARRAY_ELEMENT_BUDGET

    # Never zero, even for a pool larger than the whole budget -- must
    # still make forward progress (one epoch at a time) rather than hang.
    assert _epoch_chunk_size(PROPAGATION_ARRAY_ELEMENT_BUDGET * 10) == 1
