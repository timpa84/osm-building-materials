# OSM building materials – Sweden, Norway, Denmark, France

Finds every OpenStreetMap building (`building=*` or `building:part=*`) that has a
material tag – `building:material`, `building:facade:material`,
`building:structure` or `roof:material` – and writes one GeoJSON map per country
plus a MapLibre dashboard with statistics.

> **Status: early test.** An exploratory prototype made within
> [BEAM ME UP](https://www.sintef.no/en/projects/2026/beam-me-up/), a project in the
> Driving Urban Transitions (DUT) partnership on better building stock models for
> circular urban development. It asks a narrow question: how much can
> crowd-sourced OSM material tags tell us about what buildings are made of? Coverage
> is well below 1 % of buildings, so treat the numbers as a view of what is mapped,
> not of the building stock. Not an official project deliverable.

## Run

```bash
uv sync
uv run python osm_materials.py            # all four countries
uv run python osm_materials.py SE         # just Sweden
uv run python osm_materials.py --refresh  # refetch instead of using data/raw_*.json
```

Then open `dashboard.html` in a browser (double-click – no server needed; it
needs internet for the base map and MapLibre itself). The UI is in English by
default; switch to Swedish with the SV/EN buttons or `dashboard.html?lang=sv`.

To share the dashboard, run `uv run python osm_materials.py --dist`: it writes
`dist/osm-materials-dashboard/` (dashboard, data scripts, summary CSVs and a
README.txt) and a zip of it. Recipients unzip and double-click `dashboard.html`.

Street view: zoom in and click a street. The screen splits in two, with Google
Street View looking from the clicked point at the nearest tagged building; the
view cone and the target building are drawn on the map. Click another building
to re-aim from the same spot, and drag the divider to resize the two halves. This uses Google's keyless embed (`output=svembed`),
which is unofficial: the cone shows the initial direction only (the iframe cannot
report back where you turn), and Google snaps to the nearest panorama.

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

- France is metropolitan France only (~140k tagged buildings, 64 MB GeoJSON – the dashboard
  takes a few seconds longer to load).
- Material tagging is sparse and unevenly mapped; numbers describe what is mapped,
  not the building stock.
- A `building:part` inside a tagged `building` counts twice in area sums. Coverage
  ("x % of buildings") counts `building=*` objects only; parts are listed separately.
- `building:levels=0` counts as zero floor area; `2;3` uses the first value.
- Areas are gross footprint/floor areas, not material volumes or masses.

## Develop

```bash
uv run ruff check . && uv run ruff format --check .
uv run python -m pyright
uv run python -m pytest
```

## License

Code: MIT, see `LICENSE`. Data: © OpenStreetMap contributors, ODbL 1.0 – the
generated files in `data/` and `dist/` are not part of the repository.
