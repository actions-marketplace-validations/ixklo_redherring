"""Explain one failed run: which failures are red herrings and which look real."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .github import GitHub, GitHubError, parse_time
from .parsers import FLAKY, TestFailure
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
    is_summary_job,
    refine_kind,
    runner_os,
)

KNOWN_FLAKY = "known flaky"
PROBABLY_FLAKY = "probably flaky"
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

# A test must have flaked this many times before its failure is called a red herring outright.
KNOWN_FLAKY_MIN = 2

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
    # redherring's own explanation.
    detail: str
    test_id: str = ""
    framework: str = ""
    history: TestStat | None = None
    # Text copied from the job log (a failure message, the last output). Untrusted: whoever
    # controls the tests controls it, so renderers must escape it.
    evidence: str = ""


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


@dataclass
class _Context:
    """Everything a verdict is judged against."""

    days: int
    attempt: int
    known: dict[str, TestStat]
    job_history: dict[str, int]
    main_failing: dict[str, str]
    main_branch: str
    # Earlier attempts of this same run (same commit): failing test id / job name -> attempts.
    earlier_tests: dict[str, list[int]]
    earlier_jobs: dict[str, list[int]]


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

    # Roll-up jobs only repeat that something else failed; drop them when anything else did.
    if any(not is_summary_job(j.get("name") or "") for j in failed):
        failed = [j for j in failed if not is_summary_job(j.get("name") or "")]
    found = scanner.readings(repo, [j["id"] for j in failed])

    main_failing: dict[str, str] = {}
    if check_main:
        main_failing, out.main_branch = _failing_on_default_branch(scanner, repo, run)
    earlier_tests, earlier_jobs = _failed_in_earlier_attempts(scanner, repo, run_id, attempt)
    ctx = _Context(
        days=days,
        attempt=attempt,
        known=history.test_index(),
        job_history={name: total for name, total, _ in history.job_causes()},
        main_failing=main_failing,
        main_branch=out.main_branch,
        earlier_tests=earlier_tests,
        earlier_jobs=earlier_jobs,
    )

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
        reading = found.get(j["id"])
        if reading is None:
            out.findings.append(
                Finding(
                    LOG_EXPIRED, None, fj.job_name, fj.url, "the job log is no longer available"
                )
            )
            continue
        fj.kind, fj.tests, fj.infra, fj.hint = reading
        fj.kind = refine_kind(fj)
        if fj.kind in (KIND_TESTS, KIND_MASS):
            for t in fj.tests:
                if t.outcome != FLAKY:
                    out.findings.append(_judge_test(t, fj, ctx))
            if not any(t.outcome != FLAKY for t in fj.tests):
                out.findings.append(_judge_opaque(fj, ctx))
        elif fj.kind == KIND_INFRA and fj.infra:
            out.findings.append(
                Finding(
                    INFRASTRUCTURE,
                    True,
                    fj.job_name,
                    fj.url,
                    fj.infra.category,
                    evidence=fj.infra.evidence,
                )
            )
        else:
            out.findings.append(_judge_opaque(fj, ctx))
    return out


def _judge_test(t: TestFailure, job: FailedJob, ctx: _Context) -> Finding:
    def finding(verdict: str, red_herring: bool, detail: str, stat: TestStat | None = None):
        return Finding(
            verdict,
            red_herring,
            job.job_name,
            job.url,
            detail,
            t.test_id,
            t.framework,
            stat,
            evidence=t.message,
        )

    if t.test_id in ctx.main_failing:
        return finding(
            ALREADY_FAILING,
            True,
            f"the latest finished run on {ctx.main_branch} failed this test too: "
            f"{ctx.main_failing[t.test_id]}",
        )
    if earlier := ctx.earlier_tests.get(t.test_id):
        # A re-run is the flakiness check: failing again on the same commit means it isn't one.
        return finding(
            LOOKS_REAL,
            False,
            f"failed again on attempt {ctx.attempt} of the same commit "
            f"(also on attempt {_attempts(earlier)}), so a re-run isn't fixing it",
        )
    stat = ctx.known.get(t.test_id)
    if stat is not None and stat.times >= KNOWN_FLAKY_MIN:
        where = f" (seen on {', '.join(stat.oses)})" if stat.oses else ""
        return finding(
            KNOWN_FLAKY,
            True,
            f"failed and then passed on a re-run of the same commit {_times(stat.times)} "
            f"in the last {ctx.days} days, across {_n(stat.commits, 'commit')}{where}",
            stat,
        )
    if stat is not None and stat.times:
        return finding(
            PROBABLY_FLAKY,
            True,
            f"failed and then passed on a re-run once in the last {ctx.days} days. Re-run once to "
            "check: if it fails again, redherring will call it real",
            stat,
        )
    return finding(LOOKS_REAL, False, f"not seen flaking in the last {ctx.days} days")


def _judge_opaque(job: FailedJob, ctx: _Context) -> Finding:
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
    evidence = "; ".join(
        part
        for part in (
            f"failed step: {job.failed_step}" if job.failed_step else "",
            f"last output: {job.hint}" if job.hint else "",
        )
        if part
    )
    if earlier := ctx.earlier_jobs.get(job.job_name):
        return Finding(
            LOOKS_REAL,
            False,
            job.job_name,
            job.url,
            f"no test named in the log, and this job failed again on attempt {ctx.attempt} of the "
            f"same commit (also on attempt {_attempts(earlier)})",
            evidence=evidence,
        )
    if n := ctx.job_history.get(job.job_name, 0):
        return Finding(
            UNEXPLAINED,
            None,
            job.job_name,
            job.url,
            f"no test named in the log, but this job failed and then passed on a re-run "
            f"{_times(n)} in the last {ctx.days} days",
            evidence=evidence,
        )
    # Same standard as for tests: never flaked in the window and no environment cause.
    return Finding(
        LOOKS_REAL,
        False,
        job.job_name,
        job.url,
        f"this job hasn't flaked in the last {ctx.days} days and the log shows no "
        "infrastructure problem",
        evidence=evidence,
    )


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def _n(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _attempts(attempts: list[int]) -> str:
    return ", ".join(str(a) for a in sorted(set(attempts)))


def _failed_in_earlier_attempts(
    scanner: Scanner, repo: str, run_id: int, attempt: int
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Tests and jobs that already failed in earlier attempts of this run (the same commit)."""
    tests: dict[str, list[int]] = {}
    jobs: dict[str, list[int]] = {}
    for k in range(1, attempt):
        try:
            earlier = scanner.attempt_jobs(repo, run_id, k)
        except GitHubError:
            continue
        failed = [
            j
            for j in earlier
            if j.get("conclusion") in FAILED_CONCLUSIONS and not is_summary_job(j.get("name") or "")
        ]
        found = scanner.readings(repo, [j["id"] for j in failed])
        for j in failed:
            jobs.setdefault(j.get("name") or "", []).append(k)
            if (reading := found.get(j["id"])) is None:
                continue
            for t in reading[1]:
                if t.outcome != FLAKY:
                    tests.setdefault(t.test_id, []).append(k)
    return tests, jobs


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
    found = scanner.readings(repo, [j["id"] for j in failed])
    out: dict[str, str] = {}
    for j in failed:
        if (reading := found.get(j["id"])) is None:
            continue
        for t in reading[1]:
            out.setdefault(t.test_id, latest.get("html_url") or "")
    return out, branch
