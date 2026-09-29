# Roadmap

The rule for everything below: the core stays local, free, and deterministic. No server, no account, no
LLM required.

## Done in 0.1

- `scan`, `why`, text/Markdown/JSON output, exit codes for scripts and agents
- Over 20 test-runner output formats, 12 infrastructure causes, roll-up and policy-check filtering
- Composite GitHub Action with job summaries and PR comments
- Flake ledger that outlives GitHub's run retention
- A skill for coding agents

## Done in 0.2

- Safer verdicts (0.1.2): failing again on the same commit means real; one past flake is only
  "probably flaky"; untrusted text is rendered as data.
- Cheap repeat scans (ETag / free 304s), flat memory, cached readings, pruned cache, one-pass Action.

## Done in 0.3

- Team notes (`.github/redherring.toml`): mark tests and jobs flaky or not flaky, read from the default
  branch.
- `redherring report`: a scrubbed, pre-filled issue for failures it couldn't read or got wrong, sent only
  by the user.

## Next (0.4)

- **Hidden flakes.** Tests that runners retry inside a *green* job (Playwright `flaky`, nextest `FLAKY`,
  pytest `RERUN`) never make a run red, so they're invisible today. Sample green logs to find them.
  Evidence it's worth it: even inside the *failed* logs of the study, Playwright's own retries
  hid 42 flaky tests (Airflow, n8n, Supabase).
- **Fewer "unknown" jobs.** In the 122-repo study nearly half of red-herring jobs named neither a test nor a
  known cause. Audits show about half of those have a visible cause in a format not yet parsed.
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
