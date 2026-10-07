# Batch 09: Audio, export and voice assets

Audio generation now rejects non-finite samples before WAV encoding and validates every RIFF declared extent, while retaining RF64 support. Corrupt chunk recovery preserves earlier backups. Shared structural-text detection recognizes quoted and Unicode sentence endings; smart chapters exclude ordinary short dialogue and complete narration, retaining explicit headings, narrator fragments and the existing no-heading fallback.

Chapter exports stage the selected audio and manifest before publication and restore prior files on error or cancellation. Failed rollback keeps recovery backups with their location. Audacity checks cancellation through track/ZIP construction and before publication. Voice deletion preserves audio if manifest publication fails; Designer edits roll audio back if metadata publication fails, preserving recovery audio if restoration fails.

Voice suggestions capture script, personas, identity and counts under the book lock, retain a generation token, and refuse stale generated suggestions. Persona recovery revalidates the same source generation under the book lock before saving. External TTS cleanup failures retain the per-chunk timeout result and later chunk bookkeeping, report leftover staging, and never publish late audio.

Validation: 185 integrated focused tests passed without skips, using synthetic text, real PCM, actual HTTP routes, publication/rollback failure injection, actual book-load transactions and real executor threads. Native Windows behavior and GPU/LLM inference are not claimed. Full release gate is pending.

Tracker: #917.

- [x] Fixes #762
- [x] Fixes #763
- [x] Fixes #789
- [x] Fixes #790
- [x] Fixes #791
- [x] Fixes #792
- [x] Fixes #793
- [x] Fixes #809
- [x] Fixes #810
- [x] Fixes #815
- [x] Fixes #816
- [x] Fixes #873
