"""Regenerate every paper figure.

    python -m scripts.paper_figures.make_all --copy-to ~/Documents/The_Spectra_Pointer/figures

Takes the common options (see common.add_common_args); figure-specific ones
(--nside, --bins, --top-n, ...) need the individual scripts.
"""

from __future__ import annotations

import argparse
import sys

from scripts.paper_figures import cmd_distribution, common, most_observed_over_time, sky_distribution

FIGURES = (sky_distribution, cmd_distribution, most_observed_over_time)


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_common_args(parser)
    parser.parse_args(argv)  # validate arguments / handle --help before running anything
    for i, module in enumerate(FIGURES):
        # The per-star cache only needs rebuilding once per run.
        module_argv = list(argv) if i == 0 else [a for a in argv if a != "--refresh"]
        print(f"== {module.__name__}")
        module.main(module_argv)


if __name__ == "__main__":
    main()
