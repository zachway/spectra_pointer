"""Figure: the most observed star of all time, over time.

Like the webapp Leaderboard's "Cumulative observations" chart, but for the
paper. Cumulative dated spectra per star in half-year steps:
  * the record -- the #1 star's cumulative count at each time -- as a thick
    grey band, with a numbered dot each time a new star takes the lead and a
    numbered key naming them;
  * today's TOP_N most observed stars in colour, named in a ranked legend;
  * former record holders not in today's top N in grey.
The x axis is broken at 1975: before that only a handful of (mostly
photographic) Eta Car spectra exist, after it the curves crowd together.

Stars are labelled with their common name where SIMBAD has one (see
common.star_label; add to common.LABEL_OVERRIDES to change any label).

Only holdings with an obs_date count (DESI and SDSS-V APOGEE carry none), so
totals here can be below the all-time counts in the paper's table. A star
only takes the record by strictly exceeding the holder's count; ties keep
the incumbent.

    python -m scripts.paper_figures.most_observed_over_time [--copy-to DIR] [--top-n 8]
"""

from __future__ import annotations

import argparse
from collections import OrderedDict

import numpy as np
from matplotlib.ticker import LogLocator, NullFormatter

from scripts.paper_figures import common
from scripts.paper_figures.common import plt

STEM = "most_observed_over_time"

# Okabe & Ito (2008) colour-blind-safe order, minus yellow (too faint on
# white) and black (reserved for the record line), plus two muted extras.
TOP_COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#8C564B", "#6A5D9B"]
OTHER_COLOR = "#a8a8a8"
RECORD_COLOR = "#1a1a1a"
BREAK_YEAR = 1975.0

# Optional per-takeover nudges for the numbered markers, keyed by the
# takeover's index in the key (1-based): (dx, dy) in points.
MARKER_LABEL_OFFSETS: dict[int, tuple[float, float]] = {
    1: (3.0, 2.5),
    10: (-3.0, -1.0),
    11: (-3.0, -1.0),
}


def load_record(args, con):
    """Build the per-star cumulative table and return the record per period.

    Returns a list of (t_end, record_n, holder_star_id), t_end being the end
    of each half-year (1893.5 = end of 1893 H1).
    """
    con.execute(f"""
        CREATE TEMP TABLE counts AS
        SELECT star_id,
               CAST(year(obs_date) + CASE WHEN month(obs_date) <= 6 THEN 0.5 ELSE 1.0 END AS DOUBLE) AS t_end,
               count(*) AS n
        FROM {common.parquet(args.data_source, 'spectroscopy_holdings')}
        WHERE {common.holdings_filter(args.matched_only)} AND obs_date IS NOT NULL
        GROUP BY ALL
    """)
    con.execute("""
        CREATE TEMP TABLE cum AS
        SELECT star_id, t_end, sum(n) OVER (PARTITION BY star_id ORDER BY t_end) AS cum_n
        FROM counts
    """)
    # A star's cumulative count never drops, so the all-star maximum at time
    # t is the running maximum of each period's own maximum -- no need to
    # carry every star forward through every period. Each period also
    # returns every star tied at its maximum, so a tie can't unseat the
    # incumbent.
    per_period = con.execute("""
        WITH m AS (SELECT t_end, max(cum_n) AS n FROM cum GROUP BY t_end)
        SELECT m.t_end, m.n, list(c.star_id ORDER BY c.star_id)
        FROM m JOIN cum c ON c.t_end = m.t_end AND c.cum_n = m.n
        GROUP BY m.t_end, m.n ORDER BY m.t_end
    """).fetchall()
    record = []
    best_n, holder = 0, None
    for t, n, tied in per_period:
        if n > best_n:
            best_n = n
            holder = holder if holder in tied else tied[0]
        record.append((t, best_n, holder))
    return record


