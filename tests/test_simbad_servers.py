import pytest
import requests
from astropy.table import Table
from pyvo.dal.exceptions import DALQueryError, DALServiceError

from ingest import simbad_servers

CDS, CFA = simbad_servers.SIMBAD_SERVERS


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(simbad_servers, "_state", {})


@pytest.fixture
def up(monkeypatch):
    """Which servers the probe reports as up; tests mutate it."""
    servers = {CDS, CFA}
    monkeypatch.setattr(simbad_servers, "_probe", lambda server: server in servers)
    return servers


class FakeSimbad:
    """Stands in for astroquery's Simbad; behaviour per server comes from
    the test's `responses` dict: a Table, or an exception to raise."""

    responses: dict = {}
    calls: list = []

    def __init__(self):
        self.server = None

    def add_votable_fields(self, *fields):
        pass

    def _answer(self, kind, arg):
        FakeSimbad.calls.append((self.server, kind, arg))
        answer = FakeSimbad.responses[self.server]
        if isinstance(answer, BaseException):
            raise answer
        return answer(arg) if callable(answer) else answer

    def query_object(self, name):
        return self._answer("object", name)

    def query_objects(self, names):
        return self._answer("objects", list(names))


@pytest.fixture
def fake_simbad(monkeypatch):
    FakeSimbad.responses = {}
    FakeSimbad.calls = []
    monkeypatch.setattr(simbad_servers, "Simbad", FakeSimbad)
    return FakeSimbad


def test_prefers_cds_when_it_is_up(up, fake_simbad):
    fake_simbad.responses = {CDS: Table({"main_id": ["cds"]}), CFA: Table({"main_id": ["cfa"]})}
    result = simbad_servers.query(lambda s: s.query_object("Vega"), "ids")
    assert result["main_id"][0] == "cds"


def test_skips_a_server_the_probe_says_is_down(up, fake_simbad):
    up.discard(CDS)
    fake_simbad.responses = {CDS: AssertionError("CDS must not be queried"), CFA: Table({"main_id": ["cfa"]})}
    result = simbad_servers.query(lambda s: s.query_object("Vega"))
    assert result["main_id"][0] == "cfa"


def test_fails_over_when_a_query_errors_and_marks_that_server_down(up, fake_simbad):
    fake_simbad.responses = {CDS: DALServiceError("503"), CFA: Table({"main_id": ["cfa"]})}
    assert simbad_servers.query(lambda s: s.query_object("Vega"))["main_id"][0] == "cfa"
    # The failure is remembered: the next query goes straight to the mirror.
    fake_simbad.calls.clear()
    simbad_servers.query(lambda s: s.query_object("Vega"))
    assert [server for server, _, _ in fake_simbad.calls] == [CFA]


def test_goes_back_to_cds_once_its_down_entry_expires(up, fake_simbad, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(simbad_servers.time, "monotonic", lambda: clock[0])
    fake_simbad.responses = {CDS: Table({"main_id": ["cds"]}), CFA: Table({"main_id": ["cfa"]})}
    up.discard(CDS)
    assert simbad_servers.query(lambda s: s.query_object("Vega"))["main_id"][0] == "cfa"
    up.add(CDS)
    clock[0] += simbad_servers.DOWN_CACHE_S - 1
    assert simbad_servers.query(lambda s: s.query_object("Vega"))["main_id"][0] == "cfa"
    clock[0] += 2
    assert simbad_servers.query(lambda s: s.query_object("Vega"))["main_id"][0] == "cds"


def test_a_rejected_query_does_not_fail_over(up, fake_simbad):
    fake_simbad.responses = {CDS: DALQueryError("bad ADQL"), CFA: AssertionError("must not fail over")}
    with pytest.raises(DALQueryError):
        simbad_servers.query(lambda s: s.query_object("Vega"))


def test_raises_simbad_unavailable_when_every_server_is_down(up, fake_simbad):
    up.clear()
    with pytest.raises(simbad_servers.SimbadUnavailable) as exc_info:
        simbad_servers.query(lambda s: s.query_object("Vega"))
    # Existing `except DALServiceError` outage handlers must still catch it.
    assert isinstance(exc_info.value, DALServiceError)


def test_query_objects_goes_one_by_one_on_a_server_without_uploads(up, fake_simbad):
    up.discard(CDS)

    def by_name(name):
        if name == "not a star":
            return Table({"main_id": [], "ids": []}, dtype=[str, str])
        return Table({"main_id": [name.upper()], "ids": [f"Gaia DR3 1|{name}"]})

    fake_simbad.responses = {CFA: by_name}
    result = simbad_servers.query_objects(["Vega", "not a star", "Proxima"], "ids")
    assert sorted(result["user_specified_id"]) == ["Proxima", "Vega"]
    assert all(kind == "object" for _, kind, _ in fake_simbad.calls)


def test_query_objects_uses_one_upload_on_cds(up, fake_simbad):
    fake_simbad.responses = {CDS: Table({"user_specified_id": ["Vega"], "ids": ["x"]})}
    simbad_servers.query_objects(["Vega", "Proxima"], "ids")
    assert fake_simbad.calls == [(CDS, "objects", ["Vega", "Proxima"])]


def test_probe_result_is_cached(monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        raise requests.ConnectionError("down")
    monkeypatch.setattr(simbad_servers.requests, "get", fake_get)
    assert simbad_servers.any_available() is False
    assert simbad_servers.any_available() is False
    assert len(calls) == len(simbad_servers.SIMBAD_SERVERS)
