"""Command line: `redherring scan`, `redherring why` and `redherring report`."""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm
from rich.text import Text

from . import __version__, ledger, report
from . import notes as team_notes
from .cache import Cache
from .comment import post_or_update
from .github import GitHub, GitHubError, resolve_token
from .render import (
    dumps,
    print_draft,
    print_scan,
    print_why,
    scan_json,
    scan_markdown,
    why_json,
    why_markdown,
)
from .scan import Scanner
from .why import INVESTIGATE, NOTHING, RERUN, explain, parse_target

EXIT_OK = 0
EXIT_REAL = 1
EXIT_ERROR = 2
EXIT_UNCLEAR = 3

_REPO = re.compile(r"^(?:https?://github\.com/|git@github\.com:)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def normalize_repo(value: str) -> str:
    m = _REPO.match(value.strip())
    if not m:
        raise ValueError(f"not a GitHub repo: {value!r} (expected OWNER/REPO)")
    return m[1]


def repo_from_git() -> str | None:
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return normalize_repo(out.stdout.strip())
    except ValueError:
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redherring",
        description="Find the CI failures that weren't your fault, from the GitHub Actions history you already have.",
    )
    p.add_argument("--version", action="version", version=f"redherring {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="list a repo's flaky tests and flaky jobs")
    s.add_argument("repo", nargs="?", help="OWNER/REPO (default: this checkout's origin)")
    s.add_argument("--days", type=int, default=30, help="how far back to look (default 30)")
    s.add_argument("--workflow", help="only this workflow (name or file, e.g. ci.yml)")
    s.add_argument("--branch", help="only runs on this branch")
    s.add_argument("--limit", type=int, default=15, help="rows per table (default 15)")
    s.add_argument(
        "--ledger",
        help="JSON file to merge this scan's evidence into (created if missing); keeps flake history "
        "after GitHub deletes old runs",
    )
    s.add_argument(
        "--keep-days",
        type=int,
        default=365,
        help="drop ledger entries older than this (default 365)",
    )
    _notes_args(s)
    _common(s)

    w = sub.add_parser("why", help="explain a failed run: red herrings vs failures that look real")
    w.add_argument("run", help="run URL, or run id together with --repo")
    w.add_argument("--repo", help="OWNER/REPO when giving a bare run id")
    w.add_argument("--days", type=int, default=30, help="history to compare against (default 30)")
    w.add_argument(
        "--no-main-check", action="store_true", help="don't look at the default branch's latest run"
    )
    w.add_argument("--ledger", help="a ledger written by `scan --ledger`, used as extra history")
    w.add_argument(
        "--comment",
        action="store_true",
        help="post (or update) the explanation as a comment on the run's open pull request",
    )
    _notes_args(w)
    _common(w)

    r = sub.add_parser(
        "report",
        help="draft a GitHub issue about a failure redherring couldn't read (you review and send it)",
        description="Draft an issue for redherring's tracker from a failed job: an excerpt of its "
        "log (with likely secrets replaced) and what redherring made of it. Nothing is sent: you "
        "get a link to GitHub's new-issue page with the draft filled in, and you decide there.",
    )
    r.add_argument("run", help="job URL, run URL, or run id together with --repo")
    r.add_argument("--repo", help="OWNER/REPO when giving a bare run id")
    r.add_argument("--job", help="which failed job, by id or name (when the run has several)")
    r.add_argument(
        "--wrong",
        action="store_true",
        help="report a wrong verdict instead of an unrecognised log",
    )
    r.add_argument(
        "--lines", type=int, default=40, help="log lines to include (default 40, max 200)"
    )
    r.add_argument("--days", type=int, default=30, help="history for --wrong (default 30)")
    r.add_argument(
        "--no-main-check", action="store_true", help="with --wrong: skip the default branch check"
    )
    r.add_argument(
        "--open", action="store_true", help="open the draft in your browser without asking first"
    )
    _notes_args(r)
    _common(r)
    return p


def _notes_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--notes",
        help=f"team notes file to use instead of {team_notes.PATH} on the default branch",
    )
    p.add_argument("--no-notes", action="store_true", help="ignore the team's notes")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=("text", "md", "json"), default="text")
    p.add_argument("--json-out", help="also write the JSON result to this file")
    p.add_argument("--cache", help="cache file (default: your user cache folder)")
    p.add_argument("--quiet", "-q", action="store_true", help="no progress messages")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    out = Console(highlight=False)
    err = Console(stderr=True, highlight=False)

    token = resolve_token()
    if not token:
        err.print(
            "[red]redherring needs a GitHub token to read Actions logs.[/red] "
            "Set GH_TOKEN (any token works for public repos), or install the GitHub CLI and run `gh auth login`."
        )
        return EXIT_ERROR

    status = None
    show_progress = not args.quiet and err.is_terminal

    def progress(msg: str) -> None:
        if status is not None:
            status.update(f"[dim]{msg}[/dim]")

    def on_wait(reason: str, seconds: float) -> None:
        err.print(f"[yellow]{reason}; waiting {seconds:.0f}s[/yellow]")

    cache = Cache(args.cache)
    gh = GitHub(token, on_wait=on_wait, http_cache=cache)
    scanner = Scanner(gh, cache, progress=progress)

    def stop() -> None:
        if status is not None:
            status.stop()

    try:
        if show_progress:
            status = err.status("[dim]starting[/dim]")
            status.start()
        if args.command == "scan":
            return _scan(args, gh, scanner, out, err, stop)
        if args.command == "report":
            return _report(args, scanner, out, err, stop)
        return _why(args, scanner, out, err, stop)
    except (GitHubError, ValueError) as e:
        if status is not None:
            status.stop()
        # Messages can quote job and test names: print them as data, not rich markup.
        err.print(Text.assemble(("error: ", "red"), str(e)))
        return EXIT_ERROR
    except KeyboardInterrupt:
        if status is not None:
            status.stop()
        return 130
    finally:
        stop()
        gh.close()
        with contextlib.suppress(Exception):
            cache.prune()
        cache.close()


