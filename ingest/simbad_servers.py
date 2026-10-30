"""SIMBAD access with failover across its mirrors.

Every SIMBAD query in this project goes through query() / query_objects()
here instead of a bare astroquery Simbad(), so an outage of one SIMBAD
service falls through to the next instead of failing the lookup.

Only two independent SIMBAD services exist (checked 2026-10-30, the same
pair astroquery's own conf.servers_list carries): CDS and the CfA mirror.
simbad.u-strasbg.fr is an alias of CDS (same IP, 130.79.128.4) and
simbad.cfa.harvard.edu an alias of simbad.harvard.edu, so listing them too
would just probe the same machines twice.

SIMBAD_SERVERS is in preference order. A server's health is probed (a quick
GET of its TAP capabilities endpoint) and cached per process -- up results
for UP_CACHE_S, down results for DOWN_CACHE_S -- and every query walks the
list from the top, so once CDS's "down" entry expires and it answers again,
queries go back to it on their own.

Why probe at all rather than just try the query: observed during the
2026-10 CDS outage, a failing astroquery call only raised after ~75s
(astroquery retries the capabilities endpoint, each attempt waiting out
simbad_conf.timeout) -- long enough to blow past the webapp's 60s proxy
timeout before any failover could even start.
"""

from __future__ import annotations

import logging
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

import requests
from astropy.table import Column, Table, vstack
from astroquery.exceptions import NoResultsWarning
from astroquery.simbad import Simbad
from pyvo.dal.exceptions import DALServiceError

logger = logging.getLogger(__name__)

SIMBAD_SERVERS = ("simbad.cds.unistra.fr", "simbad.harvard.edu")

# Observed 2026-10-30: the CfA mirror answers query_object and plain ADQL
# in ~0.1s but 504s after 60s on query_objects, which astroquery 0.4.11
# sends as a TAP table upload. Batch lookups against it go name by name.
SERVERS_WITHOUT_UPLOADS = frozenset({"simbad.harvard.edu"})
ONE_BY_ONE_WORKERS = 8
# query_object warns once per name SIMBAD doesn't know -- harmless, but a
# one-by-one batch of a sync page's target names would print thousands of
# them. query_objects (the CDS path) reports misses silently by just
# leaving them out; match that.
warnings.filterwarnings("ignore", category=NoResultsWarning)

PROBE_TIMEOUT_S = 5
UP_CACHE_S = 60
DOWN_CACHE_S = 300

# Service-level failures only. pyvo's DALQueryError (a bad query) is not a
# DALServiceError subclass, so a query the server rejected doesn't get
# blamed on the server and retried elsewhere.
FAILOVER_ERRORS = (DALServiceError, requests.RequestException, TimeoutError, ConnectionError)


class SimbadUnavailable(DALServiceError):
    """No SIMBAD server could answer. A DALServiceError subclass so every
    existing `except DALServiceError` outage handler keeps catching it."""


_state_lock = threading.Lock()
_state: dict[str, tuple[bool, float]] = {}


def _probe(server: str) -> bool:
    try:
        resp = requests.get(f"https://{server}/simbad/sim-tap/capabilities", timeout=PROBE_TIMEOUT_S)
    except requests.RequestException:
        return False
    return resp.status_code < 500


def server_available(server: str) -> bool:
    with _state_lock:
        cached = _state.get(server)
    if cached is not None:
        available, checked_at = cached
        if time.monotonic() - checked_at < (UP_CACHE_S if available else DOWN_CACHE_S):
            return available
    available = _probe(server)
    if not available:
        logger.warning("SIMBAD server %s is not answering, skipping it for %ds", server, DOWN_CACHE_S)
    with _state_lock:
        _state[server] = (available, time.monotonic())
    return available


def mark_down(server: str) -> None:
    with _state_lock:
        _state[server] = (False, time.monotonic())


def any_available() -> bool:
    return any(server_available(server) for server in SIMBAD_SERVERS)


def uploads_available() -> bool:
    """Whether a server that takes query_objects' TAP upload is up -- i.e.
    whether a batch lookup gets one request per chunk rather than going
    name by name (~7 names/s against the CfA mirror, observed 2026-10-30)."""
    return any(server_available(s) for s in SIMBAD_SERVERS if s not in SERVERS_WITHOUT_UPLOADS)


def _client(server: str, fields: tuple[str, ...]) -> Simbad:
    simbad = Simbad()
    simbad.server = server
    if fields:
        simbad.add_votable_fields(*fields)
    return simbad


T = TypeVar("T")


def _run(fn: Callable[[str], T], servers: tuple[str, ...]) -> T:
    last_exc: BaseException | None = None
    for server in servers:
        if not server_available(server):
            continue
        try:
            return fn(server)
        except FAILOVER_ERRORS as exc:
            logger.warning("SIMBAD query failed on %s, trying the next server", server, exc_info=True)
            mark_down(server)
            last_exc = exc
    raise SimbadUnavailable("no SIMBAD server is reachable") from last_exc


def query(fn: Callable[[Simbad], T], *fields: str) -> T:
    """Run fn(simbad) against the most-preferred reachable SIMBAD server,
    with `fields` added as votable fields, failing over down the list.
    Raises SimbadUnavailable if every server is down."""
    return _run(lambda server: fn(_client(server, fields)), SIMBAD_SERVERS)


def query_objects(names: list[str], *fields: str) -> Table:
    """Simbad.query_objects with failover. On a server that can't take TAP
    uploads (see SERVERS_WITHOUT_UPLOADS) the names are looked up one
    query_object call at a time, in parallel, and stacked into the same
    shape: one row per resolved name, with user_specified_id set to the
    name as given. Names SIMBAD doesn't know are just absent, as with
    query_objects."""

    def run(server: str) -> Table:
        if server not in SERVERS_WITHOUT_UPLOADS:
            return _client(server, fields).query_objects(names)

        def one(name: str) -> Table | None:
            result = _client(server, fields).query_object(name)
            if result is None or len(result) == 0:
                return None
            result = result[:1]
            result.add_column(Column([name], name="user_specified_id"))
            return result

        with ThreadPoolExecutor(max_workers=ONE_BY_ONE_WORKERS) as pool:
            tables = [t for t in pool.map(one, names) if t is not None]
        return vstack(tables, metadata_conflicts="silent") if tables else Table()

    return _run(run, SIMBAD_SERVERS)
