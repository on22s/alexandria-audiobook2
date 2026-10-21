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
snapshot as current audio. Restoring an archived performance into a line is not
available.

The archive allows 4,096 records, 16 per line and 1 GiB of audio payload in total.
Deleted and interrupted copies count until permanently removed. A copy still
needed by a pending replacement or a live chunk-deletion undo cannot be removed.
If admission fails, free archive space or turn automatic history off before
retrying generation. Existing copies stay available when it is off.

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

Snapshots preserve actual saved bytes and recorded inputs. They do not certify
the exact model, reference material or all dependencies used by an earlier render.
Listen to a candidate before using it in an audiobook.
