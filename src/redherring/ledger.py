"""A flake ledger: the evidence redherring found, saved so it outlives GitHub's retention.

From 1 October 2026 GitHub deletes workflow runs once they pass the repository's log
retention period (90 days by default, and at most 90 days for public repositories). The
ledger is a small JSON file you keep (commit it, cache it, upload it as an artifact): each
scan merges new evidence into it, and `why` can use it as extra history.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .github import parse_time
from .infra import InfraCause
from .parsers import TestFailure
from .scan import FailedJob

VERSION = 1


def load(path: Path, repo: str) -> list[FailedJob]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != VERSION:
        raise ValueError(
            f"{path} is a redherring ledger version {data.get('version')}, expected {VERSION}"
        )
    if data.get("repo") and data["repo"].lower() != repo.lower():
        raise ValueError(f"{path} is the ledger for {data['repo']}, not {repo}")
    return [_from_entry(repo, e) for e in data.get("entries", [])]


def save(path: Path, repo: str, jobs: list[FailedJob]) -> None:
    jobs = sorted(
        jobs, key=lambda j: (j.completed_at or datetime.min.replace(tzinfo=UTC), j.job_id)
    )
    data = {
        "tool": "redherring",
        "version": VERSION,
        "repo": repo,
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
        "entries": [_to_entry(j) for j in jobs],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def merge(
    older: list[FailedJob], newer: list[FailedJob], *, keep_days: int, now: datetime | None = None
) -> list[FailedJob]:
    """Union by job, newer evidence winning, dropping entries older than keep_days."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=keep_days)
    by_key = {(j.run_id, j.attempt, j.job_id): j for j in older}
    for j in newer:
        by_key[(j.run_id, j.attempt, j.job_id)] = j
    return [j for j in by_key.values() if (j.completed_at or cutoff) >= cutoff]


def _to_entry(j: FailedJob) -> dict:
    return {
        "run_id": j.run_id,
        "attempt": j.attempt,
        "final_attempt": j.final_attempt,
        "workflow": j.workflow,
        "job": j.job_name,
        "job_id": j.job_id,
        "url": j.url,
        "sha": j.head_sha,
        "branch": j.branch,
        "event": j.event,
        "os": j.os,
        "started_at": _iso(j.started_at),
        "completed_at": _iso(j.completed_at),
        "recovered_at": _iso(j.recovered_at),
        "failed_step": j.failed_step,
        "kind": j.kind,
        "tests": [
            {
                "framework": t.framework,
                "test": t.test_id,
                "outcome": t.outcome,
                "message": t.message,
            }
            for t in j.tests
        ],
        "infra": {"category": j.infra.category, "evidence": j.infra.evidence} if j.infra else None,
        "hint": j.hint,
    }


def _from_entry(repo: str, e: dict) -> FailedJob:
    infra = e.get("infra")
    return FailedJob(
        repo=repo,
        run_id=e["run_id"],
        attempt=e["attempt"],
        final_attempt=e.get("final_attempt", e["attempt"] + 1),
        workflow=e.get("workflow", ""),
        job_name=e.get("job", ""),
        job_id=e["job_id"],
        url=e.get("url", ""),
        head_sha=e.get("sha", ""),
        branch=e.get("branch", ""),
        event=e.get("event", ""),
        os=e.get("os", ""),
        started_at=parse_time(e.get("started_at")),
        completed_at=parse_time(e.get("completed_at")),
        recovered_at=parse_time(e.get("recovered_at")),
        failed_step=e.get("failed_step", ""),
        kind=e.get("kind", "unknown"),
        tests=[
            TestFailure(t["framework"], t["test"], t.get("outcome", "failed"), t.get("message", ""))
            for t in e.get("tests", [])
        ],
        infra=InfraCause(infra["category"], infra.get("evidence", "")) if infra else None,
        hint=e.get("hint", ""),
    )


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
