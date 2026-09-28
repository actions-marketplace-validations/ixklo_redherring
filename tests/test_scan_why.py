"""Scan and why, end to end against the fake Actions API."""

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from redherring import cli
from redherring.cache import Cache
from redherring.github import GitHub
from redherring.scan import KIND_EXPIRED, KIND_GATE, KIND_SUMMARY, Scanner
from redherring.why import (
    ALREADY_FAILING,
    INFRASTRUCTURE,
    INVESTIGATE,
    KNOWN_FLAKY,
    LOOKS_REAL,
    NOTHING,
    RERUN,
    explain,
    parse_target,
)

from .fakegh import REPO, Fake, at

UNTIL = at("2026-09-27T00:00:00")
FLAKY_LOG = "2026-09-20T10:04:00.0000000Z FAILED tests/test_net.py::test_fetch - ConnectionError\n"
NET_LOG = "npm warn retrying\nError: socket hang up\n##[error]Process completed with exit code 1.\n"


def history(fake: Fake) -> None:
    # 101: failed once on a flaky test, green on re-run.
    fake.run(101, attempt=2, sha="A", created="2026-09-20T10:00:00", updated="2026-09-20T10:30:00")
    fake.job(101, 1, 1001, "test (ubuntu)", "failure", end="2026-09-20T10:05:00", log=FLAKY_LOG)
    fake.job(101, 1, 1002, "lint", "success")
    # 102: network on attempt 1, the same flaky test on attempt 2, green on attempt 3 (windows).
    fake.run(102, attempt=3, sha="B", created="2026-09-21T10:00:00", updated="2026-09-21T11:00:00")
    fake.job(102, 1, 1011, "test (windows)", "failure", labels=("windows-latest",), log=NET_LOG)
    fake.job(102, 2, 1021, "test (windows)", "failure", labels=("windows-latest",), log=FLAKY_LOG)
    # 103: a roll-up job and a PR policy check; not flakiness.
    fake.run(103, attempt=2, sha="C", created="2026-09-22T10:00:00")
    fake.job(
        103,
        1,
        1031,
        "all required jobs passed",
        "failure",
        log="##[error]Process completed with exit code 1.\n",
    )
    fake.job(
        103,
        1,
        1032,
        "check-issue-link",
        "failure",
        log="##[error]PR author must be assigned to the linked issue.\n",
    )
    # 104: plain green. 105: plain red. 106: re-run to green but the log has expired.
    fake.run(104, sha="D", created="2026-09-23T10:00:00")
    fake.run(105, conclusion="failure", sha="E", created="2026-09-24T10:00:00")
    fake.run(106, attempt=2, sha="F", created="2026-09-25T10:00:00")
    fake.job(106, 1, 1061, "test (ubuntu)", "failure", log=None)


def make(fake: Fake, cache: Cache | None = None, sleeps: list | None = None) -> Scanner:
    gh = GitHub(
        "t0ken",
        transport=fake.transport(),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )
    return Scanner(gh, cache or Cache(":memory:"), workers=2)


def test_scan_counts_and_classifies():
    fake = Fake()
    history(fake)
    r = make(fake).scan(REPO, days=30, until=UNTIL)
    assert r.runs_passed == 5 and r.runs_failed == 1
    assert {x["id"] for x in r.recovered_runs} == {101, 102, 103, 106}
    assert r.runs_went_red == 5
    assert r.herring_runs == 3  # 103 was only a roll-up job and a policy check

    kinds = {j.job_id: j.kind for j in r.failed_jobs}
    assert kinds[1031] == KIND_SUMMARY and kinds[1032] == KIND_GATE and kinds[1061] == KIND_EXPIRED

    [t] = r.tests
    assert t.test_id == "tests/test_net.py::test_fetch"
    assert (t.times, t.commits, t.oses) == (2, 2, ["linux", "windows"])
    assert t.message == "ConnectionError"

    assert dict(r.cause_counts()) == {"flaky tests": 2, "network": 1, "log expired": 1}
    assert [(name, total) for name, total, _ in r.job_causes()] == [
        ("test (ubuntu)", 1),
        ("test (windows)", 1),
    ]
    # Red time for 101: attempt 1 ended 10:05, the run went green at 10:30.
    assert 25 * 60 in [round(s) for s in r.red_seconds]


