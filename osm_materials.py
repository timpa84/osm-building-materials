"""Fetch OSM buildings with material tags per country; write GeoJSON, summary CSV, dashboard data."""

import argparse
import csv
import json
import math
import re
import time
from collections import defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any

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

Coord = tuple[float, float]  # lon, lat
Ring = list[Coord]
Feature = dict[str, Any]


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


def dashboard_js(name: str, geojson: str) -> str:
    """The GeoJSON as a script, so dashboard.html works from file:// without a server.

    A literal '</script>' inside an OSM tag value would end the script tag early, so '</'
    is escaped as '<\\/' (the same string once JS reads the literal).
    """
    # JSON.parse of one string literal is much faster and lighter than parsing a 60 MB object literal.
    literal = json.dumps(geojson, ensure_ascii=False).replace("</", "<\\/")
    return f'(window.OSM_MATERIALS ??= {{}})["{name}"] = JSON.parse({literal});\n'


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # No list default together with choices: argparse < 3.14 would validate the list itself.
    parser.add_argument("countries", nargs="*", choices=COUNTRIES)
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--refresh", action="store_true", help="refetch even if cached")
    args = parser.parse_args()
    args.out.mkdir(exist_ok=True)

    for iso in args.countries or COUNTRIES:
        name = COUNTRIES[iso]
        print(f"Processing {name}...")
        raw = load_raw(iso, args.out / f"raw_{name}.json", args.refresh)
        features = [f for e in raw["elements"] if (f := to_feature(e))]
        if len(features) < len(raw["elements"]):
            print(
                f"  dropped {len(raw['elements']) - len(features)} elements without a closed ring"
            )
        collection = {
            "type": "FeatureCollection",
            "total_buildings": raw["total_buildings"],
            "features": features,
        }
        geojson = json.dumps(collection, ensure_ascii=False, separators=(",", ":"))
        (args.out / f"materials_{name}.geojson").write_text(geojson, encoding="utf-8")
        (args.out / f"materials_{name}.js").write_text(
            dashboard_js(name, geojson), encoding="utf-8"
        )
        with (args.out / f"summary_{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["key", "material", "buildings", "footprint_m2", "floor_area_m2"])
            writer.writerows(summarize(features))
        print(f"  {len(features)} of {raw['total_buildings']} buildings have material tags")


if __name__ == "__main__":
    main()
