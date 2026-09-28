# Training guide

Train a LoRA adapter on the Qwen3-TTS Base model to create your own voice. A trained voice keeps
a consistent identity and still follows delivery instructions for emotion.

> **Start from the measured recipe below, not from older tables.** A learning rate of **5e-6
> makes voices run away**: in 2 of 2 tests the voice never stopped talking, while 3 of 3 at
> 1e-6 stopped correctly. The training loss does not show this — only the generated audio
> does. Evidence: [RECIPES.md](../../RECIPES.md) §"Voice adapters".

## The recipe that works

| use | settings | evidence |
|---|---|---|
| a library voice (one narrator) | 6 epochs, learning rate **1e-6**, rank 64, alpha 128, 180–200 clips | 67 shipped voices were trained this way and pass the stop check |
| an evaluation voice | 6 epochs, learning rate **1e-6**, rank 32, alpha 128, 200 clips, seed 1234 | the adapters behind the published English voice results |

The Training tab's defaults are close to this: 5 epochs, learning rate 1e-6, batch size 1,
rank 32, alpha 128, gradient accumulation 8.

## Overview

The Training tab ships with built-in LoRA voices, so you can use LoRA voices without training
anything. To make your own:

1. **Create a dataset** — voice samples (WAV) with transcripts.
2. **Configure training** — set the parameters.
3. **Train** — runs as a separate process. It needs the GPU to itself, so the app unloads its
   TTS models first.
4. **Test** — listen to the trained voice.
5. **Assign** — use the adapter in the Voices tab.

## Creating a dataset

There are two ways, plus the Voice Lab for whole libraries.

### Option 1: Dataset Builder (synthetic voices)

Generate the samples from a text description with the VoiceDesign model, previewing each one.
Best for voices that do not exist as real recordings. Click **Build New Dataset** in the
Training tab, or open the Dataset tab. See [Dataset builder](Dataset-Builder.md).

A good synthetic dataset:

- has **6–25 samples**;
- has **real emotional variety** — neutral-only data produces flat voices that resist emotion
  instructions;
- keeps one **root description** for the voice's identity, and uses each row's emotion for
  delivery (the two are combined into the VoiceDesign prompt);
- includes **short utterances** ("Oh!", "Hmm.", "Right.") so the voice learns to stop on short
  lines;
- ends with a **long, calm, neutral passage**, which makes an ideal reference sample;
- uses a **fixed seed for the reference sample**, so the speaker embedding stays stable when you
  regenerate.

Example rows for a soldier's voice:

| emotion | text |
|---|---|
| *(empty — neutral)* | The patrol route has been secured and all positions are accounted for. |
| Barking orders sharply | Move out! I want that perimeter locked down in sixty seconds! |
| Quiet, tense warning | Keep your voice down. There's movement in the treeline, fifty meters out. |
| Exhausted, catching breath | We've been marching for twelve hours. The men need rest or we'll lose them. |
| Bitter, restrained anger | They sent us in without support. Command knew exactly what they were doing. |
| Gentle, reassuring | Easy now, son. You did good out there. We're going to get you home. |

### Option 2: upload real audio (ZIP)

Upload a ZIP like this:

```
dataset.zip
├── sample_001.wav
├── sample_002.wav
├── ...
├── ref.wav            # the reference audio for the speaker embedding
└── metadata.jsonl     # one JSON object per line
```

`metadata.jsonl`:

```json
{"audio_filepath": "sample_001.wav", "text": "The transcript of this sample.", "ref_audio": "ref.wav"}
{"audio_filepath": "sample_002.wav", "text": "Another transcript.", "ref_audio": "ref.wav"}
```

**Audio requirements**

- WAV, ideally 24 kHz mono (other sample rates are resampled automatically).
- Clean audio with no background noise.
- **One speaker per dataset.**
- Clips longer than 30 seconds are skipped.

**Reference audio**

- Use the same `ref_audio` for every sample; Qwen3-TTS's own documentation strongly
  recommends it.
- It provides the speaker embedding, so a consistent reference makes the voice more stable.
- If a line has no `ref_audio`, the trainer uses `ref.wav` in the dataset folder, then the
  first training sample.

