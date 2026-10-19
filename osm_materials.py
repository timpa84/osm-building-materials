"""Fetch OSM buildings with material tags per country; write GeoJSON, summary CSV, web data bundle."""

import argparse
import csv
import json
import math
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TAGINFO_URL = "https://taginfo.geofabrik.de/europe:{name}/api/4/key/stats?key=building"
USER_AGENT = "osm-materials-research/0.1"
COUNTRIES = {"SE": "sweden", "NO": "norway", "DK": "denmark", "FR": "france"}
# Metropolitan France (relation 1403916): the ISO area also spans the overseas territories.
# The Geofabrik "france" extract behind total_buildings() may include the overseas
# departments, which slightly inflates the French denominator.
AREA_OVERRIDES = {"FR": "area(3601403916)"}
MATERIAL_KEYS = (
    "building:material",
    "building:facade:material",
    "building:structure",
    "roof:material",
)
KEPT_KEYS = (*MATERIAL_KEYS, "building", "building:part", "building:levels", "height", "name")
DEFAULT_LEVELS = 2.0  # assumed when building:levels is missing or unparsable
EARTH_RADIUS_M = 6_371_008.8
WEB_DIR = "web"  # data/web/: the bundle dashboard.html loads
CHUNK_MAX = 3000  # a quadtree cell with more features than this is split
CHUNK_MAX_DEPTH = 16  # ... but not deeper, so a pile of identical points cannot recurse forever
CHUNK_DECIMALS = 6  # polygon coordinates in chunks (~0.1 m)
BBOX_DECIMALS = 5  # bounding boxes in the index
POINT_SCALE = 10**5  # point layer: integer units of 1e-5 degrees (~1 m)
SITE_MARKER = ".osm-materials-site"
SITE_HEAD = '<link rel="canonical" href="https://byggnadsbestandsanalys.se/labb/byggnadsmaterial/">'
SITE_BACK = '<a class="back" href="../../">← Byggnadsbeståndsanalys</a>'
DASHBOARD = Path(__file__).with_name("dashboard.html")

Coord = tuple[float, float]  # lon, lat
Ring = list[Coord]
Feature = dict[str, Any]
Bounds = tuple[float, float, float, float]  # west, south, east, north


def build_query(iso: str, timeout: int = 900) -> str:
    keys = "|".join(MATERIAL_KEYS)
    area = AREA_OVERRIDES.get(iso, f"area['ISO3166-1'='{iso}'][admin_level=2]")
    return f"[out:json][timeout:{timeout}];{area}->.a;" + (
        f"(nwr[building][~'^({keys})$'~'.'](area.a);"
        f"nwr['building:part'][~'^({keys})$'~'.'](area.a););"
        "out body geom;"
    )


def ring_area_m2(ring: Ring) -> float:
    """Shoelace area on a local equirectangular projection; accurate for building-sized rings."""
    k = math.radians(1) * EARTH_RADIUS_M
    kx = k * math.cos(math.radians(sum(lat for _, lat in ring) / len(ring)))
    pts = [(lon * kx, lat * k) for lon, lat in ring]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in pairwise(pts))) / 2


