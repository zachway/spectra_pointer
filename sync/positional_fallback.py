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
   observation epoch before that 60" cutoff is applied precisely (the live
   Gaia query itself is fetched with a wider, epoch-baseline-padded radius
   first -- see _gaia_cone_search_batch -- so a fast-mover found by the
   coarse fetch isn't missed just because Gaia's own DR3 position is fixed
   at 2016.0 and this project's oldest archives span decades). 60" was
   chosen over matcher.py's own 2' exploration because the accepted-match
   distance from that analysis scaled almost 1:1 with the search radius past
   ~90" -- the tell that a wider radius is finding "the only star in an
   empty patch of sky," not correctly disambiguating a real target.
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
   made it observable: if its correctly-positioned, close-but-modest-
   magnitude counterpart loses purely on brightness to an unrelated bright
   neighbor further off, that's exactly the ambiguous case this should
   refuse to call rather than confidently picking the brighter, wrong star.
   Unlike the faintness ceilings and contrast threshold, this specific ratio
   has NOT been empirically calibrated against known-good matches the way
   INSTRUMENT_MATCH_RADIUS_OVERRIDES_ARCSEC was (see sync.matcher) -- treat
   it as a starting guard, not a tuned constant.

Never produces match_status='matched' -- every result lands in needs_review,
regardless of how strong the evidence looks, since the underlying signal
(nearest/brightest plausible star) is an inference, not a confirmed
identifier the way name_resolved or direct_gaia_column are. An extragalactic
transient (a supernova in a host galaxy, say) isn't a star this database can
correctly resolve to at all -- for those, this mostly just fails to find any
plausible candidate (no real point source matches), which fails safe rather
than fails wrong.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import psycopg
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.table import Table
from astroquery.gaia import Gaia

from ingest.add_star import add_stars_batch
from sync import matcher
from sync.base import RawObservation, clean_float

logger = logging.getLogger(__name__)

# See module docstring point 1 for why 60", not matcher.py's tight 1" or the
# full 2' this was explored out to.
SHITTY_MATCH_RADIUS_ARCSEC = 60.0

# How coarsely to bucket a chunk's records by observation year before sizing
# each bucket's own PM-padded live-Gaia search radius (see run_shitty_
# positional_match) -- confirmed live 2026-08-17 this matters in practice:
# dao's backlog alone spans 1986-2026, and a single flat max_years across
# that whole range pads the search radius to ~369" (6.1') for every record,
# not just the handful actually that old.
GAIA_QUERY_TIME_BUCKET_YEARS = 10

# Coarse (RA, Dec) grid cell size (degrees) for grouping a chunk's records by
# sky position before querying Gaia, nested underneath the epoch bucketing
# above -- purely a performance grouping, not a correctness filter: every
# record still gets its own precise CONTAINS/CIRCLE search regardless of
# which sky bucket it landed in, so a boundary-adjacent record can never be
# missed the way a bug in an actual spatial WHERE-filter on gaia_source_lite
# could. The motivation is locality: Gaia's own archive documentation treats
# HEALPix indexing as central to how gaia_source(_lite) is organized, so a
# batch of upload points scattered across the whole sky plausibly touches
# many more disjoint regions of that index than the same batch grouped by
# proximity. Confirmed live 2026-08-17 that dao's own candidate pool is
# genuinely scattered (419 of a possible 648 10x10 degree cells populated,
# full RA range, dec -72 to +90), so this isn't just a theoretical concern
# for this archive. Deliberately (RA, Dec) together, not RA alone -- RA-only
# binning badly mis-clusters near the poles, where a small angular
# separation can span a huge RA range. NOT yet empirically validated against
# real timing data (unlike GAIA_QUERY_TIME_BUCKET_YEARS's necessity, this is
# a plausible lever, not a proven one) -- and it trades fewer/heavier Gaia
# round-trips for more/lighter ones, so the right cell size is a real,
# untuned open question.
GAIA_QUERY_SKY_BUCKET_DEG = 15.0

# See module docstring point 4. Confirmed live against this project's own
# data (2026-08-17): ~32% of records with 2+ candidates within 2' clear this
# gap -- a real, non-trivial population, not noise.
MAG_CONTRAST_THRESHOLD_MAG = 2.0

# See module docstring point 5 -- NOT empirically calibrated, a starting
# guard only.
PROXIMITY_OVERRIDE_RATIO = 3.0

