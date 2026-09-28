"""The little of the GitHub REST API that redherring needs, with rate limits handled."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

API = "https://api.github.com"
# The runs endpoint returns at most 1000 results for any single filter, however you page.
_SEARCH_CAP = 1000
_MIN_WINDOW = timedelta(minutes=30)


class GitHubError(RuntimeError):
    pass


class LogGone(GitHubError):
    """The job log has expired (retention) or was deleted."""


def resolve_token() -> str | None:
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if tok := os.environ.get(var, "").strip():
            return tok
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class GitHub:
    def __init__(
        self,
        token: str | None,
        *,
        api: str = API,
        transport: httpx.BaseTransport | None = None,
        on_wait: Callable[[str, float], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "redherring",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # httpx drops the Authorization header when a redirect leaves api.github.com,
        # which is what the log download needs (it redirects to blob storage).
        self._http = httpx.Client(
            base_url=api,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            transport=transport,
        )
        self._on_wait = on_wait or (lambda reason, seconds: None)
        self._sleep = sleep
        self.requests = 0

    def close(self) -> None:
        self._http.close()

    # -- plumbing ------------------------------------------------------------------------

    def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        for attempt in range(6):
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:
                if attempt == 5:
                    raise GitHubError(f"network error talking to GitHub: {e}") from e
                self._sleep(2**attempt)
                continue
            self.requests += 1
            if resp.status_code in (403, 429) and self._rate_limited(resp):
                continue
            if resp.status_code >= 500 and attempt < 5:
                self._sleep(2**attempt)
                continue
            return resp
        return resp

    def _rate_limited(self, resp: httpx.Response) -> bool:
        """Sleep through a rate limit and return True, or return False if this isn't one."""
        body = resp.text.lower()
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = int(resp.headers.get("x-ratelimit-reset", "0"))
            wait = max(1.0, reset - time.time() + 1)
            self._on_wait("GitHub API rate limit reached", wait)
            self._sleep(wait)
            return True
        if "secondary rate limit" in body or "abuse" in body or resp.status_code == 429:
            wait = float(resp.headers.get("retry-after", "60"))
            self._on_wait("GitHub secondary rate limit", wait)
            self._sleep(wait)
            return True
        return False

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._request(path, params)
        if resp.status_code == 404:
            raise GitHubError(
                f"not found: {path} (is the repo name right, and can your token see it?)"
            )
        if resp.status_code == 401:
            raise GitHubError(
                "GitHub rejected the token (401). Set GH_TOKEN or run `gh auth login`."
            )
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub returned {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    def paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        params = dict(params or {}, per_page=100)
        page = 1
        while True:
            data = self.get(path, dict(params, page=page))
            items = data[key]
            yield from items
            if len(items) < 100:
                return
            page += 1

    # -- endpoints -----------------------------------------------------------------------

    def repo(self, repo: str) -> dict:
        return self.get(f"/repos/{repo}")

    def run(self, repo: str, run_id: int, attempt: int | None = None) -> dict:
        if attempt:
            return self.get(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
        return self.get(f"/repos/{repo}/actions/runs/{run_id}")

    def attempt_jobs(self, repo: str, run_id: int, attempt: int) -> list[dict]:
        return list(
            self.paginate(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        )

    def runs(
        self,
        repo: str,
        since: datetime,
        until: datetime,
        *,
        status: str | None = None,
        workflow_id: int | None = None,
        extra: dict[str, Any] | None = None,
        on_page: Callable[[int], None] | None = None,
    ) -> Iterator[dict]:
        """Every run created in [since, until), splitting the window around the 1000 cap."""
        params: dict[str, Any] = {"exclude_pull_requests": "true", **(extra or {})}
        if status:
            params["status"] = status
        path = self.runs_path(repo, workflow_id)
        yield from self._runs_window(path, since, until, params, on_page)

    @staticmethod
    def runs_path(repo: str, workflow_id: int | None = None) -> str:
        if workflow_id:
            return f"/repos/{repo}/actions/workflows/{workflow_id}/runs"
        return f"/repos/{repo}/actions/runs"

    def count_runs(
        self,
        repo: str,
        since: datetime,
        until: datetime,
        *,
        status: str,
        workflow_id: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> int:
        params = {
            "status": status,
            "created": f"{iso(since)}..{iso(until - timedelta(seconds=1))}",
            "per_page": 1,
            "exclude_pull_requests": "true",
            **(extra or {}),
        }
        return self.get(self.runs_path(repo, workflow_id), params)["total_count"]

    def workflows(self, repo: str) -> list[dict]:
        return list(self.paginate(f"/repos/{repo}/actions/workflows", "workflows"))

    def _runs_window(self, path, since, until, params, on_page) -> Iterator[dict]:
        window = dict(params, created=f"{iso(since)}..{iso(until - timedelta(seconds=1))}")
        first = self.get(path, dict(window, per_page=100, page=1))
        total = first["total_count"]
        if total > _SEARCH_CAP and until - since > _MIN_WINDOW:
            mid = since + (until - since) / 2
            yield from self._runs_window(path, since, mid, params, on_page)
            yield from self._runs_window(path, mid, until, params, on_page)
            return
        batch = first["workflow_runs"]
        yield from batch
        if on_page:
            on_page(len(batch))
        page = 2
        while len(batch) == 100 and (page - 1) * 100 < min(total, _SEARCH_CAP):
            batch = self.get(path, dict(window, per_page=100, page=page))["workflow_runs"]
            yield from batch
            if on_page:
                on_page(len(batch))
            page += 1

    def job_log(self, repo: str, job_id: int) -> str:
        resp = self._request(f"/repos/{repo}/actions/jobs/{job_id}/logs")
        if resp.status_code in (404, 410):
            raise LogGone(f"log for job {job_id} is gone")
        if resp.status_code >= 400:
            raise GitHubError(f"could not download log for job {job_id}: HTTP {resp.status_code}")
        return resp.content.decode("utf-8", errors="replace")
