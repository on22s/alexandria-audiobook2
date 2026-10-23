# Review character identities and source quotes

In **Script → Review character identities and source quotes**, load the selected
book. Start from its cast, add characters, or import an exported approved roster.
Edit names and aliases, paste supporting quotes, and explicitly choose their
locations with **Find quote in source**. Review the proposed identity changes,
check the confirmation, then approve. A quote identifies words in the source;
the reviewer decides which person those words support.

Approval records stable character IDs and projects the reviewed names and aliases
into the cast used for future script generation. It preserves accepted script
wording, voice assignments, chunk IDs, selected audio and take history. It does
not apply a renamed character to already accepted lines. Use the existing cast
application and speaker-repair tools when intentionally changing those lines;
their existing audio invalidation rules still apply.

## Identity decisions

| Action | Result |
| --- | --- |
| Add | Allocate a new opaque ID; supply at least one supporting quote. |
| Rename or edit aliases/evidence | Keep the current ID and review the update. |
| Merge selected | Explicitly choose an existing surviving ID; retire the other IDs. |
| Split selected character | Keep the original ID and add one new ID, or select **Give both split characters new identities** to retire the original. Supply evidence for the new character. |
| Retire selected | Remove the selected characters and retain their retired IDs. |

Retired IDs cannot be reused. Complex changes involving several old and several
new identities require separate approved steps rather than an inferred identity
match. Resolve the current undo before approving another step.

## Imports and source revisions

**Export approved roster** downloads the approved identity/evidence document,
including its prepared-source fingerprint and retired IDs. Unsaved edits are not
part of that export. Keep the export if you want to reconcile characters with a
later upload or revised source.

With no saved roster for the current source, an import provides the historical
identity baseline. Every imported character needs explicit reconciliation with
the current source. Old quote coordinates remain historical claims: an exact
match is required before approval, and the app never silently moves an anchor.
With an existing roster, explicitly map every imported character to a current,
imported or newly allocated identity. Matching names do not merge automatically.
The review shows each mapping and retains retired or remapped IDs.

Coordinates count Unicode characters in prepared text, starting at zero in the
export/API. The screen displays inclusive, one-based locations. They are not
UTF-8 byte or JavaScript UTF-16 offsets. The saved binding includes raw bytes,
decoding, newline/front-matter preparation and the prepared text fingerprint.
Even identical prepared text needs reconciliation when its binding changes.
A saved sidecar whose raw fingerprint differs from its source-keyed cache is
rejected and preserved for manual inspection. Preserve that invalid record
before restoring a valid cache; it is not an import or a normal source revision.
A normal raw-source revision selects a different cache and can import an export
through the review screen.

## Stale drafts, undo and recovery

Approval rechecks the selected book, source, cast, roster and undo against the
preview. A changed input or busy task/book/cast writer refuses publication.
Metadata includes the saved undo record's `undo_revision`. The screen rejects a
later undo status whose record differs and sends the revision captured with the
displayed metadata for undo or discard. The server rechecks that exact record,
so a fresh undo for another book cannot authorize a write from an older review.
Undo requires a loaded character review. If metadata is unavailable, explicit
stale-record discard uses the inspected
undo status revision and remains available without the source.
After an unconfirmed write, load saved state before trying another action. The
screen can retain one previous unsaved draft for the same source and saved
roster; restore it explicitly and review again. This recovery is held in the
current page and does not survive closing or reloading the browser tab.

One exact undo is saved on disk. **Undo last approval** restores the prior cast
and roster bytes, or their prior absence. It requires the original book/source
binding and unchanged approved files. **Discard saved undo** explicitly removes
that undo while keeping the current cast and roster; it also works when a stale
source is unavailable. An unsafe or oversized undo is preserved for manual
inspection rather than granting deletion authority. Approval and undo use the
existing book transaction journal, so startup recovery rolls back interrupted
publication or retains an already committed operation.

The legacy cast editor remains available. Editing or rebuilding the cast makes
an older roster approval stale. Evidence-only approval preserves the exact cast
bytes when its effective names/aliases are unchanged. Material changes affect
new generation input/fingerprints; an existing generation checkpoint continues
with its frozen cast and refuses a missing or altered frozen copy.

## Bounds and API

Source capture is limited to 4,800,000 bytes and 1,200,000 decoded characters
before newline/front-matter preparation; prepared text is also limited to
1,200,000 characters. Large front matter cannot bypass the capture limit.
A roster supports up to 200 active characters, 32 aliases per character,
8 quotes per character, 4,000 Unicode characters per quote, 1,024 characters per
name/alias, and 2,000 retired IDs. Request bodies, imports and sidecars also have
a joint 2 MiB budget; field maxima do not promise that every maximum fits at once.
Existing cast capture is bounded to 8 MiB, persistent undo to 16 MiB and public
JSON output to 16 MiB. Invalid data is rejected before publication.

All routes use the app's existing authentication and book/task controls under
`/api/character_roster`:

| Method/path | Purpose |
| --- | --- |
| `GET /api/character_roster` | Public source/cast/approved roster metadata and current context revision. |
| `GET /source` | Bounded source excerpt, or compatible short query lookup, with current `context_revision`, `offset` and `length`. |
| `POST /source` | Quote lookup using `{context_revision, quote, offset}`. The UI uses this bounded body so long Unicode quotes do not exceed HTTP URL limits. Results contain at most 20 occurrences and a continuation offset. |
| `POST /preview` | Review `{proposal, imported_roster, import_id_mapping, context_revision}` without approval. Unused import fields must be `null`. |
| `POST /apply` | The preview request plus its `expected_revision`; recompute and approve the exact review. |
| `GET /undo` | Inspect whether saved undo is available and currently applicable. |
| `POST /undo` | Restore the exact saved state with `{expected_revision}` from the displayed metadata's `undo_revision`. |
| `POST /discard_undo` | Explicitly discard that exact undo revision. |

The exported schema is `{schema: 1, source_sha256, source_characters, entries,
retired_ids}`. Each entry has `{id, canonical_name, aliases, evidence}`, and each
evidence claim has `{source_quote, start_char, end_char}`. IDs have the form
`character_` followed by 20 lowercase hexadecimal characters. Draft proposals
contain `{source_sha256, entries, decisions}`; each decision contains `{action,
from_ids, to_ids}`. The current source fingerprint is required. Public responses
exclude raw before-images and absolute filesystem paths. Filesystem work,
source searches and JSON parsing/encoding run on workers.

## Qualification scope

Synthetic, shareable two-revision fixtures cover unique, ambiguous, unsupported
and wrong-offset quotes; import mapping; merge/split/retirement; stale refusal;
and exact rollback. Tests exercise real HTTP transfer of a maximum Unicode
quote, foreign process task/book/cast ownership, abrupt process death during
approval and undo, and the existing generation frozen-cast scheduling path.
Browser qualification uses the actual app with synthetic text, valid PCM WAVs
and take history, with CPU dependency stubs. These checks establish workflow
and preservation behavior, not native model quality or listening results.
