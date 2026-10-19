import json
import os
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from itertools import accumulate
from pathlib import Path
from typing import Any

import pytest

import osm_materials
from osm_materials import (
    CHUNK_MAX,
    CHUNK_MAX_DEPTH,
    DIST_NAME,
    MATERIAL_KEYS,
    SITE_BACK,
    SITE_HEAD,
    SITE_MARKER,
    Feature,
    build_dist,
    build_query,
    build_site,
    build_web,
    building_type,
    centroid,
    delta_encode,
    geometry,
    json_literal,
    leaf_cells,
    main,
    material_values,
    normalize,
    outward,
    parse_levels,
    point_in_ring,
    positions,
    ring_area_m2,
    round_coords,
    spread_bits,
    stitch_rings,
    summarize,
    to_feature,
)

# ~100 m x 100 m square at the equator (0.0009 deg = 100.08 m).
D = 0.0009
SQUARE = [(0.0, 0.0), (D, 0.0), (D, D), (0.0, D), (0.0, 0.0)]
HOLE = [(D / 4, D / 4), (D / 2, D / 4), (D / 2, D / 2), (D / 4, D / 2), (D / 4, D / 4)]


def overpass_geom(ring: list[tuple[float, float]]) -> list[dict[str, float]]:
    return [{"lon": lon, "lat": lat} for lon, lat in ring]


def test_ring_area_of_square() -> None:
    assert ring_area_m2(SQUARE) == pytest.approx(100.08**2, rel=1e-3)


def test_ring_area_shrinks_with_latitude() -> None:
    at_60 = [(lon, lat + 60) for lon, lat in SQUARE]
    assert ring_area_m2(at_60) == pytest.approx(ring_area_m2(SQUARE) / 2, rel=1e-3)


def test_stitch_joins_reversed_segments_and_drops_open_ones() -> None:
    a, b, c, d = SQUARE[:4]
    assert stitch_rings([[a, b, c], [a, d, c]]) == [[a, d, c, b, a]]
    assert stitch_rings([[a, b, c]]) == []


def test_stitch_failed_ring_does_not_consume_sibling_pieces() -> None:
    a, b, c, d = SQUARE[:4]
    far = (5.0, 5.0)
    # The open chain [far, a] + [a, b, c] can never close; the square must still be stitched.
    rings = stitch_rings([[a, b, c], [c, d, a], [far, a]])
    assert len(rings) == 1
    assert set(rings[0]) == {a, b, c, d}


def test_point_in_ring() -> None:
    assert point_in_ring((D / 2, D / 2), SQUARE)
    assert not point_in_ring((2 * D, D / 2), SQUARE)


def test_multipolygon_holes_are_assigned_to_their_outer() -> None:
    shift = 3 * D
    square2 = [(lon + shift, lat) for lon, lat in SQUARE]
    hole2 = [(lon + shift, lat) for lon, lat in HOLE]
    element = {
        "type": "relation",
        "id": 2,
        "members": [
            {"type": "way", "role": "outer", "geometry": overpass_geom(SQUARE)},
            {"type": "way", "role": "outer", "geometry": overpass_geom(square2)},
            {"type": "way", "role": "inner", "geometry": overpass_geom(hole2)},
        ],
    }
    result = geometry(element)
    assert result is not None
    geom, area = result
    assert geom["type"] == "MultiPolygon"
    assert sorted(len(p) for p in geom["coordinates"]) == [1, 2]
    with_hole = next(p for p in geom["coordinates"] if len(p) == 2)
    assert with_hole[0][0] == square2[0] and with_hole[1] == hole2
    assert area == pytest.approx(ring_area_m2(SQUARE) * 2 - ring_area_m2(HOLE), rel=1e-3)


def test_building_relation_parts_are_not_summed() -> None:
    element = {
        "type": "relation",
        "id": 3,
        "tags": {"type": "building"},
        "members": [
            {"type": "way", "role": "outline", "geometry": overpass_geom(SQUARE)},
            {"type": "way", "role": "part", "geometry": overpass_geom(HOLE)},
        ],
    }
    assert geometry(element) is None


