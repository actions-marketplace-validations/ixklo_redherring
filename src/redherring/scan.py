"""Mine a repository's Actions history for red herrings.

The evidence is deliberately narrow: a workflow run whose final attempt passed, after an
earlier attempt failed, on the same commit. Nothing in the code changed between the two
attempts, so whatever failed first was not caused by the commit. Each failed job in the
earlier attempt is then explained from its log: named tests (flaky tests), an
infrastructure cause (network, registry, runner...), a policy check that passed after a
PR change (not flakiness at all), or unknown.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .cache import Cache
from .github import GitHub, LogGone, parse_time
from .infra import InfraCause, classify, last_output
from .logtext import clean_lines
from .parsers import FLAKY, TestFailure, parse_failures

FAILED_CONCLUSIONS = ("failure", "timed_out")

KIND_TESTS = "tests"
KIND_INFRA = "infra"
KIND_GATE = "gate"
KIND_UNKNOWN = "unknown"
KIND_EXPIRED = "log expired"
KIND_SUMMARY = "summary"
KIND_MASS = "many tests at once"

# A job where this many tests fail together is an environment problem, not N flaky tests.
MASS_FAILURE = 10

# Jobs that fail or pass on PR metadata, not code: re-running them after adding a label or
# linking an issue is not flakiness.
_GATE_NAME = re.compile(
    r"label|precondition|issue[-_ ]?link|pr[-_ ]?title|semantic|\bcla\b|\bdco\b|changelog|triage"
    r"|assign|approv|conventional|require|policy|enforce|welcome|milestone|ready[-_ ]for|draft",
    re.I,
)
_GATE_EVENTS = {
    "pull_request_target",
    "issue_comment",
    "issues",
    "pull_request_review",
    "pull_request_review_comment",
    "label",
}
# Roll-up jobs ("all required jobs passed") fail only because another job failed.
_SUMMARY_NAME = re.compile(
    r"^(?:all[-_ ].*(?:pass|green|success|succeed|jobs|done|required|checks)\w*"
    r"|.*alls?[-_]green.*|required[-_ ]checks?\b.*|ci[-_ ]?(?:success|ok|status|result|passed|complete)"
    r"|.*\b(?:all|required) (?:jobs|checks) (?:passed|succeeded|completed?|green))\s*$",
    re.I,
)


@dataclass
class FailedJob:
    """One job that failed in an attempt the same run later re-ran to green."""

    repo: str
    run_id: int
    attempt: int
    final_attempt: int
    workflow: str
    job_name: str
    job_id: int
    url: str
    head_sha: str
    branch: str
    event: str
    os: str
    started_at: datetime | None
    completed_at: datetime | None
    recovered_at: datetime | None
    failed_step: str = ""
    kind: str = KIND_UNKNOWN
    tests: list[TestFailure] = field(default_factory=list)
    infra: InfraCause | None = None
    hint: str = ""

    @property
    def seconds(self) -> float:
        if self.started_at and self.completed_at:
            return max(0.0, (self.completed_at - self.started_at).total_seconds())
        return 0.0


@dataclass
class TestStat:
    test_id: str
    framework: str
    failures: list[FailedJob] = field(default_factory=list)
    in_job_retries: int = 0
    message: str = ""

    @property
    def times(self) -> int:
        return len({(f.run_id, f.attempt) for f in self.failures})

    @property
    def commits(self) -> int:
        return len({f.head_sha for f in self.failures})

    @property
    def oses(self) -> list[str]:
        return sorted({f.os for f in self.failures if f.os})

    @property
    def jobs(self) -> list[str]:
        return sorted({f.job_name for f in self.failures})

    @property
    def last_seen(self) -> datetime | None:
        times = [f.completed_at for f in self.failures if f.completed_at]
        return max(times) if times else None


@dataclass
class ScanResult:
    repo: str
    since: datetime
    until: datetime
    runs_passed: int = 0
    runs_failed: int = 0
    recovered_runs: list[dict] = field(default_factory=list)
    failed_jobs: list[FailedJob] = field(default_factory=list)
    tests: list[TestStat] = field(default_factory=list)
    red_seconds: list[float] = field(default_factory=list)
    api_requests: int = 0

    @property
    def runs_went_red(self) -> int:
        """Runs whose first attempt failed: the ones that ended red plus the re-run ones."""
        return self.runs_failed + len(self.recovered_runs)

    @property
    def herring_jobs(self) -> list[FailedJob]:
        return [j for j in self.failed_jobs if j.kind not in (KIND_GATE, KIND_SUMMARY)]

    @property
    def gate_jobs(self) -> list[FailedJob]:
        return [j for j in self.failed_jobs if j.kind == KIND_GATE]

    @property
    def herring_runs(self) -> int:
        return len({j.run_id for j in self.herring_jobs})

    @property
    def runner_seconds(self) -> float:
        return sum(j.seconds for j in self.herring_jobs)

    def test_index(self) -> dict[str, TestStat]:
        return {t.test_id: t for t in self.tests}

    def job_causes(self) -> list[tuple[str, int, Counter]]:
        """Jobs that flaked without naming a test, with what caused them."""
        by_job: dict[str, Counter] = defaultdict(Counter)
        for j in self.herring_jobs:
            if j.kind == KIND_TESTS:
                continue
            by_job[j.job_name][j.infra.category if j.infra else j.kind] += 1
        rows = [(name, sum(c.values()), c) for name, c in by_job.items()]
        return sorted(rows, key=lambda r: (-r[1], r[0]))

    def cause_counts(self) -> Counter:
        c: Counter = Counter()
        for j in self.herring_jobs:
            c[
                "flaky tests" if j.kind == KIND_TESTS else (j.infra.category if j.infra else j.kind)
            ] += 1
        return c


def runner_os(job: dict) -> str:
    text = " ".join(job.get("labels") or []) + " " + (job.get("name") or "")
    text = text.lower()
    for key, name in (
        ("windows", "windows"),
        ("macos", "macos"),
        ("mac-", "macos"),
        ("ubuntu", "linux"),
        ("linux", "linux"),
    ):
        if key in text:
            return name
    return (job.get("labels") or [""])[0]


def failed_step(job: dict) -> str:
    for step in job.get("steps") or []:
        if step.get("conclusion") in FAILED_CONCLUSIONS:
            return step.get("name", "")
    return ""


def explain_log(text: str) -> tuple[str, list[TestFailure], InfraCause | None, str]:
    """(kind, tests, infra cause, hint) for one failed job's log."""
    lines = clean_lines(text)
    tests = parse_failures(lines)
    if tests:
        return KIND_TESTS, tests, None, ""
    cause = classify(lines)
    if cause:
        return KIND_INFRA, [], cause, ""
    return KIND_UNKNOWN, [], None, last_output(lines)


