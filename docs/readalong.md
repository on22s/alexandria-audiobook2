# AA2 read-along: sidecar (#1720) + EPUB 3 read-aloud (#1779)

Integration is inside the Result/export workflow, not a disconnected CLI.

## Flow

1. Editor `Merge All` publishes `cloned_audiobook.mp3` exactly as before.
   Ordinary exports unchanged and never disclose book text.
2. Result tab disclosures (opt-in):
   - **Read-along sidecar**: builds a bounded JSON sidecar bound to a
     freshly encoded MP3 (see Audio authority below). No Merge All is
     needed first — the build makes its own MP3 from current Editor
     rows and current settings. User explicitly chooses "Include book
     text" (default off: timings/sections only). Download sidecar +
     matching MP3 + standalone player. Local player runs outside
     Editor with only the audio + sidecar files.
   - **EPUB 3 read-aloud**: builds a source-preserving EPUB 3 with
     SMIL Media Overlays from a fresh MP3 encode, using the retained
     original EPUB. No Merge All is needed first. Fails closed with
     editor text when no synchronization can be justified.
3. Both builds are owned background tasks (`readalong_sidecar`,
   `readaloud_epub`) in `process_state`. Sidecar and chunk timing use CPU;
   Qwen timing uses the admitted CPU or GPU device, with GPU builds
   reserving the shared GPU lock. Both builds have owner-fenced cancel,
   shared integrity approval (`ensureMergeIntegrityApproval`, same
   snapshot semantics), `admitted_snapshot` comparison in the worker,
   `verify_sources()` and `ensure_publication_transaction` publication
   with rollback. Downloads require a current receipt and serve
   exactly the validated bytes (see R2 below).

## Audio authority (fresh encode)

Every build fresh-encodes the MP3 from the current PCM timeline through
the ordinary encoder/quality/admission path (`write_combined_pcm` ->
`get_pcm_export_encoding_plan` -> `export_pcm_mp3` ->
`get_admitted_export_report` -> `ensure_export_stage_current`) and binds
the sidecar or EPUB to that fresh file's exact bytes (`audio_sha256`).
The pre-existing `cloned_audiobook.mp3` is never trusted, rehashed or
embedded: text/pause edits after a merge cannot bind a stale MP3, and
wrong/changed audio fails closed. Fresh MP3 + sidecar (or EPUB +
receipt) are published through `ensure_publication_transaction` with
`admit()` per mutation, backups and reverse rollback (chapter-export
pattern). Ordinary renames are not crash-atomic: a hard kill between
renames can leave a mismatched pair; a fresh rebuild is the recovery.
Rollback covers ordinary exceptions where the process survives.

Synchronized coverage (R4): every admitted editor row must decode.
A missing/corrupt clip fails the whole build before publication; the
app has no legitimate non-spoken skip policy for this artifact, and a
partial timeline is never published as synchronized. Coverage is
checked against real row identities/order, not the loader's skipped
counter alone.

Frame truth (R4): entry bounds and total frames are the shared
combined PCM plan's `source_frame_ranges` in the final promoted
layout (`write_combined_pcm` / `get_combined_source_plan`, already
verified against the produced PCM). Pause gaps are the gaps between
those ranges. Byte-count division and rounded `iter_timeline` anchors
do not govern timing. MP3 decoded samples include codec delay /
priming / padding and are never claimed equal to PCM frames.

