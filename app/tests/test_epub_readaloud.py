"""AA2 EPUB 3 read-aloud (#1779) — source-preserving SMIL, fail-closed mapping."""
import io
import zipfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import epub_readaloud as er
from epub_readaloud import (build_epub_readaloud, map_spine_to_chunks,
                            validate_epub_readaloud, get_xhtml_segments,
                            get_ncx_points, get_spine_documents)


def _make_source_epub():
    """Tiny synthetic EPUB 3: two spine docs, title/author/language/cover."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml',
                         '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        archive.writestr('OEBPS/content.opf',
                         '<?xml version="1.0" encoding="UTF-8"?>'
                         '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                         '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         '<dc:identifier id="id">urn:test:1</dc:identifier>'
                         '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
                         '<dc:language>en</dc:language><dc:description>Synthetic.</dc:description>'
                         '</metadata>'
                         '<manifest>'
                         '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                         '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                         '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
                         '<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
                         '</manifest>'
                         '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
        archive.writestr('OEBPS/nav.xhtml',
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
                         'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body>'
                         '<nav epub:type="toc"><ol>'
                         '<li><a href="ch1.xhtml">Chapter One</a></li>'
                         '<li><a href="ch2.xhtml">Chapter Two</a></li>'
                         '</ol></nav></body></html>')
        archive.writestr('OEBPS/ch1.xhtml',
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                         '<head><title>Chapter One</title></head><body>'
                         '<h1>Chapter One</h1><p>Hello synthetic world.</p><p>Second paragraph here.</p>'
                         '</body></html>')
        archive.writestr('OEBPS/ch2.xhtml',
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                         '<head><title>Chapter Two</title></head><body>'
                         '<h1>Chapter Two</h1><p>Final synthetic paragraph.</p>'
                         '</body></html>')
        # Minimal JPEG (SOI/EOI) — bytes preserved, never decoded.
        archive.writestr('OEBPS/cover.jpg', b'\xff\xd8\xff\xe0\xff\xd9')
    return buffer.getvalue()


def _script_entries():
    """Prepared texts matching the source spine exactly (happy path)."""
    return [
        {"index": 0, "prepared_text": "Chapter One", "start_frame": 0,
         "end_frame": 12000, "sample_rate": 24000},
        {"index": 1, "prepared_text": "Hello synthetic world.", "start_frame": 12240,
         "end_frame": 24000, "sample_rate": 24000},
        {"index": 2, "prepared_text": "Second paragraph here.", "start_frame": 24240,
         "end_frame": 36000, "sample_rate": 24000},
        {"index": 3, "prepared_text": "Chapter Two", "start_frame": 36240,
         "end_frame": 48000, "sample_rate": 24000},
        {"index": 4, "prepared_text": "Final synthetic paragraph.", "start_frame": 48240,
         "end_frame": 72000, "sample_rate": 24000},
    ]


class EpubReadaloudTests(unittest.TestCase):
    def test_bound_member_reader_refuses_root_alias(self):
        import epub_readaloud as er
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('.', b'payload')
        with zipfile.ZipFile(io.BytesIO(raw.getvalue())) as archive:
            self.assertIn('.', archive.namelist())
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                er._read_archive_member_bounded(archive, '.', 1024)

    def test_mapping_retains_original_segment_indexes_after_wordless_segments(self):
        segments = [{"fragment": None, "text": text}
                    for text in ("—", "Same words", "***", "Same words", "…")]
        entries = [{"index": position, "prepared_text": "Same words",
                    "start_frame": position * 24000, "end_frame": (position + 1) * 24000,
                    "sample_rate": 24000} for position in range(2)]
        mapping, cursor, _consumed = map_spine_to_chunks(segments, entries, require_full_coverage=True)
        self.assertEqual([1, 3], [row["segment_index"] for row in mapping])
        self.assertEqual([[0], [1]], [row["chunk_indexes"] for row in mapping])
        self.assertEqual(2, cursor)

    def test_wordless_separators_preserve_spoken_element_targets(self):
        xhtml_namespace = '{http://www.w3.org/1999/xhtml}'
        smil_namespace = '{http://www.w3.org/ns/SMIL}'
        for existing_ids in (False, True):
            for texts in (("Hello world", "Goodbye now"), ("Same words", "Same words")):
                for placement in ('leading', 'between', 'both'):
                    with self.subTest(existing_ids=existing_ids, texts=texts, placement=placement):
                        paragraphs = [(texts[0], 0), (texts[1], 1)]
                        if placement in ('between', 'both'):
                            paragraphs.insert(1, ('***', None))
                        if placement in ('leading', 'both'):
                            paragraphs.insert(0, ('—', None))
                        if placement == 'both':
                            paragraphs.append(('…', None))
                        body = '<span id="aa2-p-0001"></span>'
                        for position, (text, spoken) in enumerate(paragraphs):
                            attributes = f' id="source-{position}"' if existing_ids else ''
                            if spoken is not None:
                                attributes += f' data-spoken="{spoken}"'
                            body += f'<p{attributes}>{text}</p>'
                        chapter = ('<html xmlns="http://www.w3.org/1999/xhtml">'
                                   '<head><title>Synthetic</title></head><body>' + body + '</body></html>')
                        source_buffer = io.BytesIO()
                        with zipfile.ZipFile(io.BytesIO(_make_source_epub())) as source, \
                                zipfile.ZipFile(source_buffer, 'w') as retained:
                            for member in source.infolist():
                                raw = chapter.encode('utf-8') if member.filename == 'OEBPS/ch1.xhtml' else source.read(member)
                                retained.writestr(member, raw)
                        spoken_texts = [*texts, 'Chapter Two', 'Final synthetic paragraph.']
                        entries = [{"index": position, "prepared_text": text,
                                    "start_frame": position * 24000, "end_frame": (position + 1) * 24000,
                                    "sample_rate": 24000} for position, text in enumerate(spoken_texts)]
                        result, _ = build_epub_readaloud(retained_epub_bytes=source_buffer.getvalue(),
                            audio_bytes=b'synthetic-audio-not-decoded', audio_filename='audiobook.mp3',
                            script_entries=entries)
                        self.assertEqual(2, validate_epub_readaloud(result)['smil_files'])
                        with zipfile.ZipFile(io.BytesIO(result)) as archive:
                            document = ET.fromstring(archive.read('OEBPS/ch1.xhtml'))
                            smil = ET.fromstring(archive.read('OEBPS/ch1.smil'))
                        elements = document.findall('.//' + xhtml_namespace + 'p')
                        by_id = {element.get('id'): element for element in document.iter() if element.get('id')}
                        pairs = smil.findall('.//' + smil_namespace + 'par')
                        self.assertEqual(2, len(pairs))
                        expected_elements = [element for element in elements if element.get('data-spoken') is not None]
                        for position, pair in enumerate(pairs):
                            path, fragment = pair.find(smil_namespace + 'text').get('src').split('#')
                            self.assertEqual('ch1.xhtml', path)
                            self.assertIs(expected_elements[position], by_id[fragment])
                            self.assertEqual(texts[position], ''.join(by_id[fragment].itertext()))
                            audio = pair.find(smil_namespace + 'audio')
                            self.assertEqual(f'00:00:0{position}.000', audio.get('clipBegin'))
                            self.assertEqual(f'00:00:0{position + 1}.000', audio.get('clipEnd'))
                        for position, element in enumerate(elements):
                            if existing_ids:
                                self.assertEqual(f'source-{position}', element.get('id'))
                            elif element.get('data-spoken') is None:
                                self.assertIsNone(element.get('id'))

    def test_roundtrip_preserves_spine_metadata_and_validates(self):
        source = _make_source_epub()
        audio = b"\xff\xfbFAKE-MP3-BYTES"
        epub_bytes, report = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=audio, audio_filename="audiobook.mp3",
            script_entries=_script_entries())
        self.assertEqual("Synthetic Title", report["title"])
        self.assertEqual("Test Author", report["author"])
        self.assertEqual("en", report["language"])
        self.assertEqual(2, report["spine_documents"])
        validation = validate_epub_readaloud(epub_bytes)
        self.assertEqual(2, validation["smil_files"])
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as archive:
            names = archive.namelist()
            self.assertEqual("mimetype", names[0])
            # Cover bytes preserved verbatim.
            self.assertEqual(b"\xff\xd8\xff\xe0\xff\xd9", archive.read("OEBPS/cover.jpg"))
            smil = archive.read("OEBPS/ch1.smil").decode("utf-8")
            self.assertIn("clipBegin=", smil)
            self.assertIn("clipEnd=", smil)
            ch1 = archive.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertIn('id="aa2-p-', ch1)

    def test_edited_script_fails_closed(self):
        source = _make_source_epub()
        edited = _script_entries()
        edited[0]["prepared_text"] = "Completely rewritten opening line."
        with self.assertRaises(ValueError):
            build_epub_readaloud(retained_epub_bytes=source, audio_bytes=b"x",
                                 audio_filename="audiobook.mp3", script_entries=edited)

    def test_missing_audio_frames_fail_closed(self):
        source = _make_source_epub()
        bad = _script_entries()
        bad[1]["end_frame"] = bad[1]["start_frame"]
        with self.assertRaises(ValueError):
            build_epub_readaloud(retained_epub_bytes=source, audio_bytes=b"x",
                                 audio_filename="audiobook.mp3", script_entries=bad)

    def test_ambiguous_mapping_fails_closed(self):
        source = _make_source_epub()
        root, segments, _full = get_xhtml_segments(source, "OEBPS/ch1.xhtml")
        # Two identical segments mapping against one script entry is ambiguous.
        duplicate = [segments[0], segments[0]]
        with self.assertRaises(ValueError):
            map_spine_to_chunks(duplicate, _script_entries()[:1], start=0)

    def test_dangling_smil_reference_fails_validation(self):
        source = _make_source_epub()
        epub_bytes, _report = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
            script_entries=_script_entries())
        # Corrupt one SMIL fragment so validation must fail.
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as src, zipfile.ZipFile(buffer, 'w') as out:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "OEBPS/ch1.smil":
                    data = data.replace(b"aa2-p-0001", b"aa2-missing")
                out.writestr(item.filename, data)
        with self.assertRaises(ValueError):
            validate_epub_readaloud(buffer.getvalue())


_BOGUS_DECLARATION = b'<?xml version="1.0" encoding="bogus"?>'
_MULTIBYTE_DECLARATIONS = tuple(
    f'<?xml version="1.0" encoding="{codec}"?>'.encode('ascii')
    for codec in ('shift_jis', 'utf-16-le'))
# Expat hands these declarations to Python's codec, which raises UnicodeError
# instead of an Expat refusal; the shared unusable-codec decision maps them to
# the same malformed-input contracts (#2370/#2375).
_PYTHON_CODEC_DECLARATIONS = tuple(
    f'<?xml version="1.0" encoding="{codec}"?>'.encode('ascii')
    for codec in ('punycode', 'idna', 'undefined'))
_UNUSABLE_DECLARATIONS = ((_BOGUS_DECLARATION,)
                          + _MULTIBYTE_DECLARATIONS
                          + _PYTHON_CODEC_DECLARATIONS)
_NCX = (b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        b'<navMap><navPoint id="n1"><navLabel><text>Chapter One</text></navLabel>'
        b'<content src="ch1.xhtml"/></navPoint></navMap></ncx>')


def _put_member(archive_bytes, name, data):
    """Return a copy of an EPUB with one member added or replaced."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as source, \
            zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            payload = data if info.filename == name else source.read(info.filename)
            target.writestr(info, payload)
        if name not in source.namelist():
            target.writestr(name, data)
    return buffer.getvalue()


