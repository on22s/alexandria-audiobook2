# Editor & Export

The Editor tab provides a chunk-by-chunk view of your audiobook with tools for editing, regeneration, preview, and export.

## Chunk Editor

Each chunk shows:
- **Status** — Pending (no audio), Done (generated), or Error
- **Speaker** — Character name (editable)
- **Text** — Spoken dialogue or narration (editable)
- **Instruct** — TTS voice direction (editable)

### Editing Workflow

1. Click any field to edit it inline
2. Changes are saved automatically
3. After editing, click **Generate** on that chunk to regenerate its audio
4. Or use **Batch Render Pending** to regenerate all chunks that need audio

## Render Modes

### Render Pending (Standard)
Sends individual TTS calls in parallel using the configured worker count.
- Per-speaker seeds for reproducible output
- Works with all voice types

### Batch (Fast)
High-speed batched rendering. See [[Batch Generation]] for details.
- 3-6x real-time throughput
- Chunks grouped by voice type and sub-batched by text length
- Single batch seed (configure in Setup)

### Single Chunk Generation
Click the generate button on any individual chunk to regenerate just that line. Useful for:
- Fixing a single line after editing text or instruct
- Testing different instruct directions
- A/B comparing voice options

## Audio Preview

- **Play** individual chunks by clicking the play button
- **Play Sequence** — Plays all chunks in order with natural pauses:
  - Configurable pause between different speakers (default 500ms)
  - Configurable pause between consecutive same-speaker segments (default 250ms)
  - Both values can be adjusted in the Setup tab under **Speaker Change Pause** and **Same Speaker Pause**

## Merging

Click **Merge All** to combine all generated chunks into a single audiobook file with natural pauses. The result appears in the Result tab.

**Requirements:**
- All chunks must have generated audio (status: Done)
- Chunks are merged in order with automatic pauses

## Export Options

### Combined Audiobook
A single MP3 file with all voices and natural pauses.
- Downloaded from the **Result** tab
- File: `cloned_audiobook.mp3`

### Individual Voicelines
Separate MP3 files per line, stored in the `voicelines/` directory:
```
voicelines/
├── voiceline_0001_narrator.mp3
├── voiceline_0002_elena.mp3
├── voiceline_0003_marcus.mp3
└── ...
```
Files are numbered in timeline order with speaker names for easy import into DAWs.

### Audacity Export
One-click export of per-speaker WAV tracks for multi-track editing.

Click **Export to Audacity** in the Result tab to generate:
```
audacity_export.zip
├── project.lof       # Open in Audacity to auto-import all tracks
├── labels.txt        # Import via File > Import > Labels for annotations
├── narrator.wav      # Full-length track with only NARRATOR audio
├── elena.wav         # Full-length track with only ELENA audio
├── marcus.wav        # Full-length track with only MARCUS audio
└── ...
```

**How to use:**
1. Download and unzip `audacity_export.zip`
2. Open `project.lof` in Audacity — all speaker tracks import automatically
3. Import `labels.txt` via File > Import > Labels for chunk annotations
4. Each track is padded to the same total duration with silence — playing all tracks simultaneously sounds identical to the merged MP3

This format is ideal for:
- Per-character volume adjustment
- Adding effects to specific speakers
- Fine-tuning timing between dialogue
- Professional post-production

### MP3 Encoding Note
If you get broken or tiny (428 byte) MP3 files, conda's bundled ffmpeg may lack the MP3 encoder. Fix:
```bash
conda install -c conda-forge ffmpeg
# or remove conda's ffmpeg to use system one:
conda remove ffmpeg
```
Alexandria auto-detects this and falls back to WAV output.
