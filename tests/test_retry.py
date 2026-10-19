import pytest
import requests
from pyvo.dal.exceptions import DALQueryError, DALServiceError

import sync.retry as retry_module
from sync.retry import is_transient, retry_delay


def _http_error(status_code, headers=None):
    resp = requests.Response()
    resp.status_code = status_code
    resp.headers.update(headers or {})
    return requests.HTTPError(f"{status_code}", response=resp)


def _dal_service_error(status_code, headers=None):
    return DALServiceError.from_except(_http_error(status_code, headers), "https://x/TAP/sync")


def test_is_transient_retries_pyvo_wrapped_gateway_timeout():
    # irtf_spex, 2026-09-30: IRSA's "504 Gateway Time-out" arrives as a
    # DALServiceError, which none of TRANSIENT_ERRORS' classes match.
    assert is_transient(_dal_service_error(504))
    assert is_transient(_dal_service_error(502))
    assert is_transient(_dal_service_error(429))


def test_is_transient_does_not_retry_pyvo_client_errors():
    assert not is_transient(_dal_service_error(400))
    assert not is_transient(_dal_service_error(404))


def test_is_transient_retries_pyvo_wrapped_timeout_without_status():
    exc = DALServiceError.from_except(requests.exceptions.ReadTimeout("timed out"), "https://x/TAP/sync")
    assert is_transient(exc)


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_is_transient_retries_plain_requests_rate_limit_and_server_errors(status_code):
    # 2026-10-02 monthly reconcile: LCO's 429 (lco_floyds, lco_nres) and
    # SkyServer's 503 (sdss_v_apogee) come from raise_for_status() directly.
    assert is_transient(_http_error(status_code))


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_is_transient_does_not_retry_plain_requests_client_errors(status_code):
    assert not is_transient(_http_error(status_code))


def test_is_transient_retries_koa_temp_table_collision():
    # koa, 2026-10-14 weekly reconcile; the same query worked on the next run.
    exc = DALQueryError("Failed to create tmp_accessiddbtbl: ORA-00955: name is already used by an existing object")
    assert is_transient(exc)


def test_is_transient_does_not_retry_other_query_errors():
    assert not is_transient(DALQueryError("Column 'planeid' not found"))


def test_is_transient_leaves_real_bugs_alone():
    assert not is_transient(KeyError("planeid"))


def test_retry_delay_grows_with_attempt():
    exc = requests.exceptions.ReadTimeout("slow")
    assert retry_delay(exc, 1) == retry_module.RETRY_BACKOFF_SECONDS
    assert retry_delay(exc, 2) == 2 * retry_module.RETRY_BACKOFF_SECONDS


def test_retry_delay_honours_a_longer_retry_after():
    assert retry_delay(_http_error(429, {"Retry-After": "120"}), 1) == 120
    assert retry_delay(_dal_service_error(503, {"Retry-After": "90"}), 1) == 90


def test_retry_delay_ignores_a_shorter_or_unparseable_retry_after():
    assert retry_delay(_http_error(429, {"Retry-After": "5"}), 1) == retry_module.RETRY_BACKOFF_SECONDS
    http_date = _http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert retry_delay(http_date, 1) == retry_module.RETRY_BACKOFF_SECONDS


def test_retry_delay_caps_retry_after():
    assert retry_delay(_http_error(429, {"Retry-After": "86400"}), 1) == retry_module.MAX_RETRY_AFTER_SECONDS
