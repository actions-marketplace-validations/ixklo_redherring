"""Team notes (.github/redherring.toml) and `redherring report` drafts."""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from rich.console import Console

from redherring import cli, report
from redherring.cache import Cache
from redherring.github import GitHub
from redherring.notes import FLAKY, NOT_FLAKY, NotesError, parse
from redherring.render import print_scan, scan_json
from redherring.scan import Scanner
from redherring.why import KNOWN_FLAKY, LOOKS_REAL, RERUN, explain

from .fakegh import REPO, Fake
from .test_scan_why import FLAKY_LOG, NET_LOG, UNTIL, _Frozen, history

NOTES = """
[[flaky]]
test = "tests/test_io.py::test_rare"
reason = "writes to a shared temp dir (#12)"

[[flaky]]
job = "e2e (*)"

[[not_flaky]]
test = "tests/test_net.py::test_fetch"
reason = "we fixed the retry; failures are real now"

[[not_flaky]]
job = "deploy"
"""
RARE_LOG = "FAILED tests/test_io.py::test_rare - boom\n"


def scanner(fake: Fake) -> Scanner:
    return Scanner(GitHub("t", transport=fake.transport()), Cache(":memory:"), workers=2)


def patch_cli(monkeypatch, fake: Fake) -> None:
    monkeypatch.setattr(cli, "resolve_token", lambda: "t")
    monkeypatch.setattr(
        cli,
        "GitHub",
        lambda token, on_wait=None, http_cache=None: GitHub(
            token, transport=fake.transport(), http_cache=http_cache
        ),
    )
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)


# --- the notes file ----------------------------------------------------------------------


def test_notes_parse_and_match():
    n = parse(NOTES)
    assert [(m.kind, m.target, m.pattern) for m in n.marks] == [
        (FLAKY, "test", "tests/test_io.py::test_rare"),
        (FLAKY, "job", "e2e (*)"),
        (NOT_FLAKY, "test", "tests/test_net.py::test_fetch"),
        (NOT_FLAKY, "job", "deploy"),
    ]
    assert n.for_test("tests/test_io.py::test_rare", ["unit"]).kind == FLAKY
    assert n.for_test("tests/a.py::t", ["e2e (windows-latest, 3.12)"]).kind == FLAKY
    assert n.for_test("tests/a.py::t", ["unit"]) is None
    # not_flaky wins, even against a flaky mark on the job.
    assert n.for_test("tests/test_net.py::test_fetch", ["e2e (x)"]).kind == NOT_FLAKY
    assert n.for_job("deploy").kind == NOT_FLAKY
    assert n.for_job("deploy-docs") is None


def test_only_star_is_a_wildcard():
    n = parse('[[flaky]]\ntest = "t.py::test_x[1-2]"\n\n[[flaky]]\ntest = "t.py::test_y?"\n')
    assert n.for_test("t.py::test_x[1-2]", [])
    assert n.for_test("t.py::test_x1", []) is None
    assert n.for_test("t.py::test_yz", []) is None


def test_reasons_are_tidied():
    n = parse('[[flaky]]\ntest = "t"\nreason = "  two\\n lines \\u001b[31mred\\u001b[0m  "\n')
    assert n.marks[0].reason == "two lines red"


@pytest.mark.parametrize(
    "text,message",
    [
        ("[[flaky]\n", "isn't valid TOML"),
        ('[[flakey]]\ntest = "x"\n', "unknown section 'flakey'"),
        ('flaky = "x"\n', "write each flaky entry as [[flaky]]"),
        ('[[flaky]]\ntest = "x"\njob = "y"\n', "exactly one of"),
        ('[[not_flaky]]\nreason = "r"\n', "exactly one of"),
        ('[[flaky]]\ntest = "x"\nwhy = "r"\n', "unknown key 'why'"),
        ('[[flaky]]\ntest = "  "\n', "non-empty"),
        ('[[flaky]]\ntest = "x"\nreason = 3\n', "reason must be a string"),
    ],
)
def test_bad_notes_are_refused(text, message):
    with pytest.raises(NotesError, match=re.escape(message)):
        parse(text)


# --- notes in verdicts -------------------------------------------------------------------


def test_marked_flaky_needs_no_history(monkeypatch):
    fake = Fake()
    fake.run(500, conclusion="failure", sha="N", created="2026-09-26T12:00:00")
    fake.job(500, 1, 5001, "unit", "failure", log=RARE_LOG)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    e = explain(scanner(fake), REPO, 500, days=30, notes=parse(NOTES))
    [f] = e.findings
    assert (f.verdict, f.red_herring, f.mark) == (KNOWN_FLAKY, True, FLAKY)
    assert f.detail == (
        "the team marked this test flaky in .github/redherring.toml: "
        "writes to a shared temp dir (#12)"
    )
    assert e.recommendation == RERUN and e.notes == ".github/redherring.toml"
    # Without the notes, the same failure looks real.
    assert explain(scanner(fake), REPO, 500, days=30).findings[0].verdict == LOOKS_REAL


