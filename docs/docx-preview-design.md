# DOCX preview and supported import profile (#1726)

The single-book uploader supports the bounded DOCX profile below. Select a DOCX,
read the extracted text and policy, then choose **Accept this exact text and
select book** or **Cancel preview**. Preview does not change the current book.
Acceptance stores a separate original archive, derived TXT and receipt, then
uses the existing source selection flow. Neither operation starts inference.
Cancel is available until the separate book-selection request is submitted;
after submission the UI waits for its result instead of claiming it cancelled a
server-side selection that might already have committed.

## Supported profile (policy v2)

Use standard-library ZIP and bounded XML parsing, with no new dependency.
Accept only unencrypted transitional OOXML DOCX with body paragraphs and runs.
Preserve their order, literal Unicode text (including combining characters,
numbers and punctuation), empty paragraphs, tabs and explicit line breaks.
Join paragraphs with two LF characters; map a run tab to TAB and a line break
to LF. Headings remain ordinary paragraphs with their literal text; do not
invent Markdown markers, numbering, or punctuation. Show this policy verbatim
in the preview. Do not trim, normalize Unicode, strip numbers or infer content.

Refuse tables, footnotes/endnotes, tracked changes/moves, comments,
headers/footers, fields, textboxes, drawings, hyperlinks, alternate content,
automatic numbering, hidden text, embedded objects and unknown elements or
attributes affecting text. Report the specific unsupported structure, including
unused text-bearing package parts containing these structures. Known formatting
properties are accepted; unknown property controls refuse. A nonempty extraction
is never a fidelity guarantee.

The required parts are `[Content_Types].xml`, `_rels/.rels` and
`word/document.xml`. Optional parts are `word/_rels/document.xml.rels`,
`word/styles.xml`, `word/settings.xml`, `word/fontTable.xml`,
`word/webSettings.xml`, `word/numbering.xml`, `word/theme/theme1.xml`,
`docProps/core.xml` and `docProps/app.xml`. Their XML and root identities are
validated, and overrides and relationships must match known part types and exact
internal targets. Metadata is ignored with a visible warning; it is not fully
OOXML-schema-validated. Settings requesting tracked revisions refuse.

Body-used paragraph/run styles, their `basedOn` ancestors and paragraph/run
defaults are checked with bounded cycle detection. Hidden properties, numbering,
revision properties, missing style references and unknown used properties refuse,
even where a later override might neutralize them. Unused list/hidden style
definitions and unused numbering definitions do not alone reject a document.
Legacy style-less packages may use literal Heading1–Heading9 labels.

The explicit body walker accepts paragraphs/runs, literal text, tabs, wrapping
line breaks, harmless revision identifiers, compatibility identifiers, bookmarks,
proof markers, known paragraph/run formatting and section/layout properties.
Rendered page-break markers contribute no text; explicit page/column breaks
refuse. It never recursively collects arbitrary XML text. Two consecutive
intra-paragraph LF breaks may be treated as paragraph boundaries downstream;
this ambiguity is explicitly included in the preview policy and warnings.

Compatibility verification is synthetic: the parser suite includes a complete
producer-shaped package with metadata, relationships, styles, settings, fonts,
theme and section properties, with exact punctuation/Unicode/numbers/headings
and body text assertions. It does not qualify actual Word, LibreOffice or
python-docx output. Synthetic tests are not producer qualification.

Hard bounds, each also constrained by existing upload/quota limits:
16 MiB archive, 256 entries, 32 MiB total expanded bytes, 8 MiB per XML member,
4 MiB extracted UTF-8 text, XML depth 64 and 200,000 XML nodes. Enforce actual
streamed byte counts as well as ZIP declared sizes, and a compression ratio
limit of 100. Refuse encryption, unsupported compression, duplicate or
case-colliding names, symlinks/special files, absolute/drive/backslash paths,
dot segments, NULs, macros and unknown package parts. Never extract to disk.
Validate content types, package relationships and document identity. Refuse
external relationships and unsafe internal targets; never fetch a target.
Use an XML parser target that rejects DTD/entity declarations before expansion,
and enforces depth/node limits during parsing, not after building a tree.
Only UTF-8 XML is accepted. DTDs, entities, processing instructions and XML
comments are refused by the parser target. Malformed, empty and excessive
documents produce refusals. The ZIP central directory is checked before the ZIP
library allocates its entry index. Multi-disk, ZIP64, trailing data and oversized
directory metadata are outside the profile; archive comments are limited to
1024 bytes, member names to 256 bytes and extra fields/member comments to 1024.

