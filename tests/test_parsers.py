"""Parsers against trimmed real CI logs (tests/fixtures/logs) and runner-format samples."""

from pathlib import Path

import pytest

from redherring.infra import classify, last_output
from redherring.logtext import clean_lines
from redherring.parsers import FAILED, FLAKY, parse_failures
from redherring.scan import KIND_INFRA, KIND_TESTS, KIND_UNKNOWN, explain_log

LOGS = Path(__file__).parent / "fixtures" / "logs"


def ids(text: str) -> list[tuple[str, str]]:
    return sorted((f.framework, f.test_id) for f in parse_failures(clean_lines(text)))


# --- real logs --------------------------------------------------------------------------

REAL = {
    "vitest_vite.log": [
        (
            "vitest",
            "playground/hmr/__tests__/hmr.spec.ts > hmr should not reload if no accepted "
            "within circular imported files",
        ),
    ],
    "nextest_uv.log": [
        (
            "cargo-nextest",
            "uv-client::it ssl_certs::test_system_certs_with_ssl_cert_dir_replaces_system_roots",
        ),
    ],
    "pytest_airflow.log": [
        (
            "pytest",
            "airflow-core/tests/unit/jobs/test_triggerer_job.py::test_trigger_log[trigger1-1-0]",
        ),
    ],
    "jest_nextjs.log": [
        (
            "jest",
            "test/e2e/app-dir/sub-shell-generation-middleware/sub-shell-generation-middleware.test.ts"
            " › middleware-static-rewrite › should revalidate the overview page without replacing it with a 404",
        ),
    ],
    "go_timeout_prometheus.log": [
        (
            "go",
            "github.com/prometheus/prometheus/tsdb.TestBlockReloadInterval/extremely_small_interval",
        ),
        ("go", "github.com/prometheus/prometheus/tsdb.TestBlockReloadInterval/one_second_interval"),
    ],
    "cargo_tokio.log": [("cargo-test", "conn::http2_connect_detect_close")],
    "rspec_discourse.log": [
        (
            "rspec",
            "spec/system/scroll_manager_service_spec.rb # Ember route-scroll-manager service "
            "scrolls to the top when navigating from a topic to the homepage",
        ),
    ],
    "maven_keycloak.log": [
        ("junit", "org.keycloak.tests.suites.Base1TestSuite.concurrentLoginSingleUserSingleClient"),
    ],
}


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_logs(name):
    assert ids((LOGS / name).read_text(encoding="utf-8")) == sorted(REAL[name])


def test_go_leaf_subtests_only_and_package_attached():
    got = ids((LOGS / "go_grafana.log").read_text(encoding="utf-8"))
    assert len(got) == 6
    assert all(
        t.startswith("github.com/grafana/grafana/pkg/tests/apis/provisioning.TestIntegration")
        for _, t in got
    )


def test_testem_browser_prefix_is_stripped():
    got = ids((LOGS / "tap_testem_discourse.log").read_text(encoding="utf-8"))
    assert got and all(not t.startswith("Chrome") for _, t in got)


@pytest.mark.parametrize(
    "name,category",
    [
        ("maven_registry_keycloak.log", "package registry"),
        ("docker_apk_pulsar.log", "package registry"),
        ("ssl_pydantic.log", "network"),
        ("compose_airflow.log", "service startup"),
    ],
)
def test_real_infra_logs(name, category):
    kind, tests, cause, _ = explain_log((LOGS / name).read_text(encoding="utf-8"))
    assert kind == KIND_INFRA and not tests
    assert cause.category == category


def test_gate_log_is_unknown_with_specific_hint():
    kind, tests, cause, hint = explain_log(
        (LOGS / "gate_langchain.log").read_text(encoding="utf-8")
    )
    assert kind == KIND_UNKNOWN and cause is None
    assert hint == "PR author must be assigned to the linked issue."


# --- runner formats ---------------------------------------------------------------------

