# Script Generation

Alexandria uses an LLM to convert raw book text into a structured script with speakers, dialogue, and TTS voice directions.

## Pipeline

1. **Upload** a book file (.txt, .md, or .epub) in the Script tab — EPUB files are automatically converted to plain text on upload
2. **Generate Script** — The LLM processes the text in chunks, producing a JSON array
3. **(Optional) Review Script** — A second LLM pass fixes common annotation errors
4. **Parse Voices** — Extract unique speakers for voice configuration

## Script Format

The generated script is a JSON array:

```json
[
  {"speaker": "NARRATOR", "text": "The door creaked open slowly.", "instruct": "Calm, even narration."},
  {"speaker": "ELENA", "text": "Ah! Who's there?", "instruct": "Startled and fearful, sharp whispered question, voice cracking with panic."},
  {"speaker": "MARCUS", "text": "Haha... did you miss me?", "instruct": "Menacing confidence, low smug drawl with a dark chuckle, savoring the moment."}
]
```

### Fields

- **speaker** — Character name (uppercase) or `NARRATOR`
- **text** — The spoken dialogue or narration
- **instruct** — 1-2 sentence TTS voice direction (~8-15 words). Describes emotional tone, delivery style, and vocal quality.

### Instruct Writing

The `instruct` field is sent directly to the TTS engine as voice direction. Layer up to three vocal dimensions:

1. **Emotional tone** (what they feel) — furious, fearful, triumphant, hollow, desperate, amused
2. **Delivery** (how they say it) — whispered, low drawl, clipped and curt, measured, rising intensity
3. **Vocal quality** (what the voice does) — voice cracking, gravelly, tight, raw, breathy, strained

Not every line needs all three. Low-stakes lines can be simple ("Casual, offhand remark."). Save rich layered instructs for emotional peaks.

**Good instructs:**
- `"Cold fury, barely contained, voice tight."`
- `"Bright eager excitement, words tumbling out."`
- `"Menacing confidence, low smug drawl with a dark chuckle."`
- `"Devastated, voice cracking, struggling to hold composure."`
- `"Sharp whispered warning, urgent and hushed."`

**Narrator instructs:** Default to `"Neutral, even narration."` At scene-level tone shifts, a single tonal modifier is allowed: `"Tense, clipped narration."`, `"Quiet, somber narration."`, `"Wry, light narration."` Hold the same tone across consecutive narrator entries within a scene.

**Avoid:**
- Physical actions ("trembling", "leaning forward") — describe the voice, not the body
- Synonym stacking ("sneering contempt, dripping with disdain") — both mean the same thing
- Weak qualifiers ("slightly", "a bit") — they dilute the direction
- Bare speed words ("fast", "slow") — convey pacing through energy instead ("urgent intensity", "heavy, weighted delivery")

**Voice descriptors that work:** voice cracking, hollow, seething, flat, low, cold, numb, drained, gravelly, tight, raw, breathy, hushed, booming, clipped, strained

### Non-verbal Sounds

Vocalizations are written as real pronounceable text — no bracket tags or special tokens:
- Gasps: "Ah!", "Oh!" with instruct "Fearful, sharp gasp."
- Sighs: "Haah...", "Hff..."
- Laughter: "Haha!", "Ahaha..."
- Crying: "Hic... sniff..."
- Exclamations: "Mmm...", "Hmm...", "Ugh..."

## LLM Configuration

### Setup Tab Settings

- **Base URL** — LLM server endpoint (e.g., `http://localhost:1234/v1` for LM Studio)
- **API Key** — Your API key (use `local` for local servers)
- **Model Name** — The model to use

### Sampling Parameters

| Parameter | Default | Notes |
|-----------|---------|-------|
| Temperature | 0.6 | Lower = more deterministic |
| Top P | 0.8 | Nucleus sampling |
| Top K | 20 | Top-K sampling |
| Min P | 0 | Minimum probability |
| Presence Penalty | 0.0 | Penalize repeated tokens |
| Banned Tokens | *(empty)* | Comma-separated list (e.g., `<think>` for thinking models) |

### Smart Chunking

The LLM processes text in chunks (default 3000 chars) to handle books of any length. Between chunks:
- The character roster is passed forward for name consistency
- The last 3 script entries provide style continuity
- The chunk size is configurable in Setup > Prompt Settings

## Script Review

After generation, click **Review Script** for a second LLM pass that fixes common errors:

1. **Attribution tags in dialogue** — Strips "said he", "she replied" etc. from spoken text
2. **Misattributed narration** — Splits narration mixed into character entries as NARRATOR
3. **Dialogue in narrator entries** — Extracts character dialogue embedded in narration
4. **Over-split narrator entries** — Merges short consecutive narrator entries covering the same scene
5. **Invalid instructs** — Corrects physical actions to vocal equivalents; preserves valid rich instructs

**Contextual Review** — An alternative review mode that processes entries in overlapping windows, providing surrounding context to the LLM for better accuracy on speaker attribution and tone consistency. Uses a sliding window (default size 4) with overlap to maintain continuity across batches.

Review prompts are customizable in `review_prompts.txt`.

## Prompt Customization

LLM prompts are stored in plain-text files at the project root:

- **`default_prompts.txt`** — Script generation prompts
- **`review_prompts.txt`** — Script review prompts

Each file contains a system prompt and user prompt separated by `---SEPARATOR---`.

**How to customize:**
- **Per-session:** Edit in the Setup tab's Prompt Customization section
- **Permanent:** Edit the text files directly — changes hot-reload on next request
- **Reset:** Click "Reset to Defaults" in Setup to reload from files

### Non-English Books

The default prompts are written for English text. For other languages, edit prompts to match that language's conventions:
- French guillemets (<<>>)
- Japanese brackets
- Language-appropriate attribution patterns
- Set the TTS **Language** dropdown to match

## Recommended LLM Models

Non-thinking models work best for script generation:

| Model | Notes |
|-------|-------|
| **Qwen3-next** (80B-A3B) | Excellent JSON output and instruct directions |
| **Gemma3** (27B) | Strong JSON output |
| **Qwen2.5** (any size) | Reliable JSON output |
| **Llama 3.1/3.2** | Good character distinction |
| **Mistral/Mixtral** | Fast and reliable |

**Thinking models** (DeepSeek-R1, GLM4-air, etc.) can interfere with JSON output. Add `<think>` to **Banned Tokens** to disable thinking mode.

## Saved Scripts

Scripts can be saved and loaded from the Editor tab:
- **Save** preserves the annotated script, voice configuration, and all chunk data
- **Load** restores everything, allowing you to resume editing or regenerate specific chunks
- Scripts are stored in the `scripts/` directory
