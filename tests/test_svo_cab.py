import numpy as np

from sync.archives import svo_cab

GAUDI_SAMPLE = """<VOTABLE><RESOURCE><TABLE><DATA><TABLEDATA>
<TR>
 <TD>186</TD>
 <TD>SSAP for
observation 186</TD>  <TD><![CDATA[http://sdc.cab.inta-csic.es:80/gaudivo/servlet/Fetch?id=186&type=full]]></TD>
 <TD>fits</TD>
 <TD>HD164115</TD>
 <TD>Elodie</TD>
<TD>269.863 6.41895</TD>
<TD></TD><TD></TD><TD></TD>
<TD>2000-06-19</TD>
<TD>22:56:31</TD>
<TD>1502.71</TD></TR>  <TR>
 <TD>19</TD><TD>t</TD><TD><![CDATA[http://sdc.cab.inta-csic.es:80/gaudivo/servlet/Fetch?id=19&type=full]]></TD>
 <TD>fits</TD><TD>HD43777</TD><TD>SARG</TD><TD></TD><TD></TD><TD></TD><TD></TD><TD>not a date</TD><TD></TD><TD></TD></TR>
<TR><TD>20</TD><TD>t</TD><TD>u</TD><TD>votable</TD><TD>HD1</TD><TD>SARG</TD><TD>1 2</TD><TD></TD><TD></TD><TD></TD><TD>2003-02-10</TD><TD></TD><TD></TD></TR>
</TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>"""


def test_parse_gaudi_reads_rows_astropy_cannot():
    recs = svo_cab._parse_gaudi(GAUDI_SAMPLE)
    assert [r.archive_obs_id for r in recs] == ["gaudi:186", "gaudi:19"]  # the non-fits row is dropped
    first = recs[0]
    assert first.archive_url == "http://sdc.cab.inta-csic.es:80/gaudivo/servlet/Fetch?id=186&type=full"
    assert (first.raw_target_name, first.instrument) == ("HD164115", "GAUDI (Elodie)")
    assert (first.ra, first.dec, first.obs_date.isoformat()) == (269.863, 6.41895, "2000-06-19")
    # Unreadable position/date become missing values, not a crash.
    assert (recs[1].ra, recs[1].dec, recs[1].obs_date) == (None, None, None)


def _rows(*rows):
    return [dict(r) for r in rows]


def test_collection_records_format_dedup_placeholder_position_and_text_date(monkeypatch):
    coll = {"path": "v2/spex", "instrument": "SpeX Prism Library", "name_field": "name", "format": "text/plain",
            "text_date_field": "dateobs"}
    url = "http://svocats.cab.inta-csic.es/spex/ssap.php?ID=A&label=spec_txt"
    rows = _rows(
        {"SpecFmt": "application/x-votable+xml", "SpecURL": "vot", "AssocID": "assoc_A", "name": "A", "TargetPos": [1.0, 2.0], "dateobs": "2007 Oct 12"},
        {"SpecFmt": "text/plain", "SpecURL": url, "AssocID": "assoc_A", "name": "A", "TargetPos": [1.0, 2.0], "dateobs": "2007  Oct 12"},
        # A second epoch of the same star points at the same file: kept once.
        {"SpecFmt": "text/plain", "SpecURL": url, "AssocID": "assoc_A", "name": "A", "TargetPos": [1.0, 2.0], "dateobs": "2008 Jul 13"},
        {"SpecFmt": "text/plain", "SpecURL": "b", "AssocID": "assoc_B", "name": "B_1", "TargetPos": [0.0, 0.0], "dateobs": "unknown"},
    )
    monkeypatch.setattr(svo_cab, "_fetch_collection_rows", lambda path: rows)
    recs = svo_cab._collection_records(coll)
    assert [r.archive_obs_id for r in recs] == ["v2/spex:assoc_A", "v2/spex:assoc_B"]
    assert recs[0].obs_date.isoformat() == "2007-10-12" and recs[0].archive_url == url
    assert (recs[1].ra, recs[1].dec, recs[1].obs_date, recs[1].raw_target_name) == (None, None, None, "B 1")


def test_name_falls_back_across_fields_and_skips_masked():
    row = {"sbname": np.ma.masked, "source_name": " ", "cps_name": "222038"}
    assert svo_cab._name(row, ("sbname", "source_name", "cps_name")) == "222038"
    assert svo_cab._name({"sbname": "V* BR Psc"}, ("sbname",)) == "V* BR Psc"


def test_legacy_cursor_pulls_only_collections_added_since(monkeypatch):
    pulled = []
    monkeypatch.setattr(svo_cab, "_collection_records", lambda coll: pulled.append(coll["path"]) or [coll["path"]])
    monkeypatch.setattr(svo_cab, "_fetch_gaudi", lambda: pulled.append("gaudi") or ["gaudi"])

    recs, cursor = svo_cab.fetch({"synced_at": "2026-08-07T00:00:00", "row_count": 3090})
    assert pulled == ["v2/spex", "v2/yee2017", "v2/chiu06", "v2/bdsslow", "v2/uves", "gaudi"]
    assert len(recs) == 6 and cursor["row_count"] == 6
    assert cursor["collections_done"] == sorted([c["path"] for c in svo_cab.COLLECTIONS] + ["gaudi"])

    # Fully pulled: a no-op that leaves the cursor untouched.
    pulled.clear()
    assert svo_cab.fetch(cursor) == ([], cursor)
    assert pulled == []

    # A fresh database pulls everything.
    svo_cab.fetch({})
    assert len(pulled) == len(svo_cab.COLLECTIONS) + 1
