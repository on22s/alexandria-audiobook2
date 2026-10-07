# Batch 10: Audio generation and voice persistence

Design-voice rendering forwards the configured seed in single and batch paths, stages output copying before publication, and cleans throwaway previews after failures. Timeline-selected voices receive the effective backend capability check before batch dispatch. Dynamic narrator selection updates the caller’s supported speaker key as well as the canonical narrator key, so title-case narration receives its selected version.

Voice repair treats ensembles as deliberate assignments and distinguishes their member sets for ambiguous-conflict refusal. Legacy speaker scoring uses the shared platform venv selector, preserving running-interpreter, configured-interpreter and environment-override precedence. The VRAM benchmark now atomically saves its real printed summary beside the selected JSON output, preserving prior summary bytes and releasing the GPU lease when publication fails.

Validation: 168 integrated focused tests passed without skips. The suites exercise real PCM output, source/copy/publication failure boundaries, actual timeline/narrator dispatch, applied voice-repair artifacts, real CLI summary persistence and disposable Windows/POSIX interpreter layouts. Inference/model/GPU boundaries are synthetic; no GPU benchmark or native Windows interpreter execution is claimed. Full release gate passed: 6,948 unit tests across three shards without unit skips, plus 70 quick API checks; 12 full-mode API cases were skipped. Another 19 style-timeline/audio checks passed after correcting a preview fixture to accept and verify the configured seed.

Tracker: #917.

- [x] Fixes #875
- [x] Fixes #876
- [x] Fixes #877
- [x] Fixes #878
- [x] Fixes #881
- [x] Fixes #910
- [x] Fixes #913
