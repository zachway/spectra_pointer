-- Migration: Gaia DR3 GSP-Phot stellar parameters on stars (issue #229) --
-- teff_gspphot/logg_gspphot/mh_gspphot straight off gaiadr3.gaia_source,
-- for the CMD page's Teff-logg view and its parameter-range filters.
--
-- gspphot_checked_at is what makes scripts.backfill_gaia_gspphot resumable:
-- GSP-Phot only publishes parameters for a subset of sources (roughly
-- G < 19 with usable BP/RP spectra), so "all three still NULL" is a normal
-- final state for many stars and can't double as "not looked up yet".
--
-- All four columns are nullable with no default, so this is a
-- catalog-only change (no table rewrite) even at tens of millions of rows.
-- Run before the first scripts.backfill_gaia_gspphot /
-- scripts.export_to_parquet from this version.

BEGIN;

ALTER TABLE stars ADD COLUMN IF NOT EXISTS teff_gspphot REAL;
ALTER TABLE stars ADD COLUMN IF NOT EXISTS logg_gspphot REAL;
ALTER TABLE stars ADD COLUMN IF NOT EXISTS mh_gspphot REAL;
ALTER TABLE stars ADD COLUMN IF NOT EXISTS gspphot_checked_at TIMESTAMPTZ;

COMMIT;
