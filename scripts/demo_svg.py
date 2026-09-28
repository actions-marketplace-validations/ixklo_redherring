"""Render README demo images from real output.

    uv run python scripts/demo_svg.py OWNER/REPO DAYS RUN_URL

Needs a GitHub token (GH_TOKEN or `gh auth login`). Writes docs/demo-scan.svg and docs/demo-why.svg.
"""

import sys
from pathlib import Path

from rich.console import Console

from redherring.cache import Cache
from redherring.github import GitHub, resolve_token
from redherring.render import print_scan, print_why
from redherring.scan import Scanner
from redherring.why import explain, parse_target

DOCS = Path(__file__).resolve().parent.parent / "docs"


def main(repo: str, days: int, run_url: str) -> None:
    DOCS.mkdir(exist_ok=True)
    scanner = Scanner(GitHub(resolve_token()), Cache())

    con = Console(record=True, width=104, force_terminal=True, color_system="truecolor")
    con.print(f"[bold green]$[/] redherring scan {repo} --days {days}")
    print_scan(scanner.scan(repo, days=days), con, limit=8)
    con.save_svg(str(DOCS / "demo-scan.svg"), title=f"redherring scan {repo}")

    target_repo, run_id, attempt = parse_target(run_url, None)
    con = Console(record=True, width=104, force_terminal=True, color_system="truecolor")
    con.print(f"[bold green]$[/] redherring why {run_url}")
    print_why(explain(scanner, target_repo, run_id, attempt=attempt, days=days), con)
    con.save_svg(str(DOCS / "demo-why.svg"), title="redherring why")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3])
