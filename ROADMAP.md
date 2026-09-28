# Roadmap

The rule for everything below: the core stays local, free, and deterministic. No server, no account, no
LLM required.

## Done in 0.1

- `scan`, `why`, text/Markdown/JSON output, exit codes for scripts and agents
- 19 test-runner formats, 11 infrastructure causes, roll-up and policy-check filtering
- Composite GitHub Action with job summaries and PR comments
- Flake ledger that outlives GitHub's run retention
- A skill for coding agents

## Next (0.2)

- **Hidden flakes.** Tests that runners retry inside a *green* job (Playwright `flaky`, nextest `FLAKY`,
  pytest `RERUN`) never make a run red, so they're invisible today. Sample green logs to find them.
- **Quarantine helpers.** Generate reviewable skip/retry config per runner from the ledger (pytest markers,
  Jest lists, nextest filters), always as a pull request, never silently.
- **A stable, versioned JSON schema** for dashboards and bots.
- **More parsers:** Karma, Cypress, Deno test, Pest, Kotest, Swift Testing (see the `parser` label).

## Later (1.0)

- GitLab CI and Buildkite behind the same interface
- An MCP server exposing `why` and the ledger to agents
- Org-wide scans (many repos, one report)
- Parser plugins via entry points

## Considered and dropped

- **Counting "same commit, different run" as evidence** outside re-runs (push, schedule, merge queue).
  Measured on four large repos: zero such fail-then-pass pairs. Pull-request runs test a merge with a moving
  base branch, so they can't count either.
