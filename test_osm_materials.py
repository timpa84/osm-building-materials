import json
import zipfile
from pathlib import Path

import pytest

from osm_materials import (
    DIST_NAME,
    build_dist,
    build_query,
    dashboard_js,
    geometry,
    normalize,
    parse_levels,
    point_in_ring,
    ring_area_m2,
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


def test_dashboard_js_escapes_script_end_tag() -> None:
    js = dashboard_js("sweden", '{"name":"</script><b>x"}')
    prefix = '(window.OSM_MATERIALS ??= {})["sweden"] = JSON.parse('
    assert js.startswith(prefix)
    assert "</script>" not in js
    # The literal, once JS has read it, is the original JSON again.
    literal = js[len(prefix) : -len(");\n")]
    assert json.loads(literal.replace("<\\/", "</")) == '{"name":"</script><b>x"}'


def test_build_dist_packages_dashboard_and_data(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for name in (
        "materials_sweden.js",
        "materials_sweden.geojson",
        "summary_sweden.csv",
        "raw_sweden.json",
    ):
        (data / name).write_text("x", encoding="utf-8")
    archive = build_dist(data, tmp_path / "dist")
    build_dist(data, tmp_path / "dist")  # rebuilding replaces the previous folder
    with zipfile.ZipFile(archive) as z:
        names = {n for n in z.namelist() if not n.endswith("/")}
    assert names == {
        f"{DIST_NAME}/{n}"
        for n in (
            "dashboard.html",
            "README.txt",
            "data/materials_sweden.js",
            "data/summary_sweden.csv",
        )
    }
