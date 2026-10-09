from datetime import date, timedelta

import astropy.units as u
import pytest
from astropy_healpix import lonlat_to_healpix

from sync.archives import _aat_common, aat, aat_2df

URL = "https://archives.datacentral.org.au/results/some-uuid"
NIGHT = date(2019, 3, 1)


def _gaia_id_at(ra, dec, low_bits=12345):
    """A source_id whose encoded HEALPix level-12 pixel is at (ra, dec)."""
    pixel = int(lonlat_to_healpix(ra * u.deg, dec * u.deg, 2**12, order="nested"))
    return (pixel << 35) | low_bits


def _frame(aat_id, obj="HD 10700", instrument="ucles", runcmd="RUN", **overrides):
    row = {
        "aat_id": aat_id,
        "instrument": instrument,
        "OBJECT": obj,
        "ra": 26.0177,
        "dec": -15.9375,
        "obs_date": "2019-03-01",
        "ProgramID": "A/2019A/08",
        "EXPOSED": 600.0,
        "fits_header": {"RUNCMD": runcmd},
        "fibre_table": [],
    }
    row.update(overrides)
    return row


def _fibre(index, name, ra=134.3319, dec=-78.9874):
    return {"index": index, "name": name, "ra": ra, "dec": dec}


# --- shared helpers -------------------------------------------------------


def test_exposures_merges_the_arms_of_one_run():
    rows = [_frame(20190301200018), _frame(20190301100018), _frame(20190301100019)]
    assert [r["aat_id"] for r in _aat_common.exposures(rows)] == [20190301100018, 20190301100019]


def test_exposures_keeps_same_run_number_apart_when_object_differs():
    rows = [_frame(20190301100018, obj="Field A"), _frame(20190301300018, obj="Field B")]
    assert len(_aat_common.exposures(rows)) == 2


@pytest.mark.parametrize("ra, dec", [(None, 1.0), ("x", 1.0), (float("nan"), 1.0), (10.0, 95.0), (400.0, 1.0)])
def test_clean_position_rejects_unusable_values(ra, dec):
    assert _aat_common.clean_position(ra, dec) == (None, None)


def test_gaia_source_id_accepted_only_where_the_id_itself_points():
    ra, dec = 153.4228, -45.9907
    source_id = _gaia_id_at(ra, dec)
    assert _aat_common.gaia_source_id_from_name(str(source_id), ra, dec) == source_id
    assert _aat_common.gaia_source_id_from_name(f" Gaia DR3 {source_id}".strip(), ra, dec) == source_id
    # Same integer on a fibre somewhere else on the sky: some other survey's
    # object ID, not a Gaia source_id.
    assert _aat_common.gaia_source_id_from_name(str(source_id), ra + 5.0, dec) is None
    assert _aat_common.gaia_source_id_from_name(str(source_id), None, None) is None
    assert _aat_common.gaia_source_id_from_name("star1234", ra, dec) is None


def test_gaia_source_id_tolerates_a_nearby_offset():
    ra, dec = 153.4228, -45.9907
    source_id = _gaia_id_at(ra, dec)
    assert _aat_common.gaia_source_id_from_name(str(source_id), ra, dec + 1 / 60) == source_id


# --- paging ---------------------------------------------------------------


def test_fetch_night_pages_until_count_and_dedupes(monkeypatch):
    calls = []
    pages = [
        {"uuid": "u1", "count": 5, "next": {"aat_id": 2}, "results": [{"aat_id": 1}, {"aat_id": 2}]},
        # Later pages aren't a fixed size, and `next` stays set on the last.
        {"uuid": "u1", "count": 5, "next": {"aat_id": 5}, "results": [{"aat_id": 2}, {"aat_id": 3}, {"aat_id": 4}, {"aat_id": 5}]},
    ]

    def fake_post(body):
        calls.append(body)
        return pages[len(calls) - 1]

    monkeypatch.setattr(_aat_common, "_post", fake_post)
    rows, uuid = _aat_common.fetch_night(NIGHT, {"obstype": "RUN"})

    assert [r["aat_id"] for r in rows] == [1, 2, 3, 4, 5]
    assert uuid == "u1"
    assert calls == [
        {"start_date": "2019-03-01", "end_date": "2019-03-01", "obstype": "RUN"},
        {"uuid": "u1", "next": 2, "previous": 2},
    ]


