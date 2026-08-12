-- Migration: add a second, independent cursor to archive_sync_state for
-- periodic full re-walks (sync/reconcile.py), alongside the existing live
-- incremental sync_cursor.
--
-- Root cause this addresses: most archives (see sync/reconcile.py's own
-- docstring for the full list) paginate on an astronomical observation-time
-- column (t_min, mjd, dateobs, ...) via `WHERE col > cursor`. That watermark
-- only ever moves forward. If the source archive later adds or re-releases
-- a record with an OLD timestamp -- reprocessing, embargo lifting, backfilled
-- historical data -- it lands behind the watermark and a normal incremental
-- sync.main run will never see it again. Confirmed live for mast.py (real
-- spectra silently missed this way).
--
-- reconcile_cursor is deliberately separate from sync_cursor rather than
-- reusing it: sync.main's incremental cursor must keep advancing on every
-- run to stay cheap, while reconcile.py intentionally restarts from the
-- beginning of an archive's history and walks back up to the present (then
-- wraps around), which would fight over a shared cursor. Reuses the exact
-- fetch()/matcher upsert path as sync.main (see sync/reconcile.py) -- no
-- archive module needed to change for this migration.
--
-- Run inside a transaction against the live database.

BEGIN;

ALTER TABLE archive_sync_state
    ADD COLUMN reconcile_cursor          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN reconcile_last_run_at      TIMESTAMPTZ,
    ADD COLUMN reconcile_last_run_status  TEXT CHECK (reconcile_last_run_status IN ('success', 'partial', 'failed')),
    ADD COLUMN reconcile_last_run_notes   TEXT,
    ADD COLUMN reconcile_rows_seen_last_run INTEGER;

COMMIT;

-- Application code (sync/state.py, sync/runner.py, sync/main.py,
-- sync/reconcile.py) already updated to match this schema in the same PR
-- that added this migration -- deploy alongside it.
