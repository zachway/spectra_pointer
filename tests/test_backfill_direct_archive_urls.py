from sync.archives import dao, eso, eso_raw, irsa_missions, xmm
from scripts import backfill_direct_archive_urls as backfill

# Every expected URL below was fetched live on 2026-09-30 and returned the
# actual file (FITS, .Z FITS, or a tar of FITS).


def test_eso_and_eso_raw_file_urls():
    assert eso.FILE_URL.format(dp_id="ADP.2019-01-21T02:20:45.461") == \
        "https://dataportal.eso.org/dataportal_new/file/ADP.2019-01-21T02:20:45.461"
    assert eso_raw.FILE_URL == eso.FILE_URL


def test_dao_file_url():
    assert dao.file_url("ivo://cadc.nrc.ca/DAO?dao_c122_2013_018818/dao_c122_2013_018818") == \
        "https://ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/raven/files/cadc:DAO/dao_c122_2013_018818.fits"


def test_xmm_file_url_scheduled_and_unscheduled():
    assert xmm.file_url("0107860101", "RGS2", "S005") == (
        "https://nxsa.esac.esa.int/nxsa-sl/servlet/data-action-aio?obsno=0107860101"
        "&name=SRSPEC&level=PPS&extension=FTZ&instname=R2&expflag=S&expno=005"
    )
    assert xmm.file_url("0942540501", "RGS1", "U002").endswith("&instname=R1&expflag=U&expno=002")


def test_irsa_direct_url_only_rewrites_stale_sws_paths():
    assert irsa_missions.direct_url("https://irsa.ipac.caltech.edu/data/SWS/spectra/sws/28501652_sws.tbl") == \
        "https://irsa.ipac.caltech.edu/data/ISO/SWS/spectra/sws/28501652_sws.tbl"
    lrs = "https://irsa.ipac.caltech.edu/data/IRAS/LRS/spectra/08518-1249.sp.tbl"
    assert irsa_missions.direct_url(lrs) == lrs


def test_sdss_specobjid_decodes_to_sas_url():
    assert backfill.sdss_specobjid_to_url(7841030947256686592) == \
        "https://data.sdss.org/sas/dr20/spectro/sdss/redux/v5_13_2/spectra/lite/6964/spec-6964-56748-0960.fits"
    assert backfill.sdss_specobjid_to_url(7685548178322774016) == \
        "https://data.sdss.org/sas/dr20/spectro/sdss/redux/v5_13_2/spectra/lite/6826/spec-6826-56449-0565.fits"


# (archive_code, archive_obs_id, old archive_url, expected new archive_url)
_ROWS = [
    ("eso", "ADP.2099-01-01T00:00:00.001",
     "https://archive.eso.org/dataset/ADP.2099-01-01T00:00:00.001",
     "https://dataportal.eso.org/dataportal_new/file/ADP.2099-01-01T00:00:00.001"),
    ("eso_raw", "HARPS.2099-01-01T00:00:00.001",
     "https://archive.eso.org/dataset/HARPS.2099-01-01T00:00:00.001",
     "https://dataportal.eso.org/dataportal_new/file/HARPS.2099-01-01T00:00:00.001"),
    ("dao", "ivo://cadc.nrc.ca/DAO?dao_c122_2099_000001/dao_c122_2099_000001",
     "https://ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/caom2ops/datalink?ID=ivo%3A%2F%2Fcadc.nrc.ca%2FDAO%3Fdao_c122_2099_000001%2Fdao_c122_2099_000001",
     "https://ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/raven/files/cadc:DAO/dao_c122_2099_000001.fits"),
    ("xmm", "0999999999_RGS1_U002",
     "https://nxsa.esac.esa.int/nxsa-web/#obsid=0999999999",
     xmm.file_url("0999999999", "RGS1", "U002")),
    ("irsa_missions", "https://irsa.ipac.caltech.edu/data/SWS/spectra/sws/99999999_sws.tbl",
     "https://irsa.ipac.caltech.edu/data/SWS/spectra/sws/99999999_sws.tbl",
     "https://irsa.ipac.caltech.edu/data/ISO/SWS/spectra/sws/99999999_sws.tbl"),
    ("sdss_legacy_optical", "7841030947256686592",
     "https://skyserver.sdss.org/public/VisualTools/explore/summary?sId=7841030947256686592",
     "https://data.sdss.org/sas/dr20/spectro/sdss/redux/v5_13_2/spectra/lite/6964/spec-6964-56748-0960.fits"),
]
# Already-direct row in a rewritten archive: must be left alone.
_UNTOUCHED = ("irsa_missions", "https://irsa.ipac.caltech.edu/data/IRAS/LRS/spectra/99999-9999.sp.tbl",
              "https://irsa.ipac.caltech.edu/data/IRAS/LRS/spectra/99999-9999.sp.tbl")


def _cleanup(cur):
    cur.execute(
        "DELETE FROM spectroscopy_holdings WHERE (archive_code, archive_obs_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
        [[r[0] for r in _ROWS] + [_UNTOUCHED[0]], [r[1] for r in _ROWS] + [_UNTOUCHED[1]]],
    )


def test_backfill_rewrites_old_urls_and_is_idempotent(conn, monkeypatch):
    import os
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL", "postgresql:///spectra_test"))
    with conn.cursor() as cur:
        _cleanup(cur)
        for code, obs_id, old_url, _ in _ROWS + [(*_UNTOUCHED, None)]:
            cur.execute(
                "INSERT INTO spectroscopy_holdings (archive_code, archive_obs_id, archive_url, match_method, match_status) "
                "VALUES (%s, %s, %s, 'positional_easy_match', 'skipped')",
                [code, obs_id, old_url],
            )
    conn.commit()
    try:
        backfill.main(["--dry-run"])
        with conn.cursor() as cur:
            cur.execute("SELECT archive_url FROM spectroscopy_holdings WHERE archive_obs_id = %s", [_ROWS[0][1]])
            assert cur.fetchone()[0] == _ROWS[0][2]  # dry run wrote nothing

        backfill.main([])
        with conn.cursor() as cur:
            for code, obs_id, _, expected in _ROWS:
                cur.execute("SELECT archive_url FROM spectroscopy_holdings WHERE archive_code = %s AND archive_obs_id = %s",
                            [code, obs_id])
                assert cur.fetchone()[0] == expected, code
            cur.execute("SELECT archive_url FROM spectroscopy_holdings WHERE archive_code = %s AND archive_obs_id = %s",
                        [_UNTOUCHED[0], _UNTOUCHED[1]])
            assert cur.fetchone()[0] == _UNTOUCHED[2]
        conn.commit()

        # Second run finds nothing left in the old shapes.
        with conn.cursor() as cur:
            for code, pattern, _ in backfill.REWRITES:
                cur.execute("SELECT count(*) FROM spectroscopy_holdings WHERE archive_code = %s AND archive_url LIKE %s",
                            [code, pattern])
                assert cur.fetchone()[0] == 0, code
    finally:
        with conn.cursor() as cur:
            _cleanup(cur)
        conn.commit()
