"""PR comments: one per workflow, edited in place."""

from redherring.cache import Cache
from redherring.comment import marker, post_or_update
from redherring.github import GitHub
from redherring.scan import Scanner
from redherring.why import explain

from .fakegh import REPO, Fake
from .test_scan_why import FLAKY_LOG, _Frozen, history


def setup(monkeypatch) -> tuple[Fake, Scanner]:
    fake = Fake()
    history(fake)
    run = fake.run(201, conclusion="failure", sha="P", created="2026-09-26T12:00:00")
    run["pull_requests"] = [{"number": 42}]
    fake.job(201, 1, 2001, "test (ubuntu)", "failure", log=FLAKY_LOG)
    monkeypatch.setattr("redherring.scan.datetime", _Frozen)
    return fake, Scanner(GitHub("t", transport=fake.transport()), Cache(":memory:"), workers=2)


def test_posts_once_then_edits(monkeypatch):
    fake, sc = setup(monkeypatch)
    fake.comments[42] = [{"id": 1, "body": "unrelated human comment"}]

    e = explain(sc, REPO, 201, days=30)
    [url] = post_or_update(sc.gh, e)
    assert url.endswith("#c9001")
    ours = [c for c in fake.comments[42] if marker("CI") in c["body"]]
    assert len(ours) == 1 and "RED HERRING" in ours[0]["body"]

    post_or_update(sc.gh, explain(sc, REPO, 201, days=30))
    assert [w[0] for w in fake.writes] == ["POST", "PATCH"]
    assert len([c for c in fake.comments[42] if marker("CI") in c["body"]]) == 1
    assert fake.comments[42][0]["body"] == "unrelated human comment"


def test_fork_pr_found_by_commit(monkeypatch):
    fake, sc = setup(monkeypatch)
    next(r for r in fake.runs if r["id"] == 201)["pull_requests"] = []
    fake.pulls_by_sha["P"] = [{"number": 7, "state": "open"}, {"number": 3, "state": "closed"}]
    post_or_update(sc.gh, explain(sc, REPO, 201, days=30))
    assert [w[1] for w in fake.writes] == [f"/repos/{REPO}/issues/7/comments"]


def test_no_comment_when_nothing_failed(monkeypatch):
    fake, sc = setup(monkeypatch)
    fake.run(300, sha="Z")["pull_requests"] = [{"number": 5}]
    fake.job(300, 1, 3001, "test", "success")
    assert post_or_update(sc.gh, explain(sc, REPO, 300)) == []
    assert fake.writes == []
