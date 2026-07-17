# Alexandria Follow-up Improvement Plan

Status: **approved — Phases 1–7 complete; Phase 8 pending**
Created: 2026-07-17  
Baseline: nine-phase LoRA/Voice Lab improvement program merged through PR #149

## Objective

Validate the merged Voice Lab workflow on real audio, then improve its preflight,
run records, health visibility, diagnostics, evaluation review, stale-client
detection, and launcher integration coverage without weakening GPU, storage,
checkpoint, retry, or recovery safety nets.

## Operating rules

- Implementation starts only after explicit approval of this plan.
- Use one focused branch and pull request per phase.
- Update this file after every significant step: mark status, record evidence,
  note deviations, and identify the next action.
- Preserve the global GPU lock, VRAM checks, bounded candidate retention,
  checkpoint journals/backups, atomic writes, and retry policies.
- Never expose API keys, authentication values, private paths, or raw environment
  variables in UI diagnostics or reports.
- Prefer existing architecture: vanilla JavaScript, FastAPI routers,
  `process_state`, flat JSON, run history, and Pinokio's native log/session model.
- A phase is complete only when its stated behavior is verified. Skipped tests
  and unverified hardware paths must be reported explicitly.

## Progress ledger

| Phase | Work | Status | PR | Verification |
|---|---|---|---|---|
| 1 | Real end-to-end Voice Lab validation | Complete | #150 | Real ROCm run, paired evaluation, promotion/rollback, release verifier |
| 2 | Voice Lab preflight report | Complete | #151 | Shared preview/start decision, stale-preview gate, real ROCm probe |
| 3 | Persistent pipeline run summaries | Complete | #152 | Atomic stage records, interruption recovery, bounded retention |
| 4 | Pipeline health dashboard | Complete | #153 | Read-only health summary, recovery precedence, no new poller, 326 unit tests |
| 5 | Sanitized diagnostics export | Complete | #155 | Recursive redaction, bounded/versioned bundle, secret/URL/home-path tests |
| 6 | Evaluation history and human review | Complete | #156 | Evidence-bound append-only reviews, true blind A/B proxy, stale rejection, 346 unit tests |
| 7 | Stale-browser build detection | Complete | #157 | Serve-time build stamp, poll-piggybacked banner, no new timer, no auto-refresh |
| 8 | Launcher supervisor integration tests | Pending | — | — |

Allowed status values: `Pending`, `In progress`, `Blocked`, `Complete`.

## Phase 1 — Real end-to-end Voice Lab validation

Status: `Complete`
Branch / PR: `agent/real-voicelab-validation` / #150
Completed: Validated dedup, isolated one- and two-epoch training, production and
candidate evaluation, paired probe audio, evidence hashes, promotion, rollback,
and release gates on the configured ROCm GPU.
Verified behavior: The RX 9070 XT completed every exercised ML stage; the
candidate ranked above production; promotion installed its recorded hash; and
rollback restored the exact original hash without touching production storage.
Tests run (including skips): 36 focused tests passed. The release verifier passed
311 unit tests and 70 quick API checks; its 12 full-mode GPU/TTS/LLM checks were
explicitly skipped. An initial test invocation from the repository root ran no
tests and produced four import errors; it was corrected by running from `app/`.
Real artifacts or reports: `VOICE_LAB_VALIDATION_2026-07-17.md`. Temporary heavy
artifacts were removed after their hashes and results were recorded.
Deviations / discoveries: The smallest already-prepared real archive was used
instead of deriving another archive from the read-only audiobook originals. A
one-epoch candidate is necessarily identical to production and is correctly
deduplicated, so a bounded two-epoch run was required for paired comparison.
SpeechBrain also warns on canonical `cuda` and falls back to device 0; execution
still remained on ROCm, but explicit device formatting should be considered in
the preflight phase.
Remaining: Publish the Phase 1 branch/PR. Live forced-interruption recovery was
not induced on real weights; the corresponding focused recovery tests passed.
Next action: Commit and publish Phase 1, then begin the shared read-only preflight
builder in Phase 2.

Purpose: exercise the actual ROCm path that unit and quick API tests cannot.

