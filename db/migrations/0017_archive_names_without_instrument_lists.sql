-- Migration: drop instrument lists from archive display names.
--
-- The webapp labels a spectrum "<display_name> — <instrument>" (see
-- webapp.app.archive_instrument_label), so a name that lists its own
-- instruments repeated one in every label -- "LBT — PEPSI, MODS, LUCI — LUCI".
-- Archives holding several instruments now carry just the archive's name;
-- the instrument column says which one. archive_instrument_label shows the
-- archive name alone when it already names the (single) instrument, e.g.
-- "Las Cumbres Observatory — FLOYDS".
--
-- Webapp dicts keyed on display_name are re-keyed in the same change, so
-- re-run scripts.export_to_parquet before redeploying the webapp.
--
-- Run inside a transaction against the live database.

BEGIN;

UPDATE archives SET display_name = 'LBT Archive' WHERE archive_code = 'lbt';
UPDATE archives SET display_name = 'PolarBase (spectropolarimetry)' WHERE archive_code = 'polarbase';
UPDATE archives SET display_name = 'Lick Observatory (Mt. Hamilton)' WHERE archive_code = 'lick';
UPDATE archives SET display_name = 'ING Archive' WHERE archive_code = 'ing';
UPDATE archives SET display_name = 'Spitzer Heritage Archive' WHERE archive_code = 'spitzer_sha';
UPDATE archives SET display_name = 'CARMENES (CAHA archive)' WHERE archive_code = 'carmenes_caha';

COMMIT;
