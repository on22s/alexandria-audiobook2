"""Structure-aware synthetic EPUB builder driven by Atheris FuzzedDataProvider.

Builds a minimal valid ZIP container (mimetype, META-INF/container.xml, OPF,
optional EPUB 3 nav / EPUB 2 NCX, and XHTML/HTML chapters) while mutating
attributes, namespaces, properties, encodings, XML prologs, markup, anchors,
and text so the fuzzer exercises deep parser/extractor logic instead of getting
stuck on ZIP CRCs.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
from xml.sax.saxutils import escape as xml_escape
import zipfile

NAV_PROSE_SENTINEL = "AA2NAVPROSESENTINEL"
CHAPTER_PROSE_SENTINEL = "AA2CHAPPROSESENTINEL"

_MEDIA_TYPES = (
    "application/xhtml+xml",
    "text/html",
    "application/xhtml+xml; charset=utf-8",
    "APPLICATION/XHTML+XML",
    "TEXT/HTML; charset=UTF-8",
    "image/png",
    "text/css",
)

_NAV_PROPERTIES = (
    "nav",
    " nav ",
    "cover-image nav",
    "nav scripted",
    "navx",
    "scripted nav-like",
    "NAV",
    "Nav",
    "",
    "\tnav\n",
    "cover-image\tnav",
    "nav&#9;scripted",
    "&#10;nav&#13;",
    "cover&#12;nav",
    "nav\u00a0cover-image",
    "\u00a0nav\u00a0",
    "\u2003nav",
    "mathml",
    "svg",
)

_XML_ENCODINGS = (
    None,
    "utf-8",
    "UTF-8",
    "iso-8859-1",
    "windows-1252",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
)

_BLOCK_TAGS = ("p", "div", "h1", "h2", "h3", "li", "blockquote", "section", "article")
_TRICKY_FRAGMENTS = (
    "<script>var x = 1;</script>",
    "<style>.a { color: red; }</style>",
    "<title>Heading</title>",
    "<textarea>Literal &amp; <script> inside textarea</textarea>",
    "<script/>",
    "<style/>",
    "<title/>",
    "<ruby>漢字<rp>(</rp><rt>かんじ</rt><rp>)</rp></ruby>",
    "<ruby>漢<rt>かん</rt>字<rt>じ</rt></ruby>",
    "<!-- <script>commented</script> -->",
    "<![CDATA[ literal <script> not a tag ]]>",
    "<?pi <title>not a tag</title> ?>",
    "<x:script>prefixed stays spoken</x:script>",
    "<br/><hr/>",
    "\u200b\u200c\u200d\ufeff\u00a0\u202f\u2009",
)


@dataclass(frozen=True)
class StructuredEpub:
    """Generated synthetic EPUB archive bytes plus metadata for parity checks."""

    archive_bytes: bytes
    nav_item_id: str
    nav_href: str
    nav_in_spine: bool
    nav_linear: str | None
    nav_properties: str | None
    nav_media_type: str
    nav_omitted: bool
    has_non_nav_chapter: bool
    chapter_linear: str | None


def _escape_attr(value: str) -> str:
    """Escape XML attribute value while preserving raw character references like &#9;."""
    # Allow already-formed numeric character references (&#...;) to pass through
    # so the fuzzer can exercise XML whitespace entities in properties attributes.
    out = []
    idx = 0
    n = len(value)
    while idx < n:
        ch = value[idx]
        if ch == "&":
            semi = value.find(";", idx + 1, idx + 10)
            if semi != -1 and idx + 1 < semi and value[idx + 1] == "#":
                out.append(value[idx:semi + 1])
                idx = semi + 1
                continue
            out.append("&amp;")
        elif ch == '"':
            out.append("&quot;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        else:
            out.append(ch)
        idx += 1
    return "".join(out)


def _strip_xml_illegal(text: str) -> str:
    """Remove NUL and XML 1.0 illegal control characters for well-formed fragments."""
    return "".join(
        ch for ch in text
        if ch in ("\t", "\n", "\r") or (0x20 <= ord(ch) <= 0xD7FF) or (0xE000 <= ord(ch) <= 0xFFFD)
    )


def _consume_short_text(fdp, max_len: int) -> str:
    length = fdp.ConsumeIntInRange(0, max_len)
    if length == 0:
        return ""
    return fdp.ConsumeUnicodeNoSurrogates(length)


def _build_chapter_bytes(fdp, *, anchor_id: str, sentinel: str | None = None,
                         allow_malformed: bool = True,
                         allow_tricky_encodings: bool = True) -> bytes:
    """Build an XHTML/HTML document with mutated markup, entities, and encoding."""
    # Consume structural/encoding selectors BEFORE variable-length text so short
    # fuzzer inputs still reach all encoding and prolog branches.
    enc_mode = fdp.ConsumeIntInRange(0, 9) if allow_tricky_encodings else 0
    has_doctype = fdp.ConsumeBool()
    doctype_internal = fdp.ConsumeBool() if has_doctype else False
    has_pi = fdp.ConsumeBool()
    chunk_count = fdp.ConsumeIntInRange(0, 5)
    raw_enc = b""
    if enc_mode == 8:
        enc_len = fdp.ConsumeIntInRange(1, 12)
        raw_enc = fdp.ConsumeBytes(enc_len).replace(b'"', b"").replace(b"'", b"").replace(b">", b"")

    parts = []
    if has_doctype:
        if doctype_internal:
            parts.append('<!DOCTYPE html [<!ENTITY x "<script>">]>')
        else:
            parts.append("<!DOCTYPE html>")
    if has_pi:
        parts.append("<?xml-stylesheet type='text/css' href='style.css'?>")

    body_bits = []
    if sentinel:
        body_bits.append(f'<p id="{anchor_id}">{sentinel}</p>')
    for idx in range(chunk_count):
        mode = fdp.ConsumeIntInRange(0, 4)
        if mode == 0:
            tag = fdp.PickValueInList(list(_BLOCK_TAGS))
            text = _strip_xml_illegal(_consume_short_text(fdp, 16))
            safe = xml_escape(text)
            if fdp.ConsumeBool():
                attr = f' id="{anchor_id}"' if idx == 0 else f' xml:id="a{idx}"'
            else:
                attr = ""
            body_bits.append(f"<{tag}{attr}>{safe}</{tag}>")
        elif mode == 1:
            body_bits.append(fdp.PickValueInList(list(_TRICKY_FRAGMENTS)))
        elif mode == 2:
            # Anchor link / span with fuzzed text
            label = xml_escape(_strip_xml_illegal(_consume_short_text(fdp, 14)))
            body_bits.append(f'<a name="{anchor_id}">{label}</a>')
        elif mode == 3 and allow_malformed:
            # Raw fuzzed snippet to stress XHTML/HTML error handling & unclosed tags
            body_bits.append(_consume_short_text(fdp, 24))
        else:
            # Quotes/dashes/Unicode text for TOC title dedup & normalization
            sample = fdp.PickValueInList([
                'Chapter 1 — "The Beginning"',
                "Chapter 1 - 'The Beginning'",
                "Arc 2, Chapter 3 – “Voice”",
                "Caf\u00e9\u00a0and\u200btea",
            ])
            body_bits.append(f"<p>{xml_escape(sample)}</p>")

    html_doc = (
        "".join(parts)
        + '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:x="urn:example:x">'
        "<head><title>Synthetic Chapter</title></head>"
        "<body>" + "".join(body_bits) + "</body></html>"
    )

    if not allow_tricky_encodings:
        return html_doc.encode("utf-8")

    if enc_mode < len(_XML_ENCODINGS):
        encoding = _XML_ENCODINGS[enc_mode]
        if encoding is None:
            return html_doc.encode("utf-8")
        if encoding == "utf-16":
            return f'<?xml version="1.0" encoding="utf-16"?>{html_doc}'.encode("utf-16")
        if encoding == "utf-16-le":
            return f'<?xml version="1.0" encoding="utf-16-le"?>{html_doc}'.encode("utf-16-le")
        if encoding == "utf-16-be":
            return f'<?xml version="1.0" encoding="utf-16-be"?>{html_doc}'.encode("utf-16-be")
        header = f'<?xml version="1.0" encoding="{encoding}"?>'
        return (header + html_doc).encode(encoding, errors="replace")
    if enc_mode == 8:
        # Fuzzed XML encoding declaration (ascii/non-ascii/unknown codec)
        return b'<?xml version="1.0" encoding="' + raw_enc + b'"?>' + html_doc.encode("utf-8")
    # UTF-8 BOM
    return b"\xef\xbb\xbf" + html_doc.encode("utf-8")


def build_structured_epub(fdp, *, parity_mode: bool = False) -> StructuredEpub:
    """Generate a structure-aware synthetic EPUB from an Atheris FuzzedDataProvider.

    When ``parity_mode`` is True, documents carry unmistakable sentinels
    (``NAV_PROSE_SENTINEL``, ``CHAPTER_PROSE_SENTINEL``) and stay well-formed
    XML so both ``extract_epub_text`` and ``alexandria_alignment.load_epub``
    can be compared on their navigation-document decision.
    """
    use_subdir = fdp.ConsumeBool()
    opf_dir = "OEBPS/" if use_subdir else ""
    opf_path = f"{opf_dir}content.opf" if fdp.ConsumeBool() else f"{opf_dir}book.opf"
    version = "3.0" if fdp.ConsumeBool() else "2.0"
    use_opf_ns = True if parity_mode else fdp.ConsumeBool()

    # Navigation candidate properties & spine flags
    prop_choice = fdp.ConsumeIntInRange(0, len(_NAV_PROPERTIES))
    if prop_choice < len(_NAV_PROPERTIES):
        nav_properties: str | None = _NAV_PROPERTIES[prop_choice]
    elif fdp.ConsumeBool():
        nav_properties = None
    else:
        nav_properties = _strip_xml_illegal(_consume_short_text(fdp, 16))

    nav_in_spine = fdp.ConsumeBool()
    nav_linear_choice = fdp.ConsumeIntInRange(0, 3)
    nav_linear = (None, "yes", "no", "unknown")[nav_linear_choice]
    nav_omitted = False if parity_mode else (fdp.ConsumeIntInRange(0, 9) == 0)
    nav_media_type = (
        fdp.PickValueInList(["application/xhtml+xml", "text/html"])
        if parity_mode
        else fdp.PickValueInList(list(_MEDIA_TYPES))
    )
    nav_href = fdp.PickValueInList(["nav.xhtml", "Text/nav.xhtml", "toc%20nav.xhtml"])

    # Chapter items
    if parity_mode:
        chapter_count = fdp.ConsumeIntInRange(0, 2)
        chapter_linear = None if fdp.ConsumeBool() else "yes"
    else:
        chapter_count = fdp.ConsumeIntInRange(0, 3)
        chapter_linear = fdp.PickValueInList([None, "yes", "no"])

    include_ncx = fdp.ConsumeBool()
    include_cover = fdp.ConsumeBool()

    manifest_items = []
    nav_prop_attr = f' properties="{_escape_attr(nav_properties)}"' if nav_properties is not None else ""
    manifest_items.append(
        f'<item id="nav" href="{_escape_attr(nav_href)}" '
        f'media-type="{_escape_attr(nav_media_type)}"{nav_prop_attr}/>'
    )

    chapter_specs = []
    for i in range(chapter_count):
        cid = f"ch{i + 1}"
        chref = f"Text/ch{i + 1}.xhtml" if fdp.ConsumeBool() else f"ch{i + 1}.xhtml"
        cmtype = "application/xhtml+xml" if parity_mode else fdp.PickValueInList(list(_MEDIA_TYPES))
        cprops = ""
        if not parity_mode and fdp.ConsumeIntInRange(0, 4) == 0:
            picked = fdp.PickValueInList(list(_NAV_PROPERTIES))
            cprops = f' properties="{_escape_attr(picked)}"'
        manifest_items.append(
            f'<item id="{cid}" href="{_escape_attr(chref)}" '
            f'media-type="{_escape_attr(cmtype)}"{cprops}/>'
        )
        chapter_specs.append((cid, chref, cmtype))

    if include_cover:
        manifest_items.append(
            '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
        )
    if include_ncx:
        manifest_items.append(
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        )

    spine_items = []
    if include_cover and fdp.ConsumeBool():
        spine_items.append('<itemref idref="cover"/>')
    if nav_in_spine:
        lin_attr = f' linear="{nav_linear}"' if nav_linear is not None else ""
        spine_items.append(f'<itemref idref="nav"{lin_attr}/>')
    for cid, _chref, _cmtype in chapter_specs:
        lin_attr = f' linear="{chapter_linear}"' if chapter_linear is not None else ""
        spine_items.append(f'<itemref idref="{cid}"{lin_attr}/>')
    if not parity_mode and fdp.ConsumeIntInRange(0, 7) == 0:
        spine_items.append('<itemref idref="missing_ref"/>')

    opf_ns_attr = ' xmlns="http://www.idpf.org/2007/opf"' if use_opf_ns else ""
    spine_toc_attr = ' toc="ncx"' if include_ncx else ""
    package_xml = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<package{opf_ns_attr} version="{version}" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="uid">urn:fuzz:synthetic</dc:identifier>'
        "<dc:title>Synthetic Fuzz Book</dc:title>"
        "<dc:language>en</dc:language>"
        "</metadata>"
        f'<manifest>{"".join(manifest_items)}</manifest>'
        f'<spine{spine_toc_attr}>{"".join(spine_items)}</spine>'
        "</package>"
    )

    # Build nav document body
    nav_links = []
    for i, (_cid, chref, _cmtype) in enumerate(chapter_specs):
        target_href = f"../{chref}" if "/" in nav_href and "/" not in chref else chref
        if fdp.ConsumeBool():
            target_href += f"#a{i}"
        label = xml_escape(_strip_xml_illegal(_consume_short_text(fdp, 16)) or f"Chapter {i + 1}")
        nav_links.append(f'<li><a href="{_escape_attr(target_href)}">{label}</a></li>')
    if not parity_mode and fdp.ConsumeBool():
        nav_links.append('<li><a href="https://example.invalid/ext">External</a></li>')
        nav_links.append('<li><a href="">Empty href</a></li>')

    nav_type_attr = fdp.PickValueInList([
        'epub:type="toc"',
        'type="toc"',
        'epub:type="landmarks toc"',
        'epub:type="landmarks"',
    ])
    nav_prose = (
        f"<p>{NAV_PROSE_SENTINEL}</p>"
        if parity_mode
        else f"<p>{xml_escape(_strip_xml_illegal(_consume_short_text(fdp, 20)) or 'Preface')}</p>"
    )
    nav_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">'
        "<head><title>Contents</title></head>"
        f"<body>{nav_prose}<nav {nav_type_attr}><ol>{''.join(nav_links)}</ol></nav></body></html>"
    )

    # Build NCX
    ncx_points = []
    for i, (_cid, chref, _cmtype) in enumerate(chapter_specs):
        label = xml_escape(_strip_xml_illegal(_consume_short_text(fdp, 16)) or f"Part {i + 1}")
        ncx_points.append(
            f'<navPoint id="np{i}"><navLabel><text>{label}</text></navLabel>'
            f'<content src="{_escape_attr(chref)}"/></navPoint>'
        )
    ncx_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f'<navMap>{"".join(ncx_points)}</navMap></ncx>'
    )

    container_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        f'<rootfiles><rootfile full-path="{_escape_attr(opf_path)}" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container_xml)
        archive.writestr(opf_path, package_xml)
        if not nav_omitted:
            nav_zip_path = opf_dir + nav_href.replace("%20", " ")
            archive.writestr(nav_zip_path, nav_xhtml)
        for i, (_cid, chref, _cmtype) in enumerate(chapter_specs):
            if not parity_mode and fdp.ConsumeIntInRange(0, 9) == 0:
                continue  # Occasionally omit archive member to exercise missing-resource handling
            sentinel = f"{CHAPTER_PROSE_SENTINEL}_{i}" if parity_mode else None
            chapter_bytes = _build_chapter_bytes(
                fdp,
                anchor_id=f"a{i}",
                sentinel=sentinel,
                allow_malformed=not parity_mode,
                allow_tricky_encodings=not parity_mode,
            )
            archive.writestr(opf_dir + chref, chapter_bytes)
        if include_cover:
            archive.writestr(opf_dir + "cover.png", b"\x89PNG\r\n\x1a\nsynthetic")
        if include_ncx:
            archive.writestr(opf_dir + "toc.ncx", ncx_xml)

    return StructuredEpub(
        archive_bytes=output.getvalue(),
        nav_item_id="nav",
        nav_href=nav_href,
        nav_in_spine=nav_in_spine,
        nav_linear=nav_linear,
        nav_properties=nav_properties,
        nav_media_type=nav_media_type,
        nav_omitted=nav_omitted,
        has_non_nav_chapter=bool(chapter_specs),
        chapter_linear=chapter_linear,
    )
