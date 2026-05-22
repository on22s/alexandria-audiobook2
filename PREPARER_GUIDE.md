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
