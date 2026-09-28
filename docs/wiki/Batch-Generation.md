# Batch generation

How the app renders many lines at once, and how to tune it.

## Which path your render takes

| TTS mode (Setup) | what **Render Pending** / **Regenerate All** do |
|---|---|
| `local` (the built-in engine) | **batched** rendering: many lines per call to Qwen3-TTS, grouped by voice type |
| `external` (a Gradio server or server pool) | one call per line, sent in parallel |

Batched rendering is the fast path: about 3–6× real time on a mid-range card. It uses one
batch seed (set in Setup, or empty for random). The per-line path uses per-speaker seeds and
works with every voice type, but is slower.

## The order voice types are rendered

1. **Custom voices** — batched natively (fastest).
2. **Clone voices** — batched per speaker.
3. **LoRA voices** — batched per adapter.
4. **Voice Design** — one line at a time, because each line may have a different description.

The types with the most lines usually go first, so most of the book finishes early.

## How each type is batched

- **Custom** — all Custom lines are collected, grouped by speaker, sorted by length, and split
  into sub-batches. Each sub-batch is one call with a list of texts.
- **Clone** — lines are grouped by **speaker**, since each speaker has its own reference audio.
  The voice prompt is built once per speaker and cached.
- **LoRA** — lines are grouped by **adapter**, since each adapter has its own weights. Each
  line's instruction and character style travel with it.
- **Voice Design** — rendered one by one after all batched types finish.

## Sub-batching

The speech model keeps generating until the longest text in a batch is done, so shorter texts
in the same batch waste GPU time on padding. Sub-batching groups texts of similar length to
cut that waste.

How it works:

1. Sort the lines by text length, shortest first.
2. Walk through them, collecting a group.
3. Start a new group when the longest text is more than *ratio* × the shortest **and** the
   group already has at least *min size* lines.
4. Each group becomes its own call.

| setting (Setup) | default | meaning |
|---|---|---|
| Sub-batching | on | split batches by length |
| Min Sub-batch Size | 4 | never split a group smaller than this |
| Length Ratio | 5 | the largest allowed longest-to-shortest ratio |

**Example:** with ratio 5, texts of 10, 15, 20, 50, 55 and 200 characters split into [10, 15,
20], [50, 55] and [200], because 200 is more than 5 × 10.

## Tuning

**Click Auto-Configure first.** It reads your GPU memory and sets the batch settings from
measurements on an RX 9070 XT (the model itself takes about 4.2 GB; 4-, 8- and 16-line batches
peak at about 7.3, 9.6 and 11.8 GB).

| setting | what it does |
|---|---|
| TTS Mode | `local` is needed for batched rendering |
| Parallel Workers | the batch size for local rendering; Auto-Configure sets 1–4 depending on GPU memory |
| Max Items/Batch | a cap on lines per batch |
| Compile Codec | 3–4× faster decoding after a one-time 30–60 s warm-up |
| Sub-batching | on — reduces padding waste |

Upstream Alexandria measured larger worker counts (20–60) on a 24 GB RX 7900 XTX. Those
numbers were not re-measured in this fork; start from Auto-Configure and raise the value only
if memory allows.

### Upstream benchmarks (RX 7900 XTX, 24 GB, ROCm 6.3)

| configuration | speed |
|---|---|
| one line at a time | about 1× real time |
| batched, no codec compile | about 2× real time |
| batched with codec compile | **3–6× real time** |
| LoRA, one line at a time | about 0.5× real time |
| LoRA, batched | **about 5.5× real time** |

A 273-line audiobook (about 54 minutes of audio) rendered in about 16 minutes with batching and
codec compilation.

## GPU memory

- Between sub-batches the app runs `gc.collect()` and `torch.cuda.empty_cache()` to prevent
  memory fragmentation.
- Before a batch starts, a memory-headroom check refuses a batch that would not fit.
- Voice types are rendered separately, so peak memory is that of the largest single group,
  not the sum.
- If you run out of memory, lower **Parallel Workers** and **Max Items/Batch**.
- A language model on the same card takes memory from the TTS. If yours is hosted elsewhere,
  untick **Runs on this machine's GPU** in Setup.

## Codec compilation

With **Compile Codec** on, `torch.compile` optimises the codec decoder the first time it is
used. That adds 30–60 seconds once, then decoding is 3–4× faster for the rest of the session.
It does not recompile for each batch — only after the app restarts.

## AMD (ROCm) notes

The app applies ROCm-specific fixes automatically; you do not need to configure anything:

- **MIOpen fast-find mode** — prevents workspace allocation failures.
- **Triton AMD flash attention** — enables flash attention for the Whisper encoder.
- **A `triton_key` compatibility shim** — fixes `torch.compile` on pytorch-triton-rocm.
- **APUs** (660M/680M/780M class) run the TTS in fp32, because bf16 is broken there.
