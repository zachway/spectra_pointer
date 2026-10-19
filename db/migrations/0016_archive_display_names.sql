-- Migration: make archive display names consistent with what each archive
-- actually holds and with each other. schema.sql already seeds the new
-- names, but its INSERT never touches an existing row, so prod needs these
-- UPDATEs.
--
--   * lbt: 'LBT — PEPSI' -> 'LBT — PEPSI, MODS, LUCI'. The sync has pulled
--     MODS and LUCI alongside PEPSI for a long time (sync/archives/lbt.py),
--     so the site showed them as instruments of a "PEPSI" archive.
--   * One separator style (" — ") for an archive split out per instrument:
--     LCO used "--", and NAOJ/Ondrejov/Asiago put the instrument in
--     parentheses instead.
--   * polarbase: the name listed every instrument except neo-Narval.
--
-- The webapp's per-instrument dicts (instrument_wavelengths.py,
-- instrument_resolving_power.py) and app.py's archive-homepage map key on
-- display_name, so after running this (and scripts.backfill_instrument_
-- labels), re-run scripts.export_to_parquet before redeploying the webapp.
--
-- Run inside a transaction against the live database.

BEGIN;

UPDATE archives SET display_name = 'LBT — PEPSI, MODS, LUCI' WHERE archive_code = 'lbt';
UPDATE archives SET display_name = 'Las Cumbres Observatory — FLOYDS' WHERE archive_code = 'lco_floyds';
UPDATE archives SET display_name = 'Las Cumbres Observatory — NRES' WHERE archive_code = 'lco_nres';
UPDATE archives SET display_name = 'NAOJ — Subaru HDS (via JVO)' WHERE archive_code = 'naoj';
UPDATE archives SET display_name = 'NAOJ — Subaru MOIRCS (via JVO)' WHERE archive_code = 'subaru_moircs';
UPDATE archives SET display_name = 'Ondrejov Observatory — CCD700' WHERE archive_code = 'ondrejov';
UPDATE archives SET display_name = 'Asiago Observatory — Echelle' WHERE archive_code = 'asiago';
UPDATE archives SET display_name = 'PolarBase (ESPaDOnS/Narval/neo-Narval/SPIRou/HARPSpol spectropolarimetry)' WHERE archive_code = 'polarbase';

COMMIT;
