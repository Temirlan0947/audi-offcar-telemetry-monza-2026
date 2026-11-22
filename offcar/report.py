"""Build the portable offline viewer and benchmark the actual lap-reading path."""

import base64
import json
import os
import platform
import sqlite3
import statistics
import time
from contextlib import closing
from pathlib import Path

from .analysis import make_payload, read_trace
from .archive import SESSION, atomic_write


UI = Path(__file__).resolve().parent.parent / "ui"


def connect(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def render(db_path, html_path):
    with closing(connect(db_path)) as db:
        payload = make_payload(db)
        payload["counts"] = {str(r[0]): r[1] for r in db.execute(
            "SELECT driver,count(*) FROM telemetry GROUP BY driver")}
        payload["eventCount"] = db.execute("SELECT count(*) FROM race_control").fetchone()[0]
        payload["pitCount"] = db.execute("SELECT count(*) FROM pit_visits").fetchone()[0]
        payload["events"] = [dict(r) for r in db.execute(
            "SELECT strftime('%H:%M:%S', utc_us / 1000000, 'unixepoch') AS utc, "
            "phase,flag,message FROM race_control ORDER BY utc_us,rowid")]
    css = (UI / "style.css").read_text(encoding="utf8")
    for name in ("Barlow-Regular.ttf", "BarlowSemiCondensed-SemiBold.ttf"):
        encoded = base64.b64encode((UI / "fonts" / name).read_bytes()).decode()
        css = css.replace(f"fonts/{name}", "data:font/ttf;base64," + encoded)
    text = (UI / "index.html").read_text(encoding="utf8")
    code = "\n".join((UI / f).read_text(encoding="utf8")
                     for f in ("compare.js", "chart.js", "app.js"))
    safe_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    text = text.replace("/* FONT_LICENSE */", (UI / "fonts/OFL.txt").read_text(encoding="utf8"))
    text = text.replace("/* THEME */", css).replace("/* APPLICATION */", code)
    text = text.replace("{{SESSION_DATA}}", safe_json.replace("<", "\\u003c"))
    atomic_write(html_path, text.encode("utf8"))
    return payload


def benchmark(db_path, archive, pair, repetitions=40):
    durations = []
    with closing(connect(db_path)) as db:
        rows = db.execute("SELECT count(*) FROM telemetry").fetchone()[0]
        for _ in range(repetitions):
            start = time.perf_counter()
            for lap_id in pair:
                car, number = map(int, lap_id.split("-"))
                lap = db.execute("SELECT * FROM laps WHERE session_key=? AND driver=? AND lap=?",
                                 (SESSION, car, number)).fetchone()
                read_trace(db, lap)
            durations.append((time.perf_counter() - start) * 1000)
    files = archive.manifest["files"]
    return {
        "platform": platform.platform(), "architecture": platform.machine(),
        "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "session_key": SESSION, "samples": rows, "lap_pair": pair,
        "raw_response_bytes_gzip": sum((archive.folder / name).stat().st_size for name in files),
        "manifest_bytes": archive.path.stat().st_size, "sqlite_bytes": Path(db_path).stat().st_size,
        "download_requests": len(files),
        "source_request_seconds_sum": round(sum(r["download_seconds"] for r in files.values()), 3),
        "pair_read_and_align_ms_median": round(statistics.median(durations), 3),
        "pair_read_and_align_ms_p95": round(statistics.quantiles(durations,n=100,method="inclusive")[94], 3),
        "pair_read_and_align_trials": repetitions,
    }
