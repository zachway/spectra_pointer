"""shitty_positional_match: a deliberately low-confidence positional fallback
for records that already carry a raw position but matched neither by
identifier (direct_gaia_column/name_resolved) nor by matcher.py's tight,
1"-radius positional_easy_match.

Design (from a live data-driven brainstorm, 2026-08-17 -- see the per-archive
magnitude percentile table and the candidate-density/distance/magnitude-gap
analysis this is built on):

1. Widen the search to SHITTY_MATCH_RADIUS_ARCSEC (60", not matcher.py's 1")
   against our own tracked `stars`, *and* run a live Gaia DR3 cone search to
   the same effective radius -- our own `stars` table is a small subset of
   what Gaia actually sees in the field (confirmed live: a G=7.24 star
   sitting 3.5" from a record with zero tracked candidates, simply never
   discovered). Both sides are proper-motion-propagated to each record's own
   observation epoch before that 60" cutoff is applied precisely.
2. A BSC5-tracked star in range wins outright: nothing Gaia sees in the same
   field can outshine a star bright enough that Gaia itself couldn't measure
   it (see ingest.add_star.add_bsc_star's own docstring for why those ~70
   stars have no phot_g_mean_mag at all -- treating that as "no magnitude
   info" would rank the brightest stars in the sky as the LEAST plausible
   candidates, backwards).
3. Otherwise, drop any candidate fainter than that archive's empirical
   faintness ceiling (see ARCHIVE_FAINTNESS_CEILING_MAG) -- confirmed live
   that a handful of "sole candidate in the field" hits during this
   brainstorm were G=18-20, implausibly faint for the old pointed
   spectrographs this fallback mostly exists for. Sole-candidacy and
   plausibility are different claims; both are required.
4. Among the survivors: a single one wins outright ("matches with a single
   Gaia source"); with 2+, the brightest must beat the runner-up by
   MAG_CONTRAST_THRESHOLD magnitudes (same rule scripts/seed_bsc5_bright_
   stars.py already uses to call a Gaia cross-match candidate real rather
   than an unrelated neighbor -- confirmed live on this same sample: ~32% of
   multi-candidate records clear it, not a coin flip).
5. PROXIMITY_OVERRIDE_RATIO guards the one failure mode brightness-contrast
   alone doesn't catch: a much closer but modestly fainter candidate losing
   to a brighter one further away. Confirmed live during this brainstorm
   (e.g. a G=13.96 source at 56.8" vs. a G=16.92 source at only 6.6" in the
   same field) -- this is also the mechanism that protects against a
   transient (nova/CV) whose quiescent Gaia magnitude doesn't reflect what
   made it observable. Unlike the faintness ceilings and contrast threshold,
   this specific ratio has NOT been empirically calibrated against known-good
   matches the way INSTRUMENT_MATCH_RADIUS_OVERRIDES_ARCSEC was (see
   sync.matcher) -- treat it as a starting guard, not a tuned constant.

Never produces match_status='matched' -- every result lands in needs_review,
regardless of how strong the evidence looks, since the underlying signal
(nearest/brightest plausible star) is an inference, not a confirmed
identifier the way name_resolved or direct_gaia_column are. An extragalactic
transient (a supernova in a host galaxy, say) isn't a star this database can
correctly resolve to at all -- for those, this mostly just fails to find any
plausible candidate (no real point source matches), which fails safe rather
than fails wrong.

Query architecture (rewritten 2026-08-18, replacing an upload+CONTAINS design
that shipped across PRs #110/#112/#114/#115): the original approach uploaded
each batch of our own records to Gaia's TAP+ service and cross-matched them
against gaiadr3.gaia_source_lite via CONTAINS(POINT, CIRCLE) -- a spatial
join Gaia's query planner apparently can't optimize well for an uploaded
table, confirmed live overnight 2026-08-17/18: individual jobs ranged from
5 minutes to over 2 hours (occasionally hitting Gaia's hard 7200s server-side
abort), and because run_shitty_positional_match only committed once per
whole multi-bucket chunk, 16+ hours of real Gaia work produced zero durable
rows.

This version instead walks Gaia's own HEALPix indexing scheme directly. Per
ESA's Gaia DR3 documentation ("Source Identifiers -- Assignment and Usage
throughout DPAC", GAIA-C3-TN-ARI-BAS-020), source_id's high bits (36-63)
encode the source's nested HEALPix level-12 pixel: healpix_L = source_id >>
(35 + 2*(12-L)) for any level L <= 12. That means "every Gaia source in this
patch of sky" can be fetched with a plain `source_id BETWEEN x AND y` range
scan against gaia_source_lite's own primary-key ordering -- no upload table,
no per-point spatial join at all. Confirmed live 2026-08-18: a single real
cell (level 7, ~0.21 sq deg) returned 5,724 rows in 366s -- faster than most
of the old design's CONTAINS jobs, but not a dramatic win either, and a
matched live check against the real 13.1M-record backlog found it touches
100% of all possible cells at level 5, 97% at level 6, and 75% at level 7 --
i.e. this project's pending records are spread across essentially the whole
sky at any coarse-to-moderate granularity, so coarser levels don't reduce
round-trip count much, they just make each round trip heavier. Net result:
fully sequential processing of the whole backlog is on the order of months
at any level tested -- this rewrite fixes correctness (real proper-motion
handling) and durability (per-cell commits instead of one commit per giant
multi-hour chunk), but closing the remaining throughput gap needs a
separate lever (running multiple Gaia jobs concurrently instead of one at a
time, and/or a one-time bulk pull of the relevant Gaia catalog slice into a
local table so future runs skip Gaia's TAP service entirely) -- not yet
implemented. See GAIA_HEALPIX_LEVEL's own comment for why level 7
specifically was chosen given that tradeoff.

The new shape also removes the record-chunking and per-archive looping the
old design needed: instead of "for each archive, for each chunk of records,
query Gaia," this walks "for each HEALPix cell any pending record (across
every requested archive at once) falls in, fetch that cell's Gaia pool once
and match everything in it" -- a HEALPix cell doesn't care which archive a
record belongs to, so archives sharing sky coverage now share Gaia round
trips instead of each re-querying overlapping regions. Commits happen once
per cell rather than once per whole run, so a crash mid-run only loses at
most one cell's worth of (re-runnable) work.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

import astropy_healpix as ah
import psycopg
from astropy import units as u
from astropy.coordinates import SkyCoord
from astroquery.gaia import Gaia

from ingest.add_star import add_stars_batch
from sync import matcher
from sync.base import RawObservation, clean_float

logger = logging.getLogger(__name__)

# See module docstring point 1 for why 60", not matcher.py's tight 1" or the
# full 2' this was explored out to.
SHITTY_MATCH_RADIUS_ARCSEC = 60.0

# See module docstring point 4. Confirmed live against this project's own
# data (2026-08-17): ~32% of records with 2+ candidates within 2' clear this
# gap -- a real, non-trivial population, not noise.
MAG_CONTRAST_THRESHOLD_MAG = 2.0

# See module docstring point 5 -- NOT empirically calibrated, a starting
# guard only.
PROXIMITY_OVERRIDE_RATIO = 3.0

# phot_g_mean_mag percentiles (p99) of stars behind each archive's own
# already-CONFIRMED matches (match_status='matched'), queried live from prod
# 2026-08-17. p99 is used rather than max because max clusters at G~20-22
# across every archive regardless of its real characteristic depth --
# almost certainly a handful of pre-existing bad matches, not evidence of
# real reach.
#
# Archives not listed here haven't been individually calibrated yet --
# DEFAULT_FAINTNESS_CEILING_MAG is a deliberately mid-of-the-observed-range
# placeholder, not a real per-archive measurement. Extending this table to
# the rest of the archives sharing this fallback is follow-up work.
ARCHIVE_FAINTNESS_CEILING_MAG: dict[str, float] = {
    "dao": 13.0,
    "lick": 15.1,
    "irtf_spex": 16.4,
    "asiago": 15.8,
    "irtf_legacy": 18.0,
    "cfht_cadc": 16.3,
    "eso_raw": 19.0,
    "gtc": 21.0,
    "eso": 19.2,
    "koa": 20.4,
    "mast": 19.5,
    "gemini": 20.3,
    "ing": 19.8,
    "noirlab": 19.4,
    "oirsa": 20.8,
}
DEFAULT_FAINTNESS_CEILING_MAG = 18.0

# HEALPix level (nested scheme) used to bucket both our own pending records
# and Gaia's own source_id-encoded pixel index -- see module docstring for
# the source_id encoding this relies on.
#
# Level 7 (nside=128) gives ~0.21 sq deg/cell (~27.5' across). Safety margin
# confirmed live 2026-08-18 against this project's real backlog (13.1M
# pending records spanning obs_date 1978-02-25 to 2026-08-17): worst-case
# proper-motion drift is 38 years * MAX_PM_ARCSEC_PER_YEAR = ~6.5', plus
# SHITTY_MATCH_RADIUS_ARCSEC=60" = ~7.5' worst-case total reach -- comfortably
# under the ~27.5' cell width (~3.65x margin), so a true candidate for a
# record in cell C can only ever live in C or its immediate ring of
# neighbours (see _healpix_cell_and_ring), never further out.
#
# Level, not size, was the real open question: the same live check found the
# backlog touches 100% of all possible cells at level 5 and 97% at level 6 --
# i.e. coarser levels don't meaningfully reduce the number of Gaia round
# trips needed once you're already near full-sky coverage, they only make
# each individual round trip heavier. Level 7 (146,827 occupied cells,
# confirmed ~366s each for one real cell) was chosen over that or level 8
# (ESA's own documented level for source_id-range queries, safety margin
# ~1.83x -- tighter but still positive) specifically for morgan's practical
# constraints: smaller per-query result sets, and per-cell commits (see
# run_shitty_positional_match) landing more often, so progress is visible
# and a crash loses less. NOT chosen to minimize total wall-clock time --
# at any level tested, fully sequential processing of the whole backlog is
# on the order of months; see module docstring's note on remaining
# throughput levers (concurrency, or a one-time bulk catalog pull) still
# needed to close that gap.
GAIA_HEALPIX_LEVEL = 7

_HEALPIX = ah.HEALPix(nside=2**GAIA_HEALPIX_LEVEL, order="nested", frame="icrs")

# gaiadr3.gaia_source_lite, not the full gaia_source -- same row count, but
# only 51 columns instead of 150+, and it's Gaia's own documented
# optimization for exactly this kind of query ("substantially improve the
# performance of various types of ADQL queries" -- ESA Gaia archive's
# "Writing queries" help page, sec. 2.1). The dedicated Gaia.cross_match /
# cross_match_basic helpers were checked and ruled out for this use case --
# their radius argument is hard-capped at 10", well under
# SHITTY_MATCH_RADIUS_ARCSEC (60", before PM padding); source_id range
# scanning sidesteps that limitation entirely since it isn't a radius search
# at all.
GAIA_HEALPIX_POOL_QUERY = """
SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag
FROM gaiadr3.gaia_source_lite
WHERE {ranges_clause}
"""

GAIA_LAUNCH_JOB_ATTEMPTS = 5
GAIA_LAUNCH_JOB_BACKOFF_SECONDS = 15

# How often to poll and log an in-flight async job's phase. Gaia.launch_job_
# async(background=False) (the default) blocks internally inside astroquery
# until the job finishes, with no visibility into whether it's PENDING,
# QUEUED, or EXECUTING the whole time -- confirmed live 2026-08-17: a real
# prod run sat with ~0% CPU and zero log output for 30+ minutes with no way
# to tell whether it was stuck or just slow. background=True plus polling
# here trades a few extra round trips for that visibility.
GAIA_JOB_POLL_SECONDS = 10

# How many HEALPix cells' Gaia fetches to keep in flight at once (see
# run_shitty_positional_match) -- confirmed live 2026-08-18 that Gaia's
# TAP+ service does not give a clean multiplicative speedup from client-side
# concurrency. At 5-way: 2 of 5 launches failed transiently (recovered by
# the retry logic above) and real per-job slowdown once running (a cell that
# took 366s isolated took 987s under 5-way load; individual jobs ranged
# 806-1886s+, one didn't finish in ~40 minutes) -- consistent with
# server-side fair-share throttling for concurrent jobs from the same
# session, not something fixable client-side. At 2-way, re-tested the same
# way: both launches succeeded cleanly (no transient failures), and the one
# cell also tested at 5-way was faster (861s vs. 1265s at 5-way, still
# slower than the 366s fully-isolated baseline) -- a real but partial
# improvement, not a clean 2x. One of the two test cells didn't finish in
# ~35 minutes at 2-way either, matching its own behaviour at 5-way, so that
# appears to be a slow/dense cell rather than a concurrency artifact. Chosen
# as the safer point on this tradeoff given the 5-way failures; not
# exhaustively tuned against 3+.
GAIA_FETCH_CONCURRENCY = 2


def _launch_gaia_job(query: str):
    """Gaia.launch_job_async, retried on transient TAP failures -- same
    reasoning as ingest.add_star._launch_gaia_job (a different module's
    sync-based retry helper, not reused directly here since this one needs
    the async/polling variant): confirmed live that the synchronous endpoint
    (Gaia.launch_job) injects a client-side TOP 2000 and isn't suitable for
    an unbounded-size result like a HEALPix cell's full source pool.

    Launched with background=True and polled explicitly (see
    GAIA_JOB_POLL_SECONDS) rather than left to astroquery's own internal
    blocking wait, so a stuck/slow job is visible in logs as it happens
    instead of as silence.
    """
    last_exc: Exception | None = None
    for attempt in range(GAIA_LAUNCH_JOB_ATTEMPTS):
        try:
            job = Gaia.launch_job_async(query, background=True)
            start = time.monotonic()
            last_phase = None
            while not job.is_finished():
                phase = job.get_phase(update=True)
                if phase != last_phase:
                    logger.info("Gaia job %s: %s (%.0fs elapsed)", job.jobid, phase, time.monotonic() - start)
                    last_phase = phase
                if job.is_finished():
                    break
                time.sleep(GAIA_JOB_POLL_SECONDS)
            final_phase = job.get_phase()
            logger.info("Gaia job %s: %s (%.0fs total)", job.jobid, final_phase, time.monotonic() - start)
            if final_phase != "COMPLETED":
                raise RuntimeError(f"Gaia job {job.jobid} ended in phase {final_phase}, not COMPLETED")
            return job
        except Exception as exc:
            last_exc = exc
            if attempt < GAIA_LAUNCH_JOB_ATTEMPTS - 1:
                delay = GAIA_LAUNCH_JOB_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "Gaia TAP range-scan failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, GAIA_LAUNCH_JOB_ATTEMPTS, delay, exc,
                )
                time.sleep(delay)
    raise last_exc


@dataclass
class Candidate:
    star_id: int | None       # None if not yet tracked in `stars`
    gaia_source_id: int | None
    source_catalog: str       # 'bsc5' | 'gaia' | 'gaia_untracked'
    separation_arcsec: float
    phot_g_mean_mag: float | None


def faintness_ceiling_mag(archive_code: str) -> float:
    return ARCHIVE_FAINTNESS_CEILING_MAG.get(archive_code, DEFAULT_FAINTNESS_CEILING_MAG)


def _healpix_cell(ra: float, dec: float) -> int:
    return int(_HEALPIX.lonlat_to_healpix(ra * u.deg, dec * u.deg))


def _healpix_source_id_range(pix: int) -> tuple[int, int]:
    """See module docstring's source_id encoding. shift=35 converts a level-
    12 pixel index to source_id's leading bits; the extra 2*(12-level) widens
    that to whatever coarser GAIA_HEALPIX_LEVEL pixel this project actually
    buckets by (each level up merges 4 child pixels into 1, i.e. 2 more bits)."""
    shift = 35 + 2 * (12 - GAIA_HEALPIX_LEVEL)
    return pix << shift, ((pix + 1) << shift) - 1


def _healpix_cell_and_ring(pix: int) -> list[int]:
    """pix plus its (up to 8) immediate neighbours -- see GAIA_HEALPIX_LEVEL
    for why this ring is guaranteed wide enough to catch every true
    candidate for any record whose own cell is pix. Near the poles HEALPix
    cells are irregular and neighbours() can repeat/omit entries; harmless
    here since this only ever widens or leaves unchanged the fetched area,
    never narrows it below a full ring."""
    neighbours = [int(n) for n in _HEALPIX.neighbours(pix) if n >= 0]
    return sorted(set([pix, *neighbours]))


def pick_best_candidate(archive_code: str, candidates: list[Candidate]) -> tuple[Candidate | None, str]:
    """Pure decision logic (see module docstring points 2-5) -- no DB/network
    access, so this is the part worth unit-testing directly rather than only
    smoke-testing live.
    """
    if not candidates:
        return None, "no candidates within radius"

    bsc5_candidates = [c for c in candidates if c.source_catalog == "bsc5"]
    if bsc5_candidates:
        winner = min(bsc5_candidates, key=lambda c: c.separation_arcsec)
        return winner, "bsc5 bright-star match (categorical win)"

    ceiling = faintness_ceiling_mag(archive_code)
    plausible = [c for c in candidates if c.phot_g_mean_mag is not None and c.phot_g_mean_mag <= ceiling]
    if not plausible:
        return None, f"no candidate at or brighter than {archive_code}'s faintness ceiling (G<={ceiling})"

    plausible.sort(key=lambda c: c.phot_g_mean_mag)
    winner = plausible[0]

    if len(plausible) == 1:
        closer_but_excluded = [
            c for c in candidates
            if c is not winner and c.separation_arcsec < winner.separation_arcsec / PROXIMITY_OVERRIDE_RATIO
        ]
        if closer_but_excluded:
            return None, "a much closer candidate was excluded by the faintness ceiling -- ambiguous, not confident"
        return winner, "sole plausible candidate within radius"

    runner_up = plausible[1]
    gap = runner_up.phot_g_mean_mag - winner.phot_g_mean_mag
    if gap < MAG_CONTRAST_THRESHOLD_MAG:
        return None, f"brightest candidate only {gap:.1f} mag ahead of runner-up (< {MAG_CONTRAST_THRESHOLD_MAG} mag)"

    closer_but_fainter = [
        c for c in plausible[1:]
        if c.separation_arcsec < winner.separation_arcsec / PROXIMITY_OVERRIDE_RATIO
    ]
    if closer_but_fainter:
        return None, "a fainter candidate sits much closer than the brightness winner -- ambiguous, not confident"

    return winner, f"brightness winner by {gap:.1f} mag over runner-up"


def _load_tracked_candidates(conn: psycopg.Connection, target_ra: list[float], target_dec: list[float], radius_deg: float) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT s.star_id, s.gaia_source_id, s.source_catalog, s.ra, s.dec,
                   s.ref_epoch, s.pmra, s.pmdec, s.phot_g_mean_mag
            FROM stars s, unnest(%(target_ra)s::float8[], %(target_dec)s::float8[]) AS t(ra, dec)
            WHERE q3c_join(t.ra, t.dec, s.ra, s.dec, %(radius_deg)s)
            """,
            {"target_ra": target_ra, "target_dec": target_dec, "radius_deg": radius_deg},
        )
        return cur.fetchall()


