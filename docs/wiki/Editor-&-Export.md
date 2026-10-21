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

## Pronunciation

### Correcting one pronunciation

Open **Pronunciation**, load shared rules, and **Preview counts and line voices**.
Choose a line and **Load line rules**. Enter the original spelling and spoken replacement,
then **Find original spelling** and add the intended match as a draft. Matches refer to the
text prepared for speech, after cue and symbol cleanup. The script itself stays unchanged.

Review the draft, choose **Approved**, and **Preview line** before **Save line rules**.
Draft and rejected rules do not affect speech. Approved occurrence rules take priority over
shared rules for that occurrence; inserted replacements are never substituted a second time.
Optional restrictions select the authored speaker, the exact assigned voice or ensemble,
language, or engine mode. A phonetic hint needs an explicit spoken fallback for plain-text
engines; otherwise the preview reports that it was bypassed.

Saving line rules waits for active work to finish or be cancelled. Only changed effective
speech makes a line pending, and its prior audio file is retained. Notes and inactive drafts
keep existing audio. Changing language or engine mode also makes affected conditional lines
pending. Preview does not load a TTS model; auditions above generate disposable audio using
the chosen line voice. Listen to an audition and the rendered line to judge the reading.

Rules belong to the current Editor line identities. Reordering a line preserves its rules;
editing its text requires reviewing new matches. Loading or rebuilding a saved script creates
new lines and does not transfer occurrence rules. Overlapping approved occurrences and stale
matches are refused. A generated take records the applied and bypassed decisions with its
audio revision, so later review notes cannot relabel an earlier performance.

## Rendering audio

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

## Saved take snapshots

Inspect a rendered line and open **Saved take snapshots**. Choose **Save this take**
before changing its words, voice or seed, or regenerating it. This saves an immutable
copy of the selected audio plus its recorded text, delivery, seed counter and available
evidence. Use **This line's snapshots** to compare copies, or **All project snapshots**
to find copies from earlier lines and book generations. Playback verifies the saved
bytes and leaves the selected line audio unchanged.

**Protect** prevents deletion. **Delete with undo** hides a snapshot from playback but
keeps its bytes; **Undo deletion** restores it. **Remove permanently…** is a separate,
confirmed action that frees storage and cannot be undone. Current, referenced and
protected snapshot files cannot be removed. No automatic pruning runs.

The project archive permits 16 records per book/line identity, 4,096 records and 1 GiB
of payload in total. Each copy is limited to 64 MiB, mono/stereo and ten minutes.
Metadata is limited to 128 KiB per record and 16 MiB per manifest, plus one bounded
16 MiB pending metadata write. Deleted and interrupted copies count until explicitly
removed. A failed or interrupted copy never replaces the selected audio; its charged
incomplete record remains visible for cleanup. Unknown or damaged archive metadata
is preserved for inspection instead of being silently repaired or pruned.

Saving is explicit in this version. Automatic retention and restoring a snapshot as
the selected take are not yet available. Legacy recordings may lack exact generation
dependencies, so snapshots remain audition-only rather than certifying a voice/model
match. Loading a saved script creates new line identities; old snapshots remain in
**All project snapshots** and are never assigned to new lines by position or text.

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

### Export integrity checks

The Result tab checks finished MP3, M4B and chapter audio before replacing an earlier
delivery. These checks compare the encoded audio with an independent source-frame plan,
decode it for damaged or non-finite samples, and check M4B chapter markers and authored
book metadata. A failed or cancelled export preserves the earlier delivery.

Under **Export integrity checks**, set the silence threshold and maximum unexpected gap
for the next export. Long silence produces a warning by default; enable **Reject exports
with unexpected long silence** to refuse it instead. Authored inter-line pauses are
excluded. These checks do not certify speech content or pronunciation.

**Show checks for this book** displays completed checks separately from rejected attempts,
including frame counts and silence or chapter time ranges. Results are retained for this
app session, up to 32 deliveries and 16 rejected attempts per export type. The view omits
receipts when file metadata changes; opening it does not rehash or decode the audio.
Its SHA-256 values describe the bytes at the completed check. Export publication itself
checks complete file bytes and source identity.

Audacity checks ZIP members, WAV headers, exact track frames, LOF/labels and archive CRCs.
Its inactive-speaker gaps are intentional and are not classified as missing narration.

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
