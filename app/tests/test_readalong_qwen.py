"""Qwen read-aloud integration (#1844) — bounded, fail-first.

Receipt v2/legacy semantics, Qwen stub-worker whole builds against a
synthetic nesting EPUB, GPU admission edges via the real claim
functions, and API route start with a pinned timing policy. Real MP3
encoding and real export paths run throughout; inference is the
test-only fake worker (CONTROLLED INFERENCE ONLY).
"""
import asyncio
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _clip(tmp, name, frames=4800):
    import wave
    with wave.open(str(Path(tmp) / name), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x00\x20" * frames)


def _chapter_epub():
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
                         '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
                         '<dc:language>en</dc:language></metadata>'
                         '<manifest>'
                         '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                         '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                         '<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
                         '</manifest>'
                         '<spine><itemref idref="c1"/></spine></package>')
        archive.writestr("OEBPS/nav.xhtml",
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
                         'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body>'
                         '<nav epub:type="toc"><ol>'
                         '<li><a href="ch1.xhtml">Chapter One</a></li>'
                         '</ol></nav></body></html>')
        archive.writestr("OEBPS/ch1.xhtml",
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                         '<head><title>Chapter One</title></head><body>'
                         '<section><div><h1>Chapter One</h1>'
                         '<p id="lead">Hello synthetic world. Second sentence here.</p>'
                         '</div></section>'
                         '</body></html>')
        archive.writestr("OEBPS/cover.jpg", b"\xff\xd8\xff\xe0\xff\xd9")
    return buffer.getvalue()


