# Training Guide

Train LoRA adapters on the Qwen3-TTS Base model to create custom voice identities. Trained voices respond to instruct directions for emotion and delivery while maintaining a consistent voice character.

## Quick Reference

| Dataset Size | Epochs | Learning Rate | LoRA r | LoRA Alpha | Grad Accum | Target Loss |
|-------------|--------|---------------|--------|------------|------------|-------------|
| ~30 samples | 10-15 | 5e-6 | 32 | 128 | 4 | 4.1-4.2 |
| ~60 samples | 5-10 | 1e-6 | 32 | 128 | 4 | 4.1-4.2 |
| ~120 samples | 3 | 2e-6 | 64 | 128 | 4 | 4.1-4.2 |

**Target loss: 4.1-4.2** — this is the sweet spot for voice identity + instruct following + clean audio. Loss 4.1 is the floor; below this, garbling becomes increasingly likely. Note that identical settings can produce slightly different losses between runs, so aim for 4.15-4.2 for a reliable margin.

## Key Principles

- **More data = fewer epochs.** Each epoch teaches more with a larger dataset, so fewer passes are needed before overfitting.
- **Total exposure matters.** Samples x epochs should land around 250-400 total forward passes. Going above 600 risks overfitting.
- **Loss below 4.1 = garble risk.** Run-to-run variance means the same config can land on either side of 4.1. Below 3.5, output is consistently garbled or fails to reach EOS.
- **Loss above 4.5 = undertrained.** Clear audio but weak voice identity and faint instruct following.

## Overview

Several built-in LoRA presets are included out of the box and appear alongside your trained adapters — no training required to start using LoRA voices.

The training pipeline for custom voices:
1. **Create a dataset** — Voice samples (WAV) with transcripts
2. **Configure training** — Set hyperparameters
3. **Train** — Runs as a subprocess (GPU exclusive, main app unloads models)
4. **Test** — Preview the trained voice
5. **Assign** — Use the adapter in the Voices tab

## Creating Datasets

### Method 1: Voice Designer (Synthetic Data)

Generate training samples directly from a text description. Best for creating voices that don't exist in real recordings.

1. Go to the **Training** tab
2. Fill in the **Generate Dataset** form:
   - **Dataset Name** — Identifier (e.g., `gruff_soldier`)
   - **Root Voice Description** — Base voice identity (e.g., "A gruff middle-aged male soldier with a commanding baritone")
   - **Samples** — Emotion + text pairs

3. Add rows with varied emotions for expressive training:

| Emotion | Example Text |
|---------|-------------|
| *(empty — neutral)* | The patrol route has been secured and all positions are accounted for. |
| Barking orders sharply | Move out! I want that perimeter locked down in sixty seconds! |
| Quiet, tense warning | Keep your voice down. There's movement in the treeline, fifty meters out. |
| Exhausted, catching breath | We've been marching for twelve hours. The men need rest or we'll lose them. |
| Bitter, restrained anger | They sent us in without support. Command knew exactly what they were doing. |
| Gentle, reassuring | Easy now, son. You did good out there. We're going to get you home. |

When saving the dataset, use the **Reference Sample** dropdown to select which sample becomes `ref.wav` for speaker embedding during training. Choose a clear, representative line.

**Tips for synthetic datasets:**
- 6-25 samples is a good range
- Emotional variety is critical — neutral-only data produces flat voices that resist instruct prompting
- The root description defines the voice identity; emotions in each row control delivery style
- The combined prompt (root + emotion) is sent to the VoiceDesign model to generate each sample
- Include short utterances ("Oh!", "Hmm.", "Right.") — helps the model learn EOS behavior on short inputs
- End with a neutral passage — a long, calm, descriptive paragraph makes an ideal reference sample
- Use consistent seed for the reference sample to keep the speaker embedding stable across regenerations

### Method 2: Upload Real Audio (ZIP)

Upload a ZIP file containing:
```
dataset.zip
├── sample_001.wav
├── sample_002.wav
├── ...
├── ref.wav            # Reference audio for speaker embedding
└── metadata.jsonl     # One JSON object per line
```

**metadata.jsonl format:**
```json
{"audio_filepath": "sample_001.wav", "text": "The transcript of this sample.", "ref_audio": "ref.wav"}
{"audio_filepath": "sample_002.wav", "text": "Another transcript.", "ref_audio": "ref.wav"}
```

