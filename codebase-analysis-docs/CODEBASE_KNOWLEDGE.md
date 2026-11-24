# Alexandria Audiobook2 — Codebase Knowledge

**Analysis target:** `a978efa252e042a16a28e9cabea1a85afc06b9a9` (`origin/main`, 2026-11-24, merge of PR #2263).

**How this document was produced.** Read-only analysis of that tree: every `app/*.py`
module's docstring and public definitions, every FastAPI route, every frontend
module, the config models and the on-disk constants were extracted mechanically
(AST for Python, decorator parse for routes, regex for the JS modules), then the
cross-cutting behaviour (transactions, admission, identity, export) was read in
the source. Counts in this document are measured, not estimated; the Appendix A
block records the exact numbers and the command shapes that produced them. Where a
statement is an inference rather than a measurement it is phrased as one.

**This file replaces the 2026-11-20 analysis** (`INDEX_VERSION: 2026-11-20-c91bee0f`),
which described the tree at `c91bee0f0` and was 406 modules and roughly four weeks
of merged work out of date. The old numbers (`app/` 304 files, 17 routers,
1,339 test modules) are superseded by the Appendix A values.

---

## 0. How to read this document

- **New to the project:** §1, §2.3, then §3.1–§3.7 in order.
- **About to change code:** §4 first. It lists the invariants that fail silently.
- **Looking for a file or route:** §5, then Appendix B/C.
- **Reviewing architecture:** §2. Diagrams live in `codebase-analysis-docs/assets/`
  (`architecture.mmd`, `book-flow.mmd`, `data-model.mmd`, `task-admission.mmd`).

Repository paths are relative to the repository root unless stated otherwise.
`app/` is the application; launcher scripts (`install.js`, `start.js`, `reset.js`,
`update.js`, `pinokio.js`, `launcher_lifecycle.js`) sit at the root and are a
separate concern (see §2.8).

---

## 1. Executive summary

### 1.1 What the application is

Alexandria Audiobook2 (AA2) is a local-first, single-user web application that turns
a book into a multi-voice audiobook.

Input: `.txt`, `.md`, `.epub`, `.docx` — uploaded, reused from the uploads
directory, or pulled from an optional Calibre OPDS catalog (§3.13).

Processing:

1. A language model annotates the book in three passes:
   **segment** (narration vs speech), **attribute** (speaker per line), **instruct**
   (delivery direction per line).
2. The user reviews the script, resolves aliases, and casts a voice per character.
3. A TTS engine renders each chunk — local Qwen3-TTS, an Apple MLX backend, or a
   pool of external Gradio TTS servers.
4. The user listens, edits lines, regenerates individual chunks, and can run
   optional checks (voice drift, chunk outliers, ASR word timing, speech
   verification).
5. The app assembles the chunks into MP3, chaptered M4B, per-chapter files, an
   Audacity project, or an EPUB 3 read-aloud package, with optional sound design
   and mastering, then optionally delivers the result to an Audiobookshelf server.

A second toolchain, **Voice Lab**, turns existing audiobooks into aligned training
data, trains LoRA voice adapters, evaluates them blind, and promotes them with an
audit trail.

### 1.2 Business purpose and target users

The purpose is **high-quality, repeatable audiobook production on hardware the user
owns**, where every quality claim is measured rather than asserted. The codebase is
unusually explicit about what it does *not* prove: a word-timing match is not a
pronunciation verdict, an outlier score is not a quality qualification, a casting
tag is not an identity, a delivery receipt is not an approval.

Primary users:

- A single author/producer converting their own library on one machine (the shipped
  assumption: one active book, one GPU, one user).
- Someone building a voice catalogue: designed voices, cloned references, LoRA
  adapters, and a casting library that spans books.
- An experimenter measuring attribution/segmentation quality across models and
  prompt variants (`app/experiments/`, `benchmark_*`).

### 1.3 Feature map

| Area | Where | Summary |
|---|---|---|
| Setup & LLM provider | `routers/system.py`, `lmstudio_settings.py`, `llm_provider.py` | Local LM Studio, remote LM Studio over SSH/forwarded URL, hosted OpenAI-compatible APIs, manual transport; failover and concurrency benchmarking |
| Source ingestion | `routers/script.py`, `source_*.py`, `docx_*.py`, `epub_*.py`, `calibre_*.py` | Upload, selection binding, EPUB/DOCX extraction, Calibre OPDS import, source repair |
| Script generation | `generate_script.py`, `three_pass_generate.py`, `pass_quality.py`, `compact_pass1.py` | Single-pass and three-pass annotation, chunk quality gates, checkpoint shards, retries |
| Review & speakers | `review_script.py`, `routers/speaker_review.py`, `speaker_identity.py`, `character_roster*.py` | Second LLM pass, alias registry, character roster review, printed-label attribution |
| Casts & voices | `routers/voices.py`, `routers/voice_library.py`, `routers/voice_design.py`, `routers/casting.py` | Personas, designed/cloned/LoRA voices, casts, casting library across books |
| Editor & synthesis | `routers/editor.py`, `project.py`, `tts.py`, `synthesis_*.py`, `take_*.py` | Per-line render, takes, restore, undo, word timing, speech verification, narration markup |
| Export & publication | `routers/editor.py` export routes, `export_*.py`, `audio_*.py`, `m4b_*.py`, `readalong_*.py`, `epub_readaloud.py` | MP3/M4B/chapters/Audacity/read-along/EPUB, mastering, quality receipts, delivery |
| Sound design | `sound_design_*.py`, `routers/sound_design.py` | Cues, asset library, mixed candidates, score lineage, approval and delivery |
| Voice Lab | `routers/voicelab.py`, `routers/preparer.py`, `routers/dataset_builder.py`, `routers/lora.py`, `tools/voice_lab/` | Audiobook → dataset → LoRA → profile → named voice |
| Project scope | `project_scope_*.py`, `routers/project_scope.py` | Reversible removal of a suffix of the book, with exact undo |
| Workspace & reports | `routers/scripts_library.py`, `collection_overview.py`, `book_workspace.py`, `run_history.py` | Saved-book library, per-book stage status, run history, diagnostics |
| Benchmarks | `benchmark_*.py`, `routers/benchmark.py` | Local-vs-Thunder stage benchmarks with environment fingerprints |

### 1.4 Technology stack

- **Backend:** Python 3.10 (`app/env`), FastAPI + Starlette + Uvicorn, Pydantic v2
  models as the config shape boundary, `pydub`/`soundfile`/`numpy` for audio,
  `ffmpeg` for encoding, `rapidfuzz`/`difflib` for text comparison.
- **TTS:** Qwen3-TTS (CustomVoice / clone / LoRA), Apple MLX backend, external
  Gradio servers, optional `whisper.cpp` (CPU) for ASR, Qwen3-ForcedAligner for
  read-along alignment.
- **Frontend:** two static pages (`app/static/index.html`, plus
  `app/static/readalong-player.html` for the standalone read-along player) and
  **45** vanilla-JS modules under `app/static/js/` (no bundler, no framework),
  Bootstrap 5 and Font Awesome from `app/static/vendor`.
- **Storage:** JSON files and audio on disk. No database. Every multi-file
  publication goes through a journal (`book_state_transaction.py`, 6,700 lines) or
  a staging/retirement transaction.
- **Concurrency:** threads and subprocesses inside one server process, coordinated
  by `process_state`, a GPU mutex, cross-process task leases, per-book file locks,
  cancel events, and book-state transactions.

---

## 2. System architecture

### 2.1 Runtime topology

```
browser (SPA, 45 JS modules)
   │  fetch /api/*
   ▼
Uvicorn + FastAPI  (app/app.py, 381 lines)
   │  19 routers (351 routes)  ──────────────►  request validation (Pydantic)
   │
   ├─ asyncio.to_thread(...)  ── long routes never block the event loop
   │        │
   │        ▼
   │   background task (process_state entry)
   │        ├─ LLM calls            (llm_provider → LM Studio / hosted API / manual)
   │        ├─ TTS inference        (tts.py → torch on the local GPU)
   │        ├─ ffmpeg / workers     (subprocess_ownership, Windows job / macOS task)
   │        └─ ASR / aligner        (whisper.cpp, Qwen3-ForcedAligner — separate envs)
   │
   └─ filesystem: DATA_DIR (JSON + audio), SCRIPTS_DIR (saved books),
      reports/, logs/api/, run_history/, take_history/, voicelines/ ...
```

`app/app.py` is deliberately thin: it owns the lifespan (startup recovery, orphan
sweeps), the security-headers middleware, the request-origin gate (cross-site and
DNS-rebinding refusal, GHSA-vp9p-437w-q5x9), a connection-reset filter for the
noisy "client went away" case, and the 19 `include_router` calls. **All routes live
in `app/routers/`**; new endpoints belong there, not in `app.py`.

Two router modules carry a prefix (`/api/character_roster`, `/api/speaker_review`);
the other 17 declare full paths.

### 2.2 Request and task lifecycle

1. The browser calls a route. Every mutating route validates its inputs (Pydantic
   model or explicit checks) before touching state.
2. A route that can take more than a moment hands the work to a thread
   (`await asyncio.to_thread(...)`) or starts a registered background task.
3. Background tasks register in `process_state` (`core.py:616`) with
   `running`/`logs`/`cancel`/`paused`, and claim admission through
   `reserve_background_task`/`claim_gpu_task` — never by setting `running`
   themselves.
4. Long loops check the cancel event at boundaries, write progress into
   `process_state`, and persist durable progress (checkpoints, journals, receipts)
   so a crash or restart can resume or refuse cleanly.
5. Publication of results is transactional: stage → validate → `os.replace` →
   fsync → recover on startup. Multi-file publication uses the book journal.
6. The UI polls status routes (`/api/status`, `/api/status/{task_name}`,
   `/api/chunks/status`, `/api/status/eta`) rather than waiting on the request.

Admission is two-layered and the layers answer different questions:

- **In-process:** `process_state` + `_gpu_lock` (`check_global_gpu_lock`,
  `core.py:763`) — "may this task start *here* right now".
- **Cross-process:** task leases in `.task_ownership/` (`task_ownership.py`) —
  "is another server/CLI already doing this". `process_state` is not cross-process
  truth.

### 2.3 Book-to-audiobook data flow

```
source file ──► uploads/ (owned stage, digest-bound)
   │
   ▼  selection binding (script_selection_binding.py)
annotated_script.json  ◄── generation checkpoints / review receipt
   │  (entries: speaker, text, instruct, spoken, source_span, …)
   ▼
voice_config.json  (per-character voice, style timeline, seeds)
   │
   ▼  project.py: chunk grouping, per-chunk generation inputs
chunks.json + chunks.json.status.jsonl  (per-UID row: text, speaker, take,
   │                                     audio_path, audio_revision, receipts)
   ▼  tts.py / synthesis_*.py  (one owned render per chunk)
voicelines/*.wav|mp3  (selected take; every superseded take optionally in
   │                   take_history/ as an immutable snapshot)
   ▼  project.py + export_*.py  (plan → PCM assembly → encoder → quality receipt)
exports: audiobook.mp3, audiobook.m4b, chapter_exports/, Audacity ZIP,
         read-along sidecar JSON, EPUB read-aloud package, sound-design mixes
```

Two invariants shape the whole flow:

- **The chunk row is the selection.** `audio_path` alone says which file is the
  current take; history, retention receipts and restore metadata never select
  audio by themselves.
- **Generation inputs decide staleness.** `get_chunk_generation_inputs`
  (`project.py:229`) hashes the fields whose change makes a captured render
  obsolete — text, speaker, instruct, take, narrator/style timeline fields,
  voice revision, pronunciation rules. UI-only fields do not invalidate audio.

### 2.4 Data and identity model

The application has no database and no foreign keys. Identity is carried by
fingerprints, and almost every hard bug in this codebase's history is a fingerprint
that was too loose.

| Identity | Owner | Binds |
|---|---|---|
| `uid` (chunk) | `project.py` | One chunk row for the life of the book; audio filenames use it, `id` (list position) does not |
| Book token | `book_token.py`, `book_state_transaction.py` | Source/script/voice/companion bytes at the moment an operation was admitted |
| Script receipt | `script_receipt.py` | Script bytes + the cast/roster they were reviewed against |
| Generation checkpoint | `generation_checkpoint*.py` | Prompt variant, model, cast hash, context settings, pass settings |
| Voice revision | `voice_config` / voice library | The exact voice definition used for a render |
| Cast hash | `voice_config` | The roster the script was attributed against |
| Audio receipt | `audio_receipts.py` | The bytes of one rendered file plus the inputs that produced it |
| Take snapshot | `take_history.py` | An immutable copy of a superseded take, addressed by version |
| Export quality receipt | `export_quality_receipt.py` | One exported artefact's measured properties and the inputs it was built from |
| Delivery receipt | `audiobookshelf_delivery.py` | One upload to a specific Audiobookshelf item, with remote identifiers |
| Journal record | `book_state_transaction.py` | One multi-file publication: old/new hashes, staged paths, completion |

The rule that follows: **a receipt certifies the bytes it names, not the current
file at that path.** Replacing a file silently orphans its receipts, and the UI is
expected to say so rather than to imply the receipt still applies.

### 2.5 Concurrency, admission and recovery

- `process_state` (`core.py:616`) is a per-process dict of task name → state
  (`running`, `logs`, `cancel`, `paused`, timestamps). It is a UI/telemetry
  surface and a same-process guard, nothing more.
- `NON_GPU_TASKS` (`core.py:660`) and `GPU_TASKS` (`core.py:662`) decide which
  tasks own the GPU mutex. `LLM_TASKS` (`core.py:667`) names tasks that talk to a
  language model; `gpu_lock_conflicts` (`core.py:700`) computes what blocks what,
  with one deliberate relaxation: a **remote** LLM profile does not hold the GPU.
- `check_global_gpu_lock` (`core.py:763`) is the read-only check.
  `claim_gpu_task` / `reserve_background_task` are the atomic claim paths. Calling
  the check and then setting `running` yourself is a TOCTOU race (§4.1).
- Cross-process truth is the lease files in `.task_ownership/`
  (`task_ownership.py`): pid, host, task, book, heartbeat. `ensure_startup_recovery`
  only cleans when no foreign lease is held.
- Cancellation is cooperative. Workers poll a cancel event at boundaries; an
  in-flight model or ffmpeg call finishes first. A route that reports "cancelled"
  before the worker observes it is lying.
- Recovery surfaces exist per subsystem rather than one global resume:
  generation (`/api/generate_script/recovery[/detail]`), synthesis
  (`synthesis_windows`, `synthesis_recovery`), export staging
  (`export_staging.py`, `export_staging_retirement.py`), take retention
  (`take_retention*.py`), and the book journal (`recover_book_state_transactions`).

### 2.6 Security and trust boundaries

AA2 is a **single-user local application**. It has no accounts, roles or per-user
permissions. The only access control is **optional HTTP Basic auth**, enabled by
setting `ALEXANDRIA_AUTH_PASSWORD` (with `ALEXANDRIA_AUTH_USERNAME`), which keeps
the default Pinokio/localhost flow unchanged. Everything else is enforced at the
request and filesystem edges:

- **Origin gate.** `app/request_origin.py` (wired in `app/app.py`) refuses
  state-changing requests whose `Origin`/`Referer` is not the served origin, and
  refuses DNS-rebinding `Host` values, closing GHSA-vp9p-437w-q5x9. `CORS_ORIGINS`,
  `ALEXANDRIA_ALLOWED_ORIGINS` and `ALEXANDRIA_ALLOWED_HOSTS` extend the allowlists
  for LAN or reverse-proxy use. New mutating routes inherit this; do not add a
  route that bypasses it.
- **Security headers.** CSP, `X-Content-Type-Options`, `Referrer-Policy`,
  frame-ancestors, and a permissions policy are set in middleware.
- **Path containment.** `is_path_inside` / realpath checks plus explicit symlink
  refusal (`core.py`, `project.py`, `audio_paths.py`). User-supplied names are
  sanitised before they become filenames (`safe_filename`, voice/character name
  rules).
- **Subprocess ownership.** `subprocess_ownership.py` gives every child process a
  Windows job object / macOS process group / Linux process group so a crash or
  cancel does not leave ffmpeg or a worker running.
- **No secrets in the browser.** The LLM API key lives in `config.json` on disk;
  the UI receives a redacted view. Remote-provider calls are made server-side.
- **The manual transport is a human channel.** It waits for a human to paste model
  output; it has no timeout and no integrity guarantee beyond shape validation.

### 2.7 Logging and observability

Four separate surfaces, each with a different job:

1. **Task telemetry** — `process_state[name]["logs"]`, a bounded ring buffer
   rendered in the Activity panel. Ephemeral by design.
2. **API request log** — `logs/api/*.jsonl` via `api_logging.py`: method, path,
   status, duration, and a request id. Used by diagnostics, not by features.
3. **Run history** — `run_history/` via `run_history.py`: durable records of
   generation/synthesis/export runs with their settings and outcomes.
4. **Receipts and reports** — `reports/`, `evaluation_reviews/`, export quality
   receipts, delivery receipts, casting and sound-design lineage. These are the
   artefacts the product's quality claims rest on; they are never "just logs".

Diagnostics endpoints under `/api/status/*` (system, GPU, disk, environment
fingerprint, process inventory) are the supported way to explain a stuck run.

### 2.8 Packaging and runtime

- Launchers at the root: `install.js`, `start.js`, `update.js`, `reset.js`,
  `pinokio.js`, `launcher_lifecycle.js`. They create `app/env`, install
  requirements, pick a port, and start Uvicorn. `install.js` is the only supported
  bootstrap.
- `app/env` is a project-local virtualenv (Python 3.10). The system interpreter is
  not expected to have the dependencies; running application code from outside
  `app/env` will fail on imports.
- Extra interpreters: the MLX backend and the aligner may use separate envs
  (`mlx_python`, aligner python), so "the app's Python" is not always the Python
  doing the work.
- The GPU is shared with `app/experiments/gpu_job.sh`, a queue that holds the same
  flock. Hand-run GPU work must go through `app/experiments/gpu_guard.py` or it
  will collide with the queue (§4.1).

---

## 3. Feature areas

Each area below names the modules that own it, what the area guarantees, and what
it explicitly refuses to claim. The last part matters: this codebase treats
overclaiming as a bug class, and several modules exist only to keep a weaker signal
from being read as a stronger one.

### 3.1 Setup, configuration and the LLM provider

**Modules:** `config_settings.py` (Pydantic models), `config_io.py`,
`lmstudio_settings.py` (1,648 lines), `llm_provider.py`, `llm_failover*.py`,
`llm_transport*.py`, `manual_transport.py`, `prompt_loader.py`, `prompts/`,
`routers/system.py` (20 routes).

Configuration is a single Pydantic-validated document written atomically to
`CONFIG_PATH` (normally `app/config.json`; a generated file, not a tracked one)
with a `.bak` rotation and a lock. The models are the contract:

- `LLMConfig` — `base_url`, `api_key`, `model_name`, optional `context_length`,
  `max_input_tokens`, `max_output_tokens`, plus `provider_headers` and
  `provider_extra_body` for hosted APIs.
- `TTSConfig` — `mode` (`local` | `external` | `mlx`), `url`, `external_urls`,
  `external_timeout_seconds`, `device`, `language`, `parallel_workers`,
  `max_new_tokens`, `compile_codec`, `mlx_python`, `mlx_models`.
- `GenerationConfig` — `chunk_size` (default 3000), token budgets, sampling
  (`temperature` 0.6, `top_p` 0.8, `top_k`, `min_p`, `presence_penalty`,
  `banned_tokens`), `merge_narrators`, and the `context_rescue_windows`
  (`[2000, 4000, 6000]`) / `context_rescue_retries` policy.
- `ThreePassModelProfile` — per-pass overrides (`chunk_size`,
  `segment_temperature`, `attribute_temperature`, `instruct_temperature`,
  `segment_output_ratio`, `segmentation`, `pass1_format`).
- `PromptConfig` — system/user prompts for generation and review, and
  `attribution_preset` (default `michel2_full`).

Three things are easy to get wrong here:

1. **`config["llm"]` is a mirror of the active profile**, not the source of truth.
   Always read through `get_active_llm_config`.
2. **Profiles are more than an endpoint.** Local, remote (an LM Studio reached
   over SSH or a forwarded port) and hosted differ in GPU admission
   (§2.5), in cost warnings, and in which failure modes are possible.
3. **Remote cost is warned, never enforced.** The UI confirmation is a prompt.

`routers/system.py` also exposes the LLM concurrency probe, environment/GPU/disk
diagnostics, and the update/health surfaces the launcher uses.

### 3.2 Source ingestion and script selection

**Modules:** `routers/script.py` (67 routes), uploads stage, `docx_*`,
`epub_package.py`, `epub_publication.py`, `epub_extraction_report.py`,
`source_*.py`, `calibre_*` (§3.13).

An upload lands in an owned staging area with a content digest before it can be
used. `POST /api/upload` plus `GET/POST /api/uploads[/select]` bind the *selection*
to a specific staged file, so "the source" is a digest, not "whatever is in the
folder". DOCX and EPUB are extracted with a report of what was dropped
(`epub_extraction_report.py`: bounded import snapshots, no source rewriting).
EPUB metadata/cover are *suggestions* only — `epub_publication.py` never applies
metadata or writes project assets.

`/api/script/ingest-status` and `/api/single-narrator/source-preview` serve the
ingest UI; `/api/uploads/docx-retained` and `/api/uploads/epub-retained` tell the
user which original file a project still depends on.

### 3.3 The annotation pipeline (single-pass and three-pass)

**Modules:** `generate_script.py` (2,668 lines), `three_pass_generate.py`
(4,773 lines), `compact_pass1.py`, `pass_quality.py`, `chunk_quality.py`,
`attribution_presets.py`, `segment_prompt.py`, `generation_checkpoint*.py`,
`context_rescue*.py`, `prompt_response_parse.py`.

- **Single-pass** generation produces the whole annotated script in one structured
  pass with chunk-level retries.
- **Three-pass** splits the work: pass 1 segments narration vs dialogue, pass 2
  attributes each spoken line to a cast member, pass 3 writes the delivery
  instruction. Each pass has its own prompt, temperature, output ratio and
  validation; `compact_pass1.py` shrinks the intermediate form before pass 2.
- **Quality gates.** `pass_quality.py` and `chunk_quality.py` are pure functions
  that check a response against its source chunk (coverage, ordering, unsupported
  scripts, malformed spans). A failing chunk is retried, escalated
  (`context_rescue_windows`), or reported — never silently accepted.
- **Structured output is not guaranteed.** A provider may ignore or reject the
  schema; the pipeline validates the returned shape and indexes independently
  rather than trusting the model's ids.
- **Checkpoints are strict on purpose.** A checkpoint binds prompt variant, model,
  cast hash and settings; changing any of them means the run cannot resume, by
  design (§4.3).
- **Recovery.** `GET /api/generate_script/recovery[/detail]` describes what a
  crashed run left behind; `inject`/`skip`/`retry` operate on the failed unit
  without restarting the book.

### 3.4 Script review, aliases, roster and printed labels

**Modules:** `review_script.py` (4,468 lines), `speaker_identity.py`,
`character_roster*.py`, `routers/speaker_review.py` (6 routes),
`routers/character_roster.py` (7 routes), `pronunciation.py`,
`routers/pronunciation_panel.py` (11 routes), `character_aliases.json`.

Review is a **second LLM pass with its own prompts**, not a re-run of generation:
it proposes corrections with reasons, and the user accepts them. Three identity
mechanisms sit beside it:

- **Aliases.** `character_aliases.json` records that "the old man", "Mr. Hale" and
  "Hale" are one character. The alias graph supports merge and split; a merge that
  would produce a cycle is refused.
- **Roster.** The character roster has its own preview/apply/undo with a
  single-use exact undo (`POST /api/character_roster/undo`); it refuses when
  protected files changed underneath it.
- **Printed labels.** `speaker_identity.py` + `routers/speaker_review.py` handle
  the case where the book itself prints the speaker ("HALE: ...") — caption
  evidence is attributed with that provenance recorded, so a printed label is
  never confused with an inferred one.

**Pronunciation rules** are book-scoped replacements applied at speech-preparation
time (`pronunciation.py`), with a panel that previews the effect on real lines.

### 3.5 Casts, voices, personas and casting

**Modules:** `voice_config_store.py` (the one locked write path for the active
cast), `voice_candidates.py`, `personas.py`, `persona_prompts.py`,
`persona_validation.py`, `voice_library.py`, `routers/voice_library.py` (11),
`routers/voices.py` (29), `voice_design.py`, `routers/voice_design.py` (14),
`voice_reference.py`, `voice_reference_import.py`, `voice_manifest.py`,
`casting_catalog.py`, `casting_tags.py`, `routers/casting.py` (5),
`builtin_lora/`, `lora_models/`, `designed_voices/`, `clone_voices/`.

- **`voice_config.json`** maps character → voice, plus the style timeline, seeds
  and per-character settings. All writes go through `voice_config_store.py`; a
  second writer is a lost-update bug.
- **Personas** are generated (by an LLM) or hand-authored descriptions that are
  validated (`persona_validation.py`) before they can become a voice.
- **A voice is a candidate before it is a selection.** `voice_candidates.py` is
  "the one place a voice becomes a candidate"; `is_same_voice` defines voice
  equality for dedup.
- **Four voice sources:** designed (Qwen voice-design), cloned reference
  (`voice_reference_import.py` normalises, measures, refuses and records),
  LoRA adapter, and built-in. Every source carries a revision; a render binds the
  revision it used.
- **Casting** (§3.12) is the cross-book view: designed voices plus review marks
  and descriptive tags (`casting_tags.py` derives gender/age/tone/texture from the
  description — tags are search aids, never identity claims).

### 3.6 Editor, synthesis, takes and verification

**Modules:** `routers/editor.py` (81 routes), `project.py` (4,758 lines),
`tts.py` (4,197 lines), `audio_generation_owner.py`, `synthesis_*.py` (12 modules),
`take_publication.py`, `take_retention.py`, `take_restoration.py`,
`audio_take_history.py`, `take_metadata_pool.py`, `word_timing.py`,
`speech_verification*.py` (7 modules), `speech_numbers.py`, `speech_policy.py`,
`speech_text.py`, `speech_retry.py`, `narration_markup.py`,
`narration_markup_edit.py`, `voice_drift.py`, `chunk_outliers.py`,
`chunk_outlier_check.py`, `sentence_gap_report.py`.

The editor is where the product's promises are tested per line:

- **Render.** `POST /api/chunks/{index}/generate` renders one chunk through
  `audio_generation_owner.py`, which grants *exact-owner* admission for Editor
  synthesis (direct Python callers stay unowned). The result is published by
  `take_publication.py`, which binds the proven native render to the encoded
  artefact.
- **Takes.** `audio_take_history.py` keeps bounded, immutable snapshots;
  `take_retention.py` is opt-in predecessor receipts; **the chunk row's
  `audio_path` alone selects audio**. `take_restoration.py` restores a saved
  performance only when its execution evidence matches.
- **Synthesis windows and recovery.** `synthesis_windows.py` renders opt-in exact
  spans of already-prepared speech (no guessed backend limits);
  `synthesis_recovery*.py` reuses a window only when identity of the actual loaded
  serving code, dependencies and inputs matches. `synthesis_workspace.py` is
  registered disposable spill, separate from reusable audio evidence.
- **Word timing.** `word_timing.py` runs ASR for one chunk. Its docstring states
  the contract: *a timing match is never a pronunciation verdict*.
- **Speech verification.** `speech_verification*.py` compares a finished take
  against the intended text, with ASR discrepancies reported as ordered transcript
  evidence, acoustic evidence for short utterances, and a finite revision-bound
  receipt. It is off by default (`speech_verification_settings.py`) and never
  claims lexical truth.
- **Speech preparation.** `speech_numbers.py` verbalises money with cents, number
  ranges and slash dates; `speech_policy.py` decides what the narrator voices when
  the source has unspeakable marks; `speech_text.py` classifies non-prose risk;
  `speech_retry.py` is a default-off, call-local, finite retry budget.
- **Narration markup.** `narration_markup.py` is an explicit terminal-pause syntax;
  ordinary text stays literal, and previews are book-bound before an atomic apply.
- **Advisory checks.** `voice_drift.py` (speaker drift vs a reference),
  `chunk_outliers.py` ("CPU proxies, not a real-speech quality qualification"),
  `sentence_gap_report.py` (report-only analysis of gaps *inside* one take).

### 3.7 Export, mastering and publication

**Modules:** `audio_assembly.py`, `audio_export.py`, `audio_export_plan.py`,
`audio_pcm_producer.py`, `audio_pcm_decode.py`, `audio_pcm_mux.py`,
`audio_streaming.py`, `audio_mastering.py`, `audio_mastering_filter.py`,
`audio_encoder_identity.py`, `audio_encoding_layout.py`, `audio_channels.py`,
`audio_ffmpeg.py`, `audio_validation.py`, `m4b_encode.py`, `m4b_mux_profile.py`,
`m4b_mux_worker.py`, `m4b_worker_request.py`, `m4b_metadata.py`, `m4b_cover.py`,
`m4b_publication.py`, `audio_m4b_pcm.py`, `export_staging*.py` (6 modules),
`export_publication.py`, `export_quality.py`, `export_quality_session.py`,
`export_download.py`, `export_file_io.py`, `export_limits.py`,
`readalong_export.py`, `readalong_sidecar.py`, `epub_readaloud.py`,
`ruby_text.py`, `audiobookshelf_delivery.py`.

The export path is the most heavily engineered part of the application, because it
is where a crash costs the most:

1. **Plan.** `audio_export_plan.py` computes exact source-frame geometry for the
   incremental PCM assembler; `export_limits.py` fixes finite size limits shared
   by request validation and assembly.
2. **Stage.** `export_staging.py` (1,037 lines) creates crash-recoverable,
   ownership-marked staging; `export_staging_directory.py`,
   `export_staging_nested*.py` and `export_staging_records.py` handle nested
   allocations and bounded receipts; `export_staging_retirement.py` (1,050 lines)
   runs POSIX retirement transactions for creation, cleanup and recovery.
3. **Assemble.** `audio_pcm_producer.py` builds disk-backed PCM preserving
   Pydub's incremental promotion order; `audio_streaming.py` writes bounded PCM
   WAVs; `audio_assembly.py` is buffered, no-crossfade assembly.
4. **Encode.** All FFmpeg work is owned and cancellable
   (`audio_ffmpeg.py`, `audio_encoder_identity.py` pins the binary and its version;
   `audio_encoding_layout.py` chooses the output clock without reinterpreting
   source PCM). M4B uses a versioned mux profile and an isolated worker
   (`m4b_mux_worker.py`, strict bounded request shape).
5. **Verify.** `export_quality.py` checks finished media against an independent
   PCM plan; `export_quality_session.py` holds bounded session evidence. A quality
   receipt describes *those* bytes.
6. **Publish.** `export_publication.py` takes source snapshots and short
   owner/book/chunk publication admission; `export_file_io.py` /
   `export_download.py` allow Windows publication without reopening pinned paths.
7. **Master.** `audio_mastering.py` / `audio_mastering_filter.py` apply optional,
   *measured* processing (loudnorm), with pure settings shared to isolated workers.

Outputs: MP3, chaptered M4B, per-chapter files (`chapter_exports/`), Audacity
project ZIP, read-along sidecar (§3.16), EPUB read-aloud (§3.16), and sound-design
mixes (§3.8). Delivery to Audiobookshelf is §3.14.

### 3.8 Sound design

**Modules:** `routers/sound_design.py` (22 routes) plus 30 `sound_design_*.py`
modules; frontend `app-sound-design*.js` (8 modules).

Sound design is a pipeline with an approval gate in the middle, and each stage
refuses to stand in for the next:

| Stage | Module | Guarantee |
|---|---|---|
| Cue authoring | `sound_design_cues.py` | Strict immutable cue recipes; read-only, sample-exact placement |
| Asset library | `sound_design_assets.py`, `sound_design_uploads.py`, `sound_design_decode.py` | Durable immutable asset records independent of the active book; quota-bounded private upload staging |
| Audition | `sound_design_audition.py`, `sound_design_candidate_audition.py` | Short exact-frame excerpts; explicitly *not* media approval |
| Capture | `sound_design_capture.py`, `sound_design_take_decode.py`, `sound_design_pcm.py` | Exact dry PCM from the export's owned, timeline-trimmed sources |
| Mix | `sound_design_mix.py`, `sound_design_render.py`, `sound_design_staging.py` | Bounded exact-frame mixing inside an existing owned export worker; serialized rendering with crash accounting |
| Candidate | `sound_design_candidate.py`, `sound_design_candidates.py` | Saved candidate metadata with a pinned PCM descriptor; revision-bound discard |
| Approval | `sound_design_approval.py`, `sound_design_current.py` | Explicit consent to **one** full mixed candidate plus normalized delivery options |
| Delivery | `sound_design_delivery.py`, `sound_design_publication.py`, `sound_design_download.py`, `sound_design_cover.py` | Quota-reserved, digest-verified streaming of an approved mix |
| Lineage | `sound_design_renditions*.py`, `sound_design_registry.py`, `sound_design_rendition_cleanup.py` | Bounded immutable score lineage; "metadata is neither media admission nor approval" |

The invariant to remember: **a rendition record is history, a candidate is audio,
and approval is consent** — three different things stored three different ways.

### 3.9 Voice Lab: preparation, datasets, LoRA, evaluation

**Modules:** `routers/voicelab.py` (10), `routers/preparer.py` (7),
`routers/dataset_builder.py` (11), `routers/lora.py` (21), `voicelab_settings.py`,
`voice_manifest.py`, `voice_analysis_cache.py`, `voice_clustering.py`,
`voice_dataset_merge.py`, `voice_reference.py`, `adapter_artifacts.py`,
`adapter_checkpoint_transaction.py`, `adapter_publication.py`,
`adapter_naming_transaction.py`, `adapter_static.py`, `lora_evidence.py`,
`dataset_metadata.py`, `dataset_publication.py`, `preparer_*_settings.py`;
worker `alexandria_preparer_rocm_compatible.py` at the repository root;
`tools/voice_lab/`.

The chain is: **existing audiobook → aligned dataset → LoRA adapter → profile →
named voice**, with the preparer as the front half:

- The **preparer** runs ASR + alignment + annotation over an audiobook directory,
  writing `preparer_output/` and resumable scratch state. The router validates
  numeric and enrichment settings with the *same* rules the CLI uses
  (`preparer_numeric_settings.py`, `preparer_enrichment_settings.py`) so the two
  entry points cannot drift.
- **Dataset Builder** edits and generates training rows in `dataset_builder/`,
  with batch generation and cancellation. `dataset_metadata.py` validates shapes
  shared by uploads and standalone training metadata; `dataset_publication.py`
  publishes a dataset only after private preparation succeeds.
- **Voice Lab** deduplicates and analyses voices (`voice_analysis_cache.py` —
  content identities shared by dedup and analysis caches), clusters speaker
  identities deterministically (`voice_clustering.py`, complete-link with
  overrides), merges datasets provenance-preservingly (`voice_dataset_merge.py`),
  and picks the reference clip a dataset will be anchored to (`voice_reference.py`).
- **Adapters** are published as recoverable whole-directory generations
  (`adapter_checkpoint_transaction.py`), renamed replayably
  (`adapter_naming_transaction.py`), and served through
  `adapter_publication.py` / `adapter_static.py`. `adapter_artifacts.py` performs
  read-only structural checks *without* loading the base model.
- **Evaluation evidence** is versioned: `lora_evidence.py` defines the integrity
  contract (`validate_serving_identity`, model settings) that blind review and
  promotion rely on.

### 3.10 Workspace, library, reports and run history

**Modules:** `routers/scripts_library.py` (14), `book_workspace.py`,
`collection_overview.py`, `run_history.py`, `workspace_guidance.py`,
`diagnostics.py`, `saved_book*`, `scripts/` directory.

- **Saved books** live under `scripts/<slug>/` with their script, voice config,
  aliases, sound-design companions and export metadata. `book_state_transaction.py`
  is what makes switching between them safe: a switch is a publication of a whole
  file set, journaled, and `book_state_recovery.py` (#1660) gives an operator
  recovery path for an interrupted switch.
- **`book_workspace.py`** produces read-only, book-bound facts for the workflow
  workspace — it never starts work.
- **`collection_overview.py`** (1,031 lines) is a read-only overview that projects
  durable per-book state. Its docstring is the rule: *"never start work"*. Opening
  the library must not wake a GPU.
- **`run_history.py`** records durable history for long tasks; `run_chains/`
  groups related runs.
- **`workspace_guidance.py`** is the message contract for guidance: English text
  is preserved while the client localises via `app-i18n.js`, so a message can be
  translated without the server inventing translations.
- **`diagnostics.py`** builds sanitized, bounded support bundles and redacts
  sensitive keys (`_key_is_sensitive`). It is the supported way to ask a user for
  logs.

### 3.11 Project scope (reversible suffix removal)

**Modules:** 12 `project_scope_*.py` modules, `routers/project_scope.py` (6
routes).

Sometimes a book must lose its back matter, an appendix, or a whole trailing
section. Doing that by hand corrupts three files at once. Project scope makes it a
transaction:

- `project_scope_plan.py` builds **one read-only, source-backed preview** of an
  explicit suffix; `project_scope_source.py` provides ordered source-word
  authority; `project_scope_partition.py` checks script/chunk boundaries;
  `project_scope_literal.py` keeps an immutable retained-marker lineage;
  `project_scope_voices.py` rebases the voice timeline for retained rows.
- `project_scope_apply.py` performs a compound publication with **one bounded
  persistent exact undo**. `project_scope_files.py` is the confined read-only file
  authority shared by preview/apply/undo; `project_scope_assets.py` snapshots files
  referenced by retained voices; `project_scope_audio.py` reads retained
  selected-audio facts.
- `project_scope_exports.py` (1,680 lines) keeps large older deliveries on disk
  while fencing unchanged pre-scope files, so undo does not require re-exporting.
- `project_scope_history.py` is read-only: a scope change **never rewrites
  originating history records**.

Undo is single-use and exact. If protected files changed after the apply, undo
refuses rather than guessing.

### 3.12 Casting library (#1532)

**Modules:** `casting_catalog.py`, `casting_tags.py`, `routers/casting.py` (5
routes), frontend `app-casting.js`, `app-casting-labels.js`,
`app-casting-research.js` (1,731 lines); docs `casting-library-phase2.md`,
`casting-library-trash.md`, `casting-research-design.md`.

The casting library is the **cross-book** view of every designed voice, so a voice
created for one book can be found, compared and reused in another. It adds:

- A catalogue with review **marks** (`POST /api/casting/marks`,
  `/marks/batch`) and lifecycle changes in batch (`/lifecycle/batch`).
- Descriptive **tags** derived from the voice description — gender, age, tone,
  texture. `casting_tags.py` is pure matching over words; tags are search aids and
  the code says so.
- **Trash** semantics (a deleted voice is recoverable, per the trash design doc).
- `GET /api/casting/clone_config` exposes what a clone-based voice would need.

### 3.13 Calibre OPDS import (#1734)

**Modules:** `calibre_opds.py` (911 lines, slice 1: navigation + capture),
`calibre_catalog.py` (857 lines, slice 2: one optional user-configured catalog),
`calibre_import.py` (596 lines, slice 3: import into the existing upload
authority); routes under `/api/calibre/*` in `routers/script.py`; docs
`docs/calibre-opds.md`; frontend `app-calibre.js`.

The design is deliberately narrow: **one** optional catalog, bounded navigation,
and an import that goes *through* the existing upload authority rather than
bypassing it.

- `GET/POST /api/calibre/config`, `/browse-root`, `/browse-next` — bounded OPDS
  navigation with cross-origin review (`CrossOriginReviewRequired`): a catalog that
  redirects off-origin needs explicit user consent.
- `/preview` → `/review/approve` → `/capture` (`/capture/status`,
  `/capture/cancel`, `/capture/discard`) → `/import`. Capture is a staged,
  cancellable download; import creates a normal upload-owned source.

### 3.14 Audiobookshelf delivery

**Modules:** `audiobookshelf_delivery.py` (1,385 lines), routes in
`routers/editor.py`, frontend `app-audiobookshelf.js`.

Delivery is **explicit and manual**: the user configures a server, picks a
completed shareable export, and uploads it to a specific library item. The module
records a receipt with the remote identifiers so a retry does not create a
duplicate. It never publishes automatically and never treats a successful upload as
an approval of the audio.

### 3.15 Translated edition preview

**Modules:** `translated_edition.py` (695 lines), `routers/translated_edition.py`
(5 routes), frontend `app-translated-edition.js`.

An opt-in, **local** translation preview of a saved script: the LLM proposes a
translated edition, and the result is a **reviewable proposal** — not a new book,
not an overwrite. `get_installed_pairs` / `is_pair_*` gate which language pairs are
offered, so the UI cannot request a pair the local model was not set up for.

### 3.16 Read-along sidecar and EPUB read-aloud (#1720, #1779, #1844)

**Modules:** `readalong_sidecar.py` (439 lines), `readalong_export.py` (1,370
lines), `epub_readaloud.py` (2,563 lines), `ruby_text.py`, `qwen_align.py` (836),
`qwen_align_worker.py` (582), `qwen_runtime.py` (1,071), `qwen_score_decoder.py`.

Two related outputs:

- **Read-along sidecar (#1720)** — a bounded, versioned JSON sidecar that binds
  word timings to **frames** of the rendered audio (with the audio's SHA-256 and
  trim head frame), so a player can highlight text without re-deriving offsets.
- **EPUB 3 read-aloud (#1779)** — a source-preserving export that adds SMIL
  overlays to the *original* EPUB rather than rebuilding the book. `epub_readaloud.py`
  validates URIs (`_check_raw_uri`, `_decode_uri_path`) and never rewrites the
  source text; `ruby_text.py` is the single rule for how ruby annotations are
  narrated, shared by script extraction and read-aloud.

Alignment comes from the **Qwen3-ForcedAligner** integration (#1844), which is
deliberately fail-closed and isolated: `qwen_runtime.py` discovers the runtime and
runs an owned worker, `qwen_align_worker.py` runs **only** in the dedicated
`qwen-env` interpreter, `qwen_align.py` holds the bounded pure helpers, and
`qwen_score_decoder.py` decodes the maximum-score monotonic timestamps. If the
aligner is unavailable, the export refuses rather than emitting approximate
timings.

### 3.17 Cross-feature map

| If you change… | Also affected |
|---|---|
| Chunk fields | generation inputs hash, staleness, export PCM plan, take receipts, project scope partition |
| Script bytes | script receipt, cast hash, checkpoints, read-along sidecar, EPUB export |
| Voice config | cast hash, checkpoint validity, synthesis recovery identity, casting catalogue |
| Speech preparation (`speech_*.py`) | word timing, verification receipts, read-along text, pronunciation rules |
| Export staging | quality receipts, delivery receipts, project-scope export fencing |
| `process_state` task names | admission, conflict matrix, UI Activity panel, run history |
| Router registration | origin gate coverage, `/api/status/*` diagnostics, JS `api()` calls |

---

## 4. Nuances, subtleties and gotchas

These are the rules that fail *silently* when ignored. Most of them exist because
the alternative was a corrupted book or a lost take.

### 4.1 Concurrency and admission

1. **`process_state` is not cross-process truth.** It is a per-process dict. A
   second server, a benchmark or a CLI is coordinated only by task leases and the
   GPU flock.
2. **Check-then-start is a TOCTOU race.** Use `claim_gpu_task` /
   `reserve_background_task`; never call `check_global_gpu_lock` and then set
   `running` yourself.
3. **Only owned CPU claims exempt a task from GPU conflicts.** Being named in
   `NON_GPU_TASKS` exempts a task; assuming "it's CPU-only" does not.
4. **Remote LLM relaxation is config-sensitive.** A local profile — or a config
   that cannot be read — gets the full lock. That is intentional.
5. **Startup recovery must preserve foreign work.** `ensure_startup_recovery` only
   cleans when no task lock is held. Never delete a `.lock` file to "unstick"
   startup.
6. **Book switches require a quiet system.** `ensure_book_switch_allowed` refuses
   while tasks run or another process owns a task.
7. **Cancellation is cooperative and not instant.** An in-flight model or ffmpeg
   call finishes first.
8. **The GPU lock is shared with the experiment queue.** `experiments/gpu_guard.py`
   refuses hand-run GPU work while `gpu_job.sh` holds the lock; an inherited queue
   lease is the only exception.
9. **Synthesis has an exact-owner path.** Editor renders are admitted through
   `audio_generation_owner.py`; a direct Python caller is unowned and must not
   publish.

### 4.2 Data integrity and transactions

1. **Every publication that replaces multiple related files needs a journal.**
   `book_state_transaction.py` is the model: stage, record old/new hashes,
   validate, `os.replace`, fsync, recover on startup.
2. **Never write a JSON state file with a plain `open(..., "w")`.** Use
   `atomic_json_write`; a crash mid-write leaves a truncated file that the next
   startup may read as "empty".
3. **Read-modify-write JSON needs the lock.** `file_lock` guards config, voice
   library, saved books, chunks, take history. A read followed by a write without
   the lock loses concurrent updates.
4. **Symlinks and path escapes are refused, not followed.** Use
   `is_path_inside`/realpath and keep the explicit symlink refusals.
5. **Book tokens are not decorative.** They bind an operation to
   source/script/voice/companion bytes; a stale token must produce a conflict, not
   a best-effort write.
6. **Undo records are exact and effectively single-use.** Roster and scope undo
   refuse if protected files changed. Do not "repair" an undo by guessing.
7. **Saved books retain companions.** Script, voice config, aliases, sound-design
   cues/renditions and export metadata travel together; copying only the script
   produces a book that looks right and renders wrong.
8. **Per-UID status is a journal, not a rewrite.** `chunk_status_journal.py`
   appends durable render updates while callers hold the chunks-file lock; readers
   must fold it in order.

### 4.3 Identity and fingerprints

1. **`id` vs `uid`:** `id` is the current list position; `uid` is permanent. Audio
   filenames use `uid`.
2. **Generation inputs, not the whole chunk, define staleness.** Changing
   `audio_path` or UI-only fields does not invalidate audio; changing
   text/speaker/instruct/take/voice revision/pronunciation does.
3. **Checkpoint fingerprints are strict on purpose.** A prompt variant, cast hash,
   model or context change means the run cannot resume — start over or restore the
   settings.
4. **Cast changes can invalidate a checkpoint** even when script bytes are
   unchanged, because the roster is part of request identity.
5. **Audio receipts bind bytes and versions.** A receipt from an older file does
   not certify a replaced file at the same path.
6. **Export quality receipts describe a specific output.** They stay true about
   those bytes and stop describing the *current* book state after inputs change;
   the workspace is expected to say exactly that.
7. **Take snapshots are addressed by version, and selection is `audio_path`.**
   Metadata alone never selects audio.

### 4.4 LLM behaviour

1. **`config["llm"]` is a mirror.** Use `get_active_llm_config`.
2. **Prompt variant and adapter are coupled.** An attribution adapter trained on
   one prompt shape should be served with that shape; `michel2_full` is a default,
   not a universal.
3. **Structured output is not guaranteed.** Validate the returned shape and index
   independently of the model's ids.
4. **Retries have a policy, not a vibe.** Attempted prompts, truncation, reasoning
   escalation, near misses and API exhaustion are tracked. Do not add an unbounded
   retry loop in a new call site.
5. **API exhaustion can pause.** `on_api_exhaustion="pause"` freezes the run and
   keeps state; it is not cancellation.
6. **Reasoning tokens count.** Budgets and truncation checks include them even when
   the provider omits `reasoning_tokens`.
7. **Failover is sticky for the run.** A request already admitted to the primary
   may finish there; new requests go to the secondary.
8. **Manual transport has no timeout.** It waits for a human; cancel is the only
   exit.
9. **Remote cost is warned, not enforced.** The confirmation is a prompt.

### 4.5 Audio and TTS

1. **One render, one owner.** Synthesis publishes through
   `take_publication.py`; a render that bypasses the owner can be overwritten
   mid-write.
2. **Inter-chunk export timing is not intra-chunk assembly.** `synthesis_audio.py`
   assembles internal windows; gaps between chunks belong to export.
3. **Recovery reuse is identity-gated.** Reusing a window requires matching loaded
   serving code, dependency bindings and inputs; a near-match is a refusal.
4. **Encoded takes are validated before admission** (`audio_validation.py`), with
   optional generated-speech admission checks.
5. **Mastering is optional and measured.** Loudnorm results are recorded; do not
   present an unmeasured gain as mastering.
6. **M4B chapters follow the source clock** (`m4b_metadata.py`), not the encoder's
   rounding.
7. **The aligner runs in its own interpreter.** Never import the Qwen aligner into
   the app's Python; `qwen_runtime.py` owns the boundary and fails closed.

### 4.6 Export and publication

1. **Staging is ownership-marked and crash-recoverable.** Do not write export
   artefacts directly to their final path.
2. **Retirement is a transaction on POSIX.** `export_staging_retirement.py` exists
   because deleting a staging tree while a reader holds it is not safe; use it.
3. **Pinned readers on Windows.** `export_file_io.py` / `export_download.py` allow
   publication without reopening a path another handle holds.
4. **Quality verification uses an independent PCM plan.** Verifying an encode with
   the same plan that produced it proves nothing.
5. **Limits are finite and shared.** `export_limits.py` is the single source for
   request validation and assembly limits.

### 4.7 Frontend

1. **No framework, no bundler, no build step.** 45 plain modules in
   `app/static/js/` loaded by `index.html`; a new file must be added to the page's
   script list.
2. **`app/static/js/app-core.js` is the shared runtime** (12,060 lines, ~452
   functions):
   `api()`, polling, toasts, state mirrors. Duplicating its helpers in a feature
   module is how behaviour drifts.
3. **The server is the truth.** Optimistic UI must reconcile with the next status
   poll; nothing important may live only in browser memory.
4. **`confirmIfRemote()` is a courtesy, not a guard.** The server still enforces
   admission.
5. **Localisation lives in `app-i18n.js`;** server guidance messages carry English
   text plus keys (`workspace_guidance.py`) so the client can translate without the
   server guessing.
6. **Feature modules are large by design** (`app-workbench.js` 2,384 lines,
   `app-casting-research.js` 1,731, `app-calibre.js` 1,459). Splitting one is a
   refactor with real risk; do it deliberately.

### 4.8 Tests and CI

1. **`unittest`, not pytest.** 1,663 `test_*.py` modules under `app/tests/`; CI
   shards them (`tests / unit (1/3)`…`(3/3)`).
2. **`./ready.sh` before committing.** It is the repository's own gate.
3. **Tests that need the GPU or a model are marked/skipped**, not silently slow.
   A "passing" run on a machine without the model proves less than it looks.
4. **E2E lives outside the unit suite** (`tools/e2e`), and `app/experiments/` (350
   modules) is research code with its own conventions — do not import it into the
   app.
5. **`app/env` is required** to run tests; the system interpreter lacks the
   dependencies.

---

## 5. Technical reference

### 5.1 Glossary

| Term | Meaning |
|---|---|
| **Book** | One project: source + script + voice config + chunks + audio + exports. Identified by a slug under `scripts/` |
| **Script** | `annotated_script.json`: the ordered list of entries with speaker, text, instruction |
| **Chunk** | A synthesis unit: one or more script entries grouped for TTS. Has `uid`, `id`, `audio_path`, `audio_revision` |
| **Take** | One rendered performance of a chunk; the selected take is the one `audio_path` points at |
| **Voice** | A voice definition (designed, cloned, LoRA, built-in) with a revision |
| **Cast** | The character → voice mapping in `voice_config.json` |
| **Persona** | A description that can become a designed voice |
| **Receipt** | A record binding a result to the exact inputs and bytes that produced it |
| **Publication** | Replacing the live file(s) atomically through a journal or staging transaction |
| **Admission** | The permission to start work (GPU mutex + task lease + book guards) |
| **Preparer** | The ASR/alignment pipeline that turns an audiobook into training data |
| **Adapter** | A LoRA checkpoint directory plus its manifest |

### 5.2 Module map

`app/` contains **352** Python modules at its root (120,389 lines), plus
`app/routers/` (20 files), `app/tests/` (1,663 test modules), `app/experiments/`
(350 modules) and `app/static/` (77 files; 45 application JS modules under
`app/static/js/`). The largest and most
consequential modules:

| Module | Lines | Role |
|---|---|---|
| `book_state_transaction.py` | 6,700 | Journaled publication of the active book's whole file set |
| `three_pass_generate.py` | 4,773 | Segment → attribute → instruct pipeline |
| `project.py` | 4,758 | Chunks, generation inputs, assembly, chapter plan |
| `review_script.py` | 4,468 | Second-pass LLM review of the script |
| `tts.py` | 4,197 | TTS engines (Qwen local, MLX, external pool) |
| `core.py` | 2,997 | Paths, config, locks, `process_state`, admission |
| `generate_script.py` | 2,668 | Single-pass annotation and chunk retries |
| `epub_readaloud.py` | 2,563 | EPUB 3 read-aloud (SMIL) export |
| `project_scope_exports.py` | 1,680 | Export fencing for reversible scope changes |
| `lmstudio_settings.py` | 1,648 | LLM profiles, model discovery, provider settings |
| `benchmark_runner.py` | 1,466 | Production-backed benchmark stage adapters |
| `audiobookshelf_delivery.py` | 1,385 | Manual delivery + receipts |
| `readalong_export.py` | 1,370 | Read-along and EPUB export workers |
| `qwen_runtime.py` | 1,071 | Aligner runtime discovery, owned workers |
| `export_staging_retirement.py` | 1,050 | POSIX retirement transactions |
| `export_staging.py` | 1,037 | Crash-recoverable staging |
| `sentence_gap_report.py` | 1,032 | Report-only intra-take gap analysis |
| `collection_overview.py` | 1,031 | Read-only library overview |
| `calibre_opds.py` | 911 | OPDS navigation and capture |
| `synthesis_workspace.py` | 887 | Disposable synthesis spill |
| `calibre_catalog.py` | 857 | One optional user catalog |
| `qwen_align.py` | 836 | Fail-closed alignment helpers |
| `translated_edition.py` | 695 | Opt-in translation preview |
| `subprocess_ownership.py` | 673 | Child-process ownership per OS |
| `audio_take_history.py` | 562 | Immutable take snapshots |
| `export_quality.py` | 521 | Independent verification of finished media |
| `casting_catalog.py` | 468 | Cross-book designed-voice catalogue |
| `audio_export.py` | 465 | Cancellable CPU encoding, atomic publication |
| `readalong_sidecar.py` | 439 | Frame-bound word-timing sidecar |

Frontend: `app/static/js/app-core.js` (12,060 lines) is the shared runtime; the largest feature
modules are `app-workbench.js` (2,384), `app-casting-research.js` (1,731),
`app-calibre.js` (1,459), `app-single-voice.js` (652), `app-i18n.js` (469).

### 5.3 API surface

**351 routes** across 19 routers. Every route is declared in `app/routers/` and
registered in `app/app.py` (lines 304–374). Route counts and the path space each
router owns:

| Router | Routes | Example paths |
|---|---|---|
| `routers/editor.py` | 81 | `POST /api/chunks/{index}/word_timing` `GET /api/chunks/{index}/word_timing/audio` `GET /api/take_history_policy` |
| `routers/script.py` | 67 | `GET /api/uploads` `POST /api/uploads/select` `GET /api/script/ingest-status` |
| `routers/voices.py` | 29 | `GET /api/voices` `POST /api/voices/{speaker}/versions` `DELETE /api/voices/{speaker}/versions/{version_id}` |
| `routers/sound_design.py` | 22 | `POST /api/sound_design/assets/import` `GET /api/sound_design/assets` `POST /api/sound_design/assets/{asset_id}/purge_incomplete` |
| `routers/lora.py` | 21 | `POST /api/lora/upload_dataset` `GET /api/lora/datasets` `DELETE /api/lora/datasets/{dataset_id}` |
| `routers/system.py` | 20 | `GET /api/tts/diagnostics` `POST /api/tts/external-capabilities` `POST /api/prompts/attribution_preview` |
| `routers/voice_design.py` | 14 | `POST /api/voice_design/preview` `POST /api/voice_design/save` `GET /api/voice_design/list` |
| `routers/scripts_library.py` | 14 | `GET /api/scripts` `GET /api/workspace` `GET /api/book/recovery` |
| `routers/voice_library.py` | 11 | `GET /api/voice_library` `POST /api/voice_library/casts` `POST /api/voice_library/catalog_member` |
| `routers/pronunciation_panel.py` | 11 | `GET /api/pronunciation` `POST /api/pronunciation/save` `POST /api/pronunciation/preview` |
| `routers/dataset_builder.py` | 11 | `GET /api/dataset_builder/list` `POST /api/dataset_builder/create` `POST /api/dataset_builder/update_meta` |
| `routers/voicelab.py` | 10 | `GET /api/voicelab/health` `GET /api/voicelab/diagnostics` `POST /api/voicelab/preflight` |
| `routers/preparer.py` | 7 | `POST /api/preparer/start` `POST /api/preparer/cancel` `GET /api/preparer/list` |
| `routers/character_roster.py` | 7 | `GET /source` `POST /preview` `POST /source` |
| `routers/speaker_review.py` | 6 | `GET /options` `POST /preview` `POST /start` |
| `routers/project_scope.py` | 6 | `POST /api/project/scope/preview` `GET /api/project/scope/entries` `POST /api/project/scope/apply` |
| `routers/translated_edition.py` | 5 | `GET /api/translated_edition/pairs` `POST /api/translated_edition/proposals` `GET /api/translated_edition/latest-proposal` |
| `routers/casting.py` | 5 | `GET /api/casting/voices` `POST /api/casting/marks` `POST /api/casting/marks/batch` |
| `routers/benchmark.py` | 4 | `POST /api/benchmark/preflight` `POST /api/benchmark/start` `POST /api/benchmark/cancel` |

(The table lists up to three example paths per router, not all of them. The full
list is regenerable with the route extractor described in Appendix A.
`routers/character_roster.py` and `routers/speaker_review.py` declare an
`APIRouter` prefix, so their paths in the table are relative to
`/api/character_roster` and `/api/speaker_review` respectively.)

Status and control routes worth knowing: `/api/status` and
`/api/status/{task_name}` (declared in `routers/script.py`, not `system.py`),
`/api/chunks/status` (per-UID render state), `/api/status/eta`,
`/api/system/stats`, `/api/runs`, `/api/audio/estimate`,
`/api/chunks/playback_plan`, `/api/take_history_policy`,
`/api/generate_script/preflight`, and the `/api/benchmark/{preflight,start,cancel,status}`
quartet.

### 5.4 Configuration reference

One JSON document at `CONFIG_PATH` (`app/config.json` by default; absent from a
fresh checkout because it is generated). Precedence: **environment variable →
`config.json` → model defaults**. Shapes are Pydantic models in
`config_settings.py` (see §3.1 for fields and defaults). Writes go through
`config_io.py` with a lock and `.bak` rotation.

Three config surfaces are frequently confused:

- `llm` in `config.json` is a **mirror** of the active profile; the profiles
  themselves live in the LM Studio settings section.
- `prompts` holds custom prompt text; **absent means "use the shipped prompt"**,
  and the prompt variant/adapter pairing still applies (§4.4).
- `three_pass` holds per-pass overrides; unset values inherit `generation`.

### 5.5 On-disk layout

```
<repo>/                       DATA_DIR by default (ALEXANDRIA_DATA_DIR overrides)
├── app/                      code, static/js/, tests/, env/, experiments/, prompts/
├── scripts/<slug>/           saved books (script, voice config, companions)
├── uploads/                  staged sources (owned, digest-bound)
├── voicelines/               rendered chunk audio
├── take_history/             immutable take snapshots
├── chapter_exports/          per-chapter exports
├── reports/                  generated reports
├── logs/api/                 request log (JSONL)
├── run_history/, run_chains/ durable run records
├── evaluation_reviews/       blind LoRA review records
├── designed_voices/, clone_voices/, lora_models/, builtin_lora/
├── dataset_builder/, lora_datasets/, preparer_output/
├── docs/, tools/, codebase-analysis-docs/, ab_test_runtime/
├── RESULTS_INDEX.md, results_index.csv        ← generated
└── app/static/i18n/ui.en.json                ← generated
```

Everything above `app/` is runtime data except the tracked directories
(`docs/`, `tools/`, `builtin_lora/`, `codebase-analysis-docs/`,
`ab_test_runtime/`). **`builtin_lora/` is shipped content, not user data** — do not
prune it.

### 5.6 Environment variables

193 distinct names are referenced in the tree. The ones that change runtime
behaviour:

| Group | Variables |
|---|---|
| Server | `ALEXANDRIA_HOST`, `ALEXANDRIA_PORT`, `ALEXANDRIA_DATA_DIR`, `ALEXANDRIA_BUILD_COMMIT`, `ALEXANDRIA_LLM_TIMEOUT`, `ALEXANDRIA_RUN_ID` |
| Auth & origin | `ALEXANDRIA_AUTH_USERNAME`, `ALEXANDRIA_AUTH_PASSWORD` (enables HTTP Basic), `ALEXANDRIA_ALLOWED_HOSTS`, `ALEXANDRIA_ALLOWED_ORIGINS`, `CORS_ORIGINS` |
| Interpreters | `ALEXANDRIA_QWEN_PYTHON`, `ALEXANDRIA_ROCM_PYTHON`, `ALEXANDRIA_PREPARER_PYTHON`, `ALEXANDRIA_SIBLING_PYTHON`, `ALEXANDRIA_QWEN_MODEL`, `ALEXANDRIA_PROFILER_MODEL` |
| ASR | `ALEXANDRIA_WHISPER_CPP_BIN`, `ALEXANDRIA_WHISPER_CPP_MODEL`, `ALEXANDRIA_WHISPER_CPP_CONFIRM_MODEL` |
| GPU lock | `GPU_LOCK`, `ALEXANDRIA_GPU_LOCK_FD`, `ALEXANDRIA_GPU_LOCK_HELD`, `ALEXANDRIA_GPU_LOCK_PID`, `GPU_PROGRESS_*` |
| Storage | `ALEXANDRIA_EPUB_DIRS`, `ALEXANDRIA_ZIPS_DIR` |
| Audiobookshelf | `ALEXANDRIA_ABS_SERVER_URL`, `ALEXANDRIA_ABS_API_KEY` |
| Model downloads | `HF_TOKEN`, `HF_HUB_OFFLINE`, `HF_HUB_DISABLE_TELEMETRY`, `PYTORCH_CUDA_ALLOC_CONF`, `CMAKE_ARGS` |
| Tooling | `AA2_PYTHON`, `UV_CONSTRAINT`, `PIP_CONSTRAINT` |

The remaining ~150 are **test fixture hooks** (`FIXTURE_*`, `*_SOURCE`,
`*_BASELINE*`, `AA2_TEST_*`). They are read by tests to inject failure modes; a
production run never sets them, and a test that silently depends on one is a flake
waiting to happen.

### 5.7 Commands

| Task | Command |
|---|---|
| Install | `node install.js` (creates `app/env`, installs requirements) |
| Run | `node start.js`, or `app/env/bin/python -m uvicorn app.app:app --host 127.0.0.1 --port 7860` from the repo root |
| Full local gate | `./ready.sh` — regenerates derived files, installs git hooks, runs the verifier |
| Regenerate derived files only | `tools/regen_derived.sh` |
| Unit tests | `cd app && env/bin/python -m unittest discover -s tests -p 'test_*.py'` |
| One test | `cd app && env/bin/python -m unittest tests.test_<module>` |
| Benchmarks | `routers/benchmark.py` routes, or the workers in `app/benchmark_*` |

**`./ready.sh` is not optional.** It regenerates `RESULTS_INDEX.md`,
`results_index.csv`, the three `ab_test_runtime/audit/*.json` files,
`LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md`, `app/tests/unit_test_inventory.json` and
`app/static/i18n/ui.en.json`. A commit that changes module structure, test
inventory or UI strings without regenerating these fails CI on a generated file,
not on the code change.

### 5.8 Extension recipes

**Add an API route.** Declare it in the owning `app/routers/*.py` module with a
Pydantic request model; do not add it to `app/app.py`. If it mutates state, it is
covered by the origin gate automatically — do not add an exemption. If it starts
work, claim admission through `claim_gpu_task`/`reserve_background_task` and
register in `process_state`.

**Add a frontend module.** Create `app/static/js/app-<feature>.js`, add it to the
script list in `app/static/index.html`, reuse `api()`/polling from `app-core.js`,
and add any new UI string through the i18n flow so `ui.en.json` regenerates.

**Add a config field.** Add it to the relevant Pydantic model in
`config_settings.py` with an explicit default and bounds, then read it through the
model — never by indexing the raw dict.

**Add a persisted state file.** Give it an `atomic_json_write` path, a `file_lock`
for read-modify-write, and a place in the book journal if it belongs to the active
book's file set. If it can be rebuilt, say so and let startup rebuild it.

**Add an export artefact.** Plan it in `audio_export_plan.py` terms, stage it
through `export_staging.py`, verify it with an independent PCM plan, and record a
quality receipt. Never write directly to the final path.

**Add a long task.** Name it, add it to `GPU_TASKS`/`NON_GPU_TASKS`, give it a
cancel event, a `process_state` entry, progress reporting, a `run_history` record,
and a recovery story.

---

## 6. Coverage, assumptions and open questions

### 6.1 What this analysis is based on

- **Mechanical extraction** of all 352 `app/*.py` modules (docstring, public
  definitions, local dependencies), all 351 routes, all 45 JS modules, the config
  models, the env-var names, and the on-disk path constants.
- **Direct reading** of the cross-cutting modules: `app.py`, `core.py`,
  `book_state_transaction.py` (structure and journal protocol), `project.py`
  (generation inputs, chunk selection), `task_ownership.py`,
  `request_origin.py`, `config_settings.py`, `export_staging*.py` (protocol),
  and the docstrings of every subsystem boundary module.
- **Spot verification** of claims in `CLAUDE.md` and the previous version of this
  document against the tree.

### 6.2 What it does not cover

- **Test bodies.** 1,663 test modules were counted and their names used as
  evidence of behaviour, not read line by line.
- **`app/experiments/`** (350 modules). It is research code with its own
  conventions; it is summarised, not analysed.
- **`app/static/vendor/`** and third-party model code.
- **Line-level behaviour of the largest modules** (`three_pass_generate.py`,
  `review_script.py`, `tts.py`, `book_state_transaction.py`): their protocols and
  invariants are described, their internals are not.
- **Runtime behaviour.** No server was started, no book was generated, no export
  was produced for this document. Everything here is derived from source.

### 6.3 Open questions worth answering before a large change

1. **Where does the single-pass path end and the three-pass path begin?** Both
   exist and both are maintained; the UI decides, and the checkpoint formats
   differ. Confirm which one a change targets.
2. **How many places can publish audio?** `take_publication.py`,
   `audio_generation_owner.py`, `synthesis_render.py` and export all touch audio
   publication. Any new writer must use the owner path (§4.5).
3. **Is `process_state` ever going to become cross-process?** Today leases are the
   only cross-process truth; a change that assumes otherwise will corrupt state
   under a second server.
4. **How is the read-along sidecar versioned across exporter changes?** The
   sidecar is versioned and frame-bound; a change to trimming or assembly changes
   its meaning.
5. **Which generated files does a given change touch?** `./ready.sh --paths`
   prints the list; check it before committing.

### 6.4 How to re-verify this document

```bash
# counts
ls app/*.py | wc -l                                  # 352
grep -c "include_router" app/app.py                  # 19
ls app/routers/*.py | wc -l                          # 20 (19 + __init__)
ls app/tests/test_*.py | wc -l                       # 1663
ls app/static/*.js | wc -l                           # 45

# routes
grep -rn "@router\." app/routers/*.py | wc -l        # 353 decorators, 351 parsed routes

# admission constants
grep -n "process_state = \|NON_GPU_TASKS = \|GPU_TASKS = \|def check_global_gpu_lock" app/core.py

# generated files
tools/regen_derived.sh --paths
```

If a number here disagrees with the tree, the tree is right — regenerate with the
extractor rather than editing prose by hand.

---

## Appendix A — State block

```
INDEX_VERSION: 2026-11-24-a978efa25
TARGET_SHA: a978efa252e042a16a28e9cabea1a85afc06b9a9
TARGET_DATE: 2026-11-24
SUPERSEDES: 2026-11-20-c91bee0f
app_py_at_root: 352
app_python_lines: 120389
routers_py: 20            # 19 routers + __init__.py
include_router_calls: 19
routes: 351               # 353 @router. decorators; 2 not parsed as routes
static_js: 45
static_files: 77
test_modules: 1663
tests_all_py: 1679
experiments_py: 350
docs_files: 354
tools_files: 87
root_py: 20
root_js: 10
env_var_names: 193
```

Route counts per router (descending): editor 81, script 67, voices 29,
sound_design 22, lora 21, system 20, voice_design 14, scripts_library 14,
voice_library 11, pronunciation_panel 11, dataset_builder 11, voicelab 10,
preparer 7, character_roster 7, speaker_review 6, project_scope 6,
translated_edition 5, casting 5, benchmark 4.

## Appendix B — Priority file index

| Read first when… | File |
|---|---|
| Understanding startup, paths, locks | `app/app.py`, `app/core.py` |
| Changing anything persisted | `app/book_state_transaction.py`, `app/project.py` |
| Changing chunk/audio selection | `app/project.py`, `app/take_publication.py`, `app/audio_take_history.py` |
| Changing admission | `app/core.py` (`claim_gpu_task`, `gpu_lock_conflicts`), `app/task_ownership.py` |
| Changing the LLM path | `app/lmstudio_settings.py`, `app/llm_provider.py`, `app/generate_script.py`, `app/three_pass_generate.py` |
| Changing the script/review | `app/review_script.py`, `app/speaker_identity.py`, `app/character_roster*.py` |
| Changing voices | `app/voice_config_store.py`, `app/voice_library.py`, `app/voice_reference_import.py` |
| Changing TTS | `app/tts.py`, `app/synthesis_windows.py`, `app/synthesis_recovery*.py` |
| Changing export | `app/audio_export_plan.py`, `app/export_staging.py`, `app/export_quality.py`, `app/audio_m4b_pcm.py` |
| Changing read-along | `app/readalong_sidecar.py`, `app/readalong_export.py`, `app/epub_readaloud.py` |
| Changing the frontend shell | `app/static/index.html`, `app/static/app-core.js` |
| Changing tests/CI | `ready.sh`, `tools/regen_derived.sh`, `app/tests/` |

## Appendix C — Area → file index

| Area | Primary files |
|---|---|
| Setup / LLM | `routers/system.py`, `lmstudio_settings.py`, `llm_provider.py`, `config_settings.py` |
| Ingestion | `routers/script.py`, `epub_package.py`, `epub_publication.py`, `epub_extraction_report.py` |
| Calibre | `calibre_opds.py`, `calibre_catalog.py`, `calibre_import.py`, `static/js/app-calibre.js` |
| Generation | `generate_script.py`, `three_pass_generate.py`, `compact_pass1.py`, `pass_quality.py`, `chunk_quality.py` |
| Review | `review_script.py`, `speaker_identity.py`, `routers/speaker_review.py`, `routers/character_roster.py` |
| Pronunciation | `pronunciation.py`, `routers/pronunciation_panel.py` |
| Casts / voices | `voice_config_store.py`, `voice_candidates.py`, `routers/voices.py`, `routers/voice_library.py`, `routers/voice_design.py` |
| Casting library | `casting_catalog.py`, `casting_tags.py`, `routers/casting.py`, `static/js/app-casting*.js` |
| Editor / synthesis | `routers/editor.py`, `project.py`, `tts.py`, `audio_generation_owner.py`, `synthesis_*.py` |
| Verification | `word_timing.py`, `speech_verification*.py`, `voice_drift.py`, `chunk_outliers.py` |
| Speech text | `speech_numbers.py`, `speech_policy.py`, `speech_text.py`, `speech_retry.py` |
| Export | `audio_*.py`, `m4b_*.py`, `export_*.py`, `routers/editor.py` |
| Read-along | `readalong_sidecar.py`, `readalong_export.py`, `epub_readaloud.py`, `qwen_*.py`, `ruby_text.py` |
| Sound design | `sound_design_*.py`, `routers/sound_design.py` |
| Delivery | `audiobookshelf_delivery.py`, `static/js/app-audiobookshelf.js` |
| Translation | `translated_edition.py`, `routers/translated_edition.py` |
| Project scope | `project_scope_*.py`, `routers/project_scope.py` |
| Voice Lab | `routers/voicelab.py`, `routers/preparer.py`, `routers/dataset_builder.py`, `routers/lora.py`, `adapter_*.py`, `lora_evidence.py`, `tools/voice_lab/` |
| Workspace | `routers/scripts_library.py`, `book_workspace.py`, `collection_overview.py`, `run_history.py`, `workspace_guidance.py` |
| Benchmarks | `benchmark_*.py`, `routers/benchmark.py` |
| Diagnostics | `diagnostics.py`, `api_logging.py`, `routers/system.py` |
