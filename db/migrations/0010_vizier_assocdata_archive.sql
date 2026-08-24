-- Migration: register vizier_assocdata as a new archive -- VizieR's
-- Associated Data ObsTAP service (cdsarc.cds.unistra.fr/saadavizier.tap),
-- aggregating spectra published as journal-supplement VizieR tables. Added
-- as a fallback source, ranked below every archive with a direct link:
-- big buckets (LAMOST, HST, RAVE, ESO/VLT, Keck, Gemini, TNG, Mercator,
-- CFHT, OHP, SALT, GTC, SDSS, ING/NOT, Subaru, IRTF, Lick, NOIRLab, Spitzer,
-- Ritter, LBT, Asiago, FUSE, JWST, XMM) are excluded by exact facility_name
-- match since this project already syncs them directly; COROT and Model
-- (VII/102) are excluded too -- neither is real stellar observations
-- (CoRoT photometric time-series and synthetic spectral-type templates,
-- respectively, confirmed live). What's left (13,199 rows, confirmed live
-- 2026-08-24) is small single-paper collections from telescopes/PIs with
-- no ongoing public archive of their own. See sync/archives/
-- vizier_assocdata.py for the full investigation notes.
--
-- Run inside a transaction against the live database.

BEGIN;

INSERT INTO archives (archive_code, display_name, access_mechanism, has_native_gaia_column, native_gaia_dr, notes)
VALUES (
    'vizier_assocdata',
    'VizieR Associated Data (CDS)',
    'tap',
    FALSE,
    NULL,
    'Found via cdsarc.cds.unistra.fr/assocdata -- a real ObsTAP/ADQL service (Saada engine, endpoint cdsarc.cds.unistra.fr/saadavizier.tap/tap, table obscore) aggregating spectra individual journal articles published as VizieR electronic tables, one obs_collection per publication (284 total carry dataproduct_type=''spectrum'', 8.3M rows observed 2026-08-24). Ranking rule: always prefer a direct archive link; this is the fallback only. Most big buckets duplicate archives already synced directly (LAMOST 7.67M rows via lamost.py/lamost_mrs.py, HST via mast.py, AAT''s J/MNRAS/413/971 is literally RAVE via rave.py, plus ESO/VLT/NTT, Keck, Gemini, TNG, Mercator, CFHT, OHP, SALT, GTC, SDSS, ING/NOT telescopes, Subaru, IRTF, Lick, NOIRLab-operated CTIO/KPNO/SOAR/Blanco/Bok, Spitzer, Ritter, LBT, Asiago, FUSE, JWST, XMM) -- excluded via an exact, hand-verified facility_name literal list (EXCLUDED_FACILITY_NAMES in sync/archives/vizier_assocdata.py; ADQL here has no LOWER()/LIKE support so this is exact-match, not substring). Also restricted to em_min/em_max in [1e-7, 5e-6] m (100nm-5000nm) to drop the radio/mm long tail (IRAM/JCMT/ALMA/VLA/Effelsberg/ATCA/MeerKAT/VLBA/NOEMA/Arecibo/...) this project has no archive for. Two facility_name buckets that looked like real spectra by count/wavelength alone turned out not to be real stellar observations and are excluded too: COROT (B/corot, 177,553 rows) is mislabeled CoRoT photometric time-series flux -- most rows have no target_name, but the COROT_faint_star channel does carry one (a non-SIMBAD-resolvable internal run id, e.g. "COROT105288043"), so target_name-non-null alone was not a sufficient filter; and Model (VII/102, 96 rows) is synthetic spectral-type template spectra (target_name = "F56V"/"G04V"/...), not observations of real stars. 13,199 rows remain after all filters (confirmed live 2026-08-24) -- small single-paper collections from telescopes/PIs with no ongoing public archive of their own, reachable here only because the journal-supplement copy is public on CDS (same category of PI-only data written off as unreachable for Palomar/APO). calib_level is -1 (unset sentinel) on every remaining row -- reduction_status left ''unknown''. Paginated via an oidsaada id watermark (a real per-row unique identifier, ~19-digit, string-comparison-safe since same length across the observed range) since VizieR keeps ingesting new journal tables over time, not a frozen dump -- same shape as hermes_mercator.py/asiago.py. bib_reference (the paper''s bibcode) stored in program_id for provenance, not a literal observing-program ID.'
)
ON CONFLICT (archive_code) DO NOTHING;

COMMIT;