**Audio requirements:**
- WAV format, 24kHz mono recommended (other sample rates are resampled automatically)
- Clean audio without background noise
- One speaker per dataset (single-speaker fine-tuning only)

**Reference audio:**
- Use the same `ref_audio` for all samples (strongly recommended by Qwen3-TTS docs)
- This provides the speaker embedding — a consistent reference improves voice stability
- If no `ref_audio` field is provided, the system falls back to `ref.wav` in the dataset directory, then to the first training sample

**No emotion field needed:** Unlike the Voice Designer and Dataset Builder workflows (Methods 1 & 3), the ZIP upload metadata.jsonl has no `emotion` field. Those methods use emotion text to direct the VoiceDesign model during *synthesis* — it's a generation prompt, not training metadata. With real audio, the emotion is already captured in the recordings themselves.

However, **emotional variety in your recordings still matters.** If all your samples are neutral narration, the trained LoRA will sound flat and resist instruct-based emotion directions at inference time. Record samples across a range of deliveries (neutral, excited, angry, tense, gentle, etc.) — the model learns emotional range from the audio, not from text labels. See [[Training Guide#Emotional Range]] for guidance.

### Method 3: Dataset Builder (Interactive)

The **Dataset Builder** tab provides an interactive workflow for creating training datasets with per-sample preview:

1. **Create a project** — Give it a name, voice description, and optional global seed
2. **Define samples** — Add rows with text and emotion/style for each sample
3. **Preview audio** — Generate and listen to individual samples, or batch-generate all at once
4. **Iterate** — Regenerate samples you're not happy with, adjust emotions, add or remove rows
5. **Save as dataset** — Export the finished project as a training-ready dataset

The Dataset Builder uses the VoiceDesign model (same as Method 1) but gives you fine-grained control over each sample before committing to a dataset. See [[Dataset Builder]] for details.

### Dataset Structure

```
lora_datasets/{name}/
├── metadata.jsonl      # {audio_filepath, text} per line
├── ref.wav             # Reference audio for speaker embedding
├── ref_text.txt        # Transcript of ref.wav (must match exactly)
└── sample_000.wav ...  # Training audio files
```

### Dataset Form Persistence

The dataset generator form (name, description, sample rows) is saved to localStorage automatically. You can close the browser and return later without losing your work. If localStorage is cleared, entering the dataset name will restore the state from the server.

## Training Configuration

| Parameter | Default | Recommended | Effect |
|-----------|---------|-------------|--------|
| **Epochs** | 50 | See Quick Reference | Full passes over the dataset. More epochs = stronger voice lock but risk of overfitting |
| **Learning Rate** | 5e-6 | See Quick Reference | Higher trains faster but risks instability. Lower is safer for longer training |
| **LoRA Rank** | 32 | 32 (≤60 samples), 64 (120+) | Adapter capacity. Higher = more trainable parameters (see below) |
| **LoRA Alpha** | 64 | 128 | Scaling factor. Effective strength = alpha / rank. 128/32 = 4x or 128/64 = 2x |
| **Batch Size** | 1 | 1 | Samples per step. 1 is typical for 24GB cards |
| **Gradient Accumulation** | 8 | 4 | Simulates larger batches without more VRAM |
| **Language** | English | Match training data | Codec prefix token language. Must match your dataset's language |
| **Max Audio Length** | 30s | 30s | Clips longer than this are skipped |

### What the Parameters Do

| Setting | Effect |
|---------|--------|
| **Epochs** | Number of full passes through the dataset. More = tighter fit. |
| **Learning Rate** | How much weights adjust per step. Higher = faster learning but riskier. |
| **LoRA Rank (r)** | Capacity of the adapter (number of trainable dimensions). See Rank Selection below. |
| **LoRA Alpha** | Scaling factor. Alpha/r ratio controls effective adapter weight. 128/64 = 2x is the tested default. |
| **Grad Accumulation** | Simulates larger batch sizes. 4 is stable for most cases. |
| **Batch Size** | Samples per step. Keep at 1 (VRAM limited). |

### Rank Selection

LoRA rank (`r`) controls how many trainable dimensions each adapter layer has. Higher rank means more capacity to deviate from the base model — but also more parameters to overfit.

| Rank | Adapter Size | Best For | Notes |
|------|-------------|----------|-------|
| **r=32** | ~56 MB | ≤60 samples | Better voice quality in practice. The adapter learns voice identity without overfitting to training noise. |
| **r=64** | ~111 MB | 120+ samples | Only worthwhile with large datasets that can fill the extra capacity. On small datasets, tends to produce marginal results. |

In testing, all the best-performing adapters on ~60-sample datasets used `r=32`, while `r=64` on the same data produced larger files with worse voice quality. The extra capacity lets the adapter memorize training artifacts rather than generalizing the voice identity.

**Rule of thumb:** Use `r=32` unless your dataset has 100+ samples. If you're getting good loss numbers but the voice sounds off, try reducing rank before adjusting other parameters.

## Overfitting Guide

| Loss | Audio Quality | Instruct Following | Verdict |
|------|--------------|-------------------|---------|
| 4.4+ | Clear, no garble | Slight/faint | Undertrained |
| 4.1-4.2 | Clear, expressive | Good | Sweet spot |
| 3.9-4.1 | Expressive but garble risk | Strong | Knife's edge — run-to-run variance may garble |
| 3.4-3.8 | Garbly but legible | Strong | Starting to overfit |
| 3.0-3.3 | Garbled / no EOS | N/A | Overfit, unusable |

## Training Process

1. Click **Start Training** in the Training tab
2. The main app unloads all TTS models to free VRAM
3. Training runs as a subprocess with live log output
4. Progress shows: epoch, loss, learning rate, and estimated time
5. The best checkpoint (lowest loss) is saved automatically
6. After training, TTS models reload on next generation

**Training time:** Depends on dataset size and epochs. A 10-sample dataset with 25 epochs typically takes 5-15 minutes on a 24GB GPU.

## Training Alignment

The training script follows the official Qwen3-TTS fine-tuning approach:

- **Sub-loss weighting:** 0.3x weight on the sub-talker (code predictor) loss, matching the official `sft_12hz.py`
- **Speaker embedding:** Extracted from a single consistent reference audio via mel spectrogram, not per-sample
- **No instruct conditioning during training:** The model learns voice identity from audio + text only. Instruct is used at inference time, not training time.
- **LoRA targets:** Talker attention layers (q_proj, k_proj, v_proj, o_proj) via PEFT

## Testing a Trained Voice

After training completes:
1. The adapter appears in the **Trained Models** section of the Training tab
2. Click **Test** to generate a sample with custom text and instruct
3. If satisfied, go to the **Voices** tab and assign the adapter to a character

## Tips for Better Voices

### Emotional Range
The single most important factor for expressive LoRA voices is **emotional variety in training data**. Include samples across a range of emotions:
- Neutral narration
- Happy/excited
- Angry/frustrated
- Sad/somber
- Whispering/tense
- Shouting/commanding

Training with only neutral samples produces voices that sound flat and resist emotional instruct directions.

### Reference Audio Instruct
When generating LoRA training data, avoid giving the reference audio sample the same instruct you'll use during generation (e.g. "Neutral, even narration."). In testing, LoRA voices trained this way tend to rush and increase in loudness on sentences longer than the reference clip — the adapter bakes in the short-clip pacing. Generating the reference sample with **no instruct** (empty instruct field) produces more natural pacing at inference time, leaving the instruct free to control delivery without fighting learned patterns.

### Sample Duration Mix
Training on only short single-sentence clips (4-8s) causes tonal shifts at sentence boundaries during inference — the adapter never learns how to transition between sentences. Include a range of durations:

| Duration | Content | Purpose | Count |
|----------|---------|---------|-------|
| **1-3s** | Short exclamations ("Oh!", "Right.", "No!") | EOS behavior on short inputs | 3-5 |
| **4-8s** | Single sentences, varied emotions | Core voice identity and emotional range | Bulk of dataset |
| **15-20s** | 2-3 sentence passages | Sentence transitions, pacing, tonal flow | 5-8 |
| **20-30s** | 4-5 sentence sustained narration | Long-form delivery, paragraph flow | 2-3 |

The multi-sentence samples are critical. Without them, the model only knows "start cold, say one thing, stop" and has no learned behavior for how the voice handles commas, periods, and tonal shifts mid-passage.

### Dataset Size
- **Minimum:** 6 samples with varied emotions
- **Good:** 15-20 samples covering a range of emotions and speaking styles
- **Diminishing returns:** Beyond 25-30 samples, additional data helps less
- **15-30 minutes** of total audio is the target for a premium voice profile

### Real Audio vs Synthetic
- **Synthetic (Voice Designer):** More control over emotion labels, consistent quality, faster to create. Good for designed characters.
- **Real recordings:** More natural, captures subtle vocal characteristics. Better if you have clean single-speaker recordings.
- Both approaches produce usable LoRA voices. Synthetic data with emotional variety tends to produce more expressive results than neutral real recordings.

## Multilingual Training

LoRA adapters trained on single-language data carry that language's accent and pronunciation. An adapter trained on English samples will render German text with English pronunciation — the base model occasionally provides enough guidance for acceptable results, but it's inconsistent.

**Train a separate LoRA per language for each speaker.** This produces clean pronunciation without fighting the adapter's learned phonology.

When training non-English adapters, set the **Language** dropdown in the Training tab to match your training data's language (English, Chinese, Korean, Japanese, French, German, Italian, Portuguese, Russian, Spanish). This sets the codec prefix token used during training — a mismatch between training language and training data can cause the adapter to lose speaker identity at inference time.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Loss stays high (>10) | Invalid audio or mismatched transcripts | Check WAV files aren't corrupted; verify metadata.jsonl transcripts |
| Garbled audio on new text | Overfitting (loss too low) | Reduce epochs or learning rate |
| Generation hangs / no EOS | Severe overfitting | Retrain with fewer epochs |
| Clear but no voice identity | Undertrained (loss too high) | Increase epochs or learning rate |
| Voice sounds robotic or monotone | Training data lacks emotional variety | Regenerate dataset with more emotion+text pairs; try lower LoRA rank (8-16) |
| Voice doesn't match training samples | Undertrained or low rank | More epochs, increase LoRA rank, ensure consistent ref_audio |
| Fast/rushed speech | Ref audio instruct baked in short-clip pacing | Regenerate ref sample with empty instruct; see "Reference Audio Instruct" tip |
| Short texts hang at max_new_tokens | Model never learned short-utterance EOS | Add short vocalizations to training data |
| Initial audio glitch | Clone prompt alignment artifact | Minor — usually not present in full audiobook generation |
| ref.wav mismatch | ref_text.txt doesn't match ref.wav content | Ensure ref_text.txt contains the exact transcript of ref.wav |
| Non-English voice sounds wrong | Training language mismatch | Set Language dropdown to match training data language |

## Tested Configurations

Real training runs on Alexandria with results:

| Adapter | Samples | Epochs | LR | Alpha | Loss | Result |
|---------|---------|--------|----|-------|------|--------|
| female-lora-01 | 33 | 3 | 1e-5 | 128 | 3.93 | Working, slightly fast pacing |
| female-lora-02 | 121 | 15 | 3e-6 | 128 | 3.03 | Overfit, garbled |
| female-lora-03 | 121 | 5 | 5e-6 | 128 | 3.10 | Overfit, no EOS |
| female-lora-04 | 121 | 2 | 5e-6 | 128 | 3.86 | Understandable, garbles + weird tones |
| female-lora-05 | 121 | 1 | 5e-6 | 128 | 4.43 | Clear, weak instruct |
| female-lora-06 | 121 | 3 | 2e-6 | 64 | 3.46 | Garbly but legible |
| **female-lora-07** | **121** | **3** | **2e-6** | **128** | **4.11** | **Best — clear audio, good instruct** |
| male-lora-01 | 61 | 5 | 1e-6 | 128 | 4.44 | Clear but flat, minimal instruct following |
| male-lora-02 | 61 | 7 | 1e-6 | 128 | 4.31 | Emotive, responsive to instruct |
| **male-lora-03** | **61** | **10** | **1e-6** | **128** | **4.11** | **Best — expressive, rich, good instruct** |
| male-lora-04 | 61 | 10 | 1e-6 | 128 | 4.12 | Same config as 03, few garbled lines (run-to-run variance) |
| male-lora-05 | 61 | 9 | 1e-6 | 128 | 4.17 | Clean, expressive, safe margin |
| male-lora-06 | 61 | 12 | 1e-6 | 128 | 3.99 | Very expressive but 50% garbled |
| male-lora-07 | 61 | 14 | 1e-6 | 128 | 3.89 | Legible but overfit |