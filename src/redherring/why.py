"""Explain one failed run: which failures are red herrings and which look real."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .github import GitHub, GitHubError, parse_time
from .parsers import FLAKY
from .scan import (
    FAILED_CONCLUSIONS,
    KIND_EXPIRED,
    KIND_GATE,
    KIND_INFRA,
    KIND_MASS,
    KIND_SUMMARY,
    KIND_TESTS,
    FailedJob,
    Scanner,
    ScanResult,
    TestStat,
    explain_log,
    is_summary_job,
    refine_kind,
    runner_os,
)

KNOWN_FLAKY = "known flaky"
INFRASTRUCTURE = "infrastructure"
ALREADY_FAILING = "already failing"
POLICY_CHECK = "policy check"
LOOKS_REAL = "looks real"
UNEXPLAINED = "unexplained"
LOG_EXPIRED = "log expired"

RERUN = "rerun"
INVESTIGATE = "investigate"
UNCLEAR = "unclear"
NOTHING = "nothing failed"

_RUN_URL = re.compile(
    r"github\.com/([^/]+/[^/]+)/actions/runs/(\d+)(?:/attempts/(\d+))?(?:/job/(\d+))?"
)


@dataclass
class Finding:
    verdict: str
    # True: not caused by this change. False: probably caused by it. None: can't tell.
    red_herring: bool | None
    job_name: str
    job_url: str
    detail: str
    test_id: str = ""
    framework: str = ""
    history: TestStat | None = None


@dataclass
class Explanation:
    repo: str
    run: dict
    attempt: int
    days: int
    findings: list[Finding] = field(default_factory=list)
    history: ScanResult | None = None
    main_branch: str = ""

    @property
    def recommendation(self) -> str:
        if not self.findings:
            return NOTHING
        if any(f.red_herring is False for f in self.findings):
            return INVESTIGATE
        if all(f.red_herring is True for f in self.findings):
            return RERUN
        return UNCLEAR

    @property
    def real(self) -> list[Finding]:
        return [f for f in self.findings if f.red_herring is False]


def parse_target(target: str, repo: str | None) -> tuple[str, int, int | None]:
    """(repo, run_id, attempt) from a run URL, or a bare run id plus --repo."""
    if m := _RUN_URL.search(target):
        return m[1], int(m[2]), int(m[3]) if m[3] else None
    if target.isdigit() and repo:
        return repo, int(target), None
    raise ValueError(
        "give a run URL (https://github.com/OWNER/REPO/actions/runs/ID) or a run id with --repo OWNER/REPO"
    )


def explain(
    scanner: Scanner,
    repo: str,
    run_id: int,
    *,
    attempt: int | None = None,
    days: int = 30,
    check_main: bool = True,
    prior: list[FailedJob] | None = None,
) -> Explanation:
    gh: GitHub = scanner.gh
    run = gh.run(repo, run_id)
    attempt = attempt or run.get("run_attempt") or 1
    if attempt != run.get("run_attempt"):
        run = gh.run(repo, run_id, attempt)
    finished = run.get("status") == "completed"
    jobs = scanner.attempt_jobs(repo, run_id, attempt, final=finished)
    failed = [j for j in jobs if j.get("conclusion") in FAILED_CONCLUSIONS]
    out = Explanation(repo=repo, run=run, attempt=attempt, days=days)
    if not failed:
        return out

    scanner.progress(
        f"learning this repo's flaky tests from the last {days} days of {run.get('name')!r}"
    )
    wf = run.get("name")
    prior = [j for j in (prior or []) if not wf or j.workflow == wf]
    history = scanner.scan(repo, days=days, workflow_id=run.get("workflow_id"), prior=prior)
    out.history = history
    known = history.test_index()
    job_history = {name: total for name, total, _ in history.job_causes()}

    texts = scanner.logs(repo, [j["id"] for j in failed])
    main_failing: dict[str, str] = {}
    if check_main:
        main_failing, out.main_branch = _failing_on_default_branch(scanner, repo, run)

    # Roll-up jobs only repeat that something else failed; drop them when anything else did.
    if any(not is_summary_job(j.get("name") or "") for j in failed):
        failed = [j for j in failed if not is_summary_job(j.get("name") or "")]
    for j in failed:
        fj = FailedJob(
            repo=repo,
            run_id=run_id,
            attempt=attempt,
            final_attempt=attempt,
            workflow=run.get("name") or "",
            job_name=j.get("name") or "",
            job_id=j["id"],
            url=j.get("html_url") or "",
            head_sha=run.get("head_sha") or "",
            branch=run.get("head_branch") or "",
            event=run.get("event") or "",
            os=runner_os(j),
            started_at=parse_time(j.get("started_at")),
            completed_at=parse_time(j.get("completed_at")),
            recovered_at=None,
            failed_step=j.get("failed_step") or "",
        )
        text = texts.get(j["id"])
        if text is None:
            out.findings.append(
                Finding(
                    LOG_EXPIRED, None, fj.job_name, fj.url, "the job log is no longer available"
                )
            )
            continue
        fj.kind, fj.tests, fj.infra, fj.hint = explain_log(text)
        fj.kind = refine_kind(fj)
        if fj.kind in (KIND_TESTS, KIND_MASS):
            for t in fj.tests:
                if t.outcome == FLAKY:
                    continue
                out.findings.append(
                    _judge_test(t, fj, known.get(t.test_id), main_failing, out.main_branch, days)
                )
            if not any(t.outcome != FLAKY for t in fj.tests):
                out.findings.append(_judge_opaque(fj, job_history, days))
        elif fj.kind == KIND_INFRA and fj.infra:
            out.findings.append(
                Finding(
                    INFRASTRUCTURE,
                    True,
                    fj.job_name,
                    fj.url,
                    f"{fj.infra.category}: {fj.infra.evidence}",
                )
            )
        else:
            out.findings.append(_judge_opaque(fj, job_history, days))
    return out


def _judge_test(
    t, job: FailedJob, stat: TestStat | None, main_failing, main_branch, days
) -> Finding:
    if stat is not None and stat.times:
        where = f" (seen on {', '.join(stat.oses)})" if stat.oses else ""
        detail = (
            f"failed and then passed on a re-run of the same commit {_times(stat.times)} "
            f"in the last {days} days, across {stat.commits} commit{'s' if stat.commits != 1 else ''}{where}"
        )
        return Finding(
            KNOWN_FLAKY, True, job.job_name, job.url, detail, t.test_id, t.framework, stat
        )
    if t.test_id in main_failing:
        detail = f"the latest finished run on {main_branch} failed this test too: {main_failing[t.test_id]}"
        return Finding(ALREADY_FAILING, True, job.job_name, job.url, detail, t.test_id, t.framework)
    detail = f"not seen flaking in the last {days} days" + (f"; {t.message}" if t.message else "")
    return Finding(LOOKS_REAL, False, job.job_name, job.url, detail, t.test_id, t.framework)


def _judge_opaque(job: FailedJob, job_history: dict[str, int], days: int) -> Finding:
    if job.kind == KIND_EXPIRED:
        return Finding(
            LOG_EXPIRED, None, job.job_name, job.url, "the job log is no longer available"
        )
    if job.kind == KIND_GATE:
        return Finding(
            POLICY_CHECK,
            None,
            job.job_name,
            job.url,
            "looks like a PR policy check (labels, linked issue, title) rather than a test; "
            "it usually passes once the PR is updated",
        )
    if job.kind == KIND_SUMMARY:
        return Finding(
            UNEXPLAINED,
            None,
            job.job_name,
            job.url,
            "a roll-up job that fails when another job fails; check the jobs it depends on",
        )
    step = f" in step {job.failed_step!r}" if job.failed_step else ""
    n = job_history.get(job.job_name, 0)
    if n:
        detail = (
            f"no test named in the log{step}, but this job failed and then passed on a re-run "
            f"{_times(n)} in the last {days} days"
        )
        if job.hint:
            detail += f". Last output: {job.hint}"
        return Finding(UNEXPLAINED, None, job.job_name, job.url, detail)
    # Same standard as for tests: never flaked in the window and no environment cause.
    detail = f"this job hasn't flaked in the last {days} days and the log shows no infrastructure problem{step}"
    if job.hint:
        detail += f". Last output: {job.hint}"
    return Finding(LOOKS_REAL, False, job.job_name, job.url, detail)


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def _failing_on_default_branch(
    scanner: Scanner, repo: str, run: dict
) -> tuple[dict[str, str], str]:
    """Tests failing in the latest finished run of the same workflow on the default branch."""
    gh = scanner.gh
    try:
        branch = (run.get("repository") or {}).get("default_branch") or gh.repo(repo)[
            "default_branch"
        ]
        runs = gh.get(
            gh.runs_path(repo, run.get("workflow_id")),
            {
                "branch": branch,
                "status": "completed",
                "per_page": 10,
                "exclude_pull_requests": "true",
            },
        )["workflow_runs"]
    except GitHubError:
        return {}, ""
    created = parse_time(run.get("created_at")) or datetime.now(UTC)
    earlier = [
        r
        for r in runs
        if r["id"] != run["id"]
        and (parse_time(r.get("created_at")) or created) < created
        and r.get("conclusion") in ("success", "failure")
    ]
    if not earlier or earlier[0].get("conclusion") != "failure":
        return {}, branch
    latest = earlier[0]
    jobs = scanner.attempt_jobs(repo, latest["id"], latest.get("run_attempt") or 1)
    failed = [j for j in jobs if j.get("conclusion") in FAILED_CONCLUSIONS]
    texts = scanner.logs(repo, [j["id"] for j in failed])
    out: dict[str, str] = {}
    for j in failed:
        if (text := texts.get(j["id"])) is None:
            continue
        _, tests, _, _ = explain_log(text)
        for t in tests:
            out.setdefault(t.test_id, latest.get("html_url") or "")
    return out, branch
