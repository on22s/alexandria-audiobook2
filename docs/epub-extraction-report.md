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
omitted HTML `hidden` content (`hidden_content` rows recording omitted element
counts and first element location, never the hidden text), and skipped
missing/non-HTML/empty resources. HTML elements carrying the HTML `hidden`
attribute (ASCII case-insensitive in both HTML and XML for parity, in any
boolean form including `hidden="until-found"`, which hides content from linear
reading until interactive find-in-page reveal; XHTML-namespaced prefixed forms
such as `h:hidden` apply to XML/XHTML documents — `application/xhtml+xml`,
read-aloud, and `load_epub` — whereas `text/html` attributes have no XML
namespaces so prefixed names are distinct attributes) and all their descendants
are omitted from narration. Because `hidden` is an HTML-only global attribute,
it is not applied to SVG or MathML elements; CSS `display:none` and
`aria-hidden` are not inferred or dropped. Tolerant `text/html` processing
follows one governing rule: this is an audiobook, so losing visible text is
worse than narrating hidden text. When it meets structure it does not model, it
fails open - the hidden region ends no later than the browser would end it, and
it never silences text the browser would show. The guarantee is **structural**
(Rule 15), not a list of known-bad tags: a prepass runs the same tree
construction with output off and a `hidden` element opens a region only when
the model can prove where that region ends. The element must not be void, an
element the browser ignores (`image`, `keygen`, `frame`, `frameset`, table
structure outside a table, a nested `form`), or foster-parented out of a table;
and either its end is one the implemented optional-end-tag rules close (`p`,
`li`, `dd`, `dt`, `option`, `optgroup`, `rt`, `rp`, `tr`, `td`, `th`, `thead`,
`tbody`, `tfoot`, `caption`, and headings), or the prepass proves the model
pops it through an explicit matching end tag, an implemented implied close or
an ancestor close. A region also ends wherever the model pops its root element
for any reason. If the prepass sees an unmodelled insertion-mode anomaly
anywhere in the document (nested or poisoned `select`, an ignored table token
or nested `form`, an adoption-agency formatting shape, a blocked ancestor end
tag), it trusts no hidden region in that document and treats every hidden
element as visible. Anything not proved is narrated: `hidden` leaks in invalid
markup are documented limits; a visible drop is a bug. In tolerant `text/html`
resources, self-closing syntax on non-void elements
(such as `<div hidden=""/>`) is treated as self-closing by Python's
`HTMLParser`, so the following text is narrated. This is a deliberate
divergence *from* browsers, not parity: HTML ignores the slash on non-void
elements, so a browser hides that following text. It is kept because the cost
of the divergence is narrating hidden text (the safe direction for an
audiobook), not losing visible text. Tolerant `text/html` also models the HTML5
tree-construction rules that decide whether a hidden element still wraps later
prose: the complete `<p>`-closing start-tag set (including `center`, `dir`,
`listing`, `summary`, `xmp` and `plaintext`), the heading rule (a heading start
tag closes an open `p` and pops a current heading, so an omitted `</h2>` before
`<h2>Chapter 2` never drops the chapter), the `li`/`dd`/`dt` implied closes
(which stop at the spec's special elements except `address`/`div`/`p`),
table cells/rows/sections/`caption`/`colgroup` (a cell or row start tag closes
an open `caption`/`colgroup` in table scope), select options, ruby
annotations, and `head`/`body`. Start tags the browser ignores in body mode
(table structure outside a table, a nested `form`) create no element, so their
`hidden` attribute never opens a hidden root; end tags the browser ignores (a
stray `</span>` above a `<p>`, `</ruby>` above `<ol>`) never close a hidden
ancestor, but they do make the prepass distrust the document. Content
the HTML5 parser foster-parents out of a table (character data, prose or an
unexpected element directly inside a table, table section or row) is rendered
before the table, so a hidden table does not hide it and it is narrated; XHTML
is parsed as XML there, where no foster-parenting occurs, so the XML paths keep
the text inside the hidden table. `omitted_elements` counts outermost hidden
elements that can omit text; a hidden void element (`<br hidden>`,
`<img hidden>`) is not counted because it omits no text.
`load_epub`'s `<html>`/`<body>` root detection decodes UTF-8/UTF-16 and skips
comments, PIs, DOCTYPE internal subsets, RCDATA head content (`title`,
`textarea`, `noscript`), and `<template>` fragments, so literal `<body hidden>`
text in any of them is not mistaken for the document body. `load_epub`'s
existing `BeautifulSoup` text path still returns `''` for a UTF-16 chapter
(unchanged from before this feature); this is pre-existing and out of scope,
so encoding support is not claimed end-to-end. When every spine document in an
EPUB is hidden, script extraction returns no chapter text (with no navigation
target it returns `''`; a navigation entry that targets the hidden document
still contributes its visible table-of-contents label, exactly as it already
did for an otherwise-empty chapter), read-aloud returns no segments, and
`alexandria_alignment.load_epub` raises a `ValueError` naming hidden elements.
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
