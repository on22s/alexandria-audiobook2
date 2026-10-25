---
name: aa2-ci-triage
description: Triage failing GitHub Actions CI on an alexandria-audiobook2 PR - list the exact failing unit tests per shard, label each against main, and separate PR-caused failures from flakes and local-environment noise before fixing anything. Use when a PR's checks are red, when the user pastes failing checks ("tests / unit (2/3) failing"), asks why CI failed or whether a failure is real, or before re-running CI or pushing a fix.
---

# aa2 CI triage

CI here is three `unit (N/3)` shards plus a `test` job that only reports
whether the shards passed, all in one run. A run-level log therefore shows the
`test` job's "The unit shards finished as 'failure'" line, not the failing
test. The unit tests and the release verifier both run inside each shard's
**Shared release verifier** step.

## 1. Get the real failures

```bash
python3 .claude/skills/aa2-ci-triage/ci_failures.py <PR>        # current head
python3 .claude/skills/aa2-ci-triage/ci_failures.py --run <id>  # a specific run
python3 .claude/skills/aa2-ci-triage/ci_failures.py --run <id> --attempt 1
```

It reads each failing job's own log, names the repo explicitly (this checkout's
`upstream` remote makes a bare `gh` call resolve to Finrandojin's repo and 404),
and prints per job: the failing `module.Class.test` with subtest count and its
first error line, or, when no unit test failed, the failed step and its
`##[error]` lines. It is read-only. Exit 0 means no observed failures in completed checks, 1 means failed jobs, and 2 means checks are absent, pending, or incomplete. JSON includes `checks_pending`. A failed main run can establish a known failure but cannot establish that an unobserved test passed. PR mode inspects currently failed check runs; use `--run` to inspect the history of a now-green rerun.

## 2. Classify each failure, with evidence

| Label / signal | Meaning | Not proof of |
|---|---|---|
| `also on main` | fails on main's latest CI run too | that the PR is fine elsewhere |
| `NEW vs main` | main's latest completed push run passed | that the PR caused it |
| `not compared` | main did not establish absence of this failure | that it is new |
| `latest attempt passed (flake signal)` | an earlier attempt failed and the latest passed | that the cause is harmless |
| `latest attempt did not pass` | both attempts failed | that it is a flake |
| no unit test failed, step = Shared release verifier | the verifier rejected the tree (stale derived file, inventory, contract snapshot) | a test bug |

A `NEW vs main` failure is caused by the PR only when it **fails at the PR head
and passes at the PR's merge base, run the same way on the same machine.** Check
it before saying so:

```bash
git worktree add --detach /tmp/base "$(git merge-base origin/main <pr-head>)"
bash -c 'mods=(tests.test_a tests.test_b)
         for d in /tmp/base <pr-worktree>; do (cd "$d/app" && "$PY" -m unittest "${mods[@]}" 2>&1 | tail -3); done'
```

- Run module lists in **bash with an array**. In zsh an unquoted `$MODS` stays
  one word, unittest runs a single bogus name, and prints `Ran 1 test` and an
  error that looks like a result. A `Ran N` far below the expected count means
  the instrument failed, not the code.
- For a suspected flake, repeat the smallest reproduction only as needed and
  compare serial and concurrent conditions. Passing alone does not clear an
  ownership or concurrency bug. Identify the shared resource (timers, `/tmp`
  metadata, ports) and preserve the failing evidence.

## 3. Rule out the local environment first

Local failures that CI does not have are usually the interpreter, not the code.

```bash
PY="$(./tools/regen_derived.sh --python)"     # what ready.sh and the hooks use
uv pip install --python "$PY" -r app/requirements.txt --dry-run   # must change nothing
```

`ModuleNotFoundError` (e.g. `httpx2`, `av`) or SDK-version errors (`openai`
pinned in `requirements.txt`) mean the env is stale. A worktree without its own
`app/env` uses the main checkout's. Compare failing test IDs against main run
with the same interpreter; never count raw failure totals across environments.
Some tests depend on this machine (two in `test_synthesis_workspace` fail with a
real GPU present and pass in CI); they fail identically on main.

## 4. Before acting

- **Read PR comments newer than the head commit** (`gh api
  repos/on22s/alexandria-audiobook2/issues/<PR>/comments`). Codex posts findings
  as plain comments, which do not block a merge.
- Decide the policy for a failure once (Rule 10): re-run only something already
  shown to be a flake, at most once, and say that it was re-run.
- A fix is verified when the failing test fails before it and passes after it,
  and the full suite shows nothing new against main (Rule 21), not when the
  focused tests pass. Focused runs miss what other modules cover.
- Report the rate or ETA from this run's own job timings: shards take roughly
  10-14 minutes.
