"""One-off / re-runnable: run sync.positional_fallback.run_shitty_positional_match
against every currently skipped/needs_review holding that carries a raw
position (ra/dec) but wasn't a positional_easy_match candidate at all --
matcher.py's own 1" search found nothing at any of them.

Deliberately separate from scripts.reprocess_against_new_stars: that script
replays matcher.py's existing, confident match paths against newly-added
stars; this one runs a distinct, intentionally-lower-confidence fallback
(sync/positional_fallback.py) that always lands results in needs_review, so
it's meant to be reviewed by a human afterward, not trusted as a source of
truth the way a normal reprocess run is. Re-running is safe (already
needs_review rows just get re-evaluated), but there's no reason to run a
*full* pass (skipped_only=False, the default) on a tight schedule -- unlike
scripts.reprocess_against_new_stars, nothing about *this* fallback's inputs
changes between full-pass runs except newly-tracked stars and whatever Gaia
itself updates, and a full pass over the whole multi-million-row backlog
takes on the order of days even with sync/positional_fallback.py's
per-cell-batching and epoch-bucketing optimizations.

skipped_only=True (--skipped-only) instead re-processes only match_status=
'skipped' rows, never 'needs_review' ones -- this fallback always moves a
row's status to 'needs_review' once it's touched it at all (win or lose, see
_process_cell), so 'skipped' unambiguously means "never yet attempted by
this fallback", not merely "still unmatched". That set only grows by
whatever a regular sync run's own tight-radius match just gave up on, so
it's naturally small and cheap -- safe to run on every sync.reconcile pass
(see reconcile_shitty_positional_match there), unlike a full pass. The
tradeoff: skipped_only=True alone will never revisit a 'needs_review' row
that would now match thanks to a star some *other* archive tracked since --
only an occasional full pass (still meant to be run manually/rarely, not on
this module's own schedule) catches that drift.

Candidates are paged by HEALPix cell (see _page_candidates_by_cell) instead
of all being loaded as full RawObservation records up front. Loading the
whole ~13.1M-record backlog's full columns (archive_url/instrument/obs_date/
program_id/raw_target_name, several of them text) measured at ~15.9GB
resident, and on 2026-08-20, combined with GAIA_FETCH_CONCURRENCY=5's extra
in-flight Gaia result memory, that OOM-killed the whole morgan host (twice,
confirmed via dmesg -- not just our process; nfsd and systemd-journal also
hit their own oom-killer paths). Paging keeps only a lightweight (archive_
code, archive_obs_id, ra, dec) index of the full backlog resident for the
whole run (~2GB, not ~16GB), and only re-fetches full columns for one page's
worth of candidates at a time. Pages are built by whole HEALPix cell, never
splitting one across pages, so run_shitty_positional_match's cross-archive
cell-sharing still works fully within each page -- the only real cost is a
cell that would have been queried once now occasionally getting queried
again in a later page if a different archive's candidates in the same cell
land on the far side of a page boundary.

Usage:
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.shitty_positional_match
    DATABASE_URL=postgresql:///spectra_local python3 -m scripts.shitty_positional_match --only eso lick
"""

from __future__ import annotations

import argparse
import logging
import os
from collections import defaultdict

import psycopg

