import io
import zipfile
from types import SimpleNamespace

from scripts.build_access_grid import (
    _cell_of,
    _distinct_networks,
    _geocode_to_places,
    _network_key,
    _resolve_places,
    aggregate_cells,
)


def test_network_key_groups_ipv4_by_24_and_ipv6_by_48():
    assert _network_key("131.96.1.7") == _network_key("131.96.1.250") == "131.96.1.0/24"
    assert _network_key("131.96.2.7") != _network_key("131.96.1.7")
    # IPv6 privacy addresses rotating within one site's /48 are one visitor.
    assert _network_key("2001:db8:abcd:1::1") == _network_key("2001:db8:abcd:ffff::99") == "2001:db8:abcd::/48"
    assert _network_key("not-an-ip") is None


def test_distinct_networks_counts_each_network_once():
    entries = [("t", "131.96.1.7"), ("t", "131.96.1.8"), ("t", "131.96.9.1"), ("t", "junk")]
    assert len(_distinct_networks(entries)) == 2


def test_cell_of_snaps_to_southwest_corner_and_clamps_edges():
    assert _cell_of(33.75, -84.39, 2.0) == (32.0, -86.0)
    assert _cell_of(-0.5, 0.5, 2.0) == (-2.0, 0.0)
    assert _cell_of(90.0, 180.0, 2.0) == (88.0, 178.0)


def test_aggregate_cells_suppresses_sparse_cells_and_counts_unresolved():
    atlanta = ("US", "georgia", "atlanta")
    decatur = ("US", "georgia", "decatur")  # same 2 deg cell as Atlanta
    hilo = ("US", "hawaii", "hilo")
    nowhere = ("US", "", "nowhere")
    counts = {atlanta: 3, decatur: 2, hilo: 4, nowhere: 7}
    coords = {atlanta: (33.75, -84.39), decatur: (33.77, -84.30), hilo: (19.7, -155.08)}
    cells, n_suppressed, n_unresolved = aggregate_cells(counts, coords, cell_deg=2.0, min_visitors=5)
    # Atlanta + Decatur pool into one cell that clears the threshold; Hilo's
    # 4 alone don't, so it disappears into the global suppressed total.
    assert cells == [{"south": 32.0, "west": -86.0, "visitors": 5}]
    assert n_suppressed == 4
    assert n_unresolved == 7


def _fake_lookup(table):
    def lookup(ip):
        cc, sub, city = table[ip]
        return SimpleNamespace(
            is_private=cc == "--", country_code=cc,
            city=SimpleNamespace(name=city, subdivision_name=sub),
        )
    return SimpleNamespace(lookup=lookup)


def test_geocode_to_places_normalizes_and_skips_private():
    geoip = _fake_lookup({
        "1.1.1.1": ("DE", "North Rhine-Westphalia", "Köln"),
        "2.2.2.2": ("US", "", ""),
        "10.0.0.1": ("--", "", ""),
    })
    places, n_unplaced = _geocode_to_places({"a": "1.1.1.1", "b": "2.2.2.2", "c": "10.0.0.1"}, geoip)
    assert places == {("DE", "north rhine-westphalia", "koln"): 1}
    assert n_unplaced == 1  # the no-city US IP; the private one isn't counted at all


def _write_geonames(tmp_path, rows, admin1):
    cities = tmp_path / "cities1000.zip"
    lines = []
    for geonameid, (name, ascii_name, alts, lat, lon, cc, a1, pop) in enumerate(rows):
        f = [""] * 19
        f[0], f[1], f[2], f[3], f[4], f[5], f[8], f[10], f[14] = (
            str(geonameid), name, ascii_name, alts, str(lat), str(lon), cc, a1, str(pop),
        )
        lines.append("\t".join(f))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cities1000.txt", "\n".join(lines) + "\n")
    cities.write_bytes(buf.getvalue())
    admin1_path = tmp_path / "admin1CodesASCII.txt"
    admin1_path.write_text("".join(f"{code}\t{name}\t{name}\t0\n" for code, name in admin1.items()))
    return str(cities), str(admin1_path)


def test_resolve_places_prefers_subdivision_match_then_centroid_then_bare_city(tmp_path):
    cities, admin1 = _write_geonames(
        tmp_path,
        [
            ("Athens", "Athens", "", 33.96, -83.38, "US", "GA", 127000),
            ("Athens", "Athens", "", 39.33, -82.10, "US", "OH", 25000),
            ("Atlanta", "Atlanta", "", 33.75, -84.39, "US", "GA", 500000),
            ("Köln", "Koln", "Cologne,Koeln", 50.93, 6.95, "DE", "07", 1000000),
        ],
        {"US.GA": "Georgia", "US.OH": "Ohio", "DE.07": "North Rhine-Westphalia"},
    )
    athens_oh = ("US", "ohio", "athens")
    cologne = ("DE", "north rhine-westphalia", "cologne")  # alternate name
    gwinnett = ("US", "georgia", "not in geonames")  # -> Georgia's centroid
    athens_nosub = ("US", "", "athens")  # -> most populous Athens
    ghost = ("US", "", "nowhere")
    coords = _resolve_places([athens_oh, cologne, gwinnett, athens_nosub, ghost], cities, admin1)
    assert coords[athens_oh] == (39.33, -82.10)
    assert coords[cologne] == (50.93, 6.95)
    lat, lon = coords[gwinnett]
    assert 33.75 < lat < 33.96 and -84.39 < lon < -83.38
    assert coords[athens_nosub] == (33.96, -83.38)
    assert ghost not in coords
