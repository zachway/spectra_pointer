"""One-off/periodic: turn a request log into a country-level visitor-count
snapshot for the "Who's using The Spectra Pointer?" map on /info.

Two sources, picked with --source:
  gcloud (default) -- Cloud Run's own request logs, via `gcloud logging read`.
  local-log -- a local access log file, e.g. gunicorn's own access.log when
    the app is reverse-proxied by Apache (as on joy.chara.gsu.edu) instead
    of run behind Cloud Run. Apache's mod_proxy sets X-Forwarded-For on
    every proxied request automatically, and gunicorn trusts it from a
    127.0.0.1 peer by default (its default forwarded_allow_ips) -- so
    gunicorn's own %(h)s access-log field is already the real client IP,
    not the proxy's loopback address; confirmed against joy's live log.
    Expects gunicorn's default access-log format (effectively Apache
    combined): '%(h)s %(l)s %(u)s [%(t)s] "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'.

Privacy note (read before changing this file): the only per-request datum
this script ever touches is the client IP address, and only transiently --
each IP is geocoded to a country in memory and the IP string is discarded in
the same loop iteration (see _iter_country_codes below). Nothing this script
writes to disk, ever, is a raw IP: the persisted output
(access_heatmap.json) is an aggregate country -> count table plus a
watermark timestamp, the same shape whether one visitor or ten thousand
produced a given country's count. No new logging is added to the app itself
either -- Cloud Run already records the connecting client IP on every
request as httpRequest.remoteIp in Cloud Logging (Google's own request log,
not app code), retained under the project's normal Cloud Logging retention
(30 days by default) and deleted by Google on that schedule regardless of
what this script does. Country-level geocoding (not city, no lat/lon) is a
deliberate choice, not just a limitation of the geoip2fast library used
here -- it's the coarsest granularity that still answers "who's using
this," well short of anything that could pinpoint an individual visitor.

Geocoding is done fully offline via geoip2fast (MIT-licensed, pure Python,
bundles its own small MaxMind-GeoLite2-derived country database) -- no
third-party API calls per IP, no account/license key to manage, consistent
with this project's general aversion to live external calls in a hot path
(see webapp.app's module docstring on why it reads a precomputed snapshot
instead of live Postgres).

Incremental: each run reads the previous access_heatmap.json (if any) in
--out-dir, only asks Cloud Logging for entries newer than its "watermark"
timestamp, and adds the new country counts on top of the old ones -- so the
running total survives Cloud Logging's 30-day retention window rather than
being capped by it. First run has no watermark, so it pulls
--initial-window-days worth of history (default 30, matching that same
retention window -- there's nothing older to pull anyway).

Like scripts.export_to_parquet, this has no automatic trigger -- run it by
hand or your own cron (or an `at`-chain, on a host like joy where crontab
isn't available to this account). --source=gcloud needs `gcloud`
authenticated against the project running the Cloud Run service (whatever
the operator already uses for `gcloud run deploy`), which is a separate
credential from DATABASE_URL and not necessarily available on morgan -- so
unlike export_to_parquet.py this will often run from a different machine,
with its output copied into the same out_dir export_to_parquet.py writes to
(morgan's ~/public_html/spectra_data, see that script's docstring) so
webapp.app's access_heatmap view picks it up from the same published
snapshot directory. --source=local-log has no such credential and can just
be run wherever the log file already is (e.g. directly on joy, writing
straight into the shared out_dir -- no copying needed there since it's the
same NFS mount webapp.app already reads from).

Both sources write the identical access_heatmap.json shape and share one
running total: pointing --source at a different log than a previous run
used still just adds newly-seen requests on top of the existing per-country
counts (matched on country_code), so switching from Cloud Run to joy
hosting doesn't reset or fork the visitor count -- it's intentionally the
same file regardless of which deployment produced which slice of it.

Usage:
    python3 -m scripts.build_access_heatmap --out-dir ~/public_html/spectra_data
    python3 -m scripts.build_access_heatmap --out-dir ~/public_html/spectra_data \\
        --source local-log --log-file ~/spectra_pointer_webapp/access.log
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

from geoip2fast import GeoIP2Fast

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# gcloud logging read has no clean streaming/pagination flag for scripting,
# so this is one bounded read rather than a paged loop -- fine at this
# project's traffic (a small academic tool, not a high-QPS service); revisit
# with real pagination (--format=json + nextPageToken) if a run ever hits
# this ceiling, since that would mean silently dropped visitor counts rather
# than a crash.
LOG_READ_LIMIT = 100_000


def _parse_rfc3339(ts: str) -> datetime:
    """Cloud Logging's own RFC3339 timestamps -- observed to sometimes
    carry fractional seconds (e.g. "2026-08-11T15:39:43.106410Z") and
    sometimes not, undocumented either way, which crashed the fixed
    %Y-%m-%dT%H:%M:%SZ strptime this used before. Truncating to whole-second
    precision is fine here: the watermark only needs to exclude
    already-processed rows on the next run, so being off by under a second
    risks re-processing one request, never silently dropping one. Also used
    to parse the watermark this script itself wrote (both sources normalize
    to this same format -- see _iter_gcloud_entries/_iter_local_log_entries),
    not just gcloud's raw output, despite the name."""
    ts = ts.strip()
    if "." in ts:
        ts = ts.split(".", 1)[0] + "Z"
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _run_gcloud_logging_read(service_name: str, project: str | None, since: datetime) -> str:
    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service_name}" '
        f'AND httpRequest.remoteIp!="" '
        f'AND timestamp>"{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )
    cmd = [
        "gcloud", "logging", "read", filter_str,
        "--order=asc",
        f"--limit={LOG_READ_LIMIT}",
        # value() format prints one TAB-separated line per entry with no
        # quoting/JSON overhead -- both fields here (an RFC3339 timestamp, an
        # IP address) are guaranteed tab-free, so a plain split is safe and
        # this never has to hold a full JSON array of log entries in memory.
        "--format=value(timestamp,httpRequest.remoteIp)",
    ]
    if project:
        cmd.append(f"--project={project}")
    logger.info("running: %s", " ".join(cmd[:4]) + " ...")
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def _iter_gcloud_entries(log_lines: str) -> Iterator[tuple[str, str]]:
    """Yields (timestamp, ip) pairs from gcloud's tab-separated --format=value
    output, timestamp normalized to the same %Y-%m-%dT%H:%M:%SZ form
    _iter_local_log_entries produces -- so build() can treat both sources
    identically from here on."""
    for line in log_lines.splitlines():
        if not line.strip():
            continue
        ts, _, ip = line.partition("\t")
        ip = ip.strip()
        if not ip:
            continue
        yield _parse_rfc3339(ts).strftime("%Y-%m-%dT%H:%M:%SZ"), ip


