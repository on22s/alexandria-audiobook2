# Batch Generation

Alexandria's batch generation system groups chunks by voice type and processes them efficiently using the Qwen3-TTS native batch API.

## Render Modes

### Standard (Render Pending)
Sends individual TTS calls in parallel using the configured worker count. Each chunk is generated independently.

- Per-speaker seeds for reproducible output
- Works with all voice types
- Throughput: GPU dependent, ~0.85 on 7900XTX

### Batch (Fast)
Sends multiple lines to the TTS engine in a single batched call. Chunks are grouped by voice type and processed with type-specific batching strategies.

- 3-6x better throughput compared to single 
- Sub-batching by text length to minimize padding waste
- Single batch seed (set in Setup, or empty for random)

## Processing Order

Batch generation processes voice types in this order:

1. **Custom voices** — Batched natively (fastest)
2. **Clone voices** — Batched by speaker
3. **LoRA voices** — Batched by adapter
4. **Voice Design** — Sequential (each line has a unique voice)

This ordering ensures the most efficient types (with the most chunks) process first.

## How Batching Works

### Custom Voices
All custom voice chunks are collected, grouped by speaker, sorted by text length, and split into sub-batches. Each sub-batch is sent as a single call to `generate()` with a list of texts.

### Clone Voices
Chunks are grouped by **speaker name** (each speaker has different reference audio). Within each speaker group, texts are sorted by length and sub-batched. The voice clone prompt is built once per speaker and cached.

### LoRA Voices
Chunks are grouped by **adapter path** (each adapter requires different model weights). Within each adapter group, texts are sorted by length and sub-batched. Per-chunk instruct+character_style is built into `instruct_ids` lists.

### Voice Design
Each line may have a different voice description, so batching isn't practical. Design chunks process one at a time after all batched types complete.

## Sub-batching

Sub-batching splits a batch into smaller groups of similarly-sized texts to reduce wasted GPU compute on padding. The Qwen3-TTS autoregressive decoder generates to the length of the longest text in a batch — shorter texts waste compute on padding tokens.

**How it works:**
1. Sort all chunks by text length (shortest first)
2. Walk through sorted chunks, accumulating a group
3. Split when: `longest_text > ratio * shortest_text AND group_size >= min_size`
4. Each resulting sub-batch processes as a separate call

**Settings (Setup tab):**
| Setting | Default | Description |
|---------|---------|-------------|
| Sub-batching | Enabled | Toggle sub-batch splitting |
| Min Sub-batch Size | 4 | Don't split groups smaller than this |
| Length Ratio | 5 | Max longest/shortest ratio before splitting |

**Example:** With ratio=5, a batch containing texts of 10, 15, 20, 50, 55, 200 characters would split into groups like [10, 15, 20] and [50, 55] and [200], because 200 > 5 * 10.

## Performance Tuning

### Recommended Settings

| Setting | Value | Notes |
|---------|-------|-------|
| TTS Mode | `local` | Built-in engine required for batching |
| Compile Codec | `true` | 3-4x faster decoding after one-time warmup (~30-60s) |
| Parallel Workers | 20-60 | Batch size — higher = more throughput, more VRAM |
| Render Mode | Batch (Fast) | Activates batched TTS calls |
| Sub-batching | Enabled | Reduces padding waste |

### Benchmarks

Tested on AMD RX 7900 XTX (24 GB VRAM, ROCm 6.3):

| Configuration | Throughput |
|--------------|------------|
| Standard mode (sequential) | ~1x real-time |
| Batch mode, no codec compile | ~2x real-time |
| Batch mode + compile_codec | **3-6x real-time** |
| LoRA batch (sequential before) | ~0.5 RTF |
| LoRA batch (batched) | **~5.5 RTF** |

A 273-chunk audiobook (~54 minutes of audio) generates in approximately 16 minutes with batch mode and codec compilation.

### VRAM Management

- `gc.collect()` + `torch.cuda.empty_cache()` runs between sub-batches to prevent VRAM fragmentation
- If you encounter OOM errors, reduce **Parallel Workers**
- Mixed voice types in the same batch are processed separately — the total VRAM usage equals the peak of any single voice type group, not the sum

### Codec Compilation

When **Compile Codec** is enabled, `torch.compile` optimizes the codec decoder on first use. This adds ~30-60 seconds of warmup but provides 3-4x faster decoding for all subsequent generations in the session.

The compilation persists for the session — it doesn't re-compile on each batch, only on first use after app start.

## ROCm (AMD GPU) Notes

Alexandria automatically applies ROCm-specific optimizations:
- **MIOpen fast-find mode** — Prevents workspace allocation failures
- **Triton AMD flash attention** — Enables native flash attention for the whisper encoder
- **triton_key compatibility shim** — Fixes `torch.compile` on pytorch-triton-rocm

These are applied transparently and require no configuration.
