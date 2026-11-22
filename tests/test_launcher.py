import gzip
import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import telemetry
from offcar.archive import Archive, SESSION
from offcar.database import SCHEMA


SNAPSHOT = Path(__file__).resolve().parents[1] / "data" / "raw" / str(SESSION)


class ReviewerLaunchTests(unittest.TestCase):
    def test_first_launch_downloads_then_restarts_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve() / "Reviewer copy #1"
            reference = Archive(shutil.copytree(SNAPSHOT, project / "data/raw" / str(SESSION)))
            requests = {entry["url"]: (entry["http_status"], reference.folder / name)
                        for name, entry in reference.manifest["files"].items()}
            demo = project / "monza-demo"
            viewer = demo / "comparison.html"

            def replay_response(url, allow_empty):
                status, file = requests[url]
                return status, gzip.decompress(file.read_bytes())

            with (
                patch.object(telemetry, "__file__", str(project / "telemetry.py")),
                patch("offcar.archive.fetch", side_effect=replay_response) as fetch,
                patch("telemetry.webbrowser.open_new_tab", return_value=True) as browser,
                redirect_stdout(io.StringIO()) as console,
            ):
                telemetry.main([])
                self.assertEqual(fetch.call_count, len(requests))
                browser.assert_called_once_with(viewer.as_uri())
                self.assertTrue(viewer.is_file())
                with closing(sqlite3.connect(demo / "data/telemetry.sqlite")) as db:
                    counts = db.execute(
                        "SELECT driver,count(*) FROM telemetry GROUP BY driver ORDER BY driver"
                    ).fetchall()
                self.assertEqual(counts, [(5, 16909), (27, 16909)])
                self.assertIn("SQLite integrity and foreign key checks passed.", console.getvalue())

                fetch.reset_mock()
                fetch.side_effect = AssertionError("Complete archive must replay offline")
                browser.return_value = False
                telemetry.main([])
                fetch.assert_not_called()
                self.assertEqual(browser.call_count, 2)
                self.assertIn(str(viewer), console.getvalue())
                self.assertIn("Browser launch unavailable", console.getvalue())

    def test_foreign_key_violation_prevents_publication_and_browser_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve()
            demo = project / "monza-demo"
            shutil.copytree(SNAPSHOT, demo / "data/raw" / str(SESSION))
            viewer = demo / "comparison.html"
            viewer.write_text("previous comparison", encoding="utf8")
            # Simulate a structurally readable staging database with an orphaned driver.
            damaged_schema = SCHEMA + """
                PRAGMA foreign_keys=OFF;
                INSERT INTO drivers VALUES (-1,99,'Audi','Test','Driver','TST');
                PRAGMA foreign_keys=ON;
            """
            with (
                patch.object(telemetry, "__file__", str(project / "telemetry.py")),
                patch("offcar.archive.fetch", side_effect=AssertionError("Unexpected request")),
                patch("offcar.database.SCHEMA", damaged_schema),
                patch("telemetry.webbrowser.open_new_tab") as browser,
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(ValueError, "foreign key check failed"):
                    telemetry.main([])
                browser.assert_not_called()
            self.assertEqual(viewer.read_text(encoding="utf8"), "previous comparison")
            self.assertFalse((demo / "data/telemetry.sqlite").exists())
