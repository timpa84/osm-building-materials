# OSM building materials – Sweden, Norway, Denmark

Finds every OpenStreetMap building (`building=*` or `building:part=*`) that has a
material tag – `building:material`, `building:facade:material`,
`building:structure` or `roof:material` – and writes one GeoJSON map per country
plus a MapLibre dashboard with statistics.

## Run

```bash
uv sync
uv run python osm_materials.py            # all three countries
uv run python osm_materials.py SE         # just Sweden
uv run python osm_materials.py --refresh  # refetch instead of using data/raw_*.json
```

Then open `dashboard.html` in a browser (double-click – no server needed; it
needs internet for the base map and MapLibre itself).

Data comes live from the Overpass API, so results reflect OSM at fetch time. The
raw response is cached in `data/raw_<country>.json`.

## Output (`data/`)

- `materials_<country>.geojson` – building footprints (nodes stay points). Properties:
  the material tags, `building`, `building:levels`, `height`, `name`, `osm_id` and
  - `footprint_m2` – footprint area, holes subtracted
  - `levels` / `levels_assumed` – `building:levels`, or **2 when missing** (`levels_assumed: true`)
  - `floor_area_m2` – `footprint_m2 × levels`
  
  The collection also has `total_buildings`: all buildings in the country, for coverage.
- `materials_<country>.js` – the same GeoJSON wrapped as a script for the dashboard.
- `summary_<country>.csv` – buildings, footprint and floor area per tag and material.

## Caveats

- Material tagging is sparse and unevenly mapped; numbers describe what is mapped,
  not the building stock.
- A `building:part` inside a tagged `building` counts twice in area sums.
- Areas are gross footprint/floor areas, not material volumes or masses.

## Develop

```bash
uv run ruff check . && uv run ruff format --check .
uv run python -m pyright
uv run python -m pytest
```

Data © OpenStreetMap contributors, ODbL.
