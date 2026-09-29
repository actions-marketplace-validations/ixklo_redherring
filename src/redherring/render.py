"""Terminal, Markdown and JSON output for scans and explanations."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from statistics import median, quantiles
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .notes import FLAKY as MARKED_FLAKY
from .notes import JOB, NOT_FLAKY, TEST, Notes
from .report import ISSUES, Draft
from .scan import ScanResult, TestStat
from .why import (
    ALREADY_FAILING,
    INFRASTRUCTURE,
    INVESTIGATE,
    KNOWN_FLAKY,
    LOG_EXPIRED,
    LOOKS_REAL,
    NOTHING,
    POLICY_CHECK,
    PROBABLY_FLAKY,
    RERUN,
    UNEXPLAINED,
    Explanation,
)

# --- escaping ----------------------------------------------------------------------------
#
# Test ids, messages, job and step names come from CI logs and workflow files, which whoever
# opens a pull request can control. Render them as data, never as markup:
# - terminal: rich reads [brackets] as style tags, so wrap them in Text (shown literally);
# - Markdown (step summaries, PR comments): inline code, where @mentions, links and HTML
#   don't work.


def _md_code(s: str) -> str:
    """Untrusted text as inline code, safe inside a table cell."""
    s = " ".join(s.split()).replace("`", "'").replace("|", "\\|")
    return f"`{s}`" if s else ""


def _md_text(s: str) -> str:
    """Our own prose, safe inside a table cell (it may quote untrusted names)."""
    s = html.escape(" ".join(s.split()), quote=False)
    return s.replace("|", "\\|").replace("@", "@\u200b")


# --- small formatting helpers ------------------------------------------------------------


def hours(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    if not dt:
        return "-"
    delta = (now or datetime.now(UTC)) - dt
    d = delta.days
    if d >= 1:
        return f"{d}d ago"
    h = int(delta.total_seconds() // 3600)
    return f"{h}h ago" if h >= 1 else "just now"


def requests_note(r: ScanResult) -> str:
    note = f"{r.api_requests} GitHub API requests"
    if r.api_free:
        note += f" (+{r.api_free} unchanged, free)"
    return note


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def _where(stat: TestStat) -> str:
    return ", ".join(stat.oses) if stat.oses else "-"


_OS_SHORT = {"linux": "linux", "macos": "mac", "windows": "win"}


def _where_short(stat: TestStat) -> str:
    return " ".join(_OS_SHORT.get(o, o) for o in stat.oses) if stat.oses else "-"


def headline(r: ScanResult) -> list[str]:
    days = max(1, round((r.until - r.since).total_seconds() / 86400))
    lines = [
        f"{r.runs_passed + r.runs_failed:,} finished workflow runs in the last {days} days; "
        f"{r.runs_went_red:,} went red at least once."
    ]
    if r.runs_went_red:
        lines.append(
            f"{r.herring_runs:,} of those ({pct(r.herring_runs, r.runs_went_red)}) turned green on a plain re-run "
            "of the same commit: red herrings, not broken code."
        )
    if r.herring_jobs:
        line = f"The failed jobs burned {hours(r.runner_seconds)} of runner time."
        if r.red_seconds:
            line += (
                f" A red herring kept its commit red for {hours(median(r.red_seconds))} (median)"
            )
            if len(r.red_seconds) >= 10:
                slow = quantiles(r.red_seconds, n=10)[-1]
                line += f"; 1 in 10 stayed red longer than {hours(slow)}"
            line += "."
        lines.append(line)
    return lines


def _test_mark(notes: Notes | None, t: TestStat) -> str | None:
    mark = notes.for_test(t.test_id, t.jobs) if notes else None
    return mark.kind if mark else None


def _job_mark(notes: Notes | None, name: str) -> str | None:
    mark = notes.for_job(name) if notes else None
    return mark.kind if mark else None


def notes_check(r: ScanResult, notes: Notes | None) -> tuple[list[str], list[str]]:
    """Where the team's notes and the evidence disagree: (marked not flaky but flaked here,
    marked flaky by exact name but not seen flaking here)."""
    if notes is None:
        return [], []
    flaked = [t for t in r.tests if t.times]
    causes = r.job_causes()
    disagree = [t.test_id for t in flaked if _test_mark(notes, t) == NOT_FLAKY]
    disagree += [name for name, _, _ in causes if _job_mark(notes, name) == NOT_FLAKY]
    seen_tests = {t.test_id for t in flaked}
    seen_jobs = {name for name, _, _ in causes} | {j for t in flaked for j in t.jobs}
    unseen = [m.pattern for m in notes.plain(MARKED_FLAKY, TEST) if m.pattern not in seen_tests]
    unseen += [m.pattern for m in notes.plain(MARKED_FLAKY, JOB) if m.pattern not in seen_jobs]
    return disagree, unseen


def _few(names: list[str], limit: int = 8) -> str:
    more = f" and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


# --- scan: terminal ---------------------------------------------------------------------


def print_scan(
    r: ScanResult, console: Console, *, limit: int = 15, notes: Notes | None = None
) -> None:
    console.print(Text(f"redherring · {r.repo}", style="bold"))
    for line in headline(r):
        console.print("  " + line)
    console.print()

    named = [t for t in r.tests if t.times]
    if named:
        table = Table(
            title="Flaky tests: failed, then passed on the same commit",
            title_justify="left",
            show_edge=False,
            pad_edge=False,
        )
        table.add_column("times", justify="right")
        table.add_column("where", no_wrap=True)
        table.add_column("last", style="dim", no_wrap=True)
        table.add_column("test", overflow="fold", ratio=1)
        for t in named[:limit]:
            table.add_row(str(t.times), _where_short(t), ago(t.last_seen), Text(t.test_id))
        console.print(table)
        if len(named) > limit:
            console.print(
                f"  … and {len(named) - limit} more (use --limit or --format json)", style="dim"
            )
        console.print()

    if r.history_jobs:
        console.print(
            f"  Includes {len(r.history_jobs)} older red-herring job(s) from the ledger, "
            "from before this window.",
            style="dim",
        )
    retried = [t for t in r.tests if t.in_job_retries and not t.times]
    if retried:
        console.print(
            f"Also retried inside a job by the test runner itself: {len(retried)} test(s)",
            style="dim",
        )

    causes = r.job_causes()
    if causes:
        table = Table(
            title="Flaky jobs with no test named",
            title_justify="left",
            show_edge=False,
            pad_edge=False,
        )
        table.add_column("times", justify="right")
        table.add_column("cause")
        table.add_column("job", overflow="fold", ratio=1)
        for name, total, counter in causes[:limit]:
            table.add_row(
                str(total),
                ", ".join(f"{k} ({v})" if v > 1 else k for k, v in counter.most_common()),
                Text(name),
            )
        console.print(table)
        console.print()

    disagree, unseen = notes_check(r, notes)
    if notes and disagree:
        console.print(
            Text(
                f"Marked not flaky in {notes.source}, but failed and then passed on a re-run here: "
                + _few(disagree),
                style="yellow",
            )
        )
    if notes and unseen:
        console.print(
            Text(
                f"Marked flaky in {notes.source}, but not seen flaking here (fixed?): "
                + _few(unseen),
                style="dim",
            )
        )
    if r.gate_jobs:
        console.print(
            f"Not counted: {len(r.gate_jobs)} quick policy-check failure(s) that passed after a PR update.",
            style="dim",
        )
    if not r.recovered_runs:
        console.print(
            "No run in this window failed and then passed on a re-run. Nothing to call flaky."
        )
    console.print(Text(f"{requests_note(r)} · redherring {__version__}", style="dim"))


# --- scan: markdown ---------------------------------------------------------------------


def scan_markdown(r: ScanResult, *, limit: int = 25, notes: Notes | None = None) -> str:
    out = [f"## redherring: {_md_text(r.repo)}", ""]
    out += [f"- {line}" for line in headline(r)]
    out.append("")
    named = [t for t in r.tests if t.times]
    if named:
        out += [
            "### Flaky tests",
            "",
            "Each one failed and then passed on a re-run of the same commit.",
            "",
            "| # | test | times | commits | where | last seen | example |",
            "|--:|---|--:|--:|---|---|---|",
        ]
        for i, t in enumerate(named[:limit], 1):
            ex = t.failures[-1].url
            out.append(
                f"| {i} | {_md_code(t.test_id)} | {t.times} | {t.commits} | {_where(t)} | "
                f"{ago(t.last_seen)} | [job]({ex}) |"
            )
        if len(named) > limit:
            out.append(f"\n…and {len(named) - limit} more.")
        out.append("")
    causes = r.job_causes()
    if causes:
        out += ["### Flaky jobs with no test named", "", "| job | times | cause |", "|---|--:|---|"]
        for name, total, counter in causes[:limit]:
            cause = ", ".join(f"{k} ({v})" if v > 1 else k for k, v in counter.most_common())
            out.append(f"| {_md_code(name)} | {total} | {cause} |")
        out.append("")
    disagree, unseen = notes_check(r, notes)
    if notes and disagree:
        out.append(
            f"⚠️ Marked not flaky in {_md_code(notes.source)}, but failed and then passed on a "
            "re-run here: " + ", ".join(_md_code(x) for x in disagree[:8]) + "\n"
        )
    if notes and unseen:
        out.append(
            f"_Marked flaky in {_md_code(notes.source)}, but not seen flaking here (fixed?): "
            + ", ".join(_md_code(x) for x in unseen[:8])
            + "_\n"
        )
    if r.gate_jobs:
        out.append(
            f"_Not counted: {len(r.gate_jobs)} quick policy-check failures that passed after a PR update._\n"
        )
    if not r.recovered_runs:
        out.append("No run in this window failed and then passed on a re-run.\n")
    out.append(f"<sub>redherring {__version__} · {requests_note(r)}</sub>")
    return "\n".join(out)


# --- scan: json -------------------------------------------------------------------------


def _t(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def scan_json(r: ScanResult, notes: Notes | None = None) -> dict[str, Any]:
    disagree, unseen = notes_check(r, notes)
    return {
        "tool": "redherring",
        "version": __version__,
        "repo": r.repo,
        "since": _t(r.since),
        "until": _t(r.until),
        "runs": {
            "passed": r.runs_passed,
            "failed": r.runs_failed,
            "went_red": r.runs_went_red,
            "recovered_by_rerun": r.herring_runs,
        },
        "runner_seconds_lost": round(r.runner_seconds),
        "red_seconds": {
            "count": len(r.red_seconds),
            "median": round(median(r.red_seconds)) if r.red_seconds else None,
            "p90": round(quantiles(r.red_seconds, n=10)[-1]) if len(r.red_seconds) >= 10 else None,
            "total": round(sum(r.red_seconds)),
        },
        "causes": dict(r.cause_counts()),
        "flaky_tests": [
            {
                "test": t.test_id,
                "framework": t.framework,
                "times": t.times,
                "commits": t.commits,
                "os": t.oses,
                "jobs": t.jobs,
                "last_seen": _t(t.last_seen),
                "retried_in_job": t.in_job_retries,
                "message": t.message,
                "examples": [f.url for f in t.failures[-3:]],
                "marked": _test_mark(notes, t),
            }
            for t in r.tests
        ],
        "flaky_jobs": [
            {
                "job": name,
                "times": total,
                "causes": dict(counter),
                "marked": _job_mark(notes, name),
            }
            for name, total, counter in r.job_causes()
        ],
        "failed_jobs": [
            {
                "run_id": j.run_id,
                "attempt": j.attempt,
                "job": j.job_name,
                "url": j.url,
                "sha": j.head_sha,
                "os": j.os,
                "kind": j.kind,
                "infra": {"category": j.infra.category, "evidence": j.infra.evidence}
                if j.infra
                else None,
                "tests": [t.test_id for t in j.tests],
                "hint": j.hint or None,
                "seconds": round(j.seconds),
            }
            for j in r.failed_jobs
        ],
        "ledger_jobs_used": len(r.history_jobs),
        "notes": {
            "source": notes.source,
            "marked_not_flaky_but_flaked": disagree,
            "marked_flaky_not_seen": unseen,
        }
        if notes
        else None,
        "api_requests": r.api_requests,
        "api_requests_free": r.api_free,
    }


# --- why --------------------------------------------------------------------------------

_BADGE = {
    KNOWN_FLAKY: ("RED HERRING", "green"),
    PROBABLY_FLAKY: ("PROBABLY FLAKY", "yellow"),
    INFRASTRUCTURE: ("RED HERRING", "green"),
    ALREADY_FAILING: ("NOT THIS CHANGE", "green"),
    LOOKS_REAL: ("LOOKS REAL", "red"),
    POLICY_CHECK: ("POLICY CHECK", "yellow"),
    UNEXPLAINED: ("UNCLEAR", "yellow"),
    LOG_EXPIRED: ("UNCLEAR", "yellow"),
}


def summary_sentence(e: Explanation) -> str:
    rec = e.recommendation
    if rec == NOTHING:
        return "Nothing failed in this attempt."
    if rec == RERUN:
        text = (
            "Every failure here is a flake, an infrastructure problem, or already failing on "
            f"{e.main_branch or 'the default branch'}. Re-running is reasonable: "
            f"gh run rerun {e.run['id']} --failed --repo {e.repo}"
        )
        if any(f.verdict == PROBABLY_FLAKY for f in e.findings):
            text += ". If a test fails again, it's probably real: ask redherring again"
        return text
    if rec == INVESTIGATE:
        n = len(e.real)
        return f"{n} failure{'s' if n != 1 else ''} look{'s' if n == 1 else ''} real. Look at {'it' if n == 1 else 'them'} before re-running."
    return "Some failures couldn't be explained from history. Read those logs before re-running."


def print_why(e: Explanation, console: Console) -> None:
    run = e.run
    console.print(
        Text(
            f"redherring why · {e.repo} · {run.get('name')} #{run.get('run_number')} (attempt {e.attempt})",
            style="bold",
        )
    )
    console.print(Text(f"  {run.get('html_url')}", style="dim"))
    if e.history is not None:
        console.print(
            f"  checked against {len(e.history.recovered_runs)} re-run-to-green runs of this workflow "
            f"in the last {e.days} days",
            style="dim",
        )
    if e.notes:
        console.print(Text(f"  and the team's notes in {e.notes}", style="dim"))
    console.print()
    for f in e.findings:
        label, color = _BADGE[f.verdict]
        head = Text()
        head.append(
            f" {label} ",
            style=f"bold white on {color}" if color != "yellow" else "bold black on yellow",
        )
        head.append("  ")
        head.append(f.test_id or f"job: {f.job_name}", style="bold")
        console.print(head)
        if f.test_id:
            console.print(Text(f"    in {f.job_name}", style="dim"))
        console.print(Text(f"    {f.detail}"))
        if f.evidence:
            console.print(Text(f"    log: {f.evidence}", style="dim"))
    console.print()
    color = {RERUN: "green", INVESTIGATE: "red"}.get(e.recommendation, "yellow")
    console.print(Text(summary_sentence(e), style=f"bold {color}"))


def why_markdown(e: Explanation) -> str:
    run = e.run
    title = f"{run.get('name')} #{run.get('run_number')} (attempt {e.attempt})"
    out = [f"### redherring: {_md_text(title)}", ""]
    icon = {RERUN: "✅", INVESTIGATE: "❌"}.get(e.recommendation, "⚠️")
    out += [f"{icon} **{_md_text(summary_sentence(e))}**", ""]
    if e.findings:
        out += ["| verdict | failure | why |", "|---|---|---|"]
        for f in e.findings:
            label, _ = _BADGE[f.verdict]
            what = (
                f"{_md_code(f.test_id)}<br><sub>{_md_code(f.job_name)}</sub>"
                if f.test_id
                else _md_code(f.job_name)
            )
            why = _md_text(f.detail)
            if f.evidence:
                why += f"<br>{_md_code(f.evidence)}"
            out.append(f"| {label} | {what} | {why} ([log]({f.job_url})) |")
        out.append("")
    notes = f" and {_md_code(e.notes)}" if e.notes else ""
    out.append(
        f"<sub>redherring {__version__} · compared with the last {e.days} days of this "
        f"workflow{notes}</sub>"
    )
    return "\n".join(out)


def why_json(e: Explanation) -> dict[str, Any]:
    return {
        "tool": "redherring",
        "version": __version__,
        "repo": e.repo,
        "run_id": e.run.get("id"),
        "attempt": e.attempt,
        "run_url": e.run.get("html_url"),
        "recommendation": e.recommendation,
        "summary": summary_sentence(e),
        "findings": [
            {
                "verdict": f.verdict,
                "red_herring": f.red_herring,
                "test": f.test_id or None,
                "framework": f.framework or None,
                "job": f.job_name,
                "job_url": f.job_url,
                "detail": f.detail,
                "evidence": f.evidence or None,
                "marked": f.mark or None,
                "history": {
                    "times": f.history.times,
                    "commits": f.history.commits,
                    "os": f.history.oses,
                    "examples": [x.url for x in f.history.failures[-3:]],
                }
                if f.history
                else None,
            }
            for f in e.findings
        ],
        "history_days": e.days,
        "notes": e.notes or None,
    }


# --- report -----------------------------------------------------------------------------


def print_draft(d: Draft, console: Console) -> None:
    console.print(Text(f"redherring report · a draft issue for {ISSUES}", style="bold"))
    console.print(Text(f"  title: {d.title}"))
    for key, value in d.fields.items():
        if not value:
            continue
        console.print()
        console.print(Text(f"  {d.labels.get(key, key)}:", style="bold"))
        for line in value.splitlines():
            console.print(Text(f"    {line}", style="dim" if key == "log" else ""))
    console.print()
    console.print(
        Text(
            "Worth sending when the log shows a flaky test or an infrastructure problem (network, "
            "registry, runner…) that redherring missed. A genuine build or lint error is rightly "
            "left unknown.",
            style="dim",
        )
    )
    if d.redactions:
        console.print(
            Text(
                f"Replaced {d.redactions} thing(s) that looked like secrets or personal details "
                "(tokens, passwords, emails, home folders)."
            )
        )
    console.print(
        Text(
            "GitHub already hides your repository's secrets as ***. Read the excerpt anyway: "
            "only you can tell what's private. You can edit everything on GitHub before sending."
        )
    )
    if d.private:
        console.print(
            Text(
                "This is a private repository: its name and links are left out, but the excerpt "
                "comes from its logs.",
                style="yellow",
            )
        )


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)
