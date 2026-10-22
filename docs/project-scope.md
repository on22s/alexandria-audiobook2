# Keep a later part of the selected book

In **Script → Keep a later part of this book**, find the first saved entry to
keep and select **Preview scope**. The preview shows the original source word
range, exact removed and retained wording, chunk UIDs, voice assignments,
audio integrity checks, saved take versions and files kept outside the scope.
Review it before selecting **Apply reviewed scope**.

This operation removes a prefix from the active script. Its boundary must fall
between saved chunks. It refuses ambiguous identities, altered retained files,
changed preview inputs, pending book recovery and conflicting local or foreign
tasks. It requires the current complete ordered source-word projection to
match the saved script. An edited or incomplete script may need review first.

The original source, cast entries, selected audio and take-history payloads
remain on disk. Chunk positions change; their stable UIDs and unaffected
generation inputs stay. Voice/style timelines move with the retained rows, and
AA2 compares effective voice inputs before and after moving them. Literal
scripts receive a selected-word policy with explicit UID-specific lineage;
original chunk markers and captured take evidence are preserved.

Source comparison uses AA2's existing normalized-word policy. It does not prove
punctuation equality, speaker attribution or acoustic fidelity. Audio checks
bind current bounded, decodable files and saved publication evidence where it
exists. Legacy files do not acquire proof of their original generation inputs.
Listening and speech-content review remain separate.

## Undo and further scope edits

One persistent exact undo record stores the prior state, script, voice config,
chunk snapshot and status-journal bytes. **Undo scope change** restores those
bytes through the existing recoverable book transaction. Before publication,
AA2 checks the reviewed revision and protected file identities again.

Undo refuses after metadata, source, protected audio/history/voice assets or
exports change. Its record stays available for inspection. **Discard undo;
keep current scope** explicitly removes only that reviewed undo record. It
does not delete source, cast, audio or take history. After discard, another
scope edit can retain a later suffix; source offsets continue to refer to the
original prepared source.

Older exports remain on disk. Known unchanged pre-scope MP3, M4B, Audacity and
chapter files cannot be downloaded until rebuilt. Home labels those files as
older scope outputs. A changed file is not thereby certified: normal export
checks and content review still apply. Undo also refuses if an export was
rebuilt or created after scope apply.

Whole-source single-book generation and recovery cannot run while a shortened
scope is active. Undo first, or explicitly select the full source again. New
input/saved-book selection clears the old scope authority and rotates the book
generation as usual. Saved full-book recovery candidates remain hidden while
the shorter scope is active; their files are preserved.

## Bounds and qualification

Scope metadata/source reads are capped at 64 MiB per file; persistent undo admits
at most 32 MiB of original metadata before encoding. The complete preview is
capped at 32 MiB, including at most 8 MiB of retained voice mappings; larger
projects refuse without changes. Selected/history audio uses
the existing 64 MiB, ten-minute mono/stereo snapshot bounds. Voice references
and adapter bundles are bounded at 1 GiB, with at most 4,096 bundle nodes. Export
inventories inspect at most 4,096 nodes per output; large delivery bytes are not
copied into metadata undo. Aliases, hard links and unsupported file types are
refused rather than followed. These are scope-operation bounds, not new limits
on ordinary generation or export.

CPU fixtures cover source mapping, literal lineage, voice timelines, actual
encoded audio/history bytes, stale authority, owned task exclusion, exact undo,
write/replace failures and abrupt process death before/after commit. Saved-take
restoration uses declared CPU serving fixtures. This does not qualify native
model inference, ASR, listening quality or restoration on another host platform.