def _replace_declaration(archive_bytes, name, declaration):
    """Return a copy with ``name``'s XML declaration replaced by ``declaration``."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as source:
        original = source.read(name)
    end = original.find(b'?>')
    body = original[end + 2:] if original.startswith(b'<?xml') and end != -1 else original
    return _put_member(archive_bytes, name, declaration + body)


class UnknownDeclaredEncodingReaderTests(unittest.TestCase):
    """#2368: an unreadable declaration must not abort the read-aloud readers."""

    def test_bogus_nav_declaration_leaves_the_readaloud_toc_empty(self):
        nav = (_BOGUS_DECLARATION +
               b'<html xmlns="http://www.w3.org/1999/xhtml" '
               b'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title>'
               b'</head><body><nav epub:type="toc"><ol>'
               b'<li><a href="ch1.xhtml">Chapter One</a></li>'
               b'</ol></nav></body></html>')
        raw = _put_member(_make_source_epub(), 'OEBPS/nav.xhtml', nav)
        _opf_path, _package, _namespace, _manifest, spine, metadata = get_spine_documents(raw)
        self.assertEqual([], metadata['toc'])
        self.assertEqual(['OEBPS/ch1.xhtml', 'OEBPS/ch2.xhtml'],
                         [document['path'] for document in spine])
        self.assertEqual('Synthetic Title', metadata['title'])

    def test_bogus_ncx_declaration_returns_no_ncx_points(self):
        raw = _put_member(_make_source_epub(), 'OEBPS/toc.ncx',
                          _BOGUS_DECLARATION + _NCX)
        manifest = {'ncx': {'path': 'OEBPS/toc.ncx',
                            'media_type': 'application/x-dtbncx+xml'}}
        self.assertEqual([], get_ncx_points(raw, manifest, 'OEBPS/content.opf'))

    def test_multibyte_navigation_declarations_are_skipped(self):
        # E2: expat's second refusal is ValueError, not LookupError; it must
        # reach the same malformed-navigation fallback, not abort the reader.
        for declaration in _MULTIBYTE_DECLARATIONS + _PYTHON_CODEC_DECLARATIONS:
            with self.subTest(member='nav', declaration=declaration):
                raw = _replace_declaration(_make_source_epub(), 'OEBPS/nav.xhtml',
                                           declaration)
                _opf, _package, _ns, _manifest, spine, metadata = get_spine_documents(raw)
                self.assertEqual([], metadata['toc'])
                self.assertEqual(2, len(spine))
            with self.subTest(member='ncx', declaration=declaration):
                raw = _put_member(_make_source_epub(), 'OEBPS/toc.ncx',
                                  declaration + _NCX)
                manifest = {'ncx': {'path': 'OEBPS/toc.ncx',
                                    'media_type': 'application/x-dtbncx+xml'}}
                self.assertEqual([], get_ncx_points(raw, manifest, 'OEBPS/content.opf'))

    def test_padded_python_codec_declarations_are_skipped(self):
        # Gate-2 B1: the declaration is read to its closing ``?>`` now, so a
        # padded punycode/idna/undefined is classified like the unpadded form
        # at the nav/NCX/spine read-aloud readers instead of escaping as raw
        # codec text.
        for codec in ('punycode', 'idna', 'undefined'):
            for pad in (230, 1000, 10000):
                declaration = ('<?xml version="1.0"'.encode('ascii')
                               + b' ' * pad + f' encoding="{codec}"?>'.encode('ascii'))
                with self.subTest(member='nav', codec=codec, pad=pad):
                    raw = _replace_declaration(_make_source_epub(), 'OEBPS/nav.xhtml',
                                               declaration)
                    _opf, _package, _ns, _manifest, spine, metadata = \
                        get_spine_documents(raw)
                    self.assertEqual([], metadata['toc'])
                    self.assertEqual(2, len(spine))
                with self.subTest(member='ncx', codec=codec, pad=pad):
                    raw = _put_member(_make_source_epub(), 'OEBPS/toc.ncx',
                                      declaration + _NCX)
                    manifest = {'ncx': {'path': 'OEBPS/toc.ncx',
                                        'media_type': 'application/x-dtbncx+xml'}}
                    self.assertEqual(
                        [], get_ncx_points(raw, manifest, 'OEBPS/content.opf'))
                with self.subTest(member='spine', codec=codec, pad=pad):
                    raw = _replace_declaration(_make_source_epub(), 'OEBPS/ch1.xhtml',
                                               declaration)
                    with self.assertRaisesRegex(
                            ValueError,
                            '^Spine document is malformed: OEBPS/ch1.xhtml$'):
                        get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')

    def test_a_utf8_bom_does_not_hide_a_padded_declaration(self):
        # Gate-2 L2: the classifier skips the leading UTF-8 BOM before looking
        # for ``<?xml``. If that skip breaks, this padded ``idna`` declaration
        # is invisible and the raw codec error escapes at every reader.
        declaration = (b'\xef\xbb\xbf<?xml version="1.0"' + b' ' * 10000
                       + b' encoding="idna"?>')
        raw = _replace_declaration(_make_source_epub(), 'OEBPS/nav.xhtml',
                                   declaration)
        _opf, _package, _ns, _manifest, spine, metadata = get_spine_documents(raw)
        self.assertEqual([], metadata['toc'])
        self.assertEqual(2, len(spine))
        raw = _put_member(_make_source_epub(), 'OEBPS/toc.ncx', declaration + _NCX)
        manifest = {'ncx': {'path': 'OEBPS/toc.ncx',
                            'media_type': 'application/x-dtbncx+xml'}}
        self.assertEqual([], get_ncx_points(raw, manifest, 'OEBPS/content.opf'))
        raw = _replace_declaration(_make_source_epub(), 'OEBPS/ch1.xhtml',
                                   declaration)
        with self.assertRaisesRegex(
                ValueError, '^Spine document is malformed: OEBPS/ch1.xhtml$'):
            get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')

    def test_unreadable_spine_document_declarations_are_malformed(self):
        for declaration in _UNUSABLE_DECLARATIONS:
            with self.subTest(declaration=declaration):
                raw = _replace_declaration(_make_source_epub(), 'OEBPS/ch1.xhtml',
                                           declaration)
                with self.assertRaisesRegex(
                        ValueError, '^Spine document is malformed: OEBPS/ch1.xhtml$'):
                    get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')

    def test_unreadable_built_xhtml_declarations_are_malformed(self):
        built, _report = build_epub_readaloud(
            retained_epub_bytes=_make_source_epub(), audio_bytes=b'\xff\xfbx',
            audio_filename='audiobook.mp3', script_entries=_script_entries())
        for declaration in _UNUSABLE_DECLARATIONS:
            with self.subTest(declaration=declaration):
                raw = _replace_declaration(built, 'OEBPS/ch1.xhtml', declaration)
                with self.assertRaisesRegex(ValueError, 'Built EPUB XHTML is malformed'):
                    validate_epub_readaloud(raw)

    def test_unreadable_built_smil_declarations_are_malformed(self):
        built, _report = build_epub_readaloud(
            retained_epub_bytes=_make_source_epub(), audio_bytes=b'\xff\xfbx',
            audio_filename='audiobook.mp3', script_entries=_script_entries())
        for declaration in _UNUSABLE_DECLARATIONS:
            with self.subTest(declaration=declaration):
                raw = _replace_declaration(built, 'OEBPS/ch1.smil', declaration)
                with self.assertRaisesRegex(ValueError, 'Built EPUB SMIL is malformed'):
                    validate_epub_readaloud(raw)

    def test_unreadable_retained_container_declarations_are_malformed(self):
        # get_spine_documents normally parses container.xml first and refuses
        # it as a package (#2375), so bypass that read to exercise the build's
        # own retained-container handler.
        source = _make_source_epub()
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            opf_path, package = er.get_epub_package(archive)
        for declaration in _UNUSABLE_DECLARATIONS:
            with self.subTest(declaration=declaration):
                raw = _replace_declaration(
                    source, 'META-INF/container.xml', declaration)
                with mock.patch.object(er, 'get_epub_package',
                                       return_value=(opf_path, package)):
                    with self.assertRaisesRegex(
                            ValueError, 'Retained EPUB container is malformed'):
                        build_epub_readaloud(
                            retained_epub_bytes=raw, audio_bytes=b'\xff\xfbx',
                            audio_filename='audiobook.mp3',
                            script_entries=_script_entries())


