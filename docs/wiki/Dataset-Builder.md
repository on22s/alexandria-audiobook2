# Dataset Builder

The Dataset Builder tab provides an interactive workflow for creating LoRA training datasets with per-sample audio preview.

## When to Use

Use the Dataset Builder when you want fine-grained control over each training sample before committing to a dataset. It's ideal for:

- Iterating on voice descriptions and emotions until each sample sounds right
- Building datasets incrementally over multiple sessions
- Previewing how different emotion+text combinations affect the generated voice

For quick dataset creation without per-sample preview, use the **Generate Dataset** form in the Training tab instead.

## Workflow

### 1. Create a Project

Click **New Project** and enter a name. This creates a workspace in `dataset_builder/` that persists across sessions.

### 2. Configure Voice

- **Voice Description** — The base voice identity description (e.g., "A warm, deep male narrator with a calm baritone")
- **Global Seed** — Optional seed for reproducible generation (leave empty for random)

### 3. Define Samples

Add rows to the sample table. Each row has:

| Field | Description |
|-------|-------------|
| **Text** | The spoken content for this sample |
| **Emotion** | Emotion/style direction appended to the voice description (e.g., "cheerful", "tense whisper", "barking orders"). This is a VoiceDesign generation prompt — it controls how the synthetic audio is produced, not a training label. If you're preparing real audio for ZIP upload instead, see [[Training Guide#Method 2: Upload Real Audio (ZIP)]]. |

Tips for good training data:
- Include a range of emotions (neutral, happy, angry, sad, tense, commanding)
- Mix short exclamations ("Oh!", "Right.") with longer passages
- Include at least one long neutral passage for the reference sample
- See [[Training Guide]] for detailed dataset guidance

### 4. Generate and Preview

- **Generate single** — Click the generate button on any row to preview that sample
- **Generate All** — Batch-generate all samples at once
- **Cancel** — Stop a running batch without losing completed samples

Listen to each sample and regenerate any that don't sound right. Adjust the emotion text or voice description as needed.

### 5. Save as Dataset

When satisfied with all samples:

1. Select a **Reference Sample** — This becomes `ref.wav` for speaker embedding during training. Choose a clear, representative line (typically a long neutral passage).
2. Click **Save as Dataset** — The project is exported to `lora_datasets/` and appears in the Training tab.

## Project Persistence

- Projects are saved to `dataset_builder/{name}/` on the server
- The UI state (rows, description, seed) is stored in the project's `state.json`
- You can close the browser and return later — the project loads from the server
- Projects persist independently of training datasets: saving a dataset copies the data, it doesn't consume the project

## API

See [[API Reference#Dataset Builder]] for programmatic access to all Dataset Builder endpoints.
