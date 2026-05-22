# Alexandria Preparer Guide

Process a single audiobook into an annotated TTS training dataset with one command.

## Overview

The preparer takes an audio file (audiobook) and produces a ZIP archive containing:
- Word-aligned audio segments (`sample_NNNN.wav` at 24 kHz)
- `metadata.jsonl` with text annotations, durations, and time ranges
- Ready for use as a TTS training dataset

**Key Features:**
- **Wav2Vec2 ASR (primary)** with true CTC-aligned word timestamps
- Insanely Fast Whisper / WhisperX-CPU fallbacks
- Gemma 4 LLM annotations on GPU
- **Source-guided chunking** (`--source`): align ASR against an EPUB/TXT, use source spelling for chunk text, drop audio-only material
- **Pause-aware chunk boundaries**: chunks end at sentence punctuation or natural narrator pauses in the last 30% of each chunk, not wherever the duration threshold landed
- Resume capability — never lose work from a crash
- Periodic checkpointing every 50 segments
- Dynamic ETA based on actual measured throughput
- Single-pass audio loading (resample in memory)
- 24 kHz audio spilled to disk during annotation (~1.8 GB RAM saved on long audiobooks)
- GPU memory + utilization logging via `rocm-smi`

## Recommended Models

The preparer uses an LLM to annotate text chunks for TTS. Recommended for AMD 16 GB+ GPUs:

| Role | Model | VRAM (Q6_K) | Why |
|---|---|---|---|
| **Primary** | `Qwen2.5-14B-Instruct-Q6_K.gguf` | ~12 GB | Strong instruction following, terse structured output |
| **Fallback** | `Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf` | ~5 GB | Backup if Qwen fails to load |

## Usage

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

## Command Line Options

### Required
- `--audio PATH` — Input audio file (.wav, .mp3, .m4a, .flac, .ogg)
- `--model PATH` — Primary GGUF model path (omit only with `--skip-annotation`)

### Optional
- `--fallback-model PATH` — Backup GGUF model used if `--model` fails to load
- `--chunk-size SECONDS` — Target duration per chunk in seconds (default: `10.0`)
- `--lang CODE` — Language code for transcription (default: `en`)
- `--output PATH` — Output ZIP path (default: `alexandria_dataset.zip`)
- `--resume` — Resume from existing `dataset_temp/` instead of starting over
- `--skip-annotation` — Stop after transcription (not fully implemented)

### Source-guided chunking (optional)
Pass an EPUB or text file matching the audiobook to fix ASR mistranscriptions
at source and drop audio-only material before the LLM step.
- `--source PATH` — EPUB or TXT of the same book the audio narrates
- `--source-threshold N` — Minimum alignment ratio to keep a chunk (default: `0.65`)
- `--keep-unaligned` — When a chunk falls below `--source-threshold`, write the
  ASR text instead of dropping (default: drop)
- `--source-start N` — Start source alignment at word N (skip auto-anchor)
- `--source-start-text TEXT` — Fuzzy-search source for TEXT and start there
- `--no-auto-anchor` — Disable automatic source-anchor detection (start at word 0)

## Examples

### Standard run (Qwen primary, Gemma fallback)
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
```

### Custom output path
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio book1.wav \
  --model model.gguf \
  --output datasets/book1.zip
```

### Pause and resume (e.g., to game or free up GPU)

Press **Ctrl+C** at any time to stop the script. Your progress is saved automatically:

```
⚠ Process interrupted by user
Partial results preserved in dataset_temp/ - rerun with --resume to continue
```

When you're ready to continue, rerun with the same arguments plus `--resume`:

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --resume
```

The preparer picks up exactly where it left off — no repeated work.

### Resume after crash
If the script crashes 50 hours into a 60-hour annotation, the recovery is identical — rerun with `--resume`:

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --resume
```

The preparer will:
1. Read existing `dataset_temp/metadata.jsonl`
2. Find the last completed segment and its end time
3. Skip already-processed audio
4. Continue with new segments from that point

### Different language
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio spanish_book.wav \
  --model model.gguf \
  --lang es
```

### Larger chunks
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --chunk-size 15.0
```

## Source-Guided Chunking

When you pass `--source <epub-or-txt>`, the preparer fuzzy-aligns each ASR
chunk against the source text and either:

- **Replaces** the chunk's ASR text with the source's spelling when the
  alignment ratio meets `--source-threshold` (default 0.65). The LLM then
  annotates already-correct text. Character names, dialect spellings, and
  punctuation come out exactly as the EPUB has them.
- **Drops** the chunk entirely when the alignment is below threshold. No
  WAV file, no JSONL entry. This is what strips audio-only material:
  narrator intros, "this audiobook is a work of fiction" credits, chapter
  announcements that aren't in the text, etc.

If you'd rather keep low-confidence chunks (using their ASR text), pass
`--keep-unaligned`. Cursor doesn't advance on those, so the next chunk
still tries to align from where the prose was last known to match.

