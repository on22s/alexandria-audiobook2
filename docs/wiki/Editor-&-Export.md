# Editor & export

The Editor tab shows your audiobook line by line ("chunks"), with tools to edit, re-render,
listen and check each one. The Result tab exports the finished book.

## The chunk editor

Each chunk shows:

- **Status** — Pending (no audio yet), Done, or Error;
- **Speaker** — editable;
- **Text** — the spoken line or narration, editable;
- **Instruct** — the delivery instruction, editable.

Each chunk can also be played, re-rendered on its own, and inserted or deleted.

### Editing a line

1. Click a field and edit it; changes save automatically.
2. Click the chunk's **generate** button to re-render just that line — useful for fixing a line
   after an edit, trying a different instruction, or comparing voices.
3. Or click **Render Pending** to render every chunk that still needs audio.

## Rendering

- **Render Pending** renders every chunk that has no audio yet.
- **Regenerate All** re-renders every chunk.
- With the built-in TTS (`local` mode) both use batched rendering, about 3–6× real time. With
  an external TTS server they send one request per line in parallel. See
  [Batch generation](Batch-Generation.md).
- Both respect the GPU lock: they will not start while script generation is using the card,
  unless the language model is marked as not on this GPU.
- **Cancel** stops a render; finished chunks are kept.

## Checking the audio

- **Check Voices** compares each rendered chunk with its speaker's reference voice (ECAPA
  similarity) and flags chunks that drifted. Tick **flagged only** to see just those.
- **Text integrity** shows, word by word, where the script differs from the source text.
- Every generated file is validated as real audio; a broken file is flagged on its chunk.

## Listening

- **Play** a single chunk with its play button.
- **Play Sequence** plays chunks in order, with natural pauses:
  - between different speakers — 500 ms by default (**Speaker Change Pause**);
  - between lines from the same speaker — 250 ms by default (**Same Speaker Pause**).

  Both are set in the Setup tab.

## Merging

Click **Merge All** to join all chunks that have valid audio into one audiobook, in order,
with the pauses above. Silence at the start and end of each line (dead air — on LoRA voices a
median of 310–340 ms) is trimmed when the chunks are joined, so the pauses you set are the
pauses you hear. The result appears in the Result tab.

## Export options

### The combined audiobook

- One MP3 at 128 kbps with every voice and the natural pauses.
- Download it from the **Result** tab; the file is `cloned_audiobook.mp3`.

### M4B with chapters

**Export M4B** makes an audiobook file with chapter markers. You can set the **Title**,
**Author**, **Narrator**, **Year**, **Description** and **Cover Image**, and choose
**Per-chunk chapters**.

### Chapter files

**Export chapters** writes each chapter as its own MP3 or WAV file, named by a template such as
`{chapter_number} - {chapter_name}`. Options include number padding, book and series names,
volume, a chapter list, per-chunk files, changed chapters only, and a preview of the file
names. The files and a zip of them are in `chapter_exports/`.

### Individual lines

Each line is also saved as its own file in `voicelines/`, numbered in reading order and named by
speaker, ready to import into audio software:

```
voicelines/
├── voiceline_0001_narrator.mp3
├── voiceline_0002_elena.mp3
├── voiceline_0003_marcus.mp3
└── ...
```

### Audacity

**Export to Audacity** (Result tab) makes one WAV track per speaker for multi-track editing:

```
audacity_export.zip
├── project.lof       # open in Audacity to import every track at once
├── labels.txt        # import via File > Import > Labels for line annotations
├── narrator.wav      # full-length track with only the NARRATOR's audio
├── elena.wav         # full-length track with only ELENA's audio
├── marcus.wav        # full-length track with only MARCUS's audio
└── ...
```

To use it:

1. Download and unzip `audacity_export.zip`.
2. Open `project.lof` in Audacity; every speaker's track is imported.
3. Import `labels.txt` via **File > Import > Labels** for the line annotations.

Every track is padded with silence to the same length, so playing them all together sounds
exactly like the merged MP3. This is useful for per-character volume, effects on one speaker,
adjusting the timing between lines, and professional post-production.

## Broken or tiny MP3 files

A 428-byte MP3 means ffmpeg is missing or cannot encode MP3. The installer builds ffmpeg into
the app's environment, so this usually means the app is running outside `app/env`. Start it
from Pinokio (or activate `app/env`) and render again. See
[Troubleshooting](Troubleshooting.md).
