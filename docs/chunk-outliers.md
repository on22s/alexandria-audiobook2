# Acoustic sibling outliers (experimental)

In Editor, open the saved findings panel and choose **Check acoustic outliers**.
The CPU-only scan compares rendered takes with the same character's other takes
using the same effective voice inputs (including voice-state timeline changes).
It does not load a model, guess emotion, regenerate audio, or block generation.
Choose **Acoustic outliers** in the findings filter, inspect a line's recorded
metrics, and listen using the existing playback controls before deciding whether
to use Gen. The sibling check is separate from the reference-voice drift check.

The four measurements are active-frame RMS loudness in dBFS (not LUFS), an
energy-envelope syllable-nuclei rate proxy per elapsed second, approximate median
periodic pitch in Hz, and the fraction of active frames with periodic voicing.
Energy nuclei are not a linguistic syllable counter. Autocorrelation can mistake
harmonics or expressive/noisy speech. These are listening hints, not quality,
identity, truncation, emotion, or text-completeness certification.

Each metric needs at least eight finite measurements in its comparison group.
The signed robust score is (value - group median) / max(1.4826 * MAD, floor).
The fixed floors are 3 dB, 1 nucleus/second, 25 Hz, and 0.15 voiced ratio.
An absolute score greater than 2 flags the take. A partial positive remains
flagged with its missing metrics disclosed; silence, missing pitch, small groups,
missing files and failed decoding are unavailable, never clean. No flag is not
proof of a good take. Differences in delivery may be intentional.

The scan covers all rendered takes, decodes each once, and rejects audio outside
the selected book. Limits are 2,000 saved lines, ten minutes per scan, 60 seconds
per take, bounded decoder output, and 20 seconds per media command. Overlong takes
are unavailable rather than silently truncated. Cancel stops owned decoding and
does not save partial results. A failed or cancelled scan leaves earlier saved
evidence alone, so inspect the task message rather than treating it as a fresh
successful check.

At most four takes are decoded concurrently. The line limit is an admission
ceiling, not a completion guarantee; throughput on real books is unqualified.
The deadline still refuses the whole scan without publishing partial results.
Wait for audio rendering to stop before starting; a render that starts during
the scan also refuses it. Reload restores the scan's status and cancel controls.
Invalid saved book/voice inputs require repair, not repeated scan retries.

Publication is one guarded metadata write: book/source/voice configuration and
the entire saved population must still match the captured snapshot. Existing
flags become stale when a sibling is added, removed, reordered, edited or
regenerated. Voice configuration changes also invalidate displayed evidence.
This is metadata currency, not cryptographic verification of externally edited
audio: direct replacement of files without changing the saved take revision is
outside the application's publication contract. Audio is never modified here.

## Instrument evidence and limits

The fresh implementation uses no VoiceStudio source. Tests discriminate known
synthetic pitch, amplitude, envelope-rate and voiced-ratio controls, silence,
flat-MAD floors, small populations and consistent varied synthetic populations.
Known generated controls establish the instrument's arithmetic and failure
behavior; they do **not** qualify expressive real speech. Real-speech listening
qualification and browser/accessibility qualification are not claimed. The UI
therefore labels this feature experimental and requires listening review.
