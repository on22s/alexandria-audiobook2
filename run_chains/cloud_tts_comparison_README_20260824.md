# CUDA TTS comparison — 2026-08-24

This run compares supported, official zero-shot voice-cloning implementations
on the Thunder Compute NVIDIA L40. Every runnable arm receives the same public-
domain LJSpeech reference clip (`ref_spread3.wav`) and the same audiobook-style
target passage. Default generation settings are retained except for a fixed
seed where the official interface exposes one. Each arm has an isolated Python
environment and records its source commit or package inventory, timing, output
audio, GPU, driver, and exact input.

Arms:

- Qwen3-TTS 1.7B Base via the existing isolated `qwen-tts` environment.
- Chatterbox Multilingual V3 from the official Resemble AI repository.
- IndexTTS2 from the official IndexTTS repository and checkpoint.

VibeVoice is not executed. Microsoft states in the official repository that it
removed the original VibeVoice-TTS code and disabled the TTS weights. Using a
third-party mirror would make this an unofficial comparison and introduce a
material provenance and safety difference. This is recorded as unavailable,
not scored as a model failure.

These outputs are benchmark evidence, not trained adapters or replacements for
Alexandria's current Qwen3-TTS engine. Listening/ASR and speaker-similarity
scoring must be performed after generation before drawing a quality conclusion.
