# Dependency freshness review (for Fable)

## Purpose

A third companion brief to `FABLE_ALGORITHM_REVIEW.md` (known-algorithm
opportunities) and `FABLE_LOGIC_REVIEW.md` (control-flow correctness). This
one is about the actual **libraries and models** this project depends on:
for each one below, is it still the right choice, or has something better
(faster, more accurate, better-maintained, more permissively licensed)
emerged since it was picked? You were trained with knowledge of a lot of ML
tooling and its evolution — this is exactly the kind of "is this still
current" judgment call that's hard for an assistant without broad training
coverage of the ecosystem to make confidently.

This is a **scouting/analysis task, not an implementation task.** Produce a
written report — see "Deliverable" below.

## Ground rules

- **A pinned version is not automatically a problem.** Every pin below is
  in a working, tested app. "Newer exists" is not the same as "worth
  upgrading to" — check for breaking changes, and note that some pins exist
  for a documented reason (see the ROCm constraint immediately below). Only
  recommend a change when you can say *why* it's worth the churn.
- **This app runs on AMD ROCm, not just CUDA.** `app/requirements.txt`'s own
  top comment says it explicitly: torch/torchaudio/pytorch-triton are
  installed by `torch.js` *before* `pip install -r requirements.txt` runs,
  specifically so a plain `pip install torch` doesn't pull a CUDA-only wheel
  and break GPU detection on AMD hardware. `torch.js` pins **torch==2.10.0 /
  torchaudio==2.10.0 from `--index-url .../whl/rocm7.0`** for the ROCm path,
  vs. **torch==2.7.0 from `.../whl/cu128`** for the NVIDIA/CUDA path (see
  `torch.js`, both `~line 11-12` and `~line 57-58`) — genuinely different
  versions per platform, not an oversight. Any dependency recommendation
  below must have a real ROCm build/wheel available, not just a CUDA one —
  say explicitly if you're not sure a candidate replacement supports ROCm.
- **Distinguish two different findings**: (a) "same tool, version behind" —
  a newer release of the exact same library/model exists, with real fixes
  or improvements; vs. (b) "wrong tool" — a fundamentally better-suited
  alternative exists for the same job. Both are useful, but they lead to
  very different amounts of work — label which one each finding is.
- **Note licensing** where it's non-obvious (e.g. anything gated behind a
  Hugging Face token/access request, or non-commercial-use license) — this
  is a real constraint on swapping something in, not just a technical one.

## What to check

### Core app (`app/requirements.txt`) — pinned, CPU-side web app deps

```
fastapi==0.128.0        uvicorn==0.40.0         pydantic==2.12.5
pydub==0.25.1           requests==2.33.0        openai==2.16.0  (client SDK, not the API itself)
gradio_client==2.0.3    soundfile==0.13.1       numpy==2.2.6
librosa==0.11.0         transformers==4.57.3    peft==0.18.1
python-multipart==0.0.32   aiofiles==24.1.0      mutagen (unpinned)
```
Check each against current releases. `mutagen` has no version pin at all —
flag whether that's worth pinning for reproducibility (matches this
project's own stated principle in `app/CLAUDE.md`: "Scripts must be able to
replicate install and launch steps 100%").

### TTS model itself — `install.js`

