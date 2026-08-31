# The Spectra Pointer

A cross-match database and search tool that unifies stellar spectroscopy
holdings across 58 independent astronomical archives (ESO, SDSS,
Gaia, LAMOST, Keck/KOA, Gemini, and more) behind a single [Gaia DR3](https://www.cosmos.esa.int/web/gaia/dr3) `source_id`
lookup.

## Statement of need

Ground- and space-based spectroscopic surveys and archives each publish
their own holdings under their own conventions — different identifier
schemes, different metadata fields, different levels of positional
precision, some with public TAP/VO services and some without. As more 
spectroscopic data is observed, it becomes intractable for an
individual researcher to find out if a particular star has been observed.
This information is necessary for creating novel research, combing
through archival data, and writing proposals for time on large 
telescopes with limited budgets.

The Spectra Pointer solves this by running an independent sync process per
archive (58 are currently implemented) that discovers spectroscopic
observations and cross-matches each one to a canonical Gaia DR3 `source_id` 
— first by a named identifier when the archive publishes one (via
the [SIMBAD database](https://simbad.cds.unistra.fr/simbad/), falling back
to positional matching (via a [PostgreSQL q3c extension](https://github.com/segasai/q3c)) 
when it doesn't. The result is a single Postgres table (`spectroscopy_holdings`) that a
researcher can query by Gaia `source_id` or common name to get a
consolidated, deduplicated list of every archive holding a spectrum of
that star, along with pointers back to the original data in each home
archive. A public search webapp (built on this database) is deployed at
<https://spectra-pointer-997472993697.us-central1.run.app>. This is 
software is developed to be reusable infrastructure for anybody,
using no proprietary access and minimal personal keys.

## Architecture

```
sync/main.py            per-archive fetch → cross-match (sync/matcher.py) → Postgres
ingest/add_star.py      registers a star (Gaia source_id) to be tracked
db/schema.sql           Postgres schema (stars, spectroscopy_holdings, q3c indexing)
scripts/export_to_parquet.py   snapshots Postgres to a static Parquet dataset
webapp/app.py           read-only search UI, queries the Parquet snapshot via DuckDB
```

Purposefully, the sync layer never talks to the public web tier directly: `sync/main.py`
and `ingest/add_star.py` write to Postgres, `scripts/export_to_parquet.py`
periodically snapshots that database to Parquet, and `webapp/app.py` serves
search results from the snapshot (locally or over HTTP), so the public
webapp never holds live database credentials. This architecture synchronizes
well with Georgia State University's network, which has much storage
but cannot handle many requests. The Parquet snapshots also allow users
to effectively download the database directly, should they want to run
more complicated queries.

## Installation

Requires Python 3.9+ and PostgreSQL with the
[q3c](https://github.com/segasai/q3c) extension (used for indexed radial
positional cross-matching; not packaged for conda-forge or Homebrew as of
this writing, must be built from source).

```bash
git clone https://github.com/zachway/spectra_pointer.git
cd spectra_pointer
pip install -r requirements.txt

# create the database and schema
createdb spectra
psql spectra -f db/schema.sql

export DATABASE_URL=postgresql:///spectra

# seed the ~70 naked-eye stars Gaia itself can't see (it saturates around
# G~3) -- a few seconds, no external data to fetch beyond a live SIMBAD/Gaia
# cross-match already baked into the script
python3 -m scripts.seed_bsc5_bright_stars

# mirror the Gaia DR3 astrometry sync.positional_fallback's
# shitty_positional_match needs (source_id, ra, dec, pmra, pmdec,
# phot_g_mean_mag) into a local table, so it queries local disk instead of
# Gaia's own (rate-limited) TAP+ service -- see
# db/migrations/0011_gaia_source_lite_mirror.sql for why. This is a genuine
# one-time bulk load: ~757GB streamed (never written to disk as such -- only
# the ~124GB pruned local table is), on the order of a day even rate-limited
# at the script's default 10MB/s. Safe to interrupt and rerun -- it resumes
# from the last completed file instead of starting over.
python3 -m scripts.load_gaia_source_lite

# without the mirror above, shitty_positional_match still runs and still
# finds matches among already-tracked stars (BSC5 seed stars, and any Gaia
# stars already in `stars` via ingest.add_star/sync.matcher), but every
# not-yet-tracked Gaia source is invisible to it -- an unpopulated
# gaia_source_lite_mirror answers every query with zero rows, not an error,
# so this fails silently rather than loudly.
```

## Usage

Register a star to track, by Gaia DR3 `source_id` or by a name resolvable
via SIMBAD:

```bash
python -m ingest.add_star 2200433413577635456
python -m ingest.add_star "Proxima Cen"
```

Run every implemented archive sync to convergence (or a subset):

```bash
python -m sync.main                        # all implemented archives
python -m sync.main --only rave galah       # just these
```

Export the current database to a Parquet snapshot and run the search
webapp against it locally:

```bash
python3 -m scripts.export_to_parquet --out-dir ./data
SPECTRA_DATA_DIR=./data python3 -m webapp.app
```

See the module docstrings in `sync/main.py`, `ingest/add_star.py`, and
`webapp/app.py` for the full set of options and environment variables
(archive-specific auth, caching, and deployment configuration).

### Example: what a lookup actually returns

Querying the live deployment for Proxima Centauri (`?format=csv` on any
name/`source_id` search returns a CSV instead of the HTML page):

```bash
curl "https://spectra-pointer-997472993697.us-central1.run.app/?q=Proxima+Cen&format=csv"
```

```csv
query,source_id,status,known_as,archive,instrument,obs_date,match_status,match_method,reduction_status,archive_url
Proxima Cen,5853498713190525696,tracked,"Proxima Centauri, GJ 551, V645 Cen, ...",Chandra X-ray Observatory,HETG (ACIS-S),2001-09-13,matched,name_resolved,unknown,https://cda.harvard.edu/chaser/startViewer.do?menuItem=details&obsid=2388
Proxima Cen,5853498713190525696,tracked,"Proxima Centauri, GJ 551, V645 Cen, ...",ESO Archive (Raw),CRIRES,2009-02-16,matched,name_resolved,raw,https://archive.eso.org/dataset/CRIRE.2009-02-16T08:13:50.905
Proxima Cen,5853498713190525696,tracked,"Proxima Centauri, GJ 551, V645 Cen, ...",ESO Science Archive,CRIRES,2024-07-08,matched,name_resolved,reduced,https://archive.eso.org/...
Proxima Cen,5853498713190525696,tracked,"Proxima Centauri, GJ 551, V645 Cen, ...",Gemini Observatory Archive,GMOS-S,2011-05-07,matched,name_resolved,raw,https://archive.gemini.edu/...
```

(truncated here — the real response has one row per observation, across
every archive holding a spectrum of the star; `known_as` is abbreviated
above, the real field carries every resolved SIMBAD alias). Each row is one
independently-discovered observation, cross-matched by name or position to
the same Gaia `source_id` — `archive_url` links straight back to that
observation in its home archive. The same query without `format=csv`
returns the interactive search page at the same URL.

## Testing

Most tests exercise real cross-match logic against Postgres, so they need
a test database with the schema loaded (same q3c prerequisite as
"Installation" above):

```bash
createdb spectra_test
psql spectra_test -f db/schema.sql

pip install pytest
DATABASE_URL=postgresql:///spectra_test pytest tests/
```

`DATABASE_URL` defaults to `postgresql:///spectra_test` if unset. See
`.github/workflows/tests.yml` for the exact CI setup, including building
q3c from source on a clean Ubuntu runner.

## Deployment

The webapp is deployed on Google Cloud Run from the root `Dockerfile`:

```bash
gcloud run deploy spectra-pointer --source . --region us-central1 --allow-unauthenticated
```

## Contributing

Bug reports and pull requests are welcome via
[GitHub Issues](https://github.com/zachway/spectra_pointer/issues) and
[Pull Requests](https://github.com/zachway/spectra_pointer/pulls). If
you're adding support for a new archive, take a look at an existing
implementation under `sync/archives/` as a template — each one implements
a `fetch()` generator that the shared matcher and runner in `sync/` drive
to convergence.

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE)
for details.