### Example

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub"
```

### What you'll see in the logs

At startup, the preparer reports source loading + lexicon build:
```
▶ Loading source for guided chunking: /path/to/book.epub
  ├─ Source: 284,125 characters
  ├─ 29 recurring proper nouns (gifted, godou, grotesqueries, hana, kanoko, kaya +23 more)
  ├─ 49,686 source words
  └─ Auto-anchor: entry 3 → source word 287 (62.1% match)
```

Per dropped chunk during annotation:
```
↪ DROPPED chunk at 421.36s (source ratio 0.42 < 0.65)
```

Per low-confidence kept chunk (only with `--keep-unaligned`):
```
↪ chunk 42 kept (ASR text); source ratio 0.51 < 0.65
```

### Wrong-source-file detection

Before LLM annotation starts, the preparer pre-scans the first ~30 ASR
chunks against the source. If the average alignment is below 50% or more
than 40% of chunks fall below 60%, the preparer **aborts with an error**
rather than waste hours of LLM time on a mismatched source:

```
⚠ Source/audio divergence too high to proceed:
  Sampled 30 chunks — avg alignment 38%, 21 (70%) below 60%.
  Usually means a wrong edition or different translation.
  Re-run without --source, or pass --keep-unaligned to accept the ASR
  text for low-confidence chunks.
```

This typically means you've supplied the wrong EPUB (different
translation, abridged edition, or a different book entirely). Re-anchor
manually with `--source-start N` / `--source-start-text "..."`, swap to
the right source, or run without `--source` to fall back to the legacy
ASR-only flow.

### When to use vs. skip

**Use `--source` when:** you have the matching EPUB/TXT for the
audiobook AND you want clean character names + dialect spellings in the
training dataset AND you're okay dropping chunks that don't appear in
the source (chapter intros, narrator notes, paratext).

**Skip `--source` when:** you don't have the source text, or you're
processing audio that's known to be loosely based on the source
(adaptations, rewrites, abridgments) where strict dropping would lose
content you want to keep.

### Effect on compare-review downstream

Source-guided mode dramatically reduces what you'll see when reviewing
the resulting JSONL with `alexandria_compare.py`. Most of the edits
users typically make (character-name ASR fixes, dialect spelling fixes)
have already been applied at preparation time. Compare-review becomes a
prosody sanity check — usually a few minutes per book instead of an
hour.

## Pause-Aware Chunk Boundaries

The chunker emits a chunk when the accumulated duration reaches
`--chunk-size`, but it doesn't cut at exactly that word. It looks back
across the last 30% of the chunk's duration and prefers to cut at:

1. A word ending in sentence punctuation (`.`, `!`, `?`) — best for TTS
   coherence; picks the LATEST one in the window.
2. The longest natural pause (gap ≥ 0.25s between ASR word end + next
   word start) in the same window.
3. The final word (current word at the threshold) — only when neither
   of the above is available.

Words trimmed off by the look-back become the seed of the next chunk;
no audio is lost. Net effect: chunks end on breath/clause/sentence
breaks instead of mid-phrase. Combined with `--source` mode, the
resulting WAVs are usable for TTS training with minimal hand-editing.

## Processing Flow

1. **Validate inputs** — Check audio file and model exist
2. **Load audio** — Single read at native sample rate, resample to 16 kHz (for ASR) and 24 kHz (for output)
3. **Spill 24 kHz to disk** — Write `.alexandria_audio_24k.wav` and free RAM
4. **Transcribe audio** (Wav2Vec2 → IFW → WhisperX-CPU):
   - Wav2Vec2 processes audio in 30 s chunks with 3 s overlap
   - Each chunk decoded with `output_word_offsets=True` for true CTC-frame alignment
   - Overlap region words deduplicated by word-center ownership
5. **Annotate chunks**:
   - Load Gemma 4 model fully to GPU (`n_gpu_layers=-1`)
   - Walk word segments, accumulate into ~10 s chunks
   - Generate TTS annotation per chunk with 3-segment context window
   - Write each `sample_NNNN.wav` and append to `metadata.jsonl` immediately (crash-safe)
   - Log dynamic ETA every 10 segments
6. **Create output dataset** — Zip `dataset_temp/` contents into output path
7. **Cleanup** — Remove scratch audio + temp dir (only on success)

## Resume Capability

The preparer writes each segment's metadata to `dataset_temp/metadata.jsonl` **immediately** after processing (not buffered to end). Each entry includes:

```json
{
  "audio_filepath": "sample_0042.wav",
  "text": "annotated text...",
  "duration": 10.04,
  "start": 421.36,
  "end": 431.40
}
```

On `--resume`:
- Existing entries are loaded
- The highest `end` time becomes the resume point
- Words with `start < resume_time` are skipped
- New entries are appended (existing entries untouched)
- The last 5 entries' text becomes the LLM context

**On intentional pause (Ctrl+C) or crash**: `dataset_temp/` is preserved (not deleted). Rerun with `--resume` and the same `--audio` and `--model` arguments to continue from where you stopped.

**On success**: `dataset_temp/` is deleted after the zip is created.

### Source-file safety check

A `.source` marker is written into `dataset_temp/` containing the absolute path of the audio file being processed. On `--resume`:
- If the marker matches the current `--audio` → resume safely.
- If the marker is missing or points to a different file → **wipe `dataset_temp/` and start fresh** (logged as a warning).

This prevents accidental corruption when reusing the working directory for different audiobooks. The batch processor relies on this guarantee to safely pass `--resume` on every retry.

## GPU Monitoring

The preparer logs GPU stats at key points:
- After Wav2Vec2 model load
- Before/during chunk processing (every 50 chunks)
- After Gemma model load
- During annotation (every 10 segments)

Each log entry shows:
```
GPU Usage (chunk 50/725):
  ├─ Memory: 4.52 GB / 17.10 GB (26.4%)
  └─ Utilization: 87.5%
