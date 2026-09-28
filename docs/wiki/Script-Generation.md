# Script generation

A language model turns the book's text into a script: a list of lines, each with a speaker and
a delivery instruction for the voice.

## The pipeline

1. **Upload** a book (`.txt`, `.md` or `.epub`) in the Script tab. EPUB files are converted
   to plain text on upload.
2. **Generate Annotated Script** runs three passes over the text:
   1. **Split** — divide the text into narration and spoken lines. Setup → **Step 1:
      dialogue detection** chooses how:
      - **Auto** (recommended) — use the quote marks when they clearly split a chunk and
        pass a check; otherwise ask the model. Every published result used this.
      - **Quote marks only** — never ask the model.
      - **Quote-aware** — like quote marks, but quoted terms inside narration stay
        narration.
      - **Model only** — always ask the model.
   2. **Attribute** — give every spoken line a speaker from the book's cast list, using the
      surrounding text.
   3. **Instruct** — write a delivery instruction for every line.
3. **Review** (optional) — a second model pass that fixes common mistakes (see below).
4. **Voices** — every speaker in the script gets a voice card in the Voices tab.

Each pass is checkpointed. A run can be paused, resumed after a crash or power cut, saved as
a snapshot of the finished part, or started over without losing your settings. The activity
line under the Generate button shows the current step, unit, attempt and time left.

## Script format

The script is a JSON array:

```json
[
  {"speaker": "NARRATOR", "text": "The door creaked open slowly.", "instruct": "Calm, even narration."},
  {"speaker": "ELENA", "text": "Ah! Who's there?", "instruct": "Startled and fearful, sharp whispered question, voice cracking with panic."},
  {"speaker": "MARCUS", "text": "Haha... did you miss me?", "instruct": "Menacing confidence, low smug drawl with a dark chuckle, savoring the moment."}
]
```

- **`speaker`** — `NARRATOR`, or a character's name in capitals from the cast list.
- **`text`** — exactly what is spoken. Attribution tags ("she said") belong to the narration,
  and quote marks are dropped: the speaker field records that a line is speech.
- **`instruct`** — a short voice direction of one or two sentences (about 8–15 words)
  describing emotion, delivery and vocal quality.

## Writing good instructions

The `instruct` field is sent to the speech engine as voice direction. Layer up to three things:

1. **Emotional tone** — what the character feels: furious, fearful, triumphant, hollow,
   desperate, amused.
2. **Delivery** — how they say it: whispered, low drawl, clipped and curt, measured, rising
   intensity.
3. **Vocal quality** — what the voice does: voice cracking, gravelly, tight, raw, breathy,
   strained.

Not every line needs all three. A low-stakes line can be simple ("Casual, offhand remark.");
save the rich, layered instructions for emotional peaks.

**Good examples**

- `"Cold fury, barely contained, voice tight."`
- `"Bright eager excitement, words tumbling out."`
- `"Menacing confidence, low smug drawl with a dark chuckle."`
- `"Devastated, voice cracking, struggling to hold composure."`
- `"Sharp whispered warning, urgent and hushed."`

**The narrator** defaults to `"Neutral, even narration."`. At a change of mood in the scene, one
modifier is allowed — `"Tense, clipped narration."`, `"Quiet, somber narration."`, `"Wry,
light narration."` — and the same tone holds across the narrator's lines within that scene.

**Avoid**

- Physical actions ("trembling", "leaning forward") — describe the voice, not the body.
- Two words that mean the same thing ("sneering contempt, dripping with disdain").
- Weak qualifiers ("slightly", "a bit") — they dilute the direction.
- Bare speed words ("fast", "slow") — show pace through energy instead ("urgent intensity",
  "heavy, weighted delivery").
- Words that describe the voice itself ("deep", "raspy"). They fight the chosen voice; the
  voice's identity anchor removes them.

**Voice descriptors that work:** voice cracking, hollow, seething, flat, low, cold, numb,
drained, gravelly, tight, raw, breathy, hushed, booming, clipped, strained. The
[Voice reference](Voice-Reference.md) has a much longer vocabulary.

### Non-verbal sounds

Sounds are written as real, pronounceable text — never as bracketed tags or special tokens:

- gasps: "Ah!", "Oh!" with an instruction such as "Fearful, sharp gasp.";
- sighs: "Haah...", "Hff...";
- laughter: "Haha!", "Ahaha...";
- crying: "Hic... sniff...";
- exclamations: "Mmm...", "Hmm...", "Ugh...".