SAMPLES = [
    (
        "pytest xdist + collection error",
        "[gw1] [ 50%] FAILED tests/test_a.py::TestX::test_y[p1] \n"
        "ERROR tests/test_b.py - ModuleNotFoundError: No module named 'x'\n"
        "FAILED tests/test_a.py::TestX::test_y[p1] - assert 1 == 2\n",
        [("pytest", "tests/test_a.py::TestX::test_y[p1]"), ("pytest", "tests/test_b.py")],
    ),
    (
        "unittest",
        "======================================================================\n"
        "FAIL: test_upper (tests.test_str.StrTests.test_upper)\n"
        "ERROR: test_split (tests.test_str.StrTests)\n",
        [
            ("unittest", "tests.test_str.StrTests.test_split"),
            ("unittest", "tests.test_str.StrTests.test_upper"),
        ],
    ),
    (
        "jest without describe, console block ignored",
        "FAIL src/sum.test.js\n  ● Console\n\n    console.log\n  ● adds 1 + 2 to equal 3\n",
        [("jest", "src/sum.test.js › adds 1 + 2 to equal 3")],
    ),
    (
        "jest suite failed to run",
        "FAIL src\\broken.test.ts\n  ● Test suite failed to run\n",
        [("jest", "src/broken.test.ts")],
    ),
    (
        "vitest workspace project",
        " FAIL  |unit| src/math.test.ts > math > divides [ src/math.test.ts ]\n",
        [("vitest", "src/math.test.ts > math > divides")],
    ),
    (
        "bun",
        "(fail) parser > handles empty input [0.12ms]\n",
        [("bun", "parser > handles empty input")],
    ),
    (
        "node:test spec",
        "✖ rejects bad input (1.234ms)\n",
        [("node:test", "rejects bad input")],
    ),
    (
        "node:test tap with skip",
        "not ok 1 - parses dates\nnot ok 2 - pending thing # SKIP later\nok 3 - fine\n",
        [("tap", "parses dates")],
    ),
    (
        "mocha",
        "  2 passing (20ms)\n  1 failing\n\n  1) Array\n       #indexOf()\n         returns -1 when absent:\n"
        "     AssertionError: expected 0 to equal -1\n",
        [("mocha", "Array › #indexOf() › returns -1 when absent")],
    ),
    (
        "playwright failed + flaky",
        "  1) [chromium] › tests/login.spec.ts:12:5 › login › shows error ──────\n"
        "  1 failed\n    [chromium] › tests/login.spec.ts:12:5 › login › shows error \n"
        "  1 flaky\n    [webkit] › tests/cart.spec.ts:40:3 › cart › adds item \n"
        "  20 passed (1.2m)\n",
        [
            ("playwright", "tests/cart.spec.ts › cart › adds item"),
            ("playwright", "tests/login.spec.ts › login › shows error"),
        ],
    ),
    (
        "gotestsum",
        "=== FAIL: internal/store TestPut (0.01s)\n",
        [("go", "internal/store.TestPut")],
    ),
    (
        "go without package line",
        "--- FAIL: TestParse (0.00s)\n    --- FAIL: TestParse/empty (0.00s)\n",
        [("go", "TestParse/empty")],
    ),
    (
        "nextest timeout + flaky",
        "        TIMEOUT [  60.003s] mycrate::integration slow::hangs\n"
        "          FLAKY 2/3 [   0.412s] mycrate tests::racy\n",
        [
            ("cargo-nextest", "mycrate tests::racy"),
            ("cargo-nextest", "mycrate::integration slow::hangs"),
        ],
    ),
    (
        "rspec id form",
        "rspec './spec/models/user_spec.rb[1:2:3]' # User validates email\n",
        [("rspec", "spec/models/user_spec.rb # User validates email")],
    ),
    (
        "minitest",
        "  1) Failure:\nUserTest#test_email_is_required [test/models/user_test.rb:10]:\n",
        [("minitest", "UserTest#test_email_is_required")],
    ),
    (
        "surefire old format",
        "[ERROR] testAdd(com.example.CalcTest)  Time elapsed: 0.01 s  <<< FAILURE!\n",
        [("junit", "com.example.CalcTest.testAdd")],
    ),
    (
        "gradle",
        "com.example.CalcTest > divides by zero FAILED\n> Task :test FAILED\nBUILD FAILED in 3s\n",
        [("junit", "com.example.CalcTest.divides by zero")],
    ),
    (
        "phpunit",
        "There was 1 failure:\n\n1) Tests\\Unit\\CartTest::testTotal with data set #2\n",
        [("phpunit", "Tests\\Unit\\CartTest::testTotal [#2]")],
    ),
    (
        "dotnet",
        "  Failed MyApp.Tests.CalcTests.Divide(x: 1) [12 ms]\n  Passed MyApp.Tests.CalcTests.Add [1 ms]\n",
        [("dotnet", "MyApp.Tests.CalcTests.Divide(x: 1)")],
    ),
    (
        "ctest",
        "The following tests FAILED:\n\t  3 - net_roundtrip (Failed)\n\t  7 - io_slow (Timeout)\n",
        [("ctest", "io_slow"), ("ctest", "net_roundtrip")],
    ),
    (
        "xctest",
        "Test Case '-[AppTests.LoginTests testBadPassword]' failed (0.012 seconds).\n",
        [("xctest", "AppTests.LoginTests.testBadPassword")],
    ),
    (
        "exunit",
        "  1) test rejects blank names (MyApp.UserTest)\n     test/user_test.exs:12\n",
        [("exunit", "MyApp.UserTest: test rejects blank names")],
    ),
]


