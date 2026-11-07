"""URI encoding boundary regressions for EPUB read-aloud (#1779 P1).

ZIP member names stay decoded archive paths; every generated URI
(OPF href, SMIL text/audio src, generated nav href) is percent-encoded
exactly once, path and fragment separately.  Existing source hrefs
are preserved verbatim (never double-encoded).  Resolution decodes
exactly once and fails closed on malformed escapes / raw invalid
URIs / wrong targets.  Raw Unicode is a valid IRI and accepted.
"""
import io
import zipfile
import xml.etree.ElementTree as ET
import unittest

from epub_readaloud import (build_epub_readaloud, build_smil_document,
                            validate_epub_readaloud)

OPF_NS = "{http://www.idpf.org/2007/opf}"
SMIL_NS = "{http://www.w3.org/ns/SMIL}"


def _container(opf_path):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            f'<rootfiles><rootfile full-path="{opf_path}" media-type="application/oebps-package+xml"/></rootfiles></container>')


def _ch(body, title="Chapter One"):
    return ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">'
            f'<head><title>{title}</title></head><body>{body}</body></html>')


def make_epub3(*, zip_path="OEBPS/chap one.xhtml", href="chap%20one.xhtml",
               opf_path="OEBPS/content.opf", nav_href=None,
               body='<h1>Chapter One</h1><p>Hello synthetic world.</p>',
               ch2=False):
    """One/two spine docs; href is the literal OPF/nav href for zip_path."""
    nav_href = href if nav_href is None else nav_href
    base = zip_path.rsplit('/', 1)[0] if '/' in zip_path else ''
    opf_dir = opf_path.rsplit('/', 1)[0] if '/' in opf_path else ''
    # hrefs in OPF/nav are relative to their own directories; fixtures
    # keep OPF and nav in the same directory as the spine file unless
    # the caller supplies explicit hrefs for nested/root cases.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as a:
        a.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        a.writestr('META-INF/container.xml', _container(opf_path))
        c2_item = '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>' if ch2 else ''
        c2_spine = '<itemref idref="c2"/>' if ch2 else ''
        a.writestr(opf_path,
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="id">urn:test:1</dc:identifier>'
                   '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
                   '<dc:language>en</dc:language>'
                   '<meta property="dcterms:modified">2024-01-01T00:00:00Z</meta></metadata><manifest>'
                   '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                   f'<item id="c1" href="{href}" media-type="application/xhtml+xml"/>'
                   + c2_item +
                   '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
                   '</manifest><spine><itemref idref="c1"/>' + c2_spine + '</spine></package>')
        nav_path = (opf_dir + '/nav.xhtml') if opf_dir else 'nav.xhtml'
        a.writestr(nav_path,
                   '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
                   '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">'
                   '<head><title>Contents</title></head><body>'
                   f'<nav epub:type="toc"><ol><li><a href="{nav_href}">Chapter One</a></li></ol></nav>'
                   '</body></html>')
        a.writestr(zip_path, _ch(body))
        if ch2:
            ch2_path = (base + '/ch2.xhtml') if base else 'ch2.xhtml'
            a.writestr(ch2_path, _ch('<h1>Chapter Two</h1><p>Final synthetic paragraph.</p>', 'Chapter Two'))
        cover_path = (opf_dir + '/cover.png') if opf_dir else 'cover.png'
        a.writestr(cover_path, b'\x89PNG\r\n\x1a\nfake')
    return buf.getvalue()