def test_fetch_night_stops_when_a_page_adds_nothing(monkeypatch):
    page = {"uuid": "u1", "count": 9, "next": {"aat_id": 1}, "results": [{"aat_id": 1}]}
    calls = []

    def fake_post(body):
        calls.append(body)
        return page

    monkeypatch.setattr(_aat_common, "_post", fake_post)
    rows, _ = _aat_common.fetch_night(NIGHT, {})
    assert [r["aat_id"] for r in rows] == [1]
    assert len(calls) == 2


@pytest.mark.parametrize("answer", [None, {"uuid": "u1", "count": 0, "next": None, "results": []}])
def test_fetch_night_empty(monkeypatch, answer):
    monkeypatch.setattr(_aat_common, "_post", lambda body: answer)
    assert _aat_common.fetch_night(NIGHT, {}) == ([], None)


# --- night walk -----------------------------------------------------------


def _one_record(rows, url, night):
    return aat.to_records(rows, url, night)


def test_fetch_steps_over_empty_nights_and_returns_one_night(monkeypatch):
    asked = []

    def fake_fetch_night(night, filters):
        asked.append(night)
        if night == date(2019, 3, 4):
            return [_frame(20190304100001)], "u4"
        return [], None

    monkeypatch.setattr(_aat_common, "fetch_night", fake_fetch_night)
    records, cursor = _aat_common.fetch({"next_date": "2019-03-01"}, [("1990-01-01", None, {})], _one_record)

    assert asked == [date(2019, 3, d) for d in (1, 2, 3, 4)]
    assert [r.archive_obs_id for r in records] == ["20190304100001"]
    assert records[0].archive_url.endswith("/results/u4")
    assert cursor == {"next_date": "2019-03-05"}


def test_fetch_starts_at_the_earliest_query_and_respects_query_ranges(monkeypatch):
    asked = []

    def fake_fetch_night(night, filters):
        asked.append((night, filters))
        return [_frame(19940301100001)], "u"

    monkeypatch.setattr(_aat_common, "fetch_night", fake_fetch_night)
    queries = [("1994-03-01", "1994-03-02", {"a": 1}), ("1994-03-01", None, {"b": 2}), ("2018-08-01", None, {"c": 3})]
    records, cursor = _aat_common.fetch({}, queries, _one_record)

    assert asked == [(date(1994, 3, 1), {"a": 1}), (date(1994, 3, 1), {"b": 2})]
    # The same exposure coming back from two overlapping queries is one record.
    assert len(records) == 1
    assert cursor == {"next_date": "1994-03-02"}

    asked.clear()
    _aat_common.fetch(cursor, queries, _one_record)
    assert asked == [(date(1994, 3, 2), {"b": 2})]


def test_fetch_stops_short_of_today(monkeypatch):
    def fail(night, filters):
        raise AssertionError("must not query a night inside the ingest lag")

    monkeypatch.setattr(_aat_common, "fetch_night", fail)
    start = (date.today() - timedelta(days=_aat_common.INGEST_LAG_DAYS - 1)).isoformat()
    assert _aat_common.fetch({"next_date": start}, [("1990-01-01", None, {})], _one_record) == ([], {"next_date": start})


def test_fetch_gives_up_after_max_empty_nights_but_keeps_its_place(monkeypatch):
    monkeypatch.setattr(_aat_common, "MAX_EMPTY_NIGHTS", 3)
    monkeypatch.setattr(_aat_common, "fetch_night", lambda night, filters: ([], None))
    records, cursor = _aat_common.fetch({"next_date": "2000-01-01"}, [("1990-01-01", None, {})], _one_record)
    assert records == []
    assert cursor == {"next_date": "2000-01-04"}


