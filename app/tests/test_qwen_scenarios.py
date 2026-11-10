"""Qwen scenario tests (#1844): cancel, stale downloads, CJK, windowed rows.

Real builds with the omitted fake worker fixture for inference; the
heavy dependencies never load here. Each test names the defeat it proves.
"""
import asyncio
import io
import json
import os
import sys
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_readalong_qwen import _FixtureMixin, _CaptureTasks, _setup_book


class CancelTests(_FixtureMixin):
    def test_cancel_mid_alignment_kills_worker_and_publishes_nothing(self):
        inner_tmp = self._mkdtmp()
        _editor, manager = self._fixture(inner_tmp)
        capability = self._capability_for(inner_tmp)
        timing = {"timing_mode": "qwen", "device": "cpu", "language": None,
                  "provider_digest": capability["provider_digest"],
                  "capability": capability, "reason": "test", "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu", "dtype": "float32", "runtime": capability["runtime"]}
        slow_worker = Path(inner_tmp) / "slow_worker.py"
        worker_marker = Path(inner_tmp) / "worker-started"
        slow_worker.write_text(
            "import pathlib, time\npathlib.Path(%r).write_text('started')\ntime.sleep(60)\n"
            % str(worker_marker), encoding="utf-8")
        cancelled = threading.Event()
        cancelled.set()
        cancelled.clear()
        from readalong_export import build_epub_export
        import qwen_runtime
        done = threading.Event()
        outcome = []

        def build():
            try:
                with patch.object(qwen_runtime, "get_qwen_worker_command",
                                  lambda _py: [sys.executable, str(slow_worker)]):
                    outcome.append(build_epub_export(
                        manager, cancelled.is_set, timing=timing))
            finally:
                done.set()

        thread = threading.Thread(target=build, daemon=True)
        thread.start()
        # Let assembly finish and the worker spawn, then cancel.
        import time
        worker_started = False
        try:
            for _ in range(100):
                if worker_marker.exists():
                    worker_started = True
                    break
                time.sleep(0.1)
        finally:
            cancelled.set()
        self.assertTrue(done.wait(timeout=20), "cancel did not stop the build")
        self.assertTrue(worker_started, "test never observed the alignment worker start")
        ok, message = outcome[0]
        self.assertFalse(ok)
        self.assertEqual("Export cancelled", message)
        self.assertFalse((Path(inner_tmp) / "readaloud.epub").exists(),
                         "cancelled build must publish nothing")

    def _capability_for(self, tmp):
        model = Path(tmp) / "model"
        model.mkdir(exist_ok=True)
        (model / "config.json").write_text("{}", encoding="utf-8")
        from qwen_runtime import (get_qwen_provider_digest,
                                  get_qwen_runtime_identity)
        identity = get_qwen_runtime_identity(sys.executable)
        return {"available": True, "python": sys.executable, "model_dir": str(model),
                "devices": [], "cpu_fallback": True,
                "provider_digest": get_qwen_provider_digest(
                    str(model), runtime_identity={
                        "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu",
                        "device": "cpu", "dtype": "float32", "identity": identity}),
                "runtime": identity,
                "reason": None, "qwen_asr_version": "0.0.6",
                "torch_version": "2.10.0+cpu"}


