# OSM material tags – Sweden, Norway, Denmark

Finds every OpenStreetMap object that has a `material` tag (or a `*:material`
tag such as `roof:material`, `building:material`, `bridge:material`) and writes
one GeoJSON map per country.

## Run

```bash
uv sync
uv run python osm_materials.py            # all three countries
uv run python osm_materials.py SE         # just Sweden
```

Data comes live from the Overpass API, so results reflect OSM at run time.

## Output (`data/`)

- `materials_<country>.geojson` – one point per object (ways/relations use their
  centre). Properties: the material tags, the main feature tags, `feature`
  (e.g. `power=pole`), `name` and `osm_url`.
- `summary_<country>.csv` – count per feature / material key / value.

The GeoJSON opens directly in QGIS, geojson.io or kepler.gl.

## Develop

```bash
uv run ruff check . && uv run ruff format --check .
uv run python -m pyright
uv run python -m pytest
```

Data © OpenStreetMap contributors, ODbL.
