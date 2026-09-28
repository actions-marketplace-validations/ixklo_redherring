"""Recognise failures caused by the CI environment rather than by the code under test.

Only consulted when no test failure was found in the log: a test that failed because the
network blipped is still reported as that test (with the network hint), because the test
is what a developer can quarantine or harden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InfraCause:
    category: str
    evidence: str


# Ordered: the first category whose pattern appears wins. Patterns are deliberately
# specific; a vague match ("error") would turn real failures into excuses.
_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    (
        "runner lost",
        re.compile(
            r"The runner has received a shutdown signal|lost communication with the server"
            r"|The hosted runner (?:lost communication|encountered an error)"
            r"|The job was not acquired by Runner|Runner \S+ did not respond"
        ),
    ),
    (
        "job timeout",
        re.compile(
            r"has exceeded the maximum execution time of|The job has exceeded the maximum execution"
            r"|Some tasks were terminated on timeout"
        ),
    ),
    (
        "out of memory",
        re.compile(
            r"JavaScript heap out of memory|fatal error: runtime: out of memory|Cannot allocate memory"
            r"|OOMKilled|java\.lang\.OutOfMemoryError|exit code 137\b|^Killed\s*$|^MemoryError\b"
        ),
    ),
    ("disk full", re.compile(r"No space left on device|\bENOSPC\b")),
    (
        "runner environment",
        re.compile(
            # 0xC0000142 STATUS_DLL_INIT_FAILED: Windows couldn't start the process at all.
            r"exited \(-1073741502\)|exit code -1073741502|0xC0000142|3221225794"
            r"|bad interpreter: Text file busy"
        ),
    ),
    (
        "test worker crash",
        re.compile(
            r"Worker exited unexpectedly|Worker forks emitted error|Jest worker encountered \d+ child process exceptions"
            r"|A worker process has failed to exit gracefully"
        ),
    ),
    (
        "rate limited",
        re.compile(
            r"API rate limit exceeded|secondary rate limit|\b429 Too Many Requests|toomanyrequests"
            r"|You have reached your pull rate limit|status code: 429"
        ),
    ),
    (
        "AI provider",
        re.compile(
            r"model is at capacity|overloaded_error|\"type\":\s*\"overloaded\"|rate_limit_error"
            r"|insufficient_quota|The server had an error while processing your request"
            r"|(?:Anthropic|OpenAI|Gemini) API (?:error|is (?:down|overloaded))|Error code: 529"
        ),
    ),
    (
        "package registry",
        re.compile(
            r"Could not transfer artifact|Could not resolve dependencies for project"
            r"|npm (?:ERR!|error) (?:code )?E(?:TIMEDOUT|CONNRESET|AI_AGAIN|NOTFOUND|CONNREFUSED)"
            r"|npm (?:ERR!|error) network|ERR_PNPM_META_FETCH_FAIL|ERR_PNPM_FETCH_\d+"
            r"|YN0001: .*(?:ECONNRESET|ETIMEDOUT|socket hang up)|error An unexpected error occurred: \"https?://"
            r"|Failed to download (?:crate|file|package)|failed to (?:download|get) `\S+`"
            r"|HTTP error [45]\d\d while getting|pip\._vendor\.urllib3\.exceptions"
            r"|Hash Sum mismatch|Failed to fetch https?://|unable to select packages"
            r"|Temporary failure resolving|failed to resolve source metadata"
            r"|Error response from daemon: (?:Get|Head|pull)|net/http: TLS handshake timeout"
            r"|CondaHTTPError|HTTP \d{3} (?:Forbidden|Too Many Requests|Service Unavailable) for url"
            r"|could not download file from|error downloading file|failed to fetch anonymous token"
        ),
    ),
    (
        "network",
        re.compile(
            r"\bECONNRESET\b|\bETIMEDOUT\b|\bECONNREFUSED\b|\bEAI_AGAIN\b|socket hang up"
            r"|Connection reset by peer|Could not resolve host|Temporary failure in name resolution"
            r"|Name or service not known|Network is unreachable|SSLError|SSL: UNEXPECTED_EOF"
            r"|RemoteDisconnected|Read timed out|ReadTimeoutError|i/o timeout|connection timed out"
            r"|getaddrinfo (?:ENOTFOUND|EAI_AGAIN)|502 Bad Gateway|503 Service Unavailable|504 Gateway Time"
            r"|rpc error: code = Unavailable|keepalive ping failed|unexpected EOF while reading"
            r"|fatal: unable to access 'https?://[^']*': (?!The requested URL returned error: 4)"
            r"|\boperation timed out\b|Unexpected HTTP response: 5\d\d|Canceled because of SSL destruction"
            r"|The requested URL returned error: 5\d\d"
            r"|stream error: stream ID \d+; INTERNAL_ERROR|proxy\.golang\.org.*(?:EOF|reset|timeout)"
            r"|'git', 'clone'.*returned non-zero exit status 128|RPC failed; curl|early EOF"
        ),
    ),
    (
        "service startup",
        re.compile(
            r"docker', 'compose'.*returned non-zero|docker compose .*(?:up|start).*(?:failed|exit status)"
            r"|dependency failed to start|container \S+ is unhealthy|Service container \S+ failed"
            r"|Failed to initialize container|failed to connect to the docker API"
            r"|Cannot connect to the Docker daemon|error during connect: .*docker"
        ),
    ),
    (
        "GitHub service",
        re.compile(
            r"Failed to (?:save|restore) cache|Cache service responded with [45]\d\d|Unable to reserve cache"
            r"|Failed to (?:Create|Finalize)Artifact|Artifact upload failed|Unable to (?:download|upload) artifact"
            r"|The operation was canceled\.\s*$|Internal Server Error.*api\.github\.com"
            r"|HTTP 5\d\d \(https?://api\.github\.com"
        ),
    ),
]


_GENERIC_ERROR = re.compile(
    r"Process completed with exit code \d+\.?$|The process '.*' failed with exit code \d+$"
)
# Lines that say nothing about the cause: exit-code echoes and the like.
_NOT_A_HINT = re.compile(
    r"^\[?ELIFECYCLE\]? |^error Command failed with exit code|^npm (?:ERR!|error) (?:code|errno|path|command|A complete log)"
    r"|^DEBUG Command exited with code|^Error: Process completed with exit code|^make(?:\[\d+\])?: \*\*\*"
    r"|^warning: build failed, waiting for other jobs|^note: run with `RUST_BACKTRACE|^shell: |^env:$"
    r"|^\+ set [+-]x|^Duration\b|HOW TO REPRODUCE"
)
_LEADING_ERROR = re.compile(r"^(?:error|fatal|panic|FATAL|ERROR|Error)(?:\[[^\]]*\])?[:!]")
_ERRORISH = re.compile(
    r"\b(?:error|errors|failed|failure|fatal|panic|panicked|exception|could not|cannot|unable to"
    r"|denied|refused|timed out|terminated|exited|crash(?:ed)?)\b",
    re.I,
)


def last_output(lines: list[str]) -> str:
    """The last real line of output before the step failed: the best hint when nothing matched."""
    for i, line in enumerate(lines):
        if line.startswith("##[error]"):
            own = line.removeprefix("##[error]").strip()
            if own and not _GENERIC_ERROR.match(own):
                return own[:240]
            window = [
                text
                for prev in lines[max(0, i - 40) : i]
                if (text := prev.strip())
                and not text.startswith(("##[", "[command]"))
                and re.search(r"[A-Za-z]{3}", text)
                and not _NOT_A_HINT.search(text)
            ]
            leading = [t for t in window if _LEADING_ERROR.match(t)]
            if leading:
                return leading[-1][:240]
            errorish = [t for t in window if _ERRORISH.search(t)]
            if errorish:
                return errorish[-1][:240]
            if window:
                return window[-1][:240]
            return line.removeprefix("##[error]").strip()[:240]
    return ""


def classify(lines: list[str]) -> InfraCause | None:
    """The environment problem that most plausibly broke this job, if any.

    Scans from the end: the cause of a failed job is almost always near the bottom, and an
    early retried-and-recovered network warning shouldn't outrank it.
    """
    window = lines[-600:] if len(lines) > 600 else lines
    for category, pattern in _CATEGORIES:
        for line in reversed(window):
            if pattern.search(line):
                return InfraCause(category, line.strip()[:240])
    return None
