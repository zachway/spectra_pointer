"""Per-page retry for archive-side hiccups, shared by sync.main and sync.reconcile.

One page of one archive is retried on a transient error (see is_transient)
with growing backoff; anything else is a real bug and propagates on the
first attempt. The cursor only advances when a page succeeds (sync.runner),
so a retry re-fetches the same page.
"""

from __future__ import annotations

import logging
import time

import psycopg
import pyvo.dal.exceptions
import requests
import urllib3.exceptions

logger = logging.getLogger(__name__)

# Errors a page is worth retrying on: archive-side hiccups seen in the 2026-09-01
# monthly run (CADC read timeouts for cfht_cadc/dao, a JVO non-VOTable response
# for naoj) and a Postgres deadlock against a concurrent writer (koa).
TRANSIENT_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    urllib3.exceptions.HTTPError,
    pyvo.dal.exceptions.DALFormatError,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
)
PAGE_RETRIES = 3
RETRY_BACKOFF_SECONDS = 30
# Upper bound on a server's Retry-After, so one answer can't park a cron run.
MAX_RETRY_AFTER_SECONDS = 600

# Oracle "name is already used by an existing object": KOA's TAP failed to
# create its per-query temp table (tmp_accessiddbtbl) on 2026-10-14 and the
# same query worked on the next run -- a collision on their side, not our query.
_TRANSIENT_QUERY_ERROR_MARKERS = ("ORA-00955",)


def _is_transient_status(code) -> bool:
    """429 (rate limited) or any 5xx. Other 4xx mean our request is wrong."""
    return isinstance(code, int) and (code == 429 or 500 <= code < 600)


def _response_status(exc: requests.HTTPError):
    return exc.response.status_code if exc.response is not None else None


def is_transient(exc: BaseException) -> bool:
    """TRANSIENT_ERRORS, plus the same hiccups as they arrive wrapped.

    - requests.HTTPError from an archive calling raise_for_status() itself:
      2026-10-02, LCO's `429 Too Many Requests` (lco_floyds, lco_nres) and
      SkyServer's `503` (sdss_v_apogee) each failed a whole archive unretried.
    - pyvo wraps every HTTP/transport failure of a TAP call in
      DALServiceError, so none of the requests/urllib3 classes above ever
      match one directly -- seen 2026-09-30: a single IRSA `504 Gateway
      Time-out` on irtf_spex's first page. Retried when the service answered
      429/5xx, or when the wrapped cause is itself transient (a timeout or
      dropped connection, which carry no status code).
    - DALQueryError for the server-side collisions in
      _TRANSIENT_QUERY_ERROR_MARKERS. Any other query error is our ADQL.
    """
    if isinstance(exc, TRANSIENT_ERRORS):
        return True
    if isinstance(exc, requests.HTTPError):
        return _is_transient_status(_response_status(exc))
    if isinstance(exc, pyvo.dal.exceptions.DALQueryError):
        return any(marker in str(exc) for marker in _TRANSIENT_QUERY_ERROR_MARKERS)
    if isinstance(exc, pyvo.dal.exceptions.DALServiceError):
        if _is_transient_status(getattr(exc, "code", None)):
            return True
        return isinstance(getattr(exc, "cause", None), TRANSIENT_ERRORS)
    return False


def _retry_after_seconds(exc: BaseException) -> float | None:
    """A numeric Retry-After header on the response behind exc, if any."""
    cause = exc if isinstance(exc, requests.HTTPError) else getattr(exc, "cause", None)
    response = getattr(cause, "response", None)
    if response is None:
        return None
    try:
        return float(response.headers.get("Retry-After"))
    except (TypeError, ValueError):
        # Absent, or the HTTP-date form, which no archive has sent us.
        return None


def retry_delay(exc: BaseException, attempt: int) -> float:
    """Growing backoff, stretched to the server's Retry-After when it asks for longer."""
    delay = RETRY_BACKOFF_SECONDS * attempt
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        delay = max(delay, min(retry_after, MAX_RETRY_AFTER_SECONDS))
    return delay


def with_page_retries(conn: psycopg.Connection, archive_code: str, run_page):
    """Call run_page() (one sync.runner.run_sync page), retried on transient errors.

    Rolls the connection back before each retry (a deadlock leaves the
    transaction aborted)."""
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            return run_page()
        except Exception as exc:
            if not is_transient(exc):
                raise
            conn.rollback()
            if attempt == PAGE_RETRIES:
                raise
            delay = retry_delay(exc, attempt)
            logger.warning(
                "%s: transient error on page (attempt %d/%d), retrying in %ds: %s",
                archive_code, attempt, PAGE_RETRIES, delay, exc,
            )
            time.sleep(delay)
