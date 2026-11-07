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
3. Both builds are owned CPU background tasks (`readalong_sidecar`,
   `readaloud_epub`) in `process_state` with owner-fenced cancel,
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
- CPU-only tasks under the GPU exclusion set; ordinary merge/M4B/
  chapter paths unchanged.
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