Plan:

1. Select the smallest suitable real voice dataset from
   `/home/fakemitch/audiobooks/`; inspect it read-only before choosing it.
2. Record baseline build, interpreter, Torch/ROCm versions, GPU, free VRAM,
   free disk, and relevant Voice Lab configuration.
3. Copy or derive a minimal test input in a temporary directory. Do not alter
   the original audiobook data.
4. Run quality → dedup → one-epoch training with bounded candidates → evaluation.
5. Verify canonical device propagation through dedup, batch trainer,
   `train_lora.py`, and evaluator.
6. Verify version-2 evidence hashes, candidate summary, paired playback files,
   promotion, rollback, and interrupted-state recovery where safely testable.
7. Save a concise validation report and remove temporary heavyweight artifacts
   after confirming they are not needed for failure analysis.

Success criteria:

- One real adapter completes the intended stages on the configured ROCm GPU.
- Evidence and manifest records refer to the correct checkpoint and probe files.
- Candidate comparison is playable and promotion/rollback preserves production.
- Any failure is reproduced, classified, and retained with enough evidence to
  fix; it is not silently called a pass.

Verification:

- Focused unit tests for any defect fixed during validation.
- Full release verifier.
- Recorded real-run commands, stage outcomes, durations, and artifact locations.

## Phase 2 — Voice Lab preflight report

Status: `Complete`
Branch / PR: `agent/voicelab-preflight` / #151
Completed: Added one canonical read-only preflight builder, a sanitized preview
endpoint, start-time recomputation with stable preview identity, and a visible UI
review/confirmation panel.
Verified behavior: A real local probe reported 51 narrator folders, 451 source
ZIPs, 75 deduplicated ZIPs, the RX 9070 XT, 15.7 GB free VRAM, 23.7 GB free
disk, and all dependencies ready without exposing configured absolute paths.
Tests run (including skips): Runtime/frontend suites passed 70 tests. Release
verification passed 314 unit tests and 70 quick API checks; 12 full-mode checks
were explicitly skipped. The first release attempt stopped at expected test
inventory drift; the inventory was regenerated and the verifier then passed.
Deviations / discoveries: One-epoch candidate retention now produces an advisory
warning because that candidate necessarily duplicates production. Bare PyTorch
`cuda` remains the canonical ROCm device string; the report also exposes the
resolved AMD GPU so the backend is unambiguous.
Remaining: Publish and merge the Phase 2 PR.
Next action: Commit and publish Phase 2, then begin persistent run summaries.

Purpose: show whether a requested run is safe and runnable before claiming the
GPU or starting a subprocess.

Plan:

1. Add one pure preflight builder shared by the preview endpoint and start path.
2. Report requested stages, resolved dataset root/count, interpreter,
   dependency readiness, canonical device, GPU visibility, free VRAM/disk,
   profiler model, training settings, and expected output locations.
3. Classify findings as blockers or advisory warnings using explicit rules.
4. Recompute the preflight at start time to reject stale previews.
5. Add a Voice Lab UI panel requiring the user to review blockers/warnings.

Success criteria:

- Preview is read-only and does not claim the GPU or mutate configuration.
- Start uses the same decision function and refuses genuine blockers.
- Missing optional stages/dependencies do not block unrelated selected stages.
- Secrets and unnecessary absolute paths are absent from the response.

Verification:

- Tests for missing interpreter, wrong Torch build, low disk/VRAM, CPU fallback,
  stage-specific dependencies, stale preview, and a healthy configuration.
- API contract snapshots and release verifier.

## Phase 3 — Persistent pipeline run summaries