def test_marked_not_flaky_overrides_history(monkeypatch):
    fake = Fake()
    history(fake)  # test_fetch flaked twice: a known flake without notes
    fake.run(501, conclusion="failure", sha="M", created="2026-09-26T12:00:00")
    fake.job(501, 1, 5011, "test (ubuntu)", "failure", log=FLAKY_LOG)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    [f] = explain(scanner(fake), REPO, 501, days=30, notes=parse(NOTES)).findings
    assert (f.verdict, f.red_herring, f.mark) == (LOOKS_REAL, False, NOT_FLAKY)
    assert "we fixed the retry" in f.detail


def test_a_marked_flake_failing_again_on_the_same_commit_is_still_real(monkeypatch):
    fake = Fake()
    fake.run(502, attempt=2, conclusion="failure", sha="R", created="2026-09-26T12:00:00")
    fake.job(502, 1, 5021, "unit", "failure", log=RARE_LOG)
    fake.job(502, 2, 5022, "unit", "failure", log=RARE_LOG)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    [f] = explain(scanner(fake), REPO, 502, days=30, notes=parse(NOTES)).findings
    assert f.verdict == LOOKS_REAL and "failed again on attempt 2" in f.detail


def test_job_marks(monkeypatch):
    fake = Fake()
    fake.run(503, conclusion="failure", sha="J", created="2026-09-26T12:00:00")
    make_log = "make: *** [all] Error 2\n##[error]Process completed with exit code 2.\n"
    fake.job(503, 1, 5031, "e2e (windows-latest)", "failure", log=make_log)
    fake.job(503, 1, 5032, "deploy", "failure", log=NET_LOG)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    found = {
        f.job_name: f
        for f in explain(scanner(fake), REPO, 503, days=30, notes=parse(NOTES)).findings
    }
    e2e, deploy = found["e2e (windows-latest)"], found["deploy"]
    assert (e2e.verdict, e2e.mark) == (KNOWN_FLAKY, FLAKY)
    assert "marked this job flaky" in e2e.detail and "(pattern 'e2e (*)')" in e2e.detail
    assert (deploy.verdict, deploy.mark) == (LOOKS_REAL, NOT_FLAKY)
    assert "infrastructure problem (network)" in deploy.detail


def test_cli_reads_notes_from_the_default_branch(monkeypatch, capsys, tmp_path):
    fake = Fake()
    fake.files[".github/redherring.toml"] = NOTES
    fake.run(500, conclusion="failure", sha="N", created="2026-09-26T12:00:00")
    fake.job(500, 1, 5001, "unit", "failure", log=RARE_LOG)
    patch_cli(monkeypatch, fake)
    base = ["why", f"https://github.com/{REPO}/actions/runs/500", "--format", "json"]
    base += ["--cache", ":memory:", "-q"]

    assert cli.main(base) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["notes"] == ".github/redherring.toml"
    assert out["findings"][0]["marked"] == "flaky"

    assert cli.main([*base, "--no-notes"]) == 1
    assert json.loads(capsys.readouterr().out)["notes"] is None

    local = tmp_path / "notes.toml"
    local.write_text('[[not_flaky]]\ntest = "tests/*"\n', encoding="utf-8")
    assert cli.main([*base, "--notes", str(local)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["notes"] == str(local) and out["findings"][0]["marked"] == "not_flaky"

    fake.files[".github/redherring.toml"] = '[[flakey]]\ntest = "x"\n'
    assert cli.main(base) == 2
    assert "unknown section 'flakey'" in capsys.readouterr().err


def test_scan_shows_where_notes_and_evidence_disagree(monkeypatch):
    fake = Fake()
    history(fake)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    r = scanner(fake).scan(REPO, days=30, until=UNTIL)
    notes = parse(NOTES)
    data = scan_json(r, notes)
    assert data["flaky_tests"][0]["marked"] == "not_flaky"
    assert data["notes"] == {
        "source": ".github/redherring.toml",
        "marked_not_flaky_but_flaked": ["tests/test_net.py::test_fetch"],
        "marked_flaky_not_seen": ["tests/test_io.py::test_rare"],
    }
    assert scan_json(r)["notes"] is None
    console = Console(record=True, width=300)
    print_scan(r, console, notes=notes)
    text = console.export_text()
    assert "Marked not flaky in .github/redherring.toml" in text
    assert "not seen flaking here (fixed?): tests/test_io.py::test_rare" in text


# --- report: scrubbing and the excerpt ---------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("token ghp_" + "a" * 36, "token <token>"),
        ("url https://deploy:hunter2@host/x", "url https://<credentials>@host/x"),
        ("mail ethan@example.com now", "mail <email> now"),
        ("API_KEY=abcdef123456", "API_KEY=<secret>"),
        ('password: "s3cr3tpass"', 'password: "<secret>"'),
        ("Authorization: Bearer abc.def", "Authorization: Bearer <secret>"),
        ("dir /home/ethan/.cache", "dir /home/<user>/.cache"),
        ("dir C:\\Users\\ethan\\AppData", "dir C:\\Users\\<user>\\AppData"),
    ],
)
def test_scrub_replaces_likely_secrets(line, expected):
    assert report.scrub(line) == (expected, 1)


