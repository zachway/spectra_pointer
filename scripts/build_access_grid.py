"""Periodic: turn joy's request log into a coarse lat/lon grid of "where
this site's visitors are" for the second, finer map on /info -- a
companion to scripts.build_access_heatmap's country-level map, not a
replacement for it.

Privacy note (read before changing this file): finer-than-country location
is where a request log starts being able to point at people, not just
places -- this project's audience is astronomers, often at small
institutions, and university IP blocks are among the most precisely
geolocated addresses there are, so "one visitor near <small town with an
observatory>" can identify an individual. Four rules keep the published
grid on the "places" side of that line; weaken none of them without
re-reading this note:

  1. Coarse cells only. Each visitor is placed at a city (see "Geocoding"
     below) and then snapped to a --cell-deg grid cell (default 2 deg,
     ~220 km N-S) -- only the cell is ever published, never a city name or
     point. Anything finer than a city is mostly fiction in IP geolocation
     anyway (see "Geocoding").
  2. Small cells are suppressed. A cell is published only if at least
     --min-visitors (default 5) distinct *networks* were seen in it over
     the whole window; everything below that is folded into one global
     "suppressed" total, with no location attached.
  3. Networks, not IPs. A "visitor" here is an IPv4 /24 or an IPv6 /48, not
     a single address -- IPv6 privacy addresses rotate daily and mobile
     carriers reassign IPv4s constantly, so counting raw IPs would let one
     person's 30 days of visits clear the rule-2 threshold alone. Counting
     networks undercounts (a whole campus can share one /24), which is the
     safe direction for rule 2.
  4. Window totals only, no time series. Unlike access_heatmap.json's
     per-day country buckets, the output here is one total per cell for
     the whole trailing window -- a per-day fine-grained series would let a
     reader line a single visit up with a single day.

Nothing this script writes is an IP, a network prefix, or a hash of
either: those live only in memory for the length of one run. That's also
why this is NOT incremental the way build_access_heatmap is -- counting
*distinct* networks per cell across runs would mean persisting some
per-network identifier between runs. Instead every run re-reads the whole
--window-days of log from scratch. That works because
scripts/trim_access_log.py keeps exactly that much log on disk anyway
(both default to 30 days -- keep them matched: a shorter trim silently
shortens this map's real window, a longer one is simply ignored here), and
it's cheap at this project's traffic (one offline lookup per distinct
network, ~tens of microseconds each).

Geocoding is fully offline, in two steps, both from free, keyless,
periodically re-downloaded data files (see _ensure_data_file):
  - geoip2fast's own city database (geoip2fast-city-ipv6.dat.gz, derived
    from MaxMind GeoLite2, covers IPv4 and IPv6) maps an IP to a country +
    subdivision + city *name*. Its data format has no lat/lon columns
    (CityDetail.latitude is always None in geoip2fast 1.2.x), hence:
  - GeoNames' cities1000 + admin1CodesASCII (CC-BY 4.0) map those names to
    coordinates. MaxMind's location names are themselves GeoNames-derived,
    so the join is (country, subdivision name, city name) first, then the
    subdivision's population-weighted centroid, then (country, city name)
    alone -- in that order, since a same-named city elsewhere in the same
    country is a worse guess than the right subdivision's middle. On
    random public IPv4s, ~99.6% of IPs that geoip2fast resolves to a city
    get placed this way; the rest (plus IPs with no city at all, e.g. big
    cloud/anycast blocks) count toward "unplaced", shown on /info but not
    on the grid. GeoNames rows are streamed and only the ones this run's
    visitors actually need are kept, since indexing every alternate name
    up front costs ~750 MB.

Like build_access_heatmap, no automatic trigger of its own -- run from
scripts/refresh_access_heatmap.sh on morgan, which reads joy's access.log
over the shared NFS mount.

Usage:
    python3 -m scripts.build_access_grid --out-dir ~/public_html/spectra_data \\
        --log-file ~/spectra_pointer_webapp/access.log
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import json
import logging
import math
import os
import time
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from geoip2fast import GeoIP2Fast

from scripts.build_access_heatmap import _iter_local_log_entries

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GEOIP_CITY_URL = "https://github.com/rabuchaim/geoip2fast/releases/download/LATEST/geoip2fast-city-ipv6.dat.gz"
GEONAMES_CITIES_URL = "https://download.geonames.org/export/dump/cities1000.zip"
GEONAMES_ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

OUTPUT_FILENAME = "access_grid.json"

# Place = (country_code, normalized subdivision name, normalized city name).
Place = tuple[str, str, str]


def _norm(name: str) -> str:
    """Accent-, case- and whitespace-insensitive key, so MaxMind's "Köln"
    and GeoNames' ASCII "Koln" (or MaxMind's "Baden-Wurttemberg" and
    GeoNames' "Baden-Württemberg") land on the same key."""
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().strip()