def _slim_job(job: dict) -> dict:
    keep = ("id", "name", "conclusion", "started_at", "completed_at", "html_url", "labels")
    slim = {k: job.get(k) for k in keep}
    slim["failed_step"] = failed_step(job)
    return slim


class Scanner:
    def __init__(
        self,
        gh: GitHub,
        cache: Cache,
        *,
        workers: int = 8,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.gh = gh
        self.cache = cache
        self.workers = workers
        self.progress = progress or (lambda msg: None)

    # -- fetching with the cache in front ------------------------------------------------

    def attempt_jobs(
        self, repo: str, run_id: int, attempt: int, *, final: bool = True
    ) -> list[dict]:
        if final and (cached := self.cache.get_attempt_jobs(repo, run_id, attempt)) is not None:
            return cached
        jobs = [_slim_job(j) for j in self.gh.attempt_jobs(repo, run_id, attempt)]
        if final:
            self.cache.put_attempt_jobs(repo, run_id, attempt, jobs)
        return jobs

    def logs(self, repo: str, job_ids: list[int]) -> dict[int, str | None]:
        """Log text per job id (None when expired), downloading what isn't cached."""
        out: dict[int, str | None] = {}
        missing = []
        for jid in job_ids:
            hit = self.cache.get_log(repo, jid)
            if hit is None:
                missing.append(jid)
            else:
                out[jid] = hit[1] if hit[0] == "ok" else None

        def fetch(jid: int) -> tuple[int, str | None]:
            try:
                return jid, self.gh.job_log(repo, jid)
            except LogGone:
                return jid, None

        if missing:
            self.progress(f"downloading {len(missing)} job logs")
            with ThreadPoolExecutor(self.workers) as pool:
                for jid, text in pool.map(fetch, missing):
                    self.cache.put_log(repo, jid, text)
                    out[jid] = text
        return out

    # -- the scan ------------------------------------------------------------------------

    def scan(
        self,
        repo: str,
        *,
        days: int = 30,
        until: datetime | None = None,
        workflow_id: int | None = None,
        branch: str | None = None,
    ) -> ScanResult:
        until = until or datetime.now(UTC)
        since = until - timedelta(days=days)
        result = ScanResult(repo=repo, since=since, until=until)
        start_requests = self.gh.requests
        extra = {"branch": branch} if branch else {}

        seen = [0]

        def on_page(n: int) -> None:
            seen[0] += n
            self.progress(f"listing runs: {seen[0]}")

        passed = list(
            self.gh.runs(
                repo,
                since,
                until,
                status="success",
                workflow_id=workflow_id,
                extra=extra,
                on_page=on_page,
            )
        )
        result.runs_passed = len(passed)
        result.runs_failed = self.gh.count_runs(
            repo, since, until, status="failure", workflow_id=workflow_id, extra=extra
        )
        recovered = [r for r in passed if (r.get("run_attempt") or 1) > 1]
        result.recovered_runs = recovered
        self.progress(
            f"{len(recovered)} runs passed only after a re-run; reading their failed attempts"
        )

        # Failed jobs from every earlier attempt of every recovered run.
        tasks = [(r, k) for r in recovered for k in range(1, r["run_attempt"])]
        with ThreadPoolExecutor(self.workers) as pool:
            jobs_per_task = list(
                pool.map(lambda t: self.attempt_jobs(repo, t[0]["id"], t[1]), tasks)
            )

        failed: list[FailedJob] = []
        attempt_end: dict[tuple[int, int], datetime] = {}
        for (run, k), jobs in zip(tasks, jobs_per_task, strict=True):
            ends = [t for j in jobs if (t := parse_time(j.get("completed_at")))]
            if ends:
                attempt_end[(run["id"], k)] = max(ends)
            for j in jobs:
                if j.get("conclusion") not in FAILED_CONCLUSIONS:
                    continue
                failed.append(
                    FailedJob(
                        repo=repo,
                        run_id=run["id"],
                        attempt=k,
                        final_attempt=run["run_attempt"],
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
                        recovered_at=parse_time(run.get("updated_at")),
                        failed_step=j.get("failed_step") or "",
                    )
                )

        texts = self.logs(repo, [f.job_id for f in failed])
        for f in failed:
            text = texts.get(f.job_id)
            if text is None:
                f.kind = KIND_EXPIRED
                continue
            f.kind, f.tests, f.infra, f.hint = explain_log(text)
            f.kind = refine_kind(f)
        result.failed_jobs = failed

        for run in recovered:
            first_end = attempt_end.get((run["id"], 1))
            done = parse_time(run.get("updated_at"))
            herring = any(
                j.run_id == run["id"] and j.kind not in (KIND_GATE, KIND_SUMMARY) for j in failed
            )
            if first_end and done and herring:
                result.red_seconds.append(max(0.0, (done - first_end).total_seconds()))

        result.tests = aggregate_tests(failed)
        result.api_requests = self.gh.requests - start_requests
        return result


def refine_kind(job: FailedJob) -> str:
    """Set aside roll-up jobs and PR policy checks, which are not flakiness."""
    if job.kind == KIND_TESTS:
        failed = sum(1 for t in job.tests if t.outcome != FLAKY)
        return KIND_MASS if failed > MASS_FAILURE else KIND_TESTS
    if job.kind == KIND_INFRA:
        return job.kind
    if is_summary_job(job.job_name):
        return KIND_SUMMARY
    if job.kind == KIND_UNKNOWN and (job.event in _GATE_EVENTS or _GATE_NAME.search(job.job_name)):
        return KIND_GATE
    return job.kind


def is_summary_job(name: str) -> bool:
    # Reusable workflows prefix job names ("ci / all-green"); judge the last part.
    return bool(_SUMMARY_NAME.match(name.rsplit(" / ", 1)[-1].strip()))


def aggregate_tests(failed: list[FailedJob]) -> list[TestStat]:
    stats: dict[str, TestStat] = {}
    for job in failed:
        if job.kind != KIND_TESTS:
            continue
        for t in job.tests:
            s = stats.get(t.test_id)
            if s is None:
                s = stats[t.test_id] = TestStat(t.test_id, t.framework, message=t.message)
            if t.outcome == FLAKY:
                s.in_job_retries += 1
                continue
            s.failures.append(job)
            if not s.message and t.message:
                s.message = t.message
    ranked = [s for s in stats.values() if s.failures or s.in_job_retries]
    return sorted(
        ranked,
        key=lambda s: (
            -s.times,
            -s.commits,
            -(s.last_seen or datetime.min.replace(tzinfo=UTC)).timestamp(),
        ),
    )
