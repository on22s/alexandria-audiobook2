# Word-timed listening

In Editor, Inspect a saved line that has generated audio. Open **Word-timed listening (English audio)** and choose **Time this line**. This opt-in action runs the already installed whisper.cpp English model on the CPU. It retains AA2's normal task/shared GPU admission and cancellation rules; it refuses while that shared resource is owned by another job. No model is downloaded automatically.

The initial provider defaults to the same cached Small.en runtime used by the Preparer. Its existing `ALEXANDRIA_WHISPER_CPP_BIN` and `ALEXANDRIA_WHISPER_CPP_MODEL` environment settings apply. Other ASR providers/languages are not exposed by this initial Editor workflow. Missing runtime/model paths produce an actionable refusal. The model must be a regular file no larger than 1 GiB; there is also a system-RAM admission check. A request accepts one audio chunk of at most 120 seconds and 64 MiB, with finite decode/provider deadlines.

The existing audio player reloads through a hash-verified local stream so an older browser buffer cannot be paired with new timings. Click an underlined span to seek that player. Words highlight at their estimated times during playback. Plain words are untimed. Coalesced native groups remain groups; AA2 never invents timings for their individual words. Repeated or ambiguous words are left untimed, and invalid/overlapping/out-of-duration timestamps are refused. Use the existing line Play, Edit and Generate controls to repair the line. This does not add word-only TTS regeneration.

The displayed prepared text uses current speech normalization and pronunciation rules. Those rules may differ from an older saved take. Original script text remains visible and unchanged. ASR timing or a matching word is not proof of correct pronunciation, speaker identity or completeness: listen to the audio.

Timing records are private flat JSON files under the active data directory's `reports/word_timing`. Eligibility binds the book, row UID, saved text and instruction, prepared text, exact audio hash, take revision and provider file metadata. Editing/regenerating a take or changing the provider invalidates eligibility. Stale replies do not attach to another line. Up to 128 records are retained; per-record output is bounded to 1 MiB. This stores timing, not an extra permanent audio copy. The original audio and script are never rewritten by timing.

Cancel timing with the panel's Cancel button. Child processes are owned and reaped before task ownership retires; a cancelled or failed run does not replace a previously valid timing record.

After a page reload, the workspace opens the timing task in Editor and restores its activity/cancellation controls. When it finishes, request timing for the saved line again to read its bound cache; reload does not reconstruct an in-flight response.
