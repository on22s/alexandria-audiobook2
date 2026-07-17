# Voice Lab ROCm Validation — 2026-07-17

## Outcome

The isolated real-audio Voice Lab validation passed on the configured AMD ROCm
device. No production model, manifest, or source audiobook was modified.

## Baseline

- App build: `a0d2bf00f9fefd6ee39edcd475ac4ea9465fca75`
- GPU: AMD Radeon RX 9070 XT
- Interpreter: Python 3.10.20
- Torch / HIP: 2.7.0+rocm6.3 / 6.3.42131-fa1d09cbd
- Initial free VRAM: 16,865,296,384 bytes of 17,095,983,104 bytes
- Initial free disk: 24 GB; filesystem 97% used
- App GPU task state: idle

## Input and isolation

The run used the smallest valid existing real prepared archive:
`narrator_dracula_[audible_edition]_[b0078pa1oa]_char2_vol01.zip`. It contains
two WAV samples (8.2 and 19.9 seconds; 28.1 seconds total) plus reference audio.
All writes went to a dedicated `/tmp/alexandria-voicelab-validation.*` tree.

## Stage results

1. Dedup ran through SpeechBrain ECAPA on ROCm and retained the only archive.
2. One-epoch training prepared both samples with zero skips and completed in
   17 seconds at loss 4.5429. Its candidate matched production byte-for-byte
   and was correctly discarded.
3. A bounded two-epoch run completed in 18 seconds. Loss moved from 4.5429 to
   4.5396 and retained one distinct epoch-1 candidate.
4. Evaluation produced two deterministic probes for production and two for the
   candidate. Both evaluations passed without clipping or warnings.
5. The candidate ranked above production (mean speaker similarity approximately
   0.7333 versus 0.6550).

## Evidence and recovery

- Evidence format: version 2
- Production checkpoint before promotion:
  `341cc1ffb7b3e1d623c36460dbf87afa2f4f26f946d7c9427e3176de6891aab6`
- Candidate checkpoint:
  `5c39cab754883b5b8ab281733df1aaa66b9714123656f195ff65a7e96baccf8b`
- Promotion installed the candidate hash.
- Rollback restored the exact original production hash.
- Evaluation recorded checkpoint, reference-audio, specification, and generated
  probe hashes. Four paired WAV files existed before cleanup.
- Forced interruption was not injected into real weights. Focused recovery tests
  covering interrupted swaps and manifest recovery passed instead.

## Verification

- Focused Voice Lab/promotion suite: 36 passed, 0 failed, 0 skipped.
- Release verifier: 311 unit tests passed.
- Quick API suite: 70 passed, 0 failed, 12 explicitly skipped because they
  require full-mode GPU/TTS/LLM execution.
- API contract snapshots matched.

The first focused-test command was run from the wrong directory and collected
four import errors without executing tests. It was rerun correctly from `app/`.

## Findings

- One epoch cannot yield a distinct comparison candidate because that checkpoint
  is also production. Validation or UI language should make this relationship
  explicit when candidate comparison is requested.
- `rocm` correctly resolves to PyTorch's `cuda` backend, but SpeechBrain warns
  that bare `cuda` lacks an index and falls back to device 0. The run stayed on
  ROCm; Phase 2 should report the canonical backend and resolved device index.
- Low disk headroom made isolation and bounded retention important. The largest
  temporary footprint was about 226 MB and was removed after evidence capture.
