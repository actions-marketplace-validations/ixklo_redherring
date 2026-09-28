<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/banner-dark.svg">
    <img alt="redherring: find the CI failures that weren't your fault" src="docs/banner-light.svg" width="640">
  </picture>
</p>

<p align="center">
  <a href="https://pypi.org/project/redherring/"><img alt="PyPI" src="https://img.shields.io/pypi/v/redherring"></a>
  <a href="https://pypi.org/project/redherring/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/redherring"></a>
  <a href="https://github.com/ixklo/redherring/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ixklo/redherring/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
  <a href="https://github.com/marketplace/actions/redherring-ci"><img alt="GitHub Marketplace" src="https://img.shields.io/badge/Marketplace-redherring%20CI-2ea44f?logo=github"></a>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#redherring-why-is-this-failure-mine">Explain a failure</a> ·
  <a href="#github-action">GitHub Action</a> ·
  <a href="#for-coding-agents">For coding agents</a> ·
  <a href="docs/study/README.md">The study</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

When a CI run goes red and then turns green after someone presses **re-run**, nothing in the code changed. Whatever failed was a **red herring**: a flaky test, a network blip, a package registry hiccup, a runner that vanished.

redherring reads the GitHub Actions history your repo **already has**, finds every one of those, opens the failed logs, and names the flaky tests (20+ test-runner formats) or the infrastructure cause. When a build fails, `redherring why` tells you (or your coding agent) which failures are known red herrings and which look real.

**No server, no signup, nothing to set up in CI first.** If your repo has Actions history, the first run already has answers.

![redherring scan on astral-sh/uv: 2,129 runs in 14 days, 280 went red, 94 of those (34%) turned green on a plain re-run; a table of named flaky tests, several Windows-only; flaky jobs grouped by cause](docs/demo-scan.svg)

<sub>Real output for a public repo, 28 September 2026. The "AI provider" cause is an AI code-review job failing with "Selected model is at capacity".</sub>

## Quickstart

