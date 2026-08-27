import os

import psycopg
import pytest

# Synthetic Gaia source_ids used across matcher tests, kept in a dedicated
# range so cleanup can't accidentally touch real data.
TEST_ID_LOW = 900000000000000000
TEST_ID_HIGH = 900000000000999999


@pytest.fixture
def conn():
    database_url = os.environ.get("DATABASE_URL", "postgresql:///spectra_test")
    connection = psycopg.connect(database_url)
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO archives (archive_code, display_name) "
            "VALUES ('unit_test', 'Unit Test Archive') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO archives (archive_code, display_name) "
            "VALUES ('eso_raw', 'ESO Archive (Raw)') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            # 'noirlab'/'eso'/'eso_raw' alongside 'unit_test': instrument-
            # radius-override and eso/eso_raw reconciliation tests must use
            # those exact archive_codes (both are keyed by real archive_code,
            # not a stand-in), so their test rows need the same cleanup --
            # otherwise a leftover row referencing a test-range star_id
            # blocks the DELETE FROM stars below with a foreign key
            # violation on the next test run.
            "DELETE FROM spectroscopy_holdings WHERE archive_code IN ('unit_test', 'noirlab', 'eso', 'eso_raw')"
        )
        cur.execute(
            "DELETE FROM stars WHERE gaia_source_id BETWEEN %s AND %s",
            (TEST_ID_LOW, TEST_ID_HIGH),
        )
    connection.commit()
    yield connection
    connection.close()


# webapp.app never talks to Postgres directly (see its module docstring) --
# every route reads a DuckDB-over-Parquet snapshot built by
# scripts.export_to_parquet. Rather than hand-roll 21 Parquet schemas (a
# maintenance trap the moment a real column gets added/renamed), this fixture
# inserts a handful of synthetic rows into the same local Postgres test DB
# the other fixtures use, then runs the *real* export_tables() against it --
# same derived-table SQL, same column shapes as production, just over a tiny
# dataset. Session-scoped: the export itself takes real wall-clock time and
# every route test can safely share one immutable snapshot.
WEBAPP_TEST_STAR_1 = TEST_ID_LOW + 1
WEBAPP_TEST_STAR_2 = TEST_ID_LOW + 2
WEBAPP_TEST_ARCHIVE_CODE = "webapp_test"


@pytest.fixture(scope="session")
def spectra_data_dir(tmp_path_factory):
    database_url = os.environ.get("DATABASE_URL", "postgresql:///spectra_test")
    connection = psycopg.connect(database_url)
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO archives (archive_code, display_name, access_mechanism) "
            "VALUES (%s, 'Webapp Test Archive', 'rest_json') ON CONFLICT DO NOTHING",
            (WEBAPP_TEST_ARCHIVE_CODE,),
        )
        cur.execute("DELETE FROM spectroscopy_holdings WHERE archive_code = %s", (WEBAPP_TEST_ARCHIVE_CODE,))
        # Also clears any holdings left over (under any archive_code) from an
        # unrelated prior run against this same local test DB that happen to
        # reference a star in the shared test-id range -- otherwise the
        # DELETE FROM stars below can fail with a foreign key violation on a
        # star this fixture didn't itself create.
        cur.execute(
            "DELETE FROM spectroscopy_holdings WHERE star_id IN "
            "(SELECT star_id FROM stars WHERE gaia_source_id BETWEEN %s AND %s)",
            (TEST_ID_LOW, TEST_ID_HIGH),
        )
        cur.execute("DELETE FROM stars WHERE gaia_source_id BETWEEN %s AND %s", (TEST_ID_LOW, TEST_ID_HIGH))
        cur.execute(
            "INSERT INTO stars (gaia_source_id, ra, dec, phot_g_mean_mag, phot_bp_mean_mag, phot_rp_mean_mag, "
            "parallax, input_name, name_aliases) VALUES "
            "(%s, 10.68458, 41.26906, 8.5, 9.0, 8.0, 5.0, 'TEST STAR ONE', ARRAY['TEST STAR ONE', 'HD 999001'])",
            (WEBAPP_TEST_STAR_1,),
        )
        cur.execute(
            "INSERT INTO stars (gaia_source_id, ra, dec, phot_g_mean_mag, parallax, input_name, name_aliases) "
            "VALUES (%s, 83.82208, -5.39111, 6.2, 12.0, 'TEST STAR TWO', ARRAY['TEST STAR TWO'])",
            (WEBAPP_TEST_STAR_2,),
        )
        cur.execute("SELECT star_id FROM stars WHERE gaia_source_id = %s", (WEBAPP_TEST_STAR_1,))
        star1_id = cur.fetchone()[0]
        # raw_ra/raw_dec set (matching star1's own position) -- export_tables'
        # instrument-HEALPix export (_export_instrument_healpix) filters to
        # rows with a real raw position and crashes on an empty result
        # (numpy .fetchnumpy() with no rows), so at least one archive's test
        # holdings need this populated for the export to succeed at all.
        cur.execute(
            "INSERT INTO spectroscopy_holdings "
            "(star_id, archive_code, archive_obs_id, archive_url, instrument, obs_date, "
            " match_method, match_status, reduction_status, raw_ra, raw_dec) VALUES "
            "(%s, %s, 'test-obs-1', 'https://example.invalid/obs/1', 'TESTSPEC', '2024-01-01', "
            " 'manual', 'matched', 'reduced', 10.68458, 41.26906), "
            "(%s, %s, 'test-obs-2', 'https://example.invalid/obs/2', 'TESTSPEC', '2024-02-01', "
            " 'manual', 'matched', 'reduced', 10.68458, 41.26906)",
            (star1_id, WEBAPP_TEST_ARCHIVE_CODE, star1_id, WEBAPP_TEST_ARCHIVE_CODE),
        )
    connection.commit()

    try:
        out_dir = tmp_path_factory.mktemp("spectra_data")
        from scripts.export_to_parquet import export_tables
        export_tables(database_url, str(out_dir))
        yield str(out_dir)
    finally:
        with connection.cursor() as cur:
            cur.execute("DELETE FROM spectroscopy_holdings WHERE archive_code = %s", (WEBAPP_TEST_ARCHIVE_CODE,))
            cur.execute("DELETE FROM stars WHERE gaia_source_id BETWEEN %s AND %s", (TEST_ID_LOW, TEST_ID_HIGH))
            cur.execute("DELETE FROM archives WHERE archive_code = %s", (WEBAPP_TEST_ARCHIVE_CODE,))
        connection.commit()
        connection.close()


@pytest.fixture(scope="session")
def webapp_module(spectra_data_dir):
    # SPECTRA_DATA_DIR must be set before webapp.app's first import -- it
    # resolves the data source and opens the DuckDB/Parquet connection as a
    # module-level side effect (see webapp/app.py's `_con = _make_connection()`).
    os.environ["SPECTRA_DATA_DIR"] = spectra_data_dir
    from webapp import app as webapp_app
    return webapp_app


@pytest.fixture
def client(webapp_module):
    webapp_module.app.config["TESTING"] = True
    return webapp_module.app.test_client()
