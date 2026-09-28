# Changelog

## 0.2.1 (2026-09-28)

Cleaner failure snippets, from user feedback:

- **No more raw colour codes in snippets.** Real escape sequences were already removed, but colour
  codes that reached the log *as text* got through: a test quoting a coloured string
  (`'\x1b[1;33mWARN'`), a script echoing `\033[0m` without `-e`, JSON's `\u001b[31m`, `^[[36;1m`,
  or `[31mFAILED[0m` whose escape byte was lost. They are now removed from failure messages, infra
  evidence and hints, in every output format. Ledgers saved by older versions are cleaned on load.
  Lines like that also parse now: `[31mFAILED[0m tests/a.py::t` is a pytest failure.
- Other escape sequences are removed too (`tput sgr0`'s charset reset, save/restore cursor).
- **Better "last output" hints.** Lines like `ERROR: lockfile is out of date` were mistaken for
  environment variables and skipped. Re-reading the 6,480 logs of the study, 63 hints changed, most
  from a filler line to the real error (VS Code: `ERROR: "eslint" exited with 137.` instead of
  "Finished typecheck with 0 errors").
- Cached readings refresh on their own (the parser version changed); nothing to do.

## 0.2.0 (2026-09-28)

Lighter and much cheaper on big repos, from a code review:

- **Repeat scans are nearly free.** API pages are kept with their ETag and re-validated. GitHub answers
  unchanged pages with a 304, which doesn't count against the rate limit. Run listings use calendar
  days, so past days' queries repeat exactly. On astral-sh/uv, a repeat 7-day scan used 4 requests
  instead of 169, and 9 seconds instead of 51. Output shows free requests separately.
- **Memory stays flat.** Each log is parsed as it arrives and only what it says is kept. Before, a scan
  held every downloaded log in memory at once.
- **What each log says is cached per parser version**, so re-scans don't re-read logs, and a new version
  of the parsers re-reads them automatically (from the cache, without downloading).
- **Roll-up jobs recognisable by name aren't downloaded** (next.js alone had 314 in two weeks).
- **The cache is pruned:** logs after 120 days (GitHub's own retention is 90), stale API pages after 30.
  `REDHERRING_KEEP_LOGS=0` keeps only what logs said: a much smaller cache.
- **The GitHub Action runs once per failure** instead of twice (`--json-out` writes the JSON next to the
  Markdown), and carries a smaller cache between runs. CI now smoke-tests `why` through the Action too.
- `--json-out FILE` on `scan` and `why`. Old caches are migrated automatically.
- Housekeeping: thread-safe request counters, a slow loop on repos with many re-runs, dead code, type
  hints (mypy is clean), and tests for the terminal and Markdown output.

## 0.1.2 (2026-09-28)

Safer verdicts, from a code review:

- **A test that fails again on the same commit looks real.** `why` now checks earlier attempts of the
  run it explains. Before, a known-flaky test that failed on the re-run too was still called a red
  herring, which could send a coding agent round in circles re-running a real failure.
- **One past flake isn't enough.** "Known flaky" needs two or more re-run-to-green failures; a single one
  is "probably flaky: re-run once to check".
- **Untrusted text is rendered as data.** Test names, failure messages and log lines can be written by
  whoever opens a pull request. In PR comments and job summaries they're now inline code (no
  @mentions, links or HTML); in the terminal they're shown literally.
- **Terminal fixes.** Parametrised test names kept their `[...]` part (it was being swallowed as a style
  tag), and a log line like `[/usr/bin]` no longer crashes `why`.
- JSON findings have a separate `evidence` field for the text copied from the log.

## 0.1.1 (2026-09-28)

- The Action is named "redherring CI" so it can be listed on the GitHub Marketplace (a GitHub account
  called "redherring" already exists). No behaviour changes.

## 0.1.0 (2026-09-28)

First version.

- `redherring scan`: finds runs that went green only after a re-run of the same commit, reads the
  failed attempts' logs, and reports flaky tests, flaky jobs by cause, runner time lost and time spent red.
- `redherring why`: explains one failed run. Each failure is a known flake, infrastructure, already
  failing on the default branch, a policy check, or looks real. Exit codes for scripts and agents.
- Parsers for pytest, unittest, Jest, Vitest, Playwright, Mocha, node:test, Bun, TAP, go test,
  gotestsum, cargo test, cargo-nextest, "failed tests:" lists, RSpec, Minitest, Maven Surefire, Gradle,
  PHPUnit, dotnet test, CTest, XCTest and ExUnit.
- Infrastructure causes: network, package registry, rate limited, runner lost, runner environment,
  job timeout, out of memory, disk full, service startup, test worker crash, GitHub service, AI provider.
- Checked by two independent verdict audits on 113 real failed jobs (see the project report).
- Text, Markdown and JSON output; a composite GitHub Action; a local cache of immutable history.
- `--ledger`: a JSON file of past evidence that outlives GitHub's run retention (from 1 Oct 2026 runs
  are deleted after the log-retention period).
- `why --comment` / Action `comment: true`: one PR comment per workflow, edited in place.
