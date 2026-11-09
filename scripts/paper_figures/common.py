"""Shared data access, star naming and output handling for the paper figures.

Every figure is built from the public Parquet exports (the same files the
webapp reads -- see scripts.export_to_parquet), so anyone can regenerate them
without database access:

    https://astro.gsu.edu/~way/spectra_data/

DuckDB reads only the columns each query needs over HTTP, so a full run pulls
a few hundred MB, not the multi-GB files. Pass --data-source with a local
export directory to work offline.

The expensive step (one row per matched star: holdings count, distinct
archive count, position and photometry) is cached as Parquet under
paper/.cache/ and reused by the sky and CMD figures. Use --refresh to
rebuild it after a new export.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

import duckdb
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

DEFAULT_DATA_SOURCE = "https://astro.gsu.edu/~way/spectra_data"

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "paper" / "figures"
DEFAULT_CACHE_DIR = REPO_ROOT / "paper" / ".cache"

# AASTeX two-column text width is ~7.1 in; figure* environments span it.
FULL_WIDTH_IN = 7.1

# Some archives are split into several archive_codes (one per instrument or
# per access route). For "observed by more than one archive" those should
# count once -- a star with ESO pipeline products and ESO raw frames has not
# been seen by two archives. Codes not listed here are their own family.
ARCHIVE_FAMILY = {
    "aat_2df": "aat",
    "carmenes_caha": "carmenes",
    "carmenes_reiners2018": "carmenes",
    "carmenes_tac": "carmenes",
    "eso_raw": "eso",
    "gemini_ghost": "gemini",
    "gemini_igrins": "gemini",
    "heros_ondrejov": "ondrejov",
    "irtf_ishell": "irtf",
    "irtf_legacy": "irtf",
    "irtf_spex": "irtf",
    "lamost_mrs": "lamost",
    "lco_floyds": "lco",
    "lco_nres": "lco",
    "mast_jwst": "mast",
    "subaru_moircs": "naoj",
    "sdss_legacy_optical": "sdss",
    "sdss_v_apogee": "sdss",
    "sdss_v_optical": "sdss",
}


# --------------------------------------------------------------------------
# Command line / output
# --------------------------------------------------------------------------

def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-source", default=DEFAULT_DATA_SOURCE,
                        help="URL or local directory holding the Parquet exports (default: %(default)s)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help="where the figures are written (default: paper/figures in this repo)")
    parser.add_argument("--copy-to", type=Path, default=None,
                        help="also copy each figure here, e.g. the paper's figures/ directory")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="where the per-star table is cached (default: paper/.cache)")
    parser.add_argument("--refresh", action="store_true",
                        help="rebuild the cached per-star table from the exports")
    parser.add_argument("--matched-only", action="store_true",
                        help="drop tentative shitty_positional_match (needs_review) assignments "
                             "and keep only match_status = 'matched' holdings")
    parser.add_argument("--dpi", type=int, default=300)


def set_style() -> None:
    plt.rcParams.update({
        "font.family": "serif",
        "font.size": 9,
        "axes.titlesize": 9,
        "axes.labelsize": 9,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "mathtext.fontset": "dejavuserif",
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.03,
    })


def save_figure(fig, stem: str, args: argparse.Namespace) -> Path:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f"{stem}.png"
    fig.savefig(path, dpi=args.dpi)
    plt.close(fig)
    print(f"wrote {path}")
    if args.copy_to is not None:
        args.copy_to.expanduser().mkdir(parents=True, exist_ok=True)
        dest = args.copy_to.expanduser() / path.name
        shutil.copy2(path, dest)
        print(f"copied to {dest}")
    return path


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

def connect(data_source: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    if data_source.startswith(("http://", "https://")):
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
    return con


def parquet(data_source: str, table: str) -> str:
    """SQL table expression for one exported table."""
    return f"read_parquet('{data_source.rstrip('/')}/{table}.parquet')"


def holdings_filter(matched_only: bool) -> str:
    """WHERE clause for "a holding matched to a star".

    By default this matches the webapp's Leaderboard: every holding with a
    star_id, including tentative shitty_positional_match assignments.
    """
    clause = "star_id IS NOT NULL"
    if matched_only:
        clause += " AND match_status = 'matched'"
    return clause


def _family_values_sql() -> str:
    rows = ", ".join(f"('{code}', '{fam}')" for code, fam in sorted(ARCHIVE_FAMILY.items()))
    return f"(VALUES {rows}) AS fam(archive_code, family)"


def per_star_table(args: argparse.Namespace) -> dict:
    """One row per matched star, as a dict of numpy arrays.

    Columns: star_id, n_holdings, n_archives (distinct archive *families*,
    see ARCHIVE_FAMILY), ra, dec, parallax, phot_g_mean_mag, bp_rp.
    """
    suffix = "matched_only" if args.matched_only else "all"
    cache = args.cache_dir / f"per_star_{suffix}.parquet"
    con = connect(args.data_source)
    if args.refresh or not cache.exists():
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        print(f"building {cache} from {args.data_source} ...")
        con.execute(f"""
            COPY (
                WITH h AS (
                    SELECT h.star_id, COALESCE(fam.family, h.archive_code) AS family
                    FROM {parquet(args.data_source, 'spectroscopy_holdings')} h
                    LEFT JOIN {_family_values_sql()} USING (archive_code)
                    WHERE {holdings_filter(args.matched_only)}
                ),
                per_star AS (
                    SELECT star_id, count(*) AS n_holdings, count(DISTINCT family) AS n_archives
                    FROM h GROUP BY star_id
                )
                SELECT p.star_id, p.n_holdings, p.n_archives,
                       s.ra, s.dec, s.parallax, s.phot_g_mean_mag,
                       s.phot_bp_mean_mag - s.phot_rp_mean_mag AS bp_rp
                FROM per_star p
                JOIN {parquet(args.data_source, 'stars')} s USING (star_id)
            ) TO '{cache}' (FORMAT parquet)
        """)
    cols = con.execute(f"SELECT * FROM read_parquet('{cache}')").fetchnumpy()
    # fetchnumpy returns NULLs as masked arrays whose hidden data is 0, and
    # np.isfinite doesn't see through the mask -- a NULL BP-RP would land at
    # exactly 0. Make NULL an explicit NaN in the float columns.
    out = {}
    for name, arr in cols.items():
        if name in ("star_id", "n_holdings", "n_archives"):
            out[name] = np.asarray(arr)
        else:
            out[name] = np.ma.filled(np.ma.asarray(arr, dtype=float), np.nan)
    return out


# --------------------------------------------------------------------------
# Star names
# --------------------------------------------------------------------------

# Hand-set labels for stars whose aliases don't yield a good name
# automatically. Keyed by star_id (stable across exports).
LABEL_OVERRIDES: dict[int, str] = {
    9180302: "Alkaid",  # eta UMa; its only NAME alias is a raw "NAME ALCAID"
}

GREEK = {
    "alf": "alpha", "alp": "alpha", "alpha": "alpha", "bet": "beta", "beta": "beta",
    "gam": "gamma", "gamma": "gamma", "del": "delta", "delta": "delta",
    "eps": "epsilon", "epsilon": "epsilon", "zet": "zeta", "zeta": "zeta",
    "eta": "eta", "tet": "theta", "the": "theta", "theta": "theta",
    "iot": "iota", "iota": "iota", "kap": "kappa", "kappa": "kappa",
    "lam": "lambda", "lambda": "lambda", "mu.": "mu", "mu": "mu", "nu.": "nu", "nu": "nu",
    "ksi": "xi", "xi": "xi", "omi": "omicron", "omicron": "omicron", "pi.": "pi", "pi": "pi",
    "rho": "rho", "sig": "sigma", "sigma": "sigma", "tau": "tau", "ups": "upsilon",
    "upsilon": "upsilon", "phi": "phi", "chi": "chi", "psi": "psi", "ome": "omega", "omega": "omega",
}

# "* alf Cen A", "* tau Boo", "* alf02 Cen", "Eta Car", "beta Pic"
_BAYER = re.compile(r"^(?:\*\s+)?([a-z]{2,7}\.?)(\d{0,2})\s+([A-Za-z]{3})(?:\s+([A-Z]))?$", re.I)
# "* 55 Cnc", "*  61 Dra"
_FLAMSTEED = re.compile(r"^\*\s+(\d{1,3})\s+([A-Za-z]{3})(?:\s+([A-Z]))?$")
# "* a Cen", "* b Per"
_LATIN_BAYER = re.compile(r"^\*\s+([a-zA-Z])\s+([A-Za-z]{3})(?:\s+([A-Z]))?$")
# "BD+ 28 4211", "BD +28 4211", "CPD-60 5483"
_DURCHMUSTERUNG = re.compile(r"^(BD|CD|CPD)\s*([+-])\s*(\d+)\s+(\d+)\s*([A-Za-z]?)$", re.I)
_PLANET_SURVEYS = {"WASP": "WASP", "KELT": "KELT", "HAT-P": "HAT-P", "HATP": "HAT-P", "XO": "XO",
                   "TRES": "TrES", "QATAR": "Qatar", "COROT": "CoRoT", "KEPLER": "Kepler", "K2": "K2"}
_PLANET_HOST = re.compile(r"^(WASP|KELT|HAT-?P|XO|TrES|Qatar|CoRoT|Kepler|K2)[\s_-]*(\d+)\s*([A-Za-z]?)$", re.I)
_CATALOG_ORDER = ("HD", "GJ", "HIP", "HR", "BD", "CD", "CPD", "TYC", "2MASS")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


_IAU_CONSTELLATIONS = (
    "And Ant Aps Aqr Aql Ara Ari Aur Boo Cae Cam Cnc CVn CMa CMi Cap Car Cas Cen Cep Cet Cha Cir Col "
    "Com CrA CrB Crv Crt Cru Cyg Del Dor Dra Equ Eri For Gem Gru Her Hor Hya Hyi Ind Lac Leo LMi Lep "
    "Lib Lup Lyn Lyr Men Mic Mon Mus Nor Oct Oph Ori Pav Peg Per Phe Pic Psc PsA Pup Pyx Ret Sge Sgr "
    "Sco Scl Sct Ser Sex Tau Tel Tri TrA Tuc UMa UMi Vel Vir Vol Vul"
).split()
_CONSTELLATION_CASE = {c.lower(): c for c in _IAU_CONSTELLATIONS}


def _constellation(abbr: str) -> str:
    """IAU capitalisation of a constellation abbreviation ("UMA" -> "UMa")."""
    return _CONSTELLATION_CASE.get(abbr.lower(), abbr[0].upper() + abbr[1:].lower())


def _greek_label(alias: str, mathtext: bool) -> str | None:
    m = _BAYER.match(_clean(alias))
    if not m:
        return None
    letter, num, con, comp = m.groups()
    name = GREEK.get(letter.lower())
    if name is None:
        return None
    sym = f"$\\{name}$" if mathtext else name
    if name == "omicron":  # no \omicron in TeX/mathtext
        sym = "o" if mathtext else name
    label = f"{sym}{'$^{' + str(int(num)) + '}$' if (num and mathtext) else num} {_constellation(con)}"
    return f"{label} {comp}" if comp else label


def star_label(aliases, input_name=None, gaia_source_id=None, star_id=None, mathtext: bool = True) -> str:
    """Best human-readable name for a star from its name_aliases.

    Preference: LABEL_OVERRIDES > SIMBAD "NAME ..." common name > Bayer
    (Greek letter) designation > Flamsteed number > variable-star name >
    HD/GJ/HIP/... catalogue number > first alias > Gaia DR3 id.
    """
    if star_id is not None and star_id in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[star_id]
    aliases = [_clean(a) for a in (aliases or []) if a and a.strip()]
    if input_name:
        aliases.append(_clean(input_name))

    # SIMBAD common names. Raw target names sometimes mimic the format in
    # capitals ("NAME ALCAID"), so prefer a mixed-case one.
    # capitals ("NAME ALCAID"); an all-caps one ranks below a Bayer name.
    names = [a[5:].strip() for a in aliases if a.startswith("NAME ") and a[5:].strip()]
    mixed = [n for n in names if not n.isupper()]
    if mixed:
        return mixed[0]
    # SIMBAD-formatted Bayer ids ("* bet Pic") first, then raw ones ("Eta Car").
    for simbad_only in (True, False):
        for a in aliases:
            if simbad_only != a.startswith("* "):
                continue
            g = _greek_label(a, mathtext)
            if g:
                return g
        if simbad_only and names:
            return names[0].title()
    for a in aliases:  # Latin-letter Bayer, "* a Cen"
        m = _LATIN_BAYER.match(a)
        if m:
            letter, con, comp = m.groups()
            return f"{letter} {_constellation(con)}" + (f" {comp}" if comp else "")
    for a in aliases:
        m = _FLAMSTEED.match(a)
        if m:
            num, con, comp = m.groups()
            return f"{int(num)} {_constellation(con)}" + (f" {comp}" if comp else "")
    for a in aliases:
        if a.startswith("V* "):
            return a[3:]
    for a in aliases:  # exoplanet-survey host names: "WASP 33", "WASP-033"
        m = _PLANET_HOST.match(a)
        if m:
            return f"{_PLANET_SURVEYS[m.group(1).upper()]}-{int(m.group(2))}{m.group(3).upper()}"
    for prefix in _CATALOG_ORDER:
        if prefix in ("BD", "CD", "CPD"):
            for a in aliases:
                m = _DURCHMUSTERUNG.match(a)
                if m and m.group(1).upper() == prefix:
                    cat, sign, zone, num, comp = m.groups()
                    return f"{prefix}{sign}{int(zone)} {int(num)}{comp}"
            continue
        for a in aliases:
            m = re.match(rf"^{prefix}\s*([+-]?[\d ]+[A-Z]?)$", a)
            if m:
                return f"{prefix} {m.group(1).strip().lstrip('0') if prefix in ('HD', 'HIP', 'HR') else m.group(1).strip()}"
    if aliases:
        return aliases[0]
    return f"Gaia DR3 {gaia_source_id}" if gaia_source_id is not None else f"star {star_id}"


def star_labels(con: duckdb.DuckDBPyConnection, data_source: str, star_ids, mathtext: bool = True) -> dict[int, str]:
    ids = sorted({int(s) for s in star_ids})
    if not ids:
        return {}
    rows = con.execute(f"""
        SELECT star_id, name_aliases, input_name, gaia_source_id
        FROM {parquet(data_source, 'stars')}
        WHERE star_id IN ({', '.join(map(str, ids))})
    """).fetchall()
    return {sid: star_label(al, inp, gid, sid, mathtext) for sid, al, inp, gid in rows}