# --- several nights per query ---------------------------------------------


def test_fetch_window_asks_once_per_query_and_groups_rows_by_night(monkeypatch):
    asked = []

    def fake_fetch_range(first_night, stop, filters):
        asked.append((first_night, stop, filters))
        # Same run number and OBJECT on two nights: two exposures, not arms
        # of one, so they must not be merged.
        return [_frame(20190302100007), _frame(20190305100007), _frame(20190305200007)], "week"

    monkeypatch.setattr(_aat_common, "fetch_range", fake_fetch_range)
    queries = [("1990-01-01", "2019-03-04", {"a": 1}), ("2019-03-03", None, {"b": 2})]
    records, cursor = _aat_common.fetch({"next_date": "2019-03-01"}, queries, _one_record, window_nights=7)

    # Each query is asked only for the part of the window inside its range.
    assert asked == [
        (date(2019, 3, 1), date(2019, 3, 4), {"a": 1}),
        (date(2019, 3, 3), date(2019, 3, 8), {"b": 2}),
    ]
    assert sorted(r.archive_obs_id for r in records) == ["20190302100007", "20190305100007"]
    assert all(r.archive_url.endswith("/results/week") for r in records)
    assert cursor == {"next_date": "2019-03-08"}


def test_fetch_window_steps_over_empty_windows_and_stops_at_the_ingest_lag(monkeypatch):
    asked = []

    def fake_fetch_range(first_night, stop, filters):
        asked.append((first_night, stop))
        return [], None

    monkeypatch.setattr(_aat_common, "fetch_range", fake_fetch_range)
    last_night = date.today() - timedelta(days=_aat_common.INGEST_LAG_DAYS)
    start = last_night - timedelta(days=9)
    records, cursor = _aat_common.fetch({"next_date": start.isoformat()}, [("1990-01-01", None, {})], _one_record, window_nights=7)

    assert records == []
    # Second window is cut short so it never reaches inside the ingest lag.
    assert asked == [(start, start + timedelta(days=7)), (start + timedelta(days=7), last_night + timedelta(days=1))]
    assert cursor == {"next_date": (last_night + timedelta(days=1)).isoformat()}


def test_a_window_that_fails_is_split_down_to_single_nights(monkeypatch):
    asked = []

    def fake_fetch_range(first_night, stop, filters):
        asked.append(("range", first_night, stop))
        if (stop - first_night).days > 2:
            raise _aat_common.requests.Timeout("too big")
        return [_frame(int(first_night.strftime("%Y%m%d")) * 1_000_000 + 100001)], f"u{first_night.day}"

    def fake_fetch_night(night, filters):
        asked.append(("night", night))
        return [_frame(int(night.strftime("%Y%m%d")) * 1_000_000 + 100002)], f"n{night.day}"

    monkeypatch.setattr(_aat_common, "fetch_range", fake_fetch_range)
    monkeypatch.setattr(_aat_common, "fetch_night", fake_fetch_night)
    records, cursor = _aat_common.fetch({"next_date": "2019-03-01"}, [("1990-01-01", None, {})], _one_record, window_nights=5)

    assert asked == [
        ("range", date(2019, 3, 1), date(2019, 3, 6)),
        ("range", date(2019, 3, 1), date(2019, 3, 3)),
        ("range", date(2019, 3, 3), date(2019, 3, 6)),
        ("night", date(2019, 3, 3)),
        ("range", date(2019, 3, 4), date(2019, 3, 6)),
    ]
    assert len(records) == 3
    assert cursor == {"next_date": "2019-03-06"}


def test_only_aat_walks_several_nights_per_query(monkeypatch):
    seen = {}

    def fake_fetch(cursor, queries, to_records, window_nights=1):
        seen[to_records.__module__] = window_nights
        return [], cursor

    monkeypatch.setattr(_aat_common, "fetch", fake_fetch)
    aat.fetch({})
    aat_2df.fetch({})
    assert seen == {"sync.archives.aat": 7, "sync.archives.aat_2df": 1}