# phot_g_mean_mag percentiles (p99) of stars behind each archive's own
# already-CONFIRMED matches (match_status='matched'), queried live from prod
# 2026-08-17 -- see project memory project_suspicious_faint_matches_bright_
# archives.md for the full table including p50/p90/p95 and the reasoning for
# using p99 rather than max (max clusters at G~20-22 across every archive
# regardless of its real characteristic depth -- almost certainly a handful
# of pre-existing bad matches, not evidence of real reach).
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

# gaiadr3.gaia_source_lite, not the full gaia_source -- same row count, but
# only 51 columns instead of 150+, and it's Gaia's own documented
# optimization for exactly this kind of query ("substantially improve the
# performance of various types of ADQL queries" -- ESA Gaia archive's
# "Writing queries" help page, sec. 2.1). Confirmed live 2026-08-17 that
# gaia_source_lite carries every column this query needs (source_id, ra,
# dec, pmra, pmdec, phot_g_mean_mag). The dedicated Gaia.cross_match /
# cross_match_basic helpers were also checked and ruled out for this use
# case -- their radius argument is hard-capped at 10", well under
# SHITTY_MATCH_RADIUS_ARCSEC (60", before PM padding).
GAIA_XMATCH_RADIUS_QUERY = """
SELECT u.rec_id, g.source_id, g.ra, g.dec, g.pmra, g.pmdec, g.phot_g_mean_mag
FROM tap_upload.pending AS u
JOIN gaiadr3.gaia_source_lite AS g
  ON 1 = CONTAINS(POINT('ICRS', g.ra, g.dec), CIRCLE('ICRS', u.ra, u.dec, {radius_deg}))
"""

GAIA_LAUNCH_JOB_ATTEMPTS = 5
GAIA_LAUNCH_JOB_BACKOFF_SECONDS = 15

# How often to poll and log an in-flight async job's phase. Gaia.launch_job_
# async(background=False) (the default) blocks internally inside astroquery
# until the job finishes, with no visibility into whether it's PENDING,
# QUEUED, or EXECUTING the whole time -- confirmed live 2026-08-17: a real
# prod run sat with ~0% CPU and zero log output for 30+ minutes on a single
# chunk with no way to tell whether it was stuck or just slow. background=
# True plus polling here trades a few extra round trips for that visibility.
GAIA_JOB_POLL_SECONDS = 10


def _launch_gaia_upload_job(query: str, upload_resource, upload_table_name: str):
    """Gaia.launch_job_async, retried on transient TAP failures -- same
    reasoning as ingest.add_star._launch_gaia_job (not reused directly since
    that one doesn't accept upload_resource/upload_table_name), but the async
    variant specifically: confirmed live that the synchronous endpoint
    (Gaia.launch_job) hard-times-out (HTTP 408) on an uploaded-table
    cross-match against gaia_source, even for a handful of rows -- the sync
    endpoint's time budget doesn't fit this query shape, only the async one
    does.

    Launched with background=True and polled explicitly (see
    GAIA_JOB_POLL_SECONDS) rather than left to astroquery's own internal
    blocking wait, so a stuck/slow job is visible in logs as it happens
    instead of as silence.
    """
    last_exc: Exception | None = None
    for attempt in range(GAIA_LAUNCH_JOB_ATTEMPTS):
        try:
            job = Gaia.launch_job_async(
                query, upload_resource=upload_resource, upload_table_name=upload_table_name, background=True,
            )
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
                    "Gaia TAP upload cross-match failed (attempt %d/%d), retrying in %ds: %s",
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


