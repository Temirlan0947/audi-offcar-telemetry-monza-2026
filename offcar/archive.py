"""Checkpoint complete OpenF1 responses. A closed session can be replayed offline."""

import gzip
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SESSION = 11357
TEAM = "Audi"
API = "https://api.openf1.org/v1"


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, obj):
    atomic_write(path, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def micros(value):
    if value is None:
        return None
    delta = parse_time(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def api_url(endpoint, filters):
    # OpenF1 uses a literal operator between the field and value, even for '<'.
    query = "&".join(quote(key) + quote(op, safe="=") + quote(str(value), safe=":-")
                     for key, op, value in filters)
    return f"{API}/{endpoint}?{query}"


def fetch(url, allow_empty):
    for attempt in range(4):
        try:
            request = Request(url, headers={"User-Agent": "offcar-telemetry/1.0",
                                            "Accept-Encoding": "gzip"})
            with urlopen(request, timeout=45) as response:
                payload = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    payload = gzip.decompress(payload)
                return response.status, payload
        except HTTPError as exc:
            if exc.code == 404 and allow_empty:
                body = exc.read()
                if exc.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                if json.loads(body).get("detail") == "No results found.":
                    return 404, body
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            retry = exc.headers.get("Retry-After", "")
            time.sleep(float(retry) if retry.isdigit() else 2 ** attempt)
        except (URLError, TimeoutError, IncompleteRead, EOFError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def row_list(status, body):
    obj = json.loads(body)
    if status == 404 and obj == {"detail": "No results found."}:
        return []
    if status != 200 or not isinstance(obj, list):
        raise ValueError("Unexpected OpenF1 response format")
    return obj


class Archive:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / "manifest.json"
        if self.path.exists():
            self.manifest = json.loads(self.path.read_bytes())
        else:
            self.manifest = {"session_key": SESSION, "team": TEAM,
                             "status": "incomplete", "files": {}}

    def checkpoint(self, status):
        self.manifest["status"] = status
        write_json(self.path, self.manifest)

    def read(self, name):
        entry = self.manifest["files"][name]
        content = gzip.decompress((self.folder / name).read_bytes())
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError(f"Checksum mismatch in {name}")
        return row_list(entry["http_status"], content)

    def obtain(self, endpoint, driver=None, *, window=None, filters=(), allow_empty=False):
        directory = f"car-{driver}" if driver is not None else "session"
        suffix = f"-{window:02}" if window is not None else ""
        name = f"{directory}/{endpoint}{suffix}.json.gz"
        query = [("session_key", "=", SESSION)]
        if driver is not None:
            query.append(("driver_number", "=", driver))
        query.extend(filters)
        url = api_url(endpoint, query)
        cached = self.manifest["files"].get(name)
        if cached and cached["url"] == url:
            try:
                return self.read(name)
            except (OSError, EOFError, ValueError):
                print(f"Repairing {name}", flush=True)
        print(f"Downloading {name}", flush=True)
        start = time.perf_counter()
        status, payload = fetch(url, allow_empty)
        elapsed = time.perf_counter() - start
        rows = row_list(status, payload)
        if not allow_empty and not rows:
            raise ValueError(f"Missing required data: {endpoint} {driver or ''}")
        for row in rows:
            if row["session_key"] != SESSION or (
                driver is not None and row["driver_number"] != driver
            ):
                raise ValueError(f"Response contains an unexpected session or driver in {name}")
        atomic_write(self.folder / name, gzip.compress(payload, compresslevel=6, mtime=0))
        self.manifest["files"][name] = {
            "endpoint": endpoint, "driver": driver, "url": url,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "http_status": status, "rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest(),
            "download_seconds": round(elapsed, 4),
        }
        self.checkpoint("incomplete")
        return rows


def telemetry_windows(session):
    """Half-open chunks with unbounded first/last edges retain pre/post-session samples."""
    start = parse_time(session["date_start"])
    end = parse_time(session["date_end"])
    cut = start + timedelta(minutes=10)
    lower = []
    while cut < end:
        stamp = cut.isoformat(timespec="seconds").replace("+00:00", "Z")
        yield lower + [("date", "<", stamp)]
        lower = [("date", ">=", stamp)]
        cut += timedelta(minutes=10)
    yield lower


def download(archive, refresh=False):
    if refresh:
        archive.manifest["files"] = {}
    archive.checkpoint("incomplete")
    session, = archive.obtain("sessions")
    team = archive.obtain("drivers", filters=[("team_name", "=", TEAM)])
    numbers = sorted({r["driver_number"] for r in team if r["team_name"] == TEAM})
    if len(numbers) != 2:
        raise ValueError(f"Expected two Audi entries, found {numbers}")
    archive.obtain("race_control", allow_empty=True)
    for number in numbers:
        archive.obtain("laps", number)
        archive.obtain("stints", number, allow_empty=True)
        archive.obtain("pit", number, allow_empty=True)
        count = 0
        for i, filters in enumerate(telemetry_windows(session)):
            count += len(archive.obtain("car_data", number, window=i,
                                        filters=filters, allow_empty=True))
        if not count:
            raise ValueError(f"No telemetry returned for car {number}")
    archive.checkpoint("complete")
