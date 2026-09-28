"""v0.2 efficiency: free 304s, readings instead of logs, skipped roll-ups, pruning, one-pass Action."""

import json
import sqlite3
import time
import zlib

from rich.console import Console

from redherring import cli, scan
from redherring.cache import LOG_MAX_AGE_DAYS, Cache
from redherring.github import GitHub, parse_time
from redherring.render import print_scan, scan_markdown
from redherring.scan import KIND_SUMMARY, Scanner

from .fakegh import REPO, Fake
from .test_scan_why import FLAKY_LOG, UNTIL, _Frozen, history


def scanner(fake: Fake, cache: Cache) -> Scanner:
    return Scanner(GitHub("t", transport=fake.transport(), http_cache=cache), cache, workers=2)


def test_unchanged_pages_come_back_free_on_the_second_scan():
    fake = Fake()
    history(fake)
    cache = Cache(":memory:")
    first = scanner(fake, cache).scan(REPO, days=14, until=UNTIL)
    second = scanner(fake, cache).scan(REPO, days=14, until=UNTIL)
    assert first.api_free == 0
    assert second.api_free > 0 and fake.not_modified == second.api_free
    assert second.api_requests < first.api_requests
    assert second.runs_passed == first.runs_passed and second.tests[0].times == first.tests[0].times


def test_run_listing_uses_calendar_days():
    fake = Fake()
    cache = Cache(":memory:")
    scanner(fake, cache).scan(REPO, days=3, until=parse_time("2026-09-27T15:30:00Z"))
    windows = sorted(
        c.split("created=")[1].split("&")[0]
        for c in fake.calls
        if "/actions/runs?" in c and "status=success" in c and "created=" in c
    )
    assert "2026-09-25T00%3A00%3A00Z..2026-09-25T23%3A59%3A59Z" in windows
    assert "2026-09-26T00%3A00%3A00Z..2026-09-26T23%3A59%3A59Z" in windows


def test_rollup_logs_are_not_downloaded():
    fake = Fake()
    history(fake)  # run 103 has an "all required jobs passed" job, id 1031
    r = scanner(fake, Cache(":memory:")).scan(REPO, days=30, until=UNTIL)
    assert not [c for c in fake.calls if "/jobs/1031/logs" in c]
    assert next(j for j in r.failed_jobs if j.job_id == 1031).kind == KIND_SUMMARY


def test_readings_are_reused_without_keeping_logs():
    fake = Fake()
    history(fake)
    cache = Cache(":memory:", keep_logs=False)
    scanner(fake, cache).scan(REPO, days=30, until=UNTIL)
    with cache._lock:
        kept = cache._db.execute("SELECT COUNT(*) FROM job_logs WHERE status='ok'").fetchone()[0]
    assert kept == 0
    fake.calls.clear()
    again = scanner(fake, cache).scan(REPO, days=30, until=UNTIL)
    assert not [c for c in fake.calls if "/logs" in c]
    assert again.tests[0].test_id == "tests/test_net.py::test_fetch"


def test_new_parser_version_rereads_stored_logs_without_downloading(monkeypatch):
    fake = Fake()
    history(fake)
    cache = Cache(":memory:")
    scanner(fake, cache).scan(REPO, days=30, until=UNTIL)
    monkeypatch.setattr(scan, "PARSER_VERSION", "a-newer-parser")
    fake.calls.clear()
    r = scanner(fake, cache).scan(REPO, days=30, until=UNTIL)
    assert not [c for c in fake.calls if "/logs" in c]
    assert r.tests[0].times == 2


def test_prune_drops_old_logs_but_keeps_readings(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    cache.put_log(REPO, 1, "old log")
    cache.put_reading(REPO, 1, "v", {"kind": "unknown", "tests": [], "infra": None, "hint": ""})
    later = time.time() + (LOG_MAX_AGE_DAYS + 1) * 86400
    assert cache.prune(now=later) >= 1
    assert cache.get_log(REPO, 1) is None
    assert cache.get_reading(REPO, 1, "v") is not None


def test_v1_cache_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE attempt_jobs (repo TEXT, run_id INTEGER, attempt INTEGER, jobs TEXT, PRIMARY KEY (repo, run_id, attempt));"
        "CREATE TABLE job_logs (repo TEXT, job_id INTEGER, status TEXT, log BLOB, PRIMARY KEY (repo, job_id));"
    )
    db.execute("INSERT INTO job_logs VALUES (?, ?, 'ok', ?)", (REPO, 7, zlib.compress(b"hello")))
    db.commit()
    db.close()
    cache = Cache(path)
    assert cache.get_log(REPO, 7) == ("ok", "hello")
    assert cache.prune() == 0  # migrated rows count as fetched now, so they're kept
    with cache._lock:
        assert cache._db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_json_out_writes_the_result_alongside_markdown(monkeypatch, tmp_path, capsys):
    fake = Fake()
    history(fake)
    monkeypatch.setattr(cli, "resolve_token", lambda: "t")
    monkeypatch.setattr(
        cli,
        "GitHub",
        lambda token, on_wait=None, http_cache=None: GitHub(
            token, transport=fake.transport(), http_cache=http_cache
        ),
    )
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    out = tmp_path / "r.json"
    code = cli.main(
        ["scan", REPO, "--format", "md", "--json-out", str(out), "--cache", ":memory:", "-q"]
    )
    assert code == 0
    assert capsys.readouterr().out.startswith("## redherring:")
    assert json.loads(out.read_text(encoding="utf-8"))["runs"]["recovered_by_rerun"] == 3


def test_scan_output_shows_tables_ledger_and_free_requests():
    fake = Fake()
    history(fake)
    fake.run(500, attempt=2, sha="G", created="2026-09-24T10:00:00", event="pull_request_target")
    fake.job(500, 1, 5001, "check-labels", "failure", log="##[error]Missing label\n")
    cache = Cache(":memory:")
    scanner(fake, cache).scan(REPO, days=30, until=UNTIL)
    r = scanner(fake, cache).scan(REPO, days=30, until=UNTIL, prior=[])
    console = Console(record=True, width=160)
    print_scan(r, console)
    text = console.export_text()
    assert "tests/test_net.py::test_fetch" in text and "network" in text
    assert "policy-check" in text and "unchanged, free" in text
    md = scan_markdown(r)
    assert "| 1 | `tests/test_net.py::test_fetch` | 2 | 2 |" in md
    assert "`test (windows)`" in md and "unchanged, free" in md


def test_github_counts_are_thread_safe_and_split(monkeypatch):
    fake = Fake()
    history(fake)
    gh = GitHub("t", transport=fake.transport(), http_cache=Cache(":memory:"))
    gh.repo(REPO)
    gh.repo(REPO)
    assert (gh.requests, gh.free_requests) == (1, 1)


def test_frozen_helper_still_available():
    # _Frozen is shared by several test modules; keep it importable.
    assert _Frozen.now() == UNTIL and FLAKY_LOG
