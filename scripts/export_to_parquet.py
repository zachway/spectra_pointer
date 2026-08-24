"""One-off/periodic: export the live Postgres tables to Parquet files where
joy's Apache can serve them directly.

webapp.app no longer holds a live DATABASE_URL connection — it reads a
Parquet snapshot over plain HTTP instead (see its module docstring). That
snapshot needs to land somewhere joy's Apache (mod_userdir) already serves
publicly, e.g. ~/public_html/spectra_data on morgan — since morgan and joy
share the same NFS home directory, writing there is enough, no separate
publish/sync step. This script is the only thing besides sync.main and
ingest.add_star that still talks to the real Postgres database. There is no
automatic trigger — run it by hand (or your own cron) whenever you want the
hosted search to pick up a run's worth of sync results.

Usage:
    DATABASE_URL=postgresql:///spectra_local \\
    python3 -m scripts.export_to_parquet --out-dir ~/public_html/spectra_data
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import tempfile

import duckdb
import healpy
import numpy as np
import psycopg

from webapp.instrument_wavelengths import INSTRUMENT_WAVELENGTH_RANGE_NM

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TABLES = ["stars", "archives", "spectroscopy_holdings", "archive_sync_state"]

# Per-archive status breakdown (last sync time/status, observation date
# range, plus a count per match category) for the Archive Status page. Was
# assembled live in webapp.app from a plain LEFT JOIN of
# archives/archive_sync_state -- cheap on its own (both are small tables),
# but the richer per-archive counts the page now shows (how many
# direct-Gaia-matched, name-resolved, positional, needs-review, skipped)
# need a GROUP BY over the full, ever-growing holdings table, same
# OOM-shaped risk as everything else precomputed here.
#
# category must be a CASE, not COALESCE(match_method, match_status) --
# observed that match_method is NOT null on skipped/needs_review rows
# (it retains whichever method was *attempted*, e.g. positional_easy_match
# tried and failed -> skipped, but match_method still says
# positional_easy_match). COALESCE would pick match_method every time it's
# non-null, silently recategorizing skipped/needs_review rows under
# whatever method almost worked -- observed as a real bug: 5.7M
# skipped rows were showing up as "Positional" matches on the Archive
# Status page instead of "Skipped".
ARCHIVE_STATUS_QUERY = """
WITH counts AS (
    SELECT
        archive_code,
        CASE WHEN match_status = 'matched' THEN match_method ELSE match_status END AS category,
        count(*) AS n
    FROM pg.spectroscopy_holdings
    GROUP BY archive_code, category
),
date_ranges AS (
    SELECT archive_code, min(obs_date) AS min_obs_date, max(obs_date) AS max_obs_date
    FROM pg.spectroscopy_holdings
    WHERE obs_date IS NOT NULL
    GROUP BY archive_code
)
SELECT
    a.archive_code,
    a.display_name,
    s.last_run_at,
    s.last_run_status,
    s.rows_seen_last_run,
    d.min_obs_date,
    d.max_obs_date,
    c.category,
    c.n
FROM pg.archives a
LEFT JOIN pg.archive_sync_state s ON s.archive_code = a.archive_code
LEFT JOIN date_ranges d ON d.archive_code = a.archive_code
LEFT JOIN counts c ON c.archive_code = a.archive_code
ORDER BY a.display_name, c.category
"""

# Per-archive, per-instrument holdings counts -- backs the "Tracked
# instruments" table on the Archive Status page. Same GROUP-BY-over-the-
# full-holdings-table reasoning as everything else precomputed here. One
# archive can span several instruments (e.g. Gemini alone has 18), so this
# is its own table rather than folded into ARCHIVE_STATUS_QUERY above.
INSTRUMENTS_QUERY = """
SELECT a.display_name, h.instrument, count(*) AS n
FROM pg.spectroscopy_holdings h
JOIN pg.archives a ON a.archive_code = h.archive_code
WHERE h.instrument IS NOT NULL
GROUP BY a.display_name, h.instrument
ORDER BY a.display_name, n DESC
"""

# Per-(instrument, HEALPix cell) observation counts, for the /instruments
# page's sky-coverage-by-instrument map. This replaced an earlier per-
# instrument random() sample (capped at 2000 points/instrument, top 12
# instruments only) -- that showed an arbitrary subset of each instrument's
# footprint rather than its actual density, and needed a legend click to
# isolate one instrument at a time. A true density histogram needs a count
# per sky cell over *every* position-tagged row, not a capped sample, so
# there's no meaningful top-N cutoff here -- the query below covers every
# instrument with position data, and the per-instrument dropdown on the page
# picks which one's histogram to render.
#
# DuckDB has no native HEALPix function, so the actual ang2pix binning can't
# be pushed down as SQL (unlike everything else in this file) -- it happens
# in Python via healpy below, then the aggregated (instrument, cell, count)
# result is fed back into DuckDB just for the final atomic Parquet write.
# Same "memory isn't capped here" tradeoff as the sample this replaced: this
# script runs on morgan, not the 1GiB-limited Cloud Run container, so
# pulling every position-tagged row into Python for one precompute pass is
# fine even at tens of millions of rows.
INSTRUMENT_HEALPIX_NSIDE = 32

INSTRUMENT_POSITIONS_QUERY = """
SELECT instrument, raw_ra, raw_dec
FROM pg.spectroscopy_holdings
WHERE instrument IS NOT NULL AND raw_ra IS NOT NULL AND raw_dec IS NOT NULL
AND raw_ra BETWEEN 0 AND 360 AND raw_dec BETWEEN -90 AND 90
"""


def _export_instrument_healpix(con: duckdb.DuckDBPyConnection, path: str) -> None:
    # fetchnumpy() rather than .df()/.fetch_arrow_table() -- neither pandas
    # nor pyarrow is a guaranteed dependency here (both are only pulled in as
    # optional extras of packages already in requirements.txt), while numpy
    # is a hard dependency of astropy/astropy-healpix/healpy, all of which
    # are required unconditionally. fetchnumpy() needs only numpy.
    result = con.execute(_localize(INSTRUMENT_POSITIONS_QUERY)).fetchnumpy()
    ra = result["raw_ra"].astype(np.float64)
    dec = result["raw_dec"].astype(np.float64)
    pix = healpy.ang2pix(INSTRUMENT_HEALPIX_NSIDE, ra, dec, lonlat=True)

    # Group by (instrument, pix) without pandas: factorize instrument names
    # to integer codes, fold (code, pix) into one int64 key so np.unique can
    # do the count in a single vectorized pass, then unpack the key back
    # apart. npix comfortably fits pix (< 12*64**2) in the low bits for any
    # nside this project would realistically use.
    npix = healpy.nside2npix(INSTRUMENT_HEALPIX_NSIDE)
    uniq_instruments, instrument_codes = np.unique(result["instrument"], return_inverse=True)
    combined_keys = instrument_codes.astype(np.int64) * npix + pix.astype(np.int64)
    uniq_keys, counts = np.unique(combined_keys, return_counts=True)
    out_instrument = uniq_instruments[uniq_keys // npix]
    out_pixel = (uniq_keys % npix).astype(np.int64)

    con.execute("CREATE OR REPLACE TEMP TABLE instrument_healpix_counts "
                "(instrument VARCHAR, healpix_pixel BIGINT, n BIGINT)")
    con.executemany(
        "INSERT INTO instrument_healpix_counts VALUES (?, ?, ?)",
        list(zip(out_instrument.tolist(), out_pixel.tolist(), counts.tolist())),
    )
    _atomic_copy(
        con,
        "SELECT instrument, healpix_pixel, n FROM instrument_healpix_counts ORDER BY instrument, healpix_pixel",
        path,
    )


# Precomputed sample of all stars for the /sky all-sky map -- was a live
# `USING SAMPLE n` against the full `stars` table on every page load, which
# forces DuckDB's remote-parquet reader to scan nearly the whole ~500MB+
# file over HTTP from joy every time (observed: ~27s for a 5,000-row
# sample against a 9.8M-row table, the dominant cost behind "webapp is
# sluggish switching tabs"). Precomputed here where the SAMPLE only runs
# once per export instead of once per tab click; must match webapp.app's
# SKY_SAMPLE_SIZE constant.
SKY_SAMPLE_SIZE = 30000

SKY_SAMPLE_QUERY = f"""
    SELECT gaia_source_id, ra, dec, phot_g_mean_mag,
           COALESCE(name_aliases[1], input_name, CAST(gaia_source_id AS VARCHAR)) AS known_as
    FROM pg.stars
    WHERE ra IS NOT NULL AND dec IS NOT NULL AND phot_g_mean_mag IS NOT NULL
    USING SAMPLE {SKY_SAMPLE_SIZE}
