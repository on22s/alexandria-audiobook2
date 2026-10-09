# Voice types

Every speaker in a script gets a voice card in the Voices tab. This page explains the voice
types you can choose and when to use each.

## Personas for character states

Advanced global persona generation creates a separate persona and preview for each settled age/gender state of a character. Each state uses its own dialogue and nearby narration, retaining the original script evidence indices. Repeated states later in the story have separate numbered cards.

In Voices, use **Show cards** to display all characters, characters with state cards, or characters without state cards. State cards have independent voice types, reference audio/transcripts, persona descriptions, styles, seeds, approvals and candidate pools. Generate a missing or stale state persona before editing its voice. The character's base voice and manual versions remain in the character's expandable base-voice section.

**Only characters without a voice yet** also generates missing or stale state personas in advanced mode. Regenerate persona on one state card targets that state only. Failures are reported per state; successful completed states are saved, and previous settings for failed states are retained.

Generation and editing do not apply voice changes to rendered audio. Open the character's **Voice changes** controls in its base section, review the suggested state versions, and press **Apply**. Future rendering uses those versions at their timeline boundaries. A change inside a merged chunk, edited dialogue that no longer aligns, or an outdated script requires rebuilding/reviewing the chunks before application. Applying, editing or clearing a voice timeline marks only lines whose effective voice changes as pending. Use **Render Pending** in the Editor to replace them. The previous audio files remain on disk, but pending lines are detached from exports until a new render succeeds.

A known gender change without a large age jump is flagged as a **Possible identity reveal** for review. Labels remain as written in the script. If the character should sound the same throughout, set their base voice, choose **Main voice throughout**, then **Apply**; choosing alone does not save anything. Keep separate state voices when the change is intentional.

Saved Designer voices with known age labels, playable reference audio and a reference transcript are also offered in **Voice changes** alongside LoRA library voices. They are applied as clone references. Age/gender labels guide suggestions; exact declared child bands rank first, and voices used by other characters are shown as reused. Missing or corrupt references are excluded. To use one as a character's base voice, select its saved Designer reference under **Voice Clone**.

Use **Remove saved state persona** to remove an unapplied state version while keeping its audio files and other voices. Clear its applied timeline points first if playback still uses it.

Use **Recover persona** on a state card to open manual recovery with that exact state selected. When entering a character in the recovery panel yourself, explicitly choose its base persona or numbered state. State recovery and Resume retain the base and other states; use only dialogue/context from the selected state for the copied prompt.


## The speech models

The app uses three Qwen3-TTS models:

| model | ID | used for |
|---|---|---|
| **CustomVoice** | `Qwen3-TTS-12Hz-1.7B` | 9 built-in voices that follow delivery instructions |
| **Base** | `Qwen3-TTS-12Hz-1.7B-Base` | cloning a voice from reference audio, and LoRA voices |
| **VoiceDesign** | `Qwen3-TTS-12Hz-1.7B-VoiceDesign` | creating a voice from a text description |

Models load when first needed and stay cached. Only the model the current voice type needs is
loaded into GPU memory.

## The voice types at a glance

| type in the Voices tab | model | best for | batched? |
|---|---|---|---|
| **Custom Voice** | CustomVoice | the narrator and main characters | yes |
| **Built-in Voice** | Base + a shipped LoRA | ready-made trained voices, no training needed | yes |
| **Voice Clone** | Base | a character who needs one specific voice | yes |
| **LoRA Voice** | Base + your LoRA | recurring characters who need a unique, lasting voice | yes |
| **Voice Design** | VoiceDesign | minor characters with 1–3 lines | no — one line at a time |
| **Together** | the members' own voices | a group speaking at once | — |

## Choosing a type

```
Is this a main character with many lines?
├─ Yes → Do you need a voice other than the 9 built-in ones?
│        ├─ Yes → LoRA Voice (train one) or Built-in Voice (use a shipped one)
│        └─ No  → Custom Voice
└─ No  → Does the character have more than 3–4 lines?
         ├─ Yes → Voice Clone (save a designed voice and use it as the reference)
         └─ No  → Voice Design (no setup needed)
```

## Identity anchor and style timeline

Every voice card has a **Character Style** field. It is the voice's **identity anchor**: a
fixed description (for example "Heavy Scottish accent" or "Refined aristocratic tone") that is
sent with every line, so the per-line delivery instructions cannot change who the character
sounds like.

- For **Custom** voices the anchor comes **first** and the line's own instruction follows it:
  `"Heavy Scottish accent Excited, bright energy."`. Measured on 120 real lines, this cut the
  narrator's pitch drift from 3.49 to 2.53 semitones and kept the voice closer to its own
  opening (ECAPA 0.737 → 0.771).
- For **LoRA** and **Built-in** voices the character style is added **after** the line's
  instruction: `"Excited, bright energy. Heavy Scottish accent"`.

The **style timeline** lets a character change from a given line onward — "older after the
time skip" — without becoming a different speaker. Each change point sets a new anchor from
that line on.

## Generate Personas (assign voices automatically)

Instead of setting up each speaker by hand, click **Generate Personas**:

1. The language model reads sample lines for each speaker.
2. It writes a voice description ("A gruff middle-aged man with a deep, gravelly voice") and
   a sample sentence.
3. The VoiceDesign model renders reference audio from each description.
4. Each speaker is assigned that audio as a **Voice Clone**.

Options:

- **Scope** — by default only characters *without a voice yet*; you can choose all characters.
- **Save the current voices to the library first** — keeps what you already have.
- **Advanced** — sets the batch size (default 40), for scripts with very large casts.

Notes:

- Persona voices are clone voices: they keep a consistent identity but a neutral tone, because
  clone voices do not follow delivery instructions.
- For more expressive voices, start with personas and then switch key characters to LoRA or
  Custom.
- The generated reference audio is saved in `designed_voices/` and can be reused.

**Suggest LoRA Voices** does something similar with trained voices: it matches the LoRA
voices you have to your characters.

## Aliases

The **Alias of** dropdown on a voice card makes one speaker use another speaker's voice.

Use it for:

- name variants: "DR. SMITH" → "SMITH";
- age variants: "YOUNG ELENA" → "ELENA";
- titles: "KING MARCUS" → "MARCUS";
- cutting down the number of voices to set up.

How it works:

- When audio is generated, an aliased speaker uses the target speaker's voice.
- Aliases chain: if A → B → C, speaker A uses C's voice.
- A loop (A → B → A) is detected, logged as a warning, and stopped.
- The alias is stored in `voice_config.json` as the speaker's `alias_of` field.

## Custom Voice

**Model:** CustomVoice. **Best for:** the narrator and main characters.

Choose one of 9 built-in voices, with full control over emotion and delivery through the
line's instruction.

- **Voices:** Aiden, Dylan, Eric, Ono_anna, Ryan, Serena, Sohee, Uncle_fu, Vivian.
  - Most are not native English speakers; Ryan and Aiden are.
- **Settings:** the voice, and the **Character Style** (the identity anchor).
- **Batching:** yes — lines are batched and split into sub-batches by length.

## Built-in Voice

**Model:** Base with a shipped LoRA adapter. **Best for:** a ready-made voice without training.

The repository ships a set of trained voices in `builtin_lora/`. They download from Hugging
Face the first time you use one, and behave like LoRA voices.

## Voice Clone

**Model:** Base. **Best for:** a character who needs one specific voice.

Clones a voice from a 5–15 second reference recording.

- **Settings:**
  - **Designed Voice** — pick a voice saved in the Designer tab (fills in the audio path); or
  - **Custom Path** — the path to any WAV file;
  - **Reference Text** — the exact transcript of the reference audio.
- **Tips:**
  - Use clean audio with no background noise.
  - References shorter than about 7 seconds give an unstable voice; the import check
    normalises and measures them.
  - The Designer tab can create reference audio from a text description.
  - Uploaded references record their transcript, source and rights basis.
- **Delivery instructions are ignored** for clone voices: the voice comes entirely from the
  reference.
- **Batching:** yes — lines are grouped by speaker and batched together.

## LoRA Voice

**Model:** Base with a LoRA adapter. **Best for:** recurring characters who need a unique,
lasting voice that follows delivery instructions.

A LoRA adapter fine-tunes the Base model on a small set of voice samples. The result is more
consistent than a clone, and it responds to emotion and delivery instructions.

- **Settings:** the adapter (from the Training tab, or a built-in one), and the **Character
  Style**.
- **How it works:**
  1. Train an adapter on your voice samples (see the [Training guide](Training-Guide.md)).
  2. At generation time the adapter is loaded on top of the Base model.
  3. A reference clip from training provides the voice prompt.
  4. The anchor plus the line's instruction control the delivery.
- **Clone or LoRA?**
  - **Clone** — quick, no training; the voice comes from one reference clip.
  - **LoRA** — more consistent over many lines and follows emotional direction, but needs
    training.
- **Batching:** yes — lines are grouped by adapter; each adapter's group is batched on its own.

## Voice Design

**Model:** VoiceDesign. **Best for:** minor characters with 1–3 lines.

Creates a voice on the fly from a text description. **Each line gets its own freshly generated
voice**, so there is no lasting identity between lines.

- **Setting:** **Description** — the character's base voice ("Young strong soldier", "Old
  gravelly bartender").
- **How the description and the line's instruction combine:**
  - description: `"Young strong soldier"`;
  - line instruction: `"Shouting in rising desperation, with hints of panic"`;
  - sent to the model: `"Young strong soldier, Shouting in rising desperation, with hints of panic"`.
- **Tips:**
  - Keep the description about the voice itself (age, gender, vocal quality), and leave
    emotion and delivery to the instructions.
  - Avoid contradictions such as "calm gentle voice" with "screaming furiously" — they can
    cause audio glitches.
  - For a character with more than 3–4 lines, save a designed voice and use it as a clone.
- **Batching:** no — each line may have a different description, so these lines are rendered
  one at a time after all batched types.

## Together

For a line spoken by several characters at once. Each member speaks the line in the voice they
already have, and the clips are mixed together. The clips are aligned to the longest one, so
the result sounds like a chorus rather than exact unison.

## How consistent is each type?

| type | consistency | why |
|---|---|---|
| Custom | high | the same built-in voice every time (with a seed) |
| Clone | high | the same reference audio gives the same voice |
| LoRA | high | a trained identity, stable across lines |
| Voice Design | low to medium | the same description keeps voices similar, not identical |

For Voice Design, consistency depends on how similar the combined description and instruction
are from line to line. If the description stays the same and only the emotion changes, the
voices sound close enough not to be jarring.

The Editor's **Check Voices** measures this directly: it compares each rendered line with its
speaker's reference and flags lines that drifted.