def _setup_book(tmp):
    texts = ["Chapter One", "Hello synthetic world. Second sentence here."]
    for i, text in enumerate(texts):
        _clip(tmp, f"clip{i}.wav")
    chunks = [{"id": i, "uid": f"u{i}", "speaker": "Narrator", "text": text,
               "audio_path": f"clip{i}.wav", "audio_revision": f"r{i}",
               "status": "done"} for i, text in enumerate(texts)]
    (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    book = Path(tmp) / "book.txt"
    book.write_text(" ".join(texts), encoding="utf-8")
    (Path(tmp) / "state.json").write_text(
        json.dumps({"input_file_path": str(book)}), encoding="utf-8")
    (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
    epub_bytes = _chapter_epub()
    text_sha = hashlib.sha256(book.read_bytes()).hexdigest()
    origin_sha = hashlib.sha256(epub_bytes).hexdigest()
    origin_id = hashlib.sha256(
        f"AA2_epub_publication_v1:{origin_sha}:{text_sha}".encode()).hexdigest()
    candidate = {"version": 1, "id": origin_id,
                 "origin": {"kind": "epub", "filename": "source.epub",
                            "sha256": origin_sha, "text_sha256": text_sha},
                 "fields": {"title": ["Synthetic Title"], "author": ["Test Author"]},
                 "other_creators": [], "metadata_status": "available",
                 "cover": None, "cover_status": "not_declared", "warnings": []}
    from publication_source import get_new_upload_publication_bytes
    (Path(tmp) / "book.txt.publication.json").write_bytes(
        get_new_upload_publication_bytes(text_sha, candidate, 1024 * 1024))
    (Path(tmp) / "book.source.epub").write_bytes(epub_bytes)
    return chunks


class _FixtureMixin(unittest.TestCase):
    def _ready_capability(self, tmp):
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
                "torch_version": "2.10.0+cpu", "device": "cpu", "dtype": "float32"}

    def _mkdtmp(self):
        tmp = tempfile.mkdtemp(prefix="qwen-")
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp

    def _release(self):
        import core
        for name in ("readaloud_epub", "audio", "lora_training", "script"):
            if name in core._task_claims:
                try:
                    core.release_gpu_task_claim(name)
                except Exception:
                    core._task_claims.pop(name, None)

    def _fixture(self, tmp):
        from routers import editor
        import core
        from project import ProjectManager
        _setup_book(tmp)
        manager = ProjectManager(tmp)
        patches = [
            patch.object(editor, "DATA_DIR", tmp),
            patch.object(editor, "SCRIPT_PATH", str(Path(tmp) / "annotated_script.json")),
            patch.object(editor, "AUDIOBOOK_PATH", str(Path(tmp) / "cloned_audiobook.mp3")),
            patch.object(editor, "project_manager", manager),
            patch.object(core, "DATA_DIR", tmp),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        editor.process_state["readaloud_epub"].update(
            {"running": False, "cancel": False, "result": None, "logs": []})
        self._release()
        self.addCleanup(self._release)
        return editor, manager


class QwenClaimTests(_FixtureMixin):
    """Qwen GPU claims exclude other GPU work in BOTH directions; chunk
    claims (cpu_only=True) stay compatible like the old exemption."""

    def test_gpu_claim_conflicts_both_ways(self):
        import core
        editor, _manager = self._fixture(self._mkdtmp())
        # Simulate a running Qwen GPU claim for readaloud_epub.
        core._task_claims["readaloud_epub"] = {"id": "c" * 32, "phase": "started",
                                              "cpu_only": False}
        try:
            # Qwen GPU readaloud_epub blocks a starting audio task...
            with self.assertRaises(Exception) as ctx:
                core.check_global_gpu_lock("audio")
            self.assertIn("readaloud", str(ctx.exception.detail).lower())
            # ... and stale task claim cleanup is the only exemption.
            self.assertIn("audio", {name for name in
                                    core.gpu_lock_conflicts("readaloud_epub")})
        finally:
            core._task_claims.pop("readaloud_epub", None)

    def test_route_qwen_gpu_refused_while_audio_runs(self):
        import core
        editor, _manager = self._fixture(self._mkdtmp())
        from fastapi import HTTPException
        core._task_claims["audio"] = {"id": "d" * 32, "phase": "started",
                                      "cpu_only": False}
        editor.process_state["audio"]["running"] = True
        gpu_ready = self._ready_capability(self._mkdtmp())
        gpu_ready["devices"] = [{"device": "cuda:0", "name": "fake-gpu",
                                 "capability": [8, 6],
                                 "free_bytes": 6 * 1024**3,
                                 "total_bytes": 8 * 1024**3}]
        import qwen_runtime
        from routers.editor import ReadaloudEpubRequest
        try:
            with patch.object(qwen_runtime, "get_qwen_capability", lambda _root: gpu_ready), \
                    self.assertRaises(HTTPException) as ctx:
                editor._start_readaloud_epub(
                    _CaptureTasks(),
                    ReadaloudEpubRequest(timing_mode="qwen"))
            self.assertEqual(400, ctx.exception.status_code)
            # Chunk mode still starts alongside the same running task.
            with patch.object(qwen_runtime, "get_qwen_capability", lambda _root: gpu_ready):
                response = editor._start_readaloud_epub(
                    _CaptureTasks(), ReadaloudEpubRequest(timing_mode="chunk"))
            self.assertEqual("started", response["status"])
            self.assertEqual("chunk", response["timing_mode"])
            self.assertTrue(core._task_claims["readaloud_epub"]["cpu_only"])
        finally:
            core._task_claims.pop("audio", None)
            editor.process_state["audio"]["running"] = False

    def test_chunk_claim_stays_cpu_only_and_unblocked(self):
        import core
        editor, _manager = self._fixture(self._mkdtmp())
        self.assertEqual(set(), core.gpu_lock_conflicts("readaloud_epub", cpu_only=True))
        core._task_claims["readaloud_epub"] = {"id": "c" * 32, "phase": "started",
                                              "cpu_only": True}
        try:
            for name in ("audio", "script", "lora_training"):
                self.assertNotIn("readaloud_epub", core.gpu_lock_conflicts(name))
        finally:
            core._task_claims.pop("readaloud_epub", None)

    def test_promotion_into_gpu_tasks(self):
        import core
        self.assertNotIn("readaloud_epub", core.NON_GPU_TASKS)
        self.assertIn("readaloud_epub", core.GPU_TASKS)


class QwenReceiptTests(unittest.TestCase):
    @staticmethod
    def _legacy():
        return {key: "a" * 64 for key in (
            "book_token", "rows_digest", "source_sha256", "settings_digest",
            "retained_epub_sha256", "audio_sha256", "epub_sha256")}

    def test_v2_and_legacy_shapes(self):
        from readalong_export import get_validated_epub_receipt, get_epub_receipt_timing_mode
        legacy = self._legacy()
        get_validated_epub_receipt(json.dumps(legacy).encode("utf-8"))
        self.assertEqual("chunk", get_epub_receipt_timing_mode(legacy))
        v2 = dict(legacy, timing_mode="qwen", timing_device="cpu", timing_dtype="float32",
                  timing_language="English", timing_provider="b" * 64)
        get_validated_epub_receipt(json.dumps(v2).encode("utf-8"))
        self.assertEqual("qwen", get_epub_receipt_timing_mode(v2))

    def test_bad_shapes_rejected(self):
        from readalong_export import get_validated_epub_receipt
        legacy = self._legacy()
        for wrong in (
                {"timing_mode": "qwen"},                       # missing others
                dict(legacy, extra="x"),
                dict(legacy, timing_mode="sideways", timing_device="cpu", timing_dtype="float32",
                     timing_language="English", timing_provider="b" * 64),
                dict(legacy, timing_mode="qwen", timing_device="cpu", timing_dtype="float32",
                     timing_language="English", timing_provider="short")):
            with self.assertRaises(ValueError):
                get_validated_epub_receipt(json.dumps(wrong).encode("utf-8"))


class QwenBuildTests(_FixtureMixin):
    def test_model_language_subset_refuses_before_assembly(self):
        tmp, upstream = self._mkdtmp(), self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        capability = self._capability(tmp, upstream)
        capability['languages'] = ['French']
        timing = dict(timing_mode='qwen', device='cpu', language='English',
            provider_digest=capability['provider_digest'], capability=capability,
            qwen_asr_version='0.0.6', torch_version='2.10.0+cpu', dtype='float32')
        import readalong_export as rexp
        with patch.object(rexp, '_assemble_with_store', side_effect=AssertionError('must refuse before assembly')) as assembly:
            success, message = rexp.build_epub_export(manager, lambda: False, timing=timing)
        self.assertFalse(success)
        self.assertIn('selected Qwen model', message)
        assembly.assert_not_called()

    def test_builder_accepts_canonical_capability_runtime_identity(self):
        tmp = self._mkdtmp()
        upstream = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        capability = self._capability(tmp, upstream)
        capability['runtime_identity'] = capability.pop('runtime')
        timing = dict(timing_mode='qwen', device='cpu', language=None,
                      provider_digest=capability['provider_digest'], capability=capability,
                      qwen_asr_version='0.0.6', torch_version='2.10.0+cpu', dtype='float32')
        import qwen_runtime
        from readalong_export import build_epub_export
        fake = Path(__file__).resolve().parent / 'fixtures' / 'qwen_fake_worker.py'
        with patch.object(qwen_runtime, 'get_qwen_worker_command', lambda _py: [sys.executable, str(fake)]):
            success, message = build_epub_export(manager, lambda: False, timing=timing)
        self.assertTrue(success, message)
        receipt = json.loads((Path(tmp) / 'readaloud.receipt.json').read_text(encoding='utf-8'))
        self.assertEqual('qwen', receipt['timing_mode'])
        self.assertEqual(capability['provider_digest'], receipt['timing_provider'])

    def _capability(self, tmp, upstream_tmp):
        model = Path(upstream_tmp) / "model"
        model.mkdir(exist_ok=True)
        (model / "config.json").write_text('{"synthetic": true}', encoding="utf-8")
        (model / "tokenizer_config.json").write_text('{}', encoding="utf-8")
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

    def test_qwen_build_sentence_pars_and_receipt(self):
        tmp = self._mkdtmp()
        upstream = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        capability = self._capability(tmp, upstream)
        timing = {"timing_mode": "qwen", "device": "cpu", "language": None,
                  "provider_digest": capability["provider_digest"],
                  "capability": capability, "reason": "Qwen ready; using cpu.", "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu", "dtype": "float32", "runtime": capability["runtime"]}
        import qwen_runtime
        fake_worker = Path(__file__).resolve().parent / "fixtures" / "qwen_fake_worker.py"
        from readalong_export import build_epub_export, get_epub_receipt_timing_mode
        with patch.object(qwen_runtime, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(fake_worker)]):
            ok, message = build_epub_export(manager, lambda: False, timing=timing)
        self.assertTrue(ok, message)
        receipt_path = Path(tmp) / "readaloud.receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("qwen", get_epub_receipt_timing_mode(receipt))
        self.assertEqual("cpu", receipt["timing_device"])
        self.assertEqual("English", receipt["timing_language"])
        self.assertEqual(capability["provider_digest"], receipt["timing_provider"])
        from epub_readaloud import validate_epub_readaloud
        epub_bytes = (Path(tmp) / "readaloud.epub").read_bytes()
        validate_epub_readaloud(epub_bytes)
        smil_ns = "{http://www.w3.org/ns/SMIL}"
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as archive:
            smil = __import__("xml.etree.ElementTree", fromlist=["x"]).fromstring(
                archive.read("OEBPS/ch1.smil"))
        self.assertGreaterEqual(len(smil.findall(".//" + smil_ns + "par")), 3)

    def test_qwen_provider_replacement_refuses_publish(self):
        tmp = self._mkdtmp()
        upstream = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        capability = self._capability(tmp, upstream)
        timing = {"timing_mode": "qwen", "device": "cpu", "language": None,
                  "provider_digest": capability["provider_digest"],
                  "capability": capability, "reason": "test", "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu", "dtype": "float32", "runtime": capability["runtime"]}
        import qwen_runtime
        fake_worker = Path(__file__).resolve().parent / "fixtures" / "qwen_fake_worker.py"
        from readalong_export import build_epub_export
        with patch.object(qwen_runtime, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(fake_worker)]):
            ok, message = build_epub_export(manager, lambda: False, timing=timing)
        self.assertTrue(ok, message)
        # Replace the model after a successful build: next build must fail.
        model = Path(capability["model_dir"]) / "config.json"
        model.write_text('{"synthetic": "replaced"}', encoding="utf-8")
        timing2 = dict(timing)
        timing2["provider_digest"] = capability["provider_digest"]
        with patch.object(qwen_runtime, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(fake_worker)]):
            ok2, message2 = build_epub_export(manager, lambda: False, timing=timing2)
        self.assertFalse(ok2)
        self.assertIn("provider changed", message2.lower())

    def test_chunk_build_unchanged(self):
        tmp = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        from readalong_export import build_epub_export, get_epub_receipt_timing_mode
        ok, message = build_epub_export(manager, lambda: False)
        self.assertTrue(ok, message)
        receipt = json.loads((Path(tmp) / "readaloud.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual("chunk", get_epub_receipt_timing_mode(receipt))
        self.assertNotIn("timing_provider", receipt)


class QwenRouteTests(_FixtureMixin):
    def test_route_start_pins_policy(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        import qwen_runtime
        ready = self._ready_capability(tmp)
        with patch.object(qwen_runtime, "get_qwen_capability", lambda _root: ready):
            response = editor._start_readaloud_epub(
                None, None) if False else _start_with(editor, tmp)
        self.assertEqual("qwen", response["timing_mode"])
        self.assertEqual("cpu", response["timing_device"])

    def test_route_explicit_unavailable_refuses(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        import qwen_runtime
        from fastapi import HTTPException
        from routers.editor import ReadaloudEpubRequest
        not_ready = {"available": False, "devices": [], "cpu_fallback": False,
                     "reason": "missing module: qwen_asr"}
        with patch.object(qwen_runtime, "get_qwen_capability", lambda _root: not_ready):
            with self.assertRaises(HTTPException):
                editor._start_readaloud_epub(
                    _CaptureTasks(),
                    ReadaloudEpubRequest(timing_mode="qwen"))

    def test_capability_endpoint(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        import qwen_runtime
        ready = self._ready_capability(tmp)
        with patch.object(qwen_runtime, "get_qwen_capability", lambda _root, **kw: ready):
            payload = asyncio.run(editor.get_readaloud_epub_capability())
        self.assertTrue(payload["capability"]["available"])
        self.assertIn("timing_mode", payload["policy"])
        self.assertNotIn("python", payload["capability"])
        self.assertNotIn("model_dir", payload["capability"])
        self.assertNotIn("provider_digest", payload["capability"])

    def _ready_capability(self, tmp):
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


class _CaptureTasks:
    def __init__(self):
        self.registered = []

    def add_task(self, func, *args, **kwargs):
        self.registered.append((func, args, kwargs))


def _start_with(editor, tmp):
    # Direct production route call; integrity/data overridden by fixture.
    return editor._start_readaloud_epub(_CaptureTasks(), None)


if __name__ == "__main__":
    unittest.main()
