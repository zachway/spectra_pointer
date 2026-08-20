-- Migration: register carmenes_reiners2018 as a new archive -- a third
-- dataset on CARMENES's GTO portal (welcome.action), found while auditing
-- whether every link on that page was accounted for after carmenes_tac
-- shipped. Unlike DR1's zip-only access, this one has a real direct
-- per-channel FITS file per star, no zip needed -- see
-- sync/archives/carmenes_reiners2018.py for the full live-verification
-- notes.
--
-- NOTE: carmenes_tac (PR #119, branch carmenes-tac, still draft as of this
-- writing) also claims migration number 0008 -- same situation as the
-- existing 0003 duplicate on main (0003_reduction_status.sql /
-- 0003_sdss_v_optical_gaia_dr3.sql). Whichever of the two merges second
-- should renumber to 0009 rather than resolve the collision here.
--
-- Run inside a transaction against the live database.

BEGIN;

INSERT INTO archives (archive_code, display_name, access_mechanism, has_native_gaia_column, native_gaia_dr, notes)
VALUES (
    'carmenes_reiners2018',
    'CARMENES Reiners et al. 2018 Input Catalog',
    'bulk_file',
    FALSE,
    NULL,
    'A third GTO-portal dataset (welcome.action, jsp/reinersetal2018.jsp) found auditing whether every link there was accounted for after carmenes_tac shipped. 324 rows confirmed live (matches the paper''s "324 survey stars"), one representative epoch per star, but unlike DR1''s zip-only access each row carries a real direct unauthenticated getDataPublic.action FITS link per channel (VIS + NIR). Real VIS sample 5,051,520 bytes, SPEC/CONT/SIG/WAVE image extensions (4096x61); real NIR sample 2,341,440 bytes, same shape (4080x28) -- same product tier as DR1/carmenes_tac, reduction_status hardcoded ''reduced''. No SIMBAD-resolvable name on this page itself -- reuses carmenes._parse_dr1_table()''s Karmn -> discovery-name mapping (323 of 324 Karmn ids found there) plus ingest.add_star.resolve_stellar_gaia_ids_batch, same as carmenes_tac. See sync/archives/carmenes_reiners2018.py.'
)
ON CONFLICT (archive_code) DO NOTHING;

COMMIT;
