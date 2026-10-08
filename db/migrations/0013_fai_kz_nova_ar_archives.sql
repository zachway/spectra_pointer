-- Migration: register fai_kz and nova_ar as new archives -- two small GAVO
-- DaCHS data centers found in the 2026-10-08 registry-wide SSA sweep (see
-- sync/archives/fai_kz.py and sync/archives/nova_ar.py). Must be run BEFORE
-- the first sync of either archive: the holdings table's foreign key to
-- archives.archive_code otherwise rejects every row.
--
-- The same change also extends svo_cab and irsa_missions with more
-- collections; those are existing archive_codes and need no migration.
--
-- Run inside a transaction against the live database.

BEGIN;

INSERT INTO archives (archive_code, display_name, access_mechanism, has_native_gaia_column, native_gaia_dr, notes)
VALUES
    (
        'fai_kz',
        'Fesenkov Astrophysical Institute (Kazakhstan VO)',
        'tap',
        FALSE,
        NULL,
        'Found 2026-10-08 in a registry-wide SSA sweep: a GAVO DaCHS data center at dachs.fai.kz, queried through its ivoa.obscore TAP table. Two stellar collections: KazVO eShel (echelle spectra of bright hot supergiants from the Three College Observatory, 2013 onward) and the FAI AZT-8 planetary-nebula archive (1971-1998); the AGN collection on the same server is excluded. Real position, observation date and direct FITS link on every row. Small and still growing, so re-pulled in full every 30 days. See sync/archives/fai_kz.py.'
    ),
    (
        'nova_ar',
        'NOVA (Argentine Virtual Observatory)',
        'ssa',
        FALSE,
        NULL,
        'Found 2026-10-08 in a registry-wide SSA sweep: a GAVO DaCHS data center at nova.fcaglp.unlp.edu.ar (La Plata). Two stellar services: the NOVA Spectral Catalog (mostly CASLEO REOSC optical spectra, 1998-2016) and MaGOSS (Magellan/FIRE and Gemini/GNIRS near-IR spectra of Galactic O stars). Queried over SSA with no POS, since this server''s TAP faults on the MaGOSS table. MaGOSS rows carry a placeholder date and so match by name only. Re-pulled in full every 30 days. See sync/archives/nova_ar.py.'
    )
ON CONFLICT (archive_code) DO NOTHING;

COMMIT;
