# OSM building materials – Sweden, Norway, Denmark, France

Finds every OpenStreetMap building (`building=*` or `building:part=*`) that has a
material tag – `building:material`, `building:facade:material`,
`building:structure` or `roof:material` – and writes one GeoJSON map per country
plus a MapLibre dashboard with statistics.

> **Status: early test.** An exploratory prototype made within
> [Beam me up!](https://dutpartnership.eu/projects/beam-me) (BEAM ME UP), a
> Driving Urban Transitions (DUT) project in the Circular Urban Economies pathway
> (DUT Call 2024, 2026–2028, coordinated by SINTEF). The project aims to enhance
> building stock models for circular urban development by combining diverse data
> sources to predict construction material streams for reuse, repurposing and
> recycling. The four countries here are the project's partner countries.
>
> This test asks a narrow question: how much can crowd-sourced OSM material tags
> tell us about what buildings are made of? Coverage is well below 1 % of
> buildings, so treat the numbers as a view of what is mapped, not of the building
> stock. Not an official project deliverable.

## Run

```bash
uv sync
uv run python osm_materials.py            # all four countries
uv run python osm_materials.py SE         # just Sweden
uv run python osm_materials.py --refresh  # refetch instead of using data/raw_*.json
uv run python osm_materials.py --web      # only rebuild data/web/ from the existing GeoJSON
```

Every run ends by rebuilding `data/web/` from all `materials_*.geojson` files in
`data/`, so `osm_materials.py SE` still gives a bundle with all four countries;
`--web` does just that step, without network.

Then open `dashboard.html` in a browser (double-click – no server needed; it
needs internet for the base map and MapLibre itself). The UI is in English by
default; switch to Swedish with the SV/EN buttons or `dashboard.html?lang=sv`.
The dashboard starts from a small index and the building centroids in
`data/web/` and loads building outlines per area as you zoom in. Everything
is a `<script>` that sets a global (never `fetch()`ed JSON), which is why it
works from `file://`.

To share the dashboard, run `uv run python osm_materials.py --dist`: it writes
`dist/osm-materials-dashboard/` (dashboard, `data/web/`, summary CSVs and a
README.txt) and a zip of it. Recipients unzip and double-click `dashboard.html`.
Both `--dist` and `--site` need `data/web/` to exist (run `--web` first).

To publish it as a website, run `uv run python osm_materials.py --site DIR`:
it writes `DIR/index.html` (`dashboard.html` with its `<!-- site:head -->` and
`<!-- site:back -->` placeholders filled with a canonical link and a back link),
`DIR/data/web/` and a marker file `DIR/.osm-materials-site`. A folder that
already has content is only emptied and rewritten if it carries that marker;
otherwise the command stops without touching it.

Street view: click a building and choose *Show in street view*, or zoom in and
click a street to stand there. The screen splits in two, with Google Street View
on one side and the camera, its view cone and the target building on the map.
Click another building to re-aim from the same spot; drag the divider to resize.

Google's keyless embed (`output=svembed`, unofficial) cannot say where the
panorama it shows was taken; it shows the one nearest the point it is given. So
the dashboard decides where the camera stands, using the base map (OpenFreeMap
vector tiles) already in the browser:

- The camera always stands on a street (`transportation` layer; driveways and
  service roads count less, footpaths and tunnels not at all). A street click
  more than 30 m from a street shows no image instead of a far-away panorama.
  When the camera is moved to the street it slides there on the map.
- For *Show in street view* it searches the streets within 60 m of the building
  for the spot that sees most of the facade without standing against it. Lines
  of sight are checked against the base map's buildings and their
  `render_height` (a lower building in front does not block the view of the
  lower facade). If no street sees the building – a courtyard building, say – no
  image is shown.
- The view is aimed at the middle of the visible facade, not at the centroid.

The cone shows the initial direction only (the iframe cannot report back where
you turn), and Google may still pick a panorama a few metres from the camera.
The current view is kept in the URL (`#map=zoom/lat/lon`), so it can be shared.

Data comes live from the Overpass API, so results reflect OSM at fetch time. The
raw response is cached in `data/raw_<country>.json` (not committed).

The generated files in `data/` are committed (fetched 2026-09-18), so a fresh clone
can open `dashboard.html` right away without running the script. The GeoJSON files
open directly in QGIS.

## Output (`data/`)

- `materials_<country>.geojson` – building footprints (nodes stay points). Properties:
  the material tags, `building`, `building:levels`, `height`, `name`, `osm_id` and
  - `footprint_m2` – footprint area, holes subtracted
  - `levels` / `levels_assumed` – `building:levels`, or **2 when missing** (`levels_assumed: true`)
  - `floor_area_m2` – `footprint_m2 × levels`

  The collection also has `fetched` (the date the data was fetched) and `total_buildings`:
  all buildings in the country, for coverage.
- `web/` – what the dashboard loads, rebuilt from the GeoJSON files (replaces the earlier
  `materials_<country>.js`, which put every building into one script per country):
  - `index.js` (`window.OSM_WEB`) – fetch date, per-country totals and bounding boxes, the
    material and building-type lists, statistics grouped by country, type, material per tag and
    assumed levels, and the list of chunks with their bounding boxes.
  - `points.js` (`window.OSM_POINTS`) – per country the building centroids as compact columns
    (1e-5 degree units, Z-ordered and delta-encoded) with a material code per tag.
  - `chunks/<country>-<path>.js` (`window.OSM_CHUNK(id, geojson)`) – the building outlines,
    at most 3000 per chunk (a quadtree on the centroids; `r` is the whole country), with
    `country`, `type` and the normalized `n:<tag>` values added to the properties.
- `summary_<country>.csv` – buildings, footprint and floor area per tag and material.

## Caveats

- France is metropolitan France only (~140k tagged buildings, 64 MB GeoJSON).
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
node --test   # street-view geometry in dashboard.html (Node 20+, no packages)
```

## License

Code: MIT, see `LICENSE`. The files in `data/` are derived from OpenStreetMap:
© OpenStreetMap contributors, [ODbL 1.0](https://www.openstreetmap.org/copyright).
