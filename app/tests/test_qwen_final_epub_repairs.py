"""Tiny production EPUB admission, archive and fragment regressions (#1844)."""
import io
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET

import epub_readaloud as er
import readalong_export
from tests.test_epub_readaloud_sentences import _source_epub


def _short_declared_member():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("part.bin", b"actual payload")
    raw = bytearray(buffer.getvalue())
    # Keep the real compressed payload and its valid CRC, but lie about
    # uncompressed length in both headers. No large allocation is involved.
    central = raw.index(b"PK\x01\x02")
    local = raw.index(b"PK\x03\x04")
    struct.pack_into("<I", raw, central + 24, len(b"actual payload") + 1)
    struct.pack_into("<I", raw, local + 22, len(b"actual payload") + 1)
    return bytes(raw)


def _replace_member(raw, name, replacement):
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, \
            zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        for member in source.namelist():
            if member == name and replacement is None:
                continue
            output.writestr(member, replacement if member == name else source.read(member))
    return buffer.getvalue()


def _build_fragments(count, *, reserved=0):
    body = "".join('<p%s>Echo.</p>' %
                   (' id="aa2-p-%04d"' % (index + 1) if index < reserved else "")
                   for index in range(count))
    entries = [{"index": index, "prepared_text": "Echo.",
                "start_frame": index * 10, "end_frame": (index + 1) * 10,
                "sample_rate": 1000} for index in range(count)]
    aligned = [{"tokens": [{"text": "Echo", "start_time": 0.0,
                             "end_time": 0.01}]} for _ in range(count)]
    return dict(retained_epub_bytes=_source_epub(body), audio_bytes=b"synthetic mp3",
                audio_filename="audiobook.mp3", script_entries=entries,
                aligned_rows=aligned)