def _sky_bucket(ra: float, dec: float) -> tuple[int, int]:
    """See GAIA_QUERY_SKY_BUCKET_DEG -- a coarse grouping key only, purely
    for query-batching locality, never a search boundary: every record still
    gets its own precise CONTAINS/CIRCLE search regardless of which bucket
    it lands in, so a record can never be missed by landing in the "wrong"
    bucket. The one known imprecision is the RA=0/360 seam (a point at
    359.9 and one at 0.1 are ~0.2 deg apart on sky but land in different
    buckets) -- harmless for the same reason, just a missed locality
    grouping for that pair, not a missed match.
    """
    return (int(ra // GAIA_QUERY_SKY_BUCKET_DEG), int(dec // GAIA_QUERY_SKY_BUCKET_DEG))


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


def _gaia_cone_search_batch(records: list[RawObservation], radius_arcsec: float, max_years: float) -> dict[int, list[tuple]]:
    """Batched Gaia DR3 cone search via table upload -- one TAP round trip for
    the whole chunk instead of one per record (same batching reasoning as
    ingest.add_star's own batch queries: Gaia's TAP+ endpoint starts erroring
    after ~10 back-to-back queries in a short window).

    Returns rec_id (index into `records`) -> [(gaia_source_id, ra, dec, pmra, pmdec, phot_g_mean_mag), ...]
    -- raw astrometry, NOT yet propagated or filtered to radius_arcsec. The
    query's own search radius is padded by max_years' worth of worst-case
    proper motion (matcher.MAX_PM_ARCSEC_PER_YEAR, same reasoning as
    matcher._load_candidate_stars) so a fast-mover isn't missed outright by
    a search centered on Gaia's fixed 2016.0-epoch position when the record
    itself may be decades older -- this project's oldest archives (DAO,
    Lick, ...) genuinely span that range. The caller (run_shitty_
    positional_match) is responsible for propagating these to each record's
    own actual observation epoch and filtering precisely down to
    radius_arcsec, exactly as it already does for tracked `stars`
    candidates -- this coarse, padded fetch only narrows Gaia's ~1.8B rows
    down to a manageable candidate set.
    """
    if not records:
        return {}
    upload = Table({
        "rec_id": list(range(len(records))),
        "ra": [r.ra for r in records],
        "dec": [r.dec for r in records],
    })
    padded_radius_deg = (radius_arcsec + matcher.MAX_PM_ARCSEC_PER_YEAR * max_years) / 3600.0
    query = GAIA_XMATCH_RADIUS_QUERY.format(radius_deg=padded_radius_deg)
    job = _launch_gaia_upload_job(query, upload, "pending")
    table = job.get_results()

    out: dict[int, list[tuple]] = defaultdict(list)
    for row in table:
        out[int(row["rec_id"])].append((
            int(row["source_id"]),
            float(row["ra"]), float(row["dec"]),
            clean_float(row["pmra"]), clean_float(row["pmdec"]),
            clean_float(row["phot_g_mean_mag"]),
        ))
    return out


def run_shitty_positional_match(conn: psycopg.Connection, archive_code: str, records: list[RawObservation]) -> dict:
    """Entry point -- operates only on records that already carry a raw
    position (ra/dec/obs_date); a record with no position at all isn't in
    scope for this fallback. Always writes match_status='needs_review', never
    'matched' -- see module docstring.
    """
    counts = {"shitty_matched": 0, "no_confident_candidate": 0}

    positional = [
        r for r in records
        if r.ra is not None and r.dec is not None and r.obs_date is not None and -90.0 <= r.dec <= 90.0
    ]
    if not positional:
        return counts

    radius_deg = SHITTY_MATCH_RADIUS_ARCSEC / 3600.0
    tracked_rows = _load_tracked_candidates(conn, [r.ra for r in positional], [r.dec for r in positional], radius_deg)
    star_positions = {row[0]: row for row in tracked_rows}  # star_id -> full row

    # Grouped by observation epoch, same reasoning as matcher.match_records:
    # a batch here can span an archive's whole backlog (years to decades),
    # and propagating every tracked star to only one record's epoch while
    # reusing that for every other record would silently misplace any
    # fast-mover proportional to how far its actual epoch is from the one
    # used -- exactly the bug matcher.py's own by_epoch grouping exists to
    # avoid.
    by_epoch: dict[float, list[int]] = defaultdict(list)
    for i, r in enumerate(positional):
        by_epoch[matcher._to_jyear(r.obs_date)].append(i)

    propagated_by_epoch_and_star: dict[float, dict[int, SkyCoord]] = {}
    if star_positions:
        propagate_rows = [(row[0], row[3], row[4], row[5], row[6], row[7]) for row in star_positions.values()]
        for epoch in by_epoch:
            ids, propagated = matcher._propagate(propagate_rows, epoch)
            propagated_by_epoch_and_star[epoch] = dict(zip(ids, propagated))

    # Coarse, padded fetch (see _gaia_cone_search_batch) -- bucketed by
    # distance from Gaia DR3's own fixed 2016.0 epoch, not by calendar
    # decade. A single worst-case radius forces every record to pay for
    # whichever one observation in the chunk happens to be oldest --
    # confirmed live: dao's own backlog spans 1986-2026, padding the search
    # radius to ~369" (6.1', ~36x the unpadded area) for every record in a
    # chunk even though most of them are far more recent and need much
    # less. Bucketing by *calendar* decade would still needlessly split,
    # e.g., a 2019 and a 2020 record into separate buckets/queries despite
    # both being only ~3-4 years from 2016.0 -- and wouldn't exploit that
    # padding is symmetric (a record 6 years before 2016 needs exactly the
    # padding as one 6 years after). Bucketing by |year - 2016| in fixed-
    # width tiers instead groups by what actually drives the padding, while
    # still bounding the extra Gaia round-trips per chunk to a handful.
    epoch_buckets: dict[int, list[int]] = defaultdict(list)
    for i, r in enumerate(positional):
        years_from_gaia_epoch = abs(r.obs_date.year - matcher.GAIA_DR3_REF_EPOCH)
        epoch_buckets[int(years_from_gaia_epoch // GAIA_QUERY_TIME_BUCKET_YEARS)].append(i)

    # Sky-position bucketing nested underneath the epoch bucketing (see
    # GAIA_QUERY_SKY_BUCKET_DEG) -- groups each epoch bucket's records by
    # coarse (RA, Dec) cell before querying, so one Gaia round-trip searches
    # a spatially clustered batch of points instead of one scattered across
    # the whole sky.
    live_hits_raw: dict[int, list[tuple]] = {}
    for epoch_idxs in epoch_buckets.values():
        sky_buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in epoch_idxs:
            sky_buckets[_sky_bucket(positional[i].ra, positional[i].dec)].append(i)

        for idxs in sky_buckets.values():
            bucket_records = [positional[i] for i in idxs]
            bucket_max_years = max(
                abs(matcher._to_jyear(r.obs_date) - matcher.GAIA_DR3_REF_EPOCH) for r in bucket_records
            )
            bucket_hits = _gaia_cone_search_batch(bucket_records, SHITTY_MATCH_RADIUS_ARCSEC, bucket_max_years)
            for local_rec_id, hits in bucket_hits.items():
                live_hits_raw[idxs[local_rec_id]] = hits

    # Dedup live candidates by gaia_source_id across the whole batch (the
    # same nearby star can show up as a raw candidate for many records) so
    # each one is only propagated once per epoch, same as tracked stars.
    live_astrometry: dict[int, tuple] = {}
    for hits in live_hits_raw.values():
        for gaia_source_id, ra, dec, pmra, pmdec, phot_g_mean_mag in hits:
            live_astrometry[gaia_source_id] = (ra, dec, matcher.GAIA_DR3_REF_EPOCH, pmra, pmdec, phot_g_mean_mag)

    propagated_by_epoch_and_live_id: dict[float, dict[int, SkyCoord]] = {}
    if live_astrometry:
        propagate_rows = [(gid, row[0], row[1], row[2], row[3], row[4]) for gid, row in live_astrometry.items()]
        for epoch in by_epoch:
            ids, propagated = matcher._propagate(propagate_rows, epoch)
            propagated_by_epoch_and_live_id[epoch] = dict(zip(ids, propagated))

    with conn.cursor() as cur:
        for epoch, indices in by_epoch.items():
            propagated_by_star_id = propagated_by_epoch_and_star.get(epoch, {})
            propagated_by_live_id = propagated_by_epoch_and_live_id.get(epoch, {})
            for i in indices:
                r = positional[i]
                target = SkyCoord(ra=r.ra * u.deg, dec=r.dec * u.deg)

                by_gaia_id: dict[int, Candidate] = {}
                tracked_untagged = []
                for star_id, gaia_source_id, source_catalog, ra, dec, ref_epoch, pmra, pmdec, phot_g_mean_mag in star_positions.values():
                    sep = propagated_by_star_id[star_id].separation(target).arcsec
                    if sep > SHITTY_MATCH_RADIUS_ARCSEC:
                        continue
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

                # PM-propagated the same way as tracked stars above -- see
                # _gaia_cone_search_batch's docstring for why this matters
                # (a fast-mover found by the padded coarse fetch can still
                # sit outside the true 60" radius once precisely propagated
                # to this record's own epoch, and must be filtered out here,
                # not trusted just for having appeared in the raw hit list).
                for gaia_source_id, _ra, _dec, _pmra, _pmdec, phot_g_mean_mag in live_hits_raw.get(i, []):
                    if gaia_source_id in by_gaia_id:
                        continue  # already tracked -- richer record wins
                    sep = propagated_by_live_id[gaia_source_id].separation(target).arcsec
                    if sep > SHITTY_MATCH_RADIUS_ARCSEC:
                        continue
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
    return counts
