"""Pins sync/matcher.py's _normalize_name, webapp/app.py's
_normalize_star_name, and scripts/export_to_parquet.py's
STAR_NAME_INDEX_NORMALIZE_SQL as equivalent.

These three independently normalize the same SIMBAD-derived star names for
three different purposes (sync-time archive cross-matching, the manual
search box, and the precomputed index the search box queries), and can't
share a single implementation across the Python/SQL boundary (see the
comments next to each). They drifted once already: PR #108 taught
matcher.py to strip "V*"/"Cl*" prefixes so variable stars and cluster
members cross-match archive records correctly, but the webapp/export pair
was never updated, leaving those same stars unfindable by manual search
even though sync matched them correctly. This test exists so that kind of
drift fails CI instead of shipping silently again.
"""

import duckdb
import pytest

from scripts.export_to_parquet import STAR_NAME_INDEX_NORMALIZE_SQL
from sync.matcher import _normalize_name

CASES = [
    "NAME Arcturus",
    "* alf Boo",
    "V* RR Lyr",
    "Cl* NGC 6791 ABC 123",
    "HR  5340",
    "Gl169.1A",
    "GJ 169.1 A",
    "Gl 169.1 A",
    "  Vega  ",
    "name vega",
]


@pytest.mark.parametrize("raw_name", CASES)
def test_matcher_and_export_sql_agree(raw_name):
    con = duckdb.connect()
    sql_result = con.execute(
        f"SELECT {STAR_NAME_INDEX_NORMALIZE_SQL.format(col='name')} FROM (SELECT ? AS name)",
        [raw_name],
    ).fetchone()[0]
    assert sql_result == _normalize_name(raw_name)


@pytest.mark.parametrize("raw_name", CASES)
def test_matcher_and_webapp_agree(raw_name, webapp_module):
    assert webapp_module._normalize_star_name(raw_name) == _normalize_name(raw_name)