@pytest.mark.parametrize(
    "line",
    [
        "GITHUB_TOKEN: ***",
        "max_tokens=100",
        "No matching version found for left-pad@9.9.9",
        "/home/runner/work/app and C:\\Users\\runneradmin\\x",
        "commit 3f2a9c0e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39",
    ],
)
def test_scrub_keeps_useful_text(line):
    assert report.scrub(line) == (line, 0)


def test_excerpt_is_output_up_to_the_first_error():
    lines = [
        "a",
        "",
        "##[group]Run x --flag",
        "x --flag",  # the step's script, echoed
        "shell: /usr/bin/bash -e {0}",
        "##[endgroup]",
        "##[group]Some tool output",
        "b",
        "##[endgroup]",
        "c",
        "##[error]boom",
        "cleanup",
    ]
    assert report.excerpt(lines, 9) == [
        "a",
        "##[group]Run x --flag",  # kept: which step the output below belongs to
        "##[group]Some tool output",
        "b",
        "c",
        "##[error]boom",
    ]
    assert report.excerpt(lines, 2) == ["c", "##[error]boom"]
    assert report.excerpt(["x", "y"], 5) == ["x", "y"]


# --- report: drafts ----------------------------------------------------------------------

UNKNOWN_LOG = (
    "##[group]Run ./scripts/e2e.sh\n"
    "./scripts/e2e.sh\n"
    "shell: /usr/bin/bash -e {0}\n"
    "##[endgroup]\n"
    "starting browser for ethan@example.com\n"
    "using https://deploy:hunter2secret@internal.example.com/api\n"
    "GITHUB_TOKEN: ***\n"
    "export API_KEY=abcdef123456\n"
    "cache dir /home/ethan/.cache and /home/runner/work\n"
    "\n"
    "widget assembly failed: gear 7 is out of alignment\n"
    "##[error]Process completed with exit code 1.\n"
    "Post job cleanup.\n"
)
RUN_600 = f"https://github.com/{REPO}/actions/runs/600"


def unknown_run(fake: Fake, *, private: bool = False) -> None:
    fake.private = private
    fake.run(600, conclusion="failure", sha="U", created="2026-09-26T12:00:00")
    fake.job(600, 1, 6001, "e2e (ubuntu)", "failure", log=UNKNOWN_LOG)
    fake.job(600, 1, 6002, "unit", "failure", log="FAILED tests/a.py::t - x\n")


def test_report_drafts_an_issue_for_the_unrecognised_job():
    fake = Fake()
    unknown_run(fake)
    d = report.build(scanner(fake), RUN_600, None)
    assert d.template == "parser.yml"
    assert d.title == 'Not recognised: "Run tests" step on linux'
    log = d.fields["log"].splitlines()
    assert log[0] == "##[group]Run ./scripts/e2e.sh"
    assert log[-1] == "##[error]Process completed with exit code 1."
    assert "<email>" in d.fields["log"] and "hunter2" not in d.fields["log"]
    assert "Post job cleanup." not in log and not any(x.startswith("shell:") for x in log)
    assert d.redactions == 4
    assert "read this log as: unknown" in d.fields["said"]
    assert "failed step: Run tests" in d.fields["said"]
    assert d.fields["url"] == f"{RUN_600}/job/6001"
    url = urlparse(d.url())
    assert (url.netloc, url.path) == ("github.com", "/ixklo/redherring/issues/new")
    q = parse_qs(url.query)
    assert q["template"] == ["parser.yml"] and q["title"] == [d.title]
    assert q["log"] == [d.fields["log"]]
    md = d.markdown()
    assert md.startswith("## Not recognised") and "**Log excerpt**" in md and "```text" in md


def test_private_repos_leave_out_names_and_links():
    fake = Fake()
    unknown_run(fake, private=True)
    d = report.build(scanner(fake), RUN_600, None)
    assert d.private and d.fields["url"] == ""
    assert "actions%2Fruns" not in d.url() and "actions/runs" not in d.url()