def load_notes(args, gh: GitHub, repo: str, err: Console) -> team_notes.Notes | None:
    """The team's notes: --notes FILE, else the file on the default branch (so a pull request
    can't change its own verdicts), else none."""
    if args.no_notes:
        return None
    if args.notes:
        path = Path(args.notes)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ValueError(f"can't read the notes file {path}: {e.strerror}") from None
        return team_notes.parse(text, str(path))
    try:
        found = gh.file_text(repo, team_notes.PATH)
    except GitHubError as e:
        err.print(
            Text(f"couldn't read {team_notes.PATH} ({e}); going on without team notes", "yellow")
        )
        return None
    return team_notes.parse(found, team_notes.PATH) if found is not None else None


def _write_json_out(args, data: dict) -> None:
    if args.json_out:
        Path(args.json_out).write_text(dumps(data) + "\n", encoding="utf-8")


def _scan(
    args, gh: GitHub, scanner: Scanner, out: Console, err: Console, stop: Callable[[], None]
) -> int:
    repo = normalize_repo(args.repo) if args.repo else repo_from_git()
    if not repo:
        raise ValueError(
            "which repo? Pass OWNER/REPO, or run inside a checkout with a GitHub origin."
        )
    if args.days < 1:
        raise ValueError("--days must be at least 1")
    notes = load_notes(args, gh, repo, err)
    workflow_id = _resolve_workflow(gh, repo, args.workflow) if args.workflow else None
    ledger_path = Path(args.ledger) if args.ledger else None
    prior = ledger.load(ledger_path, repo) if ledger_path else []
    result = scanner.scan(
        repo, days=args.days, workflow_id=workflow_id, branch=args.branch, prior=prior
    )
    if ledger_path:
        merged = ledger.merge(prior, result.failed_jobs, keep_days=args.keep_days)
        ledger.save(ledger_path, repo, merged)
    stop()
    _write_json_out(args, scan_json(result, notes))
    if args.format == "json":
        print(dumps(scan_json(result, notes)))
    elif args.format == "md":
        print(scan_markdown(result, limit=args.limit, notes=notes))
    else:
        print_scan(result, out, limit=args.limit, notes=notes)
    return EXIT_OK