"""

# Precomputed (normalized name -> identifier) index backing
# webapp.app._lookup_local_star's name-matching branch. That branch used to
# filter `stars` directly with normalize() wrapped around input_name/
# name_aliases in the WHERE clause -- observed that wrapping the
# filtered columns in a function defeats Parquet's row-group min/max pruning
# entirely (pruning needs a bare column vs. a constant), so *every*
# name-based search -- i.e. nearly every real query, since people type "HD
# 110067" rather than a Gaia source_id -- pulled nearly the entire
# multi-hundred-MB stars.parquet over HTTP. Same OOM/timeout shape as the
# stars/spectroscopy_holdings sorting fixes below, just on the search path
# those fixes don't cover (they only help the numeric-ID branch).
#
# This table holds only a few short columns (vs. stars' full wide schema),
# one row per input_name and one more per alias, so even a full scan of it
# is fast regardless of pruning. webapp.app's name branch looks up here
# first to resolve gaia_source_id/bsc_hr_number, then falls through to the
# already-pruning-friendly numeric lookup against `stars`.
#
# NORMALIZE_SQL must match webapp.app's _normalize_star_name exactly (same
# duplicated-constant tradeoff as SKY_SAMPLE_SIZE/CMD_SAMPLE_SIZE below --
# export_to_parquet.py can't import webapp.app without triggering its
# module-level _make_connection(), which needs SPECTRA_DATA_URL/DIR set)
# -- otherwise a name normalized one way here and looked up another way in
# the webapp would silently never match.
STAR_NAME_INDEX_NORMALIZE_SQL = r"lower(regexp_replace(regexp_replace(trim({col}), '^(NAME|\*)\s+', '', 'i'), '\s+', ' ', 'g'))"

STAR_NAME_INDEX_QUERY = f"""
SELECT DISTINCT
    {STAR_NAME_INDEX_NORMALIZE_SQL.format(col="name")} AS normalized_name,
    gaia_source_id,
    bsc_hr_number
FROM (
    SELECT gaia_source_id, bsc_hr_number, input_name AS name
    FROM pg.stars
    UNION ALL
    SELECT gaia_source_id, bsc_hr_number, UNNEST(name_aliases) AS name
    FROM pg.stars
    WHERE name_aliases IS NOT NULL
)
WHERE name IS NOT NULL AND trim(name) != ''
ORDER BY normalized_name
"""

LEADERBOARD_TOP_N = 20

# Fully precomputed Leaderboard chart data — not just the raw per-(star,
# period) counts, but the actual top-N-per-period selection webapp.app plots.
#
# First cut of this only moved the raw GROUP BY here and left webapp.app to
# pick the top 5 per period in Python — that GROUP BY alone still produces
# one row per (star, period) with no cap on distinct stars, and this catalog
# tracks millions of stars (DESI/SDSS-V/LAMOST alone put it past 2M), so the
# "aggregated, therefore small" assumption baked into the old in-app version
# was wrong at this catalog's actual scale. webapp.app was then calling
# Python's sorted() over the full ~2M-star population once per period
# (~70+ periods, x2 for both the within-period and cumulative rankings) just
# to keep the top 5 -- observed as what was actually driving the OOM
# (1.1GB+) even after the raw-GROUP-BY-only version of this fix shipped.
#
# Second cut moved the ranking into one SQL query using window functions
# over a star x period grid (cross join, needed so a star's cumulative
# total carries forward through periods where it had no new observations,
# and can still rank). That worked at ~2M stars / ~70 periods, but LAMOST
# pushed the real star count with dated observations to 6.1M -- a 6.1M x
# 74 = ~453M row grid -- which OOM'd DuckDB's 24.9 GiB memory_limit even
# with disk-spilling enabled (temp_directory set), observed. The
# grid was always ~60x bigger than it needed to be: only ~7.3M (star,
# period) pairs actually have any observations at all.
#
# Rewritten below to never materialize that grid:
#   - Within-period top-N doesn't need it at all -- a star with zero new
#     observations this period can never outrank one with a positive
#     count, so ranking directly against the real (star, period, n) rows
#     (no zero-filled ones) gives the same top-N.
#   - Cumulative top-N does need every active star's running total as of
#     each period (including periods where it didn't newly observe), but
#     that's a sweep, not a cross join: walk the ~74 periods in order,
#     keep one running total per star ever observed (bounded by star
#     count, not star x period), and snapshot the top-N after each
#     period's update. Same ranking result as the old grid-based window
#     function, without ever holding more than one row per star.
#   - Only once the (small, low tens-of-thousands) set of stars that ever
#     made top-N by either metric is known does a star x period grid get
#     built -- cast stars only, ~74x that small set, nowhere near the
#     full 6.1M-star grid.
def _common_name_expr(alias_col: str, input_col: str, id_col: str) -> str:
    # name_aliases[1] is just whatever token SIMBAD's "ids" field happened
    # to list first (see ingest.add_star) -- often a Bayer/Flamsteed
    # designation like "* alf Boo" or a catalog number, not the star's
    # actual common name. SIMBAD does mark common names, with a "NAME "
    # prefix (e.g. "NAME Arcturus") -- same convention webapp.app's
    # _normalize_star_name already strips for search matching. Prefer that
    # alias, stripped of its prefix, before falling back to the old
    # first-alias/input_name/gaia_source_id chain.
    return f"""COALESCE(
        list_transform(
            list_filter({alias_col}, x -> x LIKE 'NAME %'),
            x -> trim(substr(x, 6))
        )[1],
        {alias_col}[1],
        {input_col},
        CAST({id_col} AS VARCHAR)
    )"""


LEADERBOARD_COUNTS_QUERY = """
SELECT
    star_id,
    year(obs_date) AS yr,
    CASE WHEN month(obs_date) <= 6 THEN 1 ELSE 2 END AS half,
    count(*) AS n
FROM pg.spectroscopy_holdings
WHERE obs_date IS NOT NULL AND star_id IS NOT NULL
GROUP BY star_id, yr, half
"""

# gaia_source_id (still a real, if no longer primary, column on stars -- see
# db/schema.sql) is only resolved at the very end, via the join to pg.stars
# below -- everything upstream (holdings, the grid, both top-N temp tables)
# is keyed by star_id, since that's the only identifier every tracked star
# has (a small number are BSC5-sourced with no gaia_source_id at all -- see
# db/migrations/0001_star_id_surrogate_key.sql).
LEADERBOARD_FINAL_QUERY = f"""
WITH cast_stars AS (
    SELECT star_id FROM leaderboard_top_period
    UNION
    SELECT star_id FROM leaderboard_top_cum
),
periods AS (
    SELECT DISTINCT yr, half FROM leaderboard_counts
),
grid AS (
    SELECT cs.star_id, p.yr, p.half
    FROM cast_stars cs CROSS JOIN periods p
)
SELECT
    s.star_id,
    s.gaia_source_id,
    s.bsc_hr_number,
    {_common_name_expr('s.name_aliases', 's.input_name', 's.gaia_source_id')} AS label,
    g.yr, g.half,
    tp.n AS within_n,
    tc.cum_n AS cumulative_n
FROM grid g
LEFT JOIN leaderboard_top_period tp USING (star_id, yr, half)
LEFT JOIN leaderboard_top_cum tc USING (star_id, yr, half)
JOIN pg.stars s ON s.star_id = g.star_id
ORDER BY s.star_id, g.yr, g.half
"""


def _export_leaderboard(con: duckdb.DuckDBPyConnection, path: str) -> None:
    con.execute(f"CREATE OR REPLACE TEMP TABLE leaderboard_counts AS {_localize(LEADERBOARD_COUNTS_QUERY)}")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE leaderboard_top_period AS
        SELECT star_id, yr, half, n
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY yr, half ORDER BY n DESC, star_id
            ) AS period_rank
            FROM leaderboard_counts
        )
        WHERE period_rank <= {LEADERBOARD_TOP_N}
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE leaderboard_cum_state (
            star_id BIGINT PRIMARY KEY, cum_n BIGINT
        )
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE leaderboard_top_cum (
            star_id BIGINT, yr INTEGER, half INTEGER, cum_n BIGINT
        )
    """)
    periods = con.execute(
        "SELECT DISTINCT yr, half FROM leaderboard_counts ORDER BY yr, half"
    ).fetchall()
    for yr, half in periods:
        con.execute(
            """
            INSERT INTO leaderboard_cum_state (star_id, cum_n)
            SELECT star_id, n FROM leaderboard_counts
            WHERE yr = ? AND half = ?
            ON CONFLICT (star_id) DO UPDATE
                SET cum_n = leaderboard_cum_state.cum_n + excluded.cum_n
            """,
            [yr, half],
        )
        con.execute(
            f"""
            INSERT INTO leaderboard_top_cum
            SELECT star_id, ?, ?, cum_n
            FROM leaderboard_cum_state
            ORDER BY cum_n DESC, star_id
            LIMIT {LEADERBOARD_TOP_N}
            """,
            [yr, half],
        )

    _atomic_copy(con, _localize(LEADERBOARD_FINAL_QUERY), path)


DIVERSITY_TOP_N = 20