def test_second_scan_is_served_from_cache():
    fake = Fake()
    history(fake)
    cache = Cache(":memory:")
    make(fake, cache).scan(REPO, days=30, until=UNTIL)
    fake.calls.clear()
    make(fake, cache).scan(REPO, days=30, until=UNTIL)
    assert not [c for c in fake.calls if "/jobs" in c or "/logs" in c]


def test_window_is_split_around_the_1000_result_cap():
    fake = Fake(cap_total=1500)
    history(fake)
    r = make(fake).scan(REPO, days=30, until=UNTIL)
    listing = [
        c
        for c in fake.calls
        if c.startswith(f"/repos/{REPO}/actions/runs?") and "status=success" in c
    ]
    assert len(listing) > 10  # it had to split
    assert r.runs_passed == 5  # and still saw every run exactly once


def test_rate_limit_waits_and_retries():
    fake = Fake(
        interrupts=[
            (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 5)}),
            (429, {"retry-after": "3"}),
        ]
    )
    sleeps: list[float] = []
    gh = make(fake, sleeps=sleeps).gh
    assert gh.repo(REPO)["default_branch"] == "main"
    assert len(sleeps) == 2 and sleeps[1] == 3.0


def failing_run(fake: Fake) -> None:
    history(fake)
    # Main is already red on test_legacy.
    fake.run(150, conclusion="failure", branch="main", sha="M", created="2026-09-26T09:00:00")
    fake.job(
        150, 1, 1501, "test (ubuntu)", "failure", log="FAILED tests/test_old.py::test_legacy - x\n"
    )
    # The PR run under question.
    fake.run(201, conclusion="failure", branch="feature", sha="P", created="2026-09-26T12:00:00")
    fake.job(
        201,
        1,
        2001,
        "test (ubuntu)",
        "failure",
        log=(
            "FAILED tests/test_net.py::test_fetch - ConnectionError\n"
            "FAILED tests/test_new.py::test_feature - assert 2 == 3\n"
            "FAILED tests/test_old.py::test_legacy - x\n"
        ),
    )
    fake.job(
        201,
        1,
        2002,
        "all required jobs passed",
        "failure",
        log="##[error]Process completed with exit code 1.\n",
    )
    # A second PR run: only a known flake and a network error.
    fake.run(202, conclusion="failure", branch="feature", sha="Q", created="2026-09-26T13:00:00")
    fake.job(202, 1, 2011, "build", "failure", log=NET_LOG)
    fake.job(202, 1, 2012, "test (ubuntu)", "failure", log=FLAKY_LOG)


def test_why_separates_flaky_preexisting_and_real(monkeypatch):
    fake = Fake()
    failing_run(fake)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    e = explain(make(fake), REPO, 201, days=30)
    verdicts = {f.test_id: f.verdict for f in e.findings}
    assert verdicts == {
        "tests/test_net.py::test_fetch": KNOWN_FLAKY,
        "tests/test_new.py::test_feature": LOOKS_REAL,
        "tests/test_old.py::test_legacy": ALREADY_FAILING,
    }
    assert e.recommendation == INVESTIGATE
    assert all(f.job_name != "all required jobs passed" for f in e.findings)
    flaky = next(f for f in e.findings if f.verdict == KNOWN_FLAKY)
    assert "2 commits" in flaky.detail and flaky.red_herring is True


def test_why_recommends_rerun_when_everything_is_a_red_herring(monkeypatch):
    fake = Fake()
    failing_run(fake)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    e = explain(make(fake), REPO, 202, days=30)
    assert sorted(f.verdict for f in e.findings) == [INFRASTRUCTURE, KNOWN_FLAKY]
    assert e.recommendation == RERUN


def test_why_on_green_run():
    fake = Fake()
    fake.run(300, sha="Z")
    fake.job(300, 1, 3001, "test", "success")
    e = explain(make(fake), REPO, 300)
    assert e.recommendation == NOTHING and e.history is None


def test_parse_target():
    assert parse_target("https://github.com/a/b/actions/runs/123", None) == ("a/b", 123, None)
    assert parse_target("https://github.com/a/b/actions/runs/123/attempts/2", None) == (
        "a/b",
        123,
        2,
    )
    assert parse_target("https://github.com/a/b/actions/runs/123/job/9", None) == ("a/b", 123, None)
    assert parse_target("123", "a/b") == ("a/b", 123, None)
    with pytest.raises(ValueError):
        parse_target("123", None)


