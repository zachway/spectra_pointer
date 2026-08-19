"""One-off/periodic: turn Cloud Run's own request logs into a country-level
visitor-count snapshot for the "Who's using The Spectra Pointer?" map on
/info.

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
hand or your own cron. Needs `gcloud` authenticated against the project
running the Cloud Run service (whatever the operator already uses for
`gcloud run deploy`), which is a separate credential from DATABASE_URL and
not necessarily available on morgan -- so unlike export_to_parquet.py this
will often run from a different machine, with its output copied into the
same out_dir export_to_parquet.py writes to (morgan's
~/public_html/spectra_data, see that script's docstring) so webapp.app's
access_heatmap view picks it up from the same published snapshot directory.

Usage:
    python3 -m scripts.build_access_heatmap --out-dir ~/public_html/spectra_data
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Iterator

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


def _parse_gcloud_timestamp(ts: str) -> datetime:
    """Cloud Logging's own RFC3339 timestamps -- observed to sometimes
    carry fractional seconds (e.g. "2026-08-11T15:39:43.106410Z") and
    sometimes not, undocumented either way, which crashed the fixed
    %Y-%m-%dT%H:%M:%SZ strptime this used before. Truncating to whole-second
    precision is fine here: the watermark only needs to exclude
    already-processed rows on the next run (the filter in
    _run_gcloud_logging_read is a strict '>', not '>='), so being off by
    under a second risks re-processing one request, never silently
    dropping one."""
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


def _iter_country_codes(log_lines: str, geoip: GeoIP2Fast) -> Iterator[tuple[str, str]]:
    """Yields (country_code, country_name) pairs, one per real (non-private)
    client IP -- the IP itself never leaves this generator. Skips rows geoip2fast
    can't resolve to a real public address (private/reserved ranges -- health
    checks and Google-internal probes commonly show up here -- and any row
    gcloud didn't return an IP for at all)."""
    for line in log_lines.splitlines():
        if not line.strip():
            continue
        _timestamp, _, ip = line.partition("\t")
        ip = ip.strip()
        if not ip:
            continue
        result = geoip.lookup(ip)
        if result.is_private or not result.country_code:
            continue
        yield result.country_code, result.country_name


def _latest_timestamp(log_lines: str) -> str | None:
    latest = None
    for line in log_lines.splitlines():
        ts, _, _ = line.partition("\t")
        ts = ts.strip()
        if ts:
            latest = ts  # --order=asc, so the last non-empty line is newest
    if latest is None:
        return None
    # Normalized to the canonical no-fractional-seconds form here (rather
    # than persisting whatever raw precision gcloud happened to return) so
    # every access_heatmap.json this ever writes has one consistent
    # watermark format, and _parse_gcloud_timestamp's fractional-seconds
    # handling only has to cover reading old already-written files, not new
    # ones too.
    return _parse_gcloud_timestamp(latest).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def build(out_dir: str, service_name: str, project: str | None, initial_window_days: int) -> None:
    previous = _load_previous(out_dir)
    if previous["watermark"]:
        since = _parse_gcloud_timestamp(previous["watermark"])
    else:
        since = datetime.now(timezone.utc) - timedelta(days=initial_window_days)

    log_lines = _run_gcloud_logging_read(service_name, project, since)
    if not log_lines.strip():
        logger.info("no new request log entries since %s", since.isoformat())
        return

    geoip = GeoIP2Fast()
    counts = previous["countries"]  # {country_code: {"country": ..., "country_code": ..., "count": ...}}
    n_new = 0
    for country_code, country_name in _iter_country_codes(log_lines, geoip):
        entry = counts.setdefault(country_code, {"country": country_name, "country_code": country_code, "count": 0})
        entry["count"] += 1
        n_new += 1

    watermark = _latest_timestamp(log_lines) or previous["watermark"]
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
    parser.add_argument("--service-name", default="spectra-pointer", help="Cloud Run service name")
    parser.add_argument("--project", default=None, help="GCP project id (defaults to gcloud's configured project)")
    parser.add_argument("--initial-window-days", type=int, default=30, help="lookback on the first run, before any watermark exists")
    args = parser.parse_args()

    out_dir = os.path.expanduser(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o755)

    build(out_dir, args.service_name, args.project, args.initial_window_days)


if __name__ == "__main__":
    main()
