import gzip
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from offcar.analysis import TRACK_M, build_trace, lap_context, make_payload
from offcar.archive import Archive, SESSION, micros
from offcar.database import load_database, publish_database


class ArchiveTests(unittest.TestCase):
    def test_source_precision_and_timezones(self):
        instant = micros("2026-09-05T14:07:09.286123Z")
        self.assertEqual(instant, micros("2026-09-05T16:07:09.286123+02:00"))
        self.assertEqual(instant + 1, micros("2026-09-05T14:07:09.286124Z"))

    def test_checkpoint_is_reusable_and_damaged_content_is_repaired(self):
        payload = b'[{"session_key":11357,"driver_number":27}]'
        with tempfile.TemporaryDirectory() as folder:
            archive = Archive(folder)
            with patch("offcar.archive.fetch", return_value=(200, payload)) as fetch:
                archive.obtain("laps", 27)
                archive = Archive(folder)  # Simulate a process restart.
                archive.obtain("laps", 27)
                self.assertEqual(fetch.call_count, 1)
                (Path(folder) / "car-27/laps.json.gz").write_bytes(gzip.compress(b"[]"))
                with self.assertRaisesRegex(ValueError, "Checksum"):
                    archive.read("car-27/laps.json.gz")
                archive.obtain("laps", 27)
                self.assertEqual(fetch.call_count, 2)
            self.assertEqual(archive.manifest["status"], "incomplete")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                load_database(archive, Path(folder) / "new.sqlite")

    def test_no_results_keeps_exact_response_body(self):
        payload = b'{"detail":"No results found."}'
        with tempfile.TemporaryDirectory() as folder:
            archive = Archive(folder)
            with patch("offcar.archive.fetch", return_value=(404, payload)):
                self.assertEqual(archive.obtain("pit", 5, allow_empty=True), [])
            path = Path(folder) / "car-5/pit.json.gz"
            self.assertEqual(gzip.decompress(path.read_bytes()), payload)
            self.assertEqual(archive.manifest["files"]["car-5/pit.json.gz"]["http_status"], 404)


class AlignmentTests(unittest.TestCase):
    @staticmethod
    def readings():
        # 80 s at constant speed covers the Monza centreline exactly.
        return [(i * 250_000, TRACK_M * 3.6 / 80, 100, 0, 8, 11000) for i in range(321)]

    def test_missing_time_splits_distance_and_recovers_from_finish(self):
        rows = [r for r in self.readings() if not 30_000_000 <= r[0] <= 32_000_000]
        trace = build_trace(rows, 0, 80, (26, 27))
        self.assertEqual(len(trace["pieces"]), 2)
        self.assertEqual(trace["anchors"], ["start", "finish"])
        self.assertAlmostEqual(trace["maxGap"], 2.5)
        left, right = trace["missing"][0]
        self.assertAlmostEqual(right-left, 2.5 * TRACK_M / 80, places=3)
        self.assertEqual(trace["pieces"][-1][-1][0], TRACK_M)

    def test_unknown_controls_are_distinct_from_released_controls(self):
        rows = self.readings()
        rows[24] = (6_000_000, rows[24][1], 104, 104, 8, 11000)
        rows[25] = (6_250_000, rows[25][1], None, None, 8, 11000)
        trace = build_trace(rows, 0, 80, (26, 27))
        self.assertEqual(trace["unknownPedals"], 2)
        self.assertEqual(trace["pieces"][0][24][3:5], [None, None])
        self.assertEqual(trace["pieces"][0][23][3:5], [100, 0])
        self.assertEqual(trace["samples"], 320)
        self.assertEqual(trace["missing"], [])

    def test_finish_line_sample_is_excluded_from_observed_peak(self):
        rows = self.readings()
        rows[-1] = (80_000_000, 300, 100, 0, 8, 12000)
        trace = build_trace(rows, 0, 80, (26, 27))
        self.assertLess(trace["vmax"], 300)
        self.assertFalse(trace["pieces"][0][-1][-1])

    def test_caution_already_active_and_late_deletion(self):
        lap = {"start_us": 100_000_000, "duration_s": 80, "driver": 5, "lap": 11}
        base = {"phase": 2, "scope": "Sector", "driver": None, "sector": 2}
        events = [dict(base, utc_us=t, flag=flag, message=msg) for t, flag, msg in (
            (0, None, "SESSION STARTED"), (97_000_000, "YELLOW", "YELLOW IN TRACK SECTOR 2"),
            (101_000_000, "CLEAR", "CLEAR IN TRACK SECTOR 2"),
            (199_000_000, None, "CAR 5 (BOR) TIME 1:22.517 DELETED - TRACK LIMITS LAP 11"),
        )]
        self.assertEqual(lap_context(lap, events), (2, ["YELLOW IN TRACK SECTOR 2"], True))


class MonzaSnapshotTests(unittest.TestCase):
    def test_reader_can_hold_old_snapshot_while_writer_publishes(self):
        with tempfile.TemporaryDirectory() as temp:
            old = Path(temp) / "published.sqlite"
            new = Path(temp) / "new.sqlite"
            for version, path in enumerate((old, new)):
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("CREATE TABLE sample(version)")
                    db.execute("INSERT INTO sample VALUES (?)", (version,))
            with closing(sqlite3.connect(old)) as reader:
                reader.execute("BEGIN")
                self.assertEqual(reader.execute("SELECT version FROM sample").fetchone(), (0,))
                publish_database(new, old)
                self.assertEqual(reader.execute("SELECT version FROM sample").fetchone(), (0,))
                reader.rollback()
                self.assertEqual(reader.execute("SELECT version FROM sample").fetchone(), (1,))

    def test_supplied_snapshot_matches_timing_and_driver_scope(self):
        folder = Path(__file__).resolve().parents[1] / "data/raw" / str(SESSION)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "telemetry.sqlite"
            load_database(Archive(folder), path)
            with closing(sqlite3.connect(path)) as db:
                counts = db.execute("SELECT driver,count(*) FROM telemetry GROUP BY driver").fetchall()
                self.assertEqual(counts, [(5, 16909), (27, 16909)])
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                db.row_factory = sqlite3.Row
                data = make_payload(db)
        self.assertEqual(data["defaultPair"], ["27-11", "5-11"])
        self.assertEqual(len(data["laps"]), 24)
        self.assertEqual(sum(not lap["reason"] for lap in data["laps"]), 8)
        eligible = {lap["id"]: lap for lap in data["laps"] if not lap["reason"]}
        a, b = [eligible[key] for key in data["defaultPair"]]
        self.assertAlmostEqual(b["duration"] - a["duration"], -0.262)
        self.assertEqual((a["trace"]["vmax"], a["trap"]), (336, 327))
        self.assertEqual((b["trace"]["vmax"], b["trap"]), (335, 326))
        self.assertEqual((a["phase"], b["phase"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
