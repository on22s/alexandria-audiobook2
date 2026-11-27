# EPUB extraction report

In Script, select **Include an EPUB extraction report with this import** before
choosing an EPUB. Expand **EPUB import snapshot** after the upload succeeds.
The report is optional and local. It starts no model and changes no extraction
choices. The ordinary upload, admission, cancellation and publication protections
still apply. Uploading an EPUB remains an import action.

The snapshot binds the captured archive SHA-256 to the exact UTF-8 import-text
SHA-256. It records the package, spine indices and resource hashes, navigation
source/fallback, resolved targets, unresolved anchors, anchors inside omitted
hidden subtrees (`target_in_hidden_subtree`), duplicate navigation,
duplicate-anchor first-occurrence behavior, title insertion/suppression decisions,
omitted HTML `hidden` content in well-formed XHTML (`hidden_content` rows
recording omitted element counts and first element location, never the hidden
text), and skipped missing/non-HTML/empty resources.

Hidden handling applies only where the browser itself parses the document as
XML: an `application/xhtml+xml` spine item that is well-formed XML. One shared
predicate answers that question for script extraction, read-aloud and
`load_epub`. In such a document, an element is hidden only when both Chrome and
Firefox render it hidden: the element is in the XHTML namespace
(`http://www.w3.org/1999/xhtml`) **and** carries an attribute whose name is
exactly lowercase `hidden` in no namespace. XML attribute names are
case-sensitive, so `HIDDEN` and `Hidden` are ordinary attributes; a namespaced
attribute such as `h:hidden` is not the null-namespace HTML attribute, even
when `h` is bound to the XHTML URI; and an element in no namespace (`xmlns=""`,
or a document with no `xmlns` at all) is not an HTML element. Attribute values
do not matter: `hidden=""`, `hidden="false"` and `hidden="until-found"` all hide
(`until-found` hides content from linear reading until interactive
find-in-page reveal). Hidden elements and all their descendants are omitted
from narration. Because `hidden` is an HTML-only global attribute, it is not
applied to SVG or MathML elements with an explicit `xmlns` (a bare
`<svg>`/`<math>` inherits the XHTML namespace in XML and does take it); CSS
`display:none` and `aria-hidden` are not inferred or dropped.
XML has no HTML integration points either: an unprefixed child of an
explicitly namespaced SVG `foreignObject` or MathML `annotation-xml` stays in
the SVG/MathML namespace, so its `hidden` is not applied.

Every other media type (`text/html` and any other non-XHTML type), and malformed
XHTML, gets no hidden handling. Its output or refusal is byte-identical to the
code before this feature: hidden content is narrated whenever that code
narrated the document. That includes documents with bare
boolean attributes (`<p hidden>` without a value), unclosed tags, and HTML-only
entities such as `&nbsp;`, none of which are well-formed XML, so a quoted
`hidden` attribute cannot newly engage the strict parse. A malformed XHTML
document that already had a `script`/`style`/`title` opening is rejected by the
same strict parse as before, with the same error. This is a
documented leak, chosen because dropping visible text is worse than narrating
hidden text; it is never a visible drop. A well-formed `text/html` document
therefore keeps HTML5 tree construction: a hidden table cell containing a
`caption`, or a hidden paragraph containing a block element, narrates the text
a browser renders, exactly as before.
`omitted_elements` counts outermost hidden elements that can omit text; a
hidden void element (`<br hidden>`, `<img hidden>`) is not counted because it
omits no text. `content_policy` names `html_hidden_not_spoken` only when hidden
omission actually applied to an included resource, so a hidden-free or
non-XHTML import keeps the previous policy string unchanged.
For a document the shared predicate accepts, `load_epub` decides hidden
regions from the same raw-XML Expat walk the extraction pass uses, never from
`ebooklib`'s HTML reserialization (which lowercases attribute names and applies
HTML implied-end rules). Extraction and `load_epub` therefore agree on which
text is omitted; a hidden subtree is cut from the XML before the HTML parser
runs, so text cannot be moved out of it. A hidden element is only a subtree:
`load_epub` parses what remains visible around it, so a hidden `<body>` beside
visible content keeps that visible content (the hidden body is not by itself
"the document is hidden"). The document root's own start tag and the document
`<body>` (the direct child of the root `<html>`) are recognized through the
XML tree, so a `<body hidden>` nested in `<template>`, `noscript` or `textarea`
is not mistaken for the document body. The walk Expat-parses each
`application/xhtml+xml` spine item, which `load_epub` did not do before this
feature; it uses the extraction pass's own configuration (no entity
expansion, no external fetch) and is bounded by Expat's amplification limit.
`load_epub`'s existing `BeautifulSoup` text path still returns `''` for a
hidden-free UTF-16 chapter (unchanged from before this feature); a
hidden-bearing accepted document is substituted as UTF-8 and does load, so
encoding support is not claimed end-to-end. A spine document is entirely
hidden only when hidden omission actually removed non-whitespace text **and no
visible text remains after the cut** (whatever its shape: a hidden root, a
hidden body with nothing visible beside it, or all text inside hidden
elements). In that case script extraction returns no chapter text (with no
navigation target it returns `''`; a navigation entry that targets the hidden
document still contributes its visible table-of-contents label, exactly as it
already did for an otherwise-empty chapter), read-aloud returns no segments,
and `alexandria_alignment.load_epub` raises a `ValueError` naming hidden
elements.
A document whose only hidden elements are void elements (for example
`<br hidden=""/><img hidden=""/>`) removed no text, so it behaves as before the
feature. An EPUB with one entirely hidden chapter and one empty visible chapter
is not in that class: `load_epub` returns the empty visible text, as it did
before this feature, instead of raising the hidden-specific error. A book with
no considered spine documents keeps the existing `no readable spine documents`
refusal.
Navigation targets pointing only to
anchors inside a hidden subtree do not resolve into visible text and do not
insert navigation labels; they are reported with reason
`target_in_hidden_subtree`. A navigation document listed in the
spine is a table of contents, not story text: it is skipped and its spine row
names the reason `navigation_document`, so it is never narrated. The whole
document is skipped, including any prose a book places in the same file (for
example a title page or foreword): extraction does not try to separate it,
which matches read-aloud. Whether that skipped file held non-TOC text is not
reported, because deciding it would need a second parse of the navigation
member beyond the parse the extractor already made. A book whose only text
document is its navigation document is refused at import with HTTP 400 (both
direct upload and Calibre import go through `upload_file`), so no extraction
report is delivered for it; a navigation document that is declared in the spine
but absent from the archive is not counted, so such a book keeps the generic
empty-book refusal. A navigation document missing from the archive is still
reported as `missing_archive_resource`. Navigation outside the spine is
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
