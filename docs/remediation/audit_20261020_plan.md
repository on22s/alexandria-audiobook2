# 2026-10-20 source-review remediation plan

Goal **3.5** · [Execution tracker #1366](https://github.com/on22s/alexandria-audiobook2/issues/1366) · **OPEN**

Reviewed main: `4c31e52c39cb3a1c81cc4cac03bf4ae9d5e2a96d`. This plan covers 41 findings (40 confirmed, one evidence-backed investigation), 16 enhancement observations and 13 test gaps. It creates **58 child issues**: 41 bug/investigation tasks and 17 enhancements. The seventeenth enhancement groups the two independent experiment-reporting gaps. The other test gaps belong to their related fix or test-hardening task.

**Baseline, 2026-10-20:** 0 of 58 execution tasks complete. Filing issues and this plan are tracking work, not completed fixes. The original review checked 1,903 source/test files and recorded 192 passing targeted tests; that is not a complete release or GPU/native-platform pass.

## Working contract

- Execute batches in the order below, respecting dependencies. Use one focused PR per batch; split further when a shared-layer change needs separate review. Do not stack unmerged implementation branches.
- Fetch current main immediately before creating each batch worktree. Reproduce the concrete failure before editing; a closed older issue, a passing happy-path test or an automated model verdict does not establish a fix.
- Every task needs a linked PR and exact tested commit, or an evidence-backed duplicate/refutation/obsolete disposition. The plausible cross-worker reservation issue must first be reproduced outside the already-protected active-file lock.
- Preserve GPU admission/VRAM checks, atomic writes, ownership fences, retries, literal/source fidelity, cancellation and existing compatibility. Do not improve speed by weakening these mechanisms.
- Add the real regression cases from the issue. Keep accepted/unaffected behavior covered. Record passed, skipped, blocked and unrun checks separately; unexpected skips fail the gate.
- Run `./ready.sh` before committing, stage only required regenerated outputs and use exact published-head CI before declaring a batch complete. Run required native/GPU/model/listening checks through the existing queue; if a required check cannot run, keep its task open. Do not claim them from CPU fixtures.
- CPU/Node fixtures can run while the GPU queue is busy. Never edit a live checkout, change a running script in place, bypass the shared GPU lock or preempt queued work.
- Close only the issues actually accepted by the merged batch. Historical-driver tasks can be completed by an explicit archival/current-runner boundary where their issue calls for it; other enhancements are not silently dropped. A new deferral needs the owner's agreement and a recorded reason.
- This plan tracks only the retained review scope. Raw unverified model flags are not additional committed bugs. Do not add speculative rewrites, retraining campaigns or bulk voice promotion.

## Batch queue

### Batch 01: Protect saved data and voice publication

**Dependencies:** none; start here. **Tasks:** 4. **Status:** accepted and merged (four tasks). **PR:** [#1426](https://github.com/on22s/alexandria-audiobook2/pull/1426).

**Batch acceptance:** Prove malformed-library byte preservation, conflicting two-process dataset mutation refusal, unchanged unrelated saved names, and affected-only audio invalidation. Investigate F031 with A queued for a backward pass while the foreign worker reviews B; do not mistake its existing active-file lock for the missing whole-batch reservation.

- [x] **F001** [#1367](https://github.com/on22s/alexandria-audiobook2/issues/1367) — Dataset project mutation guards ignore generation owned by another server process
- [x] **F002** [#1368](https://github.com/on22s/alexandria-audiobook2/issues/1368) — A routine voice-library mutation permanently replaces an unreadable existing library with an empty one
- [x] **F003** [#1369](https://github.com/on22s/alexandria-audiobook2/issues/1369) — Applying suggested voices leaves completed chunks attached to audio from the previous voice
- [x] **F031** [#1370](https://github.com/on22s/alexandria-audiobook2/issues/1370) — Saved-book overwrite protection does not notice batch review in another worker Investigation; not a confirmed active-file overwrite.

### Batch 02: Repair LLM configuration and resumable generation

**Dependencies:** Batch 01 (accepted). **Tasks:** 7. **Status:** accepted and merged. **PR:** [#1428](https://github.com/on22s/alexandria-audiobook2/pull/1428).

**Batch acceptance:** Exercise failure → checkpoint → corrected retry, profile-setting changes, stable resume prompts, cancellation while waiting for a manual reply, and structured-output opt-out preservation. For documented hosted providers, align the supported contract without relaxing arbitrary-host or credential safety policy.

- [x] **F004** [#1371](https://github.com/on22s/alexandria-audiobook2/issues/1371) — Malformed field types enter validated single-pass checkpoints and permanently block resume
- [x] **F017** [#1372](https://github.com/on22s/alexandria-audiobook2/issues/1372) — Cancelling a report explanation cannot stop a manual-transport request
- [x] **F019** [#1373](https://github.com/on22s/alexandria-audiobook2/issues/1373) — Saving unrelated Setup settings resets a saved structured-output opt-out
- [x] **F020** [#1374](https://github.com/on22s/alexandria-audiobook2/issues/1374) — The Remote profile cannot save or test the hosted providers advertised by Setup documentation
- [x] **F021** [#1375](https://github.com/on22s/alexandria-audiobook2/issues/1375) — Changing the profile reasoning-effort setting silently reuses generation checkpoints from the old setting
- [x] **F022** [#1376](https://github.com/on22s/alexandria-audiobook2/issues/1376) — Interrupted default three-pass runs resume with a different attribution-roster order than uninterrupted runs.
- [x] **F023** [#1377](https://github.com/on22s/alexandria-audiobook2/issues/1377) — Diagnostic instruction failures are never retried when resuming the same run

### Batch 03: Preserve rendered text, voice identity, and timing

**Dependencies:** Batch01, Batch02 (accepted). **Tasks:** 7. **Status:** all seven tasks accepted and merged. **PR:** [#1431](https://github.com/on22s/alexandria-audiobook2/pull/1431), [#1433](https://github.com/on22s/alexandria-audiobook2/pull/1433).

**Batch acceptance:** Exercise single/native/external/design/clone dispatch using deterministic CPU callbacks and valid synthetic PCM; preserve once-only pronunciation and reference alias policy. Check effective take behavior, complete-sample timing, and repeated stalled-request resource lifetime. Queue any required real-model/audio confirmation separately.

- [x] **F024** [#1378](https://github.com/on22s/alexandria-audiobook2/issues/1378) — Different take reuses the original seed for normally assigned voices Includes T01.
- [x] **F025** [#1379](https://github.com/on22s/alexandria-audiobook2/issues/1379) — Timeline rounding accumulates drift against the encoded PCM
- [x] **F026** [#1380](https://github.com/on22s/alexandria-audiobook2/issues/1380) — LoRA and design rendering apply pronunciation substitutions more than once Includes T06.
- [x] **F027** [#1381](https://github.com/on22s/alexandria-audiobook2/issues/1381) — External clone rendering loses references after adapter rename Includes T03.
- [x] **F028** [#1382](https://github.com/on22s/alexandria-audiobook2/issues/1382) — Timed-out external batches retain unbounded executor threads Includes T02.
- [x] **F029** [#1383](https://github.com/on22s/alexandria-audiobook2/issues/1383) — Renamed adapters lose voice-drift measurements despite valid serving aliases Includes T04.
- [x] **F030** [#1384](https://github.com/on22s/alexandria-audiobook2/issues/1384) — Relocated runtime data makes every built-in voice reference fail drift admission

### Batch 04: Repair preparation and frontend recovery

**Dependencies:** Batch 01 (accepted). **Tasks:** 5. **Status:** accepted and merged. **PR:** [#1429](https://github.com/on22s/alexandria-audiobook2/pull/1429).

**Batch acceptance:** Exercise unchanged-file retry/resume after consuming the preparation token, malformed-ID repair guidance, failed upload state recovery and parameterless training reattachment. Keep unrelated/newer selections and cancellation state intact.

- [x] **F018** [#1385](https://github.com/on22s/alexandria-audiobook2/issues/1385) — Starting preparation consumes its preview token, so retrying or resuming the unchanged file fails
- [x] **F038** [#1386](https://github.com/on22s/alexandria-audiobook2/issues/1386) — Pronunciation preview crashes for saved chunks missing an ID instead of returning the Editor recovery message
- [x] **F039** [#1387](https://github.com/on22s/alexandria-audiobook2/issues/1387) — Batch upload failures leave the status spinner claiming upload is still running Includes T05.
- [x] **F040** [#1388](https://github.com/on22s/alexandria-audiobook2/issues/1388) — Reattached training announces an undefined epoch total to screen readers Includes T07.
- [x] **E07** [#1389](https://github.com/on22s/alexandria-audiobook2/issues/1389) — LoRA uploads duplicate error parsing instead of using the shared HTTP parser

### Batch 05: Bind experiment resume and caches to their inputs

**Dependencies:** Batch 02. **Tasks:** 5. **Status:** accepted. **PR:** [#1430](https://github.com/on22s/alexandria-audiobook2/pull/1430).

**Batch acceptance:** Change seed, source bytes, segmentation, adapter bytes and corpus identity independently. Require the correct cache invalidation or a clear refusal; transport failures must remain retryable and cannot become completed records.

- [x] **F006** [#1390](https://github.com/on22s/alexandria-audiobook2/issues/1390) — Cascade phase resume binds cheap answers to mutable numeric segment positions without verifying the input identity.
- [x] **F011** [#1391](https://github.com/on22s/alexandria-audiobook2/issues/1391) — A temporary attribution transport failure permanently poisons the narrator-prior checkpoint.
- [x] **F012** [#1392](https://github.com/on22s/alexandria-audiobook2/issues/1392) — Reference-intervention reruns can train stale cached datasets while evaluating a changed source ZIP.
- [x] **F014** [#1393](https://github.com/on22s/alexandria-audiobook2/issues/1393) — Shipping readiness silently pools base/LoRA results from incompatible runs and model stacks.
- [x] **F016** [#1394](https://github.com/on22s/alexandria-audiobook2/issues/1394) — Voice-drift runs reuse cached audio across changed seeds, source text, and adapter weights while reporting the new run settings.

### Batch 06: Make training data and measurement gates trustworthy

**Dependencies:** Batch05 (accepted). **Tasks:** 8. **Status:** accepted. **PR:** [#1432](https://github.com/on22s/alexandria-audiobook2/pull/1432).

**Batch acceptance:** Use known-good, known-bad and boundary fixtures. Require comparable paired denominators, alias-aware scoring, corpus pseudo-speaker filtering, stable large-count statistics, true representative reference choice and gate refusal when requested generations fail. An identical served model cannot be reported as an adapter effect.

- [x] **F005** [#1395](https://github.com/on22s/alexandria-audiobook2/issues/1395) — The tight-dataset experiment writes the first selected clip as reference instead of the promised within-arm centroid representative.
- [x] **F008** [#1396](https://github.com/on22s/alexandria-audiobook2/issues/1396) — The documented --no-scale base-only mode still reports two identical served-model runs as an adapter comparison.
- [x] **F009** [#1397](https://github.com/on22s/alexandria-audiobook2/issues/1397) — LUAR overrides discard fixture aliases while retaining alias-aware baseline scores, undercounting correct repairs.
- [x] **F010** [#1398](https://github.com/on22s/alexandria-audiobook2/issues/1398) — Both PDNC training builders teach corpus pseudo-speakers as real attribution labels.
- [x] **F013** [#1399](https://github.com/on22s/alexandria-audiobook2/issues/1399) — The exact sign test overflows on supported corpus-sized discordant counts.
- [x] **F015** [#1400](https://github.com/on22s/alexandria-audiobook2/issues/1400) — The adapter stop gate can pass an adapter after most requested generations fail.
- [x] **E09** [#1401](https://github.com/on22s/alexandria-audiobook2/issues/1401) — Current candidates with a legitimately empty plain_already_works list are described as missing the third state.
- [x] **E17** [#1402](https://github.com/on22s/alexandria-audiobook2/issues/1402) — Preserve paired-row coverage and transport failures in experiment reports Includes T08, T09.

### Batch 07: Restore diagnostic CLI and provenance contracts

**Dependencies:** Batch02, Batch06 (accepted). **Tasks:**7. **Status:** combined release passed; new exact-head CI and merge pending. **PR:** [#1447](https://github.com/on22s/alexandria-audiobook2/pull/1447).

**Batch acceptance:** Run supported CLI initialization and minimal one-item cases without model inference. Verify obsolete callback/constructor signatures, empty strata, perfect-result summaries and failed patch capture. Provenance must distinguish saved, partial and failed evidence; never advertise an empty patch as reproducible code.

- [ ] **F007** [#1403](https://github.com/on22s/alexandria-audiobook2/issues/1403) — The per-line character-traits experiment installs a callback with an obsolete signature and crashes on accepted attribution.
- [ ] **F033** [#1404](https://github.com/on22s/alexandria-audiobook2/issues/1404) — Requesting one pitch-separation adapter divides by zero when the manifest has multiple adapters
- [ ] **F034** [#1405](https://github.com/on22s/alexandria-audiobook2/issues/1405) — Respelling-selectivity reports crash when all valid paired terms fall in one stratum.
- [ ] **F035** [#1406](https://github.com/on22s/alexandria-audiobook2/issues/1406) — A valid perfect-attribution result crashes the roster-order CLI while printing its summary.
- [ ] **F036** [#1407](https://github.com/on22s/alexandria-audiobook2/issues/1407) — Two optional three-pass diagnostics still use the removed presegment_quotes constructor argument
- [ ] **F037** [#1408](https://github.com/on22s/alexandria-audiobook2/issues/1408) — The two-by-two attribution experiment crashes before loading inputs because REPO is assigned after its first use
- [ ] **F041** [#1409](https://github.com/on22s/alexandria-audiobook2/issues/1409) — Dirty-run provenance reports a saved reproduction patch even when git diff fails

### Batch 08: Improve API and export responsiveness

**Dependencies:** Batch 01, Batch 03, Batch 04. **Tasks:** 5. **Status:** not started. **PR:** not opened.

**Batch acceptance:** Measure cold/warm API, preview and selected-chapter behavior on a fixed workload, with event-loop responsiveness and peak PCM/memory observed separately. Preserve output, ownership and cancellation parity. Any GPU-slot change needs proof that the path is CPU-only; do not loosen the safety policy by assumption.

- [ ] **E01** [#1410](https://github.com/on22s/alexandria-audiobook2/issues/1410) — Voice design save/delete acquire blocking file locks and copy complete audio files on the API event loop
- [ ] **E02** [#1411](https://github.com/on22s/alexandria-audiobook2/issues/1411) — Word timing advertised as CPU-only reserves the global GPU task slot
- [ ] **E05** [#1412](https://github.com/on22s/alexandria-audiobook2/issues/1412) — Filename previews decode every complete audio file
- [ ] **E06** [#1413](https://github.com/on22s/alexandria-audiobook2/issues/1413) — Cold chapter export retains the entire book PCM even for one selected chapter
- [ ] **E10** [#1414](https://github.com/on22s/alexandria-audiobook2/issues/1414) — Separate complete retention maintenance from the UI listing cap

### Batch 09: Harden diagnostics and regression tests

**Dependencies:** Batch 05, Batch 06. **Tasks:** 7. **Status:** not started. **PR:** not opened.

**Batch acceptance:** Use mutation-sensitive assertions: wrong empty API names, an unreaped dead lock holder, forbidden chunk changes and zeroed decoded speech must make the relevant tests fail. Restore correct behavior afterward. Check deleted-artifact refusal, telemetry distinctions, temporary cleanup, ETA denominators and post-purge failure semantics without deleting real assets.

- [ ] **F032** [#1415](https://github.com/on22s/alexandria-audiobook2/issues/1415) — The artifact-loss guard reports success when a measured artifact is deleted entirely.
- [ ] **E04** [#1416](https://github.com/on22s/alexandria-audiobook2/issues/1416) — Unexpected convention-detector errors are labelled as ordinary no-convention telemetry.
- [ ] **E08** [#1417](https://github.com/on22s/alexandria-audiobook2/issues/1417) — Per-adapter extracted WAV temporary directories persist after analysis.
- [ ] **E11** [#1418](https://github.com/on22s/alexandria-audiobook2/issues/1418) — Use a consistent completed-attempt denominator for batch ETA
- [ ] **E12** [#1419](https://github.com/on22s/alexandria-audiobook2/issues/1419) — Remove unused mtime cache state
- [ ] **E13** [#1420](https://github.com/on22s/alexandria-audiobook2/issues/1420) — Distinguish post-purge verification errors from mutation-free refusal
- [ ] **E16** [#1421](https://github.com/on22s/alexandria-audiobook2/issues/1421) — Move unittest.main guards below all test classes for complete direct module runs. Includes T10, T11, T12, T13.

### Batch 10: Document and isolate historical drivers

**Dependencies:** Batch 07, Batch 09. **Tasks:** 3. **Status:** not started. **PR:** not opened.

**Batch acceptance:** Keep dated evidence and historical logs intact. Mark historical entry points, record their old runtime/cache assumptions and provide a separately named current runner only when required. Preserve historical artifacts rather than silently treating a modernized rerun as the original measurement.

- [ ] **E03** [#1422](https://github.com/on22s/alexandria-audiobook2/issues/1422) — The historical probe launcher repeats readiness probing but lacks an explicit readiness failure branch.
- [ ] **E14** [#1423](https://github.com/on22s/alexandria-audiobook2/issues/1423) — Keep dated experimental drivers visibly historical and migrate them before reuse
- [ ] **E15** [#1424](https://github.com/on22s/alexandria-audiobook2/issues/1424) — Update the frozen identity verifier cache path before rerunning it against current scoring code

## Batch 01 accepted evidence

[PR #1426](https://github.com/on22s/alexandria-audiobook2/pull/1426) merged on
2026-10-20 as `b50f014092dbed90d782747a763bc483362e19b5`; tested head
`c9a31073979d9787eaa9aacf13156a62fa622cc5` has the identical committed tree.
All six exact-head GitHub gates passed: three unit shards, the aggregate
inventory verifier, native Windows and macOS prerequisites. Native Windows
ran nine resource-lock tests and four existing subprocess-ownership cases.
The local quick release passed 8,546 unit tests and 72 API checks, with
12 expected full-mode exclusions. The combined targeted run passed 238 tests.

All four child issues are closed with their acceptance checklists and evidence.
**Progress: 4/58 accepted; 54 remain.** The F031 case was reproduced with Book A
forward-reviewed while the foreign batch reviews B and A's file lock is free;
its original plausible review classification remains preserved historically.
GPU/model/listening and complete native macOS qualification are unrun and are
not inferred from these control-plane regression checks.

## Batch 02 accepted verification

All seven failures were reproduced before their fixes. Fresh response acceptance
and checkpoint loading now share strict entry-schema checks; a poisoned older
checkpoint retains its valid prefix and regenerates its damaged suffix. Report
explanation cancellation interrupts both manual-client waits, preserves foreign
queue files and releases the task claim only after its worker exits. The
cancellation context has no optional provider dependencies.

An unrelated Setup save retains each named profile's hidden structured-output
opt-out, including during mode switches; explicit edits and profile-bound
credentials keep their existing policy. The documented DeepSeek, OpenAI and
OpenRouter HTTPS API origins pass Save, Preview and diagnostics while lookalike,
userinfo, arbitrary-host and private-network refusals remain covered. Synthetic
HTTP fixtures exercise the real SDK; no production credential or inference was
used. Generation fingerprints now include effective profile reasoning options,
respecting custom-body and explicit CLI precedence.

Duplicate-colored attribution requests use source-order rosters under every
exhaustion policy, matching resumed prompts. Diagnostic instruction resume
retries only failed rows, retains accepted work, and clears resolved fallback
markers and failure records. Repeated failures remain visibly incomplete.

Development checks passed 387 distinct CPU/Node regression methods across the
combined and follow-up runs, including actual CLI recovery and fingerprint
artifacts. Required `./ready.sh` quick verification passed: 8,564 unit tests with no unit
skips, 72 API checks with no failures and 12 explicit full-mode exclusions, and
all compile/inventory/evidence/results/API-contract gates. All six exact-head CI
checks passed, including native Windows ownership, macOS prerequisites and the
complete-inventory aggregate. PR #1428 merged as
`33d3111ab87245c0e75937d31eada4e5e391280c`, tested head
`46ad293ba91f735c703cebefd155dbdeeb9884dd`; the trees are identical.
**11/58 accepted, 47 remain.** GPU/model/listening and full native-platform
qualifications are unrun; macOS prerequisites alone do not establish them.

## Batch 04 accepted verification

This independent batch depends only on accepted Batch 01. All five preparation
and UI recovery tasks have fixes, with 115 distinct CPU/Node targeted checks
passed. The real channel picker and single/batch Start handlers retain a preview
after refusal, consume its token after an accepted start, and reuse the same
File and channel selection on retry. Fresh previews receive new tokens, and
stale acknowledgements cannot clear those tokens. Native single and batch HTTP
fixtures confirm selected bytes reach repeated admission; single resume retains
its flag. Model execution is replaced by bounded CPU fixtures.

Pronunciation preview shares the Editor's chunk-ID validation and returns repair
guidance without replacing progress, audio or lexicon bytes. Ordinary upload and
preflight failures replace the batch spinner without starting a worker or
poller. Training reattachment presents meaningful accessible progress before
metrics arrive, then recovers the epoch total. Dataset upload refusals use the
shared parser for JSON, HTML and plaintext responses while retaining selected
files, HTTP status and control recovery.

Required `./ready.sh` quick verification against the accepted Batch 02 base
passed 8,570 unit tests without unit skips, 72 API checks without failures and
12 explicit full-mode exclusions, plus all compile/inventory/evidence/results/
API-contract gates. PR #1429 merged as
`0dd5e8465c4d96a31cd526f5ba3cc2b195a36927`, tested head
`08c140e61ad6bf0d16160ff48b160e07d5c0f079`; the trees are identical. All eight
exact-head CI checks passed: three unit shards, the complete-inventory
aggregate, native Windows ownership and three OS reports-checkJs candidates.
**21/58 accepted; 37 remain.** GPU/model/listening and full native-platform
qualifications are unrun and are not inferred from these control-plane checks.

## Batch 03 partial acceptance and worker-lifetime qualification

Six tasks merged in PR #1431 as `f94ad7745d5b78745d875f74dd18153e9c1f0226`;
its tree matches tested head `56a4bcfc040661184c5ceccdbe2e5656d63439f2`.
All five exact-head CI gates passed. Initial audio quick verification passed
8,581 unit tests; the combined accepted Batch05/audio quick gate passed8,594
without unit skips,72 API checks without failures and12 explicit full-mode
exclusions, plus all8 gates. The six child checklists have merged proof. Their
CPU/native file/PCM regressions do not establish real-model, listening or full
native-platform qualification.

F028/T02 remains required. Its original repeated-timeout worker growth was
reproduced before changing scheduling. One fixed application pool now serves
both single and batch calls. Each endpoint slot retains a cached SDK client in
an owned interpreter, so deadlines bound SDK construction, blocked prediction
and non-daemon SDK threads. Success keeps client/session reuse; timeout reaps
before replacement. Engine reset and ordinary/abrupt parent exit preserve
ownership. Queued cancellation, submission deadlines, timely later completion,
private staging and prior-output preservation remain tested.

Development verification passed118 distinct CPU checks on accepted mainf94ad774.
Tests use actual native subprocess ownership, deliberately blocked non-daemon
SDK-shaped executors, actual installed SDK HTTP construction, native PCM/ZIP,
real naming/alias operations and bounded model/provider callbacks. Clone upload
copies belong to the SDK owner: failed termination retains their bytes and
refuses more admission until cleanup succeeds. Both failed-stop defects were
reproduced before repair. Remote inference cancellation remains provider-limited.

Required `./ready.sh` quick passed8,613 unit tests without unit skips,72 API
checks without failures and12 explicit full-mode exclusions, plus all8 gates
(893.029 seconds). Exact published-head native Windows/macOS/Linux checks and
merge remain pending. F028 stays open, and Batch03 is incomplete until all
seven tasks are accepted. **27/58 accepted; 31 remain.**

The initial published head passed its local quick gate and native Linux/macOS
checks (19 cases each). Native Windows failed on graceful taskkill errors for
headless SDK workers; its resources correctly remained retained, so F028 stays
open. The SDK now uses owned Windows Job termination and confirms zero kernel
membership and a bounded root wait before release. All119 targeted CPU cases
plus4 navigation checks pass with termination/query/timeout failure controls.
Integrated with accepted Batch06 main, `./ready.sh` quick passed8,626 unit tests
without unit skips,72 API passes and12 explicit full-mode exclusions; all8 gates
passed in881.726 seconds. Renewed exact-head native3OS checks and merge remain
pending. Generic training shutdown and admission policies are unchanged. **35/58 accepted; 23 remain.**

## Batch 05 verification progress

All five failures were reproduced before their fixes. Development validation
passed 206 targeted CPU regression methods, using real experiment CLI phases,
checkpoint records, dataset archives and valid PCM with bounded inference,
training and score callbacks. Unchanged-input reuse and refusal byte preservation
remain explicit controls.

Cascade state binds decoded checkpoint/source/gold/roster identities, effective
shared prompt/config policy and harness bytes; legacy or mismatched state refuses
before expensive inference. Narrator-prior checkpoint recovery opts into retrying
only unanswered rows while preserving actual wrong answers and the default
checkpoint policy for other experiments.

Reference-intervention caches retain source-ZIP and extracted-file hashes, stage
new extraction/arm copies atomically, and refuse legacy, changed or partially
copied inputs before training. Shipping readiness requires one complete paired
serving artifact with matching row/text/gold/prompt identities; ambiguous ledgers
require an explicit selector. Its output records the exact source hash and model
and limits conclusions to the measured run.

Drift WAV reuse binds source text, seed, adapter serving generation, synthesis
settings, pronunciation and generation code. Adapter admission and generation
pins retain serving ownership safeguards. A locked working directory plus
per-WAV receipts preserve unchanged valid audio and regenerate changed or
unreceipted clips individually. Adapter/source mutation during measurement
refuses publication. No model/training/listening qualification is inferred from
these callback-driven fixtures.

Required local quick release passed 8,583 unit tests without unit skips, 72 API
checks without failures and 12 explicit full-mode exclusions, plus all eight
release gates. All five exact-head CI gates passed, including the complete
inventory aggregate and native Windows ownership. PR #1430 merged as
`3a4a0dde477d0daaa38962c20f3284b858f4a3d4`; its tree is identical to tested
head `5ccc085c460c23d1ad7749b8c4e1d5dd6ca70d9a`. All five child issues have
merged acceptance evidence. **21/58 accepted; 37 remain.**

## Batch 06 verification progress

All eight tasks retain their original issue and review-entry mappings. Concrete
failure fixtures were run against freshly fetched main `3a4a0dde` before fixes.
Development verification passed 80 CPU checks, including four existing function
tests, actual diagnostic CLI paths, native CSV/ZIP/WAV operations and an
independent exact-integer binomial oracle. Models, provider responses, training
and score callbacks are bounded fixtures; no inference or listening result is
claimed.

The arm writer now chooses reference audio and text from its own selected
centroid. Both PDNC builders share the canonical pseudo-speaker predicate while
retaining ordinary speakers and existing sampling controls. `--no-scale` emits
one as-served arm; scaled comparisons retain their interventions and keys.
LUAR candidate admission and both arm scores use fixture aliases, retaining the
original artifact correctness beside the rescored value. Sign tests reuse the
shared stable exact-binomial implementation.

The stop CLI records requested/generated/failed counts and every failed ID,
writes an inconclusive refusal even when all calls fail, and never approves a
successful subset. Lexicon scans distinguish an available empty third state
from a legacy missing key and avoid the misleading regeneration notice.
Paired reports expose shared-cohort accuracy separately from whole-arm totals,
unmatched counts and transport-failure IDs. Symbolization preserves every
attempt's status while retaining the aggregate attempt denominator and legacy
answer maps. Disjoint/all-failed cohorts show unavailable shared accuracy;
duplicate or nonboolean pilot rows refuse.

Required `./ready.sh` quick passed8,606 unit tests without unit skips,72 API
checks without failures and12 explicit full-mode exclusions, plus all8 gates
(867.009 seconds). All five exact-head CI gates passed. PR #1432 merged as
`d74e11bff8ab3d2116c004f349e56d0be271babd`, with a tree identical to tested
head `8e7e8b6bd849cef806fbdaaea197a6566c58d86b`. The eight child issues have
verified merged acceptance checklists. **35/58 accepted; 23 remain.**

## Completion gate

- [ ] All 58 child issues have merged acceptance evidence or an evidence-backed duplicate/refutation/obsolete disposition.
- [ ] All 41 findings, 16 observations and 13 test gaps remain mapped exactly once in the task manifest; no item disappears between batches.
- [ ] Regression checks include the review's concrete failure modes, with negative fixtures and unchanged-behavior controls.
- [ ] Required full-release, platform, GPU/model and listening checks have explicit results; blocked required checks remain open.
- [ ] The tracker, batch checkboxes and Goal 3.5 agree with the merged repository state.

## Task manifest and updates

The [machine-readable task map](audit_20261020_tasks.json) binds review IDs to issues, batches, dependencies and test-gap owners. Update its status and this checklist together after a batch merges. Do not renumber the original review IDs. Repository issue links contain the portable source evidence and acceptance criteria; private local artifacts are not required to understand a task.

## Batch 05 merged acceptance reconciliation

PR #1430 merged as `3a4a0dde477d0daaa38962c20f3284b858f4a3d4`; its tree
matches tested head `5ccc085c460c23d1ad7749b8c4e1d5dd6ca70d9a`. All five
exact-head CI gates passed. The five child issues have checked acceptance and
merged proof. Required local quick release passed 8,583 unit tests without
unit skips, 72 API checks without failures and12 explicit full-mode exclusions,
and all eight gates. GPU/model/training/listening and complete native-platform
qualification are unrun. **21/58 accepted; 37 remain.**


## Batch06 accepted reconciliation — 2026-10-20

PR #1432 merged by the repository owner as `d74e11bff8ab3d2116c004f349e56d0be271babd`, with a tree identical to tested head `8e7e8b6bd849cef806fbdaaea197a6566c58d86b`. All five exact-head CI checks passed. Local quick release passed8,606 unit tests without unit skips,72 API passes and12 explicit full-mode exclusions; all8 gates passed in867.009s.80 targeted CPU checks plus4 navigation checks passed. All eight child checklists have verified merged evidence. GPU/model/training/listening and full product-native qualifications remain unrun. **35/58 accepted;23 remain.**

## Batch07 verification progress — 2026-10-20

All seven failures were reproduced on freshly fetched accepted main `d74e11bf` before repair.210 distinct targeted CPU tests now pass, including17 new regression methods. The trait hook preserves product traits, alias folding and kept-answer flags; single-adapter pitch selection validates counts and empty pools; selectivity and roster reports retain nullable statistics and print n/a for absent denominators. Both optional diagnostics use the current constructor's intended segmentation modes; the two-by-two runner initializes its repository paths before loading inputs. Dirty-run patch capture checks tracked/untracked diffs and producer status, writes a private temporary patch, publishes by rename, and reports UNSAVED on capture/write/publication failures. Native binary and odd-name restoration plus existing queue ownership/VRAM/refusal controls remain covered. Model/provider callbacks are synthetic where stated. Required `./ready.sh` quick passed8,623 unit tests with zero unit skips,72 API passes without failures and12 explicit full-mode exclusions; all8 gates passed in868.979 seconds. Exact published-head CI and merge remain pending; no Batch07 task is accepted. GPU/model/listening and full native-platform qualifications remain unrun.


## Batch03 completed — F028 accepted, 2026-10-20

F028 PR #1433 merged by the repository owner as `e37a69e62f0db28e1c2e9fdb9250dc550bb18169`, with a tree identical to tested head `34c858a47be2fe91cdebe712ba38d56d4f400c01`. All eight exact-head CI checks passed, including20 selected SDK/engine lifetime cases on native Windows, macOS and Linux. Local quick release passed8,626 unit tests without unit skips,72 API passes with zero failures and12 explicit full-mode exclusions; all8 gates passed881.726s.119 targeted CPU checks plus4 navigation checks passed. The initial Windows shutdown failure and a macOS runner-capacity cancellation remain recorded; the repaired revision passed before acceptance. All seven Batch03 tasks are now accepted. Remote inference cancellation remains provider-dependent; GPU/model/listening and full product-native qualifications remain unrun. **36/58 accepted;22 remain.**

PR #1447 is being integrated with this accepted main. The seven implementation paths merged cleanly; two tracking-document conflicts were reconciled and the derived unit inventory is rebuilt. The previous8623-unit gate is historical evidence; the combined source needs a fresh release gate and exact published-head CI before Batch07 acceptance.


## Batch07 integrated qualification — 2026-10-20

PR #1447 integrated against accepted F028 main e37a69e62f0db28e1c2e9fdb9250dc550bb18169. Fresh combined ./ready.sh quick passed8643 unit tests with zero unit skips,72 API passes/zero failures and12 explicit full-mode exclusions;all8 gates passed859.861s. 234 distinct targeted checks passed(230 CPU plus4 navigation);all17 new methods remain inventoried. The original8623-unit gate remains historical evidence. Both prose/task conflicts are reconciled; implementation paths merged cleanly and the derived inventory was rebuilt. New exact published-head CI and merge remain pending;no Batch07 task accepted. GPU/model/listening/full native-platform qualification unrun.36/58 accepted;22 remain.