from scripts.reprocess_against_new_stars import _rows_to_records_by_archive
from sync.positional_fallback import _healpix_cell, run_shitty_positional_match

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Max candidates per page. At ~1.2KB/candidate (measured: ~15.9GB / 13.1M
# full-column records), 1M candidates is ~1.2GB of RawObservation data per
# page -- comfortably bounded even stacked against several concurrent Gaia
# HEALPix-pool fetches (see GAIA_FETCH_CONCURRENCY), unlike loading the
# entire backlog's full columns at once.
CANDIDATE_PAGE_SIZE = 1_000_000

def _candidate_where(skipped_only: bool) -> str:
    """See module docstring for skipped_only's meaning: 'skipped' alone
    (never yet touched by this fallback) for a cheap incremental pass, or
    'skipped' plus 'needs_review' (also re-check what this fallback already
    tried before) for the full, multi-day backlog pass."""
    match_statuses = "('skipped')" if skipped_only else "('skipped', 'needs_review')"
    return f"match_status IN {match_statuses} AND raw_ra IS NOT NULL AND raw_dec IS NOT NULL"


def _index_candidates_by_cell(
    conn: psycopg.Connection, only_archives: list[str] | None, skipped_only: bool = False,
) -> dict[int, list[tuple[str, str]]]:
    """Lightweight (archive_code, archive_obs_id, ra, dec) pass over the
    whole backlog, immediately reduced to just (archive_code, archive_obs_id)
    keys grouped by HEALPix cell -- never materializes a plain list of every
    candidate's full row, only this per-cell index, which is what decides
    page boundaries."""
    query = f"SELECT archive_code, archive_obs_id, raw_ra, raw_dec FROM spectroscopy_holdings WHERE {_candidate_where(skipped_only)}"
    params: tuple = ()
    if only_archives:
        query += " AND archive_code = ANY(%s)"
        params = (only_archives,)

    by_cell: dict[int, list[tuple[str, str]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(query, params)
        for archive_code, archive_obs_id, ra, dec in cur:
            by_cell[_healpix_cell(ra, dec)].append((archive_code, archive_obs_id))
    return by_cell


def _page_candidates_by_cell(by_cell: dict[int, list[tuple[str, str]]], page_size: int):
    """Yields pages of (archive_code, archive_obs_id) keys, each page made
    up of whole HEALPix cells -- accumulates cells into a page until the
    next one would push it over page_size, except a single cell denser than
    page_size still gets its own (oversized) page rather than being split.
    Pops each cell out of by_cell as it's consumed so the index doesn't
    outlive its usefulness."""
    page: list[tuple[str, str]] = []
    for cell in list(by_cell.keys()):
        keys = by_cell.pop(cell)
        if page and len(page) + len(keys) > page_size:
            yield page
            page = []
        page.extend(keys)
    if page:
        yield page


def _load_candidate_page(conn: psycopg.Connection, page_keys: list[tuple[str, str]], skipped_only: bool = False) -> dict:
    by_archive_ids: dict[str, list[str]] = defaultdict(list)
    for archive_code, archive_obs_id in page_keys:
        by_archive_ids[archive_code].append(archive_obs_id)

    query = f"""
        SELECT archive_code, archive_obs_id, archive_url, instrument, obs_date, program_id,
               raw_target_name, raw_ra, raw_dec
        FROM spectroscopy_holdings
        WHERE archive_code = %s AND archive_obs_id = ANY(%s) AND {_candidate_where(skipped_only)}
    """
    rows: list[tuple] = []
    with conn.cursor() as cur:
        for archive_code, archive_obs_ids in by_archive_ids.items():
            cur.execute(query, (archive_code, archive_obs_ids))
            rows.extend(cur.fetchall())
    return _rows_to_records_by_archive(rows)


def run(conn: psycopg.Connection, only_archives: list[str] | None = None, skipped_only: bool = False) -> dict:
    by_cell = _index_candidates_by_cell(conn, only_archives, skipped_only=skipped_only)
    total_candidates = sum(len(keys) for keys in by_cell.values())
    logger.info(
        "shitty_positional_match: %d candidates across %d HEALPix cells, paging by %d (skipped_only=%s)",
        total_candidates,
        len(by_cell),
        CANDIDATE_PAGE_SIZE,
        skipped_only,
    )

    totals: dict[str, int] = {}
    for page_num, page_keys in enumerate(_page_candidates_by_cell(by_cell, CANDIDATE_PAGE_SIZE), start=1):
        by_archive = _load_candidate_page(conn, page_keys, skipped_only=skipped_only)
        logger.info("shitty_positional_match: page %d, %d candidates across %d archives", page_num, len(page_keys), len(by_archive))
        page_totals = run_shitty_positional_match(conn, by_archive)
        for key, value in page_totals.items():
            totals[key] = totals.get(key, 0) + value

    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", metavar="ARCHIVE_CODE", help="restrict to these archive_codes")
    parser.add_argument(
        "--skipped-only",
        action="store_true",
        help="only match_status='skipped' rows (never yet attempted by this fallback), not 'needs_review' ones "
        "too -- a cheap incremental pass instead of the full multi-day backlog scan (see module docstring). "
        "This is what sync.reconcile runs on every pass; pass this directly for a manual one-off incremental run.",
    )
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        totals = run(conn, only_archives=args.only, skipped_only=args.skipped_only)
    logger.info("done: %s", totals)


if __name__ == "__main__":
    main()
