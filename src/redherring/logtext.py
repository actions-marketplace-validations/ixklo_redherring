"""Turn a raw GitHub Actions job log into plain lines.

Actions prefixes every line with an ISO timestamp, and most test runners colour their
output with ANSI escapes. Parsers see neither. Colour codes that reached the log as text
are removed too: a test quoting a coloured string ('\\x1b[1;33mWARN'), a script echoing
"\\033[0m" without -e, or "[31mFAILED[0m" whose escape byte got lost on the way.
"""

from __future__ import annotations

import re

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
# CSI (colours, cursor moves), OSC (titles, links), charset selection like the ESC ( B
# that `tput sgr0` writes, and two-byte ones like ESC 7. A lone ESC goes too.
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[ -/]+[0-~]"
    r"|\x1b[0-~]?"
)
# ESC spelled out: \x1b \033 \u001b \u{1b} \e (any number of backslashes), ^[ and the
# symbols terminals print for it (␛ ←). Then a colour or erase code, or a numbered move,
# so shell patterns like ^[[:space:]] or ^[[a-z] stay.
_SPELLED_ANSI = re.compile(
    r"(?:\\+(?:x1[bB]|0?33|u001[bB]|u\{1[bB]\}|e)|\^\[|[␛←])"
    r"\[(?:[0-9;]*[mK]|\d[0-9;]*[A-Za-z])"
)
# Codes whose ESC was lost. Only removed from a line that also has a reset code, and never
# right before "]", so test ids like test_timeout[5m] survive.
_BARE_SGR = re.compile(r"\[[0-9;]*m(?!\])")
_BARE_RESET = re.compile(r"\[(?:0|22|39|49)m(?!\])")


def strip_color(line: str) -> str:
    """Remove colour codes from one line: real escapes, spelled-out ones and leftovers."""
    if "\x1b" in line:
        line = _ANSI.sub("", line)
    if "[" in line:
        if "\\" in line or "^[" in line or "␛" in line or "←" in line:
            line = _SPELLED_ANSI.sub("", line)
        has_reset = "[0m" in line or "[39m" in line or "[22m" in line or "[49m" in line
        if has_reset and _BARE_RESET.search(line):
            line = _BARE_SGR.sub("", line)
    return line


def clean_lines(text: str) -> list[str]:
    """Strip BOM, per-line timestamps, colour codes and carriage returns."""
    if text.startswith("﻿"):
        text = text[1:]
    out = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        line = _TIMESTAMP.sub("", line, count=1)
        # Progress bars overwrite themselves with \r; keep only the final state.
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        out.append(strip_color(line))
    return out