def test_several_unrecognised_jobs_need_a_choice():
    fake = Fake()
    unknown_run(fake)
    fake.job(600, 1, 6003, "e2e (windows)", "failure", labels=("windows-latest",), log=UNKNOWN_LOG)
    with pytest.raises(ValueError, match="2 failed jobs weren't recognised"):
        report.build(scanner(fake), RUN_600, None)
    assert report.build(scanner(fake), RUN_600, None, job="windows").title.endswith("on windows")
    assert report.build(scanner(fake), RUN_600, None, job="6001").fields["url"].endswith("/6001")


def test_nothing_to_report_when_every_failure_has_a_cause():
    fake = Fake()
    fake.run(610, conclusion="failure", sha="C", created="2026-09-26T12:00:00")
    fake.job(610, 1, 6101, "unit", "failure", log="FAILED tests/a.py::t - x\n")
    with pytest.raises(report.NothingToReport, match="named a cause"):
        report.build(scanner(fake), f"https://github.com/{REPO}/actions/runs/610", None)


def test_long_logs_are_trimmed_to_fit_a_link():
    fake = Fake()
    fake.run(620, conclusion="failure", sha="L", created="2026-09-26T12:00:00")
    big = "".join(f"line {i} " + "x" * 250 + "\n" for i in range(200))
    fake.job(
        620, 1, 6201, "build", "failure", log=big + "##[error]Process completed with exit code 1.\n"
    )
    d = report.build(scanner(fake), f"https://github.com/{REPO}/actions/runs/620", None, lines=200)
    assert len(d.url()) <= report.MAX_URL and d.trimmed > 0
    assert d.fields["log"].startswith(f"… {d.trimmed} earlier line(s) left out")
    assert d.fields["log"].endswith("##[error]Process completed with exit code 1.")


def test_wrong_verdict_from_a_job_url(monkeypatch):
    fake = Fake()
    history(fake)
    fake.run(630, conclusion="failure", sha="W", created="2026-09-26T12:00:00")
    fake.job(630, 1, 6301, "test (ubuntu)", "failure", log=FLAKY_LOG)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    job_url = f"https://github.com/{REPO}/actions/runs/630/job/6301"
    d = report.build(scanner(fake), job_url, None, wrong=True)
    assert d.template == "verdict.yml"
    assert d.title == "Wrong verdict: known flaky for tests/test_net.py::test_fetch"
    assert "- known flaky: tests/test_net.py::test_fetch:" in d.fields["said"]
    assert d.fields["run"] == job_url and "test_fetch" in d.fields["log"]


def test_cli_report_shows_the_draft_and_never_opens_it_by_itself(monkeypatch, capsys):
    fake = Fake()
    unknown_run(fake)
    patch_cli(monkeypatch, fake)
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)
    args = ["report", RUN_600, "--cache", ":memory:", "-q"]

    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "a draft issue for ixklo/redherring" in out and "Replaced 4 thing(s)" in out
    assert "https://github.com/ixklo/redherring/issues/new?template=parser.yml" in out
    assert opened == []  # not a terminal, and no --open: just the link

    assert cli.main([*args, "--open"]) == 0
    assert "Opened in your browser" in capsys.readouterr().out
    assert len(opened) == 1

    assert cli.main([*args, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["template"] == "parser.yml" and data["redactions"] == 4
    assert data["url"] == opened[0]


def test_cli_report_when_there_is_nothing_to_report(monkeypatch, capsys):
    fake = Fake()
    fake.run(610, conclusion="failure", sha="C", created="2026-09-26T12:00:00")
    fake.job(610, 1, 6101, "unit", "failure", log="FAILED tests/a.py::t - x\n")
    patch_cli(monkeypatch, fake)
    run = f"https://github.com/{REPO}/actions/runs/610"
    assert cli.main(["report", run, "--cache", ":memory:", "-q"]) == 0
    assert "named a cause for every failed job" in capsys.readouterr().out


def test_the_example_notes_file_parses():
    root = Path(__file__).parents[1]
    notes = parse((root / "examples" / "redherring.toml").read_text(encoding="utf-8"))
    assert [(m.kind, m.target) for m in notes.marks] == [
        (FLAKY, "test"),
        (FLAKY, "job"),
        (NOT_FLAKY, "test"),
        (NOT_FLAKY, "job"),
    ]


@pytest.mark.parametrize(
    "template,fields",
    [
        (report.PARSER_TEMPLATE, ("log", "said", "url")),
        (report.VERDICT_TEMPLATE, ("run", "said", "log")),
    ],
)
def test_issue_forms_have_the_fields_report_fills(template, fields):
    form = Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / template
    text = form.read_text(encoding="utf-8")
    for field_id in fields:
        assert f"id: {field_id}\n" in text
