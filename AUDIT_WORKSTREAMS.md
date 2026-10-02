# Whole-audit remediation map

The original Desktop audit contains 1,374 candidate claims. The checked-in
`AUDIT_COVERAGE.tsv` has one row per original one-based candidate ID and is the
coverage source of truth. Its original verdict is **not** our verdict. A row
marked `unreviewed` has only been inventoried, not verified. The source JSON's
SHA-256 and the evidence for fixes completed so far are in
`AUDIT_REMEDIATION.md`.

| Workstream | Original groups | Candidates | Review order |
| --- | --- | ---: | ---: |
| Root scripts and shared alignment | root-1, root-2, root-3 | 337 | 1 |
| App core, state, configuration, and GPU coordination | app-core-1, app-core-2 | 186 | 2 |
| Script generation, review, and annotation pipeline | pipeline-1, pipeline-2 | 207 | 3 |
| API routers and storage boundaries | routers-1, routers-2 | 186 | 4 |
| TTS, audio assembly, and exports | audio-1, audio-2 | 193 | 5 |
| Standalone tools and experiments | tools-1 | 88 | 6 |
| Vanilla-JS frontend | frontend-1, frontend-2, frontend-3 | 177 | 7 |

For each workstream, inspect every candidate against the current code and its
callers; cluster duplicates by one root ID; document a concrete failure or a
reason the claim is refuted; fix actionable root causes with regression tests;
then update every affected row's disposition and evidence. A shared fix may
close several IDs. Do not treat an original `REFUTED` verdict as reviewed.

Allowed dispositions are `unreviewed`, `open`, `fixed`, `already_fixed`,
`duplicate`, `refuted`, and `cleanup_done`. Closed rows require evidence;
duplicates also name their root ID. An `open` row cannot enter the PR without
an explicit decision. Keep safety guards and GPU locks intact while fixing
their callers.

This recovered map describes the earlier whole-audit plan. The owner's current
instruction is to address P0, P1, and P2 directly, without Space Bunny or API
review calls, on the combined branch. The current Desktop review state is the
scope/disposition authority; this TSV retains earlier evidence. Check its
original-row integrity with `python tools/audit_coverage.py check`. Defer
`./ready.sh` until every in-scope finding is addressed. Publishing the combined
bug fixes requires the owner's explicit instruction.

## Root-1 candidate map (77 IDs)

This map assigns every root-1 claim exactly once. It is a **triage map**, not
verification of unresolved claims. Review the linked calls and safety guards
for each bundle, then record final dispositions in `AUDIT_COVERAGE.tsv`.

| Bundle | IDs | Count |
| --- | --- | ---: |
| Already fixed on this branch | 21, 23–30, 40–41, 48, 72 | 13 |
| Source tokenization, cursor, divergence | 2, 8, 12 | 3 |
| ASR language, timestamps, limits, timeout, confidence cost | 3–6, 66–67 | 6 |
| Diarization and speaker-boundary semantics | 7, 11, 13, 63, 74 | 5 |
| Resume identity, checkpoint/artifact ownership, durability | 14–16, 34–39, 45–46, 49, 73, 77 | 14 |
| Annotation batch behavior, model loading, metrics | 9–10, 31–33, 58–60, 65, 69 | 10 |
| ZIP volume lifecycle and completeness | 17–19, 76 | 4 |
| CLI validation and phase orchestration | 20, 42–44, 56, 71, 75 | 7 |
| Large-audio memory/decode use | 61–62, 64 | 3 |
| Remaining compare UI/performance | 47, 68, 70 | 3 |
| Progress and import-time diagnostics | 55, 57 | 2 |
| Originally refuted; independently recheck | 1, 22, 50–54 | 7 |

The resume/artifact bundle is the highest-risk root-1 unit: claims 14–16,
34–39, 45–46, 49, 73, and 77 may share one run-identity/lifecycle cause.
Do not implement them as independent pathname checks until that shared
contract has been traced across phase subprocesses and cleanup.

The other 260 root-script claims are split across 149 `run_chains/` claims,
52 root-level Python claims, 43 root-level shell claims, and 16 launcher-JS
claims. Review live queue/lock scripts before chain callers. Historical or
archived chains still need an explicit disposition, but must not be rewritten
while a live shell is reading them. Launcher-JS claims require checking the
actual `pinokio.js` wiring and project examples before any edit.

### Root-1 resume/artifact contract to implement

This bundle covers IDs 14–16, 34–39, 45–46, 49, 73, and 77. The current
`.source` file identifies only a pathname; the orchestrator skips ASR and
enrichment when output files merely exist; annotation loads optional files by
existence; and `dataset_temp` is shared by direct invocations. These are one
lifecycle problem, not fourteen unrelated path checks.

1. Give the run one manifest in `dataset_temp` containing audio content
   identity, source identity if supplied, effective processing options, and
   each phase's completed artifact identity and expected shape. A fresh run
   must discard prior generated phase artifacts before the first phase. Resume
   must refuse a changed identity or incompatible options, never silently
   combine old and new entries. Parent and child phases must read the same
   manifest; file existence alone cannot authorize reuse. This covers 14–15,
   35–36, 45, 73, and 77.
2. Serialize ownership of the shared work directory across independent CLI
   processes, including a full orchestrated run and direct phase calls. Do
   not remove the existing FastAPI GPU guard; the directory lock protects
   artifact integrity, while GPU coordination is separately tracked by #86.
   This covers 39.
3. Validate every checkpoint record's shape, timeline, and local
   `sample_NNNN.wav` basename before returning a resume point or packaging a
   ZIP. Reject or truncate a bad tail before appending; refuse path traversal
   and absent WAVs rather than packaging arbitrary files. This covers 34 and
   46 (and overlaps packaging claim 18).
4. Stage JSON phase artifacts to same-directory temporary files, validate
   their shape, then atomically replace and mark their phase complete. Fsync
   the WAV directory before making its metadata record durable. Only tag newly
   generated WAVs; never delete or overwrite a caller-owned `--scratch-audio`
   path. This covers 16, 37–38, and 49.

Regression matrix: book B after book A; same path with changed audio bytes;
changed chunking/model/source options; killed ASR/enrichment write; old list-
shape enrichment; malformed checkpoint object; `../../secret.wav`; missing
referenced WAV; two simultaneous direct CLI runs; resume with existing tags;
and caller-owned scratch pointing at an existing WAV or the input audio.
Only mark this bundle fixed once these behaviors are exercised, not merely
because manifest serialization itself has a green test.