def _period_name(t_end: float) -> str:
    yr = int(np.floor(t_end - 0.5))
    return f"{yr}"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_common_args(parser)
    parser.add_argument("--top-n", type=int, default=8, help="colour and label today's N most observed stars (max 8)")
    parser.add_argument("--start-year", type=float, default=1890)
    args = parser.parse_args(argv)
    args.top_n = min(args.top_n, len(TOP_COLORS))
    common.set_style()

    con = common.connect(args.data_source)
    record = load_record(args, con)
    t_rec = np.array([r[0] for r in record])
    n_rec = np.array([r[1] for r in record], dtype=float)
    s_rec = [r[2] for r in record]
    t_now = t_rec[-1]

    # Takeovers: (star_id, t_end of first period held, count then)
    takeovers = [(s_rec[i], t_rec[i], n_rec[i]) for i in range(len(s_rec)) if i == 0 or s_rec[i] != s_rec[i - 1]]
    leaders = list(OrderedDict.fromkeys(s for s, _, _ in takeovers))
    top_now = [sid for sid, _ in con.execute(f"""
        SELECT star_id, max(cum_n) AS n FROM cum GROUP BY star_id ORDER BY n DESC, star_id LIMIT {args.top_n}
    """).fetchall()]
    shown = list(OrderedDict.fromkeys(top_now + leaders))
    labels = common.star_labels(con, args.data_source, shown)
    color = {sid: TOP_COLORS[i] for i, sid in enumerate(top_now)}

    traj = {}
    for sid, t, c in con.execute(f"""
        SELECT star_id, t_end, cum_n FROM cum WHERE star_id IN ({', '.join(map(str, shown))}) ORDER BY star_id, t_end
    """).fetchall():
        traj.setdefault(sid, ([], []))
        traj[sid][0].append(t)
        traj[sid][1].append(float(c))

    print("record holders (takeover year, count at takeover):")
    for k, (sid, t, n) in enumerate(takeovers, 1):
        print(f"  {k:2d}  {_period_name(t)}  {labels[sid]:<22} {n:>9,.0f}")
    print(f"today's top {args.top_n}: " + ", ".join(f"{labels[s]} ({traj[s][1][-1]:,.0f})" for s in top_now))

    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(common.FULL_WIDTH_IN, 4.0), sharey=True,
                                   gridspec_kw={"width_ratios": [1.25, 3.6], "wspace": 0.04})

    for ax in (ax0, ax1):
        # Grey first, colours on top, today's #1 last.
        for sid in sorted(shown, key=lambda s: (s in color, -top_now.index(s) if s in color else 0)):
            t, c = traj[sid]
            ax.step(np.r_[t, t_now], np.r_[c, c[-1]], where="post", lw=1.0 if sid in color else 0.7,
                    color=color.get(sid, OTHER_COLOR), zorder=3 if sid in color else 2)
        ax.step(np.r_[t_rec, t_now + 0.5], np.r_[n_rec, n_rec[-1]], where="post", lw=3.2,
                color=RECORD_COLOR, alpha=0.35, solid_capstyle="butt", zorder=1)
        ax.grid(True, which="major", lw=0.3, color="0.88")
        ax.set_yscale("log")

    # Numbered takeover markers on whichever panel holds them.
    for k, (sid, t, n) in enumerate(takeovers, 1):
        ax = ax0 if t < BREAK_YEAR else ax1
        ax.plot(t - 0.5, n, "o", ms=3.2, mfc="white", mec=RECORD_COLOR, mew=0.8, zorder=5)
        dx, dy = MARKER_LABEL_OFFSETS.get(k, (-2.5, 2.5))
        ax.annotate(str(k), xy=(t - 0.5, n), xytext=(dx, dy), textcoords="offset points",
                    ha="right" if dx <= 0 else "left", va="bottom" if dy >= 0 else "top",
                    fontsize=6.5, fontweight="bold", color=RECORD_COLOR, zorder=6)

    # Key for the takeovers, in the empty upper part of the early panel.
    key = "\n".join(f"{k:>2}  {_period_name(t)}  {labels[sid]}" for k, (sid, t, _) in enumerate(takeovers, 1))
    ax0.text(0.04, 0.985, "Record holder\n" + key, transform=ax0.transAxes, va="top", ha="left",
             fontsize=6.2, linespacing=1.25, color="0.15", zorder=6,
             bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="0.8", lw=0.5))

    # Today's top N, ranked, in the empty upper left of the main panel.
    handles = [plt.Line2D([], [], color=color[sid], lw=1.4) for sid in top_now]
    names = [f"{labels[sid]} ({traj[sid][1][-1]:,.0f})" for sid in top_now]
    ax1.legend(handles, names, loc="upper left", bbox_to_anchor=(0.0, 1.0), frameon=True, framealpha=1,
               edgecolor="0.8", fontsize=6.5, handlelength=1.4, borderaxespad=0.3, labelspacing=0.25,
               ncols=2, columnspacing=1.0, title="Most observed today", title_fontsize=6.5).set_zorder(6)
    ax1.set_ylim(0.7, n_rec[-1] * 40)  # headroom for the legend above every curve

    ax0.set_xlim(args.start_year, BREAK_YEAR)
    ax1.set_xlim(BREAK_YEAR, t_now + 1)
    ax0.set_xticks(np.arange(1900, BREAK_YEAR, 25))
    ax1.set_xticks(np.arange(1980, t_now + 1, 10))
    ax0.yaxis.set_minor_locator(LogLocator(subs=range(2, 10)))
    ax0.yaxis.set_minor_formatter(NullFormatter())
    ax0.set_ylabel("Cumulative dated spectra")
    fig.supxlabel("Year", y=0.0, fontsize=9)

    # Broken-axis styling.
    ax0.spines["right"].set_visible(False)
    ax1.spines["left"].set_visible(False)
    ax1.tick_params(axis="y", which="both", left=False)
    for ax in (ax0, ax1):
        ax.spines["top"].set_visible(False)
    ax1.spines["right"].set_visible(False)
    d = dict(marker=[(-1, -1.5), (1, 1.5)], markersize=6, linestyle="none", color="k", mec="k", mew=0.8, clip_on=False)
    ax0.plot([1], [0], transform=ax0.transAxes, **d)
    ax1.plot([0], [0], transform=ax1.transAxes, **d)

    common.save_figure(fig, STEM, args)


if __name__ == "__main__":
    main()
