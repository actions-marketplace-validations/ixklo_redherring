"""A tiny in-memory GitHub Actions API for tests, served through httpx.MockTransport."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from redherring.github import parse_time

REPO = "acme/app"


def ts(s: str) -> str:
    return s if s.endswith("Z") else s + "Z"


@dataclass
class Fake:
    runs: list[dict] = field(default_factory=list)
    # (run_id, attempt) -> jobs
    jobs: dict[tuple[int, int], list[dict]] = field(default_factory=dict)
    # job_id -> log text, or None for an expired log
    logs: dict[int, str | None] = field(default_factory=dict)
    default_branch: str = "main"
    calls: list[str] = field(default_factory=list)
    # Queue of (status, headers) to return before real answers, for rate-limit tests.
    interrupts: list[tuple[int, dict]] = field(default_factory=list)
    cap_total: int | None = None
    # sha -> PRs ({"number", "state"}) for /commits/{sha}/pulls
    pulls_by_sha: dict[str, list[dict]] = field(default_factory=dict)
    # PR number -> issue comments
    comments: dict[int, list[dict]] = field(default_factory=dict)
    writes: list[tuple[str, str, dict]] = field(default_factory=list)
    # Answer If-None-Match with 304 like GitHub does (304s cost no rate limit).
    etags: bool = True
    not_modified: int = 0

    def run(
        self,
        id,
        *,
        attempt=1,
        conclusion="success",
        sha="sha",
        branch="feature",
        event="push",
        created="2026-09-20T10:00:00",
        updated=None,
        workflow_id=7,
        name="CI",
    ):
        r = {
            "id": id,
            "run_attempt": attempt,
            "conclusion": conclusion,
            "status": "completed",
            "head_sha": sha,
            "head_branch": branch,
            "event": event,
            "name": name,
            "workflow_id": workflow_id,
            "path": ".github/workflows/ci.yml",
            "created_at": ts(created),
            "updated_at": ts(updated or created),
            "run_number": id,
            "html_url": f"https://github.com/{REPO}/actions/runs/{id}",
            "pull_requests": [],
        }
        self.runs.append(r)
        return r

    def job(
        self,
        run_id,
        attempt,
        job_id,
        name,
        conclusion,
        *,
        start="2026-09-20T10:00:00",
        end="2026-09-20T10:05:00",
        labels=("ubuntu-latest",),
        log="",
    ):
        j = {
            "id": job_id,
            "name": name,
            "conclusion": conclusion,
            "started_at": ts(start),
            "completed_at": ts(end),
            "labels": list(labels),
            "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{job_id}",
            "steps": [{"name": "Run tests", "conclusion": conclusion}],
        }
        self.jobs.setdefault((run_id, attempt), []).append(j)
        self.logs[job_id] = log
        return j

    # -- transport -----------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        resp = self._route(request)
        if not self.etags or request.method != "GET" or resp.status_code != 200:
            return resp
        if "json" not in resp.headers.get("content-type", ""):
            return resp
        etag = '"' + hashlib.md5(resp.content).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            self.not_modified += 1
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(200, content=resp.content, headers={**resp.headers, "etag": etag})

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        q = dict(request.url.params)
        self.calls.append(f"{path}?{request.url.query.decode()}")
        if self.interrupts:
            status, headers = self.interrupts.pop(0)
            return httpx.Response(
                status, headers=headers, json={"message": "API rate limit exceeded"}
            )

        if path == f"/repos/{REPO}":
            return _json({"full_name": REPO, "default_branch": self.default_branch})
        if m := re.fullmatch(rf"/repos/{REPO}/actions/(?:workflows/(\d+)/)?runs", path):
            return self._list_runs(q, int(m[1]) if m[1] else None)
        if m := re.fullmatch(rf"/repos/{REPO}/actions/runs/(\d+)/attempts/(\d+)/jobs", path):
            jobs = self.jobs.get((int(m[1]), int(m[2])), [])
            return _json({"total_count": len(jobs), "jobs": jobs})
        if m := re.fullmatch(rf"/repos/{REPO}/actions/runs/(\d+)(?:/attempts/(\d+))?", path):
            run = next((r for r in self.runs if r["id"] == int(m[1])), None)
            if run is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if m[2]:
                run = dict(run, run_attempt=int(m[2]))
            return _json(run)
        if m := re.fullmatch(rf"/repos/{REPO}/actions/jobs/(\d+)/logs", path):
            text = self.logs.get(int(m[1]))
            if text is None:
                return httpx.Response(410, text="Gone")
            return httpx.Response(200, text=text)
        if m := re.fullmatch(rf"/repos/{REPO}/commits/(\w+)/pulls", path):
            return _json(self.pulls_by_sha.get(m[1], []))
        if m := re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/comments", path):
            number = int(m[1])
            if request.method == "POST":
                body = json.loads(request.content)
                self.writes.append(("POST", path, body))
                new_id = 9000 + sum(len(v) for v in self.comments.values())
                c = {
                    "id": new_id,
                    "body": body["body"],
                    "html_url": f"https://github.com/{REPO}/pull/{number}#c{new_id}",
                }
                self.comments.setdefault(number, []).append(c)
                return _json(c)
            return _json(self.comments.get(number, []))
        if m := re.fullmatch(rf"/repos/{REPO}/issues/comments/(\d+)", path):
            body = json.loads(request.content)
            self.writes.append((request.method, path, body))
            for cs in self.comments.values():
                for c in cs:
                    if c["id"] == int(m[1]):
                        c["body"] = body["body"]
                        return _json(c)
            return httpx.Response(404, json={"message": "Not Found"})
        if path == f"/repos/{REPO}/actions/workflows":
            return _json(
                {
                    "total_count": 1,
                    "workflows": [{"id": 7, "name": "CI", "path": ".github/workflows/ci.yml"}],
                }
            )
        return httpx.Response(404, json={"message": f"unhandled {path}"})

    def _list_runs(self, q: dict, workflow_id: int | None) -> httpx.Response:
        runs = [r for r in self.runs if workflow_id is None or r["workflow_id"] == workflow_id]
        if s := q.get("status"):
            runs = [
                r
                for r in runs
                if r["conclusion"] == s or (s == "completed" and r["status"] == "completed")
            ]
        if b := q.get("branch"):
            runs = [r for r in runs if r["head_branch"] == b]
        if c := q.get("created"):
            lo, hi = c.split("..")
            lo_t, hi_t = parse_time(lo), parse_time(hi)
            runs = [r for r in runs if lo_t <= parse_time(r["created_at"]) <= hi_t]
        runs.sort(key=lambda r: r["created_at"], reverse=True)
        total = len(runs)
        if self.cap_total is not None and q.get("created"):
            lo, hi = (parse_time(x) for x in q["created"].split(".."))
            # Pretend wide windows hold more than the API will page through.
            if (hi - lo).total_seconds() > 6 * 3600:
                total = max(total, self.cap_total)
        per = int(q.get("per_page", 30))
        page = int(q.get("page", 1))
        return _json({"total_count": total, "workflow_runs": runs[(page - 1) * per : page * per]})


def _json(obj) -> httpx.Response:
    return httpx.Response(
        200, content=json.dumps(obj), headers={"content-type": "application/json"}
    )


def at(s: str) -> datetime:
    return parse_time(ts(s))
