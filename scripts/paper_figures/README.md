# Paper figures

Scripts that make the figures for The Spectra Pointer paper. They read the
public Parquet exports at <https://astro.gsu.edu/~way/spectra_data/> (the same
files the webapp serves), so anyone can rebuild them without database access.

```sh
# all figures, written to paper/figures/ and copied into the paper
python -m scripts.paper_figures.make_all --copy-to ~/Documents/The_Spectra_Pointer/figures

# one figure, with its own options
python -m scripts.paper_figures.sky_distribution --nside 128
python -m scripts.paper_figures.most_observed_over_time --top-n 6
```

| Script | Output | What it shows |
|---|---|---|
| `sky_distribution.py` | `sky_distribution.png` | Left: every matched holding, binned at its star's position (HEALPix, spectra deg⁻²). Right: stars with spectra in more than one archive (stars deg⁻²). ICRS Mollweide, RA increasing left, Galactic plane dashed. |
| `cmd_distribution.py` | `cmd_distribution.png` | The same two panels on the Gaia CMD (BP−RP vs. M_G). |
| `most_observed_over_time.py` | `most_observed_over_time.png` | Cumulative dated spectra over time: the all-time record as a grey band, numbered at each change of record holder, plus today's top 8 in colour. |

## Choices worth knowing when writing captions

- **"Matched" holdings** include tentative `shitty_positional_match`
  (`needs_review`) assignments, the same as the webapp Leaderboard. Pass
  `--matched-only` to keep only `match_status = 'matched'`.
- **"More than one archive"** counts archive *families*: codes that are one
  archive split by instrument or access route (`eso`/`eso_raw`,
  `lco_floyds`/`lco_nres`, the SDSS codes, ...) count once. The mapping is
  `ARCHIVE_FAMILY` in `common.py`.
- **CMD**: `M_G = G + 5 log10(parallax/mas) − 10`, no extinction correction
  and no parallax-quality cut (the export has no `parallax_error`). Stars
  with parallax ≤ 0 or no BP/RP are dropped; the script prints how many.
- **Record over time** uses only holdings with an `obs_date` (DESI and
  SDSS-V APOGEE have none), so its totals can be below the undated all-time
  counts. A star takes the record only by strictly passing the holder; ties
  keep the incumbent.
- **Colour scales** are logarithmic and top out at the 99.9th percentile of
  non-empty bins (the colour bar's arrow), so a few extremely well-observed
  fields don't wash out the rest. Grey sky pixels have no data.

## Editing

- Star names come from `common.star_label`: SIMBAD `NAME` > Bayer (Greek)
  > Flamsteed > variable-star name > exoplanet-survey host (WASP-33, ...) >
  HD/GJ/HIP/... Override any star with `common.LABEL_OVERRIDES` (keyed by
  `star_id`).
- Overlapping record-holder numbers: nudge them with `MARKER_LABEL_OFFSETS` in
  `most_observed_over_time.py`.
- Size and fonts: `common.FULL_WIDTH_IN` (AASTeX `figure*` width) and
  `common.set_style`.

## Caching

The sky and CMD figures share one per-star table (holdings count, distinct
archive families, position, photometry), built once from the exports and
cached at `paper/.cache/per_star_{all,matched_only}.parquet` (~550 MB,
git-ignored). Pass `--refresh` after a new weekly export. The over-time
figure queries the exports directly (~20 s).
