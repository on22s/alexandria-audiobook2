# Original codebase review remediation

Source: `/home/fakemitch/Desktop/codebase_review_findings.json` (SHA-256
`a822366d76c1b6cd19a4f7da28a71a464574985a5bb0b3c01a1683991337e167`).
Candidate IDs below are one-based positions in that file's `candidates` array.
It contains 1,374 candidates: 1,049 originally marked CONFIRMED, 199
PLAUSIBLE, and 126 REFUTED. Those labels are inputs, not our conclusions.

P0 priority for this pass comes from Desktop
`codebase_review_findings_scored.json` (69 IDs). The score is a triage cue,
not a verified severity: the scoring notes say its P0 labels are inflated.
The Desktop Space Bunny suggestion files are examples to inspect, not patches
to apply without code and test review. `AUDIT_COVERAGE.tsv` records the
independent disposition for every original ID.

This is the recovered earlier-remediation ledger, integrated on September 29.
Its 98 fixed, 2 refuted, and 2 duplicate dispositions are historical evidence;
IDs absent below have not been reviewed by that earlier pass. The current
P0/P1/P2 scope and later dispositions are maintained in the Desktop
`space_bunny_workflow/review_state_20260929.json` and
`space_bunny_review_20260929.md`. A `fixed` row here describes a targeted
change and regression test, not a merged or shipped fix.