Status: `Complete`
Branch / PR: `agent/voicelab-run-summaries` / #152
Completed: Extended the existing run-history records with atomic Voice Lab
request, sanitized preflight, build, stage, dataset/adapter, log-reference,
failure, duration, and next-action summaries. Added startup interruption recovery
and deterministic count/age retention.
Verified behavior: Stage transitions persist independently of the live
`process_state`; startup distinguishes abandoned runs as `interrupted`; active
runs and the newest failure survive pruning; summary-write failures preserve the
previous valid JSON and do not stop pipeline execution.
Tests run (including skips): Focused history/pipeline tests passed. Release
verification passed 318 unit tests and 70 quick API checks; 12 full-mode checks
were explicitly skipped.
Deviations / discoveries: The generic history runner already supplied immutable
run identities and final completed/failed/cancelled status, so Phase 3 extended
that record rather than adding a Voice Lab-only store. Real forced process death
was not induced; startup interruption behavior is covered with persisted records.
Remaining: Publish and merge the Phase 3 PR.
Next action: Commit and publish Phase 3, then build the read-only health summary.

Purpose: make every Voice Lab run diagnosable after the live process ends.

Plan:

1. Extend the existing run-history style rather than create a second tracking
   architecture.
2. Create an immutable run identity and atomically persist initial request,
   sanitized preflight, build identity, and stage plan before execution.
3. Update the record after every stage with timestamps, duration, exit status,
   concise failure classification, datasets/adapters affected, and next action.
4. Mark cancellation and process interruption distinctly from stage failure.
5. Apply bounded count/age retention without deleting the active or latest
   failed record.

Success criteria:

- A crash between stages leaves a valid, useful partial record.
- Completed, failed, cancelled, and interrupted runs are distinguishable.
- Records contain summaries and log references, not unbounded raw logs.
- Retention is deterministic, recoverable where practical, and tested.

Verification:

- Atomic-write failure, interruption, cancellation, multi-stage partial failure,
  successful run, and retention-boundary tests.
- Release verifier.

## Phase 4 — Pipeline health dashboard

Status: `Complete`
Branch / PR: `agent/voicelab-health-dashboard` / #153
Completed: Added one read-only health summary endpoint `GET /api/voicelab/health`
plus a compact Voice Lab dashboard card, both built from the existing Phase 3
run summaries, live `process_state`, checkpoint recovery journals, and runtime
build identity — no new tracking store and no new polling timer.
Verified behavior: The builder is pure and read-only (a dedicated test asserts it
mutates neither the passed state nor the history directory). Recovery-required
status and next action take precedence over ordinary run status. Corrupt history
records are skipped and the summary still builds. The live endpoint returns 200
with the expected keys; on this machine it reported `status: ok` from the last
completed run. The dashboard refreshes on tab open, on each existing
`pollVoicelab` tick while a run is live, and on mid-run page reload — adding no
`setInterval`.
Tests run (including skips): New `test_voicelab_health.py` (7 cases:
idle/running/newest-of-kind/failure-detail/corrupt-history/recovery-precedence/
no-mutation) and a frontend regression assertion — all pass. Release verifier
passed: 326 unit tests, quick API suite 70 passed / 0 failed / 12 skipped (the
12 require full-mode GPU/TTS/LLM). API contract and unit-test inventory snapshots
were regenerated for the new route.
Real artifacts or reports: none beyond code; dashboard is read-only over existing
records so no GPU run was required.
Deviations / discoveries: For the idle (not running, no recovery) next action the
dashboard uses the newest run's next action rather than the plan's
failure-then-success ordering — a later success should not be overridden by an
older failure, and the truly-unresolved case (interrupted checkpoint swaps)
is already handled by the higher-precedence recovery status. `last_success` and
`last_failure` are still surfaced separately regardless. The recovery scan was
consolidated into one shared `list_adapters_needing_recovery` helper in
`routers/lora.py` (Rule 15) rather than re-deriving the journal check.
Remaining: Open and merge the Phase 4 PR.
Next action: Commit and publish Phase 4, then begin the sanitized diagnostics
export in Phase 5.

Purpose: expose current and recent Voice Lab health without reading terminal
logs manually.

Plan:

1. Build one read-only health summary from `process_state`, persistent run
   summaries, runtime build identity, and checkpoint recovery state.
2. Show active run/stage, elapsed time, selected device, last success, last
   failure, pending recovery, and direct next action.
3. Add a compact Voice Lab dashboard with links to the relevant run report,
   model/candidate view, or recovery control.
4. Keep polling lightweight and reuse the existing polling infrastructure.