# Two more /leaderboard charts, alongside the observation-count ones above:
# "most diversely observed" (broadest total wavelength coverage from the
# distinct instruments ever pointed at a star, in dex -- sum of each
# distinct instrument's own log10(wave_max/wave_min), not a literal
# overlap-aware union of ranges: a star seen by two spectrographs whose
# ranges mostly overlap still gets both instruments' full widths added, and
# a star seen by disjoint bands (e.g. an X-ray grating and an optical
# echelle) gets a wider total than the gap between those bands would
# suggest if you tried to read this as one contiguous span. "Covered" here
# means "how much distinct wavelength real estate its instrument mix
# spans", the same simplification INSTRUMENT_WAVELENGTH_RANGE_NM's own
# multi-band-envelope caveat already accepts for single instruments -- true
# interval-merge is a fair amount more SQL for a metric nobody asked to be
# that precise) and "most popular" (raw count of distinct instruments ever
# pointed at a star, no wavelength dict needed at all). Repeat holdings from
# the same instrument contribute nothing further to either metric, by
# construction below (only a star+instrument pair's *first* period counts).
#
# Unlike LEADERBOARD_TOP_N's within-period/cumulative pair, both of these
# metrics are inherently cumulative only (there's no meaningful "new
# instruments this half-year alone" chart) -- so, simpler than
# _export_leaderboard's period-by-period sweep (which exists specifically to
# re-rank *every* period, since a star's rank among all ~6M+ tracked stars
# at some past period can't be known from present-day data alone), this
# picks today's top DIVERSITY_TOP_N stars by either metric just once, then
# reconstructs their own historical trajectory -- valid here because each
# star's trajectory is a simple non-decreasing running total, fully
# determined by that one star's own per-period deltas (a window-function
# SUM), not by comparison against every other star at each past period the
# way a rank is.
DIVERSITY_COUNTS_QUERY = """
SELECT DISTINCT
    h.star_id,
    year(h.obs_date) AS yr,
    CASE WHEN month(h.obs_date) <= 6 THEN 1 ELSE 2 END AS half,
    a.display_name || '::' || h.instrument AS instrument_key
FROM pg.spectroscopy_holdings h
JOIN pg.archives a ON a.archive_code = h.archive_code
WHERE h.obs_date IS NOT NULL AND h.star_id IS NOT NULL AND h.instrument IS NOT NULL
"""

DIVERSITY_FINAL_QUERY = f"""
SELECT
    s.star_id, s.gaia_source_id, s.bsc_hr_number,
    {_common_name_expr('s.name_aliases', 's.input_name', 's.gaia_source_id')} AS label,
    g.yr, g.half, g.cum_instrument_count, g.cum_loglam_covered
FROM diversity_grid_filled g
JOIN pg.stars s ON s.star_id = g.star_id
ORDER BY s.star_id, g.yr, g.half
"""


def _instrument_log_width_rows() -> list[tuple[str, float]]:
    # Same "archive display_name::instrument" key shape DIVERSITY_COUNTS_QUERY
    # builds in SQL -- see that query's instrument_key column.
    rows = []
    for (display_name, instrument), (wave_min, wave_max) in INSTRUMENT_WAVELENGTH_RANGE_NM.items():
        if wave_min <= 0 or wave_max <= wave_min:
            continue
        rows.append((f"{display_name}::{instrument}", math.log10(wave_max / wave_min)))
    return rows


def _export_diversity(con: duckdb.DuckDBPyConnection, path: str) -> None:
    con.execute(f"CREATE OR REPLACE TEMP TABLE diversity_counts AS {_localize(DIVERSITY_COUNTS_QUERY)}")

    # Every known instrument's log-wavelength width, keyed the same way as
    # diversity_counts.instrument_key -- a star+instrument pair with no entry
    # here (see INSTRUMENT_WAVELENGTH_RANGE_NM's own "left out rather than
    # guessed" cases) still counts toward cum_instrument_count below, just
    # contributes 0 to cum_loglam_covered via the LEFT JOIN + coalesce further
    # down, same graceful-degradation shape as the search page's chart.
    con.execute("CREATE OR REPLACE TEMP TABLE instrument_log_width (instrument_key VARCHAR PRIMARY KEY, log_width DOUBLE)")
    con.executemany("INSERT INTO instrument_log_width VALUES (?, ?)", _instrument_log_width_rows())

    # A dense 1..N period index (not the raw yr/half pair) so "ORDER BY
    # period" below is a plain integer sort/window frame, and so periods
    # with literally zero (star, instrument) first-sightings still get a
    # slot for the forward-fill grid further down.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE diversity_periods AS
        SELECT yr, half, ROW_NUMBER() OVER (ORDER BY yr, half) AS period_idx
        FROM (SELECT DISTINCT yr, half FROM diversity_counts)
    """)

    # First period each (star, instrument) pair was ever seen -- later
    # sightings of the same pair (repeat observations, or the same
    # instrument again in a later period) don't move this, which is exactly
    # the "don't count the same instrument twice" dedup both leaderboard
    # metrics need.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE instrument_first_seen AS
        SELECT dc.star_id, dc.instrument_key, min(p.period_idx) AS period_idx
        FROM diversity_counts dc
        JOIN diversity_periods p USING (yr, half)
        GROUP BY dc.star_id, dc.instrument_key
    """)

    # Collapse each star's newly-first-seen instruments within a period into
    # that period's delta -- count and log-width both just sum over however
    # many new instruments arrived that period (0 for most star/period
    # combos, hence not a dense grid).
    con.execute("""
        CREATE OR REPLACE TEMP TABLE diversity_period_deltas AS
        SELECT
            ifs.star_id, ifs.period_idx,
            count(*) AS new_instrument_count,
            sum(coalesce(lw.log_width, 0)) AS new_loglam
        FROM instrument_first_seen ifs
        LEFT JOIN instrument_log_width lw ON lw.instrument_key = ifs.instrument_key
        GROUP BY ifs.star_id, ifs.period_idx
    """)

    # Running totals, one row per (star, period-they-had-an-update) --
    # sparse, not yet a full star x all-periods grid.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE diversity_cum AS
        SELECT star_id, period_idx,
            SUM(new_instrument_count) OVER (PARTITION BY star_id ORDER BY period_idx) AS cum_instrument_count,
            SUM(new_loglam) OVER (PARTITION BY star_id ORDER BY period_idx) AS cum_loglam_covered
        FROM diversity_period_deltas
    """)

    # Each star's own final (as-of-today) totals -- QUALIFY keeps just the
    # last period_idx row per star, cheap since diversity_cum is already one
    # row per (star, update period), bounded by real star x instrument
    # activity rather than the full tracked-star population.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE diversity_final AS
        SELECT star_id, cum_instrument_count, cum_loglam_covered
        FROM diversity_cum
        QUALIFY ROW_NUMBER() OVER (PARTITION BY star_id ORDER BY period_idx DESC) = 1
    """)

    # Cast stars: union of today's top DIVERSITY_TOP_N by each metric --
    # small (at most 2x DIVERSITY_TOP_N), same "only chart the stars that
    # ever made a top-N list" shape as leaderboard's cast_stars.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE diversity_cast_stars AS
        (SELECT star_id FROM diversity_final ORDER BY cum_instrument_count DESC, star_id LIMIT {DIVERSITY_TOP_N})
        UNION
        (SELECT star_id FROM diversity_final ORDER BY cum_loglam_covered DESC, star_id LIMIT {DIVERSITY_TOP_N})
    """)

    # Full cast_stars x all-periods grid, forward-filled across periods a
    # cast star had no new instrument (a plain LEFT JOIN would otherwise
    # leave those periods NULL, showing as a break in the line even though
    # the true cumulative value just held steady) -- IGNORE NULLS carries
    # each star's last real value forward; genuinely-before-this-star's-
    # first-instrument periods stay NULL, same "don't drag the line across
    # time it doesn't apply to" behavior as leaderboard's own charts.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE diversity_grid_filled AS
        SELECT star_id, yr, half,
            LAST_VALUE(cum_instrument_count IGNORE NULLS) OVER (PARTITION BY star_id ORDER BY period_idx) AS cum_instrument_count,
            LAST_VALUE(cum_loglam_covered IGNORE NULLS) OVER (PARTITION BY star_id ORDER BY period_idx) AS cum_loglam_covered
        FROM (
            SELECT cs.star_id, p.yr, p.half, p.period_idx, dc.cum_instrument_count, dc.cum_loglam_covered
            FROM diversity_cast_stars cs
            CROSS JOIN diversity_periods p
            LEFT JOIN diversity_cum dc ON dc.star_id = cs.star_id AND dc.period_idx = p.period_idx
        )
    """)

    _atomic_copy(con, _localize(DIVERSITY_FINAL_QUERY), path)


# Precomputed "most observed" star list for the CMD page — was a random
# USING SAMPLE over `stars` (cheap: no join needed), changed to the N
# most-observed stars instead, which does need a join against the full
# holdings table to count observations per star. Counting is done here
# rather than in webapp.app for the same reason as the Leaderboard: no
# reason to make the memory-constrained hosted container re-scan a
# multi-million-row, ever-growing table on every request when the output is
# a fixed-size, infrequently-changing top-N. Deliberately counts *all*
# holdings rows, not just ones with obs_date (unlike the Leaderboard query
# above) -- DESI and SDSS-V carry no per-observation dates at all (see
# webapp.app's /info page), so filtering on obs_date here would silently
# drop their stars from "most observed" entirely.
CMD_SAMPLE_SIZE = 30000

CMD_STARS_QUERY = f"""
WITH obs_counts AS (
    SELECT star_id, count(*) AS n
    FROM pg.spectroscopy_holdings
    WHERE star_id IS NOT NULL
    GROUP BY star_id
)
SELECT
    s.gaia_source_id,
    s.phot_bp_mean_mag - s.phot_rp_mean_mag AS bp_rp,
    s.phot_g_mean_mag + 5 * log10(s.parallax) - 10 AS abs_g_mag,
    {_common_name_expr('s.name_aliases', 's.input_name', 's.gaia_source_id')} AS label
FROM pg.stars s
JOIN obs_counts oc ON oc.star_id = s.star_id
WHERE s.phot_bp_mean_mag IS NOT NULL AND s.phot_rp_mean_mag IS NOT NULL
  AND s.phot_g_mean_mag IS NOT NULL AND s.parallax > 0
ORDER BY oc.n DESC
LIMIT {CMD_SAMPLE_SIZE}
"""


