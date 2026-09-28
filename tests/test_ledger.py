"""The flake ledger keeps evidence after GitHub deletes old runs."""

import json

import pytest

from redherring import cli, ledger
from redherring.cache import Cache
from redherring.github import GitHub
from redherring.scan import Scanner
from redherring.why import KNOWN_FLAKY, LOOKS_REAL, explain

from .fakegh import REPO, Fake, at
from .test_scan_why import FLAKY_LOG, UNTIL, _Frozen, history


def scanner(fake: Fake) -> Scanner:
    return Scanner(GitHub("t", transport=fake.transport()), Cache(":memory:"), workers=2)


def test_round_trip_and_merge(tmp_path):
    fake = Fake()
    history(fake)
    r = scanner(fake).scan(REPO, days=30, until=UNTIL)
    path = tmp_path / "ledger.json"
    ledger.save(path, REPO, r.failed_jobs)
    back = ledger.load(path, REPO)
    assert {(j.run_id, j.attempt, j.job_id) for j in back} == {
        (j.run_id, j.attempt, j.job_id) for j in r.failed_jobs
    }
    fetch = next(j for j in back if j.job_id == 1001)
    assert fetch.tests[0].test_id == "tests/test_net.py::test_fetch" and fetch.os == "linux"
    net = next(j for j in back if j.job_id == 1011)
    assert net.infra.category == "network"

    # Every fixture job finished on 2026-09-20: kept with a week to spare, dropped after.
    assert len(ledger.merge(back, [], keep_days=6, now=at("2026-09-25T00:00:00"))) == 6
    assert ledger.merge(back, [], keep_days=6, now=at("2026-09-27T00:00:00")) == []

    # Newer evidence for the same job replaces the old entry instead of duplicating it.
    fresh = [j for j in r.failed_jobs if j.job_id == 1001]
    fresh[0].hint = "newer"
    merged = ledger.merge(back, fresh, keep_days=30, now=at("2026-09-25T00:00:00"))
    assert len(merged) == 6
    assert next(j for j in merged if j.job_id == 1001).hint == "newer"


def test_wrong_repo_or_version_is_refused(tmp_path):
    path = tmp_path / "ledger.json"
    ledger.save(path, "other/repo", [])
    with pytest.raises(ValueError, match="other/repo"):
        ledger.load(path, REPO)
    path.write_text(json.dumps({"version": 99, "repo": REPO}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        ledger.load(path, REPO)


def test_missing_ledger_is_empty(tmp_path):
    assert ledger.load(tmp_path / "nope.json", REPO) == []


def test_ledger_history_outlives_the_window(tmp_path, monkeypatch):
    # Evidence from an old scan...
    old = Fake()
    history(old)
    path = tmp_path / "ledger.json"
    ledger.save(path, REPO, scanner(old).scan(REPO, days=30, until=UNTIL).failed_jobs)

    # ...after GitHub deleted those runs: the repo now only has a new failing run.
    fake = Fake()
    fake.run(201, conclusion="failure", sha="P", created="2026-09-26T12:00:00")
    fake.job(
        201,
        1,
        2001,
        "test (ubuntu)",
        "failure",
        log=FLAKY_LOG + "FAILED tests/test_new.py::test_x - no\n",
    )
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)

    without = explain(scanner(fake), REPO, 201, days=30)
    assert {f.test_id: f.verdict for f in without.findings}[
        "tests/test_net.py::test_fetch"
    ] == LOOKS_REAL

    with_ledger = explain(scanner(fake), REPO, 201, days=30, prior=ledger.load(path, REPO))
    verdicts = {f.test_id: f.verdict for f in with_ledger.findings}
    assert verdicts == {
        "tests/test_net.py::test_fetch": KNOWN_FLAKY,
        "tests/test_new.py::test_x": LOOKS_REAL,
    }


def test_cli_scan_writes_and_grows_the_ledger(tmp_path, monkeypatch, capsys):
    fake = Fake()
    history(fake)
    monkeypatch.setattr(cli, "resolve_token", lambda: "t")
    monkeypatch.setattr(
        cli, "GitHub", lambda token, on_wait=None: GitHub(token, transport=fake.transport())
    )
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    path = tmp_path / "flakes.json"
    args = ["scan", REPO, "--format", "json", "--cache", ":memory:", "-q", "--ledger", str(path)]
    assert cli.main(args) == 0
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first["repo"] == REPO and len(first["entries"]) == 6
    capsys.readouterr()

    # A narrower second scan keeps the older entries and reports them as history.
    assert cli.main([*args[:2], "--days", "3", *args[2:]]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ledger_jobs_used"] > 0
    assert out["flaky_tests"][0]["times"] == 2
    assert len(json.loads(path.read_text(encoding="utf-8"))["entries"]) == 6
