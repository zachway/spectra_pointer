"""Derives webapp.spectrum_viewer.SCALE_FACTOR -- the per-archive display
scale that lets the spectrum viewer's multi-spectrum overlay share one
"Scaled Flux" y-axis instead of needing a separate auto-scaled axis per
flux_unit (the earlier approach: correct, but visually busier than the
user wanted once several instruments were on one plot).

Not a physical calibration -- most of these archives have none at all (see
FLUX_FAMILY_ARBITRARY in webapp/spectrum_viewer.py). For each archive_code
in SUPPORTED_ARCHIVES, this fetches one real matched holding (the first one
DuckDB returns, same as ad-hoc live-verification elsewhere in this project),
takes the median of |flux| across that spectrum's finite, nonzero pixels as
a robust "typical magnitude" for that archive, and sets factor = 1 / typical
so a representative spectrum from that archive plots around order ~1.
Fixed and multiplicative -- applied identically to every holding from that
archive_code regardless of the actual star's brightness, so a real
brighter/fainter star still plots higher/lower after scaling (this is a
per-archive display convenience, not a per-spectrum renormalization that
would flatten every star to the same height).

The 4 archives already sharing FLUX_FAMILY_ERG_CM2_S_A (a real physical
baseline -- desi, mast_jwst, sdss_v_optical/sdss_legacy_optical, see
spectrum_viewer.py's Jy->erg/s/cm^2/A conversion) get ONE shared factor,
the geometric mean of 3 real examples (desi, mast_jwst, sdss_v_optical --
sdss_legacy_optical uses the identical SDSS spec-file convention, no
separate example needed), not 4 independent ones -- giving them different
factors would have thrown away the real relative comparability that unit
conversion already earned them. Every other archive has no shared basis to
begin with, so each gets its own independent factor.

Run: python3 -m scripts.derive_flux_scale_factors
(needs SPECTRA_DATA_URL or SPECTRA_DATA_DIR, same as webapp.app)

This is a one-off derivation script, not something run on a schedule --
SCALE_FACTOR in webapp/spectrum_viewer.py is a plain hardcoded dict, updated
by hand if a re-run here produces meaningfully different numbers (e.g. after
a new archive is added to SUPPORTED_ARCHIVES).
"""

import os

import duckdb
import numpy as np

from webapp.spectrum_viewer import SUPPORTED_ARCHIVES, SpectrumUnavailable, fetch_spectrum

ERG_FAMILY_CODES = ("desi", "mast_jwst", "sdss_v_optical")
TARGET_MAGNITUDE = 1.0


def _get_cursor() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    data_url = os.environ.get("SPECTRA_DATA_URL")
    data_dir = os.environ.get("SPECTRA_DATA_DIR")
    source = data_url if data_url else data_dir
    if not source:
        raise RuntimeError("Set SPECTRA_DATA_URL or SPECTRA_DATA_DIR (see webapp.app's module docstring).")
    con.execute(f"CREATE VIEW holdings AS SELECT * FROM read_parquet('{source}/spectroscopy_holdings.parquet')")
    return con


def _median_abs_flux(archive_code: str, con: duckdb.DuckDBPyConnection) -> float | None:
    extra = "AND instrument LIKE 'Spitzer/IRS%'" if archive_code == "irsa_missions" else ""
    row = con.execute(
        f"SELECT archive_url, archive_obs_id, instrument FROM holdings "
        f"WHERE archive_code=? AND match_status='matched' {extra} LIMIT 1",
        [archive_code],
    ).fetchone()
    if row is None:
        print(f"{archive_code}: no matched sample found")
        return None
    holding = {"archive_code": archive_code, "archive_url": row[0], "archive_obs_id": row[1], "instrument": row[2]}
    try:
        result = fetch_spectrum(holding)
    except SpectrumUnavailable as exc:
        print(f"{archive_code}: rejected -- {exc}")
        return None
    all_flux = np.concatenate([np.abs(np.array(seg["flux"])) for seg in result["segments"]])
    all_flux = all_flux[np.isfinite(all_flux) & (all_flux > 0)]
    if len(all_flux) == 0:
        print(f"{archive_code}: no finite nonzero flux points in sample")
        return None
    return float(np.median(all_flux))


def main() -> None:
    con = _get_cursor()

    erg_medians = []
    for code in ERG_FAMILY_CODES:
        med = _median_abs_flux(code, con)
        if med is not None:
            erg_medians.append(med)
    if erg_medians:
        erg_geomean = float(np.exp(np.mean(np.log(erg_medians))))
        erg_factor = TARGET_MAGNITUDE / erg_geomean
        print(f"\nerg_cm2_s_A_1e-17 family: geomean={erg_geomean:.6g}, shared factor={erg_factor:.6g}")
        print("  applies to: desi, mast_jwst, sdss_v_optical, sdss_legacy_optical\n")

    for code in sorted(SUPPORTED_ARCHIVES - set(ERG_FAMILY_CODES) - {"sdss_legacy_optical"}):
        med = _median_abs_flux(code, con)
        if med is not None:
            factor = TARGET_MAGNITUDE / med
            print(f"{code}: median|flux|={med:.6g}, factor={factor:.6g}")


if __name__ == "__main__":
    main()
