-- Migration: register spitzer_sha as a new archive -- per-AOR metadata for
-- every Spitzer IRS staring/mapping observation plus MIPS-SED, via the
-- Spitzer Heritage Archive's own search service (see sync/archives/
-- spitzer_sha.py). Must be run BEFORE the first sync of this archive: the
-- holdings table's foreign key to archives.archive_code otherwise rejects
-- every row.
--
-- Run inside a transaction against the live database.

BEGIN;

INSERT INTO archives (archive_code, display_name, access_mechanism, has_native_gaia_column, native_gaia_dr, notes)
VALUES (
    'spitzer_sha',
    'Spitzer Heritage Archive (IRS + MIPS-SED)',
    'rest_json',
    FALSE,
    NULL,
    'Per-AOR metadata for every IRS staring/mapping observation plus MIPS-SED (55-95 um), Spitzer''s 2003-2009 cryogenic-mission spectra that irsa_missions.py''s small curated collections miss. No documented API: found 2026-09-18 by replaying the SHA web app''s own Firefly search (unauthenticated POST to sha.ipac.caltech.edu/applications/Spitzer/SHA/CmdSrv/sync?cmd=tableSearch, id aorByPosition), crawled as a fixed sky grid. Real observation dates on every row. archive_url is the AOR''s directory on IRSA''s file server. See sync/archives/spitzer_sha.py.'
)
ON CONFLICT (archive_code) DO NOTHING;

COMMIT;
