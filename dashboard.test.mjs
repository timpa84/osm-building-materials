// Tests for the street-view geometry in dashboard.html. Run: node --test
// The functions are taken from the page's own script, so the tests exercise the code that ships.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("./dashboard.html", import.meta.url), "utf8");
/** The page's script from the line starting with `from` up to (not including) the one with `to`. */
function slice(from, to) {
  const start = html.indexOf(from);
  const end = html.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `dashboard.html no longer has "${from}" ... "${to}"`);
  return html.slice(start, end);
}

const ORIGIN = [18, 59.3];
/** Lon/lat x metres east and y metres north of ORIGIN, with the page's own flat projection. */
const at = (x, y) => [ORIGIN[0] + x / (111320 * Math.cos((ORIGIN[1] * Math.PI) / 180)), ORIGIN[1] + y / 110540];
const box = (x0, y0, x1, y1) => [at(x0, y0), at(x1, y0), at(x1, y1), at(x0, y1), at(x0, y0)];
const street = (props, ...pts) => ({ properties: props, geometry: { type: "LineString", coordinates: pts.map((p) => at(...p)) } });
const wall = (height, ...corners) => ({ properties: { render_height: height }, geometry: { type: "Polygon", coordinates: [box(...corners)] } });
const offsetM = ([lon, lat]) => [(lon - ORIGIN[0]) * 111320 * Math.cos((ORIGIN[1] * Math.PI) / 180), (lat - ORIGIN[1]) * 110540];
const bboxOf = (pts) => [0, 1, 0, 1].map((i, k) => (k < 2 ? Math.min : Math.max)(...pts.map((p) => p[i])));
const building = (...corners) => ({ properties: { levels: 2 }, geometry: { type: "Polygon", coordinates: [box(...corners)] } });

/** The geometry and street-view search of the page, run against a fake base map. */
function page(streets, walls) {
  const map = {
    querySourceFeatures: (source, { sourceLayer }) => (sourceLayer === "transportation" ? streets : walls),
  };
  const context = vm.createContext({ map });
  vm.runInContext(
    [
      slice("const overlaps =", "const chunksIn ="),
      slice("const outerRing =", "function colorExpr()"),
      slice("/** Metres east/north from a to b", "/** A street click:"),
      "this.api = { firstCrossing, firstSeen, inRing, ringDist, streetPenalty, seenFrom, sightTarget, viewpointFor, offsetM };",
    ].join("\n"),
    context,
  );
  return context.api;
}

// A 20 m x 20 m building around the origin, two storeys (6 m), and a street 15 m south of it.
const HOUSE = building(-10, -10, 10, 10);
const SOUTH_STREET = street({ class: "minor" }, [-80, -25], [80, -25]);

test("firstCrossing finds where a segment enters a ring", () => {
  const { firstCrossing } = page([], []);
  const square = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]];
  assert.equal(firstCrossing([-10, 5], [20, 5], square), 1 / 3);
  assert.equal(firstCrossing([-10, 20], [20, 20], square), null);
});

test("streets that Google does not drive are not used", () => {
  const { streetPenalty } = page([], []);
  assert.equal(streetPenalty({ class: "minor" }), 0);
  assert.equal(streetPenalty({ class: "minor", brunnel: "tunnel" }), null);
  assert.equal(streetPenalty({ class: "path", subclass: "footway" }), null);
  assert.equal(streetPenalty({ class: "path", subclass: "pedestrian" }), 4);
  assert.ok(streetPenalty({ class: "service", service: "driveway" }) > streetPenalty({ class: "service" }));
});

test("only the facade facing the camera is seen", () => {
  const { sightTarget, seenFrom } = page([], []);
  const target = sightTarget(HOUSE, ORIGIN, []);
  const seen = seenFrom([0, -25], target);
  assert.ok(seen.length > 0);
  // Nothing on the far (north) side, which the building itself hides.
  assert.ok(seen.every(([, y]) => y < 9.9), JSON.stringify(seen));
});

test("the viewpoint is on the street, aimed at the building", () => {
  const { viewpointFor, offsetM } = page([SOUTH_STREET], []);
  const view = viewpointFor(HOUSE);
  assert.ok(view);
  const [x, y] = offsetM(ORIGIN, view.pos);
  assert.ok(Math.abs(y + 25) < 0.01, `camera ${y} m north of the origin, street is at -25`);
  assert.ok(Math.abs(x) < 15, `camera ${x} m east; the facade spans -10..10`);
  assert.ok(view.heading < 30 || view.heading > 330, `heading ${view.heading}`);
  assert.ok(view.span > 20 && view.span < 120);
});

test("a taller building in between hides it: no viewpoint", () => {
  const { viewpointFor } = page([SOUTH_STREET], [wall(15, -60, -20, 60, -15)]);
  assert.equal(viewpointFor(HOUSE), null);
});

test("a low wall in between does not hide it", () => {
  const { viewpointFor } = page([SOUTH_STREET], [wall(2, -60, -20, 60, -15)]);
  assert.ok(viewpointFor(HOUSE));
});

test("the building's own copy in the base map is not an obstacle", () => {
  const { viewpointFor } = page([SOUTH_STREET], [wall(6, -10, -10, 10, 10)]);
  assert.ok(viewpointFor(HOUSE));
});

test("no street within reach, or only a footpath: no viewpoint", () => {
  assert.equal(page([street({ class: "minor" }, [-80, -90], [80, -90])], []).viewpointFor(HOUSE), null);
  assert.equal(page([street({ class: "path", subclass: "footway" }, [-80, -25], [80, -25])], []).viewpointFor(HOUSE), null);
});

test("a nearer street that sees the building wins over a far one", () => {
  const near = street({ class: "minor" }, [-80, 18], [80, 18]);
  const { viewpointFor, offsetM } = page([SOUTH_STREET, near], []);
  const [, y] = offsetM(ORIGIN, viewpointFor(HOUSE).pos);
  assert.ok(Math.abs(y - 18) < 0.01, `camera at ${y}`);
});

test("a street click looks at the nearest building it can see", () => {
  const hidden = building(-10, 30, 10, 50); // nearer, but behind a tall wall
  const open = building(40, -10, 60, 10);
  const { firstSeen } = page([], []);
  const walls = [{ ring: box(-20, 20, 20, 25).map((c) => offsetM(c)), height: 20 }].map((w) => ({ ...w, bbox: bboxOf(w.ring) }));
  const hit = firstSeen([0, 0], [hidden, open], ORIGIN, walls);
  assert.equal(hit?.f, open);
  assert.ok(hit.view.heading > 60 && hit.view.heading < 120, `heading ${hit.view.heading}`);
});

test("a camera standing inside a building does not look at it", () => {
  const { firstSeen } = page([], []);
  assert.equal(firstSeen([0, 0], [HOUSE], ORIGIN, []), null);
});
