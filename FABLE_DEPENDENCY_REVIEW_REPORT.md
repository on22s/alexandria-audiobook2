# Dependency freshness report (Fable, 2026-07-19)

Response to `FABLE_DEPENDENCY_REVIEW.md`. Latest-version numbers below are
**live PyPI queries made today** (not training memory). Model-level "is X
still the strongest" judgments draw on training knowledge with a January
2026 cutoff — releases after that date wouldn't be visible to me except
through the PyPI/HF checks noted; I flag that wherever it matters.

**Legend:** (a) = same tool, version behind · (b) = wrong/questionable tool

## Summary of actionable items

| Item | Type | Action |
|---|---|---|
| `mutagen` unpinned | policy | **Done** — pinned `==1.47.0`; also installed into the ROCm env, where its only real consumer (preparer WAV tagging) was silently skipping for lack of it |
| `pyannote.audio` unpinned/undocumented | (a)+(b) | **Done** — `requirements-diarization.txt` pins `>=3.1,<4` with the live-verified reasons; PREPARER_GUIDE.md documents the flags/token — **live-verified breakage today** |
| ROCm env torch mismatch | doc drift | Sibling `rocm_python` env actually runs **torch 2.7.0+rocm6.3**, not the 2.10.0+rocm7.0 that `app/CLAUDE.md`'s "verified 2026-07-14" note attributes to it (2.10.0+rocm7.0 is in **this** repo's `app/env`). Doc/memory correction needed, not a code change. |
| `transformers==4.57.3` | (a) | Hold; v5 (5.14.1) is a major migration, do not drift into it |
| `pydub==0.25.1` | (b) latent | Fine on Py3.10; hard-breaks on Python ≥3.13 (`audioop` removed) — replace on any Python bump |
| CUDA torch 2.7.0 path | (a) | Upgrade next time the CUDA path is touched anyway (current stable: 2.13) |
| Everything else | — | Leave as-is |

---

## Core app — `app/requirements.txt`

| Package | Pinned | Latest (live) | Verdict |
|---|---|---|---|
| fastapi | 0.128.0 | 0.139.2 | (a) Leave. Feature churn, no correctness fix relevant to this app's route usage. |
| uvicorn | 0.40.0 | 0.51.0 | (a) Leave. Localhost dev server; nothing gained. |
| pydantic | 2.12.5 | 2.13.4 | (a) Leave. Minor line, still v2 API. |
| pydub | 0.25.1 | 0.25.1 | **(b) latent.** Current — because upstream is dead (no release since 2021). It depends on the stdlib `audioop` module, **removed in Python 3.13**, so it hard-breaks on any future Python bump (env is 3.10 today, so it works). Trigger: when Python is bumped past 3.12, replace its usage with `soundfile`+`ffmpeg` calls (both already in the stack). No action now. |
| requests | 2.33.0 | 2.34.2 | (a) Leave. Patch-level. |
| openai | 2.16.0 | 2.46.0 | (a) Leave. Used as a plain chat-completions client against LM Studio, not OpenAI's newest endpoints; the surface this app touches is stable. |
| gradio_client | 2.0.3 | 2.5.0 | (a) Leave, with a caveat: gradio_client compatibility is really governed by the *server's* Gradio version (the external TTS server mode in `tts.py`). Bump only if a target server upgrades and handshakes fail. |
| soundfile | 0.13.1 | 0.14.0 | (a) Leave. Minor; current version works incl. the seek-window reads added in PR #197. |
| numpy | 2.2.6 | 2.5.1 | (a) Leave. Must stay compatible with the ROCm torch 2.10.0 wheel; 2.2.x is a safe known-good. Bump only alongside a torch bump. |
| librosa | 0.11.0 | 0.11.0 | Current. ✓ |
| transformers | 4.57.3 | **5.14.1** | (a) **Hold deliberately.** v5 is a major with breaking changes across pipelines/model-loading APIs that this app touches through wav2vec2 CTC, whisper pipelines, and qwen-tts. Staying on late-4.x is correct until a coordinated migration (own task, own testing). Worth watching for the last 4.x maintenance release and pinning to that if a security fix appears. |
| peft | 0.18.1 | 0.19.1 | (a) Leave. LoRA training works; bump opportunistically with the next transformers move. |
| python-multipart | 0.0.32 | 0.0.32 | Current. ✓ |
| aiofiles | 24.1.0 | 25.1.0 | (a) Leave. Trivial. |
| mutagen | **unpinned** | 1.48.1 | **Pinned `==1.47.0`** (the version actually installed/tested in `app/env`, deliberately not the newer 1.48.1). Follow-up findings while applying: mutagen is imported by **no app-env code at all** — its only consumer is the preparer's WAV tagging (`alexandria_preparer_rocm_compatible.py:2554`), which runs in the ROCm interpreter env, where mutagen was **missing** — WAV tagging has been silently skipped (guarded import). Fixed by installing `mutagen==1.47.0` into that env; the requirements comment now documents the real consumer. |

