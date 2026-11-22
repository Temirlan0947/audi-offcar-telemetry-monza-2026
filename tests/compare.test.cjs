const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const api = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../ui/compare.js"), "utf8"), api);

function lap(time, breaks = false) {
  const points = [0, 100, 200, 300].map((m) => [
    m,
    (m / 300) * time,
    240 + m / 10,
    100,
    0,
    7,
    10000,
    true,
  ]);
  return {
    duration: time,
    sectors: [time / 3, time / 3, time / 3],
    trace: {
      pieces: breaks ? [points.slice(0, 2), points.slice(2)] : [points],
      anchors: ["start", "finish"],
    },
  };
}

test("positive delta means B takes longer over the same distance", () => {
  const a = lap(6),
    b = lap(6.3);
  const da = api.sectionStats(a, 100, 200),
    db = api.sectionStats(b, 100, 200);
  assert.ok(Math.abs(db.duration - da.duration - 0.1) < 1e-8);
  assert.equal(da.vmax, 260);
  assert.equal(api.sectionStats(a, 111, 112).vmax, null);
  const finish = api.sectorChecks(a, b, [100, 200, 300]).at(-1);
  assert.equal(finish.anchor, true);
  assert.equal(finish.difference, null);
});

test("no interpolated hover, section time or delta across a dropout", () => {
  const a = lap(6, true),
    b = lap(7);
  assert.equal(api.lapAt(a, 130), null);
  assert.equal(api.sectionStats(a, 90, 210), null);
  assert.equal(api.deltaTrace(a, b).length, 2);
  assert.equal(api.sectionStats(a, 210, 300).exit, 270);
});

test("unknown brake remains unknown until a real change", () => {
  const a = lap(6);
  a.trace.pieces[0][1][4] = null;
  a.trace.pieces[0][2][4] = 1;
  assert.equal(api.lapAt(a, 150)[4], null);
  assert.equal(api.lapAt(a, 200)[4], 1);
});
