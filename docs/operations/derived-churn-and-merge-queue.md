# Derived-file churn and merge-group qualification

## Measured baseline (2026-11-16)

Compared each merge commit with its first parent, parsing JSON as well as
examining byte diffs. These are measurements, not attribution from PR titles.

| PR | Merge commit | Goal audit diff (+/- lines) | Inventory additions | Other derived changes |
|---|---|---:|---:|---|
| #1974 | f19ce7546b837f66f3849a51bd7c01d9024ff3c5 | 6/6 | 12 tests in four modules | RESULTS_INDEX date only; 31 English catalog entries |
| #1980 | 32d6cdcd79ed09068bfd89ae58eab90c6d54bc3f | 6/6 | 14 tests in five modules | 13 English catalog entries |
| #1982 | fcc84529ffe81800d59a1891c021752189e792db | 9/15 | 5 tests in three modules | 10 English catalog entries |
| #1983 | fac807334279cb0e3703692f2b4ea0918b67e14a | 6/5 | none | none |
| #1984 | 81294d5bb0c12fa3cd5e9a001452b1741f57ee88 | 15/10 | 6 tests in one module | none |

All five goal-audit bodies and attested harness digests were identical. Only
`provenance.git` and `provenance.written` changed. Every PR changed commit and
branch. #1974/#1980 also replaced commit-tree proofs. #1982 changed clean to
dirty, removed the clean proof, and recorded modified paths/source_state;
#1983 changed those dirty diagnostics; #1984 returned to clean with a proof.
The old retention predicate compared those diagnostics (and host/arguments),
so unrelated source changes rewrote the common audit even with the same body
and harness. The same-clean-ref optimization did not cover that workflow.

Inventory changes in this sample were real test IDs, not ordering or metadata.
The inventory has no global count or hash to remove. Its writer bypassed the
formatting function and used generic indented JSON. Adding a last entry could
also rewrite a preceding closing delimiter. The new representation keeps one
sorted test ID per line within sorted module keys, through the same atomic
writer. Readers, discovery, exclusions and set comparison remain unchanged.
Totals continue to be derived by the shard/report checkers at check time.

`RESULTS_INDEX.md` moved from Generated 2026-11-15 to Generated 2026-11-16 in
#1974 without changing any of its 1,820 artifacts or 3,123 arms. Generation now
retains the historical date if the indexed Markdown is otherwise identical;
real data changes still produce a fresh report. Its check uses exactly the
same date normalization and added_commit exception as before.

Across these five comparisons the other four paths in the regeneration set
were byte-identical: `results_index.csv`,
`LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md`,
`ab_test_runtime/audit/artifact_structural_audit.json`, and
`ab_test_runtime/audit/legacy_attribution_audit.json`. Catalog changes were
real extracted UI content and are retained, not suppressed. Artifacts, goals,
catalog keys or test IDs that actually change can still require resolution.

## Preservation contract

An existing goal audit is retained only if it is clean and carries a verified
commit-tree proof, and its body and harness identity agree
with regeneration and the shared verifier verifies both stored and newly
captured provenance with `require_current=True`. This reuses #1971's
commit-bound tree proof and #1973's current-harness and clean-diagnostic checks.
Generation and `--check` share one body-equivalence function; the check's
acceptance criteria and shared verifier are unchanged. Dirty or proofless
origins are rewritten so a later clean regeneration upgrades them before a
squash-only clone loses their feature commit. A stale
body, changed harness, malformed proof or contradictory clean diagnostics must
still fail verification. Historical host, argument paths and timestamps
describe the retained origin, not a newly fabricated run.

The local `.gitattributes` merge=ours scheme, hook installer, post-merge,
post-rewrite and conflict-refusing resolver remain in place. Server-side
merges have no local driver or hooks; the proof explicitly exercises that
case. Inventory keys and IDs are sorted, with one test ID per line. Valid JSON
uses leading comma separators so appending a test or module does not rewrite
the former last entry. Well-separated additions within the same module merge
in both orders. Adjacent insertions, actual evidence changes, or
two harness-changing PRs are not promised conflict-free. Open PRs based on the
old serialization need one update across this format migration.

## Workflow/event audit and check names