Success criteria:

- Dashboard survives missing/corrupt historical records.
- Live state never replaces or mutates the shared `process_state` objects.
- Recovery-required status takes precedence over ordinary success/failure text.
- No new background worker or polling loop is introduced unnecessarily.

Verification:

- Idle, running, succeeded, failed, cancelled, stale-process, corrupt-history,
  and checkpoint-recovery tests.
- Frontend regression tests and release verifier.

## Phase 5 — Sanitized diagnostics export

Status: `Complete`
Branch / PR: `agent/voicelab-diagnostics-export-v2` / #155 (re-based on main;
supersedes #154, which was stacked on #153 and merged into the Phase 4 branch
instead of main, so Phase 5 never reached main until #155)
Completed: Added a pure, self-contained `diagnostics.py` (recursive redaction +
bounded assembly, no app imports) and a read-only `GET /api/voicelab/diagnostics`
endpoint that gathers runtime/build, a non-sensitive config summary, Voice Lab
config, the Phase 4 health summary, the latest run summary, and log-file
identifiers (never contents). Added Copy/Download Diagnostics actions in the
Voice Lab tab.
Verified behavior: Sensitive keys (api_key/token/password/authorization/cookie/
secret/…) are fully redacted at any nesting depth; URL user:pass credentials and
inline `key=secret` text are scrubbed; home paths are collapsed to `~`; long
strings/lists are truncated with explicit markers; and an oversized bundle drops
its largest sections until it fits the total byte budget. The config summary is a
whitelist that never includes `base_url`, `api_key`, or the SSH host. A live
endpoint smoke confirmed 200 with no home path present in the serialized bundle.
Generation is read-only and works after a failed run (latest_run may be a failed
record). Full logs are intentionally excluded — the bundle points at Pinokio's
Get Help / session bundle.
Tests run (including skips): New `test_voicelab_diagnostics.py` (12 cases: API
keys, bearer/basic auth, URL creds, inline creds, nested secrets, home paths,
string/list truncation, Unicode preserved, non-string scalars, versioned wrapper,
none/malformed section, oversized-drop) + a frontend regression assertion — all
pass. Release verifier passed: 339 unit tests, quick API 70 passed / 0 failed /
12 skipped (require full-mode GPU/TTS/LLM). API contract + unit-test inventory
snapshots regenerated for the new route.
Real artifacts or reports: none beyond code; read-only over existing records.
Deviations / discoveries: `diagnostics.py` is intentionally app-import-free so it
is fully unit-testable and cannot mutate app state; the endpoint gathers raw
sections and hands them in. The one-line credential scrub in
`verify_release.get_concise_error` was left as-is — it answers a different
question (bounded single-line error text for the release tool) than the
structured recursive bundle redaction, so it is not a Rule 15 duplicate.
Remaining: Merge #155 into main. (Lesson: do not stack a phase PR on another
phase branch — base every phase PR on main to avoid a merge landing on the wrong
branch, as happened with #154.)
Next action: Commit and publish Phase 5, then begin evaluation history and human
review in Phase 6.

Purpose: produce a useful support bundle without leaking secrets or flooding
reports with raw data.

Plan:

1. Define a versioned diagnostics schema containing runtime/build versions,
   sanitized device/config summary, current health, latest run summary, and
   relevant log file identifiers.
2. Centralize recursive redaction for keys, URLs, credentials, home paths, and
   environment-derived secrets.
3. Enforce per-section and total size limits; truncate with explicit markers.
4. Add Copy Diagnostics and Download Diagnostics actions in Voice Lab.
5. Refer users to Pinokio's native Get Help/session bundle for full logs rather
   than duplicating that supervisor feature.

Success criteria:

- Known and nested secret forms are removed.
- Output is deterministic, bounded, versioned, and useful offline.
- Diagnostics generation is read-only and works after a failed run.

Verification:

- Tests with API keys, bearer/basic auth, credentials in URLs, nested secrets,
  local paths, oversized logs, malformed records, and Unicode.
- Security-focused review, API/frontend tests, and release verifier.

## Phase 6 — Evaluation history and human review