@pytest.mark.parametrize(
    "value,repo",
    [
        ("a/b", "a/b"),
        ("https://github.com/a/b", "a/b"),
        ("https://github.com/a/b.git", "a/b"),
        ("git@github.com:a/b.git", "a/b"),
        ("https://github.com/a/b/", "a/b"),
    ],
)
def test_normalize_repo(value, repo):
    assert cli.normalize_repo(value) == repo


def test_cli_without_token(monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_token", lambda: None)
    assert cli.main(["scan", "a/b"]) == cli.EXIT_ERROR
    assert "GH_TOKEN" in capsys.readouterr().err


def test_cli_scan_json_and_why_exit_codes(monkeypatch, capsys):
    fake = Fake()
    now = datetime.now(UTC)

    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    fake.run(1, attempt=2, sha="A", created=iso(now - timedelta(days=2)))
    fake.job(1, 1, 11, "test", "failure", log=FLAKY_LOG)
    fake.run(2, conclusion="failure", sha="B", created=iso(now - timedelta(hours=1)))
    fake.job(2, 1, 21, "test", "failure", log=FLAKY_LOG)
    fake.run(3, conclusion="failure", sha="C", created=iso(now - timedelta(minutes=30)))
    fake.job(3, 1, 31, "test", "failure", log="FAILED tests/test_x.py::test_new - boom\n")

    monkeypatch.setattr(cli, "resolve_token", lambda: "t0ken")
    monkeypatch.setattr(
        cli,
        "GitHub",
        lambda token, on_wait=None, http_cache=None: GitHub(
            token, transport=fake.transport(), http_cache=http_cache
        ),
    )

    assert cli.main(["scan", REPO, "--format", "json", "--cache", ":memory:", "-q"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["runs"]["recovered_by_rerun"] == 1
    assert data["flaky_tests"][0]["test"] == "tests/test_net.py::test_fetch"

    run2 = f"https://github.com/{REPO}/actions/runs/2"
    assert cli.main(["why", run2, "--format", "json", "--cache", ":memory:", "-q"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["recommendation"] == RERUN

    run3 = f"https://github.com/{REPO}/actions/runs/3"
    assert cli.main(["why", run3, "--format", "md", "--cache", ":memory:", "-q"]) == cli.EXIT_REAL
    assert "LOOKS REAL" in capsys.readouterr().out


class _Frozen(datetime):
    """datetime.now() pinned to the fixture's 'today' so the default window covers it."""

    @classmethod
    def now(cls, tz=None):
        return UNTIL


def test_failed_count_is_split_when_total_count_is_capped():
    fake = Fake()
    history(fake)
    real = fake._list_runs

    def capped(q, workflow_id):
        resp = real(q, workflow_id)
        data = json.loads(resp.content)
        lo, hi = q["created"].split("..")
        # GitHub never reports more than 2500; pretend any window over a day is capped.
        if (at(hi.rstrip("Z")) - at(lo.rstrip("Z"))).total_seconds() > 86400:
            data["total_count"] = 2500
        return httpx.Response(200, json=data)

    fake._list_runs = capped
    gh = GitHub("t", transport=fake.transport())
    assert gh.count_runs(REPO, at("2026-08-28T00:00:00"), UNTIL, status="failure") == 1


def test_rerun_after_a_cancelled_attempt_never_went_red():
    fake = Fake()
    history(fake)
    fake.run(107, attempt=2, sha="G", created="2026-09-25T12:00:00")
    fake.job(107, 1, 1071, "test (ubuntu)", "cancelled")
    r = make(fake).scan(REPO, days=30, until=UNTIL)
    assert len(r.recovered_runs) == 5  # 107 was re-run...
    assert r.runs_went_red == 5  # ...but never went red


def test_rate_limit_with_a_stale_reset_backs_off_instead_of_spinning():
    stale = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) - 30)}
    fake = Fake(interrupts=[(403, stale), (403, stale), (403, stale)])
    sleeps: list[float] = []
    gh = make(fake, sleeps=sleeps).gh
    assert gh.repo(REPO)["default_branch"] == "main"
    assert sleeps == [5.0, 10.0, 15.0]
