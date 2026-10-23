# Disk-backed MP3 assembly

Ordinary MP3 merges now snapshot trimmed source PCM to temporary disk storage and consume PCM blocks synchronously. A blocked writer prevents the producer from pulling the next block; no background queue accumulates assembled audio. The staged PCM file is encoded by an owned FFmpeg child. Existing pause overrides, format-promotion order, timeline trimming, mastering and atomic final publication are retained.

Cancellation and write/encoder failures retire owned processes and remove temporary files. Original chunk files and a previous completed export remain unchanged. Scratch storage is required beside the destination for source snapshots and assembled PCM. A single compressed input is still decoded as a whole chunk, so assembly bounds do not establish a bound on individual decoder memory.

The normal block allowance is 64 KiB, with the existing finite wide-frame allowance up to 128 KiB. Unsupported custom segment/layout behavior or a nonstandard converter uses the compatibility path, which may retain whole-book audio in memory. MP3 encoding retains its existing cancellation-driven lifetime; mastering retains its finite deadline.

This continues issue #1088 for ordinary MP3 merging. It does not close the remaining native-platform, large WAV/ZIP64 or custom-layout acceptance gates. Synthetic Linux regression checks compare actual staged PCM and decoded MP3 audio with the previous builder, and exercise slow consumers, short writes, cancellation, child cleanup and preserved prior exports.

## Export pause limit

Authored pauses are limited to 760,000 ms (about 12.7 minutes, or 16,758,000 bytes of 11,025 Hz mono 16-bit silence). The limit is enforced in three places from one definition (`app/export_limits.py`): the per-line `pause_after` request model, the saved speaker-pause settings, and the shared pause resolution used by every export path. The Settings and Editor inputs receive the same maximum from `GET /api/config`, so they cannot drift from the server.

Because the shared resolution is the choke point, the limit applies to full-book WAV/M4B/MP3 streaming as well as the chapter export and legacy whole-buffer paths; those paths now all agree instead of only the allocating ones. The streaming paths would have accepted a larger pause because they allocate in blocks, so this is a compatibility narrowing, not just a memory guard. An existing book or saved setting above the limit fails loudly with the requested pause, the byte size and the limit, rather than exporting or attempting the allocation. Shorten the pause and export again.

The guard bounds one silence allocation, not total chapter memory: chapter export still assembles and exports a whole chapter, and the compatibility path for custom segment behavior still retains whole-book audio. Those broader limits remain under #1088.