| ID | Disposition | Evidence in this branch |
| --- | --- | --- |
| 7 | fixed | Requested diarization now raises on unavailable model/dependency or pipeline failure instead of returning an empty successful result; `test_requested_diarization_failure_is_not_empty_success` covers both paths. |
| 14–16, 35–39, 45, 49, 73, 77 | fixed | One content/options run manifest now owns phase artifacts and resume decisions; a cross-process lock protects `dataset_temp`, JSON artifacts are atomically written and shape-checked, WAV metadata tags are idempotent, directory entries are fsynced, and caller-owned scratch paths are refused/preserved. `test_preparer_run_manifest.py` and `test_preparer_run_state.py` cover changed input/options, phase handoff, locking, durability, tagging, and scratch collisions. |
| 18, 34, 46 | fixed | Checkpoint records now require safe local sample basenames, valid timeline/text, and existing WAVs; malformed records fail before appending, and ZIP preflight refuses missing or escaped WAVs without replacing the old output. `test_preparer_run_state.py` covers those failures. |
| 19, 76 | fixed | ZIP output now plans unique paths before writing and records its managed volumes. A shorter managed rerun removes obsolete volumes; tests cover both group-name collisions and volume reduction. |
| 17 | fixed | Managed reruns remove obsolete ZIPs. A pre-manifest rerun with an ambiguous old volume refuses completion without deleting files it cannot prove it owns. Tests cover both paths. Interrupted multi-volume replacement remains a separate lifecycle risk. |
| 12 | fixed | The source divergence preflight honors `--keep-unaligned`; a phase-level test proves the low-alignment case aborts without the flag and reaches annotation with it. |
| 13, 20 | fixed | Direct CLI validation now rejects missing diarization tokens and invalid numeric ranges before phase work; the phase-level tests cover both. |
| 32 | fixed | Empty annotation output raises instead of reporting a completed ZIP; the empty-dataset test verifies no archive is created. |
| 42–44 | fixed | One phase-command builder preserves option values equal to phase names; enrichment uses the requested chunk size and resolves its helper beside the preparer. Tests exercise the command and an enrichment phase from another working directory. |
| 75 | duplicate of 42 | The observable raw-argv failure is covered by the shared phase-command builder; separate phase processes remain necessary for ROCm isolation. |
| 56 | refuted | The flag is explicitly documented as reserved and rejected in `docs/guides/PREPARER_GUIDE.md`; silently accepting it would imply a transcription-only export that does not exist. |
| 61–62 | fixed | Audio longer than 10 minutes (and overflowing WAVs) now decodes to a temporary file-backed float32 array instead of capturing ffmpeg stdout or loading the native-rate file through librosa. The ASR phase test exercises actual ffmpeg output, checks the memory map, and checks cleanup. Backend-specific transcription memory remains outside this loader fix. |
| 63 | fixed | The diarization input reads bounded SoundFile blocks into a disk-backed array and passes a tensor view without the former full `sf.read` plus `.copy()`. Tests cover stereo values, multiple blocks, and reject use of full-file `sf.read`. Pyannote's internal memory use still needs a real long-book run to measure. |
| 86 | fixed | The standalone batch CLI and FastAPI GPU tasks now hold the same `gpu_job.sh` filesystem lock for their task lifetime. Tests verify contention, lock retention during batch execution, API claim retention, and release. The original summary called these TTS subprocesses; this wrapper actually launches the preparer. |
| 338 | fixed | Cancelling an active subprocess now sets the shared cancel flag before sending SIGTERM, allowing the stream worker to escalate to SIGKILL if needed. The focused test checks the active-PID path; the existing subprocess test exercises escalation against a child that ignores SIGTERM. |
| 448 | refuted | `ProjectManager._read_chunks` rejects a persisted list containing non-dicts, backs up `chunks.json` as `.corrupt`, and regenerates chunks before startup's `reset_stuck_chunks` accesses them. `test_bad_chunks_are_backed_up_and_regenerated` covers `[None]`. |
| 1339–1342 | fixed | The Hugging Face purge scans all HEAD paths for LFS objects, checks the author's model, dataset, and Space repositories, rejects lesson/purge path overlap, and checks required expected bytes against target object sizes before any commit or permanent deletion. `test_hf_purge_guards.py` verifies each refusal leaves the archive unchanged. |
| 106 | fixed | Rollback validates every backup and live adapter, stages all backup copies under `MODELS`, then swaps directories with an in-process reversal path for failed swaps. Tests verify missing or unreadable backups leave live adapters intact. A crash-consistent transaction spanning all adapters and the manifest remains covered by separate ID 108. |
| 276 | fixed | `local_gpu_job.py` now preserves any caller-supplied `GPU_LOCK` and lets `gpu_job.sh` choose its repository default when unset. The runner test checks the forwarded environment. |
| 339, 373 | fixed | Subprocess logging replaces invalid output bytes and drains the remaining lines; `_safe_subpath` rejects the base directory itself. Focused runtime tests exercise both failure cases. |
| 888–889 | fixed | The LoRA calibration worker rejects fixture IDs that escape its output root, and validates every metadata audio path against the source and temporary dataset before copying. Tests show the outside file survives and training never starts. |
| 151 | fixed | Enrichment output is written to a same-directory temporary file, fsynced, and atomically renamed. An injected rename failure preserves the previous output and removes the temporary file. |
| 757 | fixed | When GPU model dimensions cannot be read, the VRAM estimator permits one sequence rather than bypassing its batch limit. A focused test exercises the missing-config path. |
| 927 | fixed | Single-book start-over builds and validates the generation command before deleting recovery files. An invalid command now leaves the checkpoint and manifest byte-identical and never claims the GPU. |
| 528 | fixed | Direct three-pass runs and the CLI now reject an empty prepared source before writing checkpoints or success artifacts. A focused test covers empty and whitespace-only input. |
| 1037, 1041 | fixed | A failed dataset-builder thread start and a failed preparer upload publication/schedule now release their GPU claim immediately. Preparer publication restores every prior upload if a later replacement fails. Focused tests inject both failures and check the prior files and lock state. |
| 952 | fixed | Snapshot creation and normal saved-script writes now use the same per-name file lock. Snapshot refuses a name whose script, voice companion, or metadata already exists; a regression test verifies the prior saved script stays byte-identical. |
| 613 | fixed | Pass-1 fidelity now counts only spoken `text` as output; when source speaker labels were intentionally extracted, it compares against source region text without those labels. A regression test shows a label cannot supply omitted spoken words, and 52 segment/three-pass helper tests pass. |
| 602 | fixed | Batch alias keys now use the existing `get_exact_alias_match` resolver, which preserves distinguishing honorifics while tolerating case and punctuation. The normalization tests and a direct Mr./Mrs. lookup check pass. |
| 683 | duplicate of 602 | The later honorific merge is the same batch-mapping lookup corrected under ID 602. |
| 639, 664 | fixed | Nickname discovery always reads the existing human-editable alias registry, refuses malformed JSON, and merges discoveries under the same file lock used by the UI save endpoint. Current UI edits win on key conflicts. Tests cover alias-free, conflicting, and corrupt-registry cases. |
| 1092–1093 | fixed | Both audio ZIP routes now assemble archives in a unique disk file and serve them with `FileResponse`, removing the file after response completion. Focused route tests verify archive contents and cleanup without using an in-memory ZIP buffer. |
| 763 | fixed | Per-chunk MP3 and WAV outputs are written to unique same-directory staging files and atomically replaced only after successful export or copy. An injected encoder-plus-copy failure leaves both prior files byte-identical; the real MP3 bitrate test also passes. |
| 736 | fixed | Single custom and clone requests now use the configured external timeout, write only to unique staging paths, and refuse late results after timeout. A hung-client test checks both paths return promptly, preserve earlier output, and clean late staging files. |
| 547 | fixed | Alias updates now reload the registry under its write lock before merging. A regression test adds a second alias during the LLM call and verifies both survive. |
| 545 | fixed | Review checkpoints now bind to a SHA-256 fingerprint of their source and, for incomplete runs, the exact partial output. A regression test accepts both valid resume states and rejects a different book at the same path. Legacy unbound checkpoints start fresh. |
| 550, 587 | fixed | Incomplete review output retains the checkpoint's unmerged entry layout; narrator and alias post-processing run after all batches succeed. A two-run test verifies the failed batch retries, the first output stays unmerged, and the successful resume merges once and clears the checkpoint. VRAM-skipped batches remain at their original positions and are marked failed in the checkpoint; resume rewinds before the first skipped batch, including when concurrent workers finish out of order. |
| 445 | fixed | Built-in adapter files copy through same-directory staging files, and a completion marker records required file sizes only after all copies succeed. Download status requires that marker. A test injects an interrupted copy and a legacy partial file, then verifies a retry replaces the partial file. |
| 424 | fixed | Duplicate repair counts the smallest repeated entry unit and removes only copies beyond exact source occurrences. Tests cover one, two, and three source copies with extra model copies. When only approximate source evidence exists, an excess remains unresolved for manual review. |
| 426 | fixed | Generic Cyrillic transliteration now requires the original token to be absent from the source; explicitly known source corruptions retain their existing mapping. A Russian source test verifies legitimate Cyrillic text is not rewritten. |
| 853, 896, 922 | fixed | The repair CLI and UI save route now use one voice-config update helper that holds the file lock across read, backup, transform, and atomic write. CLI apply rejects a config changed after its report. Tests verify an intervening UI-style save survives without backup, a successful merge keeps its original backup, and the UI alias save still works. |
| 955 | fixed | The dataset delete route returns 409 while LoRA training owns the task slot. A route-level test verifies the dataset survives during training and can be deleted after the worker exits. |
| 956 | fixed | Voice Lab start now checks the same pending checkpoint-swap journals used by its health view immediately before claiming the GPU, returning 409 with affected adapter IDs. A route-level test verifies no task is claimed. |
| 949 | fixed | Script generation and manual recovery now use the checkpoint file lock around the final task claim and recovery mutation; recovery rechecks `running` after acquiring the lock. Tests simulate a generation claiming while recovery waits and verify generation claims inside the lock. |
| 643, 666, 673 | fixed | Constructing a manual LLM client no longer deletes a live prompt. Requests claim the shared prompt slot under a short file lock and wait their turn; only the matching owner releases it. Dead-owner requests are cleared on the next request. Tests verify a second client preserves the first prompt, concurrent requests complete in order with their own replies, and an abandoned request is replaced. |
| 27 | fixed | Equal-scoring source windows now prefer the one nearest the cursor; a repeated exact-phrase test pins the prior rewind. |
| 28 | fixed | Auto-anchor now skips merely acceptable intro matches for a stronger prose match, while preserving the earliest high-confidence match; both selection paths have regression tests. |
| 21 | fixed | A saved `skip` decision is now revisited on resume instead of silently bypassed. |
| 23 | fixed | Fresh-session quality estimation is no longer suppressed by auto-kept intro entries, and sampling begins at the anchor rather than including unmatched intro. |
| 24 | fixed | CLI rejects zero, negative, above-one, and nonfinite auto-approval thresholds before review. |
| 25 | fixed | Targeted reset now uses the same tolerant checkpoint loader as normal review; corrupt checkpoint JSON no longer crashes reset. |
| 26 | fixed | Targeted reset indexes parsed nonblank JSONL records, matching normal review, and refuses mismatched input/output record counts. |
| 41 | fixed | Corrected JSONL and targeted-reset output are written to a same-directory temporary file and atomically replaced only after full serialization. |
| 40 | fixed | Checkpoints now record SHA-256 identities of the JSONL and source plus the resolved output path; a changed or legacy identity refuses resume/reset rather than replaying old decisions. |
| 72 | fixed | Per-book proper nouns now travel explicitly through compare and preparer alignment calls; an interleaved two-book boundary test verifies no policy leakage. |
| 29 | fixed | A mashed-up annotation token now places pauses only at the outer boundaries of multiple source words; the regression test exercises emphasis and both pause markers. |
| 30 | fixed | `estimate_alignment_quality` now advances at the caller's threshold; `test_quality_estimate_advances_at_the_callers_threshold` covers a 0.40 match accepted at 0.35. |
| 48, 967 | fixed | Both EPUB parsers call one archive preflight before decompression. It caps input size, entry count, total expanded bytes, and per-member expansion ratio. |
| 78–79 | fixed | Dataset names include source-path identity; completion records validate source identity and every produced ZIP volume. Batch-processor preflight tests cover collisions and broken ZIPs. |
| 80–82 | fixed | Input files and numeric CLI options are validated before launching the preparer; batch-processor preflight tests cover directories, zero, NaN, and out-of-range values. |
| 83 | fixed | Source candidates are sorted for deterministic fuzzy ties; the tie regression test covers equal-scoring EPUBs. |
| 84–85 | fixed | Interrupted children are reaped after SIGKILL; the preparer path is resolved relative to this script. Both have focused tests. |
| 87 | fixed | Batch receipts are atomically replaced; a failed write preserves the prior receipt in the regression test. |
| 88 | fixed | The source catalog is tokenized once and matches are reused; the regression test counts scans across a two-book batch. |
| 385–386 | fixed | Both source-repair CLIs reject source/output/report collisions, including hardlink and symlink aliases, before processing. |