Publication receipt seam (R4 for R2): app builds require a receipt
binding the captured book token, saved rows, source SHA and pause
settings, plus a settings digest that hashes config bytes. The source
snapshot checks config identity again before publication; config drift
refuses publication. A missing identity fails closed. The portable sidecar helper keeps an optional
receipt for standalone use. The sidecar embeds the receipt as
`source`; EPUB publishes `readaloud.receipt.json` beside the EPUB in
the same transaction with `{book_token, rows_digest, source_sha256,
settings_digest, retained_epub_sha256, audio_sha256, epub_sha256}` (no
paths). The `settings_digest` (pause defaults + config bytes) is an
R2 receipt addition: pauses/config changes can stale a synchronized
export even when book/rows/source are unchanged, and downloads
recompute it from live state. Retained
EPUB bytes are hash-verified in memory at locate time
(`sha256(retained_bytes)` must equal the publication-capture origin);
the build uses those bytes and never re-reads the retained file, so
later retained-file mutation cannot change the built EPUB. The
snapshot separately guards the original TXT identity.

## Decisions

- Sidecar binds to the fresh encode, not to a pre-existing file. The
  timeline is recomputed with `iter_timeline`/`write_combined_pcm`
  frame plan + `PCMSourceStore` so pause/trim stays frame-exact (#1379
  frame clock). Bounds/total are the plan's promoted source ranges;
  trimmed head/tail word spans stay `aligned-estimated` (linear within
  the trimmed span, never frame-certified). SMIL clips that collapse
  to zero or overlap after rounding to milliseconds are refused (R4 clock truth):
  padding them would invent drifted durations. Player/body verify the
  hash; wrong or changed audio is rejected.
- Word timing is *reuse only* (no ASR run, no inference/billable
  call): `editor_word_timing.get_timing_inputs` /
  `get_cached_word_timing` are tried with the exact binding, resolved
  via the real book-token (`get_book_operation_identity`) and provider
  (`get_whisper_cpp_runtime`) helpers. Statuses: `aligned-estimated`
  (ASR spans, mapped with true trim-head/tail from
  `get_trim_bounds_ms`, never frame-certified), `chunk-estimated`
  (chunk bounds only), `missing`, `ambiguous` (no word spans).
  Without true trim coordinates the transform is `ambiguous`, never
  proportionally stretched.
- EPUB mapping normalizes source XHTML text and script prepared text
  (`get_speech_preparation`) with exact normalized token coverage as
  the authority; similarity is only a hint. Normalization is NFKC +
  casefold + word tokens (case/punctuation intentionally ignored; no
  pronunciation or text-parity claim). Sustained mismatch, a
  changed word at high ratio, a missing interior/leading chunk, or a
  split beyond exact coverage fails closed. Many chunks may form one
  segment when coverage is exact; one chunk spanning multiple segments
  has no segment boundary and is refused explicitly (no invented
  sub-paragraph timing). Every `body` text node (excluding
  `head`/`script`/`style`) is counted once in source order — block
  leaves plus container coarsening for mixed direct/block content —
  while markup and existing stable ids are preserved. EPUB2 input is
  upgraded to EPUB3 output with a generated `nav` from the NCX
  (labels/order/hrefs preserved; `guide` dropped, NCX and resource
  bytes retained); spine/nav/cover/metadata are otherwise preserved.
