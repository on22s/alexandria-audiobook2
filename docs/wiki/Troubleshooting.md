# Troubleshooting

Start with the logs. They almost always name the problem:

- the **activity line** under the Script tab's Generate button — what a run is waiting on, the
  attempt, and the time left;
- `logs/api/<task>-latest.log` — the full log of each background task (for example
  `logs/api/audio-latest.log`);
- `logs/review_responses.log` — every language-model request and reply, with the finish
  reason, token counts and time taken;
- `logs/api/start.js/latest` — the launcher's log; `logs/sessions/` groups related install,
  start and helper runs.

The README's [Troubleshooting](../../README.md#troubleshooting) section covers the launcher (Pinokio
not reaching "Open Web UI"). This page covers the rest.

## Script generation

### Generation fails or produces garbage

- Check that the language-model server is running and reachable at the **Base URL**; click
  **Test Connection** in Setup.
- Check that the **Model Name** matches the model the server has loaded (the refresh button
  lists them).
- Read the model's actual reply in `logs/review_responses.log`.
- Use a model from [Which model for your card](Which-Model-For-Your-Card.md), keep the JSON
  schema on, and set reasoning to *low*.

### Reasoning ("thinking") models

Reasoning models work well — better, in fact. Set **Reasoning effort** to *low* and let the
server cap the budget. **Do not** add `<think>` to the banned tokens: reasoning low measured
better than reasoning off on every model that reasons.

### A model loaded with too little context

A model that silently loaded with an 8k context (often because GPU memory was already in use)
fails on long requests and can be ten times slower.

- In LM Studio, tick **Optimize LM Studio settings** in Setup.
- In llama.cpp, pass the context size yourself; `-c 8192` is enough for this app.

### "Idle" for a long time

Read the activity line: it names the step, the unit, the attempt, and what it is waiting on (the
model, a rate-limit backoff, a retry).

- If it has said nothing for minutes, the server has stopped answering — test it from Setup.
- A run paused by **When retries run out → pause** waits for you to click Resume.

### Pause is greyed out

On Windows the app cannot suspend the worker process, so Pause is disabled there. Cancel and
**Resume failed run** still work.

### Character names are inconsistent

- The cast list is carried from request to request to keep names consistent.
- Use **Find Nicknames** and **Edit aliases** so different names for one person share a voice.
- You can fix names by hand in the Editor, or with the speaker repair in Saved Scripts.

### No API at all

Set **How requests are sent** to `manual`. The app writes each request to
`manual_llm/pending.json`, and the Script tab shows a panel where you copy the prompt and paste
the reply.

## Speech generation

### The model download fails or is very slow

- The TTS models (about 3.5 GB each) download from Hugging Face the first time you render.
  A download stuck at 0% is a network problem, not a frozen app.
- **China or restricted networks:** point downloads at a mirror before launching:

  ```
  HF_ENDPOINT=https://hf-mirror.com
  ```

  or add it to `start.js` in the `shell.run` parameters:
  `env: { HF_ENDPOINT: "https://hf-mirror.com" }`.