def make_epub2(*, zip_path="OEBPS/chap one.xhtml", href="chap%20one.xhtml",
               ncx_src=None, body='<h1>Chapter One</h1><p>Hello synthetic world.</p>'):
    ncx_src = href if ncx_src is None else ncx_src
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as a:
        a.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        a.writestr('META-INF/container.xml', _container('OEBPS/content.opf'))
        a.writestr('OEBPS/content.opf',
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id" version="2.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="id">urn:test:1</dc:identifier>'
                   '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
                   '<dc:language>en</dc:language></metadata><manifest>'
                   '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                   f'<item id="c1" href="{href}" media-type="application/xhtml+xml"/>'
                   '<item id="cover" href="cover.png" media-type="image/png"/>'
                   '</manifest><spine toc="ncx"><itemref idref="c1"/></spine>'
                   '<guide><reference type="cover" title="Cover" href="cover.png"/></guide></package>')
        a.writestr('OEBPS/toc.ncx',
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                   '<head><meta name="dtb:uid" content="urn:test:1"/></head>'
                   '<docTitle><text>Synthetic Title</text></docTitle><navMap>'
                   '<navPoint id="n1" playOrder="1"><navLabel><text>Chapter One</text></navLabel>'
                   f'<content src="{ncx_src}"/></navPoint></navMap></ncx>')
        a.writestr(zip_path, _ch(body))
        a.writestr('OEBPS/cover.png', b'\x89PNG\r\n\x1a\nfake')
    return buf.getvalue()


def entries():
    return [
        {"index": 0, "prepared_text": "Chapter One", "start_frame": 0, "end_frame": 12000, "sample_rate": 24000},
        {"index": 1, "prepared_text": "Hello synthetic world.", "start_frame": 12240, "end_frame": 24000, "sample_rate": 24000},
    ]


def _build(src, audio_filename="audiobook.mp3", script=None):
    return build_epub_readaloud(retained_epub_bytes=src,
                                audio_bytes=b"\xff\xfbx",
                                audio_filename=audio_filename,
                                script_entries=script if script is not None else entries())


