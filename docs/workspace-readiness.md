# Workspace readiness facts

The Home workspace uses AA2's existing Script, Voices, Editor and audio/export
screens. Continue refreshes the workspace before navigating; it does not start,
resume or repair work. Book loads retain the existing confirmed library action.

## Shared facts and their limits

| Area | Existing authority | Workspace behavior |
| --- | --- | --- |
| Book/script | `book_state_transaction.get_book_snapshot` and selected-book token | Read saved annotations under non-recovering book admission. Workspace and companion saved-library listing reads refuse pending transaction journals with 409; explicit/startup recovery remains responsible for recovery. |
| Script recovery | `script_recovery_facts.get_saved_script_recovery_manifest`, also used by Script | Show failed/incomplete saved candidates for the selected input path. Deeply malformed optional manifests do not hide a readable book. A matching path does not prove current source bytes, model or settings; resuming rechecks eligibility in Script. |
| Cast | `routers.voices.get_voice_rows` | Use existing persona and persona-state eligibility. Prepared settings do not prove a voice has been listened to. |
| Production | `ChunkStatusJournal.get_rows`, `audio_validation.is_audio_file_present` | Observe saved/journal progress without compaction, chunk initialization or ID backfill. Count present files, errors, missing done-row audio and generating rows. Presence does not prove current script/voice dependencies or audio quality. Inspect existing chunks before choosing missing work. |
| Review | `editor_review_facts.get_editor_review_facts` and canonical speech receipt filter | Count current metadata, refused/stale or malformed evidence, unchecked speech, attention flags, drift flags and unknown speakers. Never decode or hash audio, run ASR, load a model or imply a quality pass. |
| Export | `export_quality_session.get_session_evidence` | Show bounded session checks and failed attempts for the selected book. Output metadata changes invalidate receipts. Checks describe the completed export; within-book script/voice edits do not change the book token and therefore cannot be certified by it. Raw MP3/M4B presence stays unverified. |
| Polling | `EditorPollSnapshots`, Editor status route, existing task monitor | Reuse the monitor for task controls. Editor status admits/loads Editor chunks and is unsuitable as the workspace's read-only data source. Workspace uses its own observational GET and rejects obsolete browser replies. |
| History | `run_history.list_runs` / `record_artifact` | History is available in existing run views. Artifact SHA/size describe declared bytes at recording time; they lack a cheap current-file version binding. Do not infer readiness from old hashes or call mutating `mark_interrupted_runs` from Home. |

The new view fills missing recovery, saved speech-review and historical export
facts in the existing cards. It adds no scheduler, task registry, production
engine or workflow state machine. Full current source/voice attestation, native
inference and listening remain separate checks; unknown evidence stays unknown.

## Walkthrough coverage before navigation changes

- **Creation:** missing script/chunks leaves Script as the next screen. Selecting
  a script uses canonical cast eligibility, then Editor. Opening Home never
  creates chunks. Covered by `test_book_workspace` and the isolated API suite.
- **Interrupted work:** saved generating/error/missing-file rows remain observed;
  failed/incomplete script manifests offer inspection in Script. Complete or
  different-input manifests do not offer recovery. A changed source at the same
  path still requires resume validation, as tested by the workspace regression.
- **Book recovery:** actual workspace GET and the companion saved-library GET refuse publishing, recovered,
  committed, unknown-phase and malformed journals without changing script,
  journal or backup bytes. Separate explicit admission still restores the old
  script or cleans terminal transactions. Lock/admission timeout returns 409
  before facts are read. These are the #1509 regression controls.
- **Voice/source/take edits:** canonical persona-state rules and speech receipt
  filtering expose pending or non-current evidence. The view does not reset
  rows, discard takes, regenerate completed audio or promise that present files
  match new voices. Regeneration remains an explicit Editor action through
  existing book/UID/repair-revision admission and task/GPU arbitration.
- **Export:** a completed session receipt is shown historically. Tests edit
  script and voice settings without changing the book token; current inputs
  remain unchecked. Replacing output bytes invalidates the receipt; changing
  books filters it out. Existing export publication still performs byte proofs.
- **Browser ownership:** real Node DOM tests cover late/queued reads, book
  invalidation, leaving Home during refresh, malformed evidence, hostile text,
  failed refresh and navigation. Continue performs GETs only and navigates to
  existing screens. No new mutating action is based on an old workspace card.

These are CPU API/DOM and source walkthroughs, not native model or listening
qualification. Required release verification and exact-commit CI remain
separate evidence for each PR.
