import pytest

from osm_materials import (
    build_query,
    geometry,
    normalize,
    parse_levels,
    ring_area_m2,
    stitch_rings,
    summarize,
    to_feature,
)

# ~100 m x 100 m square at the equator (0.0009 deg = 100.08 m).
D = 0.0009
SQUARE = [(0.0, 0.0), (D, 0.0), (D, D), (0.0, D), (0.0, 0.0)]


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
    hole = [(D / 4, D / 4), (D / 2, D / 4), (D / 2, D / 2), (D / 4, D / 2), (D / 4, D / 4)]
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
    ("value", "expected"), [("2", 2.0), ("2.5", 2.5), (None, None), ("2;3", None), ("0", None)]
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