# --- back-off -------------------------------------------------------------


class _Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _aat_common.requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def backoff(monkeypatch):
    """Scripted answers for requests.post, with sleeps recorded, not slept."""
    state = {"answers": [], "sleeps": [], "posts": 0}

    def fake_post(url, **kwargs):
        state["posts"] += 1
        answer = state["answers"].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(_aat_common.requests, "post", fake_post)
    monkeypatch.setattr(_aat_common.time, "sleep", state["sleeps"].append)
    monkeypatch.setattr(_aat_common, "_pause_seconds", 0.0)
    return state


def test_post_backs_off_on_429_and_then_paces_every_request(backoff):
    ok = _Response(200, [{"uuid": "u", "count": 0, "results": []}])
    backoff["answers"] = [_Response(429), _Response(429, headers={"Retry-After": "45"}), ok, ok]

    assert _aat_common._post({})["uuid"] == "u"
    # First retry waits the schedule's first step; the pause before every
    # request starts at 1s and doubles; a Retry-After header wins.
    assert backoff["sleeps"] == [30, 1.0, 45.0, 2.0]

    backoff["sleeps"].clear()
    _aat_common._post({})
    assert backoff["sleeps"] == [2.0]


def test_post_retries_server_and_connection_errors_then_gives_up(backoff):
    ok = _Response(200, [{"uuid": "u", "count": 0, "results": []}])
    backoff["answers"] = [_Response(503), _aat_common.requests.ConnectionError("reset"), ok]
    assert _aat_common._post({})["uuid"] == "u"
    assert backoff["sleeps"] == [30, 60]

    backoff["sleeps"].clear()
    backoff["answers"] = [_Response(500)] * (len(_aat_common.RETRY_WAITS_SECONDS) + 1)
    with pytest.raises(_aat_common.requests.HTTPError):
        _aat_common._post({})
    assert backoff["sleeps"] == list(_aat_common.RETRY_WAITS_SECONDS)


def test_post_retries_a_timeout_once_and_does_not_retry_a_bad_request(backoff):
    backoff["answers"] = [_aat_common.requests.Timeout("slow"), _aat_common.requests.Timeout("slow")]
    with pytest.raises(_aat_common.requests.Timeout):
        _aat_common._post({})
    assert backoff["posts"] == 2

    backoff["posts"] = 0
    backoff["answers"] = [_Response(400)]
    with pytest.raises(_aat_common.requests.HTTPError):
        _aat_common._post({})
    assert backoff["posts"] == 1


# --- aat: single-object spectrographs -------------------------------------


def test_aat_record_fields():
    (record,) = aat.to_records([_frame(20190301100018)], URL, NIGHT)
    assert record.archive_obs_id == "20190301100018"
    assert record.archive_url == URL
    assert record.instrument == "UCLES"
    assert record.obs_date == NIGHT
    assert record.program_id == "A/2019A/08"
    assert record.raw_target_name == "HD 10700"
    assert (record.ra, record.dec) == (26.0177, -15.9375)
    assert record.gaia_source_id is None
    assert record.reduction_status == "raw"


def test_aat_skips_instruments_off_the_allowlist():
    rows = [_frame(1, instrument="AAOMEGA-2dF"), _frame(2, instrument="iris2i"), _frame(3, instrument="Veloce", runcmd="OBJECT")]
    assert [r.instrument for r in aat.to_records(rows, URL, NIGHT)] == ["Veloce"]


@pytest.mark.parametrize(
    "obj",
    ["ARC FOR HD22484", "CENX_3 ARC", "FLAT_TUNG_R", "TWILIGHT SKY_R", "Bias Frame", "Dark 5s", "THAR CAK", "FibThAr", "SimThLong", "Acquire"],
)
def test_aat_drops_calibrations_named_in_object(obj):
    assert aat.to_records([_frame(1, obj=obj)], URL, NIGHT) == []


