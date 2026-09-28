"""A local cache of the parts of CI history that never change once a run is finished.

- Job lists of completed attempts, and job logs, are immutable.
- What a log says (its parsed reading) is stored per parser version, so re-scans don't re-read
  logs, and improved parsers re-read them automatically.
- API responses are kept with their ETag: GitHub answers an unchanged page with 304, which doesn't
  count against the rate limit.

Raw logs are optional (REDHERRING_KEEP_LOGS=0 keeps only readings, which is far smaller) and are
pruned once they're older than GitHub's own log retention.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

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
CREATE TABLE IF NOT EXISTS job_readings (
    repo TEXT NOT NULL, job_id INTEGER NOT NULL,
    version TEXT NOT NULL,          -- parser version that produced it
    reading TEXT NOT NULL,          -- JSON
    PRIMARY KEY (repo, job_id)
);
CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY,
    etag TEXT NOT NULL,
    body BLOB NOT NULL,             -- zlib-compressed JSON
    stored_at INTEGER NOT NULL
);
"""

# Logs above this (compressed) size are truncated to their tail: the failure is at the end.
_MAX_COMPRESSED = 4 * 1024 * 1024
_MAX_TEXT = 12 * 1024 * 1024
# GitHub keeps logs 90 days by default; ours can go a little later.
LOG_MAX_AGE_DAYS = 120
HTTP_MAX_AGE_DAYS = 30
_DAY = 86400


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


def keep_logs_default() -> bool:
    return os.environ.get("REDHERRING_KEEP_LOGS", "1").strip().lower() not in ("0", "false", "no")


class Cache:
    def __init__(self, path: Path | str | None = None, *, keep_logs: bool | None = None) -> None:
        if path == ":memory:":
            self._db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        else:
            p = Path(path) if path else default_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self.keep_logs = keep_logs_default() if keep_logs is None else keep_logs
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._db.executescript(_SCHEMA)
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version < 2:
                columns = {row[1] for row in self._db.execute("PRAGMA table_info(job_logs)")}
                if "fetched_at" not in columns:
                    self._db.execute("ALTER TABLE job_logs ADD COLUMN fetched_at INTEGER")
                    self._db.execute(
                        "UPDATE job_logs SET fetched_at = ? WHERE fetched_at IS NULL",
                        (int(time.time()),),
                    )
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    def _one(self, sql: str, args: tuple = ()) -> tuple | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def close(self) -> None:
        self._db.close()

    # -- attempt job lists ---------------------------------------------------------------

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

    # -- logs ----------------------------------------------------------------------------

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
        now = int(time.time())
        if text is None:
            self._exec(
                "INSERT OR REPLACE INTO job_logs (repo, job_id, status, log, fetched_at) "
                "VALUES (?, ?, 'gone', NULL, ?)",
                (repo, job_id, now),
            )
            return
        if not self.keep_logs:
            return
        if len(text) > _MAX_TEXT:
            text = text[-_MAX_TEXT:]
        blob = zlib.compress(text.encode("utf-8"), 6)
        while len(blob) > _MAX_COMPRESSED and len(text) > 1024:
            text = text[len(text) // 2 :]
            blob = zlib.compress(text.encode("utf-8"), 6)
        self._exec(
            "INSERT OR REPLACE INTO job_logs (repo, job_id, status, log, fetched_at) "
            "VALUES (?, ?, 'ok', ?, ?)",
            (repo, job_id, blob, now),
        )

    # -- readings (what a log says) ------------------------------------------------------

    def get_reading(self, repo: str, job_id: int, version: str) -> dict | None:
        row = self._one(
            "SELECT reading FROM job_readings WHERE repo=? AND job_id=? AND version=?",
            (repo, job_id, version),
        )
        return json.loads(row[0]) if row else None

    def put_reading(self, repo: str, job_id: int, version: str, reading: dict) -> None:
        self._exec(
            "INSERT OR REPLACE INTO job_readings VALUES (?, ?, ?, ?)",
            (repo, job_id, version, json.dumps(reading)),
        )

    # -- ETag-validated API responses ----------------------------------------------------

    def get_http(self, key: str) -> tuple[str, Any] | None:
        row = self._one("SELECT etag, body FROM http_cache WHERE key=?", (key,))
        if not row:
            return None
        return row[0], json.loads(zlib.decompress(row[1]))

    def put_http(self, key: str, etag: str, body: Any) -> None:
        blob = zlib.compress(json.dumps(body).encode("utf-8"), 6)
        self._exec(
            "INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?, ?)",
            (key, etag, blob, int(time.time())),
        )

    # -- housekeeping --------------------------------------------------------------------

    def prune(self, *, now: float | None = None) -> int:
        """Drop logs older than GitHub's retention and stale API responses. Returns rows removed.

        Readings stay: they're small and still useful after GitHub deletes the run.
        """
        now = now or time.time()
        removed = self._exec(
            "DELETE FROM job_logs WHERE fetched_at < ?", (int(now - LOG_MAX_AGE_DAYS * _DAY),)
        )
        removed += self._exec(
            "DELETE FROM http_cache WHERE stored_at < ?", (int(now - HTTP_MAX_AGE_DAYS * _DAY),)
        )
        if removed:
            with self._lock:
                pages, free = (
                    self._db.execute("PRAGMA page_count").fetchone()[0],
                    self._db.execute("PRAGMA freelist_count").fetchone()[0],
                )
                # Give the space back when a good part of the file is empty.
                if pages and free / pages > 0.25:
                    self._db.execute("VACUUM")
        return removed