def _gaia_healpix_pool(pixels: list[int]) -> list[tuple]:
    """Every Gaia source in the given set of HEALPix pixels, via a plain
    source_id range scan (see module docstring) -- raw 2016.0-epoch
    astrometry, not yet propagated to any particular observation epoch.
    """
    ranges_clause = " OR ".join(
        f"source_id BETWEEN {lo} AND {hi}"
        for lo, hi in (_healpix_source_id_range(p) for p in pixels)
    )
    query = GAIA_HEALPIX_POOL_QUERY.format(ranges_clause=ranges_clause)
    job = _launch_gaia_job(query)
    table = job.get_results()
    return [
        (
            int(row["source_id"]),
            float(row["ra"]), float(row["dec"]),
            clean_float(row["pmra"]), clean_float(row["pmdec"]),
            clean_float(row["phot_g_mean_mag"]),
        )
        for row in table
    ]


def _search_around(targets: SkyCoord, propagated: SkyCoord, ids: list, radius_arcsec: float) -> dict[int, list[tuple]]:
    """target-local-index -> [(candidate_id, separation_arcsec), ...] --
    vectorized KD-tree cross-match (astropy's search_around_sky), same
    pattern matcher.match_records already uses for its own candidate pool.
    search_around_sky's first return value indexes its *argument*
    (propagated), the second indexes self (targets) -- the reverse of what
    the field names suggest. Verified empirically (see matcher.py's own note).
    """
    idx_cat, idx_target, sep2d, _ = targets.search_around_sky(propagated, radius_arcsec * u.arcsec)
    out: dict[int, list[tuple]] = defaultdict(list)
    for cat_i, target_i, sep in zip(idx_cat, idx_target, sep2d):
        out[int(target_i)].append((ids[cat_i], sep.arcsec))
    return out


