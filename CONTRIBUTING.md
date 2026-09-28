# Contributing

Thanks for helping. The most useful contributions are small and concrete.

## Add or fix a test-runner parser

This is the best first contribution. If redherring doesn't name the tests in your CI logs, it
reports the job as `unknown` and you get less out of it.

1. Find a failed job in your repo and download its log (the job page → ⋯ → *Download log archive*,
   or `gh api repos/OWNER/REPO/actions/jobs/JOB_ID/logs`).
2. Trim it to the lines that name the failing tests (keep the timestamps; 10–40 lines is plenty) and
   save it as `tests/fixtures/logs/<runner>_<project>.log`. Only use logs from public repositories,
   and remove anything that looks like a secret.
3. Add the expected test ids to `REAL` in `tests/test_parsers.py`.
4. Add or fix a `parse_*` function in `src/redherring/parsers.py` and register it in `PARSERS`.

Rules for parsers:

- **Never guess.** Match the runner's real output format and nothing else. A false positive (calling
  a random log line a failed test) is worse than a missed test.
- **Stable ids only.** File, suite and test name. No line numbers, durations or random seeds, so the
  same test lines up across commits.
- **Stay fast.** Parsers run on multi-megabyte logs. Check for a cheap substring before running a regex.

## Add an infrastructure signature

If a job failed because of the environment and redherring called it `unknown`, add the exact error
line to a category in `src/redherring/infra.py` and a case to `test_infra_categories`. Patterns must be
specific: "error" or "failed" would turn real failures into excuses.

## Report a wrong verdict

Open an issue with the run URL (public repos) or the relevant log lines, what redherring said, and
what was actually going on.

## Development

```sh
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```

Tests never touch the network: `tests/fakegh.py` is a small in-memory Actions API.