def _network_key(ip: str) -> str | None:
    """The unit a "visitor" is counted in -- see rule 3 in the module
    docstring. None for anything that doesn't parse as an IP."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def _ensure_data_file(data_dir: str, url: str, max_age_days: int) -> str:
    """Returns a local path to url's file under data_dir, downloading it
    first if missing or older than max_age_days. A failed refresh of a file
    that already exists just logs and keeps using the stale copy -- slightly
    old geolocation data is far better than no map -- but a failed first
    download raises, since there's nothing to fall back to."""
    path = os.path.join(data_dir, url.rsplit("/", 1)[1])
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_days * 86400:
        return path
    tmp_path = path + ".tmp"
    try:
        logger.info("downloading %s", url)
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp_path, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
        os.rename(tmp_path, path)
    except OSError:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if os.path.exists(path):
            logger.warning("refresh of %s failed, using existing (stale) copy", url, exc_info=True)
            return path
        raise
    return path


def _distinct_networks(entries: Iterable[tuple[str, str]]) -> dict[str, str]:
    """{network: one IP seen from it} -- the representative IP is what gets
    geocoded. Networks this small are always one geolocation record in
    practice, so which IP stands in for it doesn't matter."""
    networks: dict[str, str] = {}
    for _ts, ip in entries:
        key = _network_key(ip)
        if key is not None and key not in networks:
            networks[key] = ip
    return networks


def _geocode_to_places(networks: dict[str, str], geoip: GeoIP2Fast) -> tuple[dict[Place, int], int]:
    """Returns ({place: distinct networks}, n_unplaceable). Each network's
    IP is dropped as soon as it's been looked up -- only place names and
    counts leave this function. Private/reserved addresses (localhost and
    LAN probes) are skipped entirely rather than counted as unplaced, same
    as build_access_heatmap; public IPs with no city (common for big
    cloud/anycast blocks) count as unplaced."""
    places: dict[Place, int] = defaultdict(int)
    n_unplaced = 0
    for ip in networks.values():
        result = geoip.lookup(ip)
        if result.is_private or not result.country_code or result.country_code == "--":
            continue
        city = getattr(result, "city", None)
        if city is None or not city.name:
            n_unplaced += 1
            continue
        places[(result.country_code, _norm(city.subdivision_name), _norm(city.name))] += 1
    return dict(places), n_unplaced