- **Rate limits (HTTP 429):** create a free [Hugging Face account](https://huggingface.co/join)
  and set `HF_TOKEN` to your access token.
- **A half-finished download:** delete the partial file in the Hugging Face cache and try
  again. `download_model.py` fetches the weights outside the app.

### The model fails to load

- Read the first traceback in `logs/api/audio-latest.log`.
- Make sure there is enough GPU memory (see the README's [GPU compatibility](../../README.md#gpu-compatibility)).
- As a last resort, set **Device** to `cpu` — much slower.
- On an AMD APU the app uses fp32 automatically; if you forced `bf16`, remove it.

### A clone voice doesn't work

- Check that the reference audio exists at the configured path.
- The reference text must be an exact transcript of the audio.
- Use clean audio, 5–15 seconds long; references under about 7 seconds give an unstable voice.
- Make sure the designed voice was **saved**, not just previewed.

### A LoRA adapter isn't found

- Check that training finished (Training tab).
- Adapter paths are relative to the project folder — check that `lora_models/<adapter_id>`
  exists.
- Refresh the adapter list in the Voices tab.

### A trained voice never stops talking

This is a defect in the trained voice (goal 2.3), usually from too high a learning rate or
overfitting. The stop check catches it before promotion. Use a shipped voice for that character
meanwhile, and retrain at a learning rate of 1e-6 (see the [Training guide](Training-Guide.md)).

### Voice Design glitches

- Avoid contradictions between the description and the instruction ("calm gentle voice" with
  "screaming furiously").
- Keep the description about the voice itself (age, gender, quality), and leave emotion and
  delivery to the instruction.

### External TTS server

- In `external` mode, make sure the Gradio TTS server is running at the configured URL.
- External mode renders one line per request; use `local` mode for batched rendering.

## Performance

### Rendering is slow

1. Click **Auto-Configure** in Setup.
2. Turn on **Compile Codec** — 3–4× faster after a one-time 30–60 s warm-up.
3. Turn on **Sub-batching** — less GPU time wasted on padding.
4. Raise **Parallel Workers** if memory allows.
5. A language model on the same card halves what the TTS can use. If yours is hosted
   elsewhere, untick **Runs on this machine's GPU**.

See [Batch generation](Batch-Generation.md).

### Out of memory

- Lower **Parallel Workers** and **Max Items/Batch**.
- Close other programs that use the GPU.
- The memory check refuses a batch it cannot fit, so running out of memory in the middle of a
  run usually means another process took the card after the check.
- LoRA training needs the GPU to itself; the app unloads its models automatically.
- Switching between voice types can briefly need extra memory while a model loads.

### The second batch hangs or fails

This is GPU memory fragmentation. The app clears memory between sub-batches to prevent it. If it
still happens, reduce the batch size or restart the app between large runs.

### MIOpen warnings (AMD)

MIOpen workspace warnings on AMD GPUs are handled automatically and do not affect quality or
speed.

## Audio output

### Broken or tiny MP3 files (428 bytes)

ffmpeg is missing or cannot encode MP3. The installer builds ffmpeg into the app's environment,
so this usually means the app is running outside `app/env`: start it from Pinokio, or activate
`app/env`. Every generated file is checked, and the Editor flags a broken chunk.

To check an ffmpeg by hand:

```bash
ffmpeg -encoders 2>/dev/null | grep mp3
```

### Audio quality

- Use clean 5–15 second references for cloning.
- Try a different seed for Custom voices (or leave it empty for random).
- Check the instructions: contradictory ones, or ones that describe the body rather than the
  voice, lower quality. Words that describe the voice itself ("deep", "raspy") fight the chosen
  voice; the identity anchor removes them.
- For LoRA voices, make sure the training data had emotional variety.

### Garbled characters in the text

- Upload repair fixes common encoding problems.
- If a book still shows `â€™`, run the repair preview from Saved Scripts.
- Make sure input files are UTF-8.
- Pictographic kana and other unspeakable characters are removed before the TTS on purpose.

## Training

### The loss stays high (above 10)

- Check that the WAV files are valid, not corrupted.
- Check that `metadata.jsonl` has the right transcript for each clip.
- Check that the reference audio exists and is clean.
- **Do not raise the learning rate** to force the loss down: at 5e-6 voices never stop talking.

### The voice sounds flat after training

- The training data lacks emotional variety — the most common cause.
- Rebuild the dataset with varied emotion and text pairs (happy, angry, sad, whispered,
  shouted).
- Try a lower LoRA rank (8–16) to keep more of the base model's expressiveness.

### Training crashes or hangs

- Training needs the GPU to itself; the app should unload its models first.
- Read the first CUDA or ROCm error in the log.
- Try a smaller batch size or fewer gradient-accumulation steps.

## Windows

### File-locking errors

Windows can lock JSON files during parallel reads and writes. The app writes files atomically
and retries with backoff. If errors continue:

- lower **Parallel Workers**;
- close editors or file explorers that have project files open.

## Checking your installation

```bash
cd app
python run_isolated_api_tests.py          # quick — every endpoint, on a throwaway copy of the app
python run_isolated_api_tests.py --full   # also generation — needs a GPU and an LLM
```

It starts its own copy of the app with an empty data folder, so your books and voices are not
touched. If quick mode passes but generation fails, the problem is loading the TTS model or
reaching the language model, not the app itself. See [Home](Home.md#checking-an-installation)
for testing an app that is already running.