def _why(args, scanner: Scanner, out: Console, err: Console, stop: Callable[[], None]) -> int:
    repo_arg = normalize_repo(args.repo) if args.repo else repo_from_git()
    repo, run_id, attempt = parse_target(args.run, repo_arg)
    prior = ledger.load(Path(args.ledger), repo) if args.ledger else []
    e = explain(
        scanner,
        repo,
        run_id,
        attempt=attempt,
        days=args.days,
        check_main=not args.no_main_check,
        prior=prior,
        notes=load_notes(args, scanner.gh, repo, err),
    )
    stop()
    _write_json_out(args, why_json(e))
    if args.format == "json":
        print(dumps(why_json(e)))
    elif args.format == "md":
        print(why_markdown(e))
    else:
        print_why(e, out)
    if args.comment:
        for url in post_or_update(scanner.gh, e):
            err.print(f"[dim]commented: {url}[/dim]")
    rec = e.recommendation
    if rec in (RERUN, NOTHING):
        return EXIT_OK
    return EXIT_REAL if rec == INVESTIGATE else EXIT_UNCLEAR


def _report(args, scanner: Scanner, out: Console, err: Console, stop: Callable[[], None]) -> int:
    repo_arg = normalize_repo(args.repo) if args.repo else repo_from_git()
    notes = None
    if args.wrong:
        repo, _, _ = parse_target(args.run, repo_arg)
        notes = load_notes(args, scanner.gh, repo, err)
    try:
        draft = report.build(
            scanner,
            args.run,
            repo_arg,
            job=args.job,
            wrong=args.wrong,
            lines=args.lines,
            days=args.days,
            check_main=not args.no_main_check,
            notes=notes,
        )
    except report.NothingToReport as e:
        stop()
        out.print(Text(str(e)))
        return EXIT_OK
    stop()
    _write_json_out(args, draft.to_json())
    if args.format == "json":
        print(dumps(draft.to_json()))
        return EXIT_OK
    if args.format == "md":
        print(draft.markdown())
        return EXIT_OK
    print_draft(draft, out)
    url = draft.url()
    out.print()
    interactive = sys.stdin.isatty() and out.is_terminal
    wanted = args.open or (
        interactive
        and Confirm.ask(
            "Open it on GitHub? Nothing is sent until you press Submit there", default=False
        )
    )
    if wanted and webbrowser.open(url):
        out.print(Text("Opened in your browser. Nothing is sent until you press Submit."))
    else:
        out.print(Text("To send it, open this link, read it, and press Submit on GitHub:"))
        print(url)
    return EXIT_OK


def _resolve_workflow(gh: GitHub, repo: str, name: str) -> int:
    wanted = name.lower()
    flows = gh.workflows(repo)
    for w in flows:
        if w["name"].lower() == wanted or w["path"].lower().endswith("/" + wanted):
            return w["id"]
    for w in flows:
        if wanted in w["name"].lower() or wanted in w["path"].lower():
            return w["id"]
    all_names = sorted(w["name"] for w in flows if not w["name"].startswith(".github/"))
    names = ", ".join(all_names[:15]) or "none"
    if len(all_names) > 15:
        names += f", … ({len(all_names) - 15} more)"
    raise ValueError(f"no workflow matching {name!r} in {repo}. Workflows: {names}")


if __name__ == "__main__":
    sys.exit(main())