def _opf_items(out, opf_path):
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        opf = ET.fromstring(z.read(opf_path))
        names = z.namelist()
        contents = {name: z.read(name) for name in names}
        return ({it.get("id"): it for it in opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")},
                names, contents)


def _member(contents, name):
    return contents[name].decode("utf-8")


class UriEncodingTests(unittest.TestCase):
    def test_space_path_encoded_once_zip_names_decoded(self):
        out, _ = _build(make_epub3())
        validate_epub_readaloud(out)
        items, names, z = _opf_items(out, "OEBPS/content.opf")
        # ZIP names stay decoded; generated hrefs are encoded exactly once.
        self.assertIn("OEBPS/chap one.xhtml", names)
        self.assertIn("OEBPS/chap one.smil", names)
        self.assertNotIn("OEBPS/chap%20one.smil", names)
        self.assertEqual("chap%20one.smil", items["aa2-smil-001"].get("href"))
        self.assertNotIn("%2520", "".join(it.get("href") or "" for it in items.values()))
        # Existing source href preserved verbatim, not double-encoded.
        self.assertEqual("chap%20one.xhtml", items["c1"].get("href"))
        smil = _member(z, "OEBPS/chap one.smil")
        self.assertIn('src="chap%20one.xhtml#aa2-p-0001"', smil)
        self.assertNotIn('src="chap one.xhtml', smil)
        root = ET.fromstring(smil)
        self.assertEqual("audio/audiobook.mp3",
                         root.find(".//" + SMIL_NS + "audio").get("src"))

    def test_unicode_path_and_raw_iri_source(self):
        # Percent-encoded Unicode source href.
        out, _ = _build(make_epub3(zip_path="OEBPS/café one.xhtml",
                                   href="caf%C3%A9%20one.xhtml",
                                   nav_href="caf%C3%A9%20one.xhtml"))
        validate_epub_readaloud(out)
        items, names, z = _opf_items(out, "OEBPS/content.opf")
        self.assertIn("OEBPS/café one.xhtml", names)
        self.assertEqual("caf%C3%A9%20one.smil", items["aa2-smil-001"].get("href"))
        smil = _member(z, "OEBPS/café one.smil")
        self.assertIn('src="caf%C3%A9%20one.xhtml#', smil)
        # Raw Unicode IRI source is valid and must not be rejected; the
        # preserved source href stays raw, generated refs are encoded.
        out2, _ = _build(make_epub3(zip_path="OEBPS/café.xhtml",
                                    href="café.xhtml", nav_href="café.xhtml"))
        validate_epub_readaloud(out2)
        items2, _n2, _z2 = _opf_items(out2, "OEBPS/content.opf")
        self.assertEqual("caf%C3%A9.smil", items2["aa2-smil-001"].get("href"))
        self.assertEqual("café.xhtml", items2["c1"].get("href"))

    def test_literal_percent_and_hash_in_filename(self):
        # Literal %2F in the ZIP name must become %252F in URIs.
        out, _ = _build(make_epub3(zip_path="OEBPS/a%2Fb.xhtml",
                                   href="a%252Fb.xhtml", nav_href="a%252Fb.xhtml"))
        validate_epub_readaloud(out)
        items, names, z = _opf_items(out, "OEBPS/content.opf")
        self.assertIn("OEBPS/a%2Fb.xhtml", names)
        self.assertEqual("a%252Fb.smil", items["aa2-smil-001"].get("href"))
        smil = _member(z, "OEBPS/a%2Fb.smil")
        self.assertIn('src="a%252Fb.xhtml#', smil)
        # Literal % and # in the ZIP name.
        out2, _ = _build(make_epub3(zip_path="OEBPS/a%b#c.xhtml",
                                    href="a%25b%23c.xhtml", nav_href="a%25b%23c.xhtml"))
        validate_epub_readaloud(out2)
        items2, names2, z2 = _opf_items(out2, "OEBPS/content.opf")
        self.assertIn("OEBPS/a%b#c.xhtml", names2)
        self.assertEqual("a%25b%23c.smil", items2["aa2-smil-001"].get("href"))
        smil2 = _member(z2, "OEBPS/a%b#c.smil")
        self.assertIn('src="a%25b%23c.xhtml#aa2-p-0001"', smil2)

    def test_nested_path(self):
        out, _ = _build(make_epub3(zip_path="OEBPS/Text/chap one.xhtml",
                                   href="Text/chap%20one.xhtml",
                                   nav_href="Text/chap%20one.xhtml"))
        validate_epub_readaloud(out)
        items, names, z = _opf_items(out, "OEBPS/content.opf")
        self.assertIn("OEBPS/Text/chap one.smil", names)
        self.assertEqual("Text/chap%20one.smil", items["aa2-smil-001"].get("href"))
        smil = _member(z, "OEBPS/Text/chap one.smil")
        # SMIL sits beside spine: relative text refs, audio one level up.
        self.assertIn('src="chap%20one.xhtml#aa2-p-0001"', smil)
        self.assertIn('src="../audio/audiobook.mp3"', smil)

    def test_root_level_opf(self):
        out, _ = _build(make_epub3(zip_path="chap one.xhtml",
                                   href="chap%20one.xhtml",
                                   opf_path="content.opf",
                                   nav_href="chap%20one.xhtml"))
        validate_epub_readaloud(out)
        items, names, z = _opf_items(out, "content.opf")
        self.assertIn("chap one.smil", names)
        self.assertEqual("chap%20one.smil", items["aa2-smil-001"].get("href"))
        self.assertEqual("audio/audiobook.mp3", items["aa2-audio-001"].get("href"))
        smil = _member(z, "chap one.smil")
        self.assertIn('src="chap%20one.xhtml#', smil)

    def test_generated_nav_and_audio_uri_encoding(self):
        # EPUB 2 NCX src is percent-encoded; generated nav + audio refs
        # (audio filename itself has a space and Unicode) must encode.
        out, _ = _build(make_epub2(), audio_filename="my audio café.mp3")
        validate_epub_readaloud(out)
        items, names, z = _opf_items(out, "OEBPS/content.opf")
        self.assertIn("OEBPS/audio/my audio café.mp3", names)
        self.assertEqual("audio/my%20audio%20caf%C3%A9.mp3",
                         items["aa2-audio-001"].get("href"))
        nav = _member(z, "OEBPS/nav.xhtml")
        self.assertIn('href="chap%20one.xhtml"', nav)
        self.assertNotIn('href="chap one.xhtml"', nav)
        smil = _member(z, "OEBPS/chap one.smil")
        self.assertIn('src="audio/my%20audio%20caf%C3%A9.mp3"', smil)

    def test_encoded_fragment_id_roundtrip(self):
        body = '<p id="café sec">Chapter One</p><p>Hello synthetic world.</p>'
        out, _ = _build(make_epub3(body=body))
        validate_epub_readaloud(out)
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            self.assertIn('id="café sec"', z.read("OEBPS/chap one.xhtml").decode("utf-8"))
            smil = z.read("OEBPS/chap one.smil").decode("utf-8")
        self.assertIn('src="chap%20one.xhtml#caf%C3%A9%20sec"', smil)

    def test_build_smil_document_encodes_path_fragment_and_audio(self):
        mapping = [{"fragment": "a b%c#d", "chunk_indexes": [0],
                    "start_frame": 0, "end_frame": 1000}]
        text = build_smil_document(mapping, "audio/my clip.mp3", 24000,
                                   existing_ids=set(),
                                   spine_relpath="OEBPS/chap one.xhtml",
                                   smil_relpath="OEBPS/chap one.smil")
        self.assertIn('src="chap%20one.xhtml#a%20b%25c%23d"', text)
        self.assertIn('src="audio/my%20clip.mp3"', text)

    def test_regeneration_does_not_double_encode(self):
        once, _ = _build(make_epub3())
        twice, _ = _build(once)
        validate_epub_readaloud(twice)
        items, names, _z = _opf_items(twice, "OEBPS/content.opf")
        self.assertEqual("chap%20one.smil", items["aa2-smil-001"].get("href"))
        self.assertEqual("chap%20one.xhtml", items["c1"].get("href"))
        self.assertNotIn("%2520", "".join(it.get("href") or "" for it in items.values()))
        self.assertIn("OEBPS/chap one.smil", names)


def _rewrite(out, member, old, new):
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(out)) as src, zipfile.ZipFile(buf, 'w') as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == member:
                data = data.replace(old, new)
            dst.writestr(info.filename, data)
    return buf.getvalue()