- Retention (R5 original-bytes authority): original EPUB bytes are retained
  under the existing upload transaction (`routers/script.py:upload_file`)
  as `<stem>.source.epub` beside the extracted TXT, under the same quota
  lease. Calibre imports reuse `upload_file`, so they are covered without
  a second authority. A canonical TXT is reused only when the stored
  retained file holds the identical original bytes (fresh SHA-256 match
  through the existing bounded descriptor capture with before/after
  file-identity checks; the TXT dedupe hash cache is not authority);
  identical bytes under a renamed filename are a duplicate with no new
  file and no duplicate quota charge. A different EPUB with identical
  extracted text refuses with 409 before any companion/retained change:
  the old original is never silently reused, no existing archive is
  overwritten, and no new candidate is published for bytes that were
  never retained. A genuinely missing retained file is attached only
  for an edition already present in provenance (or when no provenance
  exists yet), at the exact `<stem>.source.epub` name; a foreign regular
  file, symlink, hardlink or oversized file at that name also refuses
  with 409 and is never overwritten. New groups jointly select one
  stem for TXT + `*.publication.json` + retained so collision numbering
  can never split the group (no `book_2.source.epub` with `book.txt`).
  Retained + TXT + companion quota is reserved before any publish, so a
  413 leaves no new retained file and legacy-attach 413 leaves the
  existing TXT/companion byte-identical. Companion+archive reservation
  is validated before publication starts. Once a retained file is
  visible it is never unlinked after a later failure: `publication_started`
  is unknown-protecting and stays unknown, and a definitive pre-TXT
  failure reports the retained filename as quota-accounted recovery
  (HTTP 409); unknown writer-close `OSError` stays `OSError` with all
  already-published members retained. The bounded inventory identifies recovery groups for manual backup
  and storage cleanup; it does not delete files. `GET /api/uploads/epub-retained` returns a bounded,
  metadata-only inventory (streaming scan consuming at most
  `scan_limit + 1` entries, validated caps, no content/hash reads, no
  deletion) grouping TXT + retained + companion, including orphan/
  partial groups and collision-numbered names. Returned groups are
  sorted; the scanned subset is the first `scan_limit` filesystem
  entries so beyond-limit omission follows filesystem order. Observed
  bytes are a partial count of scanned named regular files; locate
  remains deterministic (`sorted` names; multiple hash matches fails
  closed). Compatibility: a second distinct edition that previously
  added a second candidate (up to the four-origin cap) now refuses;
  back up and manually remove the original group before importing that
  edition as a new book. The inventory itself has no delete action. The four-origin cap remains as defence-in-depth.
- Shared contracts: standalone player and Python validators enforce
  the same full shape (version, size/shape/frame bounds, sections,
  bookmarks, word offsets, text choice, forbidden fields). The player
  requires verified audio (SHA-256 hash match plus byte-size match
  when the binding carries `audio_bytes`) before synchronized
  highlighting or seeking; generic audio playback remains available
  as audio-only with a truthful unverified status. Audio/sidecar/
  bookmark loads use independent per-input generations, the current
  audio hash is retained so either load order reconciles, and every
  synchronized action (entry/section/bookmark seek, add/
  export) is gated on the currently verified pair at handler
  execution. Bookmark import is bound to the selected sidecar and may
  finish before audio verification; replacing the sidecar discards an
  older pending import. Word spans use half-open `[start, end)` frames and the
  player converts Python-codepoint word offsets to UTF-16 for
  display. Bounded pre-read is 64 MiB sidecar, 2 GiB audio. The
  Result tab uses the `API` helper with typed terminal results
  and task-fenced polling.
- Portable bookmarks: `aa2-readalong-bookmarks` v1 carries the full
  timeline binding (`audio_sha256` plus `sample_rate`/`frames`/
  `channels`/`sample_width` and byte/format/duration when present),
  a bounded list (max 10,000), unique ids (max 64 codepoints),
  labels (max 500 codepoints), integer frames inside the timeline
  and valid section refs. Invalid documents reject atomically;
  an empty list clears intentionally; add-bookmark clamps decoder
  padding beyond the PCM timeline and refuses additions at the limit
  so saved bookmarks stay valid. Embedded and imported bookmarks use
  the same bounds.
- Upstream ReadAloudMaker (MIT, 2026 ReadAloudMaker contributors):
  pinned commit `5928795e9188222f75ea4776feabc6e6445d6379`
  (LICENSE blob `77c87ac817dc4a6e313c9020fdf45a6c404e9e64`,
  `make_readaloud.py` blob `8ef32ac9995c44bd0c12efdc84889451c74d8dd5`).
  Only packaging/reference-validation ideas are adapted (attributed
  below); `aeneas` (AGPLv3) and `syncabook` are not used.
  No new dependencies were installed.

## Costs and limits