def _resolve_places(places: Iterable[Place], cities_zip: str, admin1_txt: str) -> dict[Place, tuple[float, float]]:
    """Maps each place to (lat, lon) using GeoNames -- see "Geocoding" in the
    module docstring for the lookup order. Streams cities1000 once and only
    keeps rows whose country+name some place actually needs."""
    places = list(places)
    wanted_names: set[tuple[str, str]] = {(cc, city) for cc, _sub, city in places}
    wanted_subs: set[tuple[str, str]] = {(cc, sub) for cc, sub, _city in places if sub}

    admin1_names: dict[str, set[str]] = {}
    with open(admin1_txt, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            code, name, ascii_name = parts[0], parts[1], parts[2]
            admin1_names[code] = {_norm(name), _norm(ascii_name)} - {""}

    # Keep the most populous match for every key -- the usual tie-break for
    # "which Springfield", and at a 2 deg cell size mostly moot anyway.
    by_full: dict[Place, tuple[float, float, int]] = {}
    by_city: dict[tuple[str, str], tuple[float, float, int]] = {}
    sub_acc: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    def _keep_bigger(d: dict, key, value: tuple[float, float, int]) -> None:
        if key not in d or d[key][2] < value[2]:
            d[key] = value

    with zipfile.ZipFile(cities_zip) as z, z.open("cities1000.txt") as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if len(f) < 15:
                continue
            cc = f[8]
            lat, lon, pop = float(f[4]), float(f[5]), int(f[14] or 0)
            subs = admin1_names.get(f"{cc}.{f[10]}", set())
            for sub in subs:
                if (cc, sub) in wanted_subs:
                    # Subdivision centroid, weighted by population so e.g.
                    # an oblast's centroid sits near where people (and so
                    # IPs) actually are, not in empty taiga.
                    w = max(pop, 1)
                    acc = sub_acc[(cc, sub)]
                    acc[0] += lat * w
                    acc[1] += lon * w
                    acc[2] += w
            names = {_norm(f[1]), _norm(f[2])} | {_norm(a) for a in f[3].split(",") if a}
            for name in names:
                if (cc, name) not in wanted_names:
                    continue
                _keep_bigger(by_city, (cc, name), (lat, lon, pop))
                for sub in subs:
                    _keep_bigger(by_full, (cc, sub, name), (lat, lon, pop))

    resolved: dict[Place, tuple[float, float]] = {}
    for place in places:
        cc, sub, city = place
        if place in by_full:
            resolved[place] = by_full[place][:2]
        elif (cc, sub) in sub_acc:
            lat_w, lon_w, w = sub_acc[(cc, sub)]
            resolved[place] = (lat_w / w, lon_w / w)
        elif (cc, city) in by_city:
            resolved[place] = by_city[(cc, city)][:2]
    return resolved


def _cell_of(lat: float, lon: float, cell_deg: float) -> tuple[float, float]:
    """South-west corner of the grid cell containing (lat, lon). Clamped so
    lat=90 / lon=180 exactly don't open a cell off the edge of the map."""
    south = math.floor(min(lat, 90 - 1e-9) / cell_deg) * cell_deg
    west = math.floor(min(lon, 180 - 1e-9) / cell_deg) * cell_deg
    return round(south, 6), round(west, 6)


def aggregate_cells(
    place_counts: dict[Place, int],
    coords: dict[Place, tuple[float, float]],
    cell_deg: float,
    min_visitors: int,
) -> tuple[list[dict], int, int]:
    """Returns (published cells, n_suppressed, n_unresolved). A place maps
    to exactly one cell and a network to exactly one place, so summing
    per-place distinct-network counts gives a cell's distinct-network count
    exactly -- no double counting."""
    cells: dict[tuple[float, float], int] = defaultdict(int)
    n_unresolved = 0
    for place, n in place_counts.items():
        if place not in coords:
            n_unresolved += n
            continue
        cells[_cell_of(*coords[place], cell_deg)] += n
    published = []
    n_suppressed = 0
    for (south, west), n in cells.items():
        if n >= min_visitors:
            published.append({"south": south, "west": west, "visitors": n})
        else:
            n_suppressed += n
    published.sort(key=lambda c: c["visitors"], reverse=True)
    return published, n_suppressed, n_unresolved


def _write_atomic(out_dir: str, payload: dict) -> None:
    path = os.path.join(out_dir, OUTPUT_FILENAME)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.chmod(tmp_path, 0o644)
    os.rename(tmp_path, path)
    logger.info(
        "wrote %s (%d cells shown, %d/%d visitor networks shown)",
        path, len(payload["cells"]), payload["shown_visitors"], payload["total_visitors"],
    )


def build(
    out_dir: str,
    log_file: str,
    window_days: int,
    cell_deg: float,
    min_visitors: int,
    data_dir: str,
    max_data_age_days: int,
) -> None:
    os.makedirs(data_dir, exist_ok=True)
    geoip_path = _ensure_data_file(data_dir, GEOIP_CITY_URL, max_data_age_days)
    cities_path = _ensure_data_file(data_dir, GEONAMES_CITIES_URL, max_data_age_days)
    admin1_path = _ensure_data_file(data_dir, GEONAMES_ADMIN1_URL, max_data_age_days)

    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    networks = _distinct_networks(_iter_local_log_entries(log_file, since))
    geoip = GeoIP2Fast(geoip2fast_data_file=geoip_path)
    place_counts, n_no_city = _geocode_to_places(networks, geoip)
    del networks  # the last reference to any IP in this run

    coords = _resolve_places(place_counts, cities_path, admin1_path)
    cells, n_suppressed, n_unresolved = aggregate_cells(place_counts, coords, cell_deg, min_visitors)

    shown = sum(c["visitors"] for c in cells)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days,
        "cell_deg": cell_deg,
        "min_visitors": min_visitors,
        "total_visitors": shown + n_suppressed + n_unresolved + n_no_city,
        "shown_visitors": shown,
        "suppressed_visitors": n_suppressed,
        "unplaced_visitors": n_unresolved + n_no_city,
        "cells": cells,
    }
    _write_atomic(out_dir, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", required=True, help="directory Apache serves, e.g. ~/public_html/spectra_data")
    parser.add_argument("--log-file", required=True, help="gunicorn's access.log")
    parser.add_argument("--window-days", type=int, default=30, help="trailing window, re-read from scratch every run (keep <= scripts.trim_access_log's --keep-days)")
    parser.add_argument("--cell-deg", type=float, default=2.0, help="grid cell size in degrees (privacy rule 1 -- see module docstring before lowering)")
    parser.add_argument("--min-visitors", type=int, default=5, help="minimum distinct networks for a cell to be shown (privacy rule 2 -- see module docstring before lowering)")
    parser.add_argument("--data-dir", default="~/.cache/spectra_pointer/geo", help="where the geoip2fast city + GeoNames files are cached")
    parser.add_argument("--max-data-age-days", type=int, default=30, help="re-download geolocation data files older than this")
    args = parser.parse_args()

    if args.min_visitors < 2:
        parser.error("--min-visitors below 2 would publish single-visitor cells; see the module docstring")

    out_dir = os.path.expanduser(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o755)

    build(
        out_dir, os.path.expanduser(args.log_file), args.window_days, args.cell_deg, args.min_visitors,
        os.path.expanduser(args.data_dir), args.max_data_age_days,
    )


if __name__ == "__main__":
    main()
