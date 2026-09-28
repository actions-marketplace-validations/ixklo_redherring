# Changelog

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