## TTS model — `install.js:53`

- **`qwen-tts==0.1.1` — current on PyPI as of today.** No (a)-type finding;
  the pin is the latest release.
- Is Qwen3-TTS still the strongest open option for multi-voice audiobook
  narration? As of my January 2026 knowledge: it was among the top open
  choices, with the notable competitors being **CosyVoice 3** (strong
  zero-shot cloning), **F5-TTS** (fast diffusion-style, weaker instruct
  control), **fish-speech/OpenAudio**, and Microsoft's **VibeVoice**
  (explicitly aimed at long-form multi-speaker audio — on paper the closest
  match to this app's use case). None is a clear-enough winner over
  Qwen3-TTS *for this pipeline* to justify abandoning the app's Qwen-tuned
  LoRA training, voice library, and instruct-annotation format — that's a
  product rebuild, not a dependency bump. **Leave; re-evaluate only if a
  concrete quality ceiling is hit** (e.g. LoRA voices plateauing below
  acceptable similarity). I cannot rule out a post-January-2026 model that
  changes this calculus — that check needs a fresh ecosystem scan, not a
  pin lookup.

## PyTorch stack — `torch.js`

- **ROCm path (`torch==2.10.0`/`torchaudio==2.10.0`/`triton-rocm==3.6.0`)**:
  leave. The pin is documented as benchmarked on RDNA4/RX 9070 XT (the
  actual target machine) and the file's own comments say why. Current
  stable torch is 2.13 (observed live today when an unconstrained install
  resolved `torch 2.13.0+cu130`), but a ROCm bump here risks exactly the
  GPU-detection/quality breakage the brief warns about, for no identified
  gain. Trigger to revisit: a ROCm 7.x driver/toolkit upgrade on the box,
  or a needed torchaudio feature.
- **CUDA path (`torch==2.7.0`, cu128, `xformers==0.0.30`)**: (a), three
  minors behind. Not this machine's path, so it's untested either way —
  upgrade it next time the CUDA path is touched for any reason, as a
  bundle (torch/torchvision/torchaudio/xformers must move together).
- **CPU fallback `torch==2.7.0`**: fine, matches CUDA-path era.
- **Intel-Mac `torch==2.2.2` (`torch.js:81`)**: **intentional, not stale.**
  The branch condition is `darwin && arch !== 'arm64'` — PyTorch dropped
  Intel-Mac (x86-64 macOS) wheels after the 2.2 series, so 2.2.2 is the
  *last version that exists* for that platform. Leave, and don't let a
  future cleanup "fix" it upward.

## Voice Lab audio-ML stack

- **Speaker embeddings — `speechbrain/spkrec-ecapa-voxceleb`
  (`voice_analysis.py:109`)**: leave. SpeechBrain itself is current-ish
  (1.1.0 on PyPI) and ECAPA-TDNN remains a standard, well-understood
  speaker-verification embedding. Newer alternatives (WeSpeaker ResNet
  models; NVIDIA TitaNet via NeMo) exist, but TitaNet/NeMo is CUDA-leaning
  (ROCm support uncertain — flagged per ground rules), and the job here
  (telling a handful of audiobook narrators apart, then complete-link
  clustering with manual overrides) is nowhere near the margin where
  embedding SOTA matters. (b)-type change not justified.
- **Diarization — `pyannote/speaker-diarization-3.1`**: **(a)+(b), with
  live evidence from today.** Findings from actually installing and
  running it this session:
  1. `pyannote.audio` is **not declared in any requirements file and not
     installed in any env on this machine** — `--diarize` could never have
     actually run here. It needs a documented home (preparer env docs or a
     constraints file).
  2. Current `pyannote.audio` is **4.0.7**, and v4 **silently redirects**
     the `speaker-diarization-3.1` model ID to the *differently-gated*
     `speaker-diarization-community-1` (observed live: 403 with a token
     that has 3.1 access). Licensing note: that's a second gated-access
     acceptance users would need.
  3. The code's `Pipeline.from_pretrained(..., token=)` kwarg is
     **4.x-only syntax**; pyannote 3.x needs `use_auth_token=` (observed
     live: TypeError, swallowed upstream as a generic "Diarization
     failed"). A compat fallback was added on the PR #197 branch.
  **Recommendation:** pin `pyannote.audio>=3.1,<4` wherever the preparer
  env gets built, now that the compat fix exists. Moving to 4.x +
  `community-1` (reportedly better DER, and it's where upstream
  development happens) is a legitimate *later* upgrade with its own
  gating/licensing step — trigger: next time the preparer env is rebuilt
  from scratch.
- **Forced alignment — wav2vec2-large-960h (pinned revision) + WhisperX**:
  leave both; the brief's "is wav2vec2 redundant with WhisperX" question
  inverts the actual architecture. Read from `choose_and_transcribe`:
  Wav2Vec2 is the **primary** GPU path (continuous CTC word timestamps),
  insanely-fast-whisper is fallback 2, WhisperX is the **last-resort CPU
  fallback** — three tiers, not two competing alignment paths. Removing
  the wav2vec2 path would delete the primary; removing WhisperX would
  delete the only CPU escape hatch. Also, WhisperX's own alignment stage
  internally uses wav2vec2-family CTC models, so the "2020-era model"
  concern applies to both paths equally — it remains the standard tool for
  CTC forced alignment. The pinned revision hash is good reproducibility
  practice; keep it.
- **ASR — vendored `insanely-fast-whisper-rocm/` fork**: keep the fork;
  "right tool, upstream is dead" rather than either of the brief's two
  labels. Upstream `insanely-fast-whisper` is still 0.0.15 (unchanged
  since ~2024, effectively unmaintained) and never gained ROCm support, so
  un-vendoring is not possible. The commonly-suggested replacement
  `faster-whisper` (1.2.1) runs on **CTranslate2, which has no ROCm
  backend** — explicitly not actionable for this app's target hardware.
  Since insanely-fast-whisper is essentially a transformers-pipeline
  wrapper and torch/transformers *do* support ROCm, the vendored fork is
  the correct shape for this stack. Distil-Whisper checkpoints (via the
  same transformers pipeline) would be a drop-in *speed* option if ASR
  throughput ever becomes the bottleneck — same tooling, smaller model.
- **Support libraries (`umap-learn` 0.5.12, `scipy`, `librosa`,
  `matplotlib`/`seaborn`)**: all mature and current-enough; nothing
  concrete stands out. Leave.

## Test-only — `requirements-test.txt`

`playwright>=1.40.0` (current 1.61.0) and `requests>=2.28.0`: open-ended
pins are acceptable for test-only deps, and both resolve to current
versions at install time. Leave. (If E2E flakiness ever appears after a
Playwright auto-upgrade, that's the moment to pin exact.)

---

## Overall

Two things worth doing now, both tiny: **pin `mutagen==1.48.1`** and **give
`pyannote.audio` a declared, pinned home (`>=3.1,<4`)** — the latter backed
by live breakage observed today rather than speculation. One thing worth
*not* doing by accident: drifting onto transformers v5. Everything else is
either current, deliberately pinned for a documented platform reason
(ROCm 2.10.0, Intel-Mac 2.2.2), or not worth the churn. The core
`qwen-tts==0.1.1` pin is the latest release; replacing the Qwen3-TTS model
family itself is a product decision outside dependency hygiene, and my
post-January-2026 visibility is limited to the live version checks above.
