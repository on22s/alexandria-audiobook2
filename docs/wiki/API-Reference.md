# API Reference

Alexandria exposes a REST API at `http://127.0.0.1:<port>` for programmatic access. The port is assigned automatically by Pinokio.

## Configuration

```bash
# Get current config
curl http://127.0.0.1:4200/api/config

# Get file-based default prompts (hot-reloads from default_prompts.txt)
curl http://127.0.0.1:4200/api/default_prompts

# Save config
curl -X POST http://127.0.0.1:4200/api/config \
  -H "Content-Type: application/json" \
  -d '{
    "llm": {"base_url": "http://localhost:1234/v1", "api_key": "local", "model_name": "qwen2.5-14b"},
    "tts": {
      "mode": "local",
      "device": "auto",
      "language": "English",
      "parallel_workers": 25,
      "batch_seed": 12345,
      "compile_codec": true,
      "sub_batch_enabled": true,
      "sub_batch_min_size": 4,
      "sub_batch_ratio": 5,
      "sub_batch_max_chars": 3000
    }
  }'
```

## Script Generation

```bash
# Upload text file
curl -X POST http://127.0.0.1:4200/api/upload -F "file=@mybook.txt"

# Generate script (starts background task)
curl -X POST http://127.0.0.1:4200/api/generate_script

# Check generation status
curl http://127.0.0.1:4200/api/status/script_generation

# Review script (second LLM pass for error correction)
curl -X POST http://127.0.0.1:4200/api/review_script

# Check review status
curl http://127.0.0.1:4200/api/status/review

# Get annotated script with post-processing
curl http://127.0.0.1:4200/api/annotated_script
```

## Voice Management

```bash
# Parse voices from script
curl -X POST http://127.0.0.1:4200/api/parse_voices

# Get voices and current config
curl http://127.0.0.1:4200/api/voices

# Save voice config
curl -X POST http://127.0.0.1:4200/api/save_voice_config \
  -H "Content-Type: application/json" \
  -d '{
    "NARRATOR": {"type": "custom", "voice": "Ryan", "character_style": "calm, measured narration"},
    "ELENA": {"type": "clone", "ref_audio": "designed_voices/previews/preview_123.wav", "ref_text": "Hello there."},
    "MARCUS": {"type": "lora", "adapter_id": "dark_voice_123", "adapter_path": "lora_models/dark_voice_123", "character_style": "menacing undertone"},
    "SOLDIER": {"type": "design", "description": "Young strong soldier"}
  }'
```

### Voice Config Fields

| Field | Used By | Description |
|-------|---------|-------------|
| `type` | All | `"custom"`, `"clone"`, `"lora"`, or `"design"` |
| `voice` | Custom | Built-in voice name (Aiden, Dylan, Eric, etc.) |
| `character_style` | Custom, LoRA | Persistent style appended to every instruct |
| `seed` | All | Random seed (`"-1"` for random) |
| `ref_audio` | Clone | Path to reference audio file |
| `ref_text` | Clone | Transcript of reference audio |
| `adapter_id` | LoRA | Adapter identifier |
| `adapter_path` | LoRA | Path to adapter directory |
| `description` | Design | Base voice description |
| `alias_of` | Any | Map this speaker to another speaker's voice config |

## Chunk Management

```bash
# Get all chunks
curl http://127.0.0.1:4200/api/chunks

# Update a chunk
curl -X POST http://127.0.0.1:4200/api/chunks/5 \
  -H "Content-Type: application/json" \
  -d '{"text": "Updated dialogue", "instruct": "Excited, bright energy."}'

# Generate audio for a single chunk
curl -X POST http://127.0.0.1:4200/api/chunks/5/generate

# Standard batch render (parallel individual calls)
curl -X POST http://127.0.0.1:4200/api/generate_batch \
  -H "Content-Type: application/json" \
  -d '{"indices": [0, 1, 2, 3, 4]}'

# Fast batch render (batched TTS calls)
curl -X POST http://127.0.0.1:4200/api/generate_batch_fast \
  -H "Content-Type: application/json" \
  -d '{"indices": [0, 1, 2, 3, 4]}'

# Merge all chunks into final audiobook
curl -X POST http://127.0.0.1:4200/api/merge
```

