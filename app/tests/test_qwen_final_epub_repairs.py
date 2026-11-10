"""Tiny production EPUB admission, archive and fragment regressions (#1844)."""
import io
import os
from contextlib import contextmanager
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
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


def _with_extra_members(raw, members):
    buffer = io.BytesIO(raw)
    with zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED) as output:
        for name, payload in members.items():
            output.writestr(name, payload)
    return buffer.getvalue()


def _with_compression_method(raw, member, method):
    changed = bytearray(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        local = archive.getinfo(member).header_offset
    struct.pack_into("<H", changed, local + 8, method)
    position = 0
    while True:
        position = changed.index(b"PK\x01\x02", position)
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", changed, position + 28)
        if changed[position + 46:position + 46 + name_len] == member.encode("utf-8"):
            struct.pack_into("<H", changed, position + 10, method)
            return bytes(changed)
        position += 46 + name_len + extra_len + comment_len


@contextmanager
def _changed_named_identity(source, *, after_eof=True):
    """Model namespace replacement without claiming a physical OS race."""
    import audio_take_history
    real_open = audio_take_history._open_regular
    real_lstat = os.lstat
    state = {"changed": False, "named_checks": 0}

    class ReadProxy:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            value = self.stream.read(size)
            if after_eof and not value:
                state["changed"] = True
            return value

    def open_proxy(*args, **kwargs):
        stream, before = real_open(*args, **kwargs)
        return ReadProxy(stream), before

    def named_version(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if Path(path) == source and state["changed"]:
            state["named_checks"] += 1
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1,
                st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
                st_ctime_ns=info.st_ctime_ns)
        return info

    with patch.object(audio_take_history, "_open_regular", side_effect=open_proxy), \
            patch.object(os, "lstat", side_effect=named_version):
        yield state


class FinalEpubAdmissionTests(unittest.TestCase):
    def test_retained_bytes_and_hash_require_current_named_identity(self):
        for reader in (readalong_export.get_retained_epub_bytes,
                       readalong_export.get_retained_epub_sha256):
            with self.subTest(reader=reader.__name__), \
                    tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
                source = Path(tmp) / "book.source.epub"
                source.write_bytes(_source_epub("<p>Echo.</p>"))
                with _changed_named_identity(source) as state:
                    with self.assertRaises(ValueError):
                        reader(source)
                self.assertTrue(state["changed"])
                self.assertGreater(state["named_checks"], 0)

    def test_language_path_requires_current_named_identity(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            source = Path(tmp) / "book.source.epub"
            source.write_bytes(_source_epub("<p>Echo.</p>"))
            read_package = er.get_epub_package
            with _changed_named_identity(source, after_eof=False) as state:
                def change_after_parse(*args, **kwargs):
                    result = read_package(*args, **kwargs)
                    state["changed"] = True
                    return result

                with patch.object(er, "get_epub_package", side_effect=change_after_parse):
                    with self.assertRaises(ValueError):
                        er.get_epub_language_from_path(source)
            self.assertGreater(state["named_checks"], 0)

    def test_root_alias_member_refused_before_source_copy(self):
        args = _build_fragments(2)
        args["retained_epub_bytes"] = _with_extra_members(
            args["retained_epub_bytes"], {".": b"root alias sentinel"})
        with self.assertRaisesRegex(ValueError, "Unsafe EPUB archive member"):
            er.build_epub_readaloud(**args)

    def test_root_alias_member_refused_by_built_validator(self):
        raw, _report = er.build_epub_readaloud(**_build_fragments(2))
        raw = _with_extra_members(raw, {".": b"root alias sentinel"})
        with self.assertRaisesRegex(ValueError, "Unsafe EPUB archive member"):
            er.validate_epub_readaloud(raw)

    def test_nested_encoded_and_noncover_payloads_preserved(self):
        members = {"OEBPS/styles/book.css": b"p { color: black; }",
                   "OEBPS/fonts/book%20font.woff": b"synthetic opaque font",
                   "OEBPS/images/extra.png": b"synthetic opaque image",
                   "OEBPS/toc.ncx": b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"/>'}
        args = _build_fragments(2)
        args["retained_epub_bytes"] = _with_extra_members(args["retained_epub_bytes"], members)
        raw, _report = er.build_epub_readaloud(**args)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(members, {name: archive.read(name) for name in members})

    def test_unsupported_metadata_compression_has_value_error_contract(self):
        raw = _source_epub("<p>Echo.</p>")
        for member in ("META-INF/container.xml", "OEBPS/content.opf"):
            with self.subTest(member=member):
                changed = _with_compression_method(raw, member, 99)
                with self.assertRaisesRegex(ValueError, "unsupported or unavailable") as caught:
                    er.get_epub_language_from_path(io.BytesIO(changed))
                self.assertIsInstance(caught.exception.__cause__, NotImplementedError)

    def test_optional_metadata_codecs_preserved_or_truthfully_unavailable(self):
        raw = _source_epub("<p>Echo.</p>")
        for method, module in ((zipfile.ZIP_BZIP2, "bz2"), (zipfile.ZIP_LZMA, "lzma")):
            with self.subTest(module=module):
                if getattr(zipfile, module) is not None:
                    output = io.BytesIO()
                    with zipfile.ZipFile(io.BytesIO(raw)) as source, \
                            zipfile.ZipFile(output, "w", method) as target:
                        for name in source.namelist():
                            target.writestr(name, source.read(name))
                    compressed = output.getvalue()
                    self.assertEqual("en", er.get_epub_language_from_path(io.BytesIO(compressed)))
                else:
                    compressed = _with_compression_method(raw, "META-INF/container.xml", method)
                with patch.object(zipfile, module, None):
                    with self.assertRaisesRegex(ValueError, "unsupported or unavailable") as caught:
                        er.get_epub_language_from_path(io.BytesIO(compressed))
                self.assertIsInstance(caught.exception.__cause__, RuntimeError)
                self.assertNotIn("malformed", str(caught.exception))

    def test_unavailable_metadata_codec_diagnostic_is_bounded(self):
        raw = _source_epub("<p>Echo.</p>")
        cause = RuntimeError("x" * 1024)
        with patch.object(er, "get_epub_package", side_effect=cause):
            with self.assertRaisesRegex(ValueError, "unsupported or unavailable") as caught:
                er.get_epub_language_from_path(io.BytesIO(raw))
        self.assertIs(cause, caught.exception.__cause__)
        self.assertLess(len(str(caught.exception)), 400)

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
    def test_distinct_source_elements_with_id_alias_collisions_refuse(self):
        for first, second in (("xml:id", "xml:id"), ("id", "xml:id")):
            with self.subTest(first=first, second=second):
                body = '<p>One <em %s="dup">A</em> two. <span %s="dup">B</span> three.</p>'
                args = _build_fragments(1)
                args["retained_epub_bytes"] = _source_epub(body % (first, second))
                args["script_entries"][0].update(prepared_text="One A two. B three.", end_frame=50)
                args["aligned_rows"] = [{"tokens": [
                    {"text": word, "start_time": index * 0.01, "end_time": (index + 1) * 0.01}
                    for index, word in enumerate(("One", "A", "two", "B", "three"))]}]
                with self.assertRaisesRegex(ValueError, "Duplicate element id"):
                    er.build_epub_readaloud(**args)

    def test_equal_source_aliases_and_straddling_element_preserved(self):
        args = _build_fragments(1)
        args["retained_epub_bytes"] = _source_epub(
            '<p>One <em id="keep" xml:id="keep">A two. B</em> three.</p>')
        args["script_entries"][0].update(prepared_text="One A two. B three.", end_frame=50)
        args["aligned_rows"] = [{"tokens": [
            {"text": word, "start_time": index * 0.01, "end_time": (index + 1) * 0.01}
            for index, word in enumerate(("One", "A", "two", "B", "three"))]}]
        raw, _report = er.build_epub_readaloud(**args)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            root = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
        body = root.find("{%s}body" % er.XHTML_NS)
        self.assertEqual("One A two. B three.", "".join(body.itertext()))
        self.assertEqual(1, sum(node.get("id") == "keep" for node in body.iter()))
        self.assertEqual(1, sum(node.get("{http://www.w3.org/XML/1998/namespace}id") == "keep"
                                for node in body.iter()))

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
                # This monitors the known set-rescan regression, not every
                # possible future allocator implementation or wall-clock cost.
                self.assertGreater(counter["checks"], 0, "Allocator work probe was not exercised")
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
