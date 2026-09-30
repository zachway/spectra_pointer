import csv
import io
import re
from pathlib import Path

from tests.conftest import WEBAPP_TEST_STAR_1

REPO_ROOT = Path(__file__).resolve().parent.parent


def _seeded_archive_codes() -> set[str]:
    """Every archive_code inserted into `archives` by db/schema.sql or a
    db/migrations/*.sql file."""
    codes = set()
    for path in [REPO_ROOT / "db" / "schema.sql", *sorted((REPO_ROOT / "db" / "migrations").glob("*.sql"))]:
        text = path.read_text()
        for m in re.finditer(r"INSERT INTO archives\b.*?VALUES(.*?);\s*$", text, re.S | re.M):
            codes.update(re.findall(r"^\s*\('([a-z0-9_]+)',", m.group(1), re.M))
    return codes


def test_every_archive_has_a_direct_download_verdict(webapp_module):
    seeded = _seeded_archive_codes()
    assert len(seeded) >= 61
    assert seeded == set(webapp_module.ARCHIVE_URL_IS_DIRECT_DOWNLOAD)


def test_direct_download_per_url_exceptions(webapp_module):
    f = webapp_module.archive_url_is_direct_download
    assert f("irsa_missions", "https://irsa.ipac.caltech.edu/data/IRAS/LRS/spectra/08518-1249.sp.tbl") is True
    assert f("irsa_missions", "https://irsa.ipac.caltech.edu/data/SWS/spectra/sws/37401910_sws.tbl") is False
    assert f("sdss_legacy_optical", "https://data.sdss.org/sas/dr20/spectro/sdss/redux/v5_13_2/spectra/lite/5357/spec-5357-55956-0457.fits") is True
    assert f("sdss_legacy_optical", "https://skyserver.sdss.org/public/VisualTools/explore/summary?sId=1") is False
    # eso/eso_raw/dao/xmm switched to file URLs on 2026-09-30; rows still
    # carrying the old landing/resolver shape (pre-backfill, or an older
    # export) must keep saying "no".
    assert f("eso", "https://archive.eso.org/dataset/ADP.2014-10-02T10:01:18.603") is False
    assert f("eso", "https://dataportal.eso.org/dataportal_new/file/ADP.2014-10-02T10:01:18.603") is True
    assert f("eso_raw", "https://dataportal.eso.org/dataportal_new/file/HARPS.2018-02-08T00:14:50.053") is True
    assert f("dao", "https://ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/caom2ops/datalink?ID=ivo%3A%2F%2Fcadc.nrc.ca%2FDAO%3Fx%2Fx") is False
    assert f("dao", "https://ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/raven/files/cadc:DAO/dao_c122_2013_018818.fits") is True
    assert f("xmm", "https://nxsa.esac.esa.int/nxsa-web/#obsid=0107860101") is False
    assert f("xmm", "https://nxsa.esac.esa.int/nxsa-sl/servlet/data-action-aio?obsno=0107860101&name=SRSPEC") is True
    assert f("cfht_cadc", "https://ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/caom2ops/datalink?ID=x") is False
    # Gemini's archive needs a session cookie, so file-shaped links still aren't direct.
    assert f("gemini_ghost", "https://archive.gemini.edu/file/S20240504S0228_red001_calibrated.fits.bz2") is False
    assert f("gemini_igrins", "https://archive.gemini.edu/file/SDCH_20221018_0046.spec_a0v.fits.bz2") is False
    assert f("desi", None) is None
    assert f("weave", "https://example.org/x") is None
    assert f("not_a_real_archive", "https://example.org/x") is None


def test_direct_download_label(webapp_module):
    label = webapp_module._direct_download_label
    assert label("lamost", "https://www.lamost.org/dr11/v2.0/spectrum/fits/526703135") == "yes"
    assert label("chandra", "https://cda.harvard.edu/chaser/startViewer.do?menuItem=details&obsid=29801") == "no"
    assert label("weave", "https://example.org/x") == ""


def _csv_header(body: str) -> list[str]:
    return next(csv.reader(io.StringIO(body)))


def test_star_csv_has_direct_download_column(client):
    body = client.get("/?q=TEST+STAR+ONE&format=csv").get_data(as_text=True)
    header = _csv_header(body)
    assert header.index("direct_download") == header.index("reduction_status") + 1


def test_batch_csv_has_direct_download_column(client):
    body = client.post("/batch", data={"names": f"{WEBAPP_TEST_STAR_1}\n", "format": "csv"}).get_data(as_text=True)
    assert "direct_download" in _csv_header(body)


def test_instrument_csv_direct_download_column_lines_up(client):
    body = client.get("/instrument_holdings.csv?archive_code=webapp_test").get_data(as_text=True)
    rows = list(csv.DictReader(io.StringIO(body)))
    assert rows
    for r in rows:
        # The test archive isn't a real archive_code, so its verdict is
        # unknown (blank) -- and archive_url/star_id stayed in their columns.
        assert r["direct_download"] == ""
        assert r["archive_url"].startswith("http")
        assert r["star_id"].isdigit()


def test_star_page_shows_direct_download_column(client):
    body = client.get("/?q=TEST+STAR+ONE").get_data(as_text=True)
    assert "<th>Reduction</th><th title=" in body
    assert ">Direct download</th>" in body