`uv pip install qwen-tts==0.1.1` (`install.js:53`) — this is the actual
voice-generation model driving the whole app. Check specifically: is
`0.1.1` current for `qwen-tts`, and separately, is Qwen3-TTS (the model
family this app is built around per `app/CLAUDE.md`'s own description)
still the strongest open option for multi-voice audiobook narration, or has
a newer open TTS model surpassed it since this was chosen? This is the
single highest-impact item on this list if there's a real answer — it's the
core product, not a support library.

### PyTorch stack — `torch.js` (see ROCm note above)

ROCm path: `torch==2.10.0`, `torchaudio==2.10.0`, `triton-rocm==3.6.0`
(`torch.js:57-58`). CUDA path: `torch==2.7.0`, `torchvision==0.22.0`,
`torchaudio==2.7.0`, optional `xformers==0.0.30` (`torch.js:11-12`). CPU
fallback paths pin `torch==2.7.0` and, in one branch, the notably older
`torch==2.2.2` (`torch.js:81`) — check whether that older CPU pin is
intentional (e.g. a compatibility floor for some other CPU-only dependency)
or just stale.

### Voice Lab audio-ML stack (repo root, ROCm-interpreter scripts)

Found by direct read of `voice_analysis.py` and
`alexandria_preparer_rocm_compatible.py` this session:

- **Speaker embeddings**: `speechbrain/spkrec-ecapa-voxceleb`
  (`voice_analysis.py:109`) — an ECAPA-TDNN architecture via the
  SpeechBrain framework. Still a reasonable/strong choice as of this
  model's training data, but check whether a newer speaker-embedding model
  (SpeechBrain has released others; there are also newer general-purpose
  embedding approaches) meaningfully outperforms it for this specific job
  (distinguishing narrators of the same audiobook series from each other).
- **Diarization**: `pyannote/speaker-diarization-3.1`
  (`alexandria_preparer_rocm_compatible.py:935`, gated behind an HF token —
  note the licensing point above) — check for a newer pyannote pipeline
  version, and whether it's still the strongest open diarization option.
- **Forced alignment / word timestamps**: two different paths exist in the
  same file — WhisperX (`whisperx_asr`/`whisperx_alignment`,
  `alexandria_preparer_rocm_compatible.py:203-204`) and a separate
  `facebook/wav2vec2-large-960h` path pinned to a specific revision hash
  (`WAV2VEC2_MODEL_REVISION`, `~line 192`) for CTC-based alignment. wav2vec2
  itself is a 2020-era model — check whether WhisperX's own alignment (which
  this codebase already also has access to) makes the separate wav2vec2 path
  redundant, or whether it's kept for a specific fallback reason (check the
  code around both call sites for why two paths exist before recommending
  removing either).
- **ASR/transcription**: a whole **vendored fork**,
  `insanely-fast-whisper-rocm/` (a full subdirectory with its own
  `pyproject.toml`/`requirements*.txt`, not a pip dependency of the main
  app) — this exists because upstream `insanely-fast-whisper` likely doesn't
  have (or didn't have, when vendored) a working ROCm build. Check: (a) does
  upstream now support ROCm directly, making the vendored fork unnecessary
  maintenance burden, and (b) is `insanely-fast-whisper` itself still the
  right tool, or has something newer (e.g. `faster-whisper`, distilled
  Whisper variants, or a new release of OpenAI's own Whisper) become the
  better ROCm-compatible choice since this fork was vendored. This is a
  "wrong tool vs. right tool but stale" judgment call — say which you think
  it is.
- **Clustering/analysis support libraries**: `umap-learn` (dimensionality
  reduction, `voice_analysis.py:615`), `scipy` (`cdist`,
  `wasserstein_distance`), `librosa` (prosody features), `matplotlib`/
  `seaborn` (plotting only, low-risk to leave alone regardless of version).
  These are mature, stable libraries — spend less time here than on the
  model choices above unless something concrete stands out.

### Test-only dependencies — `requirements-test.txt`

`playwright>=1.40.0`, `requests>=2.28.0` — open-ended `>=` pins (not exact,
unlike `app/requirements.txt`). Low priority, but note if either is
meaningfully behind current.

## Deliverable

A written report (markdown, not code), one entry per dependency/model
checked, each stating:
1. **Current pin** (version/model ID) and where it's declared.
2. **Same-tool-newer-version available?** If yes, what changed that
   matters for this app's use case (bug fixes affecting correctness,
   performance, or just churn with no real benefit).
3. **Better-tool-exists?** If yes, name it specifically and say what it's
   better at — don't recommend a switch without a concrete reason.
4. **ROCm compatibility** — explicitly confirm or flag uncertainty for any
   ML-stack recommendation (embeddings, diarization, alignment, ASR, TTS,
   torch itself). A CUDA-only recommendation is not actionable for this
   app's primary target hardware.
5. **Recommendation**: upgrade now, upgrade later (name the trigger, e.g.
   "next time torch is bumped anyway"), or leave as-is — with one line of
   why.

Do not modify any dependency file, run any install command, or touch code
as part of this task — report findings only. If something looks clearly
worth upgrading, that becomes a separate follow-up task with its own plan
(per Rule 14 in `app/CLAUDE.md`) and its own testing before landing, since a
dependency bump in this ML stack risks breaking GPU detection, model
loading, or output quality in ways that are expensive to debug after the
fact.