```

Memory is from `torch.cuda.memory_allocated()`. Utilization is from `rocm-smi --showuse --json` (AMD GPUs). Note: when Gemma is the only GPU consumer, `torch.cuda.memory_allocated()` reports 0 because llama-cpp-python manages GPU memory outside PyTorch — check `rocm-smi` for the real picture.

## Progress Tracking

### Wav2Vec2 phase
```
↳ Chunk 50/725 | avg 0.51s/chunk | ETA 5m 47s
```

### Annotation phase
```
↳ Progress: 50/4713 chunks | Avg: 46.2s/chunk | Elapsed: 38m 42s | ETA: 59h 31m
```

Time format adapts: `Nh Mm` if > 1 hour, `Mm Ss` if > 1 min, `Ns` otherwise.

## Logs

Detailed logs are written to `logs/alexandria_preparer_YYYYMMDD_HHMMSS.log`:
- Full DEBUG-level entries for every step
- GPU stat snapshots
- Subprocess command lines and outputs
- Exception tracebacks (if any)

Tail the log in another terminal:
```bash
tail -f logs/alexandria_preparer_*.log
```

## ASR Method Priority

| # | Method | Device | Context | Word Timestamps |
|---|---|---|---|---|
| 1 | **Wav2Vec2** (primary) | GPU | 30 s chunks + 3 s overlap | True (CTC frame alignment) |
| 2 | Insanely Fast Whisper | GPU | 30 s fixed chunks | Native |
| 3 | WhisperX-CPU | CPU | n/a | Aligned via forced alignment |

Wav2Vec2 is selected first because of its overlapping-chunk context preservation and CTC-aligned per-word timestamps. Falls back if not installed or if the run errors out.

## Performance Tips

- **GPU memory** — Gemma uses ~8 GB; Wav2Vec2 uses ~2 GB. Both fit comfortably on a 16 GB+ GPU.
- **RAM** — Spilling 24 kHz audio to disk saves ~1.8 GB during the multi-hour annotation phase.
- **Disk space** — Allow ~5 – 10 GB free per audiobook (scratch WAV + segment WAVs + final zip).
- **Run time** — On AMD Radeon RX 9070 XT, expect ~6 min Wav2Vec2 + ~60 h Gemma annotation for a ~5.4 h audiobook with 10 s chunks.

## Troubleshooting

### Audio file not found
```
Error: Audio file not found: audiobook.wav
```
- Check path is correct; the script's CWD is wherever you launched it from.

### Model file not found
```
Error: Model file not found: ...
```
- Download the Gemma GGUF model (e.g., `download_model.py`) or supply `--model PATH`.

### `rocm-smi unavailable`
- Verify `/opt/rocm/bin/rocm-smi` exists and is executable.
- GPU memory still works without it; only utilization % is unavailable.

### `expandable_segments not supported`
- Informational warning from PyTorch — AMD ROCm doesn't support that memory optimization. PyTorch falls back to standard allocation. No action needed.

### Resume picked the wrong file
- The preparer tracks the source via `dataset_temp/.source` marker. Mismatched markers cause `dataset_temp/` to be wiped automatically (with a warning) before fresh processing.
- If you want to force a fresh start regardless, delete `dataset_temp/` manually or omit `--resume`.

### Out of disk space
- Check `du -sh dataset_temp/ .alexandria_audio_24k.wav`.
- Free up space, then rerun with `--resume`.

## API (programmatic invocation)

### Python (subprocess)
```python
import subprocess

result = subprocess.run([
    "python", "alexandria_preparer_rocm_compatible.py",
    "--audio", "audiobook.wav",
    "--model", "Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf",
    "--chunk-size", "10.0",
    "--lang", "en",
    "--output", "my_dataset.zip",
], check=True)
```

### JavaScript (Node.js)
```javascript
const { execFile } = require("child_process");

execFile("python", [
  "alexandria_preparer_rocm_compatible.py",
  "--audio", "audiobook.wav",
  "--model", "model.gguf",
  "--output", "my_dataset.zip",
], { maxBuffer: 1024 * 1024 * 100 }, (err, stdout, stderr) => {
  if (err) throw err;
  console.log(stdout);
});
```

### Curl (not applicable)
The preparer is a CLI tool, not an HTTP service. Wrap it in a Flask/FastAPI server if you need network access.

## See Also

- `BATCH_PROCESSOR_GUIDE.md` — Process multiple audiobooks sequentially with resume across files
- `README.md` — Project overview
