# DeepSeek bug fixes — batch 5

Approved scope: audio generation and training. Keep VRAM batching, compile-reset safety, OOM cleanup, source-index timelines and training coverage refusal.

- [x] #948 Normalize partial successful gradient windows before clipping and optimizer steps, including a final OOM.
- [x] #949 Group clone chunks by their resolved reference audio and text.
- [x] #950 Honor a nonnegative batch seed for dynamic narrator voices without editing caller settings.
- [x] #951 Reject malformed timelines per affected chunk and continue valid chunks.
- [x] #952 Show separate compiled/uncompiled tier recommendations with existing headroom.
- [x] 59 focused tests pass.
- [x] ready.sh passed: 7,055 unit tests, 70 API checks; 12 API generation checks skipped.

Real differentiable CPU training fixtures and controlled rendering boundaries test behavior; no production Qwen inference, GPU training or audio-quality improvement is claimed.

Validation: 59 focused tests; ready.sh passed with 7,067 unit tests and 70 isolated API checks. The quick gate skipped 12 generation checks. No production GPU inference or native browser run is claimed.
