"""A local cache of the parts of CI history that never change once a run is finished.

Job lists of completed attempts and job logs are immutable, so re-scans only pay for the
runs list. Logs are stored compressed so parsers can be improved without re-downloading.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import zlib
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempt_jobs (
    repo TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
    jobs TEXT NOT NULL,
    PRIMARY KEY (repo, run_id, attempt)
);
CREATE TABLE IF NOT EXISTS job_logs (
    repo TEXT NOT NULL, job_id INTEGER NOT NULL,
    status TEXT NOT NULL,           -- ok | gone
    log BLOB,                       -- zlib-compressed text when status = ok
    PRIMARY KEY (repo, job_id)
);
"""

# Logs above this (compressed) size are truncated to their tail: the failure is at the end.
_MAX_COMPRESSED = 4 * 1024 * 1024
_MAX_TEXT = 12 * 1024 * 1024


def default_path() -> Path:
    if env := os.environ.get("REDHERRING_CACHE"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "redherring" / "cache.sqlite"


class Cache:
    def __init__(self, path: Path | str | None = None) -> None:
        if path == ":memory:":
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            p = Path(path) if path else default_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._db.execute(sql, args)

    def _one(self, sql: str, args: tuple = ()) -> tuple | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def close(self) -> None:
        self._db.close()

    def get_attempt_jobs(self, repo: str, run_id: int, attempt: int) -> list[dict] | None:
        row = self._one(
            "SELECT jobs FROM attempt_jobs WHERE repo=? AND run_id=? AND attempt=?",
            (repo, run_id, attempt),
        )
        return json.loads(row[0]) if row else None

    def put_attempt_jobs(self, repo: str, run_id: int, attempt: int, jobs: list[dict]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO attempt_jobs VALUES (?, ?, ?, ?)",
            (repo, run_id, attempt, json.dumps(jobs)),
        )

    def get_log(self, repo: str, job_id: int) -> tuple[str, str | None] | None:
        """(status, text) if cached, else None. status is "ok" or "gone"."""
        row = self._one(
            "SELECT status, log FROM job_logs WHERE repo=? AND job_id=?", (repo, job_id)
        )
        if not row:
            return None
        status, blob = row
        return status, zlib.decompress(blob).decode("utf-8") if blob else None

    def put_log(self, repo: str, job_id: int, text: str | None) -> None:
        if text is None:
            self._exec(
                "INSERT OR REPLACE INTO job_logs VALUES (?, ?, 'gone', NULL)", (repo, job_id)
            )
            return
        if len(text) > _MAX_TEXT:
            text = text[-_MAX_TEXT:]
        blob = zlib.compress(text.encode("utf-8"), 6)
        while len(blob) > _MAX_COMPRESSED and len(text) > 1024:
            text = text[len(text) // 2 :]
            blob = zlib.compress(text.encode("utf-8"), 6)
        self._exec("INSERT OR REPLACE INTO job_logs VALUES (?, ?, 'ok', ?)", (repo, job_id, blob))