def stitch_rings(ways: list[Ring]) -> list[Ring]:
    """Join multipolygon member ways end to end into closed rings; unclosable ones are dropped."""
    remaining = [list(w) for w in ways]
    rings: list[Ring] = []
    while remaining:
        ring, used = remaining.pop(), []
        while ring[0] != ring[-1]:
            nxt = next((w for w in remaining if ring[-1] in (w[0], w[-1])), None)
            if nxt is None:
                remaining.extend(used)  # give the pieces back so sibling rings still close
                break
            remaining.remove(nxt)
            used.append(nxt)
            ring += nxt[1:] if nxt[0] == ring[-1] else nxt[-2::-1]
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def point_in_ring(point: Coord, ring: Ring) -> bool:
    """Ray casting; used to assign each hole to the outer ring that contains it."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in pairwise(ring):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def geometry(element: dict[str, Any]) -> tuple[dict[str, Any], float] | None:
    """GeoJSON geometry and footprint area (m², holes subtracted) of an Overpass element."""

    def ring(geom: list[dict[str, float]]) -> Ring:
        return [(p["lon"], p["lat"]) for p in geom]

    if element["type"] == "node":
        return {"type": "Point", "coordinates": [element["lon"], element["lat"]]}, 0.0
    if element["type"] == "way":
        outers, inners = stitch_rings([ring(element["geometry"])]), []
    else:
        # Only multipolygon roles: a type=building relation's "outline"/"part" members
        # would otherwise be summed as separate footprints.
        members = [m for m in element["members"] if m["type"] == "way" and "geometry" in m]
        outers = stitch_rings([ring(m["geometry"]) for m in members if m["role"] in ("outer", "")])
        inners = stitch_rings([ring(m["geometry"]) for m in members if m["role"] == "inner"])
    if not outers:
        return None
    area = sum(map(ring_area_m2, outers)) - sum(map(ring_area_m2, inners))
    polygons = [[o] for o in outers]
    for hole in inners:
        polygon = next((p for p in polygons if point_in_ring(hole[0], p[0])), polygons[0])
        polygon.append(hole)
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}, area
    return {"type": "MultiPolygon", "coordinates": polygons}, area


def parse_levels(value: str | None) -> float | None:
    """'2' -> 2.0, '2;3' -> 2.0 (first value, like normalize()), '0' -> 0.0, junk -> None."""
    try:
        levels = float(normalize(value or ""))
    except ValueError:
        return None
    return levels if 0 <= levels < 200 else None


def to_feature(element: dict[str, Any]) -> Feature | None:
    geom = geometry(element)
    if geom is None:
        return None
    tags: dict[str, str] = element.get("tags", {})
    props: dict[str, Any] = {k: tags[k] for k in KEPT_KEYS if k in tags}
    levels = parse_levels(tags.get("building:levels"))
    props["levels"] = DEFAULT_LEVELS if levels is None else levels
    props["levels_assumed"] = levels is None
    props["footprint_m2"] = round(geom[1], 1)
    props["floor_area_m2"] = round(geom[1] * props["levels"], 1)
    props["osm_id"] = f"{element['type']}/{element['id']}"
    return {"type": "Feature", "geometry": geom[0], "properties": props}


def normalize(value: str) -> str:
    """First of multiple values, lower-cased: 'Wood; metal' -> 'wood'."""
    return re.split(r"[;,/]", value)[0].strip().lower().replace(" ", "_")


def summarize(features: list[Feature]) -> list[tuple[str, str, int, int, int]]:
    """Rows of (key, material, buildings, footprint m², floor area m²), largest floor area first."""
    totals: defaultdict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    for f in features:
        props = f["properties"]
        for key in MATERIAL_KEYS:
            if key in props:
                row = totals[(key, normalize(props[key]))]
                row[0] += 1
                row[1] += props["footprint_m2"]
                row[2] += props["floor_area_m2"]
    rows = [(*k, int(n), round(fp), round(fl)) for k, (n, fp, fl) in totals.items()]
    return sorted(rows, key=lambda r: -r[4])


def json_literal(value: Any) -> str:
    """JS expression for a JSON value, so the data loads from a script tag on file:// (no fetch).

    JSON.parse of one string literal is much faster and lighter than parsing a huge object
    literal. A literal '</script>' inside an OSM tag value would end the script tag early, so
    '</' is escaped as '<\\/' (the same string once JS reads the literal).
    """
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    literal = json.dumps(text, ensure_ascii=False).replace("</", "<\\/")
    return f"JSON.parse({literal})"


def read_index(web: Path) -> dict[str, Any]:
    """The index object back out of web/index.js (the inverse of json_literal)."""
    text = (web / "index.js").read_text(encoding="utf-8")
    literal = text[text.index("JSON.parse(") + len("JSON.parse(") : text.rindex(");")]
    return json.loads(json.loads(literal))  # '<\\/' is a valid JSON escape for '</'


def overpass(query: str, retries: int = 8) -> dict[str, Any]:
    for attempt in range(retries):
        r = httpx.post(
            OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=1000
        )
        # Overpass reports rate limiting as 429/504, and runtime errors as a "remark".
        if r.status_code == 200:
            body = r.json()
            if "remark" not in body:
                return body
        wait = 30 * (attempt + 1)
        print(f"  HTTP {r.status_code}, retrying in {wait}s")
        time.sleep(wait)
    raise RuntimeError("Overpass failed")


def total_buildings(name: str) -> int:
    """All objects with a building tag in the country's Geofabrik extract (cheap, unlike Overpass)."""
    r = httpx.get(TAGINFO_URL.format(name=name), headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    return next(d["count"] for d in r.json()["data"] if d["type"] == "all")


def load_raw(iso: str, path: Path, refresh: bool) -> dict[str, Any]:
    """Tagged buildings plus the country's total building count, cached on disk."""
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    raw = {
        "elements": overpass(build_query(iso))["elements"],
        "total_buildings": total_buildings(COUNTRIES[iso]),
    }
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return raw


class Record(NamedTuple):
    """What the index and the point layer need of a feature; its geometry only goes to a chunk."""

    center: Coord
    kind: str  # building type, "part" for a bare building:part
    materials: tuple[str, ...]  # normalized value per MATERIAL_KEYS, "" when missing
    assumed: bool
    footprint: float
    floor: float


def centroid(feature: Feature) -> Coord:
    """A Point's own coordinates, else the vertex average of the (first) polygon's outer ring."""
    geom = feature["geometry"]
    if geom["type"] == "Point":
        lon, lat = geom["coordinates"]
        return lon, lat
    ring = geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0]
    vertices = ring[:-1]  # without the closing vertex
    return (
        sum(v[0] for v in vertices) / len(vertices),
        sum(v[1] for v in vertices) / len(vertices),
    )