Nothing unspeakable — bare tags, pictographic kana — reaches the speech engine.

## Language-model settings

Set these in the Setup tab:

- **Base URL**, **API Key** (`local` for a local server, or `env:NAME` to read an environment
  variable) and **Model Name**.
- **Reasoning effort** — use *low* for reasoning models. Do not ban `<think>`: reasoning low
  measured better than reasoning off on every model that reasons.

### Sampling defaults

| setting | default | notes |
|---|---|---|
| Temperature | 0.6 | general setting |
| Step 1 / 2 / 3 temperatures | 0.1 each | the three passes use their own temperatures |
| Top P | 0.8 | |
| Top K | 0 (off) | |
| Min P | 0 | |
| Presence penalty | 0 | |
| Banned tokens | empty | comma-separated; leave `<think>` out |

The published accuracy numbers were measured at temperature 0, where a repeat run gives the
same answer on every line.

### How the text is divided

- **Step 1** sends about 3,000 characters per request (Setup → **Step 1: text per request**).
- **Step 2** sends 25 lines per request, plus about 2,000 characters of surrounding text
  around them. That surrounding block is what the `michel2_full` prompt adds, and it is the
  biggest single improvement measured. Setting it to 0 reproduces the scores from before
  2026-09-19.
- The cast list is carried from request to request so names stay consistent.
- If a request fails, **context rescue** retries it with wider windows (2,000, 4,000 and 6,000
  characters, two retries each).

## Reviewing a script

After generation, **Review Script** runs a second model pass that fixes:

1. **Attribution tags inside dialogue** — removes "said he", "she replied" and similar.
2. **Narration given to a character** — splits it out as NARRATOR.
3. **Dialogue hidden in narration** — extracts it as the character's line.
4. **Narration split too finely** — merges short consecutive narrator lines from the same
   scene.
5. **Invalid instructions** — turns physical actions into vocal equivalents, and keeps valid
   rich instructions.

**Contextual Review** processes the script in overlapping windows (4 lines either side by
default), so the model sees the surrounding lines. It is more accurate for speaker and tone
consistency.

**Batch review** can review a whole series of saved scripts at once, optionally finding
nicknames first and running two passes (forward, then backward).

Related tools: **Find Nicknames** discovers alternative names for characters, and **Edit
aliases** lets you correct them, so "Betty" and "BEATRICE" share one voice.

## Prompts

Each pass reads its own prompt file in `app/`:

- `default_prompts_segment.txt` (step 1), `default_prompts_attribute.txt` (step 2),
  `default_prompts_instruct.txt` (step 3);
- `review_prompts.txt` for the review pass.

Each file holds a system message, a `---SEPARATOR---` line, then a user message with
`{roster}` and `{batch}` placeholders.

To change them:

- **In Setup** — choose the attribution prompt variant (`michel2_full` is the default and the
  measured best), edit the text, and **Save as preset**. Presets survive restarts.
- **What the model will see** shows the exact messages step 2 would send.
- **Reset to Defaults** reloads the files without a restart.

Changing the prompt changes the measurement: a published score belongs to its prompt
variant. See [Prompts and adapters](Prompts-and-Adapters.md).

### Books in other languages

The default prompts are written for English. For other languages, adapt them to that
language's conventions — French guillemets (« »), Japanese brackets (「」), the usual ways of
attributing speech — and set the TTS **Language** to match.

## Which model to use

The measured recommendations — by GPU memory, with scores — are in
[Which model for your card](Which-Model-For-Your-Card.md) and in the README's
[Recommended LLM models](../../README.md#recommended-llm-models). In short:

- **Qwen3.8-27B** at UD-Q3_K_XL is the most accurate local model that fits a 16 GB card.
- **Qwen3.6-35B-A3B** is the fast choice: about as fast as an 8B model, about 11 points more
  accurate.
- **DeepSeek v4-pro** (hosted) is the most accurate model measured.

## Saved scripts

Scripts are saved and loaded from the Script tab's **Saved Scripts** section:

- **Save** keeps the script and its voice settings in `scripts/`.
- **Load** restores them, so you can keep editing or regenerate specific lines.
- **Repair** previews fix speakers or content in a saved script, and keep a backup of the
  original.
