"""Generic driver: cursor in, discover, match, cursor out — same shape for every archive."""

import psycopg

from ingest.add_star import discover_stars
from sync import matcher, state
from sync.base import FetchFn

# cursor_kind selects which pair of archive_sync_state columns to read/write:
# "sync" is the live incremental cursor sync.main advances on every run;
# "reconcile" is sync.reconcile's independent cursor for periodic full
# re-walks (see db/migrations/0006_reconcile_cursor.sql). Same fetch_fn,
# same matcher upsert path either way — only the progress bookkeeping differs.
_CURSOR_FNS = {
    "sync": (state.get_cursor, state.record_run),
    "reconcile": (state.get_reconcile_cursor, state.record_reconcile_run),
}


def run_sync(conn: psycopg.Connection, archive_code: str, fetch_fn: FetchFn, cursor_kind: str = "sync") -> dict:
    get_cursor, record_run = _CURSOR_FNS[cursor_kind]
    cursor = get_cursor(conn, archive_code)
    try:
        records, new_cursor = fetch_fn(cursor)
    except Exception as exc:
        record_run(conn, archive_code, cursor, "failed", str(exc), 0)
        raise

    # Discover new stars from this page before matching — otherwise every
    # record for a not-yet-tracked star gets silently counted as "skipped"
    # by matcher.match_records (it only matches against stars already in the
    # table). See ingest.add_star.discover_stars.
    discovery = discover_stars(conn, archive_code, records)

    counts = matcher.match_records(conn, archive_code, records)
    counts.update(discovery)
    record_run(conn, archive_code, new_cursor, "success", str(counts), len(records))
    return counts
