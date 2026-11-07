"""AA2 EPUB 3 read-aloud (#1779) — source-preserving SMIL, fail-closed mapping."""
import io
import zipfile
import unittest

from epub_readaloud import (build_epub_readaloud, map_spine_to_chunks,
                            validate_epub_readaloud, get_xhtml_segments)


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


if __name__ == "__main__":
    unittest.main()
