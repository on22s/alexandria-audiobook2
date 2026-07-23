# Learn From CosyVoice

## Current scope

Alexandria's current priority is fixing and qualifying the three-pass script
pipeline. This document records lessons for later TTS-quality work. It is not a
plan to copy CosyVoice, replace Qwen3-TTS, or expand the current three-pass task.

## Core lesson

TTS generation should produce a candidate result, not an automatically accepted
result. Generated audio should pass an audio-level quality gate before Alexandria
uses it in a finished audiobook.

## Lessons for Alexandria

### Evaluate independent quality dimensions

Measure these separately instead of reducing quality to one score:

- Text fidelity: Did the audio say the intended words?
- Speaker consistency: Does the voice still sound like the assigned character?
- Emotional fit: Does the delivery match the scene and instruction?
- Prosody: Are rhythm, phrasing, emphasis, and pauses natural?
- Stability: Did generation loop, repeat, clip, stall, or stop early?
- Performance: How long did generation take and how much memory did it use?

A line can sound natural while saying the wrong words. It can also be accurate
while using the wrong voice or emotion. Each failure needs a different response.

### Validate audio after generation

A future post-TTS qualification loop should:

1. Generate one audio chunk.
2. Transcribe the generated audio.
3. Compare the transcription with the intended entry text.
4. Check for repetition, long silence, clipping, abnormal duration, and early
   termination.
5. Accept the chunk or retry only that chunk.
6. Preserve the failed attempt and its evidence for diagnosis.

### Make every chunk reproducible

Record enough information to reproduce a bad line:

- TTS model and runtime fingerprint
- Random seed
- Voice-reference file hash
- Original and normalized text
- Speaker and delivery instruction
- Generation settings
- Retry number and failure reason
- Output-audio hash

### Structure delivery controls

Keep expressive prose where it helps the model, but also derive validated fields
that can be measured and compared:

- Emotion
- Emotional intensity
- Pace
- Volume
- Delivery style
- Language or accent, when applicable

This would make instructions easier to validate, compare between models, and
adjust during targeted retries.

### Give pronunciation its own layer

Support a per-book pronunciation dictionary for:

- Character and place names
- Invented fantasy terms
- Spells
- Initialisms and abbreviations
- Foreign-language phrases

Pronunciation corrections should be explicit and auditable instead of silently
rewriting the source text.

### Detect degeneration locally

Treat repeated syllables, looping phrases, frozen audio, unexplained silence,
and unusually long output as first-class failure modes. Retry the affected chunk
rather than restarting the book.

### Cache stable speaker conditioning

Investigate whether the current TTS engine recomputes identical voice-reference
conditioning for every chunk. If it does, a cache keyed by the reference-audio
hash may save substantial work across an audiobook. Any future cache must retain
the existing VRAM limits, GPU lock, cleanup, and retry safety nets.

### Test difficult material

Qualification should include emotional screams, fragmented speech, nested and
multi-paragraph dialogue, unusual punctuation, fantasy terminology, malformed
source material, and very long books. Clean demonstration sentences are not a
sufficient release test.

## Ideas that are not current priorities

- Low-latency streaming is useful for interactive speech but not central to
  offline audiobook generation.
- NVIDIA-specific TensorRT acceleration does not directly help the current AMD
  Vulkan/ROCm environment.
- Adding vLLM would introduce another runtime stack and should require separate
  evidence.
- CosyVoice could someday be evaluated as an isolated external TTS backend, but
  it should not be mixed into the current three-pass fixes.

## Future success criteria

A production-ready audio qualification system should be able to answer:

- Which exact chunks failed?
- Which quality dimension failed for each chunk?
- Can every failure be reproduced?
- Did a retry improve the failed dimension without damaging the others?
- Can incomplete audio ever be mistaken for a finished audiobook?
- How do models compare on quality as well as speed?

## Deferred work

This material belongs in the post-three-pass TTS hardening backlog. Revisit it
after the three-pass pipeline and its model A/B qualification are stable.