# /leaderboard (formerly /timeplots, and /stats before that) used to run eight separate full (or
# near-full) scans per request -- most-observed, trending, a bare count(*),
# by-archive, by-method against spectroscopy_holdings, plus nearest,
# fastest-movers and a spectral-type histogram against `stars` -- against the
# same growing tables responsible for the Leaderboard's OOM. None of these
# involve a cross join like the Leaderboard did, so each individually is a
# cheap single-pass aggregation, but "cheap x8, every request, over
# ever-growing tables, streamed over HTTP into a memory-capped container"
# adds up the same way -- observed: nearest/fastest-movers/spectral-
# types each took multiple seconds against a 9.8M-row `stars` served over
# plain HTTP, since ORDER BY/GROUP BY with no filter can't skip row groups.
# Precomputed here as one small JSON blob instead — total_stars and
# total_holdings are just scalars, and every list here is bounded (top-20,
# or one row per archive/match-method/spectral-bucket, all small fixed sets)
# regardless of how large the underlying tables get.
TRENDING_YEARS = 5
MOST_OBSERVED_TOP_N = 20
TRENDING_TOP_N = 20
NEAREST_TOP_N = 20
FASTEST_MOVERS_TOP_N = 20

STATS_QUERIES = {
    "most_observed": f"""
        SELECT s.gaia_source_id, s.bsc_hr_number,
               {_common_name_expr('s.name_aliases', 's.input_name', 's.gaia_source_id')} AS known_as,
               count(*) AS n
        FROM pg.spectroscopy_holdings h
        JOIN pg.stars s ON s.star_id = h.star_id
        GROUP BY s.star_id, s.gaia_source_id, s.bsc_hr_number, s.name_aliases, s.input_name
        ORDER BY n DESC
        LIMIT {MOST_OBSERVED_TOP_N}
    """,
    "trending": f"""
        SELECT s.gaia_source_id, s.bsc_hr_number,
               {_common_name_expr('s.name_aliases', 's.input_name', 's.gaia_source_id')} AS known_as,
               count(*) AS n
        FROM pg.spectroscopy_holdings h
        JOIN pg.stars s ON s.star_id = h.star_id
        WHERE h.obs_date >= CURRENT_DATE - INTERVAL '{TRENDING_YEARS}' YEAR
        GROUP BY s.star_id, s.gaia_source_id, s.bsc_hr_number, s.name_aliases, s.input_name
        ORDER BY n DESC
        LIMIT {TRENDING_TOP_N}
    """,
    "by_archive": """
        SELECT a.display_name, count(*) AS n
        FROM pg.spectroscopy_holdings h
        JOIN pg.archives a ON a.archive_code = h.archive_code
        GROUP BY a.display_name
        ORDER BY n DESC
    """,
    "by_method": """
        SELECT match_method, count(*) AS n
        FROM pg.spectroscopy_holdings
        WHERE match_status = 'matched'
        GROUP BY match_method
        ORDER BY n DESC
    """,
    "nearest": f"""
        SELECT gaia_source_id, bsc_hr_number,
               {_common_name_expr('name_aliases', 'input_name', 'gaia_source_id')} AS known_as,
               1000.0 / parallax AS distance_pc
        FROM pg.stars
        WHERE parallax > 0
        ORDER BY parallax DESC
        LIMIT {NEAREST_TOP_N}
    """,
    "fastest_movers": f"""
        SELECT gaia_source_id, bsc_hr_number,
               {_common_name_expr('name_aliases', 'input_name', 'gaia_source_id')} AS known_as,
               sqrt(pmra * pmra + pmdec * pmdec) AS total_pm
        FROM pg.stars
        WHERE pmra IS NOT NULL AND pmdec IS NOT NULL
        ORDER BY total_pm DESC
        LIMIT {FASTEST_MOVERS_TOP_N}
    """,
    "spectral_types": """
        SELECT
            CASE
                WHEN phot_bp_mean_mag - phot_rp_mean_mag < 0.0 THEN 'O/B (hot)'
                WHEN phot_bp_mean_mag - phot_rp_mean_mag < 0.3 THEN 'A'
                WHEN phot_bp_mean_mag - phot_rp_mean_mag < 0.6 THEN 'F'
                WHEN phot_bp_mean_mag - phot_rp_mean_mag < 0.9 THEN 'G'
                WHEN phot_bp_mean_mag - phot_rp_mean_mag < 1.5 THEN 'K'
                ELSE 'M (cool)'
            END AS bucket,
            count(*) AS n
        FROM pg.stars
        WHERE phot_bp_mean_mag IS NOT NULL AND phot_rp_mean_mag IS NOT NULL
        GROUP BY bucket
    """,
}


# Precomputed for the More Info page's needs-review/skipped sections -- these
# used to run four separate live queries against spectroscopy_holdings on
# every /info request (a count(), two JOIN+ORDER BY updated_at DESC LIMIT 20,
# and a GROUP BY), each one a fresh full scan of the 1GB+ remote Parquet file
# over HTTP. Individually a couple hundred ms to ~1s each from a fast
# connection, but confirmed this is the slow-page complaint in practice
# (Cloud Run's connection to joy is neither fast nor consistent, and it's the
# same live-query-over-the-full-holdings-table shape already fixed for /sky,
# /leaderboard, and /triage elsewhere in this module -- /info was just missed
# in that pass). NEEDS_REVIEW_QUERY/SKIPPED_QUERY only cover the unfiltered
# default view; /info's per-archive filter (?archive=...) is rare enough,
# and cheap enough once narrowed to one archive_code, to stay a live query in
# webapp.app.
NEEDS_REVIEW_TOP_N = 20
SKIPPED_TOP_N = 20

NEEDS_REVIEW_QUERY = f"""
SELECT a.display_name, h.raw_target_name, h.raw_ra, h.raw_dec, h.obs_date, h.theta_arcsec, h.reduction_status
FROM pg.spectroscopy_holdings h
JOIN pg.archives a ON a.archive_code = h.archive_code
WHERE h.match_status = 'needs_review'
ORDER BY h.updated_at DESC
LIMIT {NEEDS_REVIEW_TOP_N}
"""

SKIPPED_BY_ARCHIVE_QUERY = """
SELECT h.archive_code, a.display_name, count(*) AS n
FROM pg.spectroscopy_holdings h
JOIN pg.archives a ON a.archive_code = h.archive_code
WHERE h.match_status = 'skipped'
GROUP BY h.archive_code, a.display_name
ORDER BY n DESC
"""

SKIPPED_QUERY = f"""
SELECT a.display_name, h.raw_target_name, h.raw_ra, h.raw_dec, h.obs_date, h.reduction_status
FROM pg.spectroscopy_holdings h
JOIN pg.archives a ON a.archive_code = h.archive_code
WHERE h.match_status = 'skipped'
ORDER BY h.updated_at DESC
LIMIT {SKIPPED_TOP_N}
"""


# Slim, position-sorted sibling of spectroscopy_holdings for the search
# page's opt-in "search unmatched records" checkbox -- webapp.app's normal
# radial search only ever queries `stars` (Gaia/BSC5-confirmed positions),
# so a user searching a wider radius than sync.matcher's 1" easy-match
# cutoff has no way to see a nearby record that didn't make it into `stars`
# at all (match_status='skipped', 16.1M rows as of 2026-08-12) or is still
# sitting in needs_review.
#
# Deliberately NOT `SELECT * FROM pg.spectroscopy_holdings ORDER BY
# raw_ra/dec` -- observed via this table's own Parquet column
# metadata that archive_url + archive_obs_id alone account for 61% of the
# ~1.5GB file (two per-row TEXT columns), and a full-width resort wouldn't
# even shrink that: `id`'s current compression benefits from loosely
# tracking insertion order under the main file's `ORDER BY star_id` (see
# below), which a position-bucket sort would scramble. So this exports only
# the columns a cone search + result listing actually needs, and leaves
# archive_url/archive_obs_id on the main file -- webapp.app looks those up
# by `id` afterward, but only for this page's <=200 capped, already-matched
# results, never for the full candidate set the cone search itself scans.
#
# WHERE raw_ra/raw_dec IS NOT NULL cuts straight to the rows a position
# search could ever return -- observed only 17,911,462 of 48,312,943
# total holdings rows carry a position at all (many archives, e.g.
# feros_gavo/flashheros_gavo/salt_hrs/irtf_spex/irtf_ishell, are name-only
# by design, see this file's own per-archive notes in db/schema.sql). Not
# filtered to match_status='skipped' -- the checkbox is meant to search
# "everything not covered by the normal stars-table search's radius", which
# includes needs_review and even already-matched rows a user might still
# want to see alongside unmatched ones (match_status is exported so
# webapp.app can label each result). Observed this whole query,
# sorted, comes to 320.6MB -- comfortably under the 1GiB Cloud Run limit
# even loaded alongside the other exported tables, unlike a full-width
# resorted clone would have been.
#
# ORDER BY raw_dec, not a spatial/HEALPix bucket -- matches the exact
# `raw_dec BETWEEN ? AND ?` pre-filter shape webapp.app's radial search
# already uses against `stars` (see RADIAL_SEARCH_DEFAULT_RADIUS_ARCMIN's
# comment there), so Parquet's row-group min/max stats can actually skip
# row groups outside the search band instead of every query scanning the
# whole 320MB file over httpfs.
HOLDINGS_BY_POSITION_QUERY = """
SELECT id, star_id, archive_code, instrument, obs_date, match_status, raw_target_name, raw_ra, raw_dec
FROM pg.spectroscopy_holdings
WHERE raw_ra IS NOT NULL AND raw_dec IS NOT NULL
ORDER BY raw_dec
"""


