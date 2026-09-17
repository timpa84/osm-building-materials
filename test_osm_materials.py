from osm_materials import feature_type, summarize, to_feature


def test_node_feature_keeps_material_and_feature_tags() -> None:
    element = {
        "type": "node",
        "id": 1,
        "lat": 59.3,
        "lon": 18.1,
        "tags": {"power": "pole", "material": "wood", "ref": "12", "name": "P12"},
    }
    f = to_feature(element)
    assert f is not None
    assert f["geometry"]["coordinates"] == [18.1, 59.3]
    assert f["properties"] == {
        "power": "pole",
        "material": "wood",
        "name": "P12",
        "feature": "power=pole",
        "osm_url": "https://www.openstreetmap.org/node/1",
    }


def test_way_uses_center_and_prefixed_material_keys() -> None:
    element = {
        "type": "way",
        "id": 2,
        "center": {"lat": 55.0, "lon": 12.0},
        "tags": {"building": "yes", "roof:material": "tile", "materials": "x"},
    }
    f = to_feature(element)
    assert f is not None
    assert f["geometry"]["coordinates"] == [12.0, 55.0]
    assert f["properties"]["roof:material"] == "tile"
    assert "materials" not in f["properties"]


def test_way_without_center_is_skipped() -> None:
    assert to_feature({"type": "way", "id": 3, "tags": {"material": "steel"}}) is None


def test_feature_type_priority_and_fallback() -> None:
    assert feature_type({"highway": "footway", "bridge": "yes"}) == "bridge=yes"
    assert feature_type({"bridge": "no", "highway": "path"}) == "highway=path"
    assert feature_type({"material": "wood"}) == "other"


def test_summarize_counts_per_feature_key_value() -> None:
    features = [
        {"properties": {"feature": "power=pole", "material": "wood"}},
        {"properties": {"feature": "power=pole", "material": "wood"}},
        {"properties": {"feature": "building=yes", "roof:material": "tile"}},
    ]
    assert summarize(features) == [
        ("power=pole", "material", "wood", 2),
        ("building=yes", "roof:material", "tile", 1),
    ]