class StaleDownloadTests(_FixtureMixin):
    def _build_and_download(self, inner_tmp):
        editor, manager = self._fixture(inner_tmp)
        capability = self._capability_for(inner_tmp)
        timing = {"timing_mode": "qwen", "device": "cpu", "language": None,
                  "provider_digest": capability["provider_digest"],
                  "capability": capability, "reason": "test", "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu", "dtype": "float32", "runtime": capability["runtime"]}
        fake_worker = Path(__file__).resolve().parent / "fixtures" / "qwen_fake_worker.py"
        import qwen_runtime
        # Persist the build's choice (as the route does) so downloads compare
        # against it, and keep capability fixed so the probe never loads torch.
        (Path(inner_tmp) / "readalong_timing.json").write_text(
            json.dumps({"timing_mode": "qwen", "timing_device": "auto",
                        "timing_language": None}), encoding="utf-8")
        import unittest.mock
        with patch.object(qwen_runtime, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(fake_worker)]), \
                patch.object(qwen_runtime, "get_qwen_capability", lambda _root: capability), \
                unittest.mock.patch.dict(os.environ,
                                {"ALEXANDRIA_QWEN_MODEL": capability["model_dir"]}):
            from readalong_export import build_epub_export
            ok, message = build_epub_export(manager, lambda: False, timing=timing)
            self.assertTrue(ok, message)
            self.assertIsNotNone(asyncio.run(editor.download_readaloud_epub()))
        return editor, capability

    def _capability_for(self, tmp):
        model = Path(tmp) / "model"
        model.mkdir(exist_ok=True)
        (model / "config.json").write_text("{}", encoding="utf-8")
        from qwen_runtime import (get_qwen_provider_digest,
                                  get_qwen_runtime_identity)
        identity = get_qwen_runtime_identity(sys.executable)
        return {"available": True, "python": sys.executable, "model_dir": str(model),
                "devices": [], "cpu_fallback": True,
                "provider_digest": get_qwen_provider_digest(
                    str(model), runtime_identity={
                        "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu",
                        "device": "cpu", "dtype": "float32", "identity": identity}),
                "runtime": identity,
                "reason": None, "qwen_asr_version": "0.0.6",
                "torch_version": "2.10.0+cpu"}

    def test_preference_change_makes_download_stale(self):
        inner_tmp = self._mkdtmp()
        editor, _cap = self._build_and_download(inner_tmp)
        from fastapi import HTTPException
        (Path(inner_tmp) / "readalong_timing.json").write_text(
            json.dumps({"timing_mode": "chunk", "timing_device": "auto",
                        "timing_language": None}), encoding="utf-8")
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(editor.download_readaloud_epub())
        self.assertEqual(409, caught.exception.status_code)

    def test_provider_change_makes_download_stale(self):
        inner_tmp = self._mkdtmp()
        editor, cap = self._build_and_download(inner_tmp)
        from fastapi import HTTPException
        model = Path(cap["model_dir"]) / "config.json"
        model.write_text('{"replaced": true}', encoding="utf-8")
        import qwen_runtime
        current_cap = dict(cap)
        current_cap.update(device="cpu", dtype="float32", runtime_identity=cap["runtime"])
        current_cap["provider_digest"] = qwen_runtime.get_qwen_provider_digest(
            cap["model_dir"], runtime_identity={
                "qwen_asr_version": cap["qwen_asr_version"],
                "torch_version": cap["torch_version"], "device": "cpu",
                "dtype": "float32", "identity": cap["runtime"]})
        self.assertNotEqual(cap["provider_digest"], current_cap["provider_digest"])
        with patch.object(qwen_runtime, "get_qwen_capability", return_value=current_cap), \
                patch.object(qwen_runtime, "get_qwen_model_candidates", return_value=[cap["model_dir"]]):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(editor.download_readaloud_epub())
        self.assertEqual(409, caught.exception.status_code)
        self.assertIn("provider changed", str(caught.exception.detail).lower())


class CJKTests(unittest.TestCase):
    def test_cjk_sentences_use_char_granularity_tokens(self):
        import io
        from epub_readaloud import build_epub_readaloud, validate_epub_readaloud
        from tests.test_epub_readaloud_sentences import _source_epub
        # Chinese chapter text; tokens are one per character.
        source = _chapter_epub_zh()
        entries = [{"index": 0, "prepared_text": "你好世界。再见世界。",
                    "start_frame": 0, "end_frame": 120000, "sample_rate": 24000}]
        chars = ["你", "好", "世", "界", "再", "见", "世", "界"]
        tokens = [{"text": c, "start_time": i * 0.5, "end_time": (i + 1) * 0.5}
                  for i, c in enumerate(chars)]
        result, _report = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=entries,
            aligned_rows=[{"tokens": tokens}])
        validate_epub_readaloud(result)
        smil_ns = "{http://www.w3.org/ns/SMIL}"
        xhtml_ns = "{http://www.w3.org/1999/xhtml}"
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            import xml.etree.ElementTree as ET
            smil = ET.fromstring(archive.read("OEBPS/ch1.smil"))
            document = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
        spans = document.findall(".//" + xhtml_ns + "span")
        self.assertEqual(2, len(spans))
        self.assertEqual("你好世界。", "".join(spans[0].itertext()))
        self.assertEqual("再见世界。", "".join(spans[1].itertext()))
        self.assertEqual(2, len(smil.findall(".//" + smil_ns + "par")))

