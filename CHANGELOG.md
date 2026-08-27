# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.0.0] - <TODO: release date>

### Added

- Independent per-archive sync pipeline (`sync/`) covering dozens of
  spectroscopic archives (ESO, SDSS, Gaia, LAMOST, Keck/KOA, Gemini, MAST,
  Chandra/XMM, and more), each cross-matched to a canonical Gaia DR3
  `source_id` (or Bright Star Catalogue number for the small number of
  stars too bright for Gaia).
- Cross-match pipeline: SIMBAD name resolution first, falling back to a
  proper-motion-propagated 1″ positional match (via the Postgres q3c
  extension), then a lower-confidence 1′ brightness-disambiguated
  fallback.
- Postgres schema (`db/schema.sql`) with `stars` / `spectroscopy_holdings`
  / `archives`, and a per-archive incremental sync cursor.
- Public search webapp (`webapp/`): name / Gaia `source_id` / radial
  search, batch lookup, advanced filters, CSV export, a spectrum viewer,
  sky map / CMD / leaderboard / timeplots visualizations, per-instrument
  wavelength coverage, `/status` and `/stats` dashboards, and a
  crowd-sourced triage tool for unmatched holdings.
- Postgres-to-Parquet snapshot pipeline (`scripts/export_to_parquet.py`)
  the webapp reads from via DuckDB, so the public deployment never holds
  live database credentials.
- Continuum normalization module (`webapp/continuum.py`).
- Automated test suite (`tests/`) and CI (`.github/workflows/tests.yml`).
- Deployment to Google Cloud Run.

[Unreleased]: https://github.com/zachway/spectra_pointer/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/zachway/spectra_pointer/releases/tag/v1.0.0
