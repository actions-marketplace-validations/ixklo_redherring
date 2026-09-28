"""Turn a raw GitHub Actions job log into plain lines.

Actions prefixes every line with an ISO timestamp, and most test runners colour their
output with ANSI escapes. Parsers see neither.
"""

from __future__ import annotations

import re

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def clean_lines(text: str) -> list[str]:
    """Strip BOM, per-line timestamps, ANSI escapes and carriage returns."""
    if text.startswith("﻿"):
        text = text[1:]
    out = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        line = _TIMESTAMP.sub("", line, count=1)
        if "\x1b" in line:
            line = _ANSI.sub("", line)
        # Progress bars overwrite themselves with \r; keep only the final state.
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        out.append(line)
    return out


def tail_excerpt(lines: list[str], index: int, before: int = 2, after: int = 6) -> str:
    """A short excerpt around a line, for evidence in reports."""
    lo = max(0, index - before)
    hi = min(len(lines), index + after + 1)
    return "\n".join(line.rstrip() for line in lines[lo:hi]).strip()