# Precomputed per-(archive, reported target name) triage queue -- the
# /triage page used to run this grouping live against the hosted
# DuckDB/Parquet snapshot, but a true GROUP BY (archive_code, raw_target_name)
# over the full skipped set (12M+ rows, 900k+ distinct names) OOM'd the 1 GiB
# Cloud Run container outright, observed against production -- same
# OOM-shaped risk as everything else precomputed here, just without the
# option of even a windowed live fallback (there's no cheap way to know which
# recent rows share a name without grouping first). Computed here instead,
# where memory isn't capped.
#
# Each group's member list is capped at TRIAGE_QUEUE_MAX_RECORDS via a
# ROW_NUMBER()-then-FILTER, not a plain list() -- a plain list() still has to
# build the *entire* array before anyone could trim it, and some names (e.g.
# calibration-frame placeholders repeated across a whole run, or an archive
# that reports no name at all) recur hundreds of thousands of times. The
# FILTER means the aggregate only ever receives up to the cap's worth of
# rows, so the array itself never grows past that -- confirmed against a
# synthetic 200-row group that n_records still reports the true total (200)
# while the array stays capped at 50. NULL and empty-string raw_target_name
# are both treated as "no reported name" (COALESCE + NULLIF/TRIM), falling
# back to one singleton group per record via its archive_obs_id -- otherwise
# every nameless row in an archive would collapse into a single enormous
# group, which is exactly the shape that caused the OOM in the first place.
# TRIAGE_QUEUE_TOP_N bounds how many distinct (archive, name) groups are
# exported, not how many the aggregate has to scan -- the GROUP BY above
# already materializes all ~900k groups before this LIMIT applies, so raising
# it doesn't add meaningfully to the query's cost, only to the size of the
# small pool webapp.app's _shuffle_triage_pool reorders per visitor. Raised
# from 200 to spread contributors across more distinct names instead of
# everyone converging on the identical top-200-by-priority set every day --
# webapp.app's per-request weighting (favoring groups with fewer independent
# votes so far) does the rest of that work, but it can only rebalance within
# whatever this export already exposes.
TRIAGE_QUEUE_MAX_RECORDS = 50
TRIAGE_QUEUE_TOP_N = 2000

TRIAGE_QUEUE_QUERY = f"""
WITH ranked AS (
    SELECT h.archive_code, a.display_name, h.raw_target_name, h.archive_obs_id, h.archive_url,
           h.raw_ra, h.raw_dec, h.obs_date, h.instrument, h.updated_at,
           COALESCE(NULLIF(TRIM(h.raw_target_name), ''), 'obs:' || h.archive_obs_id) AS group_key,
           ROW_NUMBER() OVER (
               PARTITION BY h.archive_code,
                            COALESCE(NULLIF(TRIM(h.raw_target_name), ''), 'obs:' || h.archive_obs_id)
               ORDER BY h.updated_at DESC
           ) AS rn,
           -- Unit-vector components of (raw_ra, raw_dec), summed per group
           -- below to measure how tightly clustered a group's positions
           -- actually are. Avoids the wraparound (RA 359 vs RA 1) and pole
           -- (dec near +/-90) false positives a naive max()-min() on ra/dec
           -- would have. NULL automatically when either coordinate is NULL,
           -- so these just drop out of the sum()/count() below.
           cos(radians(h.raw_dec)) * cos(radians(h.raw_ra)) AS vx,
           cos(radians(h.raw_dec)) * sin(radians(h.raw_ra)) AS vy,
           sin(radians(h.raw_dec)) AS vz
    FROM pg.spectroscopy_holdings h
    JOIN pg.archives a ON a.archive_code = h.archive_code
    WHERE h.match_status = 'skipped'
)
SELECT
    archive_code, display_name, group_key,
    NULLIF(TRIM(any_value(raw_target_name)), '') AS raw_target_name,
    count(*) AS n_records,
    list(archive_obs_id) FILTER (WHERE rn <= {TRIAGE_QUEUE_MAX_RECORDS}) AS archive_obs_ids,
    list(archive_url) FILTER (WHERE rn <= {TRIAGE_QUEUE_MAX_RECORDS}) AS archive_urls,
    -- From the single rn=1 row (most recently updated), not independent
    -- per-column min()s -- those can pair one file's RA with a different
    -- file's Dec, producing a synthetic position that matches no real file
    -- (observed: a noirlab group showed RA from a 2010 exposure and
    -- Dec from an unrelated 2011 exposure, ~200 degrees apart from the
    -- group's actual, overwhelmingly common position).
    any_value(raw_ra) FILTER (WHERE rn = 1) AS raw_ra,
    any_value(raw_dec) FILTER (WHERE rn = 1) AS raw_dec,
    -- Angular deviation (degrees) of the group's reported positions from
    -- their mean direction -- 0 means every record with a position agrees,
    -- larger means this "one name" actually covers records at genuinely
    -- different places on the sky (webapp.app warns on this so a reviewer
    -- doesn't trust the single raw_ra/raw_dec above as if it speaks for the
    -- whole group). NULL when fewer than 2 records report a position.
    CASE WHEN count(vx) >= 2 THEN
        degrees(acos(least(1.0, greatest(0.0,
            sqrt(sum(vx) * sum(vx) + sum(vy) * sum(vy) + sum(vz) * sum(vz)) / count(vx)
        ))))
    END AS position_spread_deg,
    max(updated_at) AS updated_at,
    min(obs_date) AS obs_date,
    any_value(instrument) AS instrument
FROM ranked
GROUP BY archive_code, display_name, group_key
-- Named groups first, then largest group first within each bucket -- a
-- human can actually look a reported name up and make a judgment call,
-- unlike an anonymous calibration-frame/no-name record, so named groups are
-- higher-value to surface; within that, a name attached to more records is
-- higher-value still, since one classification resolves all of them at
-- once. This ordering determines the LIMIT selection itself, not just
-- display order -- otherwise a burst of recent nameless activity (e.g. a
-- big LAMOST MRS sync) could crowd every named group out of the top
-- {TRIAGE_QUEUE_TOP_N} entirely before webapp.app ever sees them.
--
-- Repeats the NULLIF(TRIM(any_value(...))) expression rather than
-- referencing the `raw_target_name` output alias -- observed that
-- referencing the bare alias here binds to `ranked.raw_target_name` (the
-- raw, ungrouped column of the same name carried through the CTE) instead
-- of the SELECT list's aggregate, which DuckDB then rejects as ungrouped:
-- "column must appear in the GROUP BY clause or be part of an aggregate
-- function." Wrapping the same expression in an aggregate again here
-- sidesteps the ambiguity entirely.
ORDER BY (NULLIF(TRIM(any_value(raw_target_name)), '') IS NOT NULL) DESC, count(*) DESC
LIMIT {TRIAGE_QUEUE_TOP_N}
"""


# Star-overlap between archives/instruments -- backs the /instruments page's
# overlap heatmap and Venn-diagram picker. A live per-request version would
# need either a full self-join of spectroscopy_holdings against itself (a
# multi-million-row x multi-million-row join for the biggest archives) or a
# GROUP BY over the whole table, same OOM-shaped risk as everything else
# precomputed here.
#
# Both queries take the same shape: first collapse each star down to the
# small array of distinct archives/instruments its *matched* holdings span
# (bounded by however many archives ever observed that one star -- typically
# 1-3, rarely more than a handful even for the most-studied stars), then
# UNNEST that array against itself (twice, for pairs; three times, for
# triples) and count. This is NOT a cross join over the full table -- the
# per-star arrays are what's crossed, so the work for one star is k^2 (or
# k^3) where k is that star's own small archive/instrument count, not the
# catalog's total row count. a<=b(<=c) keeps only one ordering per
# unordered pair/triple and, as a side effect, includes the a==b(==c)
# "self-pair" -- that's not wasted: it's exactly each set's own total
# distinct-star count, so the heatmap's diagonal and the Venn picker's
# per-circle totals come from this same query instead of a separate one.
# The GROUP BY also means a combo that never co-occurs for any star simply
# has no output row at all (implicit zero) rather than an explicit zero row
# -- archive_overlap(_triple) stays small on its own (low hundreds of rows,
# bounded by the handful of archive codes that exist). Instrument overlap
# needed an explicit cap instead: observed that ~450+ distinct
# instrument names made instrument_overlap_triple balloon to ~6.8M rows (not
# "nowhere near N^3" as originally assumed here) -- webapp.app's /instruments
# route pulls every row into Python dicts and JSON-serializes them straight
# into the page, which OOM'd/timed out the Cloud Run container. See
# INSTRUMENT_OVERLAP_TOP_N below.
ARCHIVE_OVERLAP_QUERY = """
WITH per_star AS (
    SELECT star_id, array_agg(DISTINCT archive_code) AS codes
    FROM pg.spectroscopy_holdings
    WHERE match_status = 'matched' AND star_id IS NOT NULL
    GROUP BY star_id
)
SELECT
    a.archive_code AS archive_a, da.display_name AS display_a,
    b.archive_code AS archive_b, db.display_name AS display_b,
    count(*) AS n_overlap
FROM per_star, UNNEST(codes) AS a(archive_code), UNNEST(codes) AS b(archive_code)
JOIN pg.archives da ON da.archive_code = a.archive_code
JOIN pg.archives db ON db.archive_code = b.archive_code
WHERE a.archive_code <= b.archive_code
GROUP BY 1, 2, 3, 4
ORDER BY 1, 3
"""

