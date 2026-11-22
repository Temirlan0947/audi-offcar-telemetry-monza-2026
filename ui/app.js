const data = JSON.parse(document.getElementById("session-json").textContent);
const byId = (id) => document.getElementById(id);
const lookup = new Map(data.laps.map((lap) => [lap.id, lap]));
const drivers = new Map(data.drivers.map((person) => [person.number, person]));
const state = {
  data,
  a: lookup.get(data.defaultPair[0]),
  b: lookup.get(data.defaultPair[1]),
  entry: 2850,
  exit: 3600,
  cursor: 3200,
  domain: [0, data.trackM],
  delta: [],
};
const dash = "—";
const number = (v, digits = 0) => (v === null || v === undefined ? dash : v.toFixed(digits));
const signed = (v) =>
  v === null ? dash : Math.abs(v) < 0.0005 ? "0.000" : (v > 0 ? "+" : "−") + Math.abs(v).toFixed(3);
const carName = (lap) => drivers.get(lap.driver).last_name;
const code = (lap) => drivers.get(lap.driver).code;
const qPhase = (lap) => (lap.phase ? "Q" + lap.phase : dash);
const lapTime = (t) =>
  t == null ? dash : Math.floor(t / 60) + ":" + (t % 60).toFixed(3).padStart(6, "0");
const fmtMetres = (m) => Math.round(m).toLocaleString("en-US");
const metric = (v, digits, unit) => (v == null ? dash : number(v, digits) + " " + unit);

function textEl(tag, content, className) {
  const el = document.createElement(tag);
  el.textContent = content;
  if (className) el.className = className;
  return el;
}

function rowOf(values, header = false) {
  const tr = document.createElement("tr");
  for (const value of values) tr.append(textEl(header ? "th" : "td", value ?? dash));
  return tr;
}

function swapRows(id, rows) {
  byId(id).replaceChildren(...rows.map((cells) => rowOf(cells)));
}

function makeHead(id) {
  const row = rowOf(["", "A · " + code(state.a), "B · " + code(state.b)], true);
  row.children[1].className = "ink-a";
  row.children[2].className = "ink-b";
  byId(id).replaceChildren(row);
}

function verdict(delta, context) {
  if (delta === null) return "Coverage gap in the selected section";
  if (Math.abs(delta) < 0.0005) return "Level to displayed precision";
  const winner = delta > 0 ? state.a : state.b;
  const letter = delta > 0 ? "A" : "B";
  return letter + " · " + carName(winner) + " " + context;
}

function buildCard(slot) {
  const card = byId("card-" + slot);
  const title = slot.toUpperCase();
  card.innerHTML = `<div class="slot-top"><span class="label">Lap ${title}</span><span class="lap-time"></span></div>
    <div class="picker-row"><select class="car-picker" aria-label="Driver for lap ${title}"></select>
    <select class="lap-picker" aria-label="Lap number for ${title}"></select></div>
    <div class="car-stats"><span class="tyre"></span><span class="speeds"></span></div>`;
  const carPicker = card.querySelector(".car-picker");
  const lapPicker = card.querySelector(".lap-picker");
  for (const person of drivers.values()) {
    carPicker.add(new Option("#" + person.number + "  " + person.last_name, person.number));
  }
  function sync() {
    const lap = state[slot];
    carPicker.value = lap.driver;
    const laps = data.laps.filter((r) => r.driver === lap.driver);
    lapPicker.replaceChildren();
    for (const l of laps) {
      const note = l.reason ? " · " + l.reason : l.flags.length ? " · Flag" : "";
      const opt = new Option("L" + l.lap + " · " + qPhase(l) + note, l.id);
      opt.disabled = Boolean(l.reason);
      lapPicker.add(opt);
    }
    lapPicker.value = lap.id;
    card.querySelector(".lap-time").textContent = lapTime(lap.duration);
    card.querySelector(".tyre").textContent =
      (lap.compound || "Tyre unknown") +
      (lap.age != null ? " · " + lap.age + " lap" + (lap.age === 1 ? "" : "s") + " old" : "");
    card.querySelector(".speeds").textContent =
      "Vmax " + number(lap.trace.vmax) + " / trap " + number(lap.trap) + " km/h";
  }
  carPicker.addEventListener("change", () => {
    const n = Number(carPicker.value);
    const candidates = data.laps.filter((l) => l.driver === n && !l.reason);
    const paired = state[slot === "a" ? "b" : "a"];
    const rank = (l) =>
      Number(!l.flags.length) * 4 +
      Number(l.phase === paired.phase) * 2 +
      Number(l.compound === paired.compound);
    candidates.sort((a, b) => rank(b) - rank(a) || a.duration - b.duration);
    state[slot] = candidates[0];
    sync();
    updatePair();
  });
  lapPicker.addEventListener("change", () => {
    state[slot] = lookup.get(lapPicker.value);
    sync();
    updatePair();
  });
  sync();
}

