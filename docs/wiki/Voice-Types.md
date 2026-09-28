# Voice Types

Alexandria uses three Qwen3-TTS models to provide five voice configuration options. Each serves a different use case.

## Qwen3-TTS Models

| Model | ID | Purpose |
|-------|------|---------|
| **CustomVoice** | `Qwen3-TTS-12Hz-1.7B` | 9 built-in voices with instruct-based emotion/style control |
| **Base** | `Qwen3-TTS-12Hz-1.7B-Base` | Voice cloning from reference audio, LoRA fine-tuning |
| **VoiceDesign** | `Qwen3-TTS-12Hz-1.7B-VoiceDesign` | Generate voices from text descriptions |

Models are loaded on demand and cached. Only the model needed for the current voice type loads into VRAM.

---

## Persona Generation (Auto-Assign Voices)

Instead of manually configuring each speaker, click **Generate Personas** in the Voices tab to auto-assign voices for all characters in one step.

**How it works:**
1. The LLM reads dialogue samples from the script for each speaker
2. It generates a voice description (e.g., "A gruff middle-aged man with a deep, gravelly voice") and a sample text for each
3. The VoiceDesign model synthesizes reference audio from each description
4. Each speaker is assigned as a **Clone Voice** using the generated reference audio

**Modes:**
- **Standard** — All speakers processed in a single LLM call
- **Advanced** — Toggle "Advanced" to control batch size (default 40). Speakers are processed in discovery batches, useful for scripts with large casts

**Notes:**
- Persona-generated voices are clone voices, so they have a consistent identity but a neutral tone — instruct directions are not followed
- For more expressive voices, use persona generation as a starting point, then switch key characters to LoRA or Custom voice types
- The generated reference audio is saved in `designed_voices/` and can be reused

---

## Speaker Aliases

Each voice card has an "Alias of" dropdown that maps one speaker to another's voice configuration.

**Use cases:**
- Character name variants: "DR. SMITH" → "SMITH"
- Age variants: "YOUNG ELENA" → "ELENA"
- Titles: "KING MARCUS" → "MARCUS"
- Reducing the number of voices to configure in scripts with many speaker variants

**How it works:**
- During audio generation, aliased speakers resolve to the target speaker's voice config
- Aliases resolve transitively: if A → B → C, speaker A uses C's voice
- Cycle detection prevents infinite loops (A → B → A logs a warning and stops)
- The alias is set per-speaker in `voice_config.json` via the `alias_of` field

---

## Custom Voice

**Model:** CustomVoice
**Best for:** Main characters, narrator

Select from 9 pre-trained voices with full instruct control over emotion and delivery.

**Available voices:** Aiden, Dylan, Eric, Ono_anna, Ryan, Serena, Sohee, Uncle_fu, Vivian

> Note: Most built-in voices are non-English. Only Ryan and Aiden are native English speakers.

**Configuration:**
- **Voice** — Select from the dropdown
- **Character Style** — Persistent traits appended to every line's instruct (e.g., "Heavy Scottish accent", "Refined aristocratic tone", "Always speaks in clipped sentences")

**How instruct works:**
Each chunk has an `instruct` field set by the LLM (e.g., "Excited, bright energy"). If you set a character style, it's appended: `"Excited, bright energy. Heavy Scottish accent"`. The combined text is sent to the TTS engine as voice direction.

**Batching:** Yes — Custom voices use the native list-based batch API with sub-batching by text length.

---

## Voice Clone

**Model:** Base
**Best for:** Characters that need a specific real or designed voice

Clone any voice from a 5-15 second reference audio sample. The TTS reproduces the voice characteristics of the reference.

**Configuration:**
- **Designed Voice** — Select a voice saved from the Voice Designer tab (auto-fills audio path)
- **Custom Path** — Or enter a path to any WAV file manually
- **Reference Text** — Exact transcript of the reference audio

**Tips:**
- Use clean, clear audio without background noise
- 5-15 seconds is ideal — too short lacks character, too long wastes processing
- The Voice Designer tab can create reference audio from text descriptions
- Instruct directions are **ignored** for clone voices — the voice identity comes entirely from the reference

**Batching:** Yes — Clone chunks are grouped by speaker and batched together. Multiple chunks for the same speaker process in one call.

---

## LoRA Voice

**Model:** Base + LoRA adapter
**Best for:** Recurring characters that need a unique, persistent voice with instruct-following

LoRA (Low-Rank Adaptation) fine-tunes the Base model on a small dataset of voice samples. The result is a voice identity that's more consistent than cloning and responds to instruct directions for emotion and delivery.

**Configuration:**
- **Adapter** — Select a trained adapter from the Training tab (includes built-in presets)
- **Character Style** — Same as Custom voice — persistent traits appended to every instruct

**How it works:**
1. Train a LoRA adapter on 6-25 voice samples (see [[Training Guide]])
2. At generation time, the adapter weights are loaded on top of the Base model
3. A reference audio from training provides the voice clone prompt
4. The instruct field (plus character style) controls delivery

**When to use LoRA vs Clone:**
- **Clone** — Quick setup, no training needed. Voice identity comes from one reference sample.
- **LoRA** — Better consistency across many lines. Responds to emotional direction. Requires training but produces a more reliable voice.

**Batching:** Yes — LoRA chunks are grouped by adapter and batched. If multiple characters use different adapters, each adapter group batches independently.

---

## Voice Design (On-the-fly)

**Model:** VoiceDesign
**Best for:** Minor/throwaway characters that appear 1-3 times

Uses the VoiceDesign model to generate a voice on-the-fly from a text description. Each line gets an independently generated voice — there's no persistent voice identity between lines.

**Configuration:**
- **Description** — Base voice description for the character (e.g., "Young strong soldier", "Old gravelly bartender")

**How the description combines with instruct:**
The base description and each line's instruct are joined:
- Description: `"Young strong soldier"`
- Line instruct: `"Shouting in rising desperation, with hints of panic"`
- VoiceDesign prompt: `"Young strong soldier, Shouting in rising desperation, with hints of panic"`

**Tips:**
- Keep the base description focused on voice identity (age, gender, vocal quality)
- Let the per-line instruct handle emotion and delivery
- Avoid contradictions between description and instruct (e.g., "calm gentle voice" + "screaming furiously") — these can cause audio glitches
- If a character has more than 3-4 lines, consider saving a designed voice and using Clone instead

**Batching:** No — Each line may have a different voice description, so design chunks process sequentially after all batched types complete.

---

## Choosing the Right Voice Type

```
Is this a main character with many lines?
├─ Yes → Do you need a unique voice (not one of the 9 built-in)?
│        ├─ Yes → LoRA Voice (train an adapter)
│        └─ No  → Custom Voice (pick a built-in voice)
└─ No  → Does this character appear more than 3-4 times?
         ├─ Yes → Voice Clone (save a designed voice, use as clone reference)
         └─ No  → Voice Design (on-the-fly, no setup needed)
```

## Voice Consistency

| Type | Consistency | Notes |
|------|------------|-------|
| Custom | High | Same built-in voice every time (with seed) |
| Clone | High | Same reference audio = same voice character |
| LoRA | High | Trained identity, consistent across lines |
| Voice Design | Low-Medium | Same base description keeps voice similar but not identical |

For Voice Design, consistency depends on how similar the combined description+instruct prompts are across lines. If the base description stays the same and only the emotional direction varies, voices will sound similar enough to not be jarring.