With [uv](https://docs.astral.sh/uv/) (or `pipx install redherring` / `pip install redherring`; Python 3.11+):

```sh
uvx redherring scan OWNER/REPO                                   # what flakes here, and what it costs
uvx redherring why https://github.com/OWNER/REPO/actions/runs/ID  # is this failure mine?
```

It needs a GitHub token to read Actions logs. If you use the [GitHub CLI](https://cli.github.com/), it borrows `gh auth token` automatically; otherwise set `GH_TOKEN`. Any token works for public repositories; private ones need `actions: read`.

## How common is this?

We ran it on **122 popular repos** (1.25 million workflow runs over two weeks). For most repos it's small: about 2% of red runs. For a quarter of them it's **7% or more**, and in some it's a daily tax. Across all 122, **690 runner-hours** went to failed jobs that a plain re-run fixed.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/study/share-dark.svg">
    <img alt="Bar chart: share of red CI runs that were red herrings for the 20 highest of 69 repos with 50 or more red runs. astral-sh/uv 34%, microsoft/vscode 23%, forem/forem 20%, ollama/ollama 18%, bitcoin/bitcoin 17%, vercel/next.js 15%, mastodon/mastodon 15%. The median of the 69 is 3%." src="docs/study/share-light.svg" width="760">
  </picture>
</p>

Method, every repo's numbers, what causes them, and how accurate the classification is: **[read the study →](docs/study/README.md)**

## `redherring scan`: what flakes in this repo?

```sh
redherring scan                       # the repo of the current checkout
redherring scan vitejs/vite --days 14
redherring scan owner/repo --workflow ci.yml --format md > FLAKY.md
```

You get:

- **Flaky tests**: each failed and then passed on a re-run of the same commit, with how often, on how many commits, on which OS (plenty of tests only flake on Windows), and when last seen.
- **Flaky jobs with no test named**, grouped by cause: `network`, `package registry`, `rate limited`, `runner lost`, `runner environment`, `job timeout`, `out of memory`, `disk full`, `service startup`, `test worker crash`, `GitHub service`, `AI provider`, `many tests at once` (an environment problem, not ten flaky tests), or `unknown`.
- **What it cost**: runner time burned by the failed jobs, and how long commits sat red waiting for someone to press re-run.

`--format json` gives everything, including example job links, for your own dashboards.

## `redherring why`: is this failure mine?

```sh
redherring why https://github.com/owner/repo/actions/runs/123456789
redherring why 123456789 --repo owner/repo --format json
```

![redherring why on a failed uv PR run: one test is a known flake (failed then passed on re-runs twice in 14 days), the other looks real](docs/demo-why.svg)

For every failure in the run, one verdict:

| verdict | meaning |
|---|---|
| **RED HERRING** (known flaky) | This test failed and then passed on a re-run of the same commit at least twice in the last N days. |
| **PROBABLY FLAKY** | It did that once. Re-run once to check. |
| **RED HERRING** (infrastructure) | The log shows a network, registry, runner, or service problem, not a test failure. |
| **NOT THIS CHANGE** | The latest finished run on the default branch fails this same test. |
| **LOOKS REAL** | Never seen flaking and no infrastructure cause, **or it already failed on an earlier attempt of this same commit**: a re-run isn't fixing it. Probably your change. |
| **UNCLEAR** / **POLICY CHECK** | Can't tell from history, or a PR policy check (labels, linked issue) that passes once the PR is updated. |

Exit codes make it scriptable: `0` all red herrings (or nothing failed), `1` something looks real, `3` unclear, `2` error.

```sh
redherring why "$RUN" -q && gh run rerun "$RUN" --failed
```

### For coding agents

Agents lose a lot of time "fixing" failures that were never theirs, and sometimes weaken a perfectly good test to make a flake go green. Add this to your `AGENTS.md` or `CLAUDE.md`:

```markdown
## CI failures
Before changing code because CI failed, run `redherring why <run-url> --format json`.
- `recommendation: "rerun"`: every failure is a flake, an infrastructure problem, or already failing
  on main. Re-run the failed jobs once (`gh run rerun <id> --failed`); don't edit code or tests for it.
  If it fails again, run `redherring why` on the new attempt: a test that fails twice on the same
  commit comes back as "looks real".
- `recommendation: "investigate"`: fix only the findings with `"red_herring": false`.
- Never skip, loosen, or delete a test just because it is listed as flaky.
```

Or install the ready-made skill: copy [`skills/redherring/`](skills/redherring/SKILL.md) into your agent's skills folder (for Claude Code, `.claude/skills/redherring/`). More setups are in [`examples/`](examples/).

## GitHub Action

Explain every failed CI run, in its job summary and as a comment on the pull request:

```yaml
# .github/workflows/redherring.yml
name: redherring
on:
  workflow_run:
    workflows: [CI]          # the name of your test workflow
    types: [completed]
permissions:
  actions: read
  contents: read
  pull-requests: write      # only needed for comment: true
jobs:
  why:
    if: github.event.workflow_run.conclusion == 'failure'
    runs-on: ubuntu-latest
    steps:
      - uses: ixklo/redherring@v0.2.1
        with:
          comment: true
```

The comment is posted once per PR and workflow, then edited in place on later failures, never duplicated.

Or post a weekly flaky-test report:

```yaml
on:
  schedule: [{ cron: "0 7 * * 1" }]
permissions:
  actions: read
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: ixklo/redherring@v0.2.1
        with:
          command: scan
          days: "30"
```

Inputs: `command` (`why` or `scan`), `run-id`, `days` (default 14), `workflow`, `ledger`, `comment`, `cache` (default on: keeps downloaded history between runs, because the Actions token allows about 1,000 API requests an hour), `fail-on-real`, `github-token`. Output: `recommendation`. From the command line, the same comment is `redherring why <run> --comment`.

## Keep your flake history: the ledger

From 1 October 2026 GitHub deletes workflow runs once they pass your log-retention period (90 days by default). Past that, redherring has nothing to read. A ledger keeps the evidence:

```sh
redherring scan --ledger .github/flake-ledger.json          # merges new evidence into the file
redherring why "$RUN" --ledger .github/flake-ledger.json    # uses it as extra history
```

The file is small JSON (one entry per failed job: test names, cause, commit, OS, link). Keep it anywhere durable. You can commit it, or carry it between scheduled runs with `actions/cache`:

```yaml
    steps:
      - uses: actions/cache@v6
        with:
          path: flake-ledger.json
          key: redherring-ledger-${{ github.run_id }}
          restore-keys: redherring-ledger-
      - uses: ixklo/redherring@v0.2.1
        with:
          command: scan
          ledger: flake-ledger.json
```

Entries older than a year are dropped (`--keep-days`).

## How it decides

redherring is deliberately strict about what it calls flaky. **Only one kind of evidence counts: the same commit failed and then passed.** That is what a re-run is. It does not guess from "failed on a PR, passed on main" or "this test fails a lot", because those are often real failures. A tool that cries wolf is worse than none.

Then, per failed job:

1. **Tests.** The log is cleaned (timestamps, colour codes) and read by runner-specific parsers. Test ids keep only stable parts (file, suite, name; never line numbers or timings) so the same test lines up across commits and OSes.
2. **Infrastructure.** If no test is named, the end of the log is matched against specific signatures (`ECONNRESET`, `Could not transfer artifact`, `The runner has received a shutdown signal`, `model is at capacity`, ...).
3. **Not flakiness at all.** Roll-up jobs ("all required jobs passed") and PR policy checks are set aside and not counted.

Supported test output:

| ecosystem | runners |
|---|---|
| Python | pytest (incl. xdist, pytest-rerunfailures), unittest |
| JavaScript / TypeScript | Jest, Vitest (incl. workspaces), Playwright (incl. its own retries), Mocha, node:test (spec and TAP), Bun, TAP (QUnit/testem) |
| Go | `go test`, gotestsum, test timeouts |
| Rust | `cargo test`, cargo-nextest (incl. retries), `failed tests:` lists (Deno and other file-based runners) |
| Ruby | RSpec, Minitest |
| JVM | Maven Surefire, Gradle (incl. verbose test logging) |
| others | PHPUnit, .NET (`dotnet test`), CTest, XCTest, ExUnit |

Missing yours? A parser is one function and a test with a real log excerpt. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Limits, honestly

- **History has an expiry date.** Job logs are kept for 90 days by default, and [from 1 October 2026](https://github.blog/changelog/2026-08-27-actions-retention-will-cover-checks-workflow-runs-and-statuses/) GitHub deletes the workflow runs themselves once they pass the repo's log retention period (90 days by default, and at most 90 days for public repos). `--days` beyond that finds nothing; use a [ledger](#keep-your-flake-history-the-ledger) to keep what was found.
- **Only re-runs count.** Repos that never press re-run, or retry inside the job without reporting it, show fewer flakes than they have. Playwright, nextest and pytest-rerunfailures retries are recognised when they appear in a failed job's log.
- **The first scan of a big repo costs API requests.** A busy monorepo can take a few thousand for 30 days (GitHub allows 5,000 an hour). redherring waits out rate limits on its own, and **later scans are cheap**: it remembers what each log said, and asks GitHub "has this page changed?" before re-downloading. GitHub answers unchanged pages with a free 304. On astral-sh/uv, a repeat 7-day scan took 4 requests instead of 169.
- **Some jobs stay "unknown".** In a study of 122 popular repos, nearly half of the red-herring jobs (46%) named neither a test nor a known infrastructure cause. Some logs genuinely say nothing ("exit 1"); others use output formats redherring doesn't parse yet. `why` treats those as "unclear", never as safe to re-run. Parser contributions fix this one format at a time.
- **GitHub Actions only**, for now.

## Privacy

redherring runs on your machine (or in your own Actions runner) and talks only to the GitHub API. It caches what it downloads in your user cache folder (`%LOCALAPPDATA%\redherring`, `~/Library/Caches/redherring`, or `~/.cache/redherring`; override with `REDHERRING_CACHE`). The cache holds job lists, what each log said, and API pages with their ETags, plus compressed copies of the logs so improved parsers can re-read them without downloading again. Logs are pruned after 120 days (GitHub deletes its own after 90). Set `REDHERRING_KEEP_LOGS=0` to keep only what the logs said; the GitHub Action does this. Nothing is sent anywhere else.

## License

MIT