@pytest.mark.parametrize("label,text,expected", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_runner_formats(label, text, expected):
    assert ids(text) == sorted(expected)


def test_flaky_outcomes_are_marked():
    text = "  1 flaky\n    [webkit] › tests/cart.spec.ts:40:3 › cart › adds item \n"
    [f] = parse_failures(clean_lines(text))
    assert f.outcome == FLAKY
    [g] = parse_failures(clean_lines("tests/test_x.py::test_net RERUN\n"))
    assert g.outcome == FLAKY and g.test_id == "tests/test_x.py::test_net"


def test_hard_failure_outranks_in_job_flake():
    text = "tests/t.py::test_a RERUN\nFAILED tests/t.py::test_a - boom\n"
    [f] = parse_failures(clean_lines(text))
    assert f.outcome == FAILED and f.message == "boom"


def test_nextest_does_not_double_count_libtest_echo():
    text = (
        "        FAIL [   1.0s] (1/2) crate::it mod::t\n"
        "    test mod::t ... FAILED\n"
        "test other::u ... FAILED\n"
    )
    assert ids(text) == [("cargo-nextest", "crate::it mod::t"), ("cargo-test", "other::u")]


def test_plain_output_is_not_a_failure():
    text = (
        "Build FAILED.\nBUILD FAILED in 3s\ntest result: FAILED. 0 passed; 1 failed\n"
        "> Task :test FAILED\nERROR: something went wrong\nFAIL\n"
    )
    assert ids(text) == []


# --- log cleaning, infra ----------------------------------------------------------------


def test_clean_lines_strips_timestamps_ansi_and_bom():
    raw = "﻿2026-09-25T10:50:46.3349012Z \x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m a.test.ts > b\r\nnext"
    assert clean_lines(raw) == [" FAIL  a.test.ts > b", "next"]


@pytest.mark.parametrize(
    "line,category",
    [
        (
            "The runner has received a shutdown signal. This can happen when the runner service is stopped",
            "runner lost",
        ),
        (
            "##[error]The job running on runner X has exceeded the maximum execution time of 360 minutes.",
            "job timeout",
        ),
        (
            "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
            "out of memory",
        ),
        ("OSError: [Errno 28] No space left on device", "disk full"),
        ("error: API rate limit exceeded for installation ID 123", "rate limited"),
        ("ERROR: Selected model is at capacity. Please try a different model.", "AI provider"),
        ("npm error code ETIMEDOUT", "package registry"),
        (
            "CondaHTTPError: HTTP 403 Forbidden for url <https://repo.anaconda.com/pkgs/main/win-64/repodata.json>",
            "package registry",
        ),
        ("curl: (6) Could not resolve host: example.com", "network"),
        (
            "failed to connect to the docker API at npipe:////./pipe/docker_engine",
            "service startup",
        ),
        ("Failed to save cache: Cache service responded with 503", "GitHub service"),
    ],
)
def test_infra_categories(line, category):
    cause = classify(["some output", line, "##[error]Process completed with exit code 1."])
    assert cause is not None and cause.category == category


def test_no_infra_cause_in_ordinary_failure():
    assert (
        classify(
            ["error[E0308]: mismatched types", "##[error]Process completed with exit code 101."]
        )
        is None
    )


def test_last_output_prefers_specific_error_then_previous_line():
    assert (
        last_output(["a", "real problem here", "##[error]Process completed with exit code 1."])
        == "real problem here"
    )
    assert (
        last_output(["a", "##[error]Missing label: needs-review"]) == "Missing label: needs-review"
    )


def test_explain_log_test_kind():
    kind, tests, cause, hint = explain_log("FAILED tests/t.py::test_a - boom\n")
    assert (
        kind == KIND_TESTS
        and tests[0].test_id == "tests/t.py::test_a"
        and cause is None
        and hint == ""
    )


def test_vitest_whole_file_failure():
    line = " FAIL  playground/nested-deps/__tests__/nested-deps.spec.ts [ playground/nested-deps/__tests__/nested-deps.spec.ts ]"
    [f] = parse_failures(clean_lines(line))
    assert (f.framework, f.test_id) == (
        "vitest",
        "playground/nested-deps/__tests__/nested-deps.spec.ts",
    )


def test_rollup_job_recognised_by_its_output():
    text = "📝 lint → ✓ success [required to succeed]\n❌ test → 🔴 failure [required to succeed]\n##[error]Process completed with exit code 1.\n"
    kind, *_ = explain_log(text)
    assert kind == "summary"


def test_hint_skips_borders_and_exit_code_echoes():
    lines = [
        "Error: page.goto: net::ERR_CONNECTION_REFUSED",
        "+------------------------------------",
        "[ELIFECYCLE] Command failed with exit code 1.",
        "##[error]Process completed with exit code 1.",
    ]
    assert last_output(lines) == "Error: page.goto: net::ERR_CONNECTION_REFUSED"


@pytest.mark.parametrize(
    "line,category",
    [
        (
            "Failed to FinalizeArtifact: Received non-retryable error: Failed request: (403) Forbidden",
            "GitHub service",
        ),
        (
            "fatal: unable to access 'https://github.com/a/b.git/': The requested URL returned error: 502",
            "network",
        ),
        ("error: Unexpected HTTP response: 500", "network"),
    ],
)
def test_more_infra_signatures(line, category):
    cause = classify([line, "##[error]Process completed with exit code 1."])
    assert cause is not None and cause.category == category


@pytest.mark.parametrize(
    "line,category",
    [
        (
            "go: reading https://sum.golang.org/tile/8/0/x136/121: stream error: stream ID 273; INTERNAL_ERROR; received from peer",
            "network",
        ),
        (
            "subprocess.CalledProcessError: Command '['git', 'clone', 'https://gitlab.com/a/b']' returned non-zero exit status 128.",
            "network",
        ),
        ("Error: HTTP 500 (https://api.github.com/repos/a/b/releases)", "GitHub service"),
        (
            "docs#build: command (D:/a/docs) pnpm.CMD run build exited (-1073741502)",
            "runner environment",
        ),
        (
            "./scripts/link.sh: /tmp/yarn: /bin/sh: bad interpreter: Text file busy",
            "runner environment",
        ),
        ("Caused by: Error: Worker exited unexpectedly", "test worker crash"),
        (
            "Some tasks were terminated on timeout. Please check the logs of the tasks (above) for more details.",
            "job timeout",
        ),
    ],
)
def test_audit_signatures(line, category):
    cause = classify([line, "##[error]Process completed with exit code 1."])
    assert cause is not None and cause.category == category


def test_audit_false_positives_are_gone():
    jvm = '{"level": "INFO", "message": "JVM arguments [-Xshare:auto, -XX:+HeapDumpOnOutOfMemoryError]"}'
    assert classify([jvm, "##[error]Process completed with exit code 1."]) is None
    denied = "fatal: unable to access 'https://github.com/a/b.git/': The requested URL returned error: 403"
    assert classify([denied, "##[error]Process completed with exit code 128."]) is None


def test_hint_prefers_compiler_errors():
    lines = [
        "error: could not compile `bevy_encase_derive` (lib) due to 3 previous errors",
        "warning: build failed, waiting for other jobs to finish...",
        "One or more CI commands failed:",
        "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace",
        "##[error]Process completed with exit code 1.",
    ]
    assert last_output(lines).startswith("error: could not compile")


def test_testem_global_error_names_the_running_test():
    text = (
        "not ok 3612 Chrome 154.0 - [undefined ms] - Global error: Uncaught TypeError: boom\n"
        "    ---\n"
        "        message: >\n"
        "            While executing test: Browser Id 4 - Acceptance: User Card - Inactive user: it shows less\n"
    )
    [f] = parse_failures(clean_lines(text))
    assert f.test_id == "Acceptance: User Card - Inactive user: it shows less"
    assert f.message.startswith("Uncaught TypeError")


def test_status_check_rollups_are_summaries():
    from redherring.scan import is_summary_job

    assert is_summary_job("Status Check - Keycloak CI")
    assert is_summary_job("Required PR Quality Checks")
    assert not is_summary_job("Unit tests")
    kind, *_ = explain_log(
        "- e2e: failed\n- unit-test: failed\n##[error]Process completed with exit code 1.\n"
    )
    assert kind == "summary"


@pytest.mark.parametrize(
    "line,category",
    [
        (
            "Failing on max retries. Error while downloading kind: HTTP Error 504: Gateway Time-out",
            "network",
        ),
        (
            "ERROR: failed to solve: ResourceExhausted: failed to copy files: failed to open target",
            "disk full",
        ),
    ],
)
def test_study_signatures(line, category):
    cause = classify([line, "##[error]Process completed with exit code 1."])
    assert cause is not None and cause.category == category


def test_env_block_is_not_a_hint():
    lines = [
        "real problem",
        "  NODE_OPTIONS: --max-old-space-size=4096",
        "##[error]Process completed with exit code 1.",
    ]
    assert last_output(lines) == "real problem"