# gunicorn's default access-log format (effectively Apache's "combined" log,
# minus referer/user-agent which this doesn't need): %(h)s %(l)s %(u)s
# [%(t)s] "%(r)s" %(s)s %(b)s ... -- only %(h)s (client IP) and %(t)s
# (request time) matter here, so this stops matching right after the
# request line rather than trying to fully parse everything past it.
_GUNICORN_LOG_LINE_RE = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "')


def _parse_gunicorn_timestamp(ts: str) -> datetime:
    """gunicorn's %(t)s, e.g. "14/Sep/2026:20:47:06 -0400"."""
    return datetime.strptime(ts, "%d/%b/%Y:%H:%M:%S %z")


def _iter_local_log_entries(log_path: str, since: datetime) -> Iterator[tuple[str, str]]:
    """Yields (timestamp, ip) pairs from a local access log already on disk
    (gunicorn's access.log -- see this module's docstring for why %(h)s is
    already the real client IP, not the reverse proxy's). Filters to entries
    after `since` here in Python since there's no server-side query to push
    that down to, unlike gcloud logging read's own timestamp filter."""
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _GUNICORN_LOG_LINE_RE.match(line)
            if not m:
                continue
            try:
                dt = _parse_gunicorn_timestamp(m.group("ts"))
            except ValueError:
                continue
            if dt <= since:
                continue
            yield dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), m.group("ip")


