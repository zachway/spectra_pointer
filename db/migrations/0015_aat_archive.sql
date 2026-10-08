-- Migration: register aat and aat_2df as new archives -- raw frames from the
-- Anglo-Australian Telescope via archives.datacentral.org.au (issue #227):
-- aat for the single-object spectrographs, aat_2df for 2dF+AAOmega with one
-- row per fibre (see sync/archives/aat.py, aat_2df.py and _aat_common.py).
-- Must be run BEFORE the first sync of either: the holdings table's foreign
-- key to archives.archive_code otherwise rejects every row.
--
-- Run inside a transaction against the live database.

BEGIN;

INSERT INTO archives (archive_code, display_name, access_mechanism, has_native_gaia_column, native_gaia_dr, notes)
VALUES
    ('aat',                     'Anglo-Australian Telescope Archive', 'rest_json', FALSE, NULL, 'Raw frames from the AAT''s single-object spectrographs (UCLES, UHRF, the RGO Spectrograph, FORS, IRIS/IRIS2 spectroscopy, Veloce), 1990-present, from archives.datacentral.org.au -- a different service from the Data Central TAP galah uses, which only carries reduced survey products. No documented API: found 2026-10-08 (issue #227) by reading the site''s own React bundle, an unauthenticated POST to /api/query/ walked one night at a time since whole-archive queries time out. One row per exposure, matched on the frame''s OBJECT name first. archive_url is the site''s results page for that night (no per-frame URL exists); frames under 18 months old are proprietary. See sync/archives/aat.py.'),
    ('aat_2df',                 'Anglo-Australian Telescope Archive — AAOmega (2dF)', 'rest_json', FALSE, NULL, 'Same service as aat, for the 2dF+AAOmega multi-fibre spectrograph (2005-present): one row per program fibre per science exposure, at the fibre''s own position, from the fibre table the archive returns with each frame (sky/guide/parked fibres already excluded). Fibre names are set by each observing team -- a Gaia source_id or catalogue designation is used when that is what the name is, otherwise the row is matched by position only. A separate archive_code from aat because these targets are several magnitudes fainter (the positional fallback''s faintness ceiling is per archive). See sync/archives/aat_2df.py.')
ON CONFLICT (archive_code) DO NOTHING;

COMMIT;