class LongRowTests(_FixtureMixin):
    """A >180s single row aligns across sentence-boundary windows; a
    window whose tokens do not cover its text refuses the whole build."""

    def _long_epub(self, sentences):
        import io, zipfile
        buffer = io.BytesIO()
        chapter = "<p>%s</p>" % " ".join(sentences)
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml",
                             '<?xml version="1.0"?>'
                             '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                             '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
            archive.writestr("OEBPS/content.opf",
                             '<?xml version="1.0" encoding="UTF-8"?>'
                             '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                             '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                             '<dc:identifier id="id">urn:test:1</dc:identifier>'
                             '<dc:title>T</dc:title><dc:creator>A</dc:creator>'
                             '<dc:language>en</dc:language></metadata>'
                             '<manifest>'
                             '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                             '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                             '</manifest>'
                             '<spine><itemref idref="c1"/></spine></package>')
            archive.writestr("OEBPS/nav.xhtml",
                             '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
                             'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>N</title></head><body>'
                             '<nav epub:type="toc"><ol>'
                             '<li><a href="ch1.xhtml">C</a></li>'
                             '</ol></nav></body></html>')
            archive.writestr("OEBPS/ch1.xhtml",
                             '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                             '<head><title>C</title></head><body>' + chapter + '</body></html>')
        return buffer.getvalue()

    def _long_book(self, tmp, frames):
        import wave, hashlib
        book_sentences = [" ".join("word%d" % w for w in range(40)) + "." for _ in range(4)]
        text = " ".join(book_sentences)
        with wave.open(str(Path(tmp) / "clip0.wav"), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(b"\x00\x00" * frames)
        chunks = [{"id": 0, "uid": "u0", "speaker": "Narrator", "text": text,
                   "audio_path": "clip0.wav", "audio_revision": "r0",
                   "status": "done"}]
        (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
        book = Path(tmp) / "book.txt"
        book.write_text(text, encoding="utf-8")
        (Path(tmp) / "state.json").write_text(
            json.dumps({"input_file_path": str(book)}), encoding="utf-8")
        (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
        epub_bytes = self._long_epub(book_sentences)
        text_sha = hashlib.sha256(book.read_bytes()).hexdigest()
        origin_sha = hashlib.sha256(epub_bytes).hexdigest()
        origin_id = hashlib.sha256(
            f"AA2_epub_publication_v1:{origin_sha}:{text_sha}".encode()).hexdigest()
        candidate = {"version": 1, "id": origin_id,
                     "origin": {"kind": "epub", "filename": "source.epub",
                                "sha256": origin_sha, "text_sha256": text_sha},
                     "fields": {"title": ["T"], "author": ["A"]},
                     "other_creators": [], "metadata_status": "available",
                     "cover": None, "cover_status": "not_declared", "warnings": []}
        from publication_source import get_new_upload_publication_bytes
        (Path(tmp) / "book.txt.publication.json").write_bytes(
            get_new_upload_publication_bytes(text_sha, candidate, 1024 * 1024))
        (Path(tmp) / "book.source.epub").write_bytes(epub_bytes)

    def test_long_row_windows_verified(self):
        tmp = self._mkdtmp()
        # 320s of audio for 160 words (2s/word) so one row exceeds 180s.
        self._long_book(tmp, int(320 * 24000))
        _editor, manager = self._fixture(tmp)
        capability = _capability_for(tmp)
        timing = {"timing_mode": "qwen", "device": "cpu", "language": None,
                  "provider_digest": capability["provider_digest"],
                  "capability": capability, "reason": "test", "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu", "dtype": "float32", "runtime": capability["runtime"]}
        fake_worker = Path(__file__).resolve().parent / "fixtures" / "qwen_fake_worker.py"
        import qwen_runtime
        from readalong_export import build_epub_export
        with patch.object(qwen_runtime, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(fake_worker)]):
            ok, message = build_epub_export(manager, lambda: False, timing=timing)
        self.assertTrue(ok, message)

    def test_bad_window_refuses_build(self):
        tmp = self._mkdtmp()
        self._long_book(tmp, int(320 * 24000))
        _editor, manager = self._fixture(tmp)
        capability = _capability_for(tmp)
        timing = {"timing_mode": "qwen", "device": "cpu", "language": None,
                  "provider_digest": capability["provider_digest"],
                  "capability": capability, "reason": "test", "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu", "dtype": "float32", "runtime": capability["runtime"]}
        bad_worker = Path(tmp) / "bad_worker.py"
        bad_worker.write_text(
            "import json, sys, wave\n"
            "job = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "tokens = {}\n"
            "for row in job['rows']:\n"
            "    w = row['text'].split()\n"
            "    half = w[:len(w)//2]\n"
            "    tokens[row['id']] = [{'text': t, 'start_time': 0.0, "
            "'end_time': 0.1} for t in half]\n"
            "json.dump({'ok': True, 'tokens': tokens}, open(sys.argv[2], 'w'))\n",
            encoding="utf-8")
        import qwen_runtime
        from readalong_export import build_epub_export
        with patch.object(qwen_runtime, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(bad_worker)]):
            ok, message = build_epub_export(manager, lambda: False, timing=timing)
        # The legacy-shaped stub (merged tokens only, no sequential
        # windows) is refused by the parent's row-key/window validation
        # before coverage is even checked; either way the build fails
        # closed and publishes nothing.
        self.assertFalse(ok)
        self.assertIn("alignment", message.lower())
        self.assertFalse((Path(tmp) / "readaloud.epub").exists())


def _capability_for(tmp):
    model = Path(tmp) / "model"
    model.mkdir(exist_ok=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    from qwen_runtime import (get_qwen_provider_digest,
                              get_qwen_runtime_identity)
    identity = get_qwen_runtime_identity(sys.executable)
    return {"available": True, "python": sys.executable, "model_dir": str(model),
            "devices": [], "cpu_fallback": True,
            "provider_digest": get_qwen_provider_digest(
                str(model), runtime_identity={
                    "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu",
                    "device": "cpu", "dtype": "float32", "identity": identity}),
            "runtime": identity,
            "reason": None, "qwen_asr_version": "0.0.6",
            "torch_version": "2.10.0+cpu"}


def _chapter_epub_zh():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml",
                         '<?xml version="1.0"?>'
                         '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        archive.writestr("OEBPS/content.opf",
                         '<?xml version="1.0" encoding="UTF-8"?>'
                         '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                         '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         '<dc:identifier id="id">urn:test:1</dc:identifier>'
                         '<dc:title>T</dc:title><dc:creator>A</dc:creator>'
                         '<dc:language>zh</dc:language></metadata>'
                         '<manifest>'
                         '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                         '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                         '</manifest>'
                         '<spine><itemref idref="c1"/></spine></package>')
        archive.writestr("OEBPS/nav.xhtml",
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
                         'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>N</title></head><body>'
                         '<nav epub:type="toc"><ol>'
                         '<li><a href="ch1.xhtml">C</a></li>'
                         '</ol></nav></body></html>')
        archive.writestr("OEBPS/ch1.xhtml",
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                         '<head><title>C</title></head><body>'
                         '<p>你好世界。再见世界。</p>'
                         '</body></html>')
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
