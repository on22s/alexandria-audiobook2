# API reference

Everything the web interface does is an HTTP call, so the app can be scripted. This page shows
the common tasks with examples. The **complete list of all 181 routes**, generated from the
code, is in the README's [API reference](../../README.md#api-reference); the interactive docs are
at `/docs` and the OpenAPI schema at `/openapi.json` on a running app.

## Basics

- **Address:** `http://127.0.0.1:<port>`. Pinokio chooses the port; Docker uses 4200. The
  examples below use `http://127.0.0.1:4200`.
- **Authentication:** off by default. If you set `ALEXANDRIA_AUTH_PASSWORD`, every request
  needs HTTP Basic credentials — see the README's
  [Authentication](../../README.md#authentication-optional) section.
- **Background tasks:** long jobs start in the background. Poll
  `GET /api/status/<task>` until `running` is false, or `GET /api/status/eta` for the progress and
  time left of whatever is running.

Task names you can poll:

| task | started by |
|---|---|
| `script` | generating a script |
| `review` / `batch_review` | reviewing one script / several |
| `nicknames` | finding nicknames |
| `persona` | generating personas |
| `voices` | suggesting LoRA voices |
| `audio` | rendering audio |
| `audacity_export` / `chapter_export` | exports |
| `drift_check` | Check Voices |
| `lora_training` / `lora_test` | training / testing a voice adapter |
| `voice_design` | Voice Designer previews |
| `dataset_builder` | Dataset Builder batch generation |
| `preparer` / `batch_preparer` | the Preparer |
| `voicelab` | the Voice Lab pipeline |
| `benchmark` | the benchmark harness |

## Settings

```bash
# read the current settings (API keys are redacted)
curl http://127.0.0.1:4200/api/config

# the default prompt texts
curl http://127.0.0.1:4200/api/default_prompts

# save settings (send the full settings object you read above, with your changes)
curl -X POST http://127.0.0.1:4200/api/config \
  -H "Content-Type: application/json" \
  -d @config.json
```

## Script generation

```bash
# upload a book; it becomes the active book
curl -X POST http://127.0.0.1:4200/api/upload -F "file=@mybook.txt"

# generate a script from the active book (all fields optional)
curl -X POST http://127.0.0.1:4200/api/generate_script \
  -H "Content-Type: application/json" \
  -d '{"first_person_narrator": null, "strip_front_matter": true, "start_over": false}'

# follow its progress
curl http://127.0.0.1:4200/api/status/script
curl http://127.0.0.1:4200/api/status/eta

# review the script (a second model pass that fixes common mistakes)
curl -X POST http://127.0.0.1:4200/api/review_script
curl http://127.0.0.1:4200/api/status/review

# the current script
curl http://127.0.0.1:4200/api/annotated_script
```

`generate_script` always works on the active book — the last one uploaded, or one chosen with
`POST /api/uploads/select`. It does not take a file name.

## Voices

```bash
# the speakers and their current voice settings
curl http://127.0.0.1:4200/api/voices

# save voice settings
curl -X POST http://127.0.0.1:4200/api/save_voice_config \
  -H "Content-Type: application/json" \
  -d '{
    "NARRATOR": {"type": "custom", "voice": "Ryan", "character_style": "calm, measured narration"},
    "ELENA":    {"type": "clone", "ref_audio": "designed_voices/previews/preview_123.wav", "ref_text": "Hello there."},
    "MARCUS":   {"type": "lora", "adapter_id": "dark_voice_123", "adapter_path": "lora_models/dark_voice_123", "character_style": "menacing undertone"},
    "SOLDIER":  {"type": "design", "description": "Young strong soldier"}
  }'
```

### Voice settings fields

| field | used by | meaning |
|---|---|---|
| `type` | all | `custom`, `builtin_lora`, `clone`, `lora`, `design` or `ensemble` |
| `voice` | custom | the built-in voice name (Aiden, Dylan, Eric, …) |
| `character_style` | custom, lora | the identity anchor sent with every line |
| `style_timeline` | custom, lora | change points: `{"from_index": N, "character_style": "..."}` |
| `seed` | all | random seed; `"-1"` for random |
| `ref_audio`, `ref_text` | clone | the reference audio and its exact transcript |
| `adapter_id`, `adapter_path` | lora | the adapter |
| `description` | design | the base voice description |
| `members` | ensemble | the speakers who speak together |
| `alias_of` | any | use another speaker's voice |

An alias:

```bash
curl -X POST http://127.0.0.1:4200/api/save_voice_config \
  -H "Content-Type: application/json" \
  -d '{
    "ELENA":       {"type": "clone", "ref_audio": "designed_voices/previews/preview_123.wav", "ref_text": "Hello there."},
    "YOUNG ELENA": {"type": "clone", "alias_of": "ELENA"}
  }'
```

When audio is rendered, "YOUNG ELENA" uses ELENA's voice.

## Personas

```bash
# generate personas for characters that have no voice yet
curl -X POST http://127.0.0.1:4200/api/generate_personas \
  -H "Content-Type: application/json" \
  -d '{"new_only": true}'

# large casts: process speakers in batches
curl -X POST http://127.0.0.1:4200/api/generate_personas \
  -H "Content-Type: application/json" \
  -d '{"advanced": true, "batch_size": 40}'

curl http://127.0.0.1:4200/api/status/persona
curl -X POST http://127.0.0.1:4200/api/cancel_persona
```

## Chunks and rendering

```bash
# all chunks
curl http://127.0.0.1:4200/api/chunks

# edit a chunk (any of text, instruct, speaker, pause_after)
curl -X POST http://127.0.0.1:4200/api/chunks/5 \
  -H "Content-Type: application/json" \
  -d '{"text": "Updated dialogue", "instruct": "Excited, bright energy."}'

# render one chunk
curl -X POST http://127.0.0.1:4200/api/chunks/5/generate

# render several: batched (the fast path used with local TTS)
curl -X POST http://127.0.0.1:4200/api/generate_batch_fast \
  -H "Content-Type: application/json" \
  -d '{"indices": [0, 1, 2, 3, 4]}'

# render several: one request per line, in parallel
curl -X POST http://127.0.0.1:4200/api/generate_batch \
  -H "Content-Type: application/json" \
  -d '{"indices": [0, 1, 2, 3, 4]}'

curl http://127.0.0.1:4200/api/status/audio

# merge into the finished audiobook
curl -X POST http://127.0.0.1:4200/api/merge
```

## Saved scripts

```bash
curl http://127.0.0.1:4200/api/scripts

curl -X POST http://127.0.0.1:4200/api/scripts/save \
  -H "Content-Type: application/json" -d '{"name": "my-novel"}'

curl -X POST http://127.0.0.1:4200/api/scripts/load \
  -H "Content-Type: application/json" -d '{"name": "my-novel"}'
```

## Voice Designer

```bash
# preview a voice from a description
curl -X POST http://127.0.0.1:4200/api/voice_design/preview \
  -H "Content-Type: application/json" \
  -d '{"description": "A warm, deep male voice with a calm and steady tone", "sample_text": "Hello, how are you?"}'

# save a preview as a designed voice
curl -X POST http://127.0.0.1:4200/api/voice_design/save \
  -H "Content-Type: application/json" \
  -d '{"name": "warm_narrator", "description": "A warm, deep male voice", "sample_text": "Hello.", "preview_file": "designed_voices/previews/preview_123.wav"}'

curl http://127.0.0.1:4200/api/voice_design/list
curl -X DELETE http://127.0.0.1:4200/api/voice_design/<voice_id>
```

## Voice (LoRA) training

```bash
# upload a dataset: a ZIP of WAV files plus metadata.jsonl
curl -X POST http://127.0.0.1:4200/api/lora/upload_dataset -F "file=@dataset.zip"

curl http://127.0.0.1:4200/api/lora/datasets
curl -X DELETE http://127.0.0.1:4200/api/lora/datasets/<dataset_id>

# train (the values shown are the defaults; see the Training guide before changing lr)
curl -X POST http://127.0.0.1:4200/api/lora/train \
  -H "Content-Type: application/json" \
  -d '{
    "name": "soldier_voice",
    "dataset_id": "gruff_soldier",
    "epochs": 5,
    "lr": 1e-6,
    "lora_r": 32,
    "lora_alpha": 128,
    "batch_size": 1,
    "gradient_accumulation_steps": 8,
    "language": "english"
  }'

curl http://127.0.0.1:4200/api/status/lora_training
curl http://127.0.0.1:4200/api/lora/models

# test a trained adapter
curl -X POST http://127.0.0.1:4200/api/lora/test \
  -H "Content-Type: application/json" \
  -d '{"adapter_id": "soldier_voice_1234567890", "text": "Moving to position.", "instruct": "Tense, whispering."}'

curl -X DELETE http://127.0.0.1:4200/api/lora/models/<adapter_id>
```

Do not raise `lr` to 5e-6: voices trained at that rate never stop talking. See the
[Training guide](Training-Guide.md).

## Dataset Builder

```bash
curl http://127.0.0.1:4200/api/dataset_builder/list

curl -X POST http://127.0.0.1:4200/api/dataset_builder/create \
  -H "Content-Type: application/json" -d '{"name": "my_voice_dataset"}'

# the description and global seed
curl -X POST http://127.0.0.1:4200/api/dataset_builder/update_meta \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male narrator", "global_seed": "42"}'

# the sample rows
curl -X POST http://127.0.0.1:4200/api/dataset_builder/update_rows \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "rows": [{"text": "Hello world.", "emotion": "cheerful"}]}'

# render one sample
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_sample \
  -H "Content-Type: application/json" \
  -d '{"dataset_name": "my_voice_dataset", "sample_index": 0, "description": "A warm male voice, cheerful", "text": "Hello world.", "seed": -1}'

# render several samples in the background
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_batch \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male voice", "samples": [{"text": "Hello.", "emotion": "cheerful"}]}'

curl http://127.0.0.1:4200/api/dataset_builder/status/my_voice_dataset
curl -X POST http://127.0.0.1:4200/api/dataset_builder/cancel

# save as a training dataset; ref_index picks the reference sample
curl -X POST http://127.0.0.1:4200/api/dataset_builder/save \
  -H "Content-Type: application/json" -d '{"name": "my_voice_dataset", "ref_index": 0}'

curl -X DELETE http://127.0.0.1:4200/api/dataset_builder/my_voice_dataset
```

## Downloads and export

```bash
# the merged audiobook
curl http://127.0.0.1:4200/api/audiobook --output audiobook.mp3

# Audacity: start the export, wait, then download the zip
curl -X POST http://127.0.0.1:4200/api/export_audacity
curl http://127.0.0.1:4200/api/status/audacity_export
curl http://127.0.0.1:4200/api/export_audacity --output audacity_export.zip
```

## A whole book, from Python

```python
import requests, time

BASE = "http://127.0.0.1:4200"

def wait_for(task):
    while requests.get(f"{BASE}/api/status/{task}").json().get("running"):
        time.sleep(5)

with open("mybook.txt", "rb") as f:
    requests.post(f"{BASE}/api/upload", files={"file": f})
requests.post(f"{BASE}/api/generate_script", json={})
wait_for("script")

requests.post(f"{BASE}/api/save_voice_config", json={
    "NARRATOR": {"type": "custom", "voice": "Ryan", "character_style": "calm narrator"},
    "HERO": {"type": "lora", "adapter_id": "hero_voice_123", "adapter_path": "lora_models/hero_voice_123"},
})

chunks = requests.get(f"{BASE}/api/chunks").json()
requests.post(f"{BASE}/api/generate_batch_fast", json={"indices": [c["id"] for c in chunks]})
wait_for("audio")

integrity = requests.get(f"{BASE}/api/editor/integrity")
integrity.raise_for_status()
integrity = integrity.json()
if integrity['status'] != 'verified':
    print(integrity)  # inspect differences or the unavailable-source reason
    if input('Continue without a verified source match? [yes/no] ') != 'yes':
        raise SystemExit('Merge declined')
response = requests.post(f"{BASE}/api/merge", json={
    'integrity_confirmation': integrity['snapshot']
})
response.raise_for_status()  # changed source/book/chunks require a fresh check
wait_for("audio")
with open("audiobook.mp3", "wb") as f:
    f.write(requests.get(f"{BASE}/api/audiobook").content)
```

## The same, from JavaScript

```javascript
const BASE = "http://127.0.0.1:4200";

async function waitFor(task) {
  while ((await (await fetch(`${BASE}/api/status/${task}`)).json()).running) {
    await new Promise(r => setTimeout(r, 5000));
  }
}

const form = new FormData();
form.append("file", fileInput.files[0]);
await fetch(`${BASE}/api/upload`, { method: "POST", body: form });
await fetch(`${BASE}/api/generate_script`, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
});
await waitFor("script");

await fetch(`${BASE}/api/save_voice_config`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ NARRATOR: { type: "custom", voice: "Ryan", character_style: "calm" } })
});

const chunks = await (await fetch(`${BASE}/api/chunks`)).json();
await fetch(`${BASE}/api/generate_batch_fast`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ indices: chunks.map(c => c.id) })
});
await waitFor("audio");

const integrityResponse = await fetch(`${BASE}/api/editor/integrity`);
if (!integrityResponse.ok) { throw new Error('Source check failed'); }
const integrity = await integrityResponse.json();
if (integrity.status !== 'verified') {
  console.log(integrity); // inspect differences or the unavailable-source reason
  if (!window.confirm('Continue without a verified source match?')) {
    throw new Error('Merge declined');
  }
}
const mergeResponse = await fetch(`${BASE}/api/merge`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ integrity_confirmation: integrity.snapshot })
});
if (!mergeResponse.ok) { throw new Error('Merge refused; check the current snapshot again'); }
```

`POST /api/merge` accepts a bodyless request only when the current editor text
matches the selected original source. Differences or unavailable provenance
return HTTP 409. Inspect `GET /api/editor/integrity` and explicitly confirm its
current `snapshot` to proceed; the server rechecks it before exporting. A saved
script-JSON book without its original source remains unavailable and requires
confirmation on each merge. This check compares text, not the words in WAVs.


Editor Undo uses the `undo_token` returned by `DELETE /api/chunks/{index}`.
Send that token, the returned `deleted` object as `chunk`, and the original
`at_index` to `POST /api/chunks/restore` within 60 seconds. A receipt can restore
only its original deletion in the same active book generation, once; stale,
modified or repeated requests return 409.

Batch upload callers can use `POST /api/upload?select_active=false` to store a
source without changing the active book. Ordinary uploads select it by default.