function cursorUpdate(value) {
  state.cursor = value;
  byId("cursor-m").textContent = fmtMetres(value) + " m";
  const lines = [];
  for (const [slot, lap] of [
    ["a", state.a],
    ["b", state.b],
  ]) {
    const p = lapAt(lap, value);
    const div = textEl("div", "", "readout-row " + slot);
    div.append(textEl("b", slot.toUpperCase() + " " + code(lap)));
    if (p) {
      const fields = [
        "Speed " + metric(p[V], 0, "km/h"),
        "Gas " + metric(p[GAS], 0, "%"),
        "Brake " + (p[BRAKE] === null ? dash : p[BRAKE] ? "on" : "off"),
        "Gear " + number(p[GEAR]),
        "RPM " + number(p[RPM]),
      ];
      for (const field of fields) div.append(textEl("span", field));
    } else div.append(textEl("em", "Coverage gap"));
    lines.push(div);
  }
  byId("cursor-values").replaceChildren(...lines);
  plot.request();
}

function setRange(start, end) {
  if (!Number.isFinite(start) || !Number.isFinite(end)) return;
  state.entry = Math.max(0, Math.min(data.trackM - 1, Math.round(start)));
  state.exit = Math.max(state.entry + 1, Math.min(data.trackM, Math.round(end)));
  byId("from-m").value = state.entry;
  byId("to-m").value = state.exit;
  byId("section-length").textContent = fmtMetres(state.exit - state.entry) + " m selected";
  const results = [state.a, state.b].map((lap) => sectionStats(lap, state.entry, state.exit));
  const [a, b] = results;
  const delta = a && b ? b.duration - a.duration : null;
  byId("section-delta").textContent = delta === null ? "Unavailable" : signed(delta) + " s";
  byId("section-delta").classList.toggle("unavailable", delta === null);
  byId("section-verdict").textContent = verdict(delta, "quicker here");
  const metrics = [
    ["Elapsed time", "duration", 3, "s"],
    ["Entry speed", "entry", 1, "km/h"],
    ["Exit speed", "exit", 1, "km/h"],
    ["Sample Vmax", "vmax", 0, "km/h"],
    ["Recorded samples", "samples", 0, ""],
  ];
  swapRows(
    "section-rows",
    metrics.map(([label, key, digits, unit]) => [
      label,
      ...results.map((r) => metric(r?.[key], digits, unit).trim()),
    ]),
  );
  plot.request();
}

function setDomain(domain) {
  state.domain = domain;
  const zoomed = domain[0] !== 0 || domain[1] !== data.trackM;
  byId("zoom").setAttribute("aria-pressed", String(zoomed));
  byId("zoom").textContent = zoomed ? "Show full trace" : "Zoom to section";
  const cursor = Math.max(domain[0], Math.min(domain[1], state.cursor));
  cursorUpdate(cursor);
}