## Saved Scripts

```bash
# List saved scripts
curl http://127.0.0.1:4200/api/scripts

# Save current script
curl -X POST http://127.0.0.1:4200/api/scripts/save \
  -H "Content-Type: application/json" \
  -d '{"name": "my-novel"}'

# Load a saved script
curl -X POST http://127.0.0.1:4200/api/scripts/load \
  -H "Content-Type: application/json" \
  -d '{"name": "my-novel"}'
```

## Persona Generation

```bash
# Generate personas (LLM analyzes script + VoiceDesign creates voices)
curl -X POST http://127.0.0.1:4200/api/generate_personas

# Advanced mode with custom batch size
curl -X POST http://127.0.0.1:4200/api/generate_personas \
  -H "Content-Type: application/json" \
  -d '{"advanced": true, "batch_size": 40}'

# Check persona generation status
curl http://127.0.0.1:4200/api/status/persona

# Cancel persona generation
curl -X POST http://127.0.0.1:4200/api/cancel_persona
```

### Voice Config with Aliases

Aliases are set via the `alias_of` field in voice config:

```bash
curl -X POST http://127.0.0.1:4200/api/save_voice_config \
  -H "Content-Type: application/json" \
  -d '{
    "ELENA": {"type": "clone", "ref_audio": "designed_voices/previews/preview_123.wav", "ref_text": "Hello there."},
    "YOUNG ELENA": {"type": "clone", "alias_of": "ELENA"}
  }'
```

During generation, "YOUNG ELENA" resolves to "ELENA" and uses her voice config.

## Voice Designer

```bash
# Preview a voice from text description
curl -X POST http://127.0.0.1:4200/api/voice_design/preview \
  -H "Content-Type: application/json" \
  -d '{"description": "A warm, deep male voice with a calm and steady tone", "sample_text": "Hello, how are you?"}'

# Save a designed voice
curl -X POST http://127.0.0.1:4200/api/voice_design/save \
  -H "Content-Type: application/json" \
  -d '{"name": "warm_narrator", "description": "A warm, deep male voice", "sample_text": "Hello.", "preview_file": "designed_voices/previews/preview_123.wav"}'

# List saved designed voices
curl http://127.0.0.1:4200/api/voice_design/list

# Delete a designed voice
curl -X DELETE http://127.0.0.1:4200/api/voice_design/delete/<voice_id>
```

## LoRA Training

```bash
# Upload a training dataset (ZIP with WAV + metadata.jsonl)
curl -X POST http://127.0.0.1:4200/api/lora/upload_dataset \
  -F "file=@dataset.zip" -F "name=my_voice"

# Generate a dataset from Voice Designer
curl -X POST http://127.0.0.1:4200/api/lora/generate_dataset \
  -H "Content-Type: application/json" \
  -d '{
    "name": "gruff_soldier",
    "description": "A gruff middle-aged male soldier",
    "samples": [
      {"emotion": "", "text": "The patrol route is secure."},
      {"emotion": "Barking orders", "text": "Move out! Lock down that perimeter!"},
      {"emotion": "Quiet, tense", "text": "Keep your voice down. Movement in the treeline."}
    ]
  }'

# List datasets
curl http://127.0.0.1:4200/api/lora/datasets

# Delete a dataset
curl -X DELETE http://127.0.0.1:4200/api/lora/datasets/<dataset_id>

# Start training
curl -X POST http://127.0.0.1:4200/api/lora/train \
  -H "Content-Type: application/json" \
  -d '{
    "name": "soldier_voice",
    "dataset_id": "gruff_soldier",
    "epochs": 25,
    "lr": "5e-6",
    "lora_r": 32,
    "lora_alpha": 64,
    "batch_size": 1,
    "gradient_accumulation_steps": 8
  }'

# Check training status
curl http://127.0.0.1:4200/api/status/lora_training

# List trained adapters
curl http://127.0.0.1:4200/api/lora/models

# Test a trained adapter
curl -X POST http://127.0.0.1:4200/api/lora/test \
  -H "Content-Type: application/json" \
  -d '{"adapter_id": "soldier_voice_1234567890", "text": "Moving to position.", "instruct": "Tense, whispering."}'

# Delete an adapter
curl -X DELETE http://127.0.0.1:4200/api/lora/models/<adapter_id>
```

