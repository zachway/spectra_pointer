"""Read/write archive_sync_state — per-archive sync progress bookkeeping."""

import psycopg
from psycopg.types.json import Jsonb


def get_cursor(conn: psycopg.Connection, archive_code: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT sync_cursor FROM archive_sync_state WHERE archive_code = %s", (archive_code,))
        row = cur.fetchone()
    return row[0] if row else {}


def record_run(
    conn: psycopg.Connection,
    archive_code: str,
    cursor: dict,
    status: str,
    notes: str,
    rows_seen: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO archive_sync_state
                (archive_code, sync_cursor, last_run_at, last_run_status, last_run_notes, rows_seen_last_run)
            VALUES (%s, %s, now(), %s, %s, %s)
            ON CONFLICT (archive_code) DO UPDATE SET
                sync_cursor = EXCLUDED.sync_cursor,
                last_run_at = EXCLUDED.last_run_at,
                last_run_status = EXCLUDED.last_run_status,
                last_run_notes = EXCLUDED.last_run_notes,
                rows_seen_last_run = EXCLUDED.rows_seen_last_run
            """,
            (archive_code, Jsonb(cursor), status, notes, rows_seen),
        )
    conn.commit()


# reconcile_cursor is a second, independent progress marker used by
# sync/reconcile.py to periodically re-walk an archive from the start of its
# history (see db/migrations/0006_reconcile_cursor.sql for why this can't
# just reuse sync_cursor). Same shape as get_cursor/record_run above, just
# against the reconcile_* columns instead.


def get_reconcile_cursor(conn: psycopg.Connection, archive_code: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT reconcile_cursor FROM archive_sync_state WHERE archive_code = %s", (archive_code,))
        row = cur.fetchone()
    return row[0] if row else {}


def record_reconcile_run(
    conn: psycopg.Connection,
    archive_code: str,
    cursor: dict,
    status: str,
    notes: str,
    rows_seen: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO archive_sync_state
                (archive_code, reconcile_cursor, reconcile_last_run_at, reconcile_last_run_status,
                 reconcile_last_run_notes, reconcile_rows_seen_last_run)
            VALUES (%s, %s, now(), %s, %s, %s)
            ON CONFLICT (archive_code) DO UPDATE SET
                reconcile_cursor = EXCLUDED.reconcile_cursor,
                reconcile_last_run_at = EXCLUDED.reconcile_last_run_at,
                reconcile_last_run_status = EXCLUDED.reconcile_last_run_status,
                reconcile_last_run_notes = EXCLUDED.reconcile_last_run_notes,
                reconcile_rows_seen_last_run = EXCLUDED.reconcile_rows_seen_last_run
            """,
            (archive_code, Jsonb(cursor), status, notes, rows_seen),
        )
    conn.commit()