def _process_cell(conn: psycopg.Connection, cell: int, cell_entries: list[tuple[str, RawObservation]], pool: list[tuple], radius_deg: float) -> dict:
    """The local (DB + matching) work for one already-fetched HEALPix cell --
    split out from the Gaia fetch (_gaia_healpix_pool) so the fetch
    (network-bound, minutes) and this (DB + vectorized matching, fast) can be
    pipelined across cells instead of the whole run waiting on one fetch at a
    time -- see GAIA_FETCH_CONCURRENCY and run_shitty_positional_match.
    """
    counts = {"shitty_matched": 0, "no_confident_candidate": 0}
    cell_records = [r for _, r in cell_entries]

    tracked_rows = _load_tracked_candidates(
        conn, [r.ra for r in cell_records], [r.dec for r in cell_records], radius_deg,
    )
    star_positions = {row[0]: row for row in tracked_rows}  # star_id -> full row

    live_astrometry = {
        source_id: (ra, dec, matcher.GAIA_DR3_REF_EPOCH, pmra, pmdec, mag)
        for source_id, ra, dec, pmra, pmdec, mag in pool
    }

    # Grouped by observation epoch, same reasoning as matcher.match_records:
    # a cell's records can span an archive's whole backlog (years to
    # decades), and propagating every candidate to only one record's epoch
    # while reusing that for every other record would silently misplace any
    # fast-mover proportional to how far its actual epoch is from the one
    # used.
    by_epoch: dict[float, list[int]] = defaultdict(list)
    for local_i, r in enumerate(cell_records):
        by_epoch[matcher._to_jyear(r.obs_date)].append(local_i)

    with conn.cursor() as cur:
        for epoch, local_idxs in by_epoch.items():
            epoch_records = [cell_records[i] for i in local_idxs]
            targets = SkyCoord(ra=[r.ra for r in epoch_records] * u.deg, dec=[r.dec for r in epoch_records] * u.deg)

            tracked_hits: dict[int, list[tuple]] = {}
            if star_positions:
                star_rows = [(row[0], row[3], row[4], row[5], row[6], row[7]) for row in star_positions.values()]
                star_ids, star_propagated = matcher._propagate(star_rows, epoch)
                tracked_hits = _search_around(targets, star_propagated, star_ids, SHITTY_MATCH_RADIUS_ARCSEC)

            live_hits: dict[int, list[tuple]] = {}
            if live_astrometry:
                live_rows = [(gid, row[0], row[1], row[2], row[3], row[4]) for gid, row in live_astrometry.items()]
                live_ids, live_propagated = matcher._propagate(live_rows, epoch)
                live_hits = _search_around(targets, live_propagated, live_ids, SHITTY_MATCH_RADIUS_ARCSEC)

            for pos, local_i in enumerate(local_idxs):
                archive_code, r = cell_entries[local_i]

                by_gaia_id: dict[int, Candidate] = {}
                tracked_untagged = []
                for star_id, sep in tracked_hits.get(pos, []):
                    row = star_positions[star_id]
                    _, gaia_source_id, source_catalog, _ra, _dec, _ref_epoch, _pmra, _pmdec, phot_g_mean_mag = row
                    cand = Candidate(
                        star_id=star_id,
                        gaia_source_id=gaia_source_id,
                        source_catalog=source_catalog,
                        separation_arcsec=sep,
                        phot_g_mean_mag=phot_g_mean_mag,
                    )
                    if gaia_source_id is not None:
                        by_gaia_id[gaia_source_id] = cand
                    else:
                        tracked_untagged.append(cand)

                # PM-propagated the same way as tracked stars above -- only
                # 60"-precise once propagated to this record's own epoch,
                # not trusted just for having appeared in the raw cell pool.
                for gaia_source_id, sep in live_hits.get(pos, []):
                    if gaia_source_id in by_gaia_id:
                        continue  # already tracked -- richer record wins
                    _ra, _dec, _ref_epoch, _pmra, _pmdec, phot_g_mean_mag = live_astrometry[gaia_source_id]
                    by_gaia_id[gaia_source_id] = Candidate(
                        star_id=None,
                        gaia_source_id=gaia_source_id,
                        source_catalog="gaia_untracked",
                        separation_arcsec=sep,
                        phot_g_mean_mag=phot_g_mean_mag,
                    )

                candidates = tracked_untagged + list(by_gaia_id.values())
                winner, reason = pick_best_candidate(archive_code, candidates)

                if winner is None:
                    matcher._upsert_holding(cur, archive_code, r, None, "shitty_positional_match", "needs_review", None)
                    counts["no_confident_candidate"] += 1
                    logger.info("%s: no confident shitty_positional_match candidate for %s (%s)", archive_code, r.archive_obs_id, reason)
                    continue

                star_id = winner.star_id
                if star_id is None:
                    # A live-Gaia-only hit -- register it the same way
                    # ingest.add_star.discover_stars does, so future syncs
                    # (and future runs of this fallback) see it as tracked too.
                    add_stars_batch(conn, [winner.gaia_source_id])
                    with conn.cursor() as lookup_cur:
                        lookup_cur.execute("SELECT star_id FROM stars WHERE gaia_source_id = %s", (winner.gaia_source_id,))
                        star_id = lookup_cur.fetchone()[0]

                matcher._upsert_holding(cur, archive_code, r, star_id, "shitty_positional_match", "needs_review", float(winner.separation_arcsec))
                counts["shitty_matched"] += 1
                logger.info("%s: shitty_positional_match %s -> star_id %d (%s)", archive_code, r.archive_obs_id, star_id, reason)
    conn.commit()
    logger.info("healpix cell %d: %d records processed -> %s", cell, len(cell_records), counts)
    return counts


