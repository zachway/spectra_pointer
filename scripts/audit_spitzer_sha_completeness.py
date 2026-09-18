"""Completeness audit for the spitzer_sha archive.

sync/archives/spitzer_sha.py finds IRS/MIPS-SED AORs by crawling an
undocumented search service on a sky grid. IRSA's file server lists the same
AORs independently: /data/SPITZER/SHA/archive/proc/<CAMPAIGN>/r<AORKEY>/ for
every processed AOR (IRSX* = IRS, MIPS* = MIPS, IRAC* = IRAC). This script
lists every IRSX campaign directory (and, with --include-mips, MIPS ones --
far more, mostly photometry/scan AORs the sync deliberately drops) and
compares the AORKEYs found there against spectroscopy_holdings rows for
archive_code 'spitzer_sha'.

Any AORKEY in the tree but not in the DB is a coverage hole (a grid cell that
missed it, or a search-service change). AORKEYs in the DB but not the tree are
fine to see for MIPS-SED (campaign dirs only listed with --include-mips).

Usage:
    python -m scripts.audit_spitzer_sha_completeness            # tree count only, no DB
    python -m scripts.audit_spitzer_sha_completeness --compare  # needs DATABASE_URL
"""

import argparse
import os
import re
import time

import requests

PROC_URL = "https://irsa.ipac.caltech.edu/data/SPITZER/SHA/archive/proc/"
REQUEST_DELAY_SEC = 0.3


def _list(url: str, pattern: str) -> list[str]:
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return sorted(set(re.findall(pattern, resp.text)))


def tree_aorkeys(include_mips: bool) -> dict[str, str]:
    """{aorkey: campaign_dir} for every processed AOR in the chosen instrument
    campaign directories."""
    prefixes = ("IRSX", "MIPS") if include_mips else ("IRSX",)
    campaigns = [d for d in _list(PROC_URL, r'href="([A-Z0-9]+)/"') if d.startswith(prefixes)]
    found: dict[str, str] = {}
    for campaign in campaigns:
        for r_dir in _list(f"{PROC_URL}{campaign}/", r'href="r(\d+)/"'):
            found[r_dir] = campaign
        time.sleep(REQUEST_DELAY_SEC)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--include-mips", action="store_true")
    parser.add_argument("--compare", action="store_true", help="compare against spectroscopy_holdings (needs DATABASE_URL)")
    args = parser.parse_args()

    tree = tree_aorkeys(args.include_mips)
    print(f"{len(tree)} AORs in the file tree ({'IRSX + MIPS' if args.include_mips else 'IRSX only'})")
    if not args.compare:
        return

    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT archive_obs_id FROM spectroscopy_holdings WHERE archive_code = 'spitzer_sha'")
        in_db = {row[0] for row in cur.fetchall()}
    missing = sorted(set(tree) - in_db)
    print(f"{len(in_db)} spitzer_sha holdings in the DB; {len(missing)} tree AORs missing from the DB")
    for key in missing[:50]:
        print(f"  missing: {key} ({tree[key]})")


if __name__ == "__main__":
    main()