def test_way_becomes_polygon_with_areas() -> None:
    element = {
        "type": "way",
        "id": 7,
        "geometry": overpass_geom(SQUARE),
        "tags": {
            "building": "house",
            "building:material": "wood",
            "building:levels": "3",
            "x": "y",
        },
    }
    f = to_feature(element)
    assert f is not None
    assert f["geometry"]["type"] == "Polygon"
    props = f["properties"]
    assert props["osm_id"] == "way/7"
    assert "x" not in props
    assert props["footprint_m2"] == pytest.approx(10016, abs=5)
    assert props["levels"] == 3
    assert props["levels_assumed"] is False
    assert props["floor_area_m2"] == pytest.approx(3 * props["footprint_m2"], abs=0.2)


def test_missing_levels_default_to_two() -> None:
    f = to_feature({"type": "way", "id": 8, "geometry": overpass_geom(SQUARE), "tags": {}})
    assert f is not None
    props = f["properties"]
    assert props["levels"] == 2
    assert props["levels_assumed"] is True
    assert props["floor_area_m2"] == pytest.approx(2 * props["footprint_m2"], abs=0.2)


def test_relation_subtracts_inner_ring() -> None:
    hole = HOLE
    element = {
        "type": "relation",
        "id": 1,
        "members": [
            {"type": "way", "role": "outer", "geometry": overpass_geom(SQUARE[:3])},
            {"type": "way", "role": "outer", "geometry": overpass_geom(SQUARE[2:])},
            {"type": "way", "role": "inner", "geometry": overpass_geom(hole)},
            {"type": "node", "role": "entrance"},
        ],
    }
    result = geometry(element)
    assert result is not None
    geom, area = result
    assert geom["type"] == "Polygon"
    assert len(geom["coordinates"]) == 2
    assert area == pytest.approx(ring_area_m2(SQUARE) * 15 / 16, rel=1e-3)


def test_node_has_no_area_and_unclosed_way_is_skipped() -> None:
    f = to_feature({"type": "node", "id": 1, "lon": 1.0, "lat": 2.0, "tags": {"building": "yes"}})
    assert f is not None
    assert f["properties"]["footprint_m2"] == 0
    assert f["properties"]["floor_area_m2"] == 0
    assert to_feature({"type": "way", "id": 2, "geometry": overpass_geom(SQUARE[:3])}) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2", 2.0), ("2.5", 2.5), (None, None), ("2;3", 2.0), ("0", 0.0), ("-1", None), ("x", None)],
)
def test_parse_levels(value: str | None, expected: float | None) -> None:
    assert parse_levels(value) == expected


def test_normalize() -> None:
    assert normalize("Wood; metal") == "wood"
    assert normalize("metal sheet") == "metal_sheet"


def test_summarize_sums_areas_per_key_and_material() -> None:
    features = [
        {"properties": {"building:material": "wood", "footprint_m2": 100, "floor_area_m2": 200}},
        {"properties": {"building:material": "Wood", "footprint_m2": 50, "floor_area_m2": 100}},
        {"properties": {"roof:material": "metal", "footprint_m2": 500, "floor_area_m2": 500}},
    ]
    assert summarize(features) == [
        ("roof:material", "metal", 1, 500, 500),
        ("building:material", "wood", 2, 150, 300),
    ]


def test_build_query_area_selection() -> None:
    assert "area['ISO3166-1'='SE'][admin_level=2]->.a;" in build_query("SE")
    assert "area(3601403916)->.a;" in build_query("FR")


# ---- web bundle ---------------------------------------------------------------------------------

WEB_SCRIPT_END = ");\n"
Totals = defaultdict[tuple[str, str, str], list[float]]  # (country, key, material) -> sums


def square_at(lon: float, lat: float, size: float = 0.001) -> dict[str, Any]:
    ring = [[lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat]]
    return {"type": "Polygon", "coordinates": [ring]}


