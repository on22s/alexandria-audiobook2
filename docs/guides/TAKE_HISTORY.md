# Take history

In Editor's Inspector, open **Automatic take history**, choose **Refresh setting**,
then **Turn on**. The project setting is off by default and applies to future
regeneration. You can change it without selecting a line.

With it on, the selected prior performance is kept before a replacement is
generated or installed. Editing words, speaker, instruction or take number keeps
the original saved performance's metadata. Repeated edits do not relabel old audio
with the new words. First-time generation has no prior performance to archive.

Use the Inspector's snapshots controls to audition saved performances, protect a
copy, delete with undo, or remove permanently after confirmation. The project
archive view also includes earlier book generations. Audition does not select a
snapshot as current audio.

On supported Linux hosts, a snapshot with recorded publication evidence for the
current book and line offers **Check & restore**. Save pending edits before using
it. The audio task checks the original inputs, voice and seed, model settings,
pronunciation, synthesis windows, native execution dependencies and actual saved
bytes. Checking may load the native voice model; it does not synthesize new audio.
Use **Cancel audio task** to request cancellation and wait for the task to finish.

A successful restore selects the original encoded bytes, take number and artifact
revision, retaining their generation receipts. It keeps the displaced performance
in history even when automatic history is off. It does not rewrite words, speaker
or delivery instructions. A changed dependency or unavailable proof causes refusal
and preserves the selected performance. Older snapshots, other book/line identities
and unsupported hosts remain available for audition. Protected snapshots can be
restored; deleted snapshots must first be undeleted.

The archive allows 4,096 records, 16 per line and 1 GiB of audio payload in total.
Deleted and interrupted copies count until permanently removed. A copy still
needed by a pending replacement or a live chunk-deletion undo cannot be removed.
If admission fails, free archive space or turn automatic history off before
retrying generation. Existing copies stay available when it is off.
Explicit restoration still requires enough space to retain the displaced take.

If generation is cancelled or publication fails, a retained prior performance
stays protected until a replacement succeeds. After a process interruption, an
incomplete archive copy remains visible and charged; explicitly remove that copy
before retrying. A completed retained copy can be reused on retry even if its old
working audio slot has changed. A damaged policy can be replaced by refreshing
the setting and turning it off, without parsing or rewriting the archive.

Enabling history cannot reconstruct audio already detached by earlier edits.
Rebuilding chunks or loading another book creates a new book/line identity;
automatic retention does not transfer pending line metadata across those changes.
Completed archive copies remain discoverable in the project archive.

Snapshots preserve actual saved bytes and recorded inputs. Snapshotting alone
does not certify the exact dependencies used by an earlier render.
Listen to a candidate before using it in an audiobook.

With automatic history enabled, eligible built-in local CustomVoice renders also
record a bounded publication receipt. This currently requires deterministic
seeds, internal synthesis windows and qualified recovery serving identity. Exact
observed speech input is captured independently of ASR, then bound to the finished
encoded file's hash and decoded shape. Selected voice/model inputs, pronunciation
decisions, window policy, book/line identity and audio revision are bound as well.
Other renders and earlier snapshots remain unproven. Existing snapshots are not
retroactively certified. A receipt is a restoration candidate; the explicit action
must still verify its current dependencies and audio.

Publication receipts and pending predecessor metadata share a 16 MiB book budget.
Each publication receipt is at most 4 KiB; immutable archive record/payload limits
still apply. Valid first-pass audio outside the receipt's 64 MiB, mono/stereo,
192 kHz and ten-minute bounds can publish without proof. New generation replaces its prior receipt atomically with the audio
selection, or clears it when complete proof is unavailable. History browsing does
not load a model, hash selected audio or run speech verification.