def building_type(props: dict[str, Any]) -> str:
    """The `building` value; 'part' for building:part objects (missing, empty or 'no')."""
    building = props.get("building")
    return building if building and building != "no" else "part"


def material_values(props: dict[str, Any]) -> dict[str, str]:
    """Normalized value per material key; keys that are absent or normalize to '' are left out."""
    values = {key: normalize(props[key]) for key in MATERIAL_KEYS if key in props}
    return {key: value for key, value in values.items() if value}


def to_record(feature: Feature) -> Record:
    props = feature["properties"]
    values = material_values(props)
    return Record(
        centroid(feature),
        building_type(props),
        tuple(values.get(key, "") for key in MATERIAL_KEYS),
        props["levels_assumed"],
        props["footprint_m2"],
        props["floor_area_m2"],
    )


def leaf_cells(centers: Sequence[Coord]) -> list[tuple[str, list[int]]]:
    """Quadtree over the centers: (path, point indices in input order) of every non-empty leaf.

    The root cell is the bounding box of the centers. A cell splits in four, digit
    2 * north + east, while it holds more than CHUNK_MAX points (at most CHUNK_MAX_DEPTH deep).
    The root's path is "".
    """
    if not centers:
        return []
    lons, lats = zip(*centers)
    leaves: list[tuple[str, list[int]]] = []

    def split(path: str, bounds: Bounds, indices: list[int]) -> None:
        if len(indices) <= CHUNK_MAX or len(path) >= CHUNK_MAX_DEPTH:
            leaves.append((path, indices))
            return
        w, s, e, n = bounds
        mx, my = (w + e) / 2, (s + n) / 2
        quarters: list[list[int]] = [[], [], [], []]
        for i in indices:
            lon, lat = centers[i]
            quarters[2 * (lat >= my) + (lon >= mx)].append(i)
        boxes = ((w, s, mx, my), (mx, s, e, my), (w, my, mx, n), (mx, my, e, n))
        for digit, (box, part) in enumerate(zip(boxes, quarters)):
            if part:
                split(f"{path}{digit}", box, part)

    split("", (min(lons), min(lats), max(lons), max(lats)), list(range(len(centers))))
    return leaves


def round_coords(coords: Any) -> Any:
    """GeoJSON coordinates of any nesting rounded to CHUNK_DECIMALS."""
    if isinstance(coords[0], list):
        return [round_coords(part) for part in coords]
    return [round(c, CHUNK_DECIMALS) for c in coords]


def positions(coords: Any) -> Iterator[list[float]]:
    """Every [lon, lat] position of GeoJSON coordinates of any nesting."""
    if isinstance(coords[0], list):
        for part in coords:
            yield from positions(part)
    else:
        yield coords


