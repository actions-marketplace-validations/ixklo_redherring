---
name: redherring
description: Use when a CI run or GitHub Actions check fails on a branch or pull request you are working on, before changing any code or test to make it pass. Tells you which failures are known flaky tests or infrastructure problems (re-run them) and which look caused by the change (fix those).
---

# Is this CI failure mine?

Run this before editing code or tests because CI is red:

```sh
redherring why <run-url> --format json
```

(`<run-url>` is `https://github.com/OWNER/REPO/actions/runs/ID`. If `redherring` isn't installed:
`uvx redherring why <run-url> --format json`.)

Read `recommendation`:

- `"rerun"`: every failure is a known flaky test, an infrastructure problem (network, registry, runner,
  AI provider...), or already failing on the default branch. Re-run the failed jobs
  (`gh run rerun <id> --failed`) and wait. Do not edit code or tests for these.
- `"investigate"`: fix only the findings with `"red_herring": false`. Their `detail` says why they look
  real (never seen flaking, no infrastructure cause).
- `"unclear"`: open the logs of the findings with `"red_herring": null` and judge them yourself.

Rules:

- Never skip, delete, loosen, or add retries to a test because redherring lists it as flaky. Say in your
  summary that it is a known flake and link the evidence (`history.examples`).
- If a finding is `"already failing"`, the default branch is broken too; mention it, don't fix it in this PR
  unless asked.
- Exit codes: 0 = all red herrings, 1 = something looks real, 3 = unclear, 2 = error.