## Chosen transaction design

Preview captures the upload in a descriptor-owned stage under the existing quota
lease, reads only captured bytes, computes text/policy/warnings and a canonical
SHA-256 receipt, then closes the temporary stage. No public candidate, source
selection or model operation occurs. The receipt binds original SHA-256,
derived UTF-8 SHA-256, policy version and exact warnings. The UI displays the
entire extracted text as text content, with explicit Accept and Cancel actions.

Acceptance resends the same File object plus receipt and explicit confirmation.
The server recaptures/recomputes and compares before publication; missing,
malformed or mismatched confirmation fails closed. A deterministic digest binds
content but is not proof a human viewed it: the API must document confirmation
as the caller's explicit assertion, not an authentication credential.

Publish original DOCX and a separately named DOCX receipt before publishing TXT
last. Generate paired names from the sanitized original filename stem (bounded
to 80 UTF-8 bytes), reserved `-aa2docx-` marker and 32 lowercase UUID hex digits;
paired suffixes are `.txt`, `.docx` and `.docx-receipt.json`.
Each native publication refuses any
existing destination, retaining recovery material on collision without retrying
or overwriting. The durable receipt binds exact
original and derived filenames and digests. Keep the EPUB-only
`publication_source` schema untouched. Use `OwnedSourceUploadStage.publish`
for all three files, with all bytes checked under the same quota lease.
Windows directory rename guards require sequential stage lifetimes. Each stage
checks the same root directory identity before publishing. Directory sync for the
original and receipt must succeed before making TXT visible.

Do not run text-only `_reuse_duplicate_upload` for DOCX. Two archives yielding
identical text must retain both originals and independent receipts. Initially,
allow separate accepted copies rather than introducing archive deduplication.
Originals and receipts remain plain named files counted by
`get_upload_storage_bytes`, but are not selectable sources.

Cancellation/failure before text publication preserves the prior book. Once
publication starts, retain owned original/receipt recovery material on uncertain
outcomes; never unlink using only a pathname. A failed transaction may leave a
quota-counted original and receipt, but no unconfirmed selectable text. Accepted
TXT may remain reusable if later selection fails. Selection failure rollback
must use the existing book-state ownership contract, not delete published files.

## Source admission and retention inspection

Reserved TXT group names are recognized by `get_docx_group_names`. Source
admission verifies the original/text/receipt binding before use. Recovery
inventory records the group for future recovery planning; it is read-only.
External source restore remains unimplemented, as it was before this feature.
There is no recovery-roundtrip qualification.

**Inspect retained DOCX files** requests `GET /api/uploads/docx-retained`.
The server uses only its configured uploads directory, accepts no client path,
and reads directory metadata without selecting, hashing or opening file content.
It lists actual filenames and bytes, expected group names, partial groups and
unavailable malformed marker entries. Complete-looking groups remain unverified:
even a malformed or changed receipt is never reported safe by this listing.

Inspection scans at most 4096 directory entries and returns at most 100 groups.
The response discloses scan completion, omitted marker entries, unavailable
metadata, observed named bytes and the quota ceiling. Counts are observational,
not a locked quota snapshot: files may change, anonymous active uploads are
excluded, and scan limits or unreadable entries can undercount. Files without
the reserved marker are not listed. Upload admission retains its existing quota
lease and authoritative accounting; inspection does not acquire a write-capable
lock or create storage.

For manual removal, stop the app, keep full backups, and do not remove currently
selected/referenced groups. Back up original, text and receipt together,
including partial failed groups. Remove only groups established offline to be
unreferenced. There is no deletion API or automatic garbage collection.

## API and evidence limits

Preview uses `POST /api/upload?select_active=false&docx_preview=true`.
Acceptance resends the same original with `select_active=false`,
`docx_confirmed=true` and `docx_preview_digest`. Missing or mismatched
confirmation refuses publication. The digest binds bytes and policy; it does
not prove a person read the preview. The returned `stored_filename` identifies
the TXT; `docx_source` names the three members. A separate
`/api/uploads/select` request selects the accepted text. Neither starts inference.
Batch DOCX import remains unsupported.

Synthetic parser, upload, admission, recovery-inventory and JavaScript tests
cover bounded extraction, provenance, partial publication, selection and real
upload-change dispatch. Retention tests cover read-only metadata, partial and
invalid groups, limits and plain-text rendering. Actual Word/LibreOffice output,
full release checks and native platforms beyond the executed test host remain
unqualified. Three-file publication is not atomic: failures can retain partial
groups or an accepted TXT whose response was lost. Inspect before retrying.
