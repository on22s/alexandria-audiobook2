# 2026-10-20 source-review remediation plan

Goal **3.5** · [Execution tracker #1366](https://github.com/on22s/alexandria-audiobook2/issues/1366) · **OPEN:55/58 accepted;3 remain**

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

**Dependencies:** Batch02, Batch06 (accepted). **Tasks:**7. **Status:** accepted and merged (seven tasks). **PR:** [#1447](https://github.com/on22s/alexandria-audiobook2/pull/1447).

**Batch acceptance:** Run supported CLI initialization and minimal one-item cases without model inference. Verify obsolete callback/constructor signatures, empty strata, perfect-result summaries and failed patch capture. Provenance must distinguish saved, partial and failed evidence; never advertise an empty patch as reproducible code.

- [x] **F007** [#1403](https://github.com/on22s/alexandria-audiobook2/issues/1403) — The per-line character-traits experiment installs a callback with an obsolete signature and crashes on accepted attribution.
- [x] **F033** [#1404](https://github.com/on22s/alexandria-audiobook2/issues/1404) — Requesting one pitch-separation adapter divides by zero when the manifest has multiple adapters
- [x] **F034** [#1405](https://github.com/on22s/alexandria-audiobook2/issues/1405) — Respelling-selectivity reports crash when all valid paired terms fall in one stratum.
- [x] **F035** [#1406](https://github.com/on22s/alexandria-audiobook2/issues/1406) — A valid perfect-attribution result crashes the roster-order CLI while printing its summary.
- [x] **F036** [#1407](https://github.com/on22s/alexandria-audiobook2/issues/1407) — Two optional three-pass diagnostics still use the removed presegment_quotes constructor argument
- [x] **F037** [#1408](https://github.com/on22s/alexandria-audiobook2/issues/1408) — The two-by-two attribution experiment crashes before loading inputs because REPO is assigned after its first use
- [x] **F041** [#1409](https://github.com/on22s/alexandria-audiobook2/issues/1409) — Dirty-run provenance reports a saved reproduction patch even when git diff fails

### Batch 08: Improve API and export responsiveness

**Dependencies:** Batch 01, Batch 03, Batch 04. **Tasks:** 5. **Status:** accepted and merged (five tasks). **PR:** [#1454](https://github.com/on22s/alexandria-audiobook2/pull/1454).

**Batch acceptance:** Measure cold/warm API, preview and selected-chapter behavior on a fixed workload, with event-loop responsiveness and peak PCM/memory observed separately. Preserve output, ownership and cancellation parity. Any GPU-slot change needs proof that the path is CPU-only; do not loosen the safety policy by assumption.

- [x] **E01** [#1410](https://github.com/on22s/alexandria-audiobook2/issues/1410) — Voice design save/delete acquire blocking file locks and copy complete audio files on the API event loop
- [x] **E02** [#1411](https://github.com/on22s/alexandria-audiobook2/issues/1411) — Word timing advertised as CPU-only reserves the global GPU task slot
- [x] **E05** [#1412](https://github.com/on22s/alexandria-audiobook2/issues/1412) — Filename previews decode every complete audio file
- [x] **E06** [#1413](https://github.com/on22s/alexandria-audiobook2/issues/1413) — Cold chapter export retains the entire book PCM even for one selected chapter
- [x] **E10** [#1414](https://github.com/on22s/alexandria-audiobook2/issues/1414) — Separate complete retention maintenance from the UI listing cap

### Batch 09: Harden diagnostics and regression tests

**Dependencies:** Batch05, Batch06 (accepted). **Tasks:**7. **Status:** accepted and merged (seven tasks). **PR:** [#1453](https://github.com/on22s/alexandria-audiobook2/pull/1453).

**Batch acceptance:** Use mutation-sensitive assertions: wrong empty API names, an unreaped dead lock holder, forbidden chunk changes and zeroed decoded speech must make the relevant tests fail. Restore correct behavior afterward. Check deleted-artifact refusal, telemetry distinctions, temporary cleanup, ETA denominators and post-purge failure semantics without deleting real assets.

- [x] **F032** [#1415](https://github.com/on22s/alexandria-audiobook2/issues/1415) — The artifact-loss guard reports success when a measured artifact is deleted entirely.
- [x] **E04** [#1416](https://github.com/on22s/alexandria-audiobook2/issues/1416) — Unexpected convention-detector errors are labelled as ordinary no-convention telemetry.
- [x] **E08** [#1417](https://github.com/on22s/alexandria-audiobook2/issues/1417) — Per-adapter extracted WAV temporary directories persist after analysis.
- [x] **E11** [#1418](https://github.com/on22s/alexandria-audiobook2/issues/1418) — Use a consistent completed-attempt denominator for batch ETA
- [x] **E12** [#1419](https://github.com/on22s/alexandria-audiobook2/issues/1419) — Remove unused mtime cache state
- [x] **E13** [#1420](https://github.com/on22s/alexandria-audiobook2/issues/1420) — Distinguish post-purge verification errors from mutation-free refusal
- [x] **E16** [#1421](https://github.com/on22s/alexandria-audiobook2/issues/1421) — Move unittest.main guards below all test classes for complete direct module runs. Includes T10, T11, T12, T13.

### Batch 10: Document and isolate historical drivers

**Dependencies:** Batch 07, Batch 09. **Tasks:** 3. **Status:** current spoken-source intent integrated; release and updated CI pending. **PR:** [#1463](https://github.com/on22s/alexandria-audiobook2/pull/1463) (draft).

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

## Batch09 verification progress — 2026-10-20

All seven Batch09 tasks have development fixes on freshly fetched accepted main d74e11bf.218 distinct targeted CPU tests plus4 navigation tests passed with zero skips.11 new methods cover deletion guards, detector-error telemetry, native ZIP/PCM cleanup, ETA and post-write purge outcomes. Five scoped mutations remained false-green under the old tests, are rejected by the hardened tests and pass again after restoration.95 unittest.main guards moved to file end; non-guard AST bodies unchanged; direct module execution matches inventoried IDs (13 training benchmark/16 production audio/10 API-summary tests). Required release/exact-head CI/merge pending; no task accepted. GPU/model/listening/full native-platform qualification unrun.

Purge preflight safeguards and exact target selection remain unchanged. Post-write failures report the completed API steps, an unconfirmed operation when applicable, observed versus expected freed bytes and accounting uncertainty. No Hub operation used real assets. Extracted dataset PCM is removed on successful metrics, missing metadata, metric failure and exceptions; input ZIP/reference bytes remain exact. Prompt caching keeps its existing file-version identity and negative-cache behavior while removing unused mtime state. ETA uses the matching successful time_seconds population. The artifact-loss guard compares the baseline Git tree and current index and refuses staged or unstaged measured-file deletion. Detector errors retain conservative validation behavior with distinct redacted error-type telemetry.


## Batch07 accepted reconciliation — 2026-10-20

Accepted via PR1447,merged as b48669b18ed9f4f026311a947f9f88158832e1c3,tree-identical to tested headf3fb0a9b98d7be7be6ec17381435cbc098f05862. All5 exact-head CI checks passed. Combined quick8643unit0skip/72API0failed12explicitfull-modeexclusions/all8gates859.861s. 234distincttargetedchecks(230CPU+4navigation),plus4navigationafterbookkeeping. Seven child acceptance checklists verified CLOSED. GPU/model/listening/full product-native qualification unrun.43/58accepted;15remain.

## Batch09 integration qualification — 2026-10-20

All seven Batch09 tasks have development fixes on freshly fetched accepted main d74e11bf.218 distinct targeted CPU tests plus4 navigation tests passed with zero skips.11 new methods cover deletion guards, detector-error telemetry, native ZIP/PCM cleanup, ETA and post-write purge outcomes. Five scoped mutations remained false-green under the old tests, are rejected by the hardened tests and pass again after restoration.95 unittest.main guards moved to file end; non-guard AST bodies unchanged; direct module execution matches inventoried IDs (13 training benchmark/16 production audio/10 API-summary tests). Required release/exact-head CI/merge pending; no task accepted. GPU/model/listening/full native-platform qualification unrun. Original d74-based quick release subsequently passed8617 unit0skip/72API0failed12explicitexclusions/all8gates856.810s. Now integrated against accepted F028+Batch07 main b48669b18ed9f4f026311a947f9f88158832e1c3;code applied cleanly and two shared tracking conflicts reconciled. Combined259 distinct targeted checks passed with zero skips in23.692s,including the accepted external SDK/engine lifetime and Batch07 regressions. Five scoped mutations again stayed false-green under the old tests,were rejected by hardened tests,and passed after restoration. All95 guard placements preserve the expected non-guard AST bodies;direct13/16/10 test modules again match exact inventory IDs. New combined release/exact-head CI/merge pending;no Batch09 task accepted.


## Batch09 combined release qualification — 2026-10-20

All seven Batch09 failures/test gaps were reproduced before repair on accepted d74e11bf. Integrated against accepted main b48669b18ed9f4f026311a947f9f88158832e1c3. 259 distinct targeted checks passed23.692s/zero skips,including accepted external SDK/engine and Batch07 controls. Five scoped mutations stayed false-green under old tests,were rejected by hardened tests,and passed after restoration. 95 unittest.main guards are at file end with the expected non-guard AST bodies preserved;direct13/16/10 benchmark/production-audio/API modules exactly match inventory IDs. Fresh combined ./ready.sh quick passed8654 unit tests with zero unit skips,72 API passes/zero failures and12 explicit full-mode exclusions;all8 gates passed888.126s. Exact published-head CI and merge pending;no Batch09 task accepted.43/58accepted;15remain. GPU/model/listening/full product-native qualification unrun. No real Hub assets modified.


## Batch09 native CI probe repair — 2026-10-20

PR1453 first exact-head CI attempt failed one existing Linux process-liveness probe with ProcessLookupError/ESRCH while reading /proc after a real child exited. The helper was unchanged from accepted main; a passing base run does not refute this observed exit race. Deterministic ESRCH reproduction fails before repair. The probe now classifies ESRCH/ENOENT as exited while permission errors still raise and a real current process remains live. Native wrapper signal/lease controls and a fresh full release are being rerun before the next commit;previous8654-unit/888.126s release remains historical,not proof for this added regression. No Batch09 task accepted;43/58accepted15remain.


## Batch09 repaired combined release — 2026-10-20

PR1453 combined candidate includes a native /proc exit-race probe repair:ENOENT/ESRCH report an exited process,while permission errors still raise and actual live PID remains live. The first exact-head CI failure and its passing base report are preserved;the same helper was present on the base. Deterministic before/after controls reproduce the observed race.267 distinct targeted checks passed48.279s without skips,including real signal-tree/lease controls;12 focused ownership checks passed24.720s. Five mutation controls and direct13/16/10 inventory parity remain covered;95 relocated guards preserve expected AST bodies. Fresh ./ready.sh quick passed8655 unit tests without skips,72API passes/zero failures/12explicit full-mode exclusions and all8gates in851.436s. Validated treed2220060a98e34c2b8bde49a607c4c6c9b54b1b1. Only bookkeeping changes follow this gate;new published-head CI and merge pending.43/58accepted15remain;no Batch09 task accepted. Full product-native/GPU/model/listening qualification unrun. Private ROCm-runtime quick API preflight separately passed72/0/12;not a full-inference pass.


## Batch08 development qualification — 2026-10-20

All five Batch08 enhancements are implemented for qualification on accepted main b48669b18ed9f4f026311a947f9f88158832e1c3. The owner approved one bounded initial legacy MP3 timing scan for E06. Content-bound timing facts preserve trim/frame offsets; first indexing holds one source clip at a time with owned disk snapshots. Later cold chapter-output selection decodes only the selected sources. Changed encoded bytes (including restored size/mtime), decoder/trim policy, corrupt facts, missing/repaired audio, cancellation and source changes during staging are covered. Strong source identities also protect changed-only reuse and preview parity. 171 distinct targeted tests passed7.730s with zero skips;14 new methods. Native MP3 measurement:24 clips, first scan24 decodes/~0.694s versus baseline~0.689s; later cold-output selected export1 decode/~0.039s versus24/~0.677s. Selected output bytes, offsets and all source bytes identical. Python peak allocations first scan491414B versus2551042B,not process RSS. Preview zero PCM decodes versus24. Tiny WAV timings show hashing overhead;no universal speed claim. Designer1MiBcopy/50ms latency injection:old ping~53/51ms,new1.197/.678ms while mutation pending. CPU-only word timing keeps task/foreign ownership and cancellation until worker exits;actual ffmpeg/provider --no-gpu command checked with synthetic ASR. Complete retention scans beyond500 UI rows. Ready, exact-head CI and merge pending;no Batch08 task accepted. GPU/model/listening/full product-native qualification unrun.


## Batch08 native CPU and resident-memory evidence — 2026-10-20

Additional native development evidence: actual cached whisper.cpp Small.en through the candidate word-timing route returned 7 mapped spans in 5.293892s; cache hit 0.003499s; unrelated ping 0.000710s while pending. Exact fresh/cache-hit chunk and audio bytes preserved;CPU-only lease and zero GPU-lock acquisitions verified. The unrelated GPU-running flag was simulated;no real GPU-concurrency or pronunciation/listening certification is claimed. A separate Linux fresh-process workload of40 native PCM16 mono WAVs (~60s each at24kHz),last selected,measured initial ru_maxrss 171572KiB accepted versus58468KiB candidate;before-export high-water marks were59152/53100KiB. Live decoded sources40 versus1 during initial scan;later cold-output selection40 versus1 decode. Waveform bytes,offsets andsourcebytes identical. Initial WAV time0.041422s versus0.347160s:strong identity hashes cost time on this workload;this is a memory improvement,not a universal latency improvement. Second RSS readings are cumulative process high-water marks. Final combined release/CI/merge remain pending.


## Batch08 decoder recovery qualification — 2026-10-20

Latest Batch08 targeted qualification after fixing a newly introduced cache edge case:172 distinct tests passed7.821s without skips;15 new methods. A transient decoder failure on existing unchanged bytes must be retried on a later export instead of becoming a permanent omission. The added deterministic regression fails before repair and passes afterward. Valid cached facts retain selected-only decoding;missing/unreadable encoded paths remain rechecked by input identity,while existing inputs whose decoding failed receive a fresh admission attempt. Native9-method timing-cache controls passed. Native MP3 cost/output parity was rerun after this policy change;the earlier measurement table is preserved as historical. Source hashing,publication cancellation/rollback and exact source/offset/waveform controls remain intact. Full combined Ready,new exact-head CI and merge pending;no Batch08 acceptance.


## Batch08 hash mutation control — 2026-10-20

The latest172-test run passed7.821s after strengthening the content-identity regression to freeze every filesystem version field for the changed source. A separate mutation replacing real content digests with a constant is rejected;restored hashing passes. Thus the regression verifies content binding independently of Linux ctime. Nine timing-cache tests pass;15 new methods remain. No additional source change follows the transient-decoder recovery fix.


## Batch09 accepted reconciliation — 2026-10-20

Accepted via PR1453,merged as 08312868514f11986e28aff57534a3421ed80219,tree-identical to tested headc0eea53488624440826538aaa6243cba1ecbe03a. All5 exact-head CI checks passed. Fresh repaired quick release8655units/zero skips,72APIpasses/zero failures/12explicit full-inference exclusions;all8gates851.436s.267distincttargeted checks passed48.279s,12focused native ownership checks passed24.720s,4navigation checks after bookkeeping. Initial native /proc exit-race failure and its passing base report preserved;deterministic ESRCH/ENOENT/permission/live-PID controls now pass. Five mutation controls/direct13/16/10inventoryparity/95guard relocations retained. Seven child issues1415–1421 acceptance bodies/checklists verified CLOSED.50/58accepted;8remain. Full GPU/TTS/LLM/listening and complete product-native qualification unrun.

## Batch08 accepted-main integration — 2026-10-20

Batch08 current indexed-export/decoder-recovery/content-hash candidate preserved as treec53f108caae307d29f52fc0011f3f04fb93d9f36 and117601-byte binary patchSHA25600bd3d6706a12575061ccbb5a64e9f39b299f2e0a108f2d544675b0d0ba5b3ff;reverse-apply check passed. Now integrated with accepted Batch09 main08312868514f11986e28aff57534a3421ed80219. Canonical main07history retained;all seven09acceptances backfilled,all five08tasks remain in progress.50resolved/5in_progress/3open. Previous172-target/realCPUASR/RSS/MP3 development proofs retained with their exact workload limits;new combined targets/inventory/Ready/CI/merge pending. No Batch08 task accepted.


## Batch08 integrated development qualification — 2026-10-20

Integrated against accepted PR1453 main08312868514f11986e28aff57534a3421ed80219;all14 Batch08 source/test blobs match preserved development treec53f108caae307d29f52fc0011f3f04fb93d9f36.428 distinct combined targeted tests passed56.080s with zero skips,including accepted09native signal/SDK/guard controls and08ownership/export/retention tests. Inventory8670=accepted8655+15new methods. All58tasks/70entries/13gapowners remain mapped;50resolved5in_progress3open. New native MP3 measurement on this accepted base:24clips;initial indexing24decodes,maxlive1,0.704015s versus accepted24/maxlive24/0.674747s;subsequent cold-output selection1decode/0.040653s versus24/0.696350s. Allselected output/sourcebytes andoffsets identical;changed-only valid-index selection0decodes. Separate40-minute native WAV fresh-process peakRSS171412KiB accepted versus56108KiB candidate(before-export highwater59544/53032KiB),maxlive40vs1;hashcostinitial0.045114svs0.332875s explicitly recorded. RSS excludes filesystem cache/child processes and second readings are cumulative highwater. Designer1MiBcopy/50ms latency injection on accepted base:ping53.878/51.377ms old versus1.196/.628ms candidate while mutation pending,transaction~51ms unchanged. Actual cached Small.en native CPU ASR through integrated route passed4.842960s,fresh/cache-hit chunk andaudio bytes preserved,ping0.000723s while pending,cache0.003258s,7mappedspans,CPUlease/zeroGPUlock acquisitions. GPU-runningflag is simulated;noactualGPUconcurrency/synthesis/pronunciation/listeningqualification. Strong content-hash mutation fails withconstantdigests evenwhenallfileversionfieldsarefrozen;restoredhashpasses. Existing undecodable bytes receivefreshadmission,recoveringtransient failures. Fresh full Ready/exact-headCI/merge pending;no08taskaccepted.


## Batch08 combined release qualification — 2026-10-20

Batch08 is integrated with accepted main 08312868514f11986e28aff57534a3421ed80219. The required ./ready.sh quick release passed all eight gates in 852.390s: 8,670 unit tests with zero unit skips, 72 API passes, zero API failures and 12 explicit full-inference exclusions. The validated tree is 8e56d12a56a4679bcb0a702df346d874b2a1f415. The combined targeted suite passed 428 distinct tests in 56.080s without skips; 15 new methods remain inventoried. The owner-approved legacy indexing scan, selected-only decoding of valid indexed clips, exact offsets and waveform bytes, transient decoder recovery, strong source hashes, atomic staging and cancellation are covered. A content-hash mutation is rejected even when all filesystem version fields are held unchanged.

Native MP3 cost measurement uses 24 fixed clips: the first bounded scan decodes 24 while retaining one source at a time; a later cold-output selection decodes one in 40.653ms versus 24 in 696.350ms. Output and input bytes and book-wide offsets are identical. A separate 40-minute native WAV fixture measured peak resident memory of 171412KiB for accepted main versus 56108KiB for the candidate. Its initial latency increases from 45.114ms to 332.875ms because identity hashing costs time. This is a measured memory improvement, not a universal latency claim. RSS and Python allocations are recorded separately. Designer copy/lock transactions preserve rollback while unrelated pings respond in 1.196/0.628ms versus 53.878/51.377ms in the fixed cold/warm workload; the transaction still takes about 51ms. Complete retention maintenance is independent of the 500-row UI cap.

The actual cached Small.en CPU ASR passed through the integrated word-timing route in 4.842960s; cache hit 0.003258s, concurrent ping 0.000723s and 7 mapped spans. The CPU task lease, zero GPU-lock acquisitions and immediate fresh/cache-hit input-byte preservation are verified. The unrelated GPU-running flag is simulated; actual GPU concurrency, TTS/LLM inference, listening and complete product-native qualification are not established by these fixtures. New exact-head CI and merge are pending. No Batch08 task is accepted yet: 50/58 accepted, eight remain.


## Batch 08 accepted reconciliation

Accepted via PR #1454, merged as f6f97a279305f36536c3dafc80391571379eb272, tree-identical to tested head 95ee2ec192d2745501981b1003e7628053504fd3 (tree 81ca0bfd55c5dfed538f5d65cd07d7926a6eb706). All nine exact-head CI checks passed: three unit shards, the inventory aggregate, Windows ownership, native SDK lifetime on Linux/Windows/macOS and macOS subprocess prerequisites. Native SDK suites each passed 20 tests with zero skips. Local quick release passed 8,670 units with zero skips, 72 API checks with zero failures and 12 explicit full-inference exclusions, and all eight gates in 852.390s. Combined targeted checks passed 428 distinct tests without skips. Normal hooks preserved the committed tree; only three bookkeeping documents changed after the release gate. All five issue acceptance bodies/checklists verified closed. 55/58 accepted; three historical tasks remain. Full GPU/TTS/LLM inference, real GPU concurrency and listening remain unrun global gates; these native ownership/SDK checks do not certify the whole product on every OS.

## Batch 10 archival disposition

Archival disposition implemented against freshly fetched accepted main f6f97a279305f36536c3dafc80391571379eb272; no original producer or dated scientific output is modified. The central catalog accounts for all 18 E14 observations across 17 actual scripts, preserving the 038-13 citation correction. Adjacent notices and the existing Thunder report clarify historical entry points and the frozen ECAPA runtime. A 38-file preservation manifest binds original producers and verifier evidence. CPU-only original-tail readiness/cwd/exit-status and exact frozen AST differing-model-bytes characterizations reran on this main. No server/model/voice campaign was run. Existing shared-stage/queue/cache controls and full Ready/exact-head CI/merge remain to be qualified. No Batch10 task accepted yet; 55/58 remain accepted.

The [historical-driver boundary](audit_20261020_historical.md) and its 18-entry catalog specify what must change before any new campaign. E03 is archived with its readiness/cwd failure and correctly propagated worker exit distinguished. E15 remains a frozen, self-hashed dated consumer; the current helper resolves a directory without creating it, and synthetic different checkpoint bytes demonstrate the consumer mismatch. Neither producer is revived. These are the explicit archival alternatives authorized by this plan, rather than new scientific measurements. Original source/evidence preservation and active shared controls are verified separately from model quality.


**Batch10 development verification — 2026-10-20.** Development verification passed 55 existing shared-stage, commit-failure, queue-inspection, ECAPA-helper and navigation tests in 17.336s without skips; 38 preservation hashes and both reproducible characterization receipts match. No new application test or producer is introduced. Full Ready, exact-head CI and merge remain pending.


## Batch 10 release qualification

Fresh ./ready.sh quick release passed all eight gates in 861.826s, including 8,670 exact inventoried unit tests with zero skips and 72 API passes, zero failures and 12 explicit full-inference exclusions. Validated tree: f3ac91c9748a5364a1069aeeab51f9bdbd7d3e65. The 55 focused shared-stage/queue/ECAPA/navigation tests passed in 17.336s with zero skips. All 38 preservation hashes and the two rerun CPU-only characterization receipts match accepted main f6f97a279305f36536c3dafc80391571379eb272. There are no changes to application/queue code, original producers, historical verification outputs, the frozen self-hashed verifier or guard exemption lists. Exact published-head CI and merged archival acceptance remain pending. 55/58 tasks are accepted; three Batch10 tasks remain in progress. Real-model runtime validation is recorded separately; this archival gate is not a listening or scientific-result recertification.


## Independent runtime qualification progress

Independent runtime qualification against accepted PR1454 source completed actual cached local-Qwen annotation/review and nine CustomVoice speech chunks on a public excerpt. All263ordered source words match script and chunks exactly;all chunks and MP3/M4B/Audacity artifact/media checks pass. The original full API report is82passes/zero failures/two explicit prerequisite skips;the two original skipped cases passed separately with uploaded source and an owned live local LLM. They remain separately recorded rather than relabelled as one84-pass run. CPU whisper.cpp recovered261/263words(99.24%);a silent-audio control recalls0/8words versus8/8for the valid short sample. Human listening observation and final main ledger publication remain pending. This small smoke does not establish whole-book prosody or the separate1090typical performance gate. Native Linux/Windows/macOS SDK ownership evidence is carried only with relevant-source identity. The live checkout and copied adapter source inputs remain unchanged.


## Batch10 current-main integration (supersedes prior release qualification)

Current Batch10 candidate is integrated with newly accepted PR1457 main e6b87326180d16e671320a495ab278afdb4ab1f4, which rejects non-finite external-provider PCM and adds two unit methods (inventory8,672). The earlier8670-unit/all8gates861.826s release and unpublished local commit a3145e608c497fa7787e47cdfe562189c8da9c16 remain preserved as prior-main evidence,not current qualification. The13-file archival candidate was preserved by commit/ref and a verified112825-byte binary patch,then reapplied cleanly atop freshly fetched main. All38archival preservation bytes remain identical;both CPU-only characterizations reran on this new accepted source. No dated producer or existing scientific output changed. Fresh combined controls and ./ready.sh/exact-head CI/merge are required again.55/58accepted;three archival tasks remain in progress. Native model/API/fidelity/ASR results on accepted08source remain separately recorded;current-main runtime qualification is pending after the new finite-audio guard. Human listening request remains pending.


**Batch10 integrated development controls.** Fresh combined qualification on accepted PR1457 passed96distinct existing shared-stage/queue/ECAPA/navigation/finite-audio/training-metadata checks in18.849s with zero skips. The current unit inventory is8,672. Full Ready and published-head CI/merge remain pending.


## Batch 10 release qualification

Fresh ./ready.sh quick release passed all eight gates in 870.999s: 8,672 exact inventoried unit tests with zero skips, and 72 API passes with zero failures and 12 explicit full-inference exclusions. Validated tree: 52d73c202c6fa538288c16ae8b064b9ab8afea44. The 96 focused shared-stage, queue, ECAPA, navigation, finite-audio and metadata tests passed in 18.849s without skips. All 38 preservation hashes and both rerun CPU-only characterizations match accepted main e6b87326180d16e671320a495ab278afdb4ab1f4. Application and queue code, original producers, dated verification outputs, the frozen self-hashed verifier and guard exemptions are unchanged. Published-head CI and merged archival acceptance remain pending; 55/58 tasks are accepted and three Batch10 tasks remain in progress. Independent actual ROCm validation on the same application source preserved all 263 ordered words, completed nine CustomVoice chunks and verified MP3, M4B and Audacity exports. The original full API report remains 82 passes, zero failures and two prerequisite skips. Those two original unmodified functions passed separately with an uploaded source and an owned local LLM, covering 84 distinct API cases across runs. CPU ASR recovered 260/263 words (98.86%) and rejected silent audio. Human listening feedback and final native/ledger reconciliation remain pending. This small excerpt tests functionality; whole-book prosody, the separate #1090 typical performance gate and historical scientific-result recertification are outside this validation.


## Current publication-path integration

The archival PR now includes accepted main22b0de7f85caccf6ebd8fae55e6df9c41ab3d3a3 through a normal pending merge. PR1462 changes chunk publication to retain prior audio when metadata persistence fails and adds nine test methods,bringing the inventory to8,681. The previously qualified/published5a2e3d03 head and its five successful CI checks remain preserved as prior-main evidence. All38historical preservation files remain byte-identical. Fresh combined publication/journal/UI/stage/queue/ECAPA/audio controls passed119distinct tests in30.928s with zero skips. The new full release and merged-head CI are pending;55/58tasks remain accepted. Actual model/API validation is being repeated against the current publication path;no live checkout or existing GPU job is changed.


## Qualified publication-source archival candidate

Current publication-source ./ready.sh passed all eight gates in 886.198s: 8,681 exact inventoried unit tests with zero skips and 72 API passes with zero failures and 12 explicit full-inference exclusions. Validated tree: bf0faa82918deefde56d8f2ba293f8fbb3472f7f. The 119 combined publication, journal, UI, stage, queue, ECAPA, audio and metadata checks passed in 30.928s without skips. All 38 historical preservation hashes remain exact; both CPU-only characterizations were rerun against accepted main 22b0de7f85caccf6ebd8fae55e6df9c41ab3d3a3. The archival change adds no application or queue behavior and changes no dated producer, verification output or guard exemption. Exact updated-head CI and merge remain pending; 55/58 tasks are accepted and three archival tasks remain in progress. Actual ROCm validation on the same application source preserved all 263 ordered words, completed 9 speech chunks and verified MP3, M4B and Audacity exports. The full API report remains 82 passes, zero failures and two prerequisite skips; those two original functions passed separately with a selected source and an owned local LLM. CPU ASR recovered 261/263 words (99.24%) and rejected silence. Native SDK ownership passed 20 tests without skips on each of Linux, Windows and macOS at the accepted publication source. Listening is unverified; no whole-book quality or separate #1090 performance result is claimed.


## Current chapter-selection source qualification

Integrated accepted chapter-selection fix PR1464 (bb566b76d67d7713277da15e46e7b3020e273aeb). The combined 180 chapter, publication, journal, UI, stage, queue, ECAPA, audio and metadata tests passed in 37.238s without skips. Real copied generated audio exported the exact previewed third chapter with identical cold/cached bytes and absolute offsets (15,530–17,040ms); a forced earlier decoder omission refused selection and preserved prior delivery. CPU ASR recovered all eight selected words. An exact source/AST comparison shows only the two chapter-export methods changed since the actual 22b model run; unchanged generation and full-book export evidence is retained with that explicit source boundary. The fresh 8,688-test release gate and updated PR CI remain pending. No archival task is accepted before merge.


## Current spoken-source qualification

Integrated accepted main f752ed83ea1fbb10407eca9dcae79b9898b84490 (PR1465 spoken-source intent). All generation/publication methods except grouping are unchanged from actual 22b model evidence; rerunning grouping on the actual annotated script reproduces exactly the nine generated inputs. The current source real selected export replay preserves cold/cached bytes and offsets, rejects decoder omissions without replacing prior delivery, and CPU ASR recovers all eight selected words. Fresh chapter/spoken regressions and the 8,695-unit release gate qualify changed grouping/export behavior; updated PR CI and merge remain pending. Three archival tasks remain unaccepted until merge.


## Qualified current-source archival candidate

Current chapter-selection-source ./ready.sh passed all eight gates in 878.449s: 8,695 exact inventoried units with zero skips, 72 API passes with zero failures and 12 explicit full-inference exclusions. Validated tree: f756b344fb455f42e85befbb39cf25dc5d86e670. Current spoken/chapter controls passed 68 tests in 11.654s without skips, supplementing the 180 chapter/publication/shared-control cases passed on the preceding bb566b76 source. Real generated audio replay on accepted main f752ed83ea1fbb10407eca9dcae79b9898b84490 preserved selected-file bytes and absolute 15,530–17,040ms offsets across cold/cached exports; a forced earlier decoder omission refused the selection without replacing prior files. CPU ASR recovered all eight selected words. All 38 archival preservation hashes remain exact. Actual ROCm generation/full API/full-book export proof at 22b0de7f is retained for unchanged functions: a source/AST comparison proves the grouping and chapter-export methods changed; actual source regrouped into identical nine generation inputs and selected exports were replayed on f752ed83. The raw full API report remains 82 passes/zero failures/two prerequisite skips; both original skipped functions passed separately with actual source and an owned local LLM (84 distinct API cases across runs). Annotation/review preserved 263 ordered words, nine CustomVoice chunks completed, and MP3/M4B/Audacity artifacts were verified. CPU ASR recovered 261/263 words (99.24%) and rejected silence. Native SDK ownership passed 20 tests with zero skips on each of Linux/Windows/macOS at exact accepted f752ed83 source. Updated PR-head CI and merge remain pending; 55/58 tasks are accepted and three archival tasks remain in progress. Listening is unrun, with no auditory quality verdict, whole-book prosody or separate #1090 performance claim.
