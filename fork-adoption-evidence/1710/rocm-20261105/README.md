# #1710: bounded native-Windows ROCm qualification

Measured 2026-11-05 on an AMD Radeon RX 9070 XT, using public Watson adapter assets and six synthetic English sentences. **CUDA is untested; this does not close #1710.**

The production switch is **dtype-wide**, not ROCm-only: every bf16 LoRA load, including NVIDIA CUDA and Linux ROCm, retains PEFT. Only this one native-Windows ROCm configuration was measured. There is no serving-mode opt-out. fp32 still merges. This bounded delivery accepts that portability/throughput uncertainty; it does not claim CUDA, Linux ROCm, other adapters or batching were qualified.

## Results

| Measurement | bf16 merged control | bf16 unmerged |
| --- | ---: | ---: |
| Median relative L2 error of adapter-induced prefill logits versus fp32 | 0.148076 | 0.049549 |
| Median wall time per generated line | 4.2500 s | 6.9845 s |
| Median wall time / generated audio duration | 1.2713 | 2.2745 |
| ASR word edits / expected words | 1 / 104 | 0 / 104 |
| Final two expected words found in transcript suffix | 12 / 12 | 12 / 12 |

Every one of the six canonical-input logit cases has lower relative error unmerged. These are the **last prefill-position codec logits**, not a full teacher-forced utterance. Inputs are captured once from fp32 Qwen, rounded to bf16 then lifted to fp32; the bf16 arms receive exactly the same numeric embeddings. The reference effect is fp32 merged-minus-base; each bf16 effect is its own serving mode minus bf16 base. Relative error is norm(candidate effect - reference effect) / norm(reference effect). Base and LoRA effects are not conflated with unrelated base-model quantization.

Unmerged is **78.9% slower by the ratio of median generation/audio times** (the goal-4.1 metric), **64.3% slower by the ratio of median line wall times**, and the median matched text/seed wall-time ratio is **1.6831**. Neither arm meets goal 4.1 median <=0.90x. Different output durations explain why these ratios differ. Arms ran in a fixed sequence after one warm-up each: this is not a controlled thermal study, confidence interval, multi-adapter benchmark, or throughput guarantee. Historical merged-serving speed results do not describe the new bf16 path. The slower no-LoRA arm is not a quantified thermal-drift floor: serving conditions differ and drift was not isolated.

A fixed native Qwen speaker encoder followed by leave-one-text-out nearest-centroid classification separates unmerged from the same-reference no-LoRA control in **23 / 24 held-out trials (95.83%)**. Both seeds of the held-out text are excluded from training. Mean signed margin is 0.0011143. This exceeds the predeclared 75% separation threshold, but establishes only a measurable speaker-embedding difference for these conditions, **not** perceptual superiority, equivalence, or generalized identity preservation. No listening acceptance is claimed.

The independent CPU Whisper small.en pass used no expected-text prompt, beam size 5, VAD off, and previous-text conditioning off. Eight metric controls passed, plus silent-audio and half-truncated-audio controls both failed the ending check as intended. Whisper hallucinated "You" on silence; therefore transcript matching is bounded evidence, never an acoustic completeness/pronunciation certificate.

## Recovery is intentionally not certified

The unmerged bf16 model receives **no merged-serving fingerprint**. The loader explicitly reports that synthesis-recovery certificates and window-cache reuse are unavailable. Recovery rejects the unmerged-mode marker with unqualified_unmerged_lora_serving before comparing a merged fingerprint, even if a stale fingerprint exists. The orchestration still reports its generic unproven_dependencies receipt. This patch does not restore unmerged recovery certification or claim those takes are certified. Normal non-recovery audio generation remains available. fp32 merged serving keeps its existing recovery fingerprint checks.

**Take-history consequence:** new bf16 LoRA renders have no certified take-publication receipt. Their historical takes remain available for audition, but **guarded Restore is unavailable** (audition-only). This is not a hidden claim that all historical takes are restorable. Serving/recovery diagnostic reasons remain coarse in orchestration.

Evaluation evidence version 3 did not distinguish this serving change. It is no longer current; rerun evaluations to produce version 4. Existing files are preserved, never relabelled as evidence for unmerged serving. Version 4 invalidation is conservative across voice types; the measured identity separation here is not a replacement for each voice evaluation.

**Remaining evaluation limitation:** version 4 invalidates old evidence but does not bind the resolved dtype or serving mode. For example, `evaluate_lora.py --device cpu` with configured `device: auto` can record merged/fp32 evidence that later validates for GPU/bf16 unmerged serving. A current v4 result is therefore not proof of same-mode evaluation. Keep evaluation and serving on the same runtime/device and independently verify mode; automatic cross-mode currency remains unresolved under #1710.

## CPU-only evidence replay and provenance supplement

