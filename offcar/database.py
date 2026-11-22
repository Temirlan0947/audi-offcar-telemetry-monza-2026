"""Materialise one verified raw snapshot into an indexed SQLite database."""

import os
import sqlite3
import time
from collections import defaultdict
from contextlib import closing

from .archive import SESSION, TEAM, micros


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE sessions (
    session_key INTEGER PRIMARY KEY, meeting_key INTEGER NOT NULL, circuit TEXT NOT NULL,
    year INTEGER NOT NULL, session_name TEXT NOT NULL, start_us INTEGER, end_us INTEGER
);
CREATE TABLE drivers (
    session_key INTEGER REFERENCES sessions, number INTEGER, team TEXT NOT NULL,
    first_name TEXT, last_name TEXT, code TEXT, PRIMARY KEY (session_key, number)
) WITHOUT ROWID;
CREATE TABLE telemetry (
    session_key INTEGER, driver INTEGER, utc_us INTEGER, speed_kmh INTEGER,
    throttle_raw INTEGER, brake_raw INTEGER, gear INTEGER, rpm INTEGER,
    PRIMARY KEY (session_key, driver, utc_us),
    FOREIGN KEY (session_key, driver) REFERENCES drivers
) WITHOUT ROWID;
CREATE TABLE laps (
    session_key INTEGER, driver INTEGER, lap INTEGER, start_us INTEGER, duration_s REAL,
    s1_s REAL, s2_s REAL, s3_s REAL, i1_kmh INTEGER, i2_kmh INTEGER, trap_kmh INTEGER,
    pit_out INTEGER, pit_in INTEGER, pit_in_evidence TEXT,
    PRIMARY KEY (session_key, driver, lap),
    FOREIGN KEY (session_key, driver) REFERENCES drivers
) WITHOUT ROWID;
CREATE TABLE stints (
    session_key INTEGER, driver INTEGER, stint INTEGER, first_lap INTEGER,
    last_lap INTEGER, compound TEXT, initial_age INTEGER,
    PRIMARY KEY (session_key, driver, stint),
    FOREIGN KEY (session_key, driver) REFERENCES drivers
) WITHOUT ROWID;
CREATE TABLE pit_visits (
    session_key INTEGER, driver INTEGER, lap INTEGER, utc_us INTEGER,
    lane_s REAL, stop_s REAL,
    FOREIGN KEY (session_key, driver) REFERENCES drivers
);
CREATE TABLE race_control (
    session_key INTEGER REFERENCES sessions, utc_us INTEGER, phase INTEGER,
    category TEXT, flag TEXT, scope TEXT, sector INTEGER, driver INTEGER,
    lap INTEGER, message TEXT
);
CREATE INDEX event_time ON race_control (session_key, utc_us);
"""


def load_database(archive, path):
    if archive.manifest["status"] != "complete":
        raise ValueError("Raw snapshot incomplete. Resume with 'collect'.")
    # The caller provides an empty staging file. No partial import reaches readers.
    grouped = defaultdict(list)
    for name, meta in sorted(archive.manifest["files"].items()):
        grouped[meta["endpoint"], meta["driver"]].append(name)

    def read(endpoint, driver=None):
        files = grouped.get((endpoint, driver))
        if not files:
            raise ValueError(f"Missing raw response: {endpoint}/{driver}")
        for name in files:
            yield from archive.read(name)

    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(SCHEMA)
        session, = read("sessions")
        if (session["session_key"], session["circuit_short_name"], session["session_name"],
            session["year"]) != (SESSION, "Monza", "Qualifying", 2026):
            raise ValueError("Snapshot does not match the Monza 2026 circuit profile")
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (
            SESSION, session["meeting_key"], session["circuit_short_name"], session["year"],
            session["session_name"], micros(session["date_start"]), micros(session["date_end"])))
        roster = {r["driver_number"]: r for r in read("drivers") if r["team_name"] == TEAM}
        if len(roster) != 2:
            raise ValueError("Two Audi drivers are required")
        for number, person in roster.items():
            key = (SESSION, number)
            db.execute("INSERT INTO drivers VALUES (?,?,?,?,?,?)", (
                *key, TEAM, person["first_name"], person["last_name"], person["name_acronym"]))
            db.executemany("INSERT INTO stints VALUES (?,?,?,?,?,?,?)", (
                (*key, r["stint_number"], r["lap_start"], r["lap_end"],
                 r["compound"], r["tyre_age_at_start"]) for r in read("stints", number)))
            pit = list(read("pit", number))
            db.executemany("INSERT INTO pit_visits VALUES (?,?,?,?,?,?)", (
                (*key, r["lap_number"], micros(r["date"]), r.get("lane_duration"),
                 r.get("stop_duration")) for r in pit))
            pit_laps = {r["lap_number"] for r in pit}
            laps = sorted(read("laps", number), key=lambda r: r["lap_number"])
            if not laps:
                raise ValueError(f"Empty lap table for car {number}")
            out_laps = {r["lap_number"] for r in laps if r["is_pit_out_lap"]}
            for r in laps:
                lap = r["lap_number"]
                reasons = []
                if lap in pit_laps:
                    reasons.append("pit record")
                if 2064 in (r.get("segments_sector_3") or []):
                    reasons.append("pit-lane mini-sector")
                if lap + 1 in out_laps:
                    reasons.append("next lap is an out-lap")
                db.execute("INSERT INTO laps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    *key, lap, micros(r["date_start"]), r["lap_duration"],
                    r["duration_sector_1"], r["duration_sector_2"], r["duration_sector_3"],
                    r["i1_speed"], r["i2_speed"], r["st_speed"], r["is_pit_out_lap"],
                    bool(reasons), ", ".join(reasons)))
            # The archived raw channels preserve out-of-range codes and NULLs.
            db.executemany("INSERT OR REPLACE INTO telemetry VALUES (?,?,?,?,?,?,?,?)", (
                (*key, micros(r["date"]), r["speed"], r["throttle"], r["brake"],
                 r["n_gear"], r["rpm"]) for r in read("car_data", number)))
            if not db.execute("SELECT 1 FROM telemetry WHERE session_key=? AND driver=? LIMIT 1",
                              key).fetchone():
                raise ValueError(f"Empty telemetry table for car {number}")
        db.executemany("INSERT INTO race_control VALUES (?,?,?,?,?,?,?,?,?,?)", (
            (SESSION, micros(e["date"]), e.get("qualifying_phase"), e["category"], e.get("flag"),
             e.get("scope"), e.get("sector"), e.get("driver_number"), e.get("lap_number"),
             e["message"]) for e in read("race_control")))
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("SQLite foreign key check failed")


def publish_database(source, destination):
    if not destination.exists():
        os.replace(source, destination)
        return
    # Windows prevents replacing a database file held by a SQL client. The SQLite
    # backup API commits a new snapshot in place, preserving active WAL readers.
    deadline = time.monotonic() + 10

    # Python 3.10 does not export SQLite's BUSY (5) and LOCKED (6) result codes.
    busy_codes = (getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6))

    def check_busy(status, remaining, total):
        if status in busy_codes and time.monotonic() > deadline:
            raise sqlite3.OperationalError("The database is busy with another transaction")

    with closing(sqlite3.connect(source)) as staged, closing(sqlite3.connect(destination)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        staged.backup(db, progress=check_busy, sleep=0.1)