# Triple overlap (a<=b<=c, including the a==b==c self-triple -- redundant
# with ARCHIVE_OVERLAP_QUERY's diagonal, kept anyway since dropping just the
# self-triples would need an extra filter for no real size benefit at this
# table's scale) -- needed for the picker's 3-circle case, since a
# proportional 3-circle Venn needs the true center (A∩B∩C) region size, not
# just the three pairwise overlaps.
ARCHIVE_OVERLAP_TRIPLE_QUERY = """
WITH per_star AS (
    SELECT star_id, array_agg(DISTINCT archive_code) AS codes
    FROM pg.spectroscopy_holdings
    WHERE match_status = 'matched' AND star_id IS NOT NULL
    GROUP BY star_id
)
SELECT
    a.archive_code AS archive_a, b.archive_code AS archive_b, c.archive_code AS archive_c,
    count(*) AS n_overlap
FROM per_star,
     UNNEST(codes) AS a(archive_code), UNNEST(codes) AS b(archive_code), UNNEST(codes) AS c(archive_code)
WHERE a.archive_code <= b.archive_code AND b.archive_code <= c.archive_code
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

# Same shape as ARCHIVE_OVERLAP_QUERY/ARCHIVE_OVERLAP_TRIPLE_QUERY, but keyed
# by instrument instead of archive_code -- one archive can host several
# instruments (see INSTRUMENTS_QUERY above), and "which instruments share
# stars" is a finer-grained, separate question from "which archives share
# stars". instrument is nullable (not every archive reports one), filtered
# out here the same way INSTRUMENTS_QUERY does.
#
# Restricted to the top INSTRUMENT_OVERLAP_TOP_N instruments by matched-holding
# count -- unlike archive_code (a few dozen values, tops), the catalog has
# 450+ distinct instrument names, and pair/triple overlap is O(n^2)/O(n^3) in
# that count. Observed: the unrestricted triple query produced ~6.8M
# rows, which then OOM'd/timed out the /instruments page (see the long
# comment above ARCHIVE_OVERLAP_QUERY). Must match webapp.app's
# INSTRUMENT_OVERLAP_HEATMAP_TOP_N, which already caps the heatmap display to
# the top 20 -- capping here too just means the Venn picker's dropdowns only
# offer those same top 20 instruments, instead of silently having no triple
# data for the other 400+ if they were picked.
INSTRUMENT_OVERLAP_TOP_N = 20

INSTRUMENT_OVERLAP_TOP_INSTRUMENTS_CTE = f"""
    top_instruments AS (
        SELECT instrument
        FROM pg.spectroscopy_holdings
        WHERE match_status = 'matched' AND instrument IS NOT NULL
        GROUP BY instrument
        ORDER BY count(*) DESC
        LIMIT {INSTRUMENT_OVERLAP_TOP_N}
    )
"""

INSTRUMENT_OVERLAP_QUERY = f"""
WITH {INSTRUMENT_OVERLAP_TOP_INSTRUMENTS_CTE},
per_star AS (
    SELECT star_id, array_agg(DISTINCT instrument) AS insts
    FROM pg.spectroscopy_holdings
    WHERE match_status = 'matched' AND star_id IS NOT NULL
      AND instrument IN (SELECT instrument FROM top_instruments)
    GROUP BY star_id
)
SELECT a.instrument AS instrument_a, b.instrument AS instrument_b, count(*) AS n_overlap
FROM per_star, UNNEST(insts) AS a(instrument), UNNEST(insts) AS b(instrument)
WHERE a.instrument <= b.instrument
GROUP BY 1, 2
ORDER BY 1, 2
"""

INSTRUMENT_OVERLAP_TRIPLE_QUERY = f"""
WITH {INSTRUMENT_OVERLAP_TOP_INSTRUMENTS_CTE},
per_star AS (
    SELECT star_id, array_agg(DISTINCT instrument) AS insts
    FROM pg.spectroscopy_holdings
    WHERE match_status = 'matched' AND star_id IS NOT NULL
      AND instrument IN (SELECT instrument FROM top_instruments)
    GROUP BY star_id
)
SELECT
    a.instrument AS instrument_a, b.instrument AS instrument_b, c.instrument AS instrument_c,
    count(*) AS n_overlap
FROM per_star,
     UNNEST(insts) AS a(instrument), UNNEST(insts) AS b(instrument), UNNEST(insts) AS c(instrument)
WHERE a.instrument <= b.instrument AND b.instrument <= c.instrument
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""


