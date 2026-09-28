"""One PR comment per workflow, updated in place on every failed attempt."""

from __future__ import annotations

from .github import GitHub
from .render import why_markdown
from .why import NOTHING, Explanation


def marker(workflow: str) -> str:
    return f"<!-- redherring:why:{workflow} -->"


def comment_body(e: Explanation) -> str:
    return f"{marker(e.run.get('name') or '')}\n{why_markdown(e)}"


def post_or_update(gh: GitHub, e: Explanation) -> list[str]:
    """Post the explanation on the run's open PRs, editing our earlier comment if there is one.

    Returns the comment URLs. Does nothing when nothing failed.
    """
    if e.recommendation == NOTHING:
        return []
    body = comment_body(e)
    tag = marker(e.run.get("name") or "")
    urls = []
    for number in gh.open_pulls_for_run(e.repo, e.run):
        mine = next(
            (c for c in gh.issue_comments(e.repo, number) if tag in (c.get("body") or "")), None
        )
        if mine:
            done = gh.write(
                "PATCH", f"/repos/{e.repo}/issues/comments/{mine['id']}", {"body": body}
            )
        else:
            done = gh.write("POST", f"/repos/{e.repo}/issues/{number}/comments", {"body": body})
        urls.append(done.get("html_url", ""))
    return urls