@pytest.mark.parametrize("obj", ["HD22484", "CEN X-3", "QV_NOR", "SIRIUS CAK", "Gl87", "523.01", "1327-083", "HIP77946"])
def test_aat_keeps_real_targets(obj):
    assert len(aat.to_records([_frame(1, obj=obj)], URL, NIGHT)) == 1


def test_aat_drops_calibration_runcmd_and_zero_exposures():
    rows = [_frame(1, runcmd="FFLAT"), _frame(2, EXPOSED=0), _frame(3, fits_header={})]
    # A 1990-93 frame has no RUNCMD header at all and is kept.
    assert [r.archive_obs_id for r in aat.to_records(rows, URL, NIGHT)] == ["3"]


def test_aat_object_that_is_a_gaia_source_id():
    ra, dec = 158.0489, 14.1377
    source_id = _gaia_id_at(ra, dec)
    (record,) = aat.to_records([_frame(1, obj=f" {source_id}", instrument="Veloce", runcmd="OBJECT", ra=ra, dec=dec)], URL, NIGHT)
    assert record.gaia_source_id == source_id
    assert record.raw_target_name is None


def test_aat_nameless_frame_is_kept_on_position_alone():
    (record,) = aat.to_records([_frame(1, obj="")], URL, NIGHT)
    assert record.raw_target_name is None and record.ra is not None
    assert aat.to_records([_frame(1, obj="", ra=None, dec=None)], URL, NIGHT) == []


# --- aat_2df: one record per fibre ----------------------------------------


def test_2df_one_record_per_fibre_per_exposure():
    fibres = [_fibre(1, "Fld_SF4_tar_0257"), _fibre(2, "Fld_SF4_tar_0009", ra=135.1857, dec=-79.0520)]
    rows = [
        _frame(20190301100018, obj="LMC-Field-SF4", instrument="aaomega", fibre_table=fibres),
        _frame(20190301200018, obj="LMC-Field-SF4", instrument="aaomega", fibre_table=fibres),
    ]
    records = aat_2df.to_records(rows, URL, NIGHT)

    assert [r.archive_obs_id for r in records] == ["20190301100018:1", "20190301100018:2"]
    assert (records[1].ra, records[1].dec) == (135.1857, -79.0520)
    assert all(r.instrument == "AAOmega (2dF)" and r.reduction_status == "raw" for r in records)
    assert all(r.program_id == "A/2019A/08" and r.obs_date == NIGHT for r in records)
    # Program-internal fibre names are not passed on to SIMBAD.
    assert all(r.raw_target_name is None and r.gaia_source_id is None for r in records)


def test_2df_fibre_identifiers():
    ra, dec = 153.4228, -45.9907
    source_id = _gaia_id_at(ra, dec)
    fibres = [
        _fibre(1, str(source_id), ra=ra, dec=dec),
        _fibre(2, "2MASS00442854-7241302"),
        _fibre(3, "HD_10700"),
        _fibre(4, "J085607.338+025411.88"),
        _fibre(5, str(source_id)),  # the same integer, nowhere near where it points
    ]
    records = aat_2df.to_records([_frame(1, instrument="aaomega", fibre_table=fibres)], URL, NIGHT)

    assert [r.gaia_source_id for r in records] == [source_id, None, None, None, None]
    assert [r.raw_target_name for r in records] == [None, "2MASS J00442854-7241302", "HD 10700", None, None]


def test_2df_skips_frames_without_fibres_and_unusable_fibres():
    rows = [
        _frame(1, instrument="aaomega", fibre_table=[]),
        _frame(2, instrument="aaomega", fibre_table=None),
        _frame(3, instrument="aaomega", fibre_table=[_fibre(1, "a", ra=None), {"name": "b", "ra": 1.0, "dec": 1.0}, _fibre(7, "c")]),
    ]
    assert [r.archive_obs_id for r in aat_2df.to_records(rows, URL, NIGHT)] == ["3:7"]


def test_both_archives_are_registered():
    from sync.main import ARCHIVES

    assert ARCHIVES["aat"] is aat.fetch
    assert ARCHIVES["aat_2df"] is aat_2df.fetch