class FinalEpubAdmissionTests(unittest.TestCase):
    def test_language_paths_obey_retained_bound(self):
        raw = _source_epub("<p>Echo.</p>")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            source = Path(tmp) / "book.source.epub"
            source.write_bytes(raw)
            with patch.object(readalong_export, "MAX_RETAINED_EPUB_BYTES", len(raw) - 1):
                with self.assertRaises(ValueError):
                    er.get_epub_language_from_path(source)

    def test_language_paths_obey_alias_admission(self):
        import direct_path_admission
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            source = Path(tmp) / "book.source.epub"
            source.write_bytes(_source_epub("<p>Echo.</p>"))
            plain = direct_path_admission.is_plain_path

            def refuse_source(path):
                return False if Path(path) == source else plain(path)

            with patch.object(direct_path_admission, "is_plain_path", side_effect=refuse_source):
                with self.assertRaises(ValueError):
                    er.get_epub_language_from_path(source)

    def test_language_paths_verify_descriptor_version(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            source = Path(tmp) / "book.source.epub"
            source.write_bytes(_source_epub("<p>Echo.</p>"))
            read_package = er.get_epub_package

            def change_after_parse(*args, **kwargs):
                result = read_package(*args, **kwargs)
                info = source.stat()
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
                return result

            with patch.object(er, "get_epub_package", side_effect=change_after_parse):
                with self.assertRaises(ValueError):
                    er.get_epub_language_from_path(source)

    def test_language_reader_preserves_bytesio_and_regular_hardlinks(self):
        raw = _source_epub("<p>Echo.</p>")
        self.assertEqual("en", er.get_epub_language_from_path(io.BytesIO(raw)))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            source = Path(tmp) / "book.source.epub"
            source.write_bytes(raw)
            other = Path(tmp) / "backup.epub"
            os.link(source, other)
            self.assertEqual("en", er.get_epub_language_from_path(source))
            self.assertEqual("en", er.get_epub_language_from_path(other))
            self.assertEqual(raw, source.read_bytes())

    def test_language_parser_has_value_error_contract(self):
        raw = _source_epub("<p>Echo.</p>")
        for member in ("META-INF/container.xml", "OEBPS/content.opf"):
            with self.subTest(member=member):
                broken = _replace_member(raw, member, b"<malformed")
                with self.assertRaises(ValueError):
                    er.get_epub_language_from_path(io.BytesIO(broken))
        missing = _replace_member(raw, "META-INF/container.xml", None)
        with self.assertRaises(ValueError):
            er.get_epub_language_from_path(io.BytesIO(missing))
        for error in (KeyError("missing container"), RecursionError("deep metadata")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(er, "get_epub_package", side_effect=error):
                with self.assertRaises(ValueError):
                    er.get_epub_language_from_path(io.BytesIO(raw))

    def test_bounded_member_rejects_short_declared_payload(self):
        with zipfile.ZipFile(io.BytesIO(_short_declared_member())) as source:
            with self.assertRaisesRegex(ValueError, "size|length|declared"):
                er._read_archive_member_bounded(source, "part.bin", 1024)

    def test_streamed_member_rejects_short_declared_payload(self):
        with zipfile.ZipFile(io.BytesIO(_short_declared_member())) as source, \
                zipfile.ZipFile(io.BytesIO(), "w", zipfile.ZIP_DEFLATED) as output:
            with self.assertRaisesRegex(ValueError, "size|length|declared"):
                er._stream_retained_member(source, output, "part.bin")

    def test_streamed_zip64_member_roundtrip(self):
        payload = b"small ZIP64 payload" * 8
        before, after = io.BytesIO(), io.BytesIO()
        with patch.object(zipfile, "ZIP64_LIMIT", 32):
            with zipfile.ZipFile(before, "w", zipfile.ZIP_DEFLATED) as source:
                source.writestr("part.bin", payload)
            with zipfile.ZipFile(io.BytesIO(before.getvalue())) as source, \
                    zipfile.ZipFile(after, "w", zipfile.ZIP_DEFLATED) as output:
                er._stream_retained_member(source, output, "part.bin")
        with zipfile.ZipFile(io.BytesIO(after.getvalue())) as output:
            self.assertEqual(payload, output.read("part.bin"))
            self.assertEqual(len(payload), output.getinfo("part.bin").file_size)
            self.assertGreaterEqual(output.getinfo("part.bin").extract_version, 45)


class FinalEpubFragmentTests(unittest.TestCase):
    def _counted_set(self, counter, cancel_after=None):
        class CountedSet(set):
            def __init__(self, *args):
                super().__init__(*args)
                self.monitored = bool(args)

            def __contains__(self, value):
                if self.monitored and isinstance(value, str) and value.startswith("aa2-p-"):
                    counter["checks"] += 1
                    if cancel_after is not None and counter["checks"] >= cancel_after:
                        counter["cancel"] = True
                return super().__contains__(value)
        return CountedSet

    def test_fresh_fragment_membership_work_is_linear(self):
        for count in (64, 128):
            with self.subTest(paragraphs=count):
                counter = {"checks": 0}
                with patch.object(er, "set", self._counted_set(counter), create=True):
                    raw, _report = er.build_epub_readaloud(**_build_fragments(count))
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    document = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
                paragraphs = document.findall(".//{%s}p" % er.XHTML_NS)
                self.assertEqual(["aa2-p-%04d" % (index + 1) for index in range(count)],
                                 [item.get("id") for item in paragraphs])
                self.assertEqual("Echo." * count, "".join(document.find(
                    "{%s}body" % er.XHTML_NS).itertext()))
                self.assertLessEqual(counter["checks"], 4 * count)

    def test_fresh_fragment_collision_loop_polls_cancel(self):
        counter = {"checks": 0, "cancel": False}
        with patch.object(er, "set", self._counted_set(counter, cancel_after=3), create=True):
            with self.assertRaisesRegex(ValueError, "cancelled"):
                er.build_epub_readaloud(**_build_fragments(129, reserved=128),
                                        cancel_check=lambda: counter["cancel"])
        self.assertLessEqual(counter["checks"], 4)

    def test_mixed_fragment_ids_keep_legacy_order(self):
        source = _source_epub('<p id="aa2-p-0001">Echo.</p><p>One. Two.</p>'
                              '<p id="aa2-p-0003">Echo.</p><p>Echo.</p><p>Echo.</p>')
        texts = ["Echo.", "One. Two.", "Echo.", "Echo.", "Echo."]
        entries = [{"index": index, "prepared_text": text,
                    "start_frame": index * 20, "end_frame": (index + 1) * 20,
                    "sample_rate": 1000} for index, text in enumerate(texts)]
        aligned = [{"tokens": [{"text": word, "start_time": index * 0.01,
                                  "end_time": (index + 1) * 0.01}
                                 for index, word in enumerate(text.replace(".", "").split())]}
                   for text in texts]
        raw, _report = er.build_epub_readaloud(retained_epub_bytes=source,
            audio_bytes=b"synthetic mp3", audio_filename="audiobook.mp3",
            script_entries=entries, aligned_rows=aligned)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            document = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
        self.assertEqual(["aa2-p-0001", "aa2-p-0002", "aa2-p-0004",
                          "aa2-p-0003", "aa2-p-0005", "aa2-p-0006"],
                         [node.get("id") for node in document.iter() if node.get("id")])


if __name__ == "__main__":
    unittest.main()
