"""Audi off-car telemetry | Monza qualifying 2026 | Python 3.10+."""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
import webbrowser
from contextlib import closing
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Audi off-car telemetry requires Python 3.10 or newer.")

from offcar.analysis import make_payload
from offcar.archive import Archive, SESSION, download, write_json
from offcar.database import load_database, publish_database
from offcar.report import benchmark, connect, render


def main(argv=None):
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(usage="%(prog)s [-h] [{collect,rebuild,benchmark} ...]",
                                     description=__doc__, epilog=(
        "Run without arguments to download both Audi cars, verify SQLite and "
        "open the lap comparison in your browser. Results go in monza-demo/."
    ))
    parser.set_defaults(workspace=project / "monza-demo", refresh=False)
    commands = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("collect", "Resume a download, build SQLite and write the offline viewer"),
        ("rebuild", "Rebuild SQLite and the viewer from the verified local archive"),
        ("benchmark", "Time local reads for the default lap pair"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--workspace", type=Path, default=project,
                         help="Folder containing data/ and comparison.html")
        if name == "collect":
            sub.add_argument("--refresh", action="store_true",
                             help="Fetch all responses again from OpenF1")
    args = parser.parse_args(argv)
    interactive = args.command is None
    workspace = args.workspace.resolve()
    data = workspace / "data"
    data.mkdir(parents=True, exist_ok=True)
    db_path = data / "telemetry.sqlite"
    html_path = workspace / "comparison.html"

    # SQLite releases this advisory lock on process exit, including Ctrl+C/crashes.
    # Readers of a published database or HTML file never acquire the writer lock.
    with closing(sqlite3.connect(data / ".writer.lock", timeout=3)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        archive = Archive(data / "raw" / str(SESSION))
        if args.command == "benchmark":
            with closing(connect(db_path)) as db:
                pair = make_payload(db)["defaultPair"]
            print(json.dumps(benchmark(db_path, archive, pair), indent=2))
            return
        if interactive:
            print(f"Audi | Monza qualifying 2026 | OpenF1 session {SESSION}", flush=True)
            print(f"Download and resume folder: {archive.folder}", flush=True)
        if interactive or args.command == "collect":
            download(archive, args.refresh)
        if archive.manifest["status"] != "complete":
            raise ValueError("Raw snapshot incomplete or missing. Run 'collect' to resume.")
        with tempfile.TemporaryDirectory(dir=data, prefix=".build-") as stage:
            new_db = Path(stage) / "telemetry.sqlite"
            new_html = Path(stage) / "comparison.html"
            start = time.perf_counter()
            load_database(archive, new_db)
            imported = time.perf_counter()
            payload = render(new_db, new_html)
            rendered = time.perf_counter()
            metrics = benchmark(new_db, archive, payload["defaultPair"])
            metrics["archive_to_sqlite_s"] = round(imported - start, 4)
            metrics["sqlite_to_html_s"] = round(rendered - imported, 4)
            metrics["html_bytes"] = new_html.stat().st_size
            write_json(Path(stage) / "benchmark.json", metrics)
            publish_database(new_db, db_path)
            os.replace(new_html, html_path)
            os.replace(Path(stage) / "benchmark.json", data / "benchmark.json")
        print(f"{metrics['samples']:,} measurements | {len(payload['laps'])} laps | "
              f"default pair {' / '.join(payload['defaultPair'])}")

    if interactive:
        print("SQLite integrity and foreign key checks passed.")
        for car in payload["drivers"]:
            count = payload["counts"][str(car["number"])]
            print(f"  #{car['number']} {car['first_name']} {car['last_name']}: "
                  f"{count:,} telemetry samples")
    print(f"Open {html_path}", flush=True)
    if interactive:
        try:
            opened = webbrowser.open_new_tab(html_path.as_uri())
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            print("Browser launch unavailable. Open the HTML file shown above.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Interrupted. Run the same command to resume.")
    except (OSError, ValueError, sqlite3.Error) as error:
        sys.exit(str(error))
