"""Estimate distance on the Monza centreline and keep timing gaps explicit."""

import bisect
import re
from datetime import datetime, timezone
from itertools import pairwise

from .archive import SESSION


# FIA Italian GP 2026, Document 6, circuit map (sector lengths 1909/1823/2061 m).
SECTOR_ENDS = (1909, 3732, 5793)
TRACK_M = SECTOR_ENDS[-1]
MAX_GAP_S = 1.0
EPS = 1e-8


def utc_label(us):
    if us is None:
        return None
    return datetime.fromtimestamp(us / 1e6, timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def lerp(x, left, right, xcol, ycol):
    fraction = (x - left[xcol]) / (right[xcol] - left[xcol])
    return left[ycol] + fraction * (right[ycol] - left[ycol])


def speed_boundary(rows, times, t):
    """Interpolate the speed at a lap boundary only from valid bracketing readings."""
    i = bisect.bisect_left(times, t)
    if i < len(rows) and times[i] == t:
        return rows[i]
    if i == 0 or i == len(rows):
        return None
    left, right = rows[i - 1], rows[i]
    if right[0] - left[0] > MAX_GAP_S or left[1] is None or right[1] is None:
        return None
    return (t, lerp(t, left, right, 0, 1), *left[2:6], False)


def build_trace(readings, start_us, duration, splits):
    """Input includes up to one reading on each side of the timed lap.

    Output point = [estimated metres, elapsed seconds, km/h, throttle %, brake 0/1,
                    gear, rpm, source observation]. Each piece is continuous.
    """
    rows = []
    for utc, speed, gas, brake, gear, rpm in readings:
        rows.append(((utc - start_us) / 1e6, speed if speed is not None and speed >= 0 else None,
                     gas if gas is not None and 0 <= gas <= 100 else None,
                     {0: 0, 100: 1}.get(brake), gear, rpm, True))
    times = [r[0] for r in rows]
    observed = [r for r in rows if 0 <= r[0] < duration]
    gaps = [b - a for a, b in pairwise([0, *(r[0] for r in observed), duration])]
    speeds = [r[1] for r in observed if r[1] is not None]
    nodes = [r for r in rows if 0 < r[0] < duration]
    first = speed_boundary(rows, times, 0)
    last = speed_boundary(rows, times, duration)
    if first is not None:
        nodes.insert(0, first)
    if last is not None:
        # The half-open ownership rule puts an exact finish-line sample on the next lap.
        nodes.append((*last[:6], False))

    runs = []
    current = []
    for node in nodes:
        if node[1] is None or (current and node[0] - current[-1][0] > MAX_GAP_S):
            if current:
                runs.append(current)
            current = []
        if node[1] is not None:
            current.append(node)
    if current:
        runs.append(current)

    anchors = [(0, 0, "start"), (duration, TRACK_M, "finish")]
    if splits[0] is not None:
        anchors.append((splits[0], SECTOR_ENDS[0], "S1"))
        if splits[1] is not None:
            anchors.append((splits[0] + splits[1], SECTOR_ENDS[1], "S2"))

    pieces = []
    used = []
    integrated = None
    for run in runs:
        if len(run) < 2:
            continue
        # Convert km/h to m/s and integrate using each actual time step.
        dist = [0.0]
        for left, right in pairwise(run):
            dist.append(dist[-1] + (left[1] + right[1]) / 7.2 * (right[0] - left[0]))
        if dist[-1] <= 0:
            continue
        scale, offset = 1.0, 0.0
        if run[0][0] == 0 and run[-1][0] == duration:
            integrated = dist[-1]
            scale = TRACK_M / integrated
            used.extend(("start", "finish"))
        else:
            anchor = next((a for a in anchors if run[0][0] <= a[0] <= run[-1][0]), None)
            if anchor is None:
                continue
            anchor_t, anchor_m, label = anchor
            ts = [r[0] for r in run]
            index = bisect.bisect_left(ts, anchor_t)
            local = dist[index]
            if ts[index] != anchor_t:
                left, right = run[index - 1], run[index]
                v = lerp(anchor_t, left, right, 0, 1)
                local = dist[index - 1] + (v + left[1]) / 7.2 * (anchor_t - left[0])
            offset = anchor_m - local
            used.append(label)
        points = []
        for s, node in zip(dist, run):
            x = round(s * scale + offset, 4)
            if 0 <= x <= TRACK_M and (not points or x > points[-1][0]):
                points.append([x, *node])
        if len(points) >= 2:
            pieces.append(points)

    # Two independent anchors can place the edges of a dropout in opposite order.
    # Drop the ambiguous overlap from both pieces so no distance has two times.
    trimmed = []
    for index, piece in enumerate(pieces):
        low = max((p[-1][0] for p in pieces[:index]), default=-1)
        high = min((p[0][0] for p in pieces[index + 1:]), default=TRACK_M + 1)
        visible = [p for p in piece if low < p[0] < high]
        if len(visible) >= 2:
            trimmed.append(visible)
    missing = []
    edge = 0.0
    for piece in trimmed:
        if piece[0][0] > edge + EPS:
            missing.append([edge, piece[0][0]])
        edge = piece[-1][0]
    if edge < TRACK_M - EPS:
        missing.append([edge, TRACK_M])
    return {
        "pieces": trimmed, "missing": missing, "anchors": used, "integratedM": integrated,
        "samples": len(observed), "maxGap": max(gaps), "vmax": max(speeds, default=None),
        "unknownPedals": sum(r[2] is None or r[3] is None for r in observed),
    }


def read_trace(db, lap):
    start = lap["start_us"]
    duration = lap["duration_s"]
    end = start + round(duration * 1e6)
    key = (SESSION, lap["driver"])
    sql = ("SELECT utc_us,speed_kmh,throttle_raw,brake_raw,gear,rpm FROM telemetry "
           "WHERE session_key=? AND driver=? AND ")
    before = db.execute(sql + "utc_us<? ORDER BY utc_us DESC LIMIT 1", (*key, start)).fetchall()
    middle = db.execute(sql + "utc_us>=? AND utc_us<? ORDER BY utc_us",
                        (*key, start, end)).fetchall()
    after = db.execute(sql + "utc_us>=? ORDER BY utc_us LIMIT 1", (*key, end)).fetchall()
    return build_trace(before + middle + after, start, duration, (lap["s1_s"], lap["s2_s"]))


def window_quality(db, lap, next_start):
    start = lap["start_us"]
    if start is None:
        return {"samples": None, "maxGap": None}
    ends = [v for v in (next_start, start + round(lap["duration_s"] * 1e6)
                        if lap["duration_s"] else None) if v is not None and v > start]
    if not ends:
        return {"samples": None, "maxGap": None}
    end = min(ends)
    times = [r[0] for r in db.execute(
        "SELECT utc_us FROM telemetry WHERE session_key=? AND driver=? AND utc_us>=? "
        "AND utc_us<? ORDER BY utc_us", (SESSION, lap["driver"], start, end))]
    return {"samples": len(times),
            "maxGap": max(b - a for a, b in pairwise([start, *times, end])) / 1e6}


def lap_context(lap, events):
    start = lap["start_us"]
    finish = (start + round(lap["duration_s"] * 1e6)
              if start is not None and lap["duration_s"] else start)
    phase = None
    active = {}
    notices = {}
    deleted = False
    for event in events:
        t, msg, flag = event["utc_us"], event["message"], event["flag"]
        car = re.search(r"\bCAR\s+(\d+)\b", msg)
        mentioned_lap = re.search(r"\bLAP\s+(\d+)\b", msg)
        if car and mentioned_lap and (int(car[1]), int(mentioned_lap[1])) == (
            lap["driver"], lap["lap"]
        ):
            if "REINSTATED" in msg:
                deleted = False
            elif "DELETED" in msg:
                deleted = True
        if start is None or t > finish:
            continue
        if msg == "SESSION STARTED":
            active.clear()
            if t <= start:
                phase = event["phase"]
        if event["scope"] == "Driver" and event["driver"] != lap["driver"]:
            continue
        if t >= start and active:
            notices.update(active)
        key = (event["scope"], event["sector"], event["driver"])
        if flag in ("YELLOW", "DOUBLE YELLOW", "RED"):
            active[key] = msg
            if t >= start:
                notices[key] = msg
        elif flag in ("CLEAR", "GREEN"):
            active.pop(key, None)
    notices.update(active)
    return phase, sorted(set(notices.values())), deleted


def make_payload(db):
    drivers = [dict(r) for r in db.execute("SELECT * FROM drivers ORDER BY number DESC")]
    events = db.execute("SELECT * FROM race_control ORDER BY utc_us, rowid").fetchall()
    stints = db.execute("SELECT * FROM stints ORDER BY first_lap").fetchall()
    laps = db.execute("SELECT * FROM laps ORDER BY driver DESC, lap").fetchall()
    starts = {(r["driver"], r["lap"]): r["start_us"] for r in laps}
    out_starts = {(r["driver"], r["lap"]): r["start_us"] for r in laps if r["pit_out"]}
    result = []
    for row in laps:
        phase, flags, deleted = lap_context(row, events)
        number, lap = row["driver"], row["lap"]
        stint = next((s for s in stints if s["driver"] == number and s["first_lap"] <= lap
                      and (s["last_lap"] is None or lap <= s["last_lap"])), None)
        sectors = [row["s1_s"], row["s2_s"], row["s3_s"]]
        reason = ""
        if row["pit_out"]:
            reason = "Out-lap"
        elif row["pit_in"]:
            reason = "In-lap"
        elif deleted:
            reason = "Deleted"
        elif row["start_us"] is None or not row["duration_s"] or None in sectors:
            reason = "Incomplete"
        trace = None if reason else read_trace(db, row)
        quality = ({k: trace[k] for k in ("samples", "maxGap")} if trace else
                   window_quality(db, row, starts.get((number, lap + 1))))
        if trace and not trace["pieces"]:
            reason = "No distance coverage"
        first = stint["first_lap"] if stint else None
        age = (stint["initial_age"] + lap - first
               if stint and stint["initial_age"] is not None else None)
        result.append({
            "id": f"{number}-{lap}", "driver": number, "lap": lap, "phase": phase,
            "duration": row["duration_s"], "start": utc_label(row["start_us"]),
            "outStart": utc_label(out_starts.get((number, first))),
            "compound": stint["compound"] if stint else None, "age": age, "sectors": sectors,
            "i1": row["i1_kmh"], "i2": row["i2_kmh"], "trap": row["trap_kmh"],
            "reason": reason, "flags": flags, "trace": trace, "quality": quality,
        })
    comparable = [r for r in result if not r["reason"] and not r["flags"]]
    pairs = [(a, b) for a in comparable for b in comparable if a["driver"] > b["driver"]]
    if not pairs:
        raise ValueError("No eligible cross-driver comparison in this session")
    def pair_rank(pair):
        a, b = pair
        same_phase = a["phase"] is not None and a["phase"] == b["phase"]
        same_tyre = a["compound"] is not None and a["compound"] == b["compound"]
        return (same_phase, same_tyre, min(a["phase"] or 0, b["phase"] or 0),
                -(a["duration"] + b["duration"]))
    a, b = max(pairs, key=pair_rank)
    return {"session": SESSION, "drivers": drivers, "laps": result,
            "defaultPair": [a["id"], b["id"]], "sectorEnds": SECTOR_ENDS,
            "trackM": TRACK_M, "gapLimit": MAX_GAP_S}
