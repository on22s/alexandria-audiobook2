# Labelled listening measurement lab

A self-contained browser page for measuring a short WAV excerpt and recording what you heard. Open `listening-lab.html` locally in a current Chromium or Firefox browser. It measures source-clock PCM samples; it does not test a TTS model, grade a voice, or decide whether a pause was intended.

Three built-in public-domain clips are embedded in the page. You can also load your own PCM WAV (PCM16/24/32 or Float32, up to 60 s and 20 MiB, one or two channels). Files stay local; nothing is uploaded.

## Use

1. Choose a built-in sample or load a local WAV.
2. Press Play and listen. Nothing autoplays.
3. Read the measured levels, and use the optional quiet-section search to select a section.
4. Answer the pause-length and background questions if they apply, and add notes.
5. Copy measurements + notes, or download the JSON.

Browser details and local filenames are omitted from exports; your notes are included. Nothing is saved automatically, so copy or download before reloading.

## What the numbers mean

- **Average signal level (RMS):** `20 log10(sqrt(mean(sample^2)))` per channel. More negative is quieter. Digital zero exports as `null`.
- **Loudest sample:** `20 log10(max(abs(sample)))`; a sample peak, not true peak.
- **Quiet sections:** the maximum per-channel block mean-square power is compared with `10^(thresholdDbfs/10)` using `<=`; runs shorter than the minimum duration are dropped. The trailing partial block is dropped, not zero-padded.
- **Regions:** half-open source-frame intervals, `floor(start*rate)` to `ceil(end*rate)`.

## Fixtures

All three built-in clips are WAV excerpts derived from one public LibriVox recording. The source URL, exact source-MP3 hash, fixture WAV hashes and license scope are recorded in [PROVENANCE.md](PROVENANCE.md).

Each fixture's measured levels cover the constructed pause and up to one second immediately before and after. Those windows include speech; they are not noise-only or perceived-loudness levels. The two constructed pauses are much quieter than the surrounding narration, so the clips do not isolate pause duration and are neither recordings of this project's TTS output nor natural-intent ground truth.

## Limits

- Peak/near-full-scale counts are not clipping diagnosis. Above-full-scale counts can be nonzero for Float32 sources, not integer PCM.
- The decoded-sample session cap (8 clips / 64 MiB of retained Float32 samples) is not a total browser-memory cap: source blobs, in-flight decoding and playback copies are not counted.
- No LUFS, true peak, word accuracy, pronunciation, perceptual quality or false-positive/miss rate is measured.
- Export omits browser details and local filenames but includes your notes. Review before sharing. A download request is not confirmation of a saved file.
- No browser, hardware, clipboard-permission, download-save or layout verification is claimed. The Node verifiers use deterministic DOM and AudioContext stubs.
- Accessibility is partial: labels, live regions and focus styles exist, but no full screen-reader or assistive-technology audit is claimed.
- Playback may resample to the device clock, and the browser may copy the decoded buffer; file measurements are not sound-pressure measurements.
- No model/pilot acceptance, capture authorization, publication clearance or audio-quality acceptance is claimed.

## Validation

Requires Node.js and Python 3.10+ (the independent reference uses only the standard library; set `PYTHON` to choose the interpreter).

```sh
node verify-lab.cjs
node verify-interface.cjs
node verify-playback-race.cjs
```

- `verify-lab.cjs` checks the instrument calibration, RIFF decoding, quiet-run boundaries, exact WAV-byte hashes, fixture metadata meaning, and an independent Python cross-check of the measurement numbers.
- `verify-interface.cjs` drives the real interface and task module against a deterministic DOM/clipboard/download stub, and checks that the three inline modules are byte-identical to their files.
- `verify-playback-race.cjs` checks pending-playback ordering with an AudioContext stub.

`.github/workflows/listening-lab.yml` runs all three on Ubuntu, Windows and macOS.
