"""Figure: sky distribution of matched spectra.

Left panel: every holding matched to a star, binned at the star's position
(HEALPix, equal-area), shown as holdings per square degree.
Right panel: stars with holdings from more than one archive (archive
families, see common.ARCHIVE_FAMILY), shown as stars per square degree.

Equatorial (ICRS) Mollweide projection, RA increasing to the left, with the
Galactic plane dashed.

    python -m scripts.paper_figures.sky_distribution [--copy-to DIR] [--nside 64]
"""

from __future__ import annotations

import argparse

import healpy as hp
import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from matplotlib.colors import LogNorm

from scripts.paper_figures import common
from scripts.paper_figures.common import plt

STEM = "sky_distribution"


def healpix_density(ra, dec, weights, nside: int) -> np.ndarray:
    """Weighted counts per square degree in each HEALPix pixel."""
    ok = np.isfinite(ra) & np.isfinite(dec)
    pix = hp.ang2pix(nside, ra[ok], dec[ok], lonlat=True)
    counts = np.bincount(pix, weights=weights[ok], minlength=hp.nside2npix(nside))
    return counts / hp.nside2pixarea(nside, degrees=True)


def _sample_grid(m: np.ndarray, nside: int, n_lon: int = 1440, n_lat: int = 720):
    """Sample a HEALPix map on a regular grid in Mollweide plot coordinates.

    Plot x runs -pi..pi with RA = -x (mod 360), so RA increases to the left.
    """
    x_edges = np.linspace(-np.pi, np.pi, n_lon + 1)
    y_edges = np.linspace(-np.pi / 2, np.pi / 2, n_lat + 1)
    xc = 0.5 * (x_edges[1:] + x_edges[:-1])
    yc = 0.5 * (y_edges[1:] + y_edges[:-1])
    X, Y = np.meshgrid(xc, yc)
    ra = np.degrees(-X) % 360.0
    dec = np.degrees(Y)
    vals = m[hp.ang2pix(nside, ra, dec, lonlat=True)]
    return x_edges, y_edges, np.ma.masked_less_equal(vals, 0)


def _galactic_plane(ax) -> None:
    gal = SkyCoord(l=np.linspace(0, 360, 2000) * u.deg, b=np.zeros(2000) * u.deg, frame="galactic").icrs
    x = -np.radians(((gal.ra.deg + 180) % 360) - 180)
    y = np.radians(gal.dec.deg)
    # Break the line where it wraps around the RA = 180 deg edge.
    breaks = np.where(np.abs(np.diff(x)) > np.pi)[0] + 1
    for xs, ys in zip(np.split(x, breaks), np.split(y, breaks)):
        ax.plot(xs, ys, ls="--", lw=0.7, color="white", alpha=0.9)


def sky_panel(ax, m: np.ndarray, nside: int, title: str, cbar_label: str,
              cmap: str = "viridis", clip_percentile: float = 99.9):
    """Draw one HEALPix density map. Pixels with no data are light grey; the
    colour scale tops out at clip_percentile of the non-empty pixels so a
    handful of extremely well-observed fields don't wash out the rest."""
    x_edges, y_edges, vals = _sample_grid(m, nside)
    nonzero = m[m > 0]
    norm = LogNorm(vmin=nonzero.min(), vmax=np.percentile(nonzero, clip_percentile))
    cmap = plt.get_cmap(cmap).with_extremes(bad="#d9d9d9")
    mesh = ax.pcolormesh(x_edges, y_edges, vals, cmap=cmap, norm=norm, shading="flat", rasterized=True)
    _galactic_plane(ax)
    ax.grid(True, color="0.35", lw=0.3, alpha=0.6)
    xt = np.radians(np.arange(-150, 181, 60))
    ax.set_xticks(xt)
    ax.set_xticklabels([f"{int(round(-np.degrees(t))) % 360}°" for t in xt], color="white", fontsize=7)
    ax.tick_params(axis="y", labelsize=7)
    ax.set_title(title, pad=6)
    cb = ax.figure.colorbar(mesh, ax=ax, orientation="horizontal", pad=0.06, fraction=0.05, aspect=35,
                            extend="max")
    cb.set_label(cbar_label)
    return mesh


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_common_args(parser)
    parser.add_argument("--nside", type=int, default=64,
                        help="HEALPix nside; 64 gives ~0.84 deg^2 pixels (default: %(default)s)")
    args = parser.parse_args(argv)
    common.set_style()

    t = common.per_star_table(args)
    ra, dec = t["ra"].astype(float), t["dec"].astype(float)
    n_hold = t["n_holdings"].astype(float)
    multi = t["n_archives"] > 1

    all_map = healpix_density(ra, dec, n_hold, args.nside)
    multi_map = healpix_density(ra[multi], dec[multi], np.ones(multi.sum()), args.nside)
    print(f"{n_hold.sum():,.0f} holdings on {len(ra):,} stars; {multi.sum():,} stars in >1 archive")

    fig, axes = plt.subplots(1, 2, figsize=(common.FULL_WIDTH_IN, 2.6),
                             subplot_kw={"projection": "mollweide"})
    sky_panel(axes[0], all_map, args.nside,
              f"All matched spectra ({n_hold.sum() / 1e6:.1f}M)",
              r"Spectra deg$^{-2}$")
    sky_panel(axes[1], multi_map, args.nside,
              f"Stars in more than one archive ({multi.sum() / 1e6:.2f}M)",
              r"Stars deg$^{-2}$")
    fig.subplots_adjust(wspace=0.08)
    common.save_figure(fig, STEM, args)


if __name__ == "__main__":
    main()
