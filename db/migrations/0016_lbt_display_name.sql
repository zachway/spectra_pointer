-- Migration: rename the lbt archive's display_name from 'LBT — PEPSI' to
-- 'LBT — PEPSI, MODS, LUCI'. The sync has pulled MODS and LUCI alongside
-- PEPSI for a long time (sync/archives/lbt.py), and schema.sql already seeds
-- the new name, but the prod row predates that change and the seed INSERT
-- never touches an existing row -- so the site showed MODS and LUCI as
-- instruments of a "PEPSI" archive.
--
-- The webapp's per-instrument dicts (instrument_wavelengths.py,
-- instrument_resolving_power.py) and app.py's archive-homepage map key on
-- display_name, so after running this, re-run scripts.export_to_parquet
-- before redeploying the webapp or the LBT rows lose their wavelength/R
-- lookups until the next export.
--
-- Run inside a transaction against the live database.

BEGIN;

UPDATE archives
SET display_name = 'LBT — PEPSI, MODS, LUCI'
WHERE archive_code = 'lbt';

COMMIT;
