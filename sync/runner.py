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


def run_sync(
    conn: psycopg.Connection,
    archive_code: str,
    fetch_fn: FetchFn,
    cursor_kind: str = "sync",
    offline: bool = False,
) -> tuple[dict, bool]:
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
    #
    # offline: passed straight through to discover_stars -- see there and
    # ingest.add_star.AddStarsResult for what it does and how gaia_degraded
    # (popped out here, not left in counts) lets a caller looping over pages
    # of one archive (sync.main.sync_archive) go sticky-offline for the rest
    # of it. Left out of counts deliberately: sync_archive/reconcile_archive
    # both break their page loop on sum(counts.values()) == 0, and a stray
    # True in that dict (True == 1) would make a converged archive with a
    # degraded Gaia TAP loop forever instead of stopping.
    discovery = discover_stars(conn, archive_code, records, offline=offline)
    gaia_degraded = discovery.pop("gaia_degraded", False)

    counts = matcher.match_records(conn, archive_code, records)
    counts.update(discovery)
    record_run(conn, archive_code, new_cursor, "success", str(counts), len(records))
    return counts, gaia_degraded
