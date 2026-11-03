# EPUB extraction report

In Script, select **Include an EPUB extraction report with this import** before
choosing an EPUB. Expand **EPUB import snapshot** after the upload succeeds.
The report is optional and local. It starts no model and changes no extraction
choices. The ordinary upload, admission, cancellation and publication protections
still apply. Uploading an EPUB remains an import action.

The snapshot binds the captured archive SHA-256 to the exact UTF-8 import-text
SHA-256. It records the package, spine indices and resource hashes, navigation
source/fallback, resolved targets, unresolved anchors, duplicate navigation,
duplicate-anchor first-occurrence behavior, title insertion/suppression decisions,
and skipped missing/non-HTML/empty resources. Navigation outside the spine is
reported. Content before a resolved anchor is retained. Resource character
positions refer to plain text before title insertion and normalization; they are
not offsets into later edited text or audio.

AA2 keeps its existing spine order, tolerant HTML parser, paragraph handling,
navigation-title insertion, invisible-character/space normalization, and blank-line
normalization. HTML document titles are not spoken or used as generated chapter
names. Without usable navigation, body text remains without a fabricated title.
The near-target title check is an existing heuristic, not authoritative chapter
identification. Malformed navigation can fall back to NCX or body text; malformed
packages still fail import unless the shared package parser's existing narrow
repair applies. Tolerantly extracted XHTML is not claimed to be XML-validated.

There are no proposed front/back-matter exclusions and no automatic note removal.
Emphasis/display formatting is not attached in this feature. Any later formatting
work must report unmatched spans and leave speech text unchanged; no unmatched
span is silently repaired by this report.

Report rows have a 128 KiB byte budget and 512-character fields. Half the row
budget is reserved for package/spine identity; navigation and policy detail cannot
consume it. Large books can still exceed either budget: `detail_complete` becomes
false, `omitted_rows_by_kind` counts missing rows per category, and
`truncated_fields` names shortened fields. Such a report is not complete resource
or target coverage. Truncation never changes the
imported text. Hashes remain full length. The report is an upload snapshot held
in the current page, not a saved historical receipt or proof about the current
editor. Source/book selection, failed upload and page reload clear it. Re-upload
to inspect changed source bytes; compare the text digest before reusing a report.
Existing uploads without a captured report are not reconstructed from filenames.

The API query `include_extraction_report=true` returns this snapshot for EPUB;
for TXT/Markdown it returns `extraction_report: null` (not applicable). Resource
text hashes cover text after navigation-label insertion, stripping and existing
normalization; archive-member hashes cover original bytes. Package details name
the selected rootfile and any existing narrow metadata repair. Empty body text
with an inserted navigation label is distinguished from extracted body text.

Synthetic tests compare report-on/off text bytes and source coverage, cover EPUB2
and EPUB3 navigation, duplicate/missing anchors, missing/malformed resources,
repeated HTML titles, spine/document order, changed identities, bounded detail,
and stale UI responses. They provide no real-book fidelity, browser layout,
accessibility, model or listening qualification. Archive paths and labels can
contain private information; sharing requires a separate user choice.
