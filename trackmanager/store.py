"""File-based track database on /arc.

One writer per file class (plan §3.2): the controller writes state/, events.jsonl,
jobs/ and controller/; workers write their own outputs and logs; people write inbox/.
JSON files are replaced atomically, JSONL files are append-only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def age_s(stamp: str | None) -> float | None:
    return None if not stamp else (now() - parse_iso(stamp)).total_seconds()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write_json(path, data) -> None:
    """Replace a JSON file atomically, so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n")
    os.replace(tmp, path)


def create_json(path, data) -> None:
    """Create a JSON file that must not exist yet (attempt files are never overwritten)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def append_jsonl(path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path) -> list[dict]:
    """Read a JSONL file, skipping a torn last line left by a killed writer."""
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return rows


class Root:
    """Paths inside one run root (the track database for one project or test run)."""

    def __init__(self, path):
        self.path = Path(path)

    def state(self, eb: str) -> Path:
        return self.path / "state" / f"{eb}.json"

    @property
    def summary(self) -> Path:
        return self.path / "state" / "summary.json"

    @property
    def events(self) -> Path:
        return self.path / "events.jsonl"

    @property
    def jobs(self) -> Path:
        return self.path / "jobs" / "jobs.jsonl"

    @property
    def jobs_summary(self) -> Path:
        return self.path / "jobs" / "summary.json"

    @property
    def lease(self) -> Path:
        return self.path / "controller" / "lease.json"

    @property
    def heartbeat(self) -> Path:
        return self.path / "controller" / "heartbeat.json"

    def waiting(self, session_id: str) -> Path:
        return self.path / "controller" / f"waiting_{session_id}.json"

    @property
    def inbox(self) -> Path:
        return self.path / "inbox" / "decisions"

    @property
    def inbox_archive(self) -> Path:
        return self.path / "inbox" / "archive"

    def track(self, eb: str) -> Path:
        return self.path / "tracks" / eb

    def iter_dir(self, eb: str, n: int) -> Path:
        """One review iteration: inputs/, attempt_<k>.json, logs/, work/, products/, result.json."""
        return self.track(eb) / f"iter_{n}"

    def inputs(self, eb: str, n: int) -> Path:
        return self.iter_dir(eb, n) / "inputs"

    def result(self, eb: str, n: int) -> Path:
        return self.iter_dir(eb, n) / "result.json"

    def review(self, eb: str, n: int) -> Path:
        return self.iter_dir(eb, n) / "review.json"

    def attempt(self, eb: str, n: int, k: int) -> Path:
        return self.iter_dir(eb, n) / f"attempt_{k}.json"

    def logs(self, eb: str, n: int, k: int) -> Path:
        return self.iter_dir(eb, n) / "logs" / f"attempt_{k}"
