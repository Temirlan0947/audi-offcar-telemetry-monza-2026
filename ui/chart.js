const PLOT_A = "#ff8a75";
const PLOT_B = "#83d6d1";

function tickStep(span, count) {
  const ideal = span / count;
  const base = 10 ** Math.floor(Math.log10(ideal));
  return ([1, 2, 5, 10].find((n) => n * base >= ideal) || 10) * base;
}

class TelemetryPlot {
  constructor(canvas, state, updateCursor, setRange) {
    this.canvas = canvas;
    this.state = state;
    this.frame = 0;
    this.drag = null;
    const position = (e) => {
      const localX = e.clientX - canvas.getBoundingClientRect().left;
      const ratio = Math.max(0, Math.min(1, (localX - this.left) / this.width));
      const [min, max] = state.domain;
      return Math.round(min + ratio * (max - min));
    };
    canvas.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      const x = position(e);
      const distance = ((state.domain[1] - state.domain[0]) * 8) / this.width;
      const handle =
        Math.abs(x - state.entry) <= distance
          ? "entry"
          : Math.abs(x - state.exit) <= distance
            ? "exit"
            : "brush";
      this.drag = { origin: x, handle, id: e.pointerId };
      canvas.setPointerCapture(e.pointerId);
      canvas.focus({ preventScroll: true });
      updateCursor(x);
    });
    canvas.addEventListener("pointermove", (e) => {
      const x = position(e);
      if (this.drag && this.drag.id === e.pointerId) {
        if (this.drag.handle === "entry") setRange(Math.min(x, state.exit - 1), state.exit);
        else if (this.drag.handle === "exit") setRange(state.entry, Math.max(x, state.entry + 1));
        else if (Math.abs(this.drag.origin - x) > 1) {
          setRange(Math.min(this.drag.origin, x), Math.max(this.drag.origin, x));
        }
      }
      updateCursor(x);
    });
    const release = () => {
      this.drag = null;
    };
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("lostpointercapture", release);
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const step = (e.shiftKey ? 100 : 10) * (e.key === "ArrowLeft" ? -1 : 1);
      updateCursor(Math.max(state.domain[0], Math.min(state.domain[1], state.cursor + step)));
    });
    new ResizeObserver(() => this.request()).observe(canvas);
    document.fonts.ready.then(() => this.request());
  }

  request() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.paint();
    });
  }

  paint() {
    const { canvas, state } = this;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const w = rect.width,
      h = rect.height;
    const pixelRatio = window.devicePixelRatio || 1;
    const cw = Math.round(w * pixelRatio),
      ch = Math.round(h * pixelRatio);
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const left = (this.left = 72),
      right = w - 16;
    this.width = right - left;
    const [min, max] = state.domain;
    const x = (m) => left + ((m - min) / (max - min)) * this.width;
    const top = 28,
      bottom = h - 23;
    const line = (x1, y1, x2, y2, color) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    };
    const text = (str, xPos, yPos, color, align = "left") => {
      ctx.fillStyle = color;
      ctx.textAlign = align;
      ctx.fillText(str, xPos, yPos);
    };
    ctx.font = "10px Consolas, monospace";
    const step = tickStep(max - min, Math.max(2, Math.floor(this.width / 80)));
    for (let m = Math.ceil(min / step) * step; m <= max; m += step) {
      line(x(m), top, x(m), bottom, "#34413b");
      if (m < max - ((max - min) * 38) / this.width) {
        text(m.toLocaleString("en-US"), x(m), bottom + 16, "#adbbb2", "center");
      }
    }
    text(max.toLocaleString("en-US"), right, bottom + 16, "#adbbb2", "right");
    let sectorStart = 0;
    for (const [i, end] of state.data.sectorEnds.entries()) {
      const s = Math.max(min, sectorStart),
        e = Math.min(max, end);
      if (e > s && x(e) - x(s) > 70) {
        text("SECTOR " + (i + 1), (x(s) + x(e)) / 2, 15, "#a6b8ab", "center");
      }
      if (end > min && end < max) line(x(end), 7, x(end), bottom, "#54685b");
      sectorStart = end;
    }
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, this.width, bottom - top);
    ctx.clip();
    ctx.fillStyle = "rgba(225,211,152,.105)";
    ctx.fillRect(x(state.entry), top, x(state.exit) - x(state.entry), bottom - top);
    for (const lap of [state.a, state.b]) {
      for (const [lower, upper] of lap.trace.missing) {
        const begin = Math.max(left, x(lower)),
          end = Math.min(right, x(upper));
        if (begin >= end) continue;
        ctx.save();
        ctx.beginPath();
        ctx.rect(begin, top, end - begin, bottom - top);
        ctx.clip();
        ctx.fillStyle = "#27312c";
        ctx.fillRect(begin, top, end - begin, bottom - top);
        for (let xx = begin - (bottom - top); xx < end; xx += 6) {
          line(xx, bottom, xx + bottom - top, top, "#44544b");
        }
        ctx.restore();
      }
    }
    ctx.restore();
    const values = state.delta
      .flat()
      .filter((p) => min <= p[M] && p[M] <= max)
      .map((p) => Math.abs(p[1]));
    const extent = Math.max(0.25, Math.ceil(Math.max(0, ...values) * 4) / 4);
    const bands = [
      {
        name: "SPEED",
        unit: "km/h",
        field: V,
        lo: 40,
        hi: 360,
        ticks: [100, 200, 300],
        weight: 0.41,
      },
      { name: "GAS", unit: "%", field: GAS, lo: 0, hi: 100, ticks: [0, 100], weight: 0.2 },
      { name: "BRAKE", unit: "", field: BRAKE, lo: 0, hi: 1, ticks: [0, 1], weight: 0.14 },
      {
        name: "Δ TIME",
        unit: "s",
        field: null,
        lo: -extent,
        hi: extent,
        ticks: [-extent, 0, extent],
        weight: 0.25,
      },
    ];
    let bandTop = top;
    for (const band of bands) {
      const bandEnd = bandTop + (bottom - top) * band.weight;
      const y = (value) =>
        bandEnd - 6 - ((value - band.lo) / (band.hi - band.lo)) * (bandEnd - bandTop - 15);
      ctx.font = "600 11px 'Barlow Semi Condensed', sans-serif";
      text(band.name, 8, bandTop + 12, "#d0ddd2");
      ctx.font = "10px Consolas, monospace";
      if (band.unit) text(band.unit, 8, bandTop + 25, "#95a79b");
      for (const tick of band.ticks) {
        line(
          left,
          y(tick),
          right,
          y(tick),
          tick === 0 && band.field === null ? "#68776b" : "#35433b",
        );
        let label = String(tick);
        if (band.field === BRAKE) label = tick ? "on" : "off";
        if (band.field === null) label = tick > 0 ? "+" + tick : String(tick);
        text(label, left - 4, y(tick) + 3, "#a9b9ae", "right");
      }
      ctx.save();
      ctx.beginPath();
      ctx.rect(left, bandTop, this.width, bandEnd - bandTop);
      ctx.clip();
      const draw = (pieces, field, color, dashed = false) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.65;
        ctx.setLineDash(dashed ? [5, 2.5] : []);
        ctx.beginPath();
        for (const piece of pieces) {
          let previous = null;
          for (const p of piece) {
            if (p[field] === null) {
              previous = null;
              continue;
            }
            const xx = x(p[M]),
              yy = y(p[field]);
            if (!previous) ctx.moveTo(xx, yy);
            else {
              if (field === GAS || field === BRAKE) ctx.lineTo(xx, y(previous[field]));
              ctx.lineTo(xx, yy);
            }
            previous = p;
          }
        }
        ctx.stroke();
        ctx.setLineDash([]);
      };
      if (band.field === null) draw(state.delta, 1, "#e1e9da");
      else {
        draw(state.a.trace.pieces, band.field, PLOT_A);
        draw(state.b.trace.pieces, band.field, PLOT_B, true);
      }
      ctx.restore();
      bandTop = bandEnd;
    }
    for (const [value, label] of [
      [state.entry, "IN"],
      [state.exit, "OUT"],
    ]) {
      if (value < min || value > max) continue;
      const xx = x(value);
      line(xx, top - 3, xx, bottom, "#c8b884");
      ctx.fillStyle = "#c8b884";
      ctx.beginPath();
      ctx.moveTo(xx - 4, top - 4);
      ctx.lineTo(xx + 4, top - 4);
      ctx.lineTo(xx, top + 2);
      ctx.fill();
      ctx.font = "9px Consolas, monospace";
      text(label, xx, top - 8, "#d7c78f", "center");
    }
    if (min <= state.cursor && state.cursor <= max) {
      ctx.setLineDash([3, 3]);
      line(x(state.cursor), top, x(state.cursor), bottom, "#dae3d4");
      ctx.setLineDash([]);
    }
  }
}
