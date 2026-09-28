# Troubleshooting

## Script Generation

### Generation fails or produces garbage
- Check LLM server is running and accessible at the configured URL
- Verify the model name matches what's loaded in your LLM server
- Try a different model — some struggle with structured JSON output
- Check Pinokio logs for error details

### Thinking model interference
Models like DeepSeek-R1, GLM4-air produce `<think>` blocks that corrupt JSON output. Add `<think>` to **Banned Tokens** in Setup to disable thinking mode.

### Characters inconsistently named
- The LLM sees the character roster between chunks for consistency
- If names still drift, try a larger/better model or reduce chunk size
- You can fix names manually in the Editor tab

## TTS Generation

### Model download fails or is very slow
- TTS models (~3.5 GB each) are downloaded from Hugging Face on first use
- **China / restricted networks:** If Hugging Face is slow or blocked, set the `HF_ENDPOINT` environment variable before launching:
  ```
  HF_ENDPOINT=https://hf-mirror.com
  ```
  Or add it to `start.js` in the `shell.run` params: `env: { HF_ENDPOINT: "https://hf-mirror.com" }`
- **Rate limits:** If you see 429 errors, create a free [Hugging Face account](https://huggingface.co/join) and set `HF_TOKEN` to your access token
- Downloads resume automatically if interrupted — just restart the app

### Model fails to load
- Check Pinokio terminal for specific error messages
- Ensure sufficient VRAM (16+ GB recommended for bfloat16)
- Try `device: cpu` as a fallback (significantly slower)
- Model weights download automatically (~3.5 GB) — check for download failures

### Voice clone not working
- Verify reference audio exists at the configured path
- Reference text must be an accurate transcript of the audio
- Use clean, clear 5-15 second audio without background noise
- Check that the designed voice was saved (not just previewed)

### LoRA adapter not found
- Ensure the adapter was trained successfully (check Training tab)
- Adapter paths are relative to project root — check `lora_models/<adapter_id>` exists
- Try refreshing the adapter list in the Voices tab

### Voice Design audio glitches
- Avoid contradictions between base description and instruct (e.g., "calm gentle voice" + "screaming furiously")
- Keep the base description focused on voice identity (age, gender, quality)
- Let the instruct handle emotion and delivery
- Adjust the LLM system prompt to generate compatible instruct directions

### External TTS server
- For external mode, ensure the Gradio TTS server is running at the configured URL
- External mode doesn't support batched generation — use local mode for best performance

## Performance

### Slow batch generation
1. Enable **Compile Codec** in Setup (3-4x faster after one-time warmup)
2. Increase **Parallel Workers** if VRAM allows
3. Use **Batch (Fast)** render mode
4. Enable **Sub-batching** (reduces wasted GPU compute on padding)

### Out of memory (OOM)
- Reduce **Parallel Workers** (batch size)
- Close other GPU-intensive applications
- LoRA training requires exclusive GPU access — the app unloads models automatically
- Switching between different voice types may briefly require extra VRAM during model loading

### VRAM fragmentation
If the second batch in a session hangs or fails, this is VRAM fragmentation. The app runs `gc.collect()` + `torch.cuda.empty_cache()` between sub-batches to prevent this. If it persists:
- Reduce batch size
- Restart the app between large batch runs

### MIOpen warnings (AMD)
MIOpen workspace allocation warnings on AMD GPUs are handled automatically. These don't affect output quality or speed.

## Audio Output

### Broken or tiny MP3 files (428 bytes)
Conda's bundled ffmpeg on Windows often lacks the MP3 encoder (libmp3lame). Alexandria detects this and falls back to WAV, but for MP3 output:
```bash
conda install -c conda-forge ffmpeg
# Or use system ffmpeg:
conda remove ffmpeg
# Verify:
ffmpeg -encoders 2>/dev/null | grep mp3
```

### Audio quality issues
- Use 5-15 second clean reference audio for cloning
- Try different seeds for custom voices (or set empty for random)
- Check instruct directions — contradictory or physically-described instructs can degrade quality
- For LoRA voices, ensure training data had emotional variety

### Mojibake characters
- The system auto-fixes common encoding issues
- Ensure input text is UTF-8 encoded

## Training

### Loss stays high (>10)
- Verify audio files are valid WAV (not corrupted)
- Check metadata.jsonl has correct transcripts matching the audio
- Try increasing learning rate slightly (e.g., 1e-5)
- Ensure ref_audio exists and is clean

### Voice sounds flat/monotone after training
- Training data lacks emotional variety — the most common issue
- Regenerate dataset with diverse emotion+text pairs (happy, angry, sad, whisper, shout)
- Try lower LoRA rank (8-16) to preserve base model expressiveness

### Training crashes or hangs
- Check VRAM — training needs exclusive GPU access
- The app should unload models automatically before training starts
- Check Pinokio terminal for CUDA/ROCm errors
- Try reducing batch size or gradient accumulation steps

## Windows-Specific

### File locking errors during generation
Windows can lock JSON files during parallel read/write. Alexandria uses atomic writes with exponential backoff retry to handle this automatically. If you still see file access errors:
- Reduce parallel workers
- Ensure no external programs (editors, file explorers) have project files open

## Verifying Your Installation

Run the automated test suite to confirm everything is working:

```bash
cd app
python test_api.py          # Quick — tests all endpoints without TTS/LLM
python test_api.py --full   # Full — also tests generation (needs TTS + LLM running)
```

Quick mode validates server connectivity, config persistence, file upload, scripts CRUD, voice configuration, chunk editing, status polling, and error handling — all without loading models. If quick tests pass but generation fails, the issue is likely with TTS model loading or LLM connectivity rather than the app itself.

## Logs

Check Pinokio logs for detailed error information:

```
logs/
├── api/     # Launcher script logs (install.js, start.js)
├── dev/     # AI coding tool logs
└── shell/   # Direct terminal interaction logs
```

The `latest` file in each directory contains the most recent session. Use timestamped files for historical analysis.
