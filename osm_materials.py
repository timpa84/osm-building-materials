"""Fetch OSM objects with material tags per country and write GeoJSON + summary CSV."""

import argparse
import csv
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "osm-materials-research/0.1"
COUNTRIES = {"SE": "sweden", "NO": "norway", "DK": "denmark"}
MATERIAL_KEY = re.compile(r"^(.*:)?material$")
# Keys that say what kind of object it is, in priority order.
FEATURE_KEYS = (
    "power", "bridge", "man_made", "building", "barrier", "historic", "amenity",
    "leisure", "tourism", "railway", "waterway", "pipeline", "highway", "natural",
    "landuse",
)  # fmt: skip

Feature = dict[str, Any]


def build_query(iso: str, timeout: int = 900) -> str:
    return (
        f"[out:json][timeout:{timeout}];"
        f"area['ISO3166-1'='{iso}'][admin_level=2]->.a;"
        "nwr[~'^(.*:)?material$'~'.'](area.a);"
        "out tags center;"
    )


def feature_type(tags: dict[str, str]) -> str:
    """Main 'key=value' describing the object, e.g. 'power=pole'."""
    for key in FEATURE_KEYS:
        if key in tags and tags[key] != "no":
            return f"{key}={tags[key]}"
    return "other"


def to_feature(element: dict[str, Any]) -> Feature | None:
    pos = element if element["type"] == "node" else element.get("center")
    if pos is None:
        return None
    tags: dict[str, str] = element.get("tags", {})
    props = {k: v for k, v in tags.items() if MATERIAL_KEY.match(k) or k in FEATURE_KEYS}
    if "name" in tags:
        props["name"] = tags["name"]
    props["feature"] = feature_type(tags)
    props["osm_url"] = f"https://www.openstreetmap.org/{element['type']}/{element['id']}"
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [pos["lon"], pos["lat"]]},
        "properties": props,
    }


def summarize(features: list[Feature]) -> list[tuple[str, str, str, int]]:
    """Rows of (feature, material key, material value, count), most common first."""
    counts: Counter[tuple[str, str, str]] = Counter()
    for f in features:
        props = f["properties"]
        for key, value in props.items():
            if MATERIAL_KEY.match(key):
                counts[(props["feature"], key, value)] += 1
    return [(*k, n) for k, n in counts.most_common()]


def fetch(iso: str, retries: int = 6) -> list[dict[str, Any]]:
    for attempt in range(retries):
        r = httpx.post(
            OVERPASS_URL,
            data={"data": build_query(iso)},
            headers={"User-Agent": USER_AGENT},
            timeout=1000,
        )
        # Overpass reports rate limiting as 429/504, and runtime errors as a "remark".
        if r.status_code == 200 and "remark" not in r.json():
            return r.json()["elements"]
        wait = 60 * (attempt + 1)
        print(f"  {iso}: HTTP {r.status_code}, retrying in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"Overpass failed for {iso}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("countries", nargs="*", default=list(COUNTRIES), choices=COUNTRIES)
    parser.add_argument("--out", type=Path, default=Path("data"))
    args = parser.parse_args()
    args.out.mkdir(exist_ok=True)

    for iso in args.countries:
        name = COUNTRIES[iso]
        print(f"Fetching {name}...")
        features = [f for e in fetch(iso) if (f := to_feature(e))]
        collection = {"type": "FeatureCollection", "features": features}
        (args.out / f"materials_{name}.geojson").write_text(
            json.dumps(collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        with (args.out / f"summary_{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["feature", "key", "value", "count"])
            writer.writerows(summarize(features))
        print(f"  {len(features)} objects")


if __name__ == "__main__":
    main()
