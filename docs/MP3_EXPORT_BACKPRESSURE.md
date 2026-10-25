# Disk-backed MP3 assembly

Ordinary MP3 merges now snapshot trimmed source PCM to temporary disk storage and consume PCM blocks synchronously. A blocked writer prevents the producer from pulling the next block; no background queue accumulates assembled audio. The staged PCM file is encoded by an owned FFmpeg child. Existing pause overrides, format-promotion order, timeline trimming, mastering and atomic final publication are retained.

Cancellation and write/encoder failures retire owned processes and remove temporary files. Original chunk files and a previous completed export remain unchanged. Scratch storage is required beside the destination for source snapshots and assembled PCM. A single compressed input is still decoded as a whole chunk, so assembly bounds do not establish a bound on individual decoder memory.

The normal block allowance is 64 KiB, with the existing finite wide-frame allowance up to 128 KiB. Unsupported custom segment/layout behavior or a nonstandard converter uses the compatibility path, which may retain whole-book audio in memory. MP3 encoding retains its existing cancellation-driven lifetime; mastering retains its finite deadline.

This continues issue #1088 for ordinary MP3 merging. It does not close the remaining native-platform, large WAV/ZIP64 or custom-layout acceptance gates. Synthetic Linux regression checks compare actual staged PCM and decoded MP3 audio with the previous builder, and exercise slow consumers, short writes, cancellation, child cleanup and preserved prior exports.

## Export pause limit

Authored pauses are limited to 760,000 ms (about 12.7 minutes, or 16,758,000 bytes of 11,025 Hz mono 16-bit silence). The limit is enforced in three places from one definition (`app/export_limits.py`): the per-line `pause_after` request model, the saved speaker-pause settings, and the shared pause resolution used by every export path. The Settings and Editor inputs receive the same maximum from `GET /api/config`, so they cannot drift from the server.

Because the shared resolution is the choke point, the limit applies to full-book WAV/M4B/MP3 streaming as well as the chapter export and legacy whole-buffer paths; those paths now all agree instead of only the allocating ones. The streaming paths would have accepted a larger pause because they allocate in blocks, so this is a compatibility narrowing, not just a memory guard. An existing book or saved setting above the limit fails loudly with the requested pause, the byte size and the limit, rather than exporting or attempting the allocation. Shorten the pause and export again.

The guard bounds one silence allocation, not total chapter memory: chapter export still assembles and exports a whole chapter, and the compatibility path for custom segment behavior still retains whole-book audio. Those broader limits remain under #1088.

## Custom/unknown fallback: explicit deferral (#1088 item 12)

This is the deliberate compatibility path, not a hidden streaming success. The
full-book WAV, M4B and MP3 writers return `None` (or MP3 skips its bounded
writer) when the input is not one of the layouts the staged producer can
preserve:

- a non-plain segment object, such as an `AudioSegment` subclass or an unknown
  custom object;
- a layout whose frame cannot fit the bounded block allowance, or whose sample
  width is not 1, 2 or 4 bytes;
- for MP3, a pydub converter whose basename is not `ffmpeg`/`ffmpeg.exe`; or
- a speaker/segment mismatch (this is a caller bug and now raises instead of
  entering the compatibility path).

Ordinary PCM layouts accepted by `is_bounded_pcm_supported` keep the staged
path, including 8-bit, multichannel and wide frames within the finite
allowance. The compatibility cases use `_get_legacy_export_audio`, which may
retain whole-book audio for full-book WAV/M4B/MP3. Chapter export does not use
those writers at all: it always assembles one chapter with
`combine_audio_with_pauses`, so its bound is a whole chapter in memory. The
authored pause itself is still limited by the shared pause guard; that does not
bound the assembled audio or decoded source PCM. The M4B and MP3 fallback paths
now print a `compatibility fallback (whole-buffer assembly)` line so an operator
can tell which path ran.

We are explicitly **deferring** the broad stateful producer for those
custom/unknown inputs rather than changing their dispatch. Preserving subclass
methods and valid legacy formats matters more here than silently routing them
through a different buffer model. An earlier broad compatibility guard was
rejected because it broke valid mixed-format, 8-bit, low-rate, multichannel and
subclass cases; the current narrow streaming eligibility is intentionally
conservative. The deferred work also depends on the unresolved native
macOS/current-main, single-source decoder-residency, system-temp and
compatibility gates tracked by #1088.

Consequences:

- Ordinary PCM that passes `is_bounded_pcm_supported` is staged and consumed in
  bounded blocks.
- Custom/unknown/over-wide inputs keep their previous whole-buffer behavior and
  their existing output compatibility.
- Chapter export remains whole-chapter regardless of the segment dispatch.
- #1088 item 2 is not claimed as a whole-app memory bound, and item 12 remains
  an explicit deferral until the producer can preserve that dispatch and pass
  the remaining compatibility/resource gates.