def run_shitty_positional_match(conn: psycopg.Connection, records_by_archive: dict[str, list[RawObservation]]) -> dict:
    """Entry point -- operates only on records that already carry a raw
    position (ra/dec/obs_date); a record with no position at all isn't in
    scope for this fallback. Always writes match_status='needs_review', never
    'matched' -- see module docstring.

    Takes every requested archive's records at once (not one archive/chunk
    at a time -- see module docstring) and buckets them by HEALPix cell:
    the unit of Gaia work is now "one cell's source pool," shared by however
    many archives happen to have pending records in that patch of sky.
    Commits once per cell, so a crash mid-run loses at most one cell's worth
    of (re-runnable) work.

    Fetches (the network-bound part, minutes per cell) are pipelined up to
    GAIA_FETCH_CONCURRENCY at a time; the DB + matching work for a completed
    cell (_process_cell, fast) runs on this thread while further fetches
    continue in the background. See GAIA_FETCH_CONCURRENCY's own comment for
    why 2, not more.
    """
    counts = {"shitty_matched": 0, "no_confident_candidate": 0}

    entries: list[tuple[str, RawObservation]] = []
    for archive_code, records in records_by_archive.items():
        for r in records:
            if r.ra is not None and r.dec is not None and r.obs_date is not None and -90.0 <= r.dec <= 90.0:
                entries.append((archive_code, r))
    if not entries:
        return counts

    by_cell: dict[int, list[int]] = defaultdict(list)
    for i, (_, r) in enumerate(entries):
        by_cell[_healpix_cell(r.ra, r.dec)].append(i)

    radius_deg = SHITTY_MATCH_RADIUS_ARCSEC / 3600.0
    cells = list(by_cell.items())

    with ThreadPoolExecutor(max_workers=GAIA_FETCH_CONCURRENCY) as executor:
        cell_iter = iter(cells)
        in_flight: dict[Future, tuple[int, list[int]]] = {}

        def submit_next() -> None:
            item = next(cell_iter, None)
            if item is None:
                return
            cell, idxs = item
            fut = executor.submit(_gaia_healpix_pool, _healpix_cell_and_ring(cell))
            in_flight[fut] = (cell, idxs)

        for _ in range(GAIA_FETCH_CONCURRENCY):
            submit_next()

        while in_flight:
            done, _ = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED)
            for fut in done:
                cell, idxs = in_flight.pop(fut)
                pool = fut.result()
                submit_next()  # keep the pipeline full as soon as a slot frees up

                cell_entries = [entries[i] for i in idxs]
                cell_counts = _process_cell(conn, cell, cell_entries, pool, radius_deg)
                for key, value in cell_counts.items():
                    counts[key] = counts.get(key, 0) + value

    return counts
