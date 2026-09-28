"""Name the failing tests in a CI log, whatever produced it.

Every parser reads the plain lines from ``logtext.clean_lines`` and returns the tests it
can identify. They never guess: a line that doesn't match a runner's real output format
is ignored, and a log with no recognisable failures returns an empty list (the caller
then looks for infrastructure causes instead).

Test ids are built from stable parts only (file, suite, name) so the same test lines up
across commits, matrix jobs and operating systems. Line numbers and timings are dropped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

FAILED = "failed"
# Failed at least once but passed on a retry inside the same job (the runner's own retry).
FLAKY = "flaky"


@dataclass(frozen=True)
class TestFailure:
    framework: str
    test_id: str
    outcome: str = FAILED
    message: str = ""


def _norm_path(p: str) -> str:
    return p.replace("\\", "/").removeprefix("./")


def _short(msg: str | None, limit: int = 240) -> str:
    msg = (msg or "").strip()
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


# --- Python -----------------------------------------------------------------------------

_PYTEST = re.compile(r"^(?:\[gw\d+\] \[\s*\d+%\] )?(FAILED|ERROR) (\S+?\.py::\S.*?)(?: - (.*))?$")
_PYTEST_COLLECT = re.compile(r"^ERROR (\S+\.py)(?: - (.*))?$")
_PYTEST_RERUN = re.compile(r"^(\S+?\.py::\S+) RERUN\b")
_UNITTEST = re.compile(r"^(FAIL|ERROR): (\w+) \(([\w.]+)\)")


def parse_pytest(lines: list[str]) -> list[TestFailure]:
    out = []
    for line in lines:
        if "FAILED" not in line and "ERROR" not in line and "RERUN" not in line:
            continue
        s = line.strip()
        if m := _PYTEST.match(s):
            out.append(TestFailure("pytest", _norm_path(m[2].strip()), FAILED, _short(m[3])))
        elif m := _PYTEST_COLLECT.match(s):
            out.append(TestFailure("pytest", _norm_path(m[1]), FAILED, _short(m[2])))
        elif m := _PYTEST_RERUN.match(s):
            out.append(TestFailure("pytest", _norm_path(m[1]), FLAKY))
    return out


def parse_unittest(lines: list[str]) -> list[TestFailure]:
    out = []
    for line in lines:
        if not (line.startswith("FAIL: ") or line.startswith("ERROR: ")):
            continue
        if m := _UNITTEST.match(line):
            name, where = m[2], m[3]
            test_id = where if where.endswith("." + name) else f"{where}.{name}"
            out.append(TestFailure("unittest", test_id))
    return out


# --- JavaScript / TypeScript ------------------------------------------------------------

_JS_FILE = r"\S+\.(?:[cm]?[jt]sx?|vue|svelte)"
_JEST_FILE = re.compile(rf"^\s*FAIL\s+(?:.*?\s)?({_JS_FILE})(?:\s+\([^)]*\))?\s*$")
_JEST_TITLE = re.compile(r"^\s*● (.+?)\s*$")
_JEST_NOT_TESTS = ("Console", "Test suite failed to run", "process.exit called")
_VITEST = re.compile(
    rf"^\s*(?:×|✗|❯)?\s*FAIL\s+(?:\|[^|]+\|\s+)?({_JS_FILE}) > (.+?)(?:\s+\[[^\]]+\])?(?:\s+\d+(?:\.\d+)?m?s)?\s*$"
)
_BUN = re.compile(r"^\(fail\) (.+?)(?: \[[\d.]+m?s\])?\s*$")
_NODE_SPEC = re.compile(r"^\s*✖ (.+?) \([\d.]+m?s\)\s*$")
_TAP = re.compile(r"^\s*not ok \d+(?: -)? (.+?)\s*$")
_TAP_DIRECTIVE = re.compile(r"#\s*(?:SKIP|TODO)\b", re.I)
_TESTEM_PREFIX = re.compile(r"^[A-Z][\w ]*? [\d.]+ - \[[^\]]*\] - ")
_MOCHA_FAILING = re.compile(r"^\s+\d+ failing\s*$")
_MOCHA_ITEM = re.compile(r"^(\s+)\d+\) (.+?)\s*$")


def parse_jest(lines: list[str]) -> list[TestFailure]:
    out = []
    current_file = ""
    for line in lines:
        if "FAIL" in line and " > " not in line and (m := _JEST_FILE.match(line)):
            current_file = _norm_path(m[1])
            continue
        if "●" in line and (m := _JEST_TITLE.match(line)):
            title = m[1]
            if title.startswith(_JEST_NOT_TESTS):
                if title.startswith("Test suite failed to run") and current_file:
                    out.append(
                        TestFailure("jest", current_file, FAILED, "Test suite failed to run")
                    )
                continue
            test_id = f"{current_file} › {title}" if current_file else title
            out.append(TestFailure("jest", test_id))
    return out


def parse_vitest(lines: list[str]) -> list[TestFailure]:
    out = []
    for line in lines:
        if "FAIL" in line and " > " in line and (m := _VITEST.match(line)):
            out.append(TestFailure("vitest", f"{_norm_path(m[1])} > {m[2]}"))
    return out


def parse_bun(lines: list[str]) -> list[TestFailure]:
    return [
        TestFailure("bun", m[1])
        for line in lines
        if line.startswith("(fail) ") and (m := _BUN.match(line))
    ]


def parse_node_spec(lines: list[str]) -> list[TestFailure]:
    return [
        TestFailure("node:test", m[1])
        for line in lines
        if "✖" in line and (m := _NODE_SPEC.match(line))
    ]


def parse_tap(lines: list[str]) -> list[TestFailure]:
    out = []
    for line in lines:
        if "not ok" not in line:
            continue
        m = _TAP.match(line)
        if not m or _TAP_DIRECTIVE.search(m[1]):
            continue
        title = _TESTEM_PREFIX.sub("", m[1])
        out.append(TestFailure("tap", title))
    return out


def parse_mocha(lines: list[str]) -> list[TestFailure]:
    """Mocha's spec reporter lists failures after an "N failing" line, titles nested by indent."""
    out = []
    in_failures = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _MOCHA_FAILING.match(line):
            in_failures = True
        elif in_failures and (m := _MOCHA_ITEM.match(line)):
            indent = len(m[1])
            parts = [m[2]]
            j = i + 1
            while not parts[-1].endswith(":") and j < len(lines):
                nxt = lines[j]
                if not nxt.strip() or len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                parts.append(nxt.strip())
                j += 1
            if parts[-1].endswith(":"):
                parts[-1] = parts[-1][:-1]
                out.append(TestFailure("mocha", " › ".join(parts)))
            i = j
            continue
        i += 1
    return out