function updatePair() {
  const { a, b } = state;
  state.delta = deltaTrace(a, b);
  const difference = b.duration - a.duration;
  byId("lap-delta").textContent = signed(difference) + " s";
  byId("lap-verdict").textContent = verdict(difference, "quicker over the lap");
  byId("plot-legend").replaceChildren(
    ...[a, b].map((l, i) =>
      textEl(
        "span",
        (i ? "B" : "A") + " · " + code(l) + " L" + l.lap,
        "legend-item" + (i ? " b" : ""),
      ),
    ),
  );
  makeHead("section-head");
  makeHead("quality-head");
  const checks = sectorChecks(a, b, data.sectorEnds);
  swapRows(
    "checks",
    checks.map((r, i) => [
      ["S1", "S2", "Finish"][i],
      fmtMetres(r.distance) + " m",
      signed(r.official),
      signed(r.estimated),
      r.estimated === null ? dash : r.anchor ? "Anchor" : signed(r.difference),
    ]),
  );
  const quality = [
    ["Recorded samples", (l) => l.trace.samples],
    ["Largest time gap", (l) => metric(l.trace.maxGap, 2, "s")],
    [
      "Unmapped distance",
      (l) =>
        metric(
          l.trace.missing.reduce((s, [lo, hi]) => s + hi - lo, 0),
          0,
          "m",
        ),
    ],
    ["Samples with unknown pedals", (l) => l.trace.unknownPedals],
    ["Lap start · UTC", (l) => l.start],
    ["Out-lap start · UTC", (l) => l.outStart],
    ["Cautions during lap", (l) => l.flags.join(" / ") || "None recorded"],
  ];
  swapRows(
    "quality-rows",
    quality.map(([label, fn]) => [label, fn(a), fn(b)]),
  );
  setRange(state.entry, state.exit);
  cursorUpdate(state.cursor);
}

function populateRegister() {
  const counts = data.counts;
  for (const [label, value] of [
    [
      "Archived samples",
      Object.values(counts)
        .reduce((a, b) => a + b, 0)
        .toLocaleString("en-US"),
    ],
    ["Recorded laps", data.laps.length],
    ["Comparable laps", data.laps.filter((l) => !l.reason).length],
    ["Race control messages", data.eventCount],
  ]) {
    const card = document.createElement("div");
    card.append(textEl("span", label, "label"), textEl("b", value));
    byId("inventory").append(card);
  }
  for (const l of data.laps) {
    const r = rowOf([
      code(l),
      l.lap,
      qPhase(l),
      l.start,
      lapTime(l.duration),
      ...l.sectors.map((s) => number(s, 3)),
      l.compound,
      l.quality.samples,
      number(l.quality.maxGap, 2),
      l.reason || (l.flags.length ? "Flag in session" : "Available"),
    ]);
    r.dataset.available = String(!l.reason);
    byId("lap-register").append(r);
  }
  byId("event-title").textContent = "Race control · " + data.eventCount + " messages";
  swapRows(
    "events",
    data.events.map((e) => [e.utc, e.phase ? "Q" + e.phase : dash, e.message]),
  );
}

const plot = new TelemetryPlot(byId("traces"), state, cursorUpdate, setRange);
buildCard("a");
buildCard("b");
updatePair();
populateRegister();
document.fonts.ready.then(() =>
  requestAnimationFrame(() => {
    plot.paint();
    requestAnimationFrame(() => {
      const ms = performance.measure("offline-view-ready", { start: 0 }).duration;
      byId("load-stat").textContent =
        "Offline view ready in " +
        Math.round(ms) +
        " ms on this device, including fonts and the initial comparison.";
    });
  }),
);
byId("from-m").addEventListener("change", (e) => {
  const n = e.target.valueAsNumber;
  setRange(Number.isFinite(n) ? Math.min(n, state.exit - 1) : state.entry, state.exit);
  if (state.entry < state.domain[0]) setDomain([0, data.trackM]);
});
byId("to-m").addEventListener("change", (e) => {
  const n = e.target.valueAsNumber;
  setRange(state.entry, Number.isFinite(n) ? Math.max(n, state.entry + 1) : state.exit);
  if (state.exit > state.domain[1]) setDomain([0, data.trackM]);
});
byId("zoom").addEventListener("click", () => {
  const full = byId("zoom").getAttribute("aria-pressed") === "true";
  setDomain(full ? [0, data.trackM] : [state.entry, state.exit]);
});
byId("full-range").addEventListener("click", () => {
  setRange(0, data.trackM);
  setDomain([0, data.trackM]);
});
for (const [id, comparison] of [
  ["nav-compare", true],
  ["nav-session", false],
]) {
  byId(id).addEventListener("click", () => {
    byId("comparison").hidden = !comparison;
    byId("session-data").hidden = comparison;
    byId("nav-compare").removeAttribute("aria-current");
    byId("nav-session").removeAttribute("aria-current");
    byId(id).setAttribute("aria-current", "page");
    plot.request();
  });
}