class MalformedHrefReaderTests(unittest.TestCase):
    """#2377 rev2: one unparsable-href rule in every read-aloud resolver."""

    _MALFORMED = ('http://[', 'http://[::1', '//[')
    _NAV = (b'<html xmlns="http://www.w3.org/1999/xhtml" '
            b'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            b'<nav epub:type="toc"><ol>'
            b'<li><a href="{href}">Bad</a></li>'
            b'<li><a href="ch1.xhtml">Chapter One</a></li>'
            b'</ol></nav></body></html>')

    def _opf_with_href(self, item_id, href):
        source = _make_source_epub()
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            opf = archive.read('OEBPS/content.opf')
        marker = f'id="{item_id}" href="'.encode()
        start = opf.index(marker) + len(marker)
        end = opf.index(b'"', start)
        return _put_member(source, 'OEBPS/content.opf',
                           opf[:start] + href.encode() + opf[end:])

    def test_a_malformed_manifest_href_is_the_documented_external_refusal(self):
        for href in self._MALFORMED:
            with self.subTest(href=href):
                raw = self._opf_with_href('c1', href)
                with self.assertRaisesRegex(
                        ValueError, 'Retained EPUB has an external manifest reference'):
                    get_spine_documents(raw)
                with self.assertRaisesRegex(
                        ValueError, 'Retained EPUB has an external manifest reference'):
                    build_epub_readaloud(
                        retained_epub_bytes=raw, audio_bytes=b'\xff\xfbx',
                        audio_filename='audiobook.mp3',
                        script_entries=_script_entries())

    def test_a_malformed_nav_href_is_skipped_like_an_external_link(self):
        for href in self._MALFORMED:
            with self.subTest(href=href):
                raw = _put_member(_make_source_epub(), 'OEBPS/nav.xhtml',
                                  self._NAV.replace(b'{href}', href.encode()))
                _opf, _package, _ns, _manifest, spine, metadata = get_spine_documents(raw)
                self.assertEqual(['OEBPS/ch1.xhtml', 'OEBPS/ch2.xhtml'],
                                 [document['path'] for document in spine])
                self.assertEqual(['OEBPS/ch1.xhtml'],
                                 [link['path'] for link in metadata['toc']])

    def test_a_malformed_ncx_content_src_is_the_documented_external_refusal(self):
        for href in self._MALFORMED:
            with self.subTest(href=href):
                ncx = ('<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
                       'version="2005-1"><navMap><navPoint id="n1">'
                       '<navLabel><text>Chapter One</text></navLabel>'
                       f'<content src="{href}"/></navPoint></navMap></ncx>').encode()
                raw = _put_member(_make_source_epub(), 'OEBPS/toc.ncx', ncx)
                manifest = {'ncx': {'path': 'OEBPS/toc.ncx',
                                    'media_type': 'application/x-dtbncx+xml'}}
                with self.assertRaisesRegex(
                        ValueError, 'Retained EPUB NCX has an external NCX reference'):
                    get_ncx_points(raw, manifest, 'OEBPS/content.opf')

    def test_a_malformed_retained_smil_href_is_the_documented_refusal(self):
        source = _make_source_epub()
        opf_path, package, namespace, manifest, spine, meta = get_spine_documents(source)
        manifest_el = package.find(namespace + 'manifest')
        item = ET.SubElement(manifest_el, namespace + 'item')
        item.set('id', 'bad-smil')
        item.set('href', 'http://[')
        item.set('media-type', er.SMIL_MEDIA_TYPE)
        with mock.patch.object(
                er, 'get_spine_documents',
                return_value=(opf_path, package, namespace, manifest, spine, meta)):
            with self.assertRaisesRegex(
                    ValueError,
                    'Retained EPUB has an external/fragment SMIL reference'):
                build_epub_readaloud(
                    retained_epub_bytes=source, audio_bytes=b'\xff\xfbx',
                    audio_filename='audiobook.mp3',
                    script_entries=_script_entries())

    def test_a_malformed_href_in_a_built_epub_is_the_documented_refusal(self):
        built, _report = build_epub_readaloud(
            retained_epub_bytes=_make_source_epub(), audio_bytes=b'\xff\xfbx',
            audio_filename='audiobook.mp3', script_entries=_script_entries())
        with zipfile.ZipFile(io.BytesIO(built)) as archive:
            opf = archive.read('OEBPS/content.opf')
            nav = archive.read('OEBPS/nav.xhtml')
            smil = archive.read('OEBPS/ch1.smil')
        cases = (
            ('manifest', 'OEBPS/content.opf',
             opf.replace(b'href="ch1.xhtml"', b'href="http://["', 1),
             'Built EPUB manifest has an external/fragment reference'),
            ('nav', 'OEBPS/nav.xhtml',
             nav.replace(b'href="ch1.xhtml"', b'href="http://["', 1),
             'Built EPUB nav link is external'),
            ('audio', 'OEBPS/ch1.smil',
             smil.replace(b'src="audio/audiobook.mp3"', b'src="http://["'),
             'Built EPUB SMIL audio reference is invalid'),
        )
        for name, member, data, message in cases:
            with self.subTest(case=name):
                with self.assertRaisesRegex(ValueError, message):
                    validate_epub_readaloud(_put_member(built, member, data))


if __name__ == "__main__":
    unittest.main()
