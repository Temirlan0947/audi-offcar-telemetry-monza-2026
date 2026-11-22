// Point layout is shared with build_trace in offcar/analysis.py.
const M = 0,
  T = 1,
  V = 2,
  GAS = 3,
  BRAKE = 4,
  GEAR = 5,
  RPM = 6,
  OBSERVED = 7;

function sampleAt(piece, metres) {
  if (metres < piece[0][M] || metres > piece.at(-1)[M]) return null;
  let lo = 0;
  let hi = piece.length - 1;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (piece[mid][M] < metres) lo = mid + 1;
    else hi = mid;
  }
  const right = piece[lo];
  if (right[M] === metres) return right;
  const left = piece[lo - 1];
  const f = (metres - left[M]) / (right[M] - left[M]);
  return [
    metres,
    left[T] + f * (right[T] - left[T]),
    left[V] + f * (right[V] - left[V]),
    left[GAS],
    left[BRAKE],
    left[GEAR],
    left[RPM],
    false,
  ];
}

function lapAt(lap, metres) {
  const piece = lap.trace.pieces.find((p) => p[0][M] <= metres && metres <= p.at(-1)[M]);
  return piece ? sampleAt(piece, metres) : null;
}

function sectionStats(lap, entry, exit) {
  if (entry >= exit) return null;
  const piece = lap.trace.pieces.find((p) => p[0][M] <= entry && exit <= p.at(-1)[M]);
  if (!piece) return null;
  const start = sampleAt(piece, entry);
  const end = sampleAt(piece, exit);
  const speeds = piece.filter((p) => p[OBSERVED] && entry <= p[M] && p[M] <= exit).map((p) => p[V]);
  return {
    duration: end[T] - start[T],
    entry: start[V],
    exit: end[V],
    vmax: speeds.length ? Math.max(...speeds) : null,
    samples: speeds.length,
  };
}

function deltaTrace(a, b) {
  // Intersect each pair of continuous runs. Never draw a delta through a gap.
  const result = [];
  for (const pieceA of a.trace.pieces) {
    for (const pieceB of b.trace.pieces) {
      const start = Math.max(pieceA[0][M], pieceB[0][M]);
      const end = Math.min(pieceA.at(-1)[M], pieceB.at(-1)[M]);
      if (end <= start) continue;
      const knots = new Set([start, end]);
      for (const piece of [pieceA, pieceB]) {
        for (const p of piece) if (start < p[M] && p[M] < end) knots.add(p[M]);
      }
      const xs = [...knots].sort((x, y) => x - y);
      result.push(xs.map((x) => [x, sampleAt(pieceB, x)[T] - sampleAt(pieceA, x)[T]]));
    }
  }
  return result;
}

function sectorChecks(a, b, boundaries) {
  let official = 0;
  return boundaries.map((distance, i) => {
    official += b.sectors[i] - a.sectors[i];
    if (i === 2) official = b.duration - a.duration;
    const p = lapAt(a, distance);
    const q = lapAt(b, distance);
    const estimated = p && q ? q[T] - p[T] : null;
    const line = i === 2 ? "finish" : "S" + (i + 1);
    const anchor = [a, b].some((lap) => lap.trace.anchors.includes(line));
    return {
      distance,
      official,
      estimated,
      anchor,
      difference: estimated === null || anchor ? null : estimated - official,
    };
  });
}