class UriNegativeTests(unittest.TestCase):
    def test_builder_rejects_malformed_source_escapes(self):
        for bad in ("chap%2Gone.xhtml", "chap%one.xhtml", "chap one.xhtml",
                    "chap%FFone.xhtml", "chap%00one.xhtml"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _build(make_epub3(href=bad, nav_href=bad))

    def test_validator_rejects_raw_space_and_bad_escape_in_output(self):
        out, _ = _build(make_epub3())
        raw = _rewrite(out, "OEBPS/content.opf", b"chap%20one.smil", b"chap one.smil")
        with self.assertRaises(ValueError):
            validate_epub_readaloud(raw)
        bad = _rewrite(out, "OEBPS/content.opf", b"chap%20one.smil", b"chap%2Gone.smil")
        with self.assertRaises(ValueError):
            validate_epub_readaloud(bad)
        raw_smil = _rewrite(out, "OEBPS/chap one.smil",
                            b"chap%20one.xhtml#", b"chap one.xhtml#")
        with self.assertRaises(ValueError):
            validate_epub_readaloud(raw_smil)
        bad_audio = _rewrite(out, "OEBPS/chap one.smil",
                             b"audio/audiobook.mp3", b"audio/audio book.mp3")
        with self.assertRaises(ValueError):
            validate_epub_readaloud(bad_audio)

    def test_validator_rejects_wrong_targets(self):
        out, _ = _build(make_epub3())
        wrong_file = _rewrite(out, "OEBPS/chap one.smil",
                              b"chap%20one.xhtml#", b"other%20one.xhtml#")
        with self.assertRaises(ValueError):
            validate_epub_readaloud(wrong_file)
        wrong_frag = _rewrite(out, "OEBPS/chap one.smil",
                              b"aa2-p-0001", b"aa2-missing")
        with self.assertRaises(ValueError):
            validate_epub_readaloud(wrong_frag)
        missing_opf = _rewrite(out, "OEBPS/content.opf",
                               b"chap%20one.smil", b"missing%20one.smil")
        with self.assertRaises(ValueError):
            validate_epub_readaloud(missing_opf)


if __name__ == "__main__":
    unittest.main()
