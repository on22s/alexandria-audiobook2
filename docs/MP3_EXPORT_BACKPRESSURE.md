# Disk-backed MP3 assembly

Ordinary MP3 merges now snapshot trimmed source PCM to temporary disk storage and consume PCM blocks synchronously. A blocked writer prevents the producer from pulling the next block; no background queue accumulates assembled audio. The staged PCM file is encoded by an owned FFmpeg child. Existing pause overrides, format-promotion order, timeline trimming, mastering and atomic final publication are retained.

Cancellation and write/encoder failures retire owned processes and remove temporary files. Original chunk files and a previous completed export remain unchanged. Scratch storage is required beside the destination for source snapshots and assembled PCM. A single compressed input is still decoded as a whole chunk, so assembly bounds do not establish a bound on individual decoder memory.

The normal block allowance is 64 KiB, with the existing finite wide-frame allowance up to 128 KiB. Unsupported custom segment/layout behavior or a nonstandard converter uses the compatibility path, which may retain whole-book audio in memory. MP3 encoding retains its existing cancellation-driven lifetime; mastering retains its finite deadline.

This continues issue #1088 for ordinary MP3 merging. It does not close the remaining native-platform, large WAV/ZIP64 or custom-layout acceptance gates. Synthetic Linux regression checks compare actual staged PCM and decoded MP3 audio with the previous builder, and exercise slow consumers, short writes, cancellation, child cleanup and preserved prior exports.