_PW_HEADER = re.compile(r"^\s*\d+\) \[([^\]]+)\] › (\S+?):\d+:\d+ › (.+?)(?:\s+─+)?\s*$")
_PW_SUMMARY_COUNT = re.compile(r"^\s+\d+ (failed|flaky|passed|skipped|did not run|interrupted)\b")
_PW_SUMMARY_ITEM = re.compile(r"^\s+\[([^\]]+)\] › (\S+?):\d+:\d+ › (.+?)\s*$")
_PW_RETRY = re.compile(r"\s+\(retry #\d+\)$")


def parse_playwright(lines: list[str]) -> list[TestFailure]:
    out = []
    section = None
    for line in lines:
        if not line.strip():
            section = None
            continue
        if m := _PW_SUMMARY_COUNT.match(line):
            section = m[1]
            continue
        if "›" not in line:
            continue
        if section in ("failed", "flaky") and (m := _PW_SUMMARY_ITEM.match(line)):
            title = _PW_RETRY.sub("", m[3])
            outcome = FLAKY if section == "flaky" else FAILED
            out.append(TestFailure("playwright", f"{_norm_path(m[2])} › {title}", outcome))
            continue
        if m := _PW_HEADER.match(line):
            title = _PW_RETRY.sub("", m[3])
            out.append(TestFailure("playwright", f"{_norm_path(m[2])} › {title}"))
    # A test in the "flaky" summary also has a numbered failure header; the summary wins.
    flaky = {f.test_id for f in out if f.outcome == FLAKY}
    return [f for f in out if f.outcome == FLAKY or f.test_id not in flaky]


# --- Go ---------------------------------------------------------------------------------

_GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+) \(")
_GO_PKG = re.compile(r"^FAIL\s+(\S+)\s+(?:[\d.]+s|\[[^\]]+\])\s*$")
_GOTESTSUM = re.compile(r"^=== FAIL: (\S+) (\S+)")
_GO_RUNNING = re.compile(r"^\s+(Test\S+) \([^)]*\)\s*$")


def parse_go(lines: list[str]) -> list[TestFailure]:
    out: list[TestFailure] = []
    pending: list[tuple[str, str]] = []
    in_timeout = False
    for line in lines:
        if "FAIL" not in line and "panic: test timed out" not in line and not in_timeout:
            continue
        if line.startswith("panic: test timed out"):
            in_timeout = True
            continue
        if in_timeout:
            if m := _GO_RUNNING.match(line):
                pending.append((m[1], "test timed out"))
                continue
            if line.strip() and not line.strip().startswith("running tests"):
                in_timeout = False
        if m := _GO_FAIL.match(line):
            pending.append((m[1], ""))
        elif m := _GOTESTSUM.match(line):
            out.append(TestFailure("go", f"{m[1]}.{m[2]}"))
        elif m := _GO_PKG.match(line):
            out.extend(_go_leaves(m[1], pending))
            pending = []
    out.extend(_go_leaves("", pending))
    return out


def _go_leaves(pkg: str, names: list[tuple[str, str]]) -> list[TestFailure]:
    all_names = {n for n, _ in names}
    leaves = [(n, msg) for n, msg in names if not any(o.startswith(n + "/") for o in all_names)]
    return [TestFailure("go", f"{pkg}.{n}" if pkg else n, FAILED, msg) for n, msg in leaves]


# --- Rust -------------------------------------------------------------------------------

_LIBTEST = re.compile(r"^test (\S+) \.\.\. FAILED\s*$")
_NEXTEST = re.compile(
    r"^\s*(FAIL|TIMEOUT|SIGSEGV|SIGABRT|SIGKILL|ABORT|FLAKY(?: \d+/\d+)?)"
    r"(?: \[\s*[\d.]+s\])?(?: \(\s*\d+/\d+\))? (\S+) (\S+)\s*$"
)


def parse_rust(lines: list[str]) -> list[TestFailure]:
    nextest: list[TestFailure] = []
    libtest: list[TestFailure] = []
    for line in lines:
        if (
            "FAIL" in line
            or "FLAKY" in line
            or "TIMEOUT" in line
            or "SIG" in line
            or "ABORT" in line
        ):
            if m := _NEXTEST.match(line):
                status = m[1]
                outcome = FLAKY if status.startswith("FLAKY") else FAILED
                msg = "timed out" if status == "TIMEOUT" else ""
                nextest.append(TestFailure("cargo-nextest", f"{m[2]} {m[3]}", outcome, msg))
            elif m := _LIBTEST.match(line):
                libtest.append(TestFailure("cargo-test", m[1]))
    # nextest echoes libtest's own "test x ... FAILED" line in captured output.
    covered = {f.test_id.split(" ", 1)[1] for f in nextest}
    return nextest + [f for f in libtest if f.test_id not in covered]


# --- Ruby -------------------------------------------------------------------------------

_RSPEC = re.compile(r"^rspec '?(\S+?)(?::\d+|\[[\d:]+\])'? # (.+?)\s*$")
_MINITEST = re.compile(r"^\s*\d+\) (Failure|Error):\s*$")
_MINITEST_NAME = re.compile(r"^([\w:]+)#(\w+)")


def parse_rspec(lines: list[str]) -> list[TestFailure]:
    return [
        TestFailure("rspec", f"{_norm_path(m[1])} # {m[2]}")
        for line in lines
        if line.startswith("rspec ") and (m := _RSPEC.match(line))
    ]


def parse_minitest(lines: list[str]) -> list[TestFailure]:
    out = []
    for i, line in enumerate(lines[:-1]):
        if (
            (") Failure:" in line or ") Error:" in line)
            and _MINITEST.match(line)
            and (m := _MINITEST_NAME.match(lines[i + 1].strip()))
        ):
            out.append(TestFailure("minitest", f"{m[1]}#{m[2]}"))
    return out


# --- JVM --------------------------------------------------------------------------------

_SUREFIRE_NEW = re.compile(
    r"^\[ERROR\] ([\w.$]+)\.([\w$]+)(?:\[[^\]]*\])?(?:\(\))?\s+-- Time elapsed: .*<<< (?:FAILURE|ERROR)!"
)
_SUREFIRE_OLD = re.compile(
    r"^\[ERROR\] ([\w$]+)(?:\[[^\]]*\])?\(([\w.$]+)\)\s+Time elapsed: .*<<< (?:FAILURE|ERROR)!"
)
_SUREFIRE_CLASS = re.compile(r"^\[ERROR\] Tests run: .*<<< (?:FAILURE|ERROR)! -+ in ([\w.$]+)")
_GRADLE = re.compile(r"^([A-Za-z_][\w.$]*) > (.+?) FAILED\s*$")


def parse_junit(lines: list[str]) -> list[TestFailure]:
    methods: list[TestFailure] = []
    classes: list[str] = []
    for line in lines:
        if "<<<" in line and line.startswith("[ERROR]"):
            if m := _SUREFIRE_NEW.match(line):
                methods.append(TestFailure("junit", f"{m[1]}.{m[2]}"))
            elif m := _SUREFIRE_OLD.match(line):
                methods.append(TestFailure("junit", f"{m[2]}.{m[1]}"))
            elif m := _SUREFIRE_CLASS.match(line):
                classes.append(m[1])
        elif line.endswith("FAILED") and " > " in line and (m := _GRADLE.match(line.strip())):
            methods.append(TestFailure("junit", f"{m[1]}.{m[2]}"))
    # Class-level ids only when no failing method was named at all (suite classes often
    # report methods under a different class than the "Tests run: ... in X" line).
    if not methods:
        methods = [TestFailure("junit", c) for c in dict.fromkeys(classes)]
    return methods


# --- Others -----------------------------------------------------------------------------

_PHPUNIT = re.compile(r"^\d+\) ((?:\w+\\)*\w+::\w+)(?: with data set (.+))?\s*$")
_DOTNET = re.compile(r"^\s*Failed (.+?) \[\s*[<\d.]+ ?(?:ms|s|m)\]\s*$")
_CTEST = re.compile(
    r"^\s*\d+ - (\S+) \((Failed|Timeout|SEGFAULT|Subprocess aborted|Exception|Child aborted|ILLEGAL)\)\s*$"
)
_XCTEST = re.compile(r"^Test Case '(?:-\[)?([\w.]+?)[ .](\w+)\]?' failed")
_EXUNIT = re.compile(r"^\s+\d+\) (test|doctest) (.+) \(([\w.]+)\)\s*$")


def parse_phpunit(lines: list[str]) -> list[TestFailure]:
    out = []
    for line in lines:
        if "::" in line and (m := _PHPUNIT.match(line)):
            test_id = m[1] + (f" [{m[2]}]" if m[2] else "")
            out.append(TestFailure("phpunit", test_id))
    return out


def parse_dotnet(lines: list[str]) -> list[TestFailure]:
    return [
        TestFailure("dotnet", m[1])
        for line in lines
        if "Failed " in line and (m := _DOTNET.match(line))
    ]


def parse_ctest(lines: list[str]) -> list[TestFailure]:
    return [
        TestFailure("ctest", m[1], FAILED, "" if m[2] == "Failed" else m[2].lower())
        for line in lines
        if " - " in line and (m := _CTEST.match(line))
    ]


def parse_xctest(lines: list[str]) -> list[TestFailure]:
    return [
        TestFailure("xctest", f"{m[1]}.{m[2]}")
        for line in lines
        if line.startswith("Test Case") and (m := _XCTEST.match(line))
    ]


def parse_exunit(lines: list[str]) -> list[TestFailure]:
    return [
        TestFailure("exunit", f"{m[3]}: {m[1]} {m[2]}")
        for line in lines
        if "test" in line and (m := _EXUNIT.match(line))
    ]


PARSERS: list[Callable[[list[str]], list[TestFailure]]] = [
    parse_pytest,
    parse_unittest,
    parse_jest,
    parse_vitest,
    parse_bun,
    parse_node_spec,
    parse_tap,
    parse_mocha,
    parse_playwright,
    parse_go,
    parse_rust,
    parse_rspec,
    parse_minitest,
    parse_junit,
    parse_phpunit,
    parse_dotnet,
    parse_ctest,
    parse_xctest,
    parse_exunit,
]


def parse_failures(lines: list[str]) -> list[TestFailure]:
    """All failing (or retried-then-passed) tests named in a log, de-duplicated."""
    seen: dict[tuple[str, str], TestFailure] = {}
    for parser in PARSERS:
        for f in parser(lines):
            key = (f.framework, f.test_id)
            prev = seen.get(key)
            # A hard failure outranks a within-job flake; keep the first message we saw.
            if prev is None or (prev.outcome == FLAKY and f.outcome == FAILED):
                seen[key] = TestFailure(
                    f.framework, f.test_id, f.outcome, f.message or (prev.message if prev else "")
                )
    return list(seen.values())
