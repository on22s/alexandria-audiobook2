# Dataset builder

The Dataset tab builds a LoRA training dataset one sample at a time, letting you listen to each
sample before you commit.

## When to use it

- You want to adjust voice descriptions and emotions until every sample sounds right.
- You want to build a dataset over several sessions.
- You want to hear how different emotion-and-text combinations change the voice.

It is also what the Training tab's **Build New Dataset** button opens.

## Workflow

### 1. Create a project

Click **New Dataset** and enter a name. This creates a workspace in `dataset_builder/` that is
kept between sessions.

### 2. Describe the voice

- **Root Voice Description** — the voice's identity, for example "A warm, deep male narrator
  with a calm baritone". The [Voice reference](Voice-Reference.md) explains which words work.
- **Global Seed** — optional, for reproducible generation; leave it empty for random.

### 3. Add samples

Click **Add Row** for each sample. Each row has:

| field | meaning |
|---|---|
| **Text** | what is spoken in this sample |
| **Emotion** | a style direction added to the voice description ("cheerful", "tense whisper", "barking orders") |

The emotion is only a prompt for **generating** the audio with VoiceDesign; it is not stored as
a training label. (If you are preparing real recordings instead, see the
[Training guide](Training-Guide.md#option-2-upload-real-audio-zip).)

Tips:

- Cover a range of emotions: neutral, happy, angry, sad, tense, commanding.
- Mix short exclamations ("Oh!", "Right.") with longer passages.
- Include at least one long neutral passage to use as the reference sample.
- The [Training guide](Training-Guide.md) has more on what makes a good dataset.

### 4. Generate and listen

- **Generate** on a row — render just that sample.
- **Generate Pending** — render every sample that has no audio yet.
- **Regen All** — re-render every sample.
- **Cancel** — stop a running batch; finished samples are kept.

Listen to each sample and regenerate any that don't sound right, adjusting the emotion or the
voice description as needed.

### 5. Save as a dataset

When every sample sounds right:

1. Choose the **reference sample**. It becomes `ref.wav`, the speaker embedding used in
   training — pick a clear, representative line, usually a long neutral passage.
2. Click **Save as Training Dataset**. The project is copied to `lora_datasets/` and appears in
   the Training tab.

You can also **Import / Export JSON** to move a project's rows between machines.

## Where projects are kept

- Each project lives in `dataset_builder/{name}/` on the server.
- The rows, description and seed are saved in the project's `state.json`, so you can close
  the browser and come back later.
- Saving a dataset **copies** the data; the project stays, and you can keep working on it.

## API

The [API reference](API-Reference.md#dataset-builder) shows how to drive the Dataset Builder
over HTTP.