**No emotion field.** Real recordings already carry their emotion, so `metadata.jsonl` has no
`emotion` field — in the Dataset Builder the emotion is only a prompt for *generating* audio.
Your recordings still need **emotional variety**, because the voice learns its range from the
audio itself (see [Emotional range](#emotional-range)).

### Option 3: Voice Lab (a whole library)

The Voice Lab tab turns audiobooks into named LoRA voices in bulk: the Preparer aligns an
audiobook with its text, then deduplication, batch training, profiling and naming follow. See
the README's [Web interface](../../README.md#web-interface) section.

### What a dataset looks like on disk

```
lora_datasets/{name}/
├── metadata.jsonl      # {audio_filepath, text} per line
├── ref.wav             # reference audio for the speaker embedding
├── ref_text.txt        # the exact transcript of ref.wav
└── sample_000.wav ...  # the training audio
```

## Training settings

| setting | Training-tab default | effect |
|---|---|---|
| **Epochs** | 5 | full passes over the dataset; more locks the voice in harder but risks overfitting |
| **Learning rate** | 1e-6 | how far the weights move per step; **do not use 5e-6** (see the warning above) |
| **LoRA rank** | 32 | the adapter's capacity (see below) |
| **LoRA alpha** | 128 | a scaling factor; the effective strength is alpha ÷ rank (128/32 = 4×, 128/64 = 2×) |
| **Batch size** | 1 | samples per step; keep it at 1 |
| **Gradient accumulation** | 8 | simulates a larger batch without more memory |
| **Language** | English | the codec's language token — **must match the dataset's language** |

### Choosing the rank

| rank | adapter size | best for |
|---|---|---|
| **32** | about 56 MB | up to about 60 samples: learns the voice without memorising noise |
| **64** | about 111 MB | 120+ samples; the shipped library voices (180–200 clips) use 64 |

On about 60 samples, upstream's best adapters all used rank 32; rank 64 on the same data made
bigger files that sounded worse, because the extra capacity memorised training artifacts.
**Rule of thumb:** rank 32 unless you have 100+ samples. If the loss looks good but the voice
sounds wrong, lower the rank before changing anything else.

## Reading the training loss

These loss bands come from upstream Alexandria's tests on small datasets. Use them as a rough
guide only — **a good loss does not prove a good voice.** The runaway at 5e-6 is invisible in
the loss; only listening, or the stop check, catches it.

| loss | audio | follows instructions | verdict |
|---|---|---|---|
| 4.4 and above | clear | faintly | undertrained |
| 4.1–4.2 | clear, expressive | well | the sweet spot |
| 3.9–4.1 | expressive, may garble | strongly | on the edge — runs vary |
| 3.4–3.8 | garbled but understandable | strongly | overfitting |
| 3.0–3.3 | garbled, or never stops | — | overfit, unusable |

- **More data needs fewer epochs**: each epoch teaches more.
- **Total exposure** (samples × epochs) of about 250–400 worked upstream; above 600 risked
  overfitting.
- Identical settings can land on either side of 4.1 from run to run.

## Training

1. Click **Start Training** in the Training tab.
2. The app unloads its TTS models to free the GPU.
3. Training runs as a separate process with a live log: epoch, loss, learning rate and time
   left.
4. The best checkpoint (lowest loss) is kept.
5. The TTS models reload on the next render.

A small dataset trains in minutes. For reference, retraining one library voice (200 clips)
takes about 5.4 minutes on an RX 9070 XT.

### How training works

The trainer follows the official Qwen3-TTS fine-tuning method:

- **Sub-loss weighting:** the code-predictor loss is weighted 0.3×, as in the official
  `sft_12hz.py`.
- **Speaker embedding:** taken from one consistent reference clip, not from each sample.
- **No instructions during training:** the voice is learned from audio and text only;
  instructions are used when generating.
- **LoRA targets:** the talker's attention layers (`q_proj`, `k_proj`, `v_proj`, `o_proj`),
  through PEFT.

## Testing and promoting a voice

1. The adapter appears under **Trained Models** in the Training tab.
2. Click **Generate** to test it with your own text and instruction.
3. The comparison and **blind review** views let you listen against the current voice without
   knowing which is which. Promoting keeps the old version, with a receipt, so it can be rolled
   back.
4. If you are happy with it, assign the adapter to a character in the Voices tab.

The **stop check** (identity gate) runs before promotion and catches a voice that never stops
talking.

## Tips for better voices

### Emotional range

The most important factor for an expressive voice is **emotional variety in the training
data**. Include:

- neutral narration;
- happy or excited;
- angry or frustrated;
- sad or somber;
- whispered or tense;
- shouting or commanding.

Neutral-only data makes a voice that sounds flat and resists emotional instructions.

### The reference sample's instruction

When generating training data, do **not** give the reference sample the instruction you will
use later (for example "Neutral, even narration."). Voices trained that way tended to rush and
grow louder on sentences longer than the reference clip. Generating the reference with an
**empty instruction** gave more natural pacing.

### Mix the clip lengths

Training only on short single-sentence clips (4–8 s) causes changes of tone between sentences,
because the voice never learns how to move from one sentence to the next. Include a range:

| length | content | teaches | how many |
|---|---|---|---|
| 1–3 s | short exclamations ("Oh!", "Right.", "No!") | stopping on short lines | 3–5 |
| 4–8 s | single sentences, varied emotions | the core voice and its range | most of the dataset |
| 15–20 s | 2–3 sentences | transitions, pacing, flow | 5–8 |
| 20–30 s | 4–5 sentences of narration | long-form delivery | 2–3 |

### How much data

- **Minimum:** 6 samples with varied emotions.
- **Good:** 15–20 samples covering a range of emotions and styles.
- **Library quality:** the shipped voices use 180–200 clips.
- Upstream found diminishing returns beyond 25–30 synthetic samples, and suggested 15–30
  minutes of audio for a premium voice.

### Real or synthetic?

- **Synthetic (Dataset Builder):** more control over emotion, consistent quality, quick to
  make. Good for invented characters.
- **Real recordings:** more natural, capturing subtle qualities of a real voice. Best when you
  have clean single-speaker recordings.
- Both work. Synthetic data with emotional variety tends to be more expressive than neutral
  real recordings.

## Other languages

An adapter trained on one language carries that language's accent. An English-trained adapter
reads German text with English pronunciation — sometimes acceptable, often not.

**Train a separate adapter per language for each speaker**, and set the Training tab's
**Language** to the dataset's language (English, Chinese, Korean, Japanese, French, German,
Italian, Portuguese, Russian or Spanish). A mismatch can make the adapter lose the speaker's
identity.

## Troubleshooting

| problem | likely cause | fix |
|---|---|---|
| loss stays above 10 | invalid audio or wrong transcripts | check the WAV files and the transcripts in `metadata.jsonl` |
| voice never stops talking | learning rate too high, or overfitting | retrain at 1e-6; use fewer epochs |
| garbled audio on new text | overfitting (loss too low) | fewer epochs |
| clear audio but no identity | undertrained (loss too high) | more epochs |
| flat, monotone voice | no emotional variety in the data | add varied emotion and text pairs; try a lower rank (8–16) |
| doesn't sound like the samples | undertrained, or rank too low | more epochs, a higher rank, one consistent `ref_audio` |
| rushed speech | the reference sample was made with an instruction | regenerate it with an empty instruction |
| short lines run to `max_new_tokens` | the voice never learned to stop on short lines | add short vocalisations to the data |
| a small glitch at the start | clone-prompt alignment | minor; usually absent in a full audiobook |
| reference mismatch | `ref_text.txt` does not match `ref.wav` | make the transcript exact |
| wrong accent in another language | language setting mismatch | set **Language** to match the data |

Do **not** raise the learning rate to fix a high loss — check the data first.

## Upstream's tested configurations

These runs were made by upstream Alexandria on small synthetic datasets. They are kept for
their loss-versus-quality observations. **Rows at 5e-6 and above predate the runaway finding**
and are not recommended.

| adapter | samples | epochs | learning rate | alpha | loss | result |
|---|---|---|---|---|---|---|
| female-lora-01 | 33 | 3 | 1e-5 | 128 | 3.93 | working, slightly fast pacing |
| female-lora-02 | 121 | 15 | 3e-6 | 128 | 3.03 | overfit, garbled |
| female-lora-03 | 121 | 5 | 5e-6 | 128 | 3.10 | overfit, never stops |
| female-lora-04 | 121 | 2 | 5e-6 | 128 | 3.86 | understandable, garbles, odd tones |
| female-lora-05 | 121 | 1 | 5e-6 | 128 | 4.43 | clear, weak instruction following |
| female-lora-06 | 121 | 3 | 2e-6 | 64 | 3.46 | garbled but understandable |
| **female-lora-07** | **121** | **3** | **2e-6** | **128** | **4.11** | **best — clear audio, follows instructions** |
| male-lora-01 | 61 | 5 | 1e-6 | 128 | 4.44 | clear but flat, barely follows instructions |
| male-lora-02 | 61 | 7 | 1e-6 | 128 | 4.31 | emotive, responsive |
| **male-lora-03** | **61** | **10** | **1e-6** | **128** | **4.11** | **best — expressive, rich, follows instructions** |
| male-lora-04 | 61 | 10 | 1e-6 | 128 | 4.12 | same settings as 03; a few garbled lines (run-to-run variation) |
| male-lora-05 | 61 | 9 | 1e-6 | 128 | 4.17 | clean, expressive, a safe margin |
| male-lora-06 | 61 | 12 | 1e-6 | 128 | 3.99 | very expressive but half the lines garbled |
| male-lora-07 | 61 | 14 | 1e-6 | 128 | 3.89 | understandable but overfit |