There are ten workflow files, but **nine** have a `pull_request` trigger. All
nine now also have an unfiltered `merge_group` trigger. The tenth,
`windows-export-staging-evidence.yml`, explicitly runs manual-only hardware
qualification; it remains workflow_dispatch-only and is not a PR/queue check.

All checkouts use the event's default ref/SHA, including
`gh-readonly-queue/main/...`; none overrides it with a PR head or base. Full
history remains enabled where provenance needs it. The only PR-payload access
is the maintenance-hints step, now guarded for PR or merge_group and selecting
the respective event's base SHA. The reusable rolling workflow continues to
skip its two Windows jobs intentionally through linux_only.

Exact job/check names (expand every listed matrix value):

| Workflow | Check names |
|---|---|
| tests | `unit (1/6)` through `unit (6/6)`; `derived-files`; `test`; `windows-owner`; `windows-portability` |
| external TTS lifetime | `external-tts (ubuntu-latest)`; `external-tts (windows-latest)`; `external-tts (macos-latest)` |
| source upload publication | `source-upload (ubuntu-latest)`; `source-upload (windows-latest)`; `source-upload (macos-latest)` |
| qwen read-aloud | `qwen-readalong (ubuntu-latest)`; `qwen-readalong (windows-latest)`; `qwen-readalong (macos-latest)` |
| native audio integrity | `audio-integrity (ubuntu-24.04)`; `audio-integrity (windows-latest)`; `audio-integrity (macos-latest)` |
| reports checkJs | `reports-checkjs (ubuntu-latest)`; `reports-checkjs (windows-latest)`; `reports-checkjs (macos-latest)` |
| listening lab | `listening-lab (ubuntu-24.04)`; `listening-lab (windows-latest)`; `listening-lab (macos-latest)` |
| macOS subprocess ownership | `native-prerequisites` |
| rolling Ubuntu qualification | `rolling / unit (1/6)` through `rolling / unit (6/6)`; `rolling / derived-files`; `rolling / test` |

The main workflow runs on every PR: **`test` and `windows-owner` are safe
unconditional required-check choices**. `test` already requires all six
shards, derived-files and windows-portability. Its exact-inventory report
aggregation remains intact. Six OS-matrix workflows now have distinct names,
avoiding the previous ambiguous `native (OS)` contexts.

Specialized workflows retain their PR path filters. Their checks above run
for every merge group but must not be made globally required for every PR
without separately removing those filters or adding always-running gates;
otherwise GitHub can leave a path-skipped required workflow pending. The
skipped `rolling / windows-owner` and `rolling / windows-portability` are not
required-check candidates. No settings, rulesets or branch protection are
changed by this implementation.

## Reproducible controls

The ordinary unit suite and the dependency-free derived-files job both run
the provenance/merge and serialization controls. The calendar control runs in
the ordinary unit suite. Queue configuration has its own standard-library
regressions. Fixtures contain synthetic source/goals/results only.

- Two branches from one synthetic Git base each add a test to a separate
  existing module and an application change. Real updater/audit CLIs run on
  both before commit, including well-separated additions to the same module.
  Both orders of ordinary merges and squash merges, with no driver
  or hooks configured, retain the original audit and pass both --check CLIs.
- A single-branch, no-local clone of squashed main proves the original branch
  commit is absent, then regenerates after an unrelated dirty app edit without
  changing the audit. The unchanged --check accepts the commit-tree proof.
- A genuinely dirty feature-origin audit is committed and regenerated from a
  clean feature tree, then squash-merged and checked in a main-only clone that
  cannot resolve the recorded feature commit. Clean proofless legacy records
  likewise upgrade during regeneration without changing check acceptance.
- A body/goals change regenerates, while stale body and changed-harness
  controls still reject. The contradictory-clean regression deletes the
  existing audit before generating a deliberately dirty record, then forges
  only its clean flag; retaining the prior valid origin would not test that
  rejection case.
- A mocked calendar jump from 2000-01-01 to 2099-12-31 leaves both results
  files byte-identical, without mocking indexed rows or comparisons.
- Before the production edits: six focused new tests ran, five failed and the
  body-change positive/rejection control passed (zero errors/skips).

Full-tree clone experiments, platform counts and readiness qualification are
recorded in the PR after execution. A completed focused suite is not a claim
that full release readiness or hosted checks passed.