def point_at(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def make_feature(
    osm_id: str,
    geom: dict[str, Any],
    tags: dict[str, str] | None = None,
    footprint: float = 100.0,
    assumed: bool = False,
) -> Feature:
    props: dict[str, Any] = {**(tags or {})}
    props |= {"levels": 2.0, "levels_assumed": assumed, "footprint_m2": footprint}
    props |= {"floor_area_m2": footprint * 2, "osm_id": osm_id}
    return {"type": "Feature", "geometry": geom, "properties": props}


def write_geojson(
    data: Path, name: str, features: list[Feature], fetched: str | None = "2026-09-18"
) -> Path:
    collection: dict[str, Any] = {"type": "FeatureCollection"}
    if fetched:
        collection["fetched"] = fetched
    collection |= {"total_buildings": 1000, "features": features}
    path = data / f"materials_{name}.geojson"
    path.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return path


def small_features() -> dict[str, list[Feature]]:
    """Two countries: several keys, missing keys, assumed levels, building=no/''/missing."""
    first = square_at(12.3456789, 56.7890123)
    multi = {
        "type": "MultiPolygon",
        "coordinates": [first["coordinates"], square_at(13, 57)["coordinates"]],
    }
    return {
        "sweden": [
            make_feature(
                "way/1",
                first,
                {"building": "house", "building:material": "Wood; metal", "roof:material": "Tiles"},
                footprint=100.4,
                assumed=True,
            ),
            make_feature(
                "way/2",
                square_at(12.5, 56.8),
                {
                    "building": "no",
                    "building:facade:material": "Brick",
                    "building:material": "wood",
                },
                footprint=60.1,
            ),
            make_feature("way/3", multi, {"roof:material": "tiles"}, footprint=50.25),
            make_feature(
                "node/4",
                point_at(11.5, 58.25),
                {"building": "yes", "building:structure": "Wood"},
                0.0,
            ),
            make_feature(
                "way/5",
                square_at(15.0, 59.0),
                {"building": "", "building:material": ";", "name": "Gårdsbyggnad"},
                footprint=80.0,
                assumed=True,
            ),
        ],
        "norway": [
            make_feature(
                "way/6",
                square_at(10.1, 60.1),
                {
                    "building": "house",
                    "building:material": "wood",
                    "building:structure": "concrete",
                },
                footprint=120.0,
                assumed=True,
            ),
            make_feature(
                "node/7", point_at(5.25, 62.5), {"building": "cabin", "roof:material": "turf"}, 0.0
            ),
        ],
    }


@pytest.fixture
def small_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    for name, features in small_features().items():
        write_geojson(data, name, features)
    build_web(data)
    return data


def read_script(path: Path, prefix: str, end: str = WEB_SCRIPT_END) -> Any:
    """The JSON value of a generated script; also pins the exact wrapper around it."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith(f"{prefix}JSON.parse(")
    assert text.endswith(end)
    return json.loads(json.loads(text[len(prefix) + len("JSON.parse(") : -len(end)]))


def read_web_index(data: Path) -> dict[str, Any]:
    return read_script(data / "web" / "index.js", "window.OSM_WEB = ")


def read_chunk(web: Path, chunk_id: str) -> list[Feature]:
    prefix = f'window.OSM_CHUNK("{chunk_id}", '
    return read_script(web / "chunks" / f"{chunk_id}.js", prefix, ")" + WEB_SCRIPT_END)["features"]


def snapshot(folder: Path) -> dict[str, bytes]:
    files = (p for p in folder.rglob("*") if p.is_file())
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in files}


def add_to(totals: Totals, group: tuple[str, str, str], count: float, fp: float, fl: float) -> None:
    total = totals[group]
    total[0] += count
    total[1] += fp
    total[2] += fl


def vertex_center(f: Feature) -> tuple[float, float]:
    geom = f["geometry"]
    if geom["type"] == "Point":
        return geom["coordinates"][0], geom["coordinates"][1]
    ring = (geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0])[:-1]
    return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)


def interleave(x: int, y: int) -> int:
    return sum(((x >> i) & 1) << (2 * i) | ((y >> i) & 1) << (2 * i + 1) for i in range(32))


def test_centroid_rules() -> None:
    assert centroid(make_feature("n/1", point_at(1.5, 2.5))) == (1.5, 2.5)
    # Vertex average without the closing vertex, not the area centroid.
    ring = [[0.0, 0.0], [4.0, 0.0], [4.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
    polygon = {"type": "Polygon", "coordinates": [ring]}
    assert centroid(make_feature("w/1", polygon)) == (2.0, 0.5)
    multi = {"type": "MultiPolygon", "coordinates": [[ring], [[[9.0, 9.0]] * 4]]}
    assert centroid(make_feature("r/1", multi)) == (2.0, 0.5)


def test_building_type_rule() -> None:
    assert building_type({"building": "house"}) == "house"
    assert building_type({"building": "no"}) == "part"
    assert building_type({"building": ""}) == "part"
    assert building_type({"building:part": "yes"}) == "part"


def test_material_values_drop_missing_and_empty() -> None:
    props = {
        "building:material": "Wood; metal",
        "roof:material": ";",
        "building:structure": " Cob ",
    }
    assert material_values(props) == {"building:material": "wood", "building:structure": "cob"}


def test_spread_bits_and_delta_encode() -> None:
    assert spread_bits(0b101) == 0b10001
    assert spread_bits(2**32 - 1) == 0x5555555555555555
    assert delta_encode([5, 7, 4, 4]) == [5, 2, -3, 0]
    assert delta_encode([]) == []


def test_outward_rounding_contains_the_bounds() -> None:
    assert outward((10.123456, 55.000001, 10.123461, 55.000009)) == [
        10.12345,
        55.0,
        10.12347,
        55.00001,
    ]
    assert outward((-0.000001, -1.5, 0.0, -1.499999)) == [-0.00001, -1.5, 0.0, -1.49999]


def test_stats_reproduce_the_per_feature_sums(small_data: Path) -> None:
    index = read_web_index(small_data)
    features = small_features()
    assert index["countries"] == ["sweden", "norway"]  # COUNTRIES order
    assert index["keys"] == list(MATERIAL_KEYS)
    assert index["materials"] == ["brick", "concrete", "tiles", "turf", "wood"]
    assert index["types"] == ["cabin", "house", "part", "yes"]

    from_stats: Totals = defaultdict(lambda: [0, 0.0, 0.0])
    for country, _type, *codes, _assumed, count, footprint, floor in index["stats"]:
        for key, code in zip(MATERIAL_KEYS, codes):
            if code:
                group = (index["countries"][country], key, index["materials"][code - 1])
                add_to(from_stats, group, count, footprint, floor)

    direct: Totals = defaultdict(lambda: [0, 0.0, 0.0])
    for name, fs in features.items():
        for f in fs:
            props = f["properties"]
            for key, value in material_values(props).items():
                add_to(direct, (name, key, value), 1, props["footprint_m2"], props["floor_area_m2"])
    assert from_stats.keys() == direct.keys() and len(direct) == 7
    for group, expected in direct.items():
        assert from_stats[group] == pytest.approx(expected, abs=0.05)


def test_stats_rows_cover_every_feature_once(small_data: Path) -> None:
    index = read_web_index(small_data)
    rows = index["stats"]
    assert rows == sorted(rows)
    assert sum(r[7] for r in rows) == 7  # nodes and parts included
    # building=no, '' and a missing building all count as "part".
    by_type: defaultdict[str, int] = defaultdict(int)
    for r in rows:
        by_type[index["types"][r[1]]] += r[7]
    assert dict(by_type) == {"house": 2, "part": 3, "yes": 1, "cabin": 1}
    assert sum(r[7] for r in rows if r[6] == 1) == 3  # levels_assumed
    assert {r[6] for r in rows} == {0, 1}
    assert index["country"]["sweden"] == {
        "total_buildings": 1000,
        "features": 5,
        "bbox": [11.5, 56.78951, 15.0005, 59.0005],
    }
    assert index["country"]["norway"]["bbox"] == [5.25, 60.1005, 10.1005, 62.5]


def test_points_decode_to_the_centroids(small_data: Path) -> None:
    index = read_web_index(small_data)
    points = read_script(small_data / "web" / "points.js", "window.OSM_POINTS = ")
    assert list(points) == index["countries"]
    for name, features in small_features().items():
        cols = points[name]
        assert list(cols) == ["lon", "lat", "c0", "c1", "c2", "c3"]
        assert {len(c) for c in cols.values()} == {len(features)}
        lon, lat = (list(accumulate(cols[k])) for k in ("lon", "lat"))
        codes = zip(*(cols[f"c{n}"] for n in range(len(MATERIAL_KEYS))))
        decoded = [(x / 1e5, y / 1e5, *c) for x, y, c in zip(lon, lat, codes)]
        for f in features:
            x, y = vertex_center(f)
            values = material_values(f["properties"])
            want = [
                index["materials"].index(values[k]) + 1 if k in values else 0 for k in MATERIAL_KEYS
            ]
            match = next(
                d
                for d in decoded
                if list(d[2:]) == want and abs(d[0] - x) < 1e-5 and abs(d[1] - y) < 1e-5
            )
            decoded.remove(match)  # each decoded point matches exactly one feature
        assert not decoded
        # Z-order: the Morton keys of the points never decrease.
        keys = [interleave(x + 180 * 10**5, y + 90 * 10**5) for x, y in zip(lon, lat)]
        assert keys == sorted(keys)


def test_leaf_cells_split_and_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    pile = [(1.0, 2.0)] * (CHUNK_MAX + 1)
    assert leaf_cells(pile) == [("3" * CHUNK_MAX_DEPTH, list(range(CHUNK_MAX + 1)))]
    assert leaf_cells(pile[:CHUNK_MAX]) == [("", list(range(CHUNK_MAX)))]
    assert leaf_cells([]) == []
    monkeypatch.setattr(osm_materials, "CHUNK_MAX", 1)
    corners = [(1.0, 1.0), (0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]  # NE, SW, SE, NW
    assert leaf_cells(corners) == [("0", [1]), ("1", [2]), ("2", [3]), ("3", [0])]


def test_chunks_hold_every_feature_once(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    # 100 x 70 points plus 120 squares: far more than one chunk, uneven density.
    grid = [
        make_feature(f"node/{i}", point_at(8 + (i % 100) * 0.07, 54.6 + (i // 100) * 0.03))
        for i in range(7000)
    ]
    squares = [
        make_feature(f"way/{i}", square_at(10 + i * 0.001, 56.0 + i * 0.0004)) for i in range(120)
    ]
    features = grid + squares
    write_geojson(data, "denmark", features)
    build_web(data)
    web = data / "web"
    index = read_script(web / "index.js", "window.OSM_WEB = ")
    chunks = index["chunks"]
    assert [c[0] for c in chunks] == sorted(c[0] for c in chunks)
    assert {p.stem for p in (web / "chunks").glob("*.js")} == {c[0] for c in chunks}
    assert len(chunks) > 3

    seen: Counter[str] = Counter()
    originals = {f["properties"]["osm_id"]: f for f in features}
    for chunk_id, w, s, e, n, count in chunks:
        got = read_chunk(web, chunk_id)
        path = chunk_id.removeprefix("denmark-")
        assert len(got) == count
        assert count <= CHUNK_MAX or len(path) == CHUNK_MAX_DEPTH
        assert path == "r" or set(path) <= set("0123")
        for f in got:
            osm_id = f["properties"]["osm_id"]
            seen[osm_id] += 1
            for lon, lat in positions(f["geometry"]["coordinates"]):
                assert w <= lon <= e and s <= lat <= n
            assert f["geometry"]["coordinates"] == round_coords(
                originals[osm_id]["geometry"]["coordinates"]
            )
    assert seen == Counter(dict.fromkeys(originals, 1))
    assert sum(c[5] for c in chunks) == len(features) == index["country"]["denmark"]["features"]


def test_a_pile_of_identical_points_stops_at_the_maximum_depth(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    pile = [make_feature(f"node/{i}", point_at(5.0, 60.0)) for i in range(CHUNK_MAX + 1)]
    write_geojson(data, "norway", pile)
    build_web(data)
    chunks = read_web_index(data)["chunks"]
    assert [(c[0], c[5]) for c in chunks] == [("norway-" + "3" * CHUNK_MAX_DEPTH, CHUNK_MAX + 1)]


def test_single_cell_country_has_the_root_chunk(small_data: Path) -> None:
    ids = [c[0] for c in read_web_index(small_data)["chunks"]]
    assert ids == ["norway-r", "sweden-r"]


def test_chunk_features_get_country_type_and_normalized_tags(small_data: Path) -> None:
    web = small_data / "web"
    by_id = {f["properties"]["osm_id"]: f for f in read_chunk(web, "sweden-r")}
    assert by_id["way/1"]["properties"] == {
        "building": "house",
        "building:material": "Wood; metal",
        "roof:material": "Tiles",
        "levels": 2.0,
        "levels_assumed": True,
        "footprint_m2": 100.4,
        "floor_area_m2": 200.8,
        "osm_id": "way/1",
        "country": "sweden",
        "type": "house",
        "n:building:material": "wood",
        "n:roof:material": "tiles",
    }
    assert by_id["way/1"]["geometry"]["coordinates"][0][0] == [12.345679, 56.789012]
    assert by_id["way/2"]["properties"]["type"] == "part"
    assert by_id["way/5"]["properties"]["type"] == "part"
    # ';' normalizes to nothing, so there is no n: tag; other tags are kept as they are.
    assert not any(k.startswith("n:") for k in by_id["way/5"]["properties"])
    assert by_id["way/5"]["properties"]["name"] == "Gårdsbyggnad"


def test_json_literal_round_trips_and_hides_script_end() -> None:
    value = {"name": "</script><b>x", "path": "a<\\/b", "ü": ["</", 1.5, None]}
    js = json_literal(value)
    assert js.startswith("JSON.parse(") and js.endswith(")")
    assert "</" not in js
    assert json.loads(json.loads(js.removeprefix("JSON.parse(").removesuffix(")"))) == value


def test_script_end_tag_in_a_tag_never_reaches_a_file(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    evil = "</script><script>alert(1)</script>"
    feature = make_feature("node/1", point_at(1.0, 2.0), {"building": "yes", "name": evil})
    write_geojson(data, "sweden", [feature])
    build_web(data)
    files = list((data / "web").rglob("*.js"))
    assert len(files) == 3  # index, points and one chunk
    for path in files:
        assert "</" not in path.read_text(encoding="utf-8")
    assert read_chunk(data / "web", "sweden-r")[0]["properties"]["name"] == evil


def test_build_is_deterministic_and_replaces_the_folder(small_data: Path) -> None:
    web = small_data / "web"
    first = snapshot(web)
    (web / "stale.js").write_text("old", encoding="utf-8")
    build_web(small_data)
    assert snapshot(web) == first
    assert not (small_data / ".web-build").exists()
    assert not (small_data / ".web-old").exists()


def test_rebuild_that_cannot_move_the_old_bundle_keeps_it(
    small_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Windows a file held open (say by a browser) blocks moving web/ aside.
    web = small_data / "web"
    first = snapshot(web)
    real_replace = Path.replace

    def locked(self: Path, target: Any) -> Path:
        if self == web:
            raise PermissionError("a file in web/ is open")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", locked)
    with pytest.raises(PermissionError):
        build_web(small_data)
    assert snapshot(web) == first
    assert not (small_data / ".web-build").exists()


def test_fetched_is_the_newest_date_in_the_collections(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    features = small_features()
    write_geojson(data, "sweden", features["sweden"], fetched="2026-09-18")
    write_geojson(data, "norway", features["norway"], fetched="2026-10-02")
    build_web(data)
    assert read_web_index(data)["fetched"] == "2026-10-02"


def test_fetched_falls_back_to_the_newest_file_date(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    features = small_features()
    for name, day in (("sweden", "2025-03-04"), ("norway", "2025-03-01")):
        path = write_geojson(data, name, features[name], fetched=None)
        stamp = datetime.fromisoformat(f"{day}T12:00:00+00:00").timestamp()
        os.utime(path, (stamp, stamp))
    build_web(data)
    assert read_web_index(data)["fetched"] == "2025-03-04"


def test_bundle_follows_the_geojson_files_present(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(SystemExit):
        build_web(data)
    write_geojson(data, "norway", small_features()["norway"])
    build_web(data)
    index = read_web_index(data)
    assert index["countries"] == ["norway"] and list(index["country"]) == ["norway"]
    write_geojson(data, "sweden", [])
    with pytest.raises(SystemExit, match="no features"):
        build_web(data)
    # The failed build neither replaced the previous bundle nor left its staging folder.
    assert read_web_index(data) == index
    assert not (data / ".web-build").exists()


def test_main_web_rebuilds_without_network(
    small_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.rmtree(small_data / "web")
    monkeypatch.setattr(sys, "argv", ["osm_materials.py", "--web", "--out", str(small_data)])
    main()
    assert (small_data / "web" / "index.js").exists()


def dashboard_with_placeholders(tmp_path: Path) -> Path:
    path = tmp_path / "dashboard.html"
    path.write_text(
        "<head>\n<!-- site:head -->\n</head>\n<body>\n<!-- site:back -->\n<p>ä</p>\n</body>\n",
        encoding="utf-8",
    )
    return path


def test_build_dist_packages_the_new_file_set(small_data: Path, tmp_path: Path) -> None:
    for name in ("summary_sweden.csv", "raw_sweden.json"):
        (small_data / name).write_text("x", encoding="utf-8")
    dashboard = dashboard_with_placeholders(tmp_path)
    archive = build_dist(small_data, tmp_path / "dist", dashboard)
    build_dist(small_data, tmp_path / "dist", dashboard)  # rebuilding replaces the previous folder
    with zipfile.ZipFile(archive) as z:
        names = {n for n in z.namelist() if not n.endswith("/")}
        readme = z.read(f"{DIST_NAME}/README.txt").decode("utf-8")
    web_files = {f"data/web/{p}" for p in snapshot(small_data / "web")}
    assert {"data/web/index.js", "data/web/points.js", "data/web/chunks/sweden-r.js"} <= web_files
    assert names == {
        f"{DIST_NAME}/{n}"
        for n in ("dashboard.html", "README.txt", "data/summary_sweden.csv", *web_files)
    }
    assert "Fetched via the Overpass API on 2026-09-18." in readme
    assert "materials_" not in readme


def test_build_dist_and_site_need_a_bundle(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    dashboard = dashboard_with_placeholders(tmp_path)
    with pytest.raises(SystemExit, match="--web"):
        build_dist(data, tmp_path / "dist", dashboard)
    with pytest.raises(SystemExit, match="--web"):
        build_site(data, tmp_path / "site", dashboard)
    assert not (tmp_path / "dist").exists() and not (tmp_path / "site").exists()


def test_build_site_writes_page_data_and_marker(small_data: Path, tmp_path: Path) -> None:
    site = tmp_path / "site"
    build_site(small_data, site, dashboard_with_placeholders(tmp_path))
    html = (site / "index.html").read_text(encoding="utf-8")
    assert html == f"<head>\n{SITE_HEAD}\n</head>\n<body>\n{SITE_BACK}\n<p>ä</p>\n</body>\n"
    assert snapshot(site / "data" / "web") == snapshot(small_data / "web")
    assert (site / SITE_MARKER).exists()


def test_build_site_skips_missing_placeholders(small_data: Path, tmp_path: Path) -> None:
    dashboard = tmp_path / "plain.html"
    dashboard.write_text("<p>plain</p>\n", encoding="utf-8")
    build_site(small_data, tmp_path / "site", dashboard)
    assert (tmp_path / "site" / "index.html").read_text(encoding="utf-8") == "<p>plain</p>\n"


def test_build_site_refuses_a_folder_it_did_not_make(small_data: Path, tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "keep").mkdir(parents=True)
    (site / "keep" / "precious.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(SystemExit, match="not touching"):
        build_site(small_data, site, dashboard_with_placeholders(tmp_path))
    assert snapshot(site) == {"keep/precious.txt": b"mine"}


def test_build_site_rebuilds_in_place(small_data: Path, tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()  # an existing empty folder is fine
    dashboard = dashboard_with_placeholders(tmp_path)
    build_site(small_data, site, dashboard)
    first = snapshot(site)
    (site / "stale.txt").write_text("old", encoding="utf-8")
    (site / "data" / "web" / "chunks" / "old.js").write_text("old", encoding="utf-8")
    build_site(small_data, site, dashboard)
    assert snapshot(site) == first


def test_main_writes_fetched_geojson_summary_and_bundle_from_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    element = {
        "type": "way",
        "id": 7,
        "geometry": overpass_geom(SQUARE),
        "tags": {"building": "house", "building:material": "wood"},
    }
    raw = data / "raw_sweden.json"
    raw.write_text(json.dumps({"elements": [element], "total_buildings": 10}), encoding="utf-8")
    stamp = datetime.fromisoformat("2026-05-06T12:00:00+00:00").timestamp()
    os.utime(raw, (stamp, stamp))
    monkeypatch.setattr(sys, "argv", ["osm_materials.py", "SE", "--out", str(data)])
    main()
    collection = json.loads((data / "materials_sweden.geojson").read_text(encoding="utf-8"))
    assert list(collection) == ["type", "fetched", "total_buildings", "features"]
    assert collection["fetched"] == "2026-05-06"
    assert (data / "summary_sweden.csv").exists()
    assert not list(data.glob("materials_*.js"))
    assert read_web_index(data)["fetched"] == "2026-05-06"