Run `python reduce-numeric.py` in this directory with NumPy installed: no Torch, model download or GPU is needed. `numeric-logits.npz` contains only the six public/synthetic last-position arrays for each precision/arm. The reducer verifies the archive digest, recomputes all 12 comparisons and both predeclared gates, and checks the recorded values within explicit tolerances. This archive/reducer was added **after** measurement from the retained tensors; it does not amend the frozen protocol or claim a new GPU run. The frozen protocol uses the shorthand "fp32 adapter-minus-base"; this supplement specifies the actual reference as **fp32 merged-minus-base**, rather than silently rewriting pre-run evidence.

`measurement_source_sha256` retains the exact captured CRLF bytes. `measurement_source_identity` additionally records the repository-standard `sha256-lf-v1` digest, computed after replacing CRLF with LF. The canonical anchor reproduces from the committed source on either platform; it is not a different measured implementation. All five original executed scripts and the pre-run protocol retain their original hashes. Per-arm runtime/state/source provenance is preserved in `audio.arm_provenance`.

An identical-vector classifier null control was added and executed **after** the original measurement: 24 trials, balanced accuracy 0.5, mean margin 0, separation gate false. This supplemental negative control is pinned by `test_lora_rocm_qualification.py`; it was not a pre-run control. No new listening, acoustic completeness or generalized identity claim follows.

The archived five scripts are the exact historical measurement tools, **not unattended acceptance gates**: they use assertions, their resume/adoption paths do not fully validate cached inputs, the scorer does not enforce arm state/source identity, and the suffix check tolerates extra trailing words. Before using a new run as evidence, independently check complete arm/ASR states, exact text/seed grids, source/runtime/adapter identities and WAV digests. The parent independently performed 115 checks against the retained raw outputs/tensors for this run. That check validates these records, not the scripts for arbitrary future inputs; silence/truncation controls do not certify full acoustic completeness.

## Reproduce without private assets

1. Use a dedicated supported native-Windows ROCm environment. This run used Python 3.12.13, torch/torchaudio 2.9.1+rocm7.2.1, PEFT 0.21.2, qwen-tts 0.1.1, transformers 4.57.3, NumPy 2.5.4 and soundfile 0.14.0. Do not replace an existing app environment or alter drivers/security settings.
2. Copy the five Python files here into an out-of-tree work directory. They resolve asset/output paths beside themselves; pass the checkout explicitly through --repo. Do not edit a running copy.
3. Download public fixtures with hf download, using --local-dir rather than changing Windows symlink privileges:

~~~sh
hf download Qwen/Qwen3-TTS-12Hz-1.7B-Base --revision fd4b254389122332181a7c3db7f27e918eec64e3 --local-dir "$WORK/qwen-model"
hf download Finrandojin/Alexandria watson/adapter_config.json watson/adapter_model.safetensors watson/ref_sample.wav watson/training_meta.json --revision d5a523e68181faf92daf696d62da02bd6bb2e7fe --local-dir "$WORK/public-adapter"
hf download Systran/faster-whisper-small.en model.bin config.json tokenizer.json vocabulary.txt --revision d1d751a5f8271d482d14ca55d9e2deeebbae577f --local-dir "$WORK/asr-small-en"
~~~

4. Under the ROCm interpreter, run sequentially (each process acquires the authoritative native GPU lock and refuses insufficient headroom):

~~~sh
python "$WORK/rocm-numeric.py" --repo "$REPO" --precision fp32
python "$WORK/rocm-numeric.py" --repo "$REPO" --precision bf16
python "$WORK/rocm-audio.py" --repo "$REPO" --arm no_lora
python "$WORK/rocm-audio.py" --repo "$REPO" --arm merged
python "$WORK/rocm-audio.py" --repo "$REPO" --arm unmerged
~~~

5. Use a separate CPU ASR environment with faster-whisper 1.2.1, CTranslate2 4.8.5, av **16.0.1**, NumPy 2.5.4 and soundfile 0.13.1. av 19.0.2 rejected faster-whisper's metadata_errors argument; it was replaced only in this isolated measurement environment. Run score-audio.py --repo "$REPO" **after all three audio arms are complete**, then score-audio-controls.py "$REPO". Each decoder call is local CPU inference.
6. Recompute the identity table with qualification_metrics.get_identity_separation on the three arm result objects; run_controls() must succeed first. Compute numeric differences from the saved fp32/bf16 logits as described above.

Every audio arm has twelve rows (six texts, seeds 1710 and 2710) plus an excluded warm-up. Logs, raw WAVs, canonical input tensors and unrounded embeddings remain local; only the last-position numeric logits are committed for CPU replay. results.json preserves hashes, all 36 timings/transcripts, all 12 numeric comparisons, and every held-out identity prediction. The exact and portable measured source identities and script hashes are recorded. No automatic downloading, private-text search, contention override, or unbounded model sweep is part of these harnesses.

## Scope still open

CUDA, Linux ROCm, other adapters/languages, long-form and batch generation, native-browser interaction, and unmerged synthesis-recovery certification are **not qualified** here. This is a scoped ROCm bug repair, not universal #1710 acceptance.
