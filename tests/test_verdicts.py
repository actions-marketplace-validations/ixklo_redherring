"""Verdict safety: re-runs that fail again, one-off flakes, and untrusted text in output."""

from rich.console import Console

from redherring.cache import Cache
from redherring.github import GitHub
from redherring.render import print_scan, print_why, why_json, why_markdown
from redherring.scan import Scanner
from redherring.why import (
    INVESTIGATE,
    KNOWN_FLAKY,
    LOOKS_REAL,
    PROBABLY_FLAKY,
    RERUN,
    Explanation,
    Finding,
    explain,
)

from .fakegh import REPO, Fake
from .test_scan_why import FLAKY_LOG, UNTIL, _Frozen, history

ONE_OFF_LOG = "FAILED tests/test_io.py::test_rare - flaky once\n"


def scanner(fake: Fake) -> Scanner:
    return Scanner(GitHub("t", transport=fake.transport()), Cache(":memory:"), workers=2)


def test_failing_again_on_the_same_commit_is_real(monkeypatch):
    fake = Fake()
    history(fake)  # test_fetch flaked twice before: a known flake
    fake.run(400, attempt=2, conclusion="failure", sha="R", created="2026-09-26T12:00:00")
    fake.job(400, 1, 4001, "test (ubuntu)", "failure", log=FLAKY_LOG)
    fake.job(400, 2, 4002, "test (ubuntu)", "failure", log=FLAKY_LOG)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)

    first = explain(scanner(fake), REPO, 400, attempt=1, days=30)
    assert [f.verdict for f in first.findings] == [KNOWN_FLAKY]
    assert first.recommendation == RERUN

    second = explain(scanner(fake), REPO, 400, days=30)  # latest attempt: 2
    [f] = second.findings
    assert (f.verdict, f.red_herring) == (LOOKS_REAL, False)
    assert "failed again on attempt 2" in f.detail and "also on attempt 1" in f.detail
    assert second.recommendation == INVESTIGATE


def test_job_failing_again_without_a_test_is_real(monkeypatch):
    fake = Fake()
    fake.run(410, attempt=2, conclusion="failure", sha="S", created="2026-09-26T12:00:00")
    fake.job(
        410,
        1,
        4101,
        "build",
        "failure",
        log="make: *** [all] Error 2\n##[error]Process completed with exit code 2.\n",
    )
    fake.job(
        410,
        2,
        4102,
        "build",
        "failure",
        log="make: *** [all] Error 2\n##[error]Process completed with exit code 2.\n",
    )
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    [f] = explain(scanner(fake), REPO, 410, days=30).findings
    assert f.verdict == LOOKS_REAL and "failed again on attempt 2" in f.detail


def test_one_past_flake_is_only_probably_flaky(monkeypatch):
    fake = Fake()
    fake.run(420, attempt=2, sha="A", created="2026-09-20T10:00:00")  # flaked once, then green
    fake.job(420, 1, 4201, "test", "failure", log=ONE_OFF_LOG)
    fake.run(421, conclusion="failure", sha="B", created="2026-09-26T12:00:00")
    fake.job(421, 1, 4211, "test", "failure", log=ONE_OFF_LOG)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    e = explain(scanner(fake), REPO, 421, days=30)
    [f] = e.findings
    assert (f.verdict, f.red_herring) == (PROBABLY_FLAKY, True)
    assert e.recommendation == RERUN
    assert "fails again" in why_markdown(e)
    assert why_json(e)["findings"][0]["evidence"] == "flaky once"


HOSTILE = "@octocat @org/team see https://evil.example <img src=x onerror=alert(1)> `x` | y"


def hostile_explanation() -> Explanation:
    run = {
        "id": 9,
        "name": "CI @org/admins",
        "run_number": 3,
        "html_url": "https://github.com/a/b/actions/runs/9",
    }
    e = Explanation(repo=REPO, run=run, attempt=1, days=30)
    e.findings = [
        Finding(
            LOOKS_REAL,
            False,
            "test @org/team",
            "https://github.com/a/b/runs/1",
            "not seen flaking",
            test_id="t[/usr/bin] `evil` @x",
            framework="pytest",
            evidence=HOSTILE,
        ),
    ]
    return e


def test_markdown_neutralises_untrusted_text():
    md = why_markdown(hostile_explanation())
    # Log-derived text only appears inside inline code, where mentions, links and HTML are inert.
    assert "@org/admins" not in md.replace("@​org/admins", "")
    assert (
        "`@octocat @org/team see https://evil.example <img src=x onerror=alert(1)> 'x' \\| y`" in md
    )
    assert "<img" not in md.replace(
        "`@octocat @org/team see https://evil.example <img src=x onerror=alert(1)> 'x' \\| y`", ""
    )
    assert "`t[/usr/bin] 'evil' @x`" in md


def test_terminal_shows_brackets_literally_and_never_crashes():
    console = Console(record=True, width=200)
    print_why(hostile_explanation(), console)
    text = console.export_text()
    assert "t[/usr/bin] `evil` @x" in text
    assert "[link=" not in HOSTILE and "https://evil.example" in text


def test_scan_table_keeps_parametrised_names(monkeypatch):
    fake = Fake()
    fake.run(430, attempt=2, sha="A", created="2026-09-20T10:00:00")
    fake.job(
        430, 1, 4301, "test", "failure", log="FAILED tests/t.py::test_x[trigger1-1-0] - boom\n"
    )
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    r = scanner(fake).scan(REPO, days=30, until=UNTIL)
    console = Console(record=True, width=200)
    print_scan(r, console)
    assert "tests/t.py::test_x[trigger1-1-0]" in console.export_text()