Status: `Complete`
Branch / PR: `agent/voicelab-evaluation-review` / #156 (based on main)
Completed: Added a pure, append-only `evaluation_reviews.py` (versioned per-adapter
store + blind A/B sessions) bound to the existing version-2 evaluation evidence
hashes and build identity, plus five read-only/CPU-only routes on `routers/lora.py`
(open session, stream blind sample, submit decision, list history, cleanup) and a
Voice Lab / LoRA-models UI (Blind review, History, Clear). Consolidated evidence
loading into one `_load_candidate_comparison_full` shared by the advisory
comparison endpoint and the review-session builder (Rule 15).
Verified behavior: Blind labels never disclose identity before submission — the
client payload carries only probe id/text, and audio is streamed through a
session/label-scoped proxy (`.../audio/A/<probe>`) that resolves the side
server-side, so the candidate's real `/candidates/` URL never reaches the browser.
A submit is rejected (409, no record written) when the evidence fingerprint at
submit differs from session open (retrain/promote/rollback). Human preference and
the automated recommendation are stored and shown as separate fields; the module
structurally cannot promote (an AST test asserts it imports no promotion code and
calls nothing named promote). History is bounded (newest 50, append-only, oldest
aged out) and removable with count/bytes reporting. A path-traversal test confirms
the audio proxy refuses any stored path outside the models dir.
Tests run (including skips): New `test_evaluation_reviews.py` (16 module cases) and
`test_lora_review_integration.py` (3 router-level cases over real evidence,
including the blind-URL leak that an initial version missed and the proxy guard) +
a frontend regression assertion — all pass. Release verifier passed: 346 unit
tests, quick API 70 passed / 0 failed / 12 skipped (require full-mode GPU/TTS/LLM).
API contract + unit-test inventory snapshots regenerated for the five new routes.
Real artifacts or reports: none beyond code; CPU-only, no GPU run required.
Deviations / discoveries: The first design returned the raw probe audio URLs in the
session; a test caught that the candidate URL contains `/candidates/` and thus
leaked identity in "blind" mode. Fixed by streaming both samples through an
identity-neutral proxy endpoint and keeping the label->path map server-side only.
Remaining: Merge #156 into main. (Phase 5 #155 is independent and can merge in any
order; this branch also marks it #155 so the ledger does not regress on merge.)
Next action: Commit and publish Phase 6, then begin stale-browser build detection
in Phase 7.

Purpose: retain bounded decision evidence and combine automated scores with
human listening judgments.

Plan:

1. Define a versioned, append-only evaluation-decision record bound to existing
   evidence hashes and build identity.
2. Preserve a bounded history of evaluation outcomes across reruns, promotion,
   and rollback without copying large checkpoint files.
3. Add optional blind A/B presentation with stable randomized labels per review.
4. Store rating, preference, and short notes only after validating that the
   referenced evidence is still current.
5. Reveal production/candidate identities after the decision and keep automated
   recommendation separate from human preference.
6. Add explicit history cleanup with count/disk reporting.

Success criteria:

- Stale or changed evidence cannot receive a new rating.
- Blind labels do not disclose identity before submission.
- Human feedback never automatically promotes a checkpoint.
- History is bounded, attributable to evidence, and safely removable.

Verification:

- Hash mismatch, replay/stale submission, label stability, malformed notes,
  promotion/rollback history, concurrent writes, and retention tests.
- Frontend tests and release verifier.

## Phase 7 — Stale-browser build detection

