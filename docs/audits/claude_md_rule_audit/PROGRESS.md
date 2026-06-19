# CLAUDE.md Rule-Compliance Audit — Progress

Plan: `docs/superpowers/plans/2026-06-19-claude-md-rule-compliance-audit.md`

Resume instructions: find the first unchecked box below, re-read the plan's Task 1 (Standard Audit Procedure) and that piece's manifest row, then proceed. If a piece was stopped partway through, a note next to its checkbox says where.

## Phase 1: app/ (Task 2)

### Group A
- [x] P01 — app/utils.py
- [x] P02 — app/hf_utils.py
- [x] P03 — app/default_prompts.py + app/persona_prompts.py + app/review_prompts.py
- [x] P04 — app/lmstudio_settings.py
- [x] P05 — app/llm_bench.py
- [x] P06 — app/find_nicknames.py
- [x] P07 — app/tts_vram_benchmark.py
- [x] P08 — app/generate_script.py
- [x] P09 — app/train_lora.py
- [x] P10 — app/generate_personas.py
- [x] P11 — app/project.py
- [x] P12a — app/review_script.py (get_vram_usage → merge_consecutive_narrators)
- [x] P12b — app/review_script.py (review_batch → main)
- [x] P13a — app/tts.py (voice_category → compute_timeline)
- [x] P13b — app/tts.py (class TTSEngine)
- [x] P14a — app/test_api.py (helpers → chunk tests)
- [x] P14b — app/test_api.py (status/preparer/voicelab/lora/dataset-builder/audio tests + run_all_tests/main)

### Group B — app/app.py
- [x] P15 — imports → check_global_gpu_lock (lines 1–1491)
- [x] P16 — /api/system/stats → /api/upload (lines 1492–2050)
- [x] P17 — /api/generate_script → /api/logs/{task_name} (lines 2051–2646)
- [x] P18 — /api/voices → /api/suggest_voices (lines 2647–3011)
- [x] P19 — /api/audiobook → /api/review/checkpoints (lines 3012–3462)
- [x] P20 — /api/scripts → /api/voice_library/apply_bulk (lines 3463–3983)
- [x] P21 — /api/voice_design/preview → /api/clone_voices/{voice_id} (lines 3984–4142)
- [x] P22 — /api/lora/upload_dataset → /api/lora/preview/{adapter_id} (lines 4143–4667)
- [x] P23 — /api/dataset_builder/* (lines 4668–5043)
- [x] P24 — /api/preparer/* (lines 5044–5318)
- [ ] P25 — /api/voicelab/* (lines 5319–end)

### Group C — app/static/index.html
- [ ] P26 — HTML/CSS shell + tab markup (lines 1–1789)
- [ ] P27 — showToast → testLlmConnection (lines ~1790–2215)
- [ ] P28 — loadConfig → _onReviewDone (lines ~2216–2810)
- [ ] P29 — _loadScriptList → pollPersonaStatus (lines ~2811–3177)
- [ ] P30 — createVoiceCard → submitCastApplyBulk (lines ~3178–3861)
- [ ] P31 — collectVoiceConfig → _runBatchRender (lines ~3862–4663)
- [ ] P32 — pollLogs → resetDesignerForm (lines ~4664–5147)
- [ ] P33 — loadLoraDatasets → dsbStopBatch (lines ~5148–5948)
- [ ] P34 — updateSystemStats → viewReport (lines ~5949–6539)

## Phase 2: root dataset-prep pipeline (Task 3)

- [ ] P35 — download_model.py
- [ ] P36 — llm_enricher.py
- [ ] P37 — name_voices.py
- [ ] P38a — voice_analysis.py (load_model → run_dedup)
- [ ] P38b — voice_analysis.py (run_analyze → main)
- [ ] P39a — alexandria_batch_processor.py (get_gpu_stats → check_disk_space)
- [ ] P39b — alexandria_batch_processor.py (class BatchProcessor → main)
- [ ] P40a — alexandria_compare.py (load_jsonl → write_output)
- [ ] P40b — alexandria_compare.py (run → main)
- [ ] P41a — alexandria_alignment.py (_expand_honorifics → trim_span_to_alignment)
- [ ] P41b — alexandria_alignment.py (_num_eq_step_trailing → merge_annotations_with_source)
- [ ] P42a — alexandria_preparer_rocm_compatible.py (validate/WAV-wrap + audio loading)
- [ ] P42b — alexandria_preparer_rocm_compatible.py (ASR transcription)
- [ ] P42c — alexandria_preparer_rocm_compatible.py (source loading/tokenization + chunking)
- [ ] P42d — alexandria_preparer_rocm_compatible.py (multi-tier alignment recovery)
- [ ] P42e — alexandria_preparer_rocm_compatible.py (LLM prosody annotation + write outputs)
- [ ] P42f — alexandria_preparer_rocm_compatible.py (resume/checkpoint/scratch-state)

## Cross-cutting / synthesis

- [ ] Task 4 — Rule 15 cross-cutting pass
- [ ] Task 5 — Synthesis report + approval gate