- Sidecar: `aa2-readalong-sidecar` v1, max 64 MiB, max 200,000
  entries, max 4,096 words per entry, max 20,000 chars per entry
  text, max 10,000 sections/bookmarks. Oversized/malformed payloads
  fail closed in builder and validator. The standalone player (R3)
  enforces the same bounds. Download handlers serve exactly the validated bytes: the sidecar
  bytes are validated then returned as the body; the EPUB bytes are
  hashed against the receipt, structure-validated, then returned;
  the matching MP3 is pinned by an open descriptor
  (`AudioStreamResponse`), hashed from that same descriptor, rewound,
  and streamed — a replacement after verification can
  never change the bytes served.
- EPUB downloads load the entire archive, including its embedded MP3,
  into memory for hash and structural validation on every request, then
  return those validated bytes. Large books can require substantial memory;
  this path does not stream the EPUB. Matching MP3 downloads stream a
  verified open descriptor, but still hash the entire MP3 before serving.
- The read-aloud EPUB already embeds its audio. Its matching-MP3 download
  is optional; no separate MP3 is required by a Media Overlays reader.
- Direct download links can display a JSON refusal when the artifact is
  stale; rebuild it from the Result tab before downloading again.
- Audio: fresh encode is local CPU work (encoder + quality decode);
  no inference or billable calls. Word timing reuses cached spans
  only. Retention counts toward the existing upload quota
  (`MAX_UPLOAD_STORAGE_BYTES`); the inventory is metadata-only.
- Sidecar and chunk timing use CPU. Qwen GPU timing reserves the shared
  GPU lock and is refused while conflicting GPU work is active;
  wait for that work to finish and start the build again. Ordinary
  merge/M4B/chapter paths are unchanged.
- EPUB `readaloud.receipt.json` is the R2 seam described above; it
  adds one small JSON beside the EPUB in the same transaction. EPUBs
  built before this slice have no receipt and are refused with 409
  (unbound; rebuild the EPUB to bind it).

## App tasks and current downloads

R2 is implemented in `app/routers/editor.py` (starts, cancels,
downloads), `app/static/js/app-readalong.js` (Result-tab flow),
`app/static/js/app-core.js` (shared integrity approval + reset
wiring), `app/static/index.html` (Result-tab text, matching-MP3
links), `app/project_scope_exports.py` (scope fencing incl. the new
artifacts with legacy inventory compatibility), `app/core.py`
(owner admission for the two task names), and `docs/`. No other
owner/book/worker semantics were changed. Independent R2 slice review
approved these contracts; the combined feature still requires final
acceptance and release checks (see readalong-acceptance.md).

Ownership: a start reserves the scheduler claim before initializing
state, initializes `result` before registration, and returns
`{status, task_name, claim_id, book_token}`; the standard
`/api/status/{task}` exposes `task_claim_id` + `readalong_book_token`
for polling. The worker carries its start generation and writes
`process_state` only while still the current generation, so stale
workers cannot overwrite a newer owner's result or logs, and an
eager worker completing during registration is never erased. Cancel
accepts an expected owner (`{claim_id, book_token}`) and refuses a
mismatched cancel; the `(ExportCancelled,
AudioGenerationCancelled)` path ends the worker `cancelled`. The two
tasks joined the exact-owner admission in `_run_owned_background_task`,
so `ExportSourceSnapshot` publication decisions honor cancellation.
Integrity approval is the existing `ensureMergeIntegrityApproval`
flow (same snapshot/renders semantics) with read-along action
wording; the button is disabled while busy and the poll checks the
returned claim/book identity. Terminal results are handled whenever
they arrive (the 5-minute note is informational only); malformed
terminal and network failures stop after a bounded error budget
(8); a poll ceiling (1800) stops waiting with a plain message.
`window.resetReadalongViews` is called from all three book paths
(`applyCurrentBookFilename` plus the two import paths in
`app-core.js`) and fences every start/poll/cancel continuation.

