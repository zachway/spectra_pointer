"""Figure: Gaia colour-magnitude distribution of matched spectra.

Same two panels as sky_distribution, on the Gaia CMD instead of the sky:
Left: every holding matched to a star, binned by the star's (BP-RP, M_G).
Right: stars with holdings from more than one archive (archive families,
see common.ARCHIVE_FAMILY).

M_G = G + 5 log10(parallax / mas) - 10, with no extinction correction and no
parallax-quality cut (the export carries no parallax_error); stars with
parallax <= 0 or missing BP/RP photometry are left out, and the count of
those is printed.

    python -m scripts.paper_figures.cmd_distribution [--copy-to DIR]
"""

from __future__ import annotations

import argparse

import numpy as np
from matplotlib.colors import LogNorm

from scripts.paper_figures import common
from scripts.paper_figures.common import plt

STEM = "cmd_distribution"

BP_RP_RANGE = (-0.75, 5.0)
MG_RANGE = (-6.0, 17.0)


def cmd_panel(ax, x, y, weights, bins, title: str, cbar_label: str, cmap: str = "viridis",
              clip_percentile: float = 99.9):
    h, xe, ye = np.histogram2d(x, y, bins=bins, range=[BP_RP_RANGE, MG_RANGE], weights=weights)
    h = np.ma.masked_less_equal(h, 0)
    norm = LogNorm(vmin=h.min(), vmax=np.percentile(h.compressed(), clip_percentile))
    mesh = ax.pcolormesh(xe, ye, h.T, cmap=cmap, norm=norm, rasterized=True)
    ax.set_xlim(*BP_RP_RANGE)
    ax.set_ylim(MG_RANGE[1], MG_RANGE[0])
    ax.set_xlabel(r"$G_\mathrm{BP} - G_\mathrm{RP}$ (mag)")
    ax.set_title(title, pad=4)
    cb = ax.figure.colorbar(mesh, ax=ax, pad=0.02, extend="max")
    cb.set_label(cbar_label)
    return mesh


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_common_args(parser)
    parser.add_argument("--bins", type=int, nargs=2, default=(230, 230), metavar=("NX", "NY"),
                        help="histogram bins in BP-RP and M_G (default: %(default)s)")
    args = parser.parse_args(argv)
    common.set_style()

    t = common.per_star_table(args)
    plx = t["parallax"].astype(float)
    g = t["phot_g_mean_mag"].astype(float)
    bp_rp = t["bp_rp"].astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        abs_g = g + 5 * np.log10(plx) - 10
    ok = np.isfinite(abs_g) & np.isfinite(bp_rp) & (plx > 0)
    n_hold = t["n_holdings"].astype(float)
    multi = t["n_archives"] > 1
    print(f"{ok.sum():,} of {len(ok):,} stars plottable "
          f"({n_hold[ok].sum():,.0f} of {n_hold.sum():,.0f} holdings); "
          f"{(ok & multi).sum():,} of {multi.sum():,} multi-archive stars")

    fig, axes = plt.subplots(1, 2, figsize=(common.FULL_WIDTH_IN, 3.4), sharey=True)
    cmd_panel(axes[0], bp_rp[ok], abs_g[ok], n_hold[ok], args.bins,
              f"All matched spectra ({n_hold[ok].sum() / 1e6:.1f}M)", "Spectra per bin")
    sel = ok & multi
    cmd_panel(axes[1], bp_rp[sel], abs_g[sel], None, args.bins,
              f"Stars in more than one archive ({sel.sum() / 1e6:.2f}M)", "Stars per bin")
    axes[0].set_ylabel(r"$M_G$ (mag)")
    fig.subplots_adjust(wspace=0.32)
    common.save_figure(fig, STEM, args)


if __name__ == "__main__":
    main()