def _fetch_all(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    con.execute(sql)
    cols = [c[0] for c in con.description]
    return [dict(zip(cols, row)) for row in con.fetchall()]


def export_stats_summary(con: duckdb.DuckDBPyConnection, out_dir: str) -> None:
    summary = {
        "total_stars": con.execute(_localize("SELECT count(*) FROM pg.stars")).fetchone()[0],
        "total_holdings": con.execute(_localize("SELECT count(*) FROM pg.spectroscopy_holdings")).fetchone()[0],
        "needs_review_total": con.execute(
            _localize("SELECT count(*) FROM pg.spectroscopy_holdings WHERE match_status = 'needs_review'")
        ).fetchone()[0],
        "trending_years": TRENDING_YEARS,
        **{name: _fetch_all(con, _localize(sql)) for name, sql in STATS_QUERIES.items()},
    }
    path = os.path.join(out_dir, "stats_summary.json")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(summary, f)
    os.chmod(tmp_path, 0o644)
    os.rename(tmp_path, path)
    logger.info("exported stats_summary -> %s", path)


# Every derived-table query above was written against pg.spectroscopy_holdings
# /pg.archives/pg.stars/pg.archive_sync_state -- the live Postgres tables --
# on the reasoning that DuckDB's postgres scanner would push filters down and
# only pull the rows each query actually needs. Observed that's not
# what happens for these shapes: DuckDB's postgres extension pushes down
# simple projections/filters on the base scan, but every GROUP BY, window
# function, and array_agg here (i.e. nearly all of them) still executes in
# DuckDB after pulling the (often majority-of-the-table) matching rows across
# the wire -- so each of the ~15 derived queries independently re-transfers
# its own large share of a 43M-row, 25GB table from Postgres, on top of
# Postgres's own execution cost for that scan (observed on the actual
# prod host: a bare count(*) over spectroscopy_holdings took 165s, a GROUP BY
# over match_status took 227s -- this host's disk, not query shape, is what
# makes a single full scan that slow).
#
# export_tables() below exports the 4 raw tables from pg.* exactly once, then
# calls this on every subsequent query string to redirect it at the
# just-written local Parquet copies instead (local_spectroscopy_holdings/
# local_archives/local_stars/local_archive_sync_state -- views created right
# after the TABLES loop). Reading the same rows back from local disk is a
# DuckDB-native Parquet scan, not a live Postgres query -- a matter of
# seconds regardless of how many derived tables need their own pass over it.
# As a side effect this also makes every derived table observe one single,
# consistent snapshot instead of potentially drifting live state across what
# used to be a run lasting tens of minutes.
def _localize(sql: str) -> str:
    return (
        sql
        .replace("pg.spectroscopy_holdings", "local_spectroscopy_holdings")
        .replace("pg.archives", "local_archives")
        .replace("pg.stars", "local_stars")
        .replace("pg.archive_sync_state", "local_archive_sync_state")
    )


def _atomic_copy(con: duckdb.DuckDBPyConnection, select_sql: str, path: str) -> None:
    # COPY TO writes straight to `path`, with no atomicity -- a request that
    # reads the file mid-write (webapp.app reads this snapshot live over
    # HTTP while exports happen independently, on no fixed schedule) sees a
    # torn/partial Parquet file and errors out. Observed as the cause
    # of a one-off duckdb "don't know what type:" crash on /stats. Writing
    # to a temp path and rename()-ing into place avoids that: rename is
    # atomic on the same filesystem, so readers only ever see a complete
    # file at `path`, never a partial one.
    tmp_path = path + ".tmp"
    con.execute(f"COPY ({select_sql}) TO '{tmp_path}' (FORMAT PARQUET)")
    os.chmod(tmp_path, 0o644)
    os.rename(tmp_path, path)


def export_tables(database_url: str, out_dir: str) -> None:
    con = duckdb.connect()
    # An in-memory connection has no temp_directory by default, so DuckDB
    # can't spill oversized intermediate results (e.g. the leaderboard
    # query's star x period grid) to disk -- it just errors out once
    # memory_limit is hit instead. Observed: LAMOST's addition pushed
    # the leaderboard grid past the box's 24.9 GiB default memory_limit for
    # the first time. Pointing temp_directory somewhere writable lets
    # DuckDB spill instead of OOMing.
    # dir=out_dir (not system /tmp, which can be small/quota-limited on a
    # shared login node) since out_dir is already known to have room for
    # the multi-GB parquet exports themselves.
    spill_dir = tempfile.mkdtemp(prefix=".duckdb_export_spill_", dir=out_dir)
    try:
        con.execute(f"SET temp_directory = '{spill_dir}'")
        con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
        con.execute(f"ATTACH '{database_url}' AS pg (TYPE postgres, READ_ONLY)")
        for table in TABLES:
            path = os.path.join(out_dir, f"{table}.parquet")
            select_sql = f"SELECT * FROM pg.{table}"
            if table == "spectroscopy_holdings":
                # webapp.app's single-star search filters this table on
                # star_id over httpfs -- observed that an unclustered
                # export means Parquet's row-group min/max stats can't skip
                # anything on that filter, so a lookup pulls a large chunk
                # of this (now >1GB) file into memory and tipped the Cloud
                # Run container's 1GiB limit into repeated OOM kills.
                # Exporting pre-sorted by star_id lets row-group pruning
                # actually work for that query, independent of table size.
                select_sql += " ORDER BY star_id"
            elif table == "stars":
                # Same class of bug as spectroscopy_holdings above, just
                # discovered later: _lookup_local_star's numeric-query path
                # filters this table on gaia_source_id over httpfs.
                # Observed -- an unsorted export of this (now >650MB)
                # table meant every row group's gaia_source_id min/max
                # spanned nearly the whole 0..~6.9e18 range, so row-group
                # pruning couldn't skip anything and a single-ID search
                # pulled the whole file into memory, OOM-killing the Cloud
                # Run container. Sorting by gaia_source_id fixes pruning for
                # that lookup; NULLs (bsc5 stars with no Gaia source) sort
                # together at the end instead of scattering across every
                # row group.
                select_sql += " ORDER BY gaia_source_id"
            _atomic_copy(con, select_sql, path)
            logger.info("exported %s -> %s", table, path)

        # Every query from here on is rewritten by _localize() (see its own
        # comment) to read these local views over the files just written
        # above, instead of re-querying live Postgres -- these are exactly
        # those 4 raw tables, read back off local disk rather than pulled
        # over the wire again.
        for table in TABLES:
            con.execute(
                f"CREATE VIEW local_{table} AS SELECT * FROM read_parquet('{os.path.join(out_dir, f'{table}.parquet')}')"
            )

        star_name_index_path = os.path.join(out_dir, "star_name_index.parquet")
        _atomic_copy(con, _localize(STAR_NAME_INDEX_QUERY), star_name_index_path)
        logger.info("exported star_name_index -> %s", star_name_index_path)

        leaderboard_path = os.path.join(out_dir, "leaderboard.parquet")
        _export_leaderboard(con, leaderboard_path)
        logger.info("exported leaderboard -> %s", leaderboard_path)

        diversity_path = os.path.join(out_dir, "diversity.parquet")
        _export_diversity(con, diversity_path)
        logger.info("exported diversity -> %s", diversity_path)

        cmd_stars_path = os.path.join(out_dir, "cmd_stars.parquet")
        _atomic_copy(con, _localize(CMD_STARS_QUERY), cmd_stars_path)
        logger.info("exported cmd_stars -> %s", cmd_stars_path)

        sky_sample_path = os.path.join(out_dir, "sky_sample.parquet")
        _atomic_copy(con, _localize(SKY_SAMPLE_QUERY), sky_sample_path)
        logger.info("exported sky_sample -> %s", sky_sample_path)

        archive_status_path = os.path.join(out_dir, "archive_status.parquet")
        _atomic_copy(con, _localize(ARCHIVE_STATUS_QUERY), archive_status_path)
        logger.info("exported archive_status -> %s", archive_status_path)

        instruments_path = os.path.join(out_dir, "instruments.parquet")
        _atomic_copy(con, _localize(INSTRUMENTS_QUERY), instruments_path)
        logger.info("exported instruments -> %s", instruments_path)

        instrument_healpix_path = os.path.join(out_dir, "instrument_healpix.parquet")
        _export_instrument_healpix(con, instrument_healpix_path)
        logger.info("exported instrument_healpix -> %s", instrument_healpix_path)

        triage_queue_path = os.path.join(out_dir, "triage_queue.parquet")
        _atomic_copy(con, _localize(TRIAGE_QUEUE_QUERY), triage_queue_path)
        logger.info("exported triage_queue -> %s", triage_queue_path)

        needs_review_path = os.path.join(out_dir, "needs_review.parquet")
        _atomic_copy(con, _localize(NEEDS_REVIEW_QUERY), needs_review_path)
        logger.info("exported needs_review -> %s", needs_review_path)

        skipped_by_archive_path = os.path.join(out_dir, "skipped_by_archive.parquet")
        _atomic_copy(con, _localize(SKIPPED_BY_ARCHIVE_QUERY), skipped_by_archive_path)
        logger.info("exported skipped_by_archive -> %s", skipped_by_archive_path)

        skipped_path = os.path.join(out_dir, "skipped.parquet")
        _atomic_copy(con, _localize(SKIPPED_QUERY), skipped_path)
        logger.info("exported skipped -> %s", skipped_path)

        holdings_by_position_path = os.path.join(out_dir, "spectroscopy_holdings_by_position.parquet")
        _atomic_copy(con, _localize(HOLDINGS_BY_POSITION_QUERY), holdings_by_position_path)
        logger.info("exported spectroscopy_holdings_by_position -> %s", holdings_by_position_path)

        archive_overlap_path = os.path.join(out_dir, "archive_overlap.parquet")
        _atomic_copy(con, _localize(ARCHIVE_OVERLAP_QUERY), archive_overlap_path)
        logger.info("exported archive_overlap -> %s", archive_overlap_path)

        archive_overlap_triple_path = os.path.join(out_dir, "archive_overlap_triple.parquet")
        _atomic_copy(con, _localize(ARCHIVE_OVERLAP_TRIPLE_QUERY), archive_overlap_triple_path)
        logger.info("exported archive_overlap_triple -> %s", archive_overlap_triple_path)

        instrument_overlap_path = os.path.join(out_dir, "instrument_overlap.parquet")
        _atomic_copy(con, _localize(INSTRUMENT_OVERLAP_QUERY), instrument_overlap_path)
        logger.info("exported instrument_overlap -> %s", instrument_overlap_path)

        instrument_overlap_triple_path = os.path.join(out_dir, "instrument_overlap_triple.parquet")
        _atomic_copy(con, _localize(INSTRUMENT_OVERLAP_TRIPLE_QUERY), instrument_overlap_triple_path)
        logger.info("exported instrument_overlap_triple -> %s", instrument_overlap_triple_path)

        export_stats_summary(con, out_dir)
    finally:
        con.close()
        shutil.rmtree(spill_dir, ignore_errors=True)


# =============================================================================
# Import pending /triage classification submissions (design sketch).
#
# webapp.app's /triage/submit route has no live Postgres access (see its
# module docstring) -- it appends one JSON line per submission to a public
# file on joy instead, over a narrowly-scoped, forced-command-restricted SSH
# connection (see webapp/app.py's _append_triage_submission and
# scripts/joy_triage_append.py). This is the other half: reads that same
# file (this script already runs on/near joy, with local filesystem access
# to the same directory it just wrote the Parquet snapshot to) and imports
# each line into the real skip_classifications table.
#
# Idempotent by construction, not by tracking an offset: every run re-reads
# the *entire* file and INSERTs with ON CONFLICT (archive_code,
# archive_obs_id, submitter) DO NOTHING, relying on skip_classifications'
# existing one-vote-per-submitter unique index -- a line that was already
# imported on a previous run just inserts 0 rows the second time, so there's
# no offset/cursor state to track or get out of sync. The file is
# deliberately never truncated -- this feature's traffic is human-submission
# scale, re-scanning it every run costs nothing worth optimizing for.
# =============================================================================

TRIAGE_SUBMISSIONS_FILENAME = "triage_submissions.jsonl"

REQUIRED_TRIAGE_FIELDS = {"archive_code", "outcome", "submitter", "submitted_at"}
ALLOWED_TRIAGE_OUTCOMES = {
    "attach_gaia_source", "attach_bright_star",
    "not_a_real_target", "not_a_star",
    "confirmed_absent_from_gaia",
}

# A name-based vote (see the "Expands one vote-by-name..." comment below)
# assumes the name identifies one real target, so a handful of matching
# skipped rows at most -- observed 2026-08-12 that assumption breaks
# for a calibration-frame name like Lick's "WideFlat": it matched 217,237
# of that one archive's 1.1M skipped rows, and the query planner badly
# misjudged the cost (estimated 1,934 rows, actual 217,237), turning one
# submission into a multi-minute scan+insert that then repeats on *every*
# future run (this file is deliberately never truncated, see above, so a
# bad vote's cost doesn't go away once paid -- it recurs forever). Rather
# than trust every submitted name to be a real, narrowly-matching target,
# skip (and warn about) any name-vote whose live candidate count exceeds
# this cap -- a human can still resolve a calibration-frame name's skipped
# rows through the normal per-archive_obs_id path instead.
#
# Deliberately NOT applied to TRIAGE_NAME_VOTE_UNCAPPED_OUTCOMES below --
# attach_gaia_source/attach_bright_star assert the whole group genuinely
# *is* one specific real, named source (unlike not_a_real_target/not_a_star/
# confirmed_absent_from_gaia, which are exactly the "this looks like junk"
# calls the cap exists to double-check), and a real, well-studied star can
# legitimately have far more than 1000 same-named archive rows (e.g. a
# multi-decade RV-monitoring target). Capping those too would silently
# drop a correct large-scale attach the same way it would have dropped
# WideFlat's incorrect one -- so this only protects the "this is junk"
# outcomes, where a name matching an implausible number of rows is itself
# a signal to slow down and double-check, not attach.
TRIAGE_NAME_VOTE_MAX_MATCHES = 1000
TRIAGE_NAME_VOTE_UNCAPPED_OUTCOMES = {"attach_gaia_source", "attach_bright_star"}


def import_triage_submissions(database_url: str, out_dir: str) -> None:
    path = os.path.join(out_dir, TRIAGE_SUBMISSIONS_FILENAME)
    if not os.path.exists(path):
        logger.info("no triage submissions file at %s yet, skipping import", path)
        return

    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    n_inserted = 0
    n_skipped = 0
    # autocommit -- each line is its own statement, so one line Postgres
    # rejects (stale archive_obs_id, a CHECK-constraint mismatch scripts/
    # joy_triage_append.py's own validation didn't already catch) doesn't
    # poison a transaction and block every line after it.
    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed JSON line in %s: %r", path, line[:200])
                n_skipped += 1
                continue

            if not (isinstance(obj, dict) and REQUIRED_TRIAGE_FIELDS.issubset(obj)
                    and obj.get("outcome") in ALLOWED_TRIAGE_OUTCOMES
                    and ("raw_target_name" in obj) != ("archive_obs_id" in obj)):
                # The != above requires *exactly* one of the two -- neither
                # present would otherwise reach the VALUES branch below and
                # KeyError on the missing %(archive_obs_id)s binding (not a
                # psycopg.Error, so the per-line except wouldn't catch it and
                # this whole import would crash instead of just skipping the
                # one bad line).
                logger.warning("skipping malformed submission (missing fields/bad outcome): %r", obj)
                n_skipped += 1
                continue

            raw_target_name = (obj.get("raw_target_name") or "").strip()
            # .get() rather than a bare key, so a line written before
            # proposed_bsc_hr_number existed (attach_bright_star wasn't a
            # valid outcome yet) still binds cleanly instead of KeyError-ing
            # this whole line's cur.execute() -- same reasoning as the
            # REQUIRED_TRIAGE_FIELDS comment above.
            params = {**obj, "raw_target_name": raw_target_name, "proposed_bsc_hr_number": obj.get("proposed_bsc_hr_number")}
            try:
                if raw_target_name:
                    # Sanity-check the fan-out *before* attempting the insert
                    # below -- see TRIAGE_NAME_VOTE_MAX_MATCHES's own comment
                    # for why this is needed, what happens without it, and
                    # why attach_* outcomes are exempted.
                    if obj.get("outcome") not in TRIAGE_NAME_VOTE_UNCAPPED_OUTCOMES:
                        cur.execute(
                            """
                            SELECT count(*) FROM spectroscopy_holdings h
                            WHERE h.archive_code = %(archive_code)s AND h.match_status = 'skipped'
                              AND NULLIF(TRIM(h.raw_target_name), '') = %(raw_target_name)s
                            """,
                            params,
                        )
                        n_candidates = cur.fetchone()[0]
                        if n_candidates > TRIAGE_NAME_VOTE_MAX_MATCHES:
                            logger.warning(
                                "skipping name-vote submission -- %r under archive_code=%r matches %d skipped "
                                "rows (cap %d), looks like a calibration-frame/placeholder name rather than a "
                                "real target: %r",
                                raw_target_name, obj.get("archive_code"), n_candidates, TRIAGE_NAME_VOTE_MAX_MATCHES, obj,
                            )
                            n_skipped += 1
                            continue

                    # Expands one vote-by-name into every record the *live*
                    # spectroscopy_holdings table currently has under that
                    # (archive_code, name) and still marked 'skipped' -- not
                    # just whatever was true at submission time, so a vote
                    # on a big group still covers all of it even if more
                    # records under that name showed up since.
                    cur.execute(
                        """
                        INSERT INTO skip_classifications
                            (archive_code, archive_obs_id, outcome, proposed_gaia_source_id,
                             proposed_bsc_hr_number, gaia_cone_search_radius_arcsec,
                             gaia_cone_search_result, submitter, note, submitted_at)
                        SELECT h.archive_code, h.archive_obs_id, %(outcome)s, %(proposed_gaia_source_id)s,
                               %(proposed_bsc_hr_number)s, %(gaia_cone_search_radius_arcsec)s,
                               %(gaia_cone_search_result)s, %(submitter)s, %(note)s, %(submitted_at)s
                        FROM spectroscopy_holdings h
                        WHERE h.archive_code = %(archive_code)s AND h.match_status = 'skipped'
                          AND NULLIF(TRIM(h.raw_target_name), '') = %(raw_target_name)s
                        ON CONFLICT (archive_code, archive_obs_id, submitter) DO NOTHING
                        """,
                        params,
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO skip_classifications
                            (archive_code, archive_obs_id, outcome, proposed_gaia_source_id,
                             proposed_bsc_hr_number, gaia_cone_search_radius_arcsec,
                             gaia_cone_search_result, submitter, note, submitted_at)
                        VALUES (%(archive_code)s, %(archive_obs_id)s, %(outcome)s, %(proposed_gaia_source_id)s,
                                %(proposed_bsc_hr_number)s, %(gaia_cone_search_radius_arcsec)s,
                                %(gaia_cone_search_result)s, %(submitter)s, %(note)s, %(submitted_at)s)
                        ON CONFLICT (archive_code, archive_obs_id, submitter) DO NOTHING
                        """,
                        params,
                    )
            except psycopg.Error as exc:
                logger.warning("skipping submission Postgres rejected (%s): %r", exc, obj)
                n_skipped += 1
                continue
            n_inserted += cur.rowcount

    logger.info(
        "imported triage submissions from %s: %d line(s) read, %d new skip_classifications row(s), %d skipped",
        path, len(lines), n_inserted, n_skipped,
    )


def _apply_skip_classification_if_quorum(pg_conn: psycopg.Connection, archive_code: str, archive_obs_id: str, quorum: int = 2) -> None:
    """Design stub -- NOT called anywhere yet. Sketching the intended
    consensus step so it's clear what's deferred and why, per the crowdsourced-
    triage design notes. Lives here rather than in webapp.app because this
    is the only piece of the pipeline with live Postgres access at all now
    (see import_triage_submissions above and webapp.app's module docstring).

    Intended logic once wired up (e.g. right after import_triage_submissions
    processes a batch): tally this holding's not-yet-applied
    skip_classifications rows grouped by outcome; if any one outcome has
    >= `quorum` independent submitters agreeing:
      - attach_gaia_source: call ingest.add_star.add_star(pg_conn,
        proposed_gaia_source_id, input_name=...) (see add_star.py:188) to
        fetch-and-track the star if it isn't already tracked, then UPDATE the
        matching spectroscopy_holdings row to that star with
        match_status='matched', match_method='manual'.
      - attach_bright_star: call ingest.add_star.add_bsc_star(pg_conn,
        proposed_bsc_hr_number, input_name=...) (see add_star.py:275) --
        same idea as attach_gaia_source, but for a naked-eye star Gaia never
        saw, tracked via bsc_hr_number instead of gaia_source_id.
      - not_a_real_target / not_a_star: UPDATE spectroscopy_holdings.match_status =
        'rejected' (already a valid value in the match_status CHECK
        constraint -- see db/schema.sql). Both are terminal in the same way;
        kept as separate outcomes only so the vote itself stays informative
        (junk data vs. a real non-stellar object) rather than for any
        difference in how they're applied here.
      - confirmed_absent_from_gaia: no spectroscopy_holdings change (there's
        genuinely no gaia_source_id/star to assign) -- just mark applied so
        the row stops resurfacing in the /triage queue.
      - Mark every skip_classifications row for this holding applied_at =
        now(), not just the winning outcome's rows, so disagreeing/minority
        submissions don't linger as "pending" forever once a decision is made.

    Open design questions this stub deliberately leaves unresolved:
      - Exact quorum size (2 above is a placeholder) -- should it scale with
        total submission count, or require a margin over the runner-up
        outcome rather than a flat count?
      - What happens if two different outcomes both individually reach quorum
        (shouldn't happen if one-vote-per-submitter holds and quorum requires
        genuine agreement, but worth a real tie-break rule before this goes
        live, not just an assumption)?
      - Should a submitter ever be able to revise their own vote? Currently
        blocked outright by db/schema.sql's one-vote-per-submitter unique
        index -- deliberate for this sketch, but worth revisiting.
    """
    raise NotImplementedError("apply/quorum step is a design stub -- see docstring, not wired up yet")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", required=True, help="directory Apache serves, e.g. ~/public_html/spectra_data")
    args = parser.parse_args()

    out_dir = os.path.expanduser(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o755)

    database_url = os.environ["DATABASE_URL"]
    # Import first, so this run's triage_queue.parquet (built inside
    # export_tables) reflects any records that just got voted 'rejected'
    # or similar by a *future* apply step -- doesn't matter yet since that
    # step isn't wired up (see _apply_skip_classification_if_quorum), but
    # importing before exporting is the right order once it is.
    import_triage_submissions(database_url, out_dir)
    export_tables(database_url, out_dir)


if __name__ == "__main__":
    main()