Currentness: downloads recompute the live identity (book token,
saved-rows digest, source-bytes SHA, settings digest) and require
every receipt field to match, plus the audio SHA for the current
MP3, plus the retained-EPUB SHA for EPUBs. Legacy unbound
artifacts, missing/tampered receipts, replaced audio/source/rows/
config, cross-book artifacts and a foreign artifact left behind
after the shared MP3 changed are all refused with 409. Sidecar and
EPUB downloads return the exact validated bytes; the new
`/api/readalong/sidecar/audio` and `/api/readaloud_epub/audio` pair
MP3 downloads stream the descriptor-pinned MP3 verified against the
receipt. The sidecar/EPUB/receipt files joined
`project_scope_exports.EXPORTS`; inventories persisted with the
legacy set remain accepted. Publication stays honestly non-crash-
atomic (receipts detect mismatched pairs; a hard kill between
renames still needs a rebuild).


See `docs/readalong-acceptance.md` (filled after freeze with measured
counts, size/timing costs, SHA256 manifest).

## Qwen3 sentence timing (#1844)

Qwen3-ForcedAligner-0.6B refines segment timing to sentence granularity.
AA2's content policy is unchanged: the build still runs `iter_timeline`
frame truth first, still fails closed when mapping is inexact, and the
Qwen result never approves content that mapping refused. Alignment is
real model inference behind this build's admission — no proportional,
fake, or word-count timing exists in the production path.

- **Placement.** Inference never runs in the export task's thread. The
  export exact-PCMs (`combined.pcm` at the promoted bounds) are sliced
  per editor row and aligned in an owned subprocess worker
  (`app/qwen_align_worker.py`) in the isolated `qwen-env` interpreter,
  with the same owned-process cancel/timeout/reap behaviour as
  `word_timing.run_timing_command`. The parent enforces `HF_HUB_OFFLINE=1`
  and `local_files_only=True`; the worker loads a provisioned local
  model directory through the official
  `Qwen3ForcedAligner.from_pretrained(..., dtype, device_map,
  local_files_only=True)` + `align(audio=path, text, language)` API
  (QwenLM/Qwen3-ASR commit 7c6daf77a2421100f5fb066495372c00129d39ff;
  HF model `Qwen/Qwen3-ForcedAligner-0.6B`, pin c7cbfc2048c462b0d63a45797104fc9db3ad62b7).
- **Admission.** `readaloud_epub` was promoted out of `NON_GPU_TASKS`:
  chunk timing claims `cpu_only=True` (unchanged behaviour), Qwen on a
  GPU claims through the shared GPU lock. The effective device is
  resolved exactly once, before `reserve_background_task`, and pinned
  for the worker; a worker cannot auto-upgrade after admission. Cancel,
  book switching and owner fencing are the existing mechanisms.
- **Chunk vs Qwen labels.** Chunk timing is `chunk-estimated` (chunk
  bounds only). Qwen clips are labelled as Qwen timing and never claim
  measured accuracy or frame certification; token times are model
  estimates and MP3 priming stays unclaimed.
- **180s per-call bound (AA2's, not upstream's).** The standalone
  upstream `align()` itself enforces no input-length limit:
  `MAX_FORCE_ALIGN_INPUT_SECONDS = 180` lives in the SDK's
  `inference/utils.py` and is used by the ASR *transcribe* wrapper
  (`qwen3_asr.py`), not by the forced aligner. AA2 nevertheless caps
  every real inference slice at 180s as a conservative bound. A whole
  editor row may exceed 180s: it is aligned as bounded sequential
  slices (below) and validated as a row, never as one inference call.