Status: `Complete`
Branch / PR: `agent/voicelab-stale-build` / #157 (based on main)
Completed: The `/` route now stamps the served page with the current build
(`get_runtime_info` short_revision) into a `<meta name="app-build">` tag. The
frontend captures it once and, inside the existing `/api/system/stats` poll,
compares it to the live backend revision — showing a dismissible, non-destructive
"newer version available / Reload now" banner on a real mismatch. No new timer;
no auto-refresh.
Verified behavior: Served build equals the stats build on a fresh load (no false
positive). Unknown build on either side is treated as informational (empty stamp
when the revision is unavailable; the literal placeholder when opened as a raw
file → detection disabled, never a false warning). The banner only appears on a
genuine mismatch and offers an explicit Reload; unsaved work is never discarded.
Tests run (including skips): Two backend serve-time tests (build stamped; empty
when unavailable) and a frontend regression (meta/banner/PAGE_BUILD/checkStaleBuild
wired, called from the stats poll, no new setInterval) — all pass. Release verifier
passed: 362 unit tests, quick API 70 passed / 0 failed / 12 skipped. Unit-test
inventory regenerated; API contract unchanged (same route, only response class).
Real artifacts or reports: none beyond code.
Deviations / discoveries: A first version replaced every `__APP_BUILD__`
occurrence, which also rewrote the JS guard literal (`PAGE_BUILD !== '__APP_BUILD__'`)
and would have permanently disabled detection on served pages. Caught before merge;
fixed by stamping only the meta-tag occurrence, with a regression test asserting
the JS placeholder literal survives.
Remaining: Merge #157 into main.
Next action: Commit and publish Phase 7, then begin launcher supervisor integration
tests in Phase 8.

Purpose: identify a browser tab serving old frontend code after a backend update.

Plan:

1. Embed a frontend build identifier when serving the page, using the same
   runtime build source as the backend.
2. Compare it periodically with `/api/system/stats` using existing system polling.
3. Show a non-destructive refresh-required banner when identifiers differ.
4. Never auto-refresh during active edits or work; provide an explicit action.
5. Treat unknown/unavailable build identifiers as informational, not mismatch.

Success criteria:

- Matching, mismatching, and unavailable identifiers behave distinctly.
- The comparison adds no new polling timer.
- Unsaved user work is never discarded automatically.

Verification:

- Frontend rendering tests for match/mismatch/unknown and active-work states.
- Runtime/API tests and release verifier.

## Phase 8 — Launcher supervisor integration tests

Purpose: verify real Pinokio-style readiness and failure parsing rather than
only checking launcher source strings.

Plan:

1. Create a small isolated test harness that runs fake Python servers/processes
   emitting the exact readiness and failure signals used by `start.js`.
2. Cover successful dynamic URL capture, import failure, traceback, FastAPI
   startup failure, address-in-use failure, and early clean exit.
3. Verify failure patterns cannot match normal healthy output accidentally.
4. Keep the real launcher structure locked to the Pinokio server example:
   daemon, relative path, dynamic port, `done: true`, and
   `local.set` from `input.event[1]`.
5. Document how to run the harness without starting a second Alexandria server.

Success criteria:

- Every supported failure exits visibly before URL publication.
- Healthy readiness captures the full URL and keeps the daemon alive.
- Tests do not bind fixed ports or depend on an installed GPU/model.
- Pinokio launcher guidance and exit checklist remain satisfied.

Verification:

- Harness test matrix on available platforms/CI.
- Existing launcher contract tests, JavaScript load check, and release verifier.

## Execution order and dependencies

Execute phases in numerical order. Phase 1 supplies real evidence for the
preflight design. Phase 2 feeds Phase 3's persisted request record. Phase 3 is
the source for Phase 4 and Phase 5. Phase 6 relies on the existing version-2
evaluation evidence but is independent of diagnostics export. Phase 7 reuses
runtime visibility. Phase 8 is last because it validates the final launcher and
documentation surface.

If Phase 1 finds a correctness defect, fix it in a narrowly scoped PR before
continuing and record that inserted PR in the ledger rather than folding an
unrelated fix into a later phase.

## Per-phase update template

After each significant step, append or update the phase with:

```text
Status:
Branch / PR:
Completed:
Verified behavior:
Tests run (including skips):
Real artifacts or reports:
Deviations / discoveries:
Remaining:
Next action:
```

## Final completion criteria

- All eight ledger rows are `Complete`, or an explicitly approved deferral is
  recorded with its reason and risk.
- All PRs are merged and `main` passes the release verifier.
- At least one real ROCm Voice Lab run has a retained validation report.
- Diagnostics are demonstrably sanitized and bounded.
- No active recovery journal, temporary validation process, or uncommitted
  project change remains.
