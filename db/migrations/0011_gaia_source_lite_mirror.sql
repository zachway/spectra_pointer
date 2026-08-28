-- Migration: local mirror of gaiadr3.gaia_source_lite's astrometry/photometry
-- columns, to replace shitty_positional_match's live Gaia TAP+ queries
-- (sync/positional_fallback.py's _gaia_healpix_pool).
--
-- Live 2026-08-27: with the local per-cell bottlenecks fixed (batched DB
-- writes, vectorized proper-motion propagation), the real remaining
-- throughput ceiling was Gaia's own TAP service -- per-cell fetch latency
-- (11s-1160s depending on server load) times ~147,000 required cell
-- fetches, with GAIA_FETCH_CONCURRENCY past ~5 empirically causing
-- server-side throttling (latency roughly tripling, occasional transient
-- job failures) rather than real parallel speedup. A 7-hour live run
-- processed only ~1.1% of the backlog at that rate. Mirroring the six
-- columns this project actually needs locally removes that ceiling
-- entirely -- see scripts/load_gaia_source_lite.py for how it's populated
-- (streamed from ESA's public gaia_source bulk CSV.gz files, since
-- gaia_source_lite itself has no separate bulk-download files).
--
-- Deliberately no primary key / btree index on source_id: a btree over
-- ~1.8 billion rows would cost ~40GB for no real benefit here.
-- source_id's high bits already encode HEALPix pixel (see
-- _healpix_source_id_range in sync/positional_fallback.py), and the
-- loader ingests files in their natural (HEALPix-ordered) sequence, so
-- the table ends up physically near-sorted by source_id -- exactly what
-- BRIN is built for (near-free to store, and it answers this project's
-- only query shape, `source_id BETWEEN lo AND hi`, by skipping whole
-- page ranges rather than doing a full scan).
--
-- gaia_source_lite_mirror_load_log tracks which source files the loader
-- has already ingested, making a ~757GB, many-hour load resumable after
-- an interruption -- since there's no uniqueness constraint on source_id
-- to prevent duplicate rows if a file were re-loaded, this log is what
-- prevents double-loading a file, not the table schema.
--
-- Run inside a transaction against the live database.

BEGIN;

CREATE TABLE gaia_source_lite_mirror (
    source_id           BIGINT NOT NULL,
    ra                   DOUBLE PRECISION NOT NULL,   -- deg, ICRS, ref_epoch 2016.0 (Gaia DR3)
    dec                  DOUBLE PRECISION NOT NULL,   -- deg, ICRS, ref_epoch 2016.0 (Gaia DR3)
    pmra                 DOUBLE PRECISION,            -- mas/yr
    pmdec                DOUBLE PRECISION,            -- mas/yr
    phot_g_mean_mag      REAL
);

CREATE INDEX gaia_source_lite_mirror_source_id_brin
    ON gaia_source_lite_mirror USING BRIN (source_id);

CREATE TABLE gaia_source_lite_mirror_load_log (
    filename    TEXT PRIMARY KEY,
    row_count   BIGINT NOT NULL,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;

-- Application code (sync/positional_fallback.py's _gaia_healpix_pool) and
-- the new loader (scripts/load_gaia_source_lite.py) are added in the same
-- PR that adds this migration -- run this migration, then run the loader
-- to populate gaia_source_lite_mirror, and verify it against a live Gaia
-- query for a few known cells, BEFORE deploying the application-code
-- swap. See the project plan doc for the full rollout sequence.