- **Sequential measured-cursor scheduling.** Rows are scheduled from
  the row's estimated speech rate with a 20s text target, a 3s audio
  tail, and 3s incremental growth. The worker (model loaded once)
  slices each row sequentially: every slice starts at the measured
  previous token end (rounded up to the next actual PCM frame, so a
  slice never starts before the preceding word's end), grows by 3s
  steps up to the 180s per-call cap while speech reaches the slice
  end, and advances only by actual frames — including a final
  sub-500ms remainder, which grows if at least one frame advances.
  The 3s tail is a scheduling target: at the actual row end or hard
  call cap, fully validated tokens may use the remaining headroom.
  The cap never permits an inference call longer than 180s.
  Word-rate estimates only schedule inference; clips come from actual
  model tokens. A single sentence up to the 180s cap gets its own
  window (no 20s refusal); a sentence needing more is partitioned at
  word boundaries, with final sentence envelopes derived from actual
  tokens. Parent validation of worker-reported windows is structural
  consistency (frame bounds, text scopes, call bound, cursor
  continuity) plus preserved provenance — it does not prove acoustic
  correspondence, and no all-language or device accuracy is claimed.
- **Encoder attention adapter.** The bundled non-FA2 audio encoder
  computes `cu_seqlens` but omits its block-diagonal attention mask
  (upstream QwenLM/Qwen3-ASR issue 213 / PR 103). The worker wraps
  each encoder layer on its private model instance so a missing mask
  is filled via the tower's own `_prepare_attention_mask`; Flash
  Attention 2 keeps its native semantics. Unexpected backends or a
  repeated install fail closed; site-packages is never patched.
- **Bounded maximum-score decoder.** Official token texts are kept,
  but times come from `app/qwen_score_decoder.py` (worker-only): a
  bounded maximum-score monotonic path with strictly positive word
  bins computed from real model timestamp scores (borrowed invariant
  from hangry-labs/Qwen3-ASR-STT @97ad227, Apache-2.0; see
  `THIRD_PARTY_NOTICES.md`). An already-valid argmax path is kept
  verbatim; otherwise the score search runs. NaN/Inf, odd counts,
  oversize or impossible paths fail closed. Predicted score size is
  checked before inference via the SDK's own
  `aligner_processor.encode_timestamp`, and actual tensor shape is
  checked before any conversion. This decoder deliberately differs
  from the donor's LIS fast path, and the parent diagnostics that
  used the donor's exact function are not proof of this code.
- **Provider-code identity.** The provider digest also hashes the
  actual bytes of `qwen_align_worker.py` and `qwen_score_decoder.py`,
  so changing the encoder adapter or decoder changes identity and a
  stale artifact cannot download as fresh.
- **Staging PCM formats.** Row staging supports the core PCM contract
  widths 1/2/4 (see `get_pcm_source_shape`); the official Qwen decode
  path (soundfile/librosa into mono float32) has no 16-bit
  requirement. Signed 16/32-bit little-endian PCM is written
  byte-exact; signed 8-bit internal PCM is biased to unsigned 8-bit
  WAV bytes (bias 128, the same rule as the existing audio export
  paths). Rate, channels, exact frame range and waveform are
  preserved; staging is a copy and never modifies source PCM/MP3.
- **Fail closed.** Non-finite, reversed, overlapping, saturating
  (identical endpoints), out-of-bounds, text-mismatched, partial or
  empty results refuse the whole build before publication. No
  monotonic repair, no linear stretch, no proportional timings.
  (Note: upstream `Qwen3ForceAlignProcessor.fix_timestamp` already
  repairs out-of-order timestamp sequences internally; timings here
  are AA2-validated *model* times, never claimed as raw unrepaired
  model output or as ground truth. All AA2-side clamp/stretch is
  absent by design.)
- **Structure.** Sentence spans wrap sentence ranges inside mapped
  block elements; inline markup/tails and every character are kept
  exactly once and original ids stay. Coarsened containers with nested
  blocks, and any structure the span wrapper cannot redistribute
  faithfully, refuse explicitly as unsupported.
- **Receipt v2/v3.** Qwen artifacts carry `{timing_mode, timing_device,
  timing_dtype, timing_language, timing_provider}` (on-disk model/interpreter digest)
  alongside the 7-key identity; new Qwen builds (v3) additionally carry
  human-readable `{timing_model, timing_qwen_asr_version}` (AC8: the
  supported `Qwen3-ForcedAligner-0.6B` engine identifier plus the
  validated qwen-asr version — provenance labels, never a measured
  acoustic-accuracy claim or proof of an owner-provided weights'
  origin; the provider digest remains the actual identity).
  Pre-publication revalidation compares the live digest. Download
  recomputes timing identity from the persisted preference
  (`readalong_timing.json`) plus the model digest: a provider
  replacement or a timing-choice change since the build returns 409.
  Legacy 7-key and v2 receipts keep existing semantics; arbitrary
  extra receipt keys are rejected.
- **Install (isolated, explicit).** The main app keeps its pins
  (transformers 4.57.3); nothing downloads during a build. To enable
  Qwen: create a Python 3.12 venv (`qwen-env/`), install per-platform
  torch, then `pip install -r app/requirements-qwen.txt`, then
  download the model ONCE with the hf CLI:
  `pip install -U "huggingface_hub[cli]"` +
  `hf download Qwen/Qwen3-ForcedAligner-0.6B --local-dir models/Qwen3-ForcedAligner-0.6B`,
  or set `ALEXANDRIA_QWEN_MODEL` to a local directory (and
  `ALEXANDRIA_QWEN_PYTHON` to an explicit interpreter). Capability comes
  from `GET /api/readaloud_epub/capability` (import/device presence
  only, no inference); unknown is never shown as ready.
  The documented HF local download creates `.cache/huggingface`
  bookkeeping; this subtree is excluded from model identity. Actual model
  data stays bounded and content-hashed, and referenced shards must exist.
  Directory traversal counts skipped names too and refuses links in model
  data. Model names and sizes are length-framed in the content digest.
- **GPU metadata without an inference context.** The isolated requirements
  include NVIDIA's `cuda-bindings` (except on macOS) and `nvidia-ml-py`.
  CUDA driver metadata supplies logical ordinals and UUIDs, respecting its
  visibility/order settings; NVML supplies the matching device's compute
  capability and current memory. The probe creates no CUDA context. Missing
  or unknown GPU metadata excludes that device; a working CPU runtime can
  still be offered, and an explicitly unavailable GPU choice refuses.
  A CPU-only Torch installation does not advertise a physical CUDA GPU.
- **Bounded output.** The worker checks actual encoded result bytes as rows
  finish and stops before aligning further rows if the existing 16 MiB
  result limit is exceeded. The independent 64 MiB job limit also remains.
  These are resource limits, not a promise that every book fits. An oversized
  job or result refuses publication and leaves the prior export intact;
  chunk timing remains the alternative. No timestamps are guessed to fit.
- **Bounded sentence reconstruction.** Each mapped source segment is limited
  to 200,000 raw characters before provenance normalization. Inline indexing
  allows at most 1,048,576 events and accumulated stack references. Per XHTML
  document, reconstructed sentence content has an 8 MiB charged-byte budget,
  including escaped text, attributes and namespace/element overhead. These
  conservative work budgets refuse before further cloning; they do not measure
  peak RAM or acoustic quality. Chunk timing remains the alternative for an
  EPUB outside these sentence-mode bounds. Retained EPUB reads also enforce the
  existing 2 GiB retained-read bound on the opened regular file, poll cancellation,
  and reject a file that changes during the read.
- **What this project does not use.** Aeneas (AGPLv3) and Syncabook are
  not dependencies of this app; no parallel packager exists. The
  sentence idea and parts of the tokenizer/window logic adapt
  ReadAloudMaker (MIT, commit `8ba70a83383e8a5bddd8b859016ffbbcefccba18`)
  under attribution in
  `THIRD_PARTY_NOTICES.md`; its standalone server, build, and
  packager/validator choices are not transplanted. This sentence-timing
  donor revision is distinct from the earlier packaging reference above.