def outward(bounds: Bounds) -> list[float]:
    """CHUNK_DECIMALS bounds rounded outward to BBOX_DECIMALS, so they still contain them."""
    step = 10 ** (CHUNK_DECIMALS - BBOX_DECIMALS)
    scale = 10**BBOX_DECIMALS
    w, s, e, n = (round(v * 10**CHUNK_DECIMALS) for v in bounds)
    return [w // step / scale, s // step / scale, -(-e // step) / scale, -(-n // step) / scale]


def web_feature(feature: Feature, country: str, record: Record) -> Feature:
    """The feature as a chunk stores it: rounded, plus country, type and normalized `n:` tags."""
    props = {
        **feature["properties"],
        "country": country,
        "type": record.kind,
        **{f"n:{key}": value for key, value in zip(MATERIAL_KEYS, record.materials) if value},
    }
    geom = feature["geometry"]
    geometry = {**geom, "coordinates": round_coords(geom["coordinates"])}
    return {"type": "Feature", "geometry": geometry, "properties": props}


def write_script(path: Path, js: str) -> None:
    path.write_text(js, encoding="utf-8", newline="\n")  # LF everywhere: same bytes on any OS


def write_chunks(
    country: str, features: list[Feature], records: list[Record], web: Path
) -> list[list[Any]]:
    """Write the country's polygon chunks; returns their index rows [id, w, s, e, n, count]."""
    rows: list[list[Any]] = []
    for path, indices in leaf_cells([r.center for r in records]):
        chunk_id = f"{country}-{path or 'r'}"
        chunk = [web_feature(features[i], country, records[i]) for i in indices]
        verts = [p for f in chunk for p in positions(f["geometry"]["coordinates"])]
        bounds = (
            min(p[0] for p in verts),
            min(p[1] for p in verts),
            max(p[0] for p in verts),
            max(p[1] for p in verts),
        )
        collection = {"type": "FeatureCollection", "features": chunk}
        js = f"window.OSM_CHUNK({json.dumps(chunk_id)}, {json_literal(collection)});\n"
        write_script(web / "chunks" / f"{chunk_id}.js", js)
        rows.append([chunk_id, *outward(bounds), len(chunk)])
    return rows


def spread_bits(x: int) -> int:
    """Put a zero bit between the bits of x (up to 32 bits), for interleaving."""
    x = (x | x << 16) & 0x0000FFFF0000FFFF
    x = (x | x << 8) & 0x00FF00FF00FF00FF
    x = (x | x << 4) & 0x0F0F0F0F0F0F0F0F
    x = (x | x << 2) & 0x3333333333333333
    return (x | x << 1) & 0x5555555555555555


def delta_encode(values: list[int]) -> list[int]:
    """First value absolute, then each value as the difference to its predecessor."""
    return values[:1] + [b - a for a, b in pairwise(values)]


def point_columns(records: list[Record], codes: dict[str, int]) -> dict[str, list[int]]:
    """Columnar centroids (delta-encoded, Z-ordered) and material codes of a country."""
    quantized = [
        (round(r.center[0] * POINT_SCALE), round(r.center[1] * POINT_SCALE)) for r in records
    ]
    # Morton key of the shifted (non-negative) coordinates: nearby points get nearby keys.
    keys = [
        spread_bits(lon + 180 * POINT_SCALE) | spread_bits(lat + 90 * POINT_SCALE) << 1
        for lon, lat in quantized
    ]
    order = sorted(range(len(records)), key=keys.__getitem__)
    columns = {
        "lon": delta_encode([quantized[i][0] for i in order]),
        "lat": delta_encode([quantized[i][1] for i in order]),
    }
    for n in range(len(MATERIAL_KEYS)):
        columns[f"c{n}"] = [codes[records[i].materials[n]] for i in order]
    return columns


def mtime_date(path: Path) -> str:
    """UTC date a file was last written, ISO formatted."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date().isoformat()


def write_web(paths: dict[str, Path], web: Path) -> None:
    """Write the bundle for the given country GeoJSON files into the (empty) folder web."""
    (web / "chunks").mkdir(parents=True)
    records: dict[str, list[Record]] = {}
    country: dict[str, dict[str, Any]] = {}
    chunks: list[list[Any]] = []
    fetched: list[str] = []
    for name, path in paths.items():
        collection = json.loads(path.read_text(encoding="utf-8"))
        features: list[Feature] = collection["features"]
        if not features:
            raise SystemExit(f"{path} has no features.")
        records[name] = [to_record(f) for f in features]
        lons, lats = zip(*(r.center for r in records[name]))
        bbox = (min(lons), min(lats), max(lons), max(lats))
        country[name] = {
            "total_buildings": collection["total_buildings"],
            "features": len(features),
            "bbox": [round(v, BBOX_DECIMALS) for v in bbox],
        }
        chunks += write_chunks(name, features, records[name], web)
        if "fetched" in collection:
            fetched.append(collection["fetched"])

    all_records = [r for rs in records.values() for r in rs]
    materials = sorted({m for r in all_records for m in r.materials} - {""})
    types = sorted({r.kind for r in all_records})
    codes = {"": 0, **{m: i + 1 for i, m in enumerate(materials)}}  # 0 = tag missing
    type_index = {t: i for i, t in enumerate(types)}

    sums: defaultdict[tuple[int, ...], list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    for ci, name in enumerate(paths):
        for r in records[name]:
            row = sums[(ci, type_index[r.kind], *(codes[m] for m in r.materials), int(r.assumed))]
            row[0] += 1
            row[1] += r.footprint
            row[2] += r.floor
    index = {
        "fetched": max(fetched, default=None) or max(mtime_date(p) for p in paths.values()),
        "countries": list(paths),
        "country": country,
        "keys": list(MATERIAL_KEYS),
        "materials": materials,
        "types": types,
        "stats": [
            [*group, int(n), round(footprint, 1), round(floor, 1)]
            for group, (n, footprint, floor) in sorted(sums.items())
        ],
        "chunks": sorted(chunks),
    }
    write_script(web / "index.js", f"window.OSM_WEB = {json_literal(index)};\n")
    points = {name: point_columns(records[name], codes) for name in paths}
    write_script(web / "points.js", f"window.OSM_POINTS = {json_literal(points)};\n")


def build_web(data: Path) -> Path:
    """Rebuild data/web/ (index, point layer, polygon chunks) from every materials_*.geojson."""
    paths = {name: data / f"materials_{name}.geojson" for name in COUNTRIES.values()}
    paths = {name: path for name, path in paths.items() if path.exists()}
    if not paths:
        raise SystemExit(f"No materials_<country>.geojson in {data}; run without --web first.")
    # Built aside and swapped in by renames, so a failed build keeps the previous bundle. Deleting
    # the old bundle first could half-fail on Windows (a file held open by a browser) and lose it.
    web, staging, old = data / WEB_DIR, data / f".{WEB_DIR}-build", data / f".{WEB_DIR}-old"
    for leftover in (staging, old):
        shutil.rmtree(leftover, ignore_errors=True)
    try:
        write_web(paths, staging)
        if web.exists():
            web.replace(old)  # fails loudly, changing nothing, if web cannot be moved
        try:
            staging.replace(web)
        except OSError:
            if old.exists():
                old.replace(web)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)  # a locked leftover is harmless and removed next build
    return web


DIST_NAME = "osm-materials-dashboard"
DIST_README = """OSM building materials - dashboard
===================================

Open dashboard.html in Chrome, Edge or Firefox (double-click; no installation or server).
Keep the data folder next to it. An internet connection is needed for the base map.

- Language: English by default; switch with the SV/EN buttons (or dashboard.html?lang=sv).
- Pick a country, a material tag (facade, roof, structure) and a measure; click a material
  bar to filter the map. Click a building for its tags.
- Street view: click a building and choose "Show in street view", or zoom in and click a
  street. The screen splits in two: Google Street View on one side, the camera and its view
  cone on the map. The camera always stands on a street from which the building can be
  seen; where there is none, no image is shown. Click another building to re-aim, drag the
  divider to resize. The cone shows the initial direction only.
- data/web/ holds the building data (an index, a point layer and the building outlines in
  chunks that load as you zoom in); data/summary_<country>.csv holds the totals per tag
  and material.

Floor area = footprint x building:levels (2 levels assumed when the tag is missing).
Tagging in OpenStreetMap is incomplete: the figures show what is mapped, not the whole stock.

Data (c) OpenStreetMap contributors, ODbL 1.0 - https://www.openstreetmap.org/copyright
Fetched via the Overpass API on {fetched}.
"""


def require_bundle(data: Path) -> Path:
    """data/web/, which must have been built (--web) before it can be packaged."""
    web = data / WEB_DIR
    if not (web / "index.js").exists():
        raise SystemExit(f"No {web / 'index.js'}; run osm_materials.py --web first.")
    return web


def build_dist(data: Path, dist: Path, dashboard: Path = DASHBOARD) -> Path:
    """Self-contained copy of the dashboard for sharing: dist/<DIST_NAME>/ and a zip of it."""
    web = require_bundle(data)
    folder = dist / DIST_NAME
    if folder.exists():
        shutil.rmtree(folder)
    (folder / "data").mkdir(parents=True)
    shutil.copy2(dashboard, folder / "dashboard.html")
    shutil.copytree(web, folder / "data" / WEB_DIR)
    for path in sorted(data.glob("summary_*.csv")):
        shutil.copy2(path, folder / "data")
    readme = DIST_README.format(fetched=read_index(web)["fetched"])
    (folder / "README.txt").write_text(readme, encoding="utf-8")
    return Path(shutil.make_archive(str(folder), "zip", root_dir=dist, base_dir=DIST_NAME))


def build_site(data: Path, site: Path, dashboard: Path = DASHBOARD) -> Path:
    """Website copy of the dashboard: site/index.html and site/data/web/, marked as ours.

    An existing non-empty folder is only emptied when it carries the marker of an earlier build.
    """
    web = require_bundle(data)
    if site.exists() and any(site.iterdir()):
        if not (site / SITE_MARKER).exists():
            raise SystemExit(f"{site} is not empty and was not made by --site; not touching it.")
        for child in site.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    (site / "data").mkdir(parents=True, exist_ok=True)
    html = dashboard.read_text(encoding="utf-8")
    html = html.replace("<!-- site:head -->", SITE_HEAD).replace("<!-- site:back -->", SITE_BACK)
    write_script(site / "index.html", html)
    shutil.copytree(web, site / "data" / WEB_DIR)
    write_script(site / SITE_MARKER, "Written by osm_materials.py --site; rebuilt in place.\n")
    return site


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # No list default together with choices: argparse < 3.14 would validate the list itself.
    parser.add_argument("countries", nargs="*", choices=COUNTRIES)
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--refresh", action="store_true", help="refetch even if cached")
    parser.add_argument(
        "--web", action="store_true", help="only rebuild data/web/ from the existing GeoJSON"
    )
    parser.add_argument("--dist", action="store_true", help="only package dist/ for sharing")
    parser.add_argument(
        "--site", type=Path, metavar="DIR", help="only write a website copy of the dashboard"
    )
    args = parser.parse_args()
    args.out.mkdir(exist_ok=True)
    if args.web or args.dist or args.site:
        if args.web:
            print(f"Wrote {build_web(args.out)}")
        if args.dist:
            print(f"Wrote {build_dist(args.out, Path('dist'))}")
        if args.site:
            print(f"Wrote {build_site(args.out, args.site)}")
        return

    for iso in args.countries or COUNTRIES:
        name = COUNTRIES[iso]
        print(f"Processing {name}...")
        raw_path = args.out / f"raw_{name}.json"
        raw = load_raw(iso, raw_path, args.refresh)
        features = [f for e in raw["elements"] if (f := to_feature(e))]
        if len(features) < len(raw["elements"]):
            print(
                f"  dropped {len(raw['elements']) - len(features)} elements without a closed ring"
            )
        collection = {
            "type": "FeatureCollection",
            # The cache is written right after fetching, so its date is the fetch date.
            "fetched": mtime_date(raw_path),
            "total_buildings": raw["total_buildings"],
            "features": features,
        }
        geojson = json.dumps(collection, ensure_ascii=False, separators=(",", ":"))
        (args.out / f"materials_{name}.geojson").write_text(geojson, encoding="utf-8")
        with (args.out / f"summary_{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["key", "material", "buildings", "footprint_m2", "floor_area_m2"])
            writer.writerows(summarize(features))
        print(f"  {len(features)} of {raw['total_buildings']} buildings have material tags")
    print(f"Wrote {build_web(args.out)}")


if __name__ == "__main__":
    main()