## Dataset Builder

```bash
# List all dataset builder projects
curl http://127.0.0.1:4200/api/dataset_builder/list

# Create a new project
curl -X POST http://127.0.0.1:4200/api/dataset_builder/create \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset"}'

# Update project metadata (description and global seed)
curl -X POST http://127.0.0.1:4200/api/dataset_builder/update_meta \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male narrator", "global_seed": "42"}'

# Update sample rows
curl -X POST http://127.0.0.1:4200/api/dataset_builder/update_rows \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "rows": [{"text": "Hello world.", "emotion": "cheerful"}]}'

# Generate a single sample preview
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_sample \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male voice", "sample_index": 0, "samples": [{"text": "Hello.", "emotion": "cheerful"}]}'

# Batch generate all samples
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_batch \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male voice", "samples": [{"text": "Hello.", "emotion": "cheerful"}]}'

# Check batch generation status
curl http://127.0.0.1:4200/api/dataset_builder/status/my_voice_dataset

# Cancel a running batch generation
curl -X POST http://127.0.0.1:4200/api/dataset_builder/cancel \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset"}'

# Save project as a training dataset
curl -X POST http://127.0.0.1:4200/api/dataset_builder/save \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "ref_sample_index": 0}'

# Delete a project
curl -X DELETE http://127.0.0.1:4200/api/dataset_builder/my_voice_dataset
```

## Audio Download

```bash
# Download merged audiobook
curl http://127.0.0.1:4200/api/audiobook --output audiobook.mp3

# Start Audacity export
curl -X POST http://127.0.0.1:4200/api/export_audacity

# Check export status
curl http://127.0.0.1:4200/api/status/audacity_export

# Download Audacity zip
curl http://127.0.0.1:4200/api/export_audacity --output audacity_export.zip
```

## Python Example

```python
import requests
import time

BASE = "http://127.0.0.1:4200"

def wait_for_task(task_name):
    while True:
        status = requests.get(f"{BASE}/api/status/{task_name}").json()
        if not status.get("running", False):
            return status
        time.sleep(2)

# Upload and generate script
with open("mybook.txt", "rb") as f:
    requests.post(f"{BASE}/api/upload", files={"file": f})
requests.post(f"{BASE}/api/generate_script")
wait_for_task("script_generation")

# Configure voices
requests.post(f"{BASE}/api/save_voice_config", json={
    "NARRATOR": {"type": "custom", "voice": "Ryan", "character_style": "calm narrator"},
    "HERO": {"type": "lora", "adapter_id": "hero_voice_123", "adapter_path": "lora_models/hero_voice_123"}
})

# Fast batch render all chunks
chunks = requests.get(f"{BASE}/api/chunks").json()
indices = [c["id"] for c in chunks]
requests.post(f"{BASE}/api/generate_batch_fast", json={"indices": indices})
wait_for_task("batch_generation")

# Merge and download
requests.post(f"{BASE}/api/merge")
with open("audiobook.mp3", "wb") as f:
    f.write(requests.get(f"{BASE}/api/audiobook").content)
```

## JavaScript Example

```javascript
const BASE = "http://127.0.0.1:4200";

async function waitForTask(taskName) {
  while (true) {
    const res = await fetch(`${BASE}/api/status/${taskName}`);
    const data = await res.json();
    if (!data.running) return data;
    await new Promise(r => setTimeout(r, 2000));
  }
}

// Upload and generate
const formData = new FormData();
formData.append("file", fileInput.files[0]);
await fetch(`${BASE}/api/upload`, { method: "POST", body: formData });
await fetch(`${BASE}/api/generate_script`, { method: "POST" });
await waitForTask("script_generation");

// Configure and render
await fetch(`${BASE}/api/save_voice_config`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    NARRATOR: { type: "custom", voice: "Ryan", character_style: "calm" }
  })
});

const chunks = await (await fetch(`${BASE}/api/chunks`)).json();
await fetch(`${BASE}/api/generate_batch_fast`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ indices: chunks.map(c => c.id) })
});
await waitForTask("batch_generation");

await fetch(`${BASE}/api/merge`, { method: "POST" });
```