def _iter_country_codes(entries: Iterable[tuple[str, str]], geoip: GeoIP2Fast) -> Iterator[tuple[str, str]]:
    """Yields (country_code, country_name) pairs, one per real (non-private)
    client IP -- the IP itself never leaves this generator. Skips entries
    geoip2fast can't resolve to a real public address (private/reserved
    ranges -- health checks and Google-internal probes commonly show up
    here for the gcloud source; localhost/LAN probes for the local-log one)."""
    for _timestamp, ip in entries:
        result = geoip.lookup(ip)
        if result.is_private or not result.country_code:
            continue
        yield result.country_code, result.country_name


def _latest_timestamp(entries: Iterable[tuple[str, str]]) -> str | None:
    """Both sources already yield entries with a normalized, fixed-width
    %Y-%m-%dT%H:%M:%SZ timestamp, so a plain string max() sorts correctly
    without re-parsing -- and doesn't assume either source hands entries
    back in chronological order (gcloud's --order=asc does; a plain file
    read naturally does too, but nothing enforces that)."""
    timestamps = [ts for ts, _ip in entries]
    return max(timestamps) if timestamps else None


def _load_previous(out_dir: str) -> dict:
    path = os.path.join(out_dir, "access_heatmap.json")
    if not os.path.exists(path):
        return {"watermark": None, "countries": {}}
    with open(path) as f:
        data = json.load(f)
    countries = {c["country_code"]: c for c in data.get("countries", [])}
    return {"watermark": data.get("watermark"), "countries": countries}


def _write_atomic(out_dir: str, payload: dict) -> None:
    path = os.path.join(out_dir, "access_heatmap.json")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.chmod(tmp_path, 0o644)
    os.rename(tmp_path, path)
    logger.info("wrote %s (%d countries, %d total requests)", path, len(payload["countries"]), payload["total_requests"])


def build(
    out_dir: str,
    source: str,
    service_name: str,
    project: str | None,
    log_file: str | None,
    initial_window_days: int,
) -> None:
    previous = _load_previous(out_dir)
    if previous["watermark"]:
        since = _parse_rfc3339(previous["watermark"])
    else:
        since = datetime.now(timezone.utc) - timedelta(days=initial_window_days)

    if source == "gcloud":
        log_lines = _run_gcloud_logging_read(service_name, project, since)
        entries = list(_iter_gcloud_entries(log_lines))
    else:
        entries = list(_iter_local_log_entries(log_file, since))
    if not entries:
        logger.info("no new request log entries since %s", since.isoformat())
        return

    geoip = GeoIP2Fast()
    counts = previous["countries"]  # {country_code: {"country": ..., "country_code": ..., "count": ...}}
    n_new = 0
    for country_code, country_name in _iter_country_codes(entries, geoip):
        entry = counts.setdefault(country_code, {"country": country_name, "country_code": country_code, "count": 0})
        entry["count"] += 1
        n_new += 1

    watermark = _latest_timestamp(entries) or previous["watermark"]
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "watermark": watermark,
        "total_requests": sum(c["count"] for c in counts.values()),
        "countries": sorted(counts.values(), key=lambda c: c["count"], reverse=True),
    }
    logger.info("%d new request(s) geocoded this run", n_new)
    _write_atomic(out_dir, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", required=True, help="directory Apache serves, e.g. ~/public_html/spectra_data")
    parser.add_argument("--source", choices=["gcloud", "local-log"], default="gcloud", help="where to read request logs from")
    parser.add_argument("--service-name", default="spectra-pointer", help="Cloud Run service name (--source=gcloud only)")
    parser.add_argument("--project", default=None, help="GCP project id, defaults to gcloud's configured project (--source=gcloud only)")
    parser.add_argument("--log-file", default=None, help="path to a local access log, e.g. gunicorn's access.log (--source=local-log only)")
    parser.add_argument("--initial-window-days", type=int, default=30, help="lookback on the first run, before any watermark exists")
    args = parser.parse_args()

    if args.source == "local-log" and not args.log_file:
        parser.error("--log-file is required when --source=local-log")

    out_dir = os.path.expanduser(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o755)

    build(out_dir, args.source, args.service_name, args.project, args.log_file, args.initial_window_days)


if __name__ == "__main__":
    main()
