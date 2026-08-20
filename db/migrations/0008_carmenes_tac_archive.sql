-- Migration: register carmenes_tac as a new archive -- the CARMENES
-- telluric-corrected template library, a sibling dataset to carmenes.py's
-- DR1 on the same GTO portal (found via welcome.action while investigating
-- the whole-star-zip-only access caveat on `carmenes` noted in the Spectral
-- Access Ledger audit). Unlike DR1, this one has a real direct per-channel
-- FITS file per star, no zip needed -- see sync/archives/carmenes_tac.py
-- for the full live-verification notes.
--
-- Run inside a transaction against the live database.

BEGIN;

INSERT INTO archives (archive_code, display_name, access_mechanism, has_native_gaia_column, native_gaia_dr, notes)
VALUES (
    'carmenes_tac',
    'CARMENES Telluric-Corrected Template Library',
    'bulk_file',
    FALSE,
    NULL,
    'A sibling dataset on the same GTO portal as carmenes.py''s DR1, linked from welcome.action as "Telluric absorption corrected high S/N optical and near-infrared template spectra of 382 M dwarf stars" (Nagel, Czesla, Kaminski et al. 2023 A&A, in press) -- jsp/tellurics_tac.jsp, 382 rows confirmed live (matches the paper''s own star count). Unlike DR1''s whole-star-zip-only access, each row here carries a real direct per-channel FITS link (getTacDataPublic.action?id=<Karmn>_VIS.fits / _NIR.fits), fetched live with a plain unauthenticated GET -- a real VIS sample came back exactly 5,466,240 bytes, a real NIR sample 2,759,040 bytes, both with SPEC/SIG/WAVE image extensions (one row per echelle order). The archive''s own readme (getTacDataPublic.action?id=carmenes.taclibrary.readme.txt, fetched live) documents SPEC=template flux, SIG=uncertainty, WAVE=natural log of vacuum wavelength (needs np.exp). Two holdings per star (VIS, NIR), not one -- genuinely separate channel files, same shape as oirsa.py''s one-archive-many-instruments case. No per-observation date -- a co-add across every epoch used, same tradeoff as carmenes.py''s own DR1 zip and sdss_v_apogee.py''s apStar files. Gaia resolution via ingest.add_star.resolve_stellar_gaia_ids_batch (SIMBAD discovery-name match, batched), same Karmn/name-pair shape as DR1 but using the shared helper instead of carmenes.py''s own inline SIMBAD query. Static, closed dataset (GTO ended, paper already in press) -- one-shot pull via a synced_at cursor. reduction_status hardcoded ''reduced'' -- explicitly a SERVAL co-add "template" spectrum per the readme, never a raw frame.'
)
ON CONFLICT (archive_code) DO NOTHING;

COMMIT;
