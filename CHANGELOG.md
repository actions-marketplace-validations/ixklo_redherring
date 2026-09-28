# Changelog

## 0.1.0 (unreleased)

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
