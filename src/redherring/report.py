"""`redherring report`: turn a failed job redherring couldn't read into a draft GitHub issue.

redherring sends nothing. The draft becomes a link to GitHub's "new issue" page with the
fields filled in: the user reads it there, edits it, and presses Submit, or doesn't.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from . import __version__
from .infra import InfraCause
from .logtext import clean_lines
from .notes import Notes
from .parsers import FLAKY, TestFailure
from .scan import (
    FAILED_CONCLUSIONS,
    KIND_UNKNOWN,
    Scanner,
    explain_log,
    failed_step,
    is_summary_job,
    runner_os,
)
from .why import _RUN_URL, Finding, explain, parse_target

ISSUES = "ixklo/redherring"
# GitHub turns away much longer links, and browsers get unhappy well before 32k.
MAX_URL = 7500
MAX_LINE = 300
MIN_LINES = 5
PARSER_TEMPLATE = "parser.yml"
VERDICT_TEMPLATE = "verdict.yml"


class NothingToReport(Exception):
    """Nothing in the run needs reporting (a message for the user, not an error)."""


# --- scrubbing ---------------------------------------------------------------------------
#
# GitHub already masks the repository's own secrets as ***. These catch the usual leaks it
# can't know about. It's best effort: the user still reads the draft before sending it.

_SCRUB: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*"), "<private key removed>"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})"), "<token>"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<key>"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "<token>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<key>"),
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}"), "<token>"),
    (re.compile(r"(?i)\b(authorization:\s*(?:bearer|basic|token)\s+)\S+"), r"\1<secret>"),
    (
        re.compile(
            r"(?i)\b([\w-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[\w-]*"
            r"[\"']?\s*[:=]\s*[\"']?)(?!\*\*\*)[^\s\"',;]{6,}"
        ),
        r"\1<secret>",
    ),
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1<credentials>@"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"), "<email>"),
    # Home folders, except the standard ones on GitHub's own runners.
    (
        re.compile(
            r"((?:/Users/|/home/|\b[A-Z]:\\Users\\))(?!(?:runner|runneradmin)\b)([^/\\\s]+)"
        ),
        r"\1<user>",
    ),
]


def scrub(text: str) -> tuple[str, int]:
    """`text` with likely secrets and personal details replaced, and how many were."""
    total = 0
    for pattern, replacement in _SCRUB:
        text, n = pattern.subn(replacement, text)
        total += n
    return text, total


# --- the excerpt -------------------------------------------------------------------------


def _clip(line: str, limit: int = MAX_LINE) -> str:
    return line if len(line) <= limit else line[: limit - 1] + "…"


def excerpt(lines: list[str], count: int = 40) -> list[str]:
    """The last `count` lines of output up to the job's first error, where the failure is.

    Each step's log opens with its whole script and env block (a "##[group]Run …" group).
    That's the workflow file again, not output, and can fill the excerpt on its own, so only
    its first line is kept, to show which step the output below belongs to.
    """
    end = next((i for i, line in enumerate(lines) if line.startswith("##[error]")), None)
    window = lines[: end + 1] if end is not None else lines
    kept: list[str] = []
    in_step_header = False
    for line in window:
        if line.startswith("##[group]Run "):
            kept.append(line)
            in_step_header = True
        elif line.strip() == "##[endgroup]":
            in_step_header = False
        elif line.strip() and not in_step_header:
            kept.append(line)
    return [_clip(line.rstrip()) for line in kept[-count:]]


# --- the draft ---------------------------------------------------------------------------


@dataclass
class Draft:
    template: str
    title: str
    # Issue-form field id -> value. The forms live in .github/ISSUE_TEMPLATE of ISSUES.
    fields: dict[str, str]
    job_url: str = ""
    private: bool = False
    redactions: int = 0
    # Excerpt lines left out so the link stays short enough for GitHub.
    trimmed: int = 0
    excerpt_lines: int = 0
    labels: dict[str, str] = field(default_factory=dict)

    def url(self) -> str:
        params = {"template": self.template, "title": self.title}
        params.update({k: v for k, v in self.fields.items() if v})
        return f"https://github.com/{ISSUES}/issues/new?" + urlencode(params, quote_via=quote)

    def markdown(self) -> str:
        out = [f"## {self.title}", ""]
        for key, value in self.fields.items():
            if not value:
                continue
            fence = "`" * max(3, _longest_backtick_run(value) + 1)
            out += [f"**{self.labels.get(key, key)}**", "", f"{fence}text", value, fence, ""]
        return "\n".join(out).rstrip() + "\n"

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": "redherring",
            "version": __version__,
            "issue_repo": ISSUES,
            "template": self.template,
            "title": self.title,
            "fields": self.fields,
            "url": self.url(),
            "job_url": self.job_url or None,
            "private_repo": self.private,
            "redactions": self.redactions,
            "excerpt_lines": self.excerpt_lines,
            "trimmed_lines": self.trimmed,
        }


def _longest_backtick_run(text: str) -> int:
    return max((len(m) for m in re.findall(r"`+", text)), default=0)


_PARSER_LABELS = {"log": "Log excerpt", "said": "What redherring said", "url": "Job URL"}
_VERDICT_LABELS = {"run": "Run or job URL", "said": "What redherring said", "log": "Log excerpt"}


def _fit(make: Callable[[list[str], int], Draft], lines: list[str]) -> Draft:
    """The draft with as much of the excerpt as fits in a link, nearest the failure first."""
    trimmed = 0
    while True:
        draft = make(lines, trimmed)
        if len(draft.url()) <= MAX_URL or not lines:
            draft.trimmed = trimmed
            draft.excerpt_lines = len(lines)
            return draft
        if len(lines) <= MIN_LINES and any(len(line) > 120 for line in lines):
            lines = [_clip(line, 120) for line in lines]
            continue
        lines = lines[1:]
        trimmed += 1


def _log_field(lines: list[str], trimmed: int) -> str:
    head = [f"… {trimmed} earlier line(s) left out to keep the link short"] if trimmed else []
    return "\n".join(head + lines)


def describe_reading(
    kind: str,
    tests: list[TestFailure],
    infra: InfraCause | None,
    hint: str,
    step: str,
    os_name: str,
) -> str:
    lines = [f"redherring {__version__} read this log as: {kind}"]
    named = [t.test_id for t in tests if t.outcome != FLAKY]
    if named:
        more = f" (and {len(named) - 10} more)" if len(named) > 10 else ""
        lines.append("tests named: " + ", ".join(named[:10]) + more)
    if infra:
        lines.append(f"infrastructure cause: {infra.category}")
    if step:
        lines.append(f"failed step: {step}")
    if os_name:
        lines.append(f"runner OS: {os_name}")
    if hint:
        lines.append(f"last output: {hint}")
    return "\n".join(lines)


def describe_findings(findings: list[Finding]) -> str:
    lines = [f"redherring {__version__} said:"]
    for f in findings:
        what = f.test_id or f"job {f.job_name}"
        lines.append(f"- {f.verdict}: {what}: {f.detail}")
    return "\n".join(lines)


# --- choosing the job and building the draft ---------------------------------------------


def _job_id(target: str) -> int | None:
    m = _RUN_URL.search(target)
    return int(m[4]) if m and m[4] else None


def _pick(failed: list[dict], wanted: str) -> dict:
    by_id = [j for j in failed if str(j["id"]) == wanted]
    if by_id:
        return by_id[0]
    exact = [j for j in failed if (j.get("name") or "").lower() == wanted.lower()]
    partial = [j for j in failed if wanted.lower() in (j.get("name") or "").lower()]
    for group in (exact, partial):
        if len(group) == 1:
            return group[0]
    raise ValueError(f"no single failed job matches {wanted!r}. Failed jobs:\n" + _job_list(failed))


def _job_list(jobs: list[dict]) -> str:
    return "\n".join(f"  {j['id']}  {j.get('name')}" for j in jobs)


def build(
    scanner: Scanner,
    target: str,
    repo_arg: str | None,
    *,
    job: str | None = None,
    wrong: bool = False,
    lines: int = 40,
    days: int = 30,
    check_main: bool = True,
    notes: Notes | None = None,
) -> Draft:
    gh = scanner.gh
    repo, run_id, attempt = parse_target(target, repo_arg)
    run = gh.run(repo, run_id)
    private = (run.get("repository") or {}).get("private")
    if private is None:
        private = gh.repo(repo).get("private", True)

    if (job_id := _job_id(target)) is not None:
        chosen = gh.job(repo, job_id)
        attempt = chosen.get("run_attempt") or attempt
    else:
        attempt = attempt or run.get("run_attempt") or 1
        jobs = scanner.attempt_jobs(repo, run_id, attempt, final=run.get("status") == "completed")
        failed = [j for j in jobs if j.get("conclusion") in FAILED_CONCLUSIONS]
        if any(not is_summary_job(j.get("name") or "") for j in failed):
            failed = [j for j in failed if not is_summary_job(j.get("name") or "")]
        if not failed:
            raise NothingToReport("Nothing failed in this attempt, so there's nothing to report.")
        if job:
            chosen = _pick(failed, job)
        elif wrong:
            if len(failed) > 1:
                raise ValueError(
                    "which failure was wrong? Pass --job with one of these ids or names:\n"
                    + _job_list(failed)
                )
            chosen = failed[0]
        else:
            found = scanner.readings(repo, [j["id"] for j in failed])
            unread = [j for j in failed if (r := found.get(j["id"])) and r[0] == KIND_UNKNOWN]
            if not unread:
                raise NothingToReport(
                    "redherring named a cause for every failed job in this run. If one of those "
                    "answers is wrong, report it with --wrong --job <id or name>."
                )
            if len(unread) > 1:
                raise ValueError(
                    f"{len(unread)} failed jobs weren't recognised. Pick one with --job:\n"
                    + _job_list(unread)
                )
            chosen = unread[0]

    text = scanner.log_text(repo, chosen["id"])
    if text is None:
        raise ValueError(
            "this job's log has expired (GitHub keeps logs for 90 days at most), so there's "
            "nothing left to report"
        )
    kind, tests, infra, hint = explain_log(text)
    step = str(chosen.get("failed_step") or "") if "failed_step" in chosen else failed_step(chosen)
    os_name = runner_os(chosen)
    job_url = "" if private else (chosen.get("html_url") or "")
    raw_lines = excerpt(clean_lines(text), max(MIN_LINES, min(lines, 200)))
    scrubbed = [scrub(line) for line in raw_lines]
    excerpt_lines = [line for line, _ in scrubbed]
    redactions = sum(n for _, n in scrubbed)

    if wrong:
        e = explain(
            scanner,
            repo,
            run_id,
            attempt=attempt,
            days=days,
            check_main=check_main,
            notes=notes,
        )
        mine = [f for f in e.findings if f.job_url == chosen.get("html_url")] or [
            f for f in e.findings if f.job_name == chosen.get("name")
        ]
        # A job `why` sets aside (a roll-up) has no finding: say what the log read as instead.
        summary = (
            describe_findings(mine)
            if mine
            else describe_reading(kind, tests, infra, hint, step, os_name)
        )
        said, n = scrub(summary)
        redactions += n
        first = mine[0] if mine else None
        about = "" if private or first is None else f" for {first.test_id or first.job_name}"
        verdict = first.verdict if first else "a verdict"
        title = _clip(f"Wrong verdict: {verdict}{about}", 120)

        def make(ls: list[str], trimmed: int) -> Draft:
            return Draft(
                VERDICT_TEMPLATE,
                title,
                {"run": job_url, "said": said, "log": _log_field(ls, trimmed)},
                job_url=job_url,
                private=private,
                redactions=redactions,
                labels=_VERDICT_LABELS,
            )

        return _fit(make, excerpt_lines)

    said, n = scrub(describe_reading(kind, tests, infra, hint, step, os_name))
    redactions += n
    where = f" on {os_name}" if os_name and os_name != "other" else ""
    title = _clip(
        f'Not recognised: "{step}" step{where}' if step else f"Not recognised: a failed job{where}",
        120,
    )

    def make_parser(ls: list[str], trimmed: int) -> Draft:
        return Draft(
            PARSER_TEMPLATE,
            title,
            {"log": _log_field(ls, trimmed), "said": said, "url": job_url},
            job_url=job_url,
            private=private,
            redactions=redactions,
            labels=_PARSER_LABELS,
        )

    return _fit(make_parser, excerpt_lines)
