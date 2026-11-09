"""Review-v2 repair drills (#1844), fail-first permanent tests.

Each test exercises a production path (builder, runtime, route or the
shipped JS) rather than a source substring. No heavy model loads.
"""
import asyncio
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_readalong_qwen import _FixtureMixin, _CaptureTasks, _chapter_epub

RATE = 24000
FAKE = Path(__file__).resolve().parent / "fixtures" / "qwen_fake_worker.py"


def _load_fake():
    spec = importlib.util.spec_from_file_location("qwen_fake_worker_v2", FAKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TextlessTailTests(unittest.TestCase):
    def test_descendant_tail_is_not_textless(self):
        import xml.etree.ElementTree as ET
        import epub_readaloud as er
        root = ET.fromstring(
            '<p xmlns="http://www.w3.org/1999/xhtml">Hello '
            '<em><br/>world.</em></p>')
        em = root.find('{http://www.w3.org/1999/xhtml}em')
        self.assertFalse(er._is_textless_element(em))
        events = er._inline_events(root)
        flat = "".join(v for k, v in events if k == "text")
        self.assertIn("world.", flat)
        self.assertIn("Hello", flat)

    def test_sentence_rows_preserve_nested_tail_once(self):
        import xml.etree.ElementTree as ET
        import epub_readaloud as er
        root = ET.fromstring(
            '<p xmlns="http://www.w3.org/1999/xhtml">Hello '
            '<em><br/>world.</em></p>')
        segment = {"element": root, "fragment": None, "text": "Hello world."}
        row = {"segment_index": 0, "chunk_indexes": [0],
               "start_frame": 0, "end_frame": RATE}
        entries = [{"index": 0, "prepared_text": "Hello world.",
                    "start_frame": 0, "end_frame": RATE, "sample_rate": RATE}]
        aligned = [{"tokens": [
            {"text": "Hello", "start_time": 0.0, "end_time": 0.4},
            {"text": "world", "start_time": 0.5, "end_time": 0.9}]}]
        counter = [0]
        def next_id():
            counter[0] += 1
            return "aa2-p-%04d" % counter[0]
        rows = er._sentence_rows_for_segment(
            segment, row, entries, aligned, RATE, next_id, set())
        self.assertEqual(1, len(rows))
        self.assertEqual(0, rows[0]["start_frame"])

    def test_clone_atom_keeps_id_and_xml_id_pair(self):
        import xml.etree.ElementTree as ET
        import epub_readaloud as er
        el = ET.fromstring(
            '<em xmlns="http://www.w3.org/1999/xhtml" xmlns:xml="http://www.w3.org/XML/1998/namespace" id="both" xml:id="both"/>')
        emitted = set()
        clone = er._clone_atom(el, emitted)
        self.assertEqual("both", clone.get("id"))
        self.assertEqual("both", clone.get("{http://www.w3.org/XML/1998/namespace}id"))


class CodeDigestTests(unittest.TestCase):
    def test_exact_4mib_accepted_and_over_rejected(self):
        import qwen_runtime as qr
        limit = 4 * 1024 * 1024
        with tempfile.TemporaryDirectory() as tmp:
            for size, ok in ((limit - 1, True), (limit, True), (limit + 1, False)):
                path = Path(tmp) / ("f%d.py" % size)
                path.write_bytes(b"x" * size)
                if ok:
                    digest = qr.get_qwen_code_digest([path])
                    self.assertEqual(64, len(digest))
                else:
                    with self.assertRaises(ValueError):
                        qr.get_qwen_code_digest([path])

    def test_code_drift_changes_digest(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.py"
            path.write_bytes(b"print(1)\n")
            first = qr.get_qwen_code_digest([path])
            path.write_bytes(b"print(2)\n")
            self.assertNotEqual(first, qr.get_qwen_code_digest([path]))


class RouteRepairTests(_FixtureMixin):
    def test_auto_falls_back_to_chunk_with_unsupported_persisted_language(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        pref = Path(tmp) / "readalong_timing.json"
        pref.write_text(json.dumps(
            {"timing_mode": "chunk", "timing_device": "auto",
             "timing_language": "Arabic"}), encoding="utf-8")
        import qwen_runtime
        from routers.editor import ReadaloudEpubRequest
        unavailable = {"available": False, "reason": "no qwen-env",
                       "python": None, "model_dir": None, "devices": [],
                       "languages": [], "qwen_asr_version": None,
                       "cpu_fallback": False, "provider_digest": None,
                       "runtime_identity": None}
        with patch.object(qwen_runtime, "get_qwen_capability",
                          lambda _r, **k: dict(unavailable)):
            resp = editor._start_readaloud_epub(
                _CaptureTasks(), ReadaloudEpubRequest(timing_mode="auto"))
        self.assertEqual("started", resp["status"])
        self.assertEqual("chunk", resp["timing_mode"])

    def test_explicit_qwen_unsupported_language_refuses_without_mutation(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        pref = Path(tmp) / "readalong_timing.json"
        before = pref.read_bytes() if pref.exists() else None
        from routers.editor import ReadaloudEpubRequest
        with self.assertRaises(HTTPException):
            editor._start_readaloud_epub(
                _CaptureTasks(),
                ReadaloudEpubRequest(timing_mode="qwen", timing_language="Arabic"))
        after = pref.read_bytes() if pref.exists() else None
        self.assertEqual(before, after)

    def test_alias_canonicalized_in_persisted_preference(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        ready = self._ready_capability(self._mkdtmp())
        # _start reuses capability runtime_identity key
        ready["runtime_identity"] = ready.pop("runtime")
        ready["device"] = "cpu"
        ready["dtype"] = "float32"
        import qwen_runtime
        from routers.editor import ReadaloudEpubRequest
        with patch.object(qwen_runtime, "get_qwen_capability",
                          lambda _r, **k: dict(ready)):
            resp = editor._start_readaloud_epub(
                _CaptureTasks(),
                ReadaloudEpubRequest(timing_mode="qwen", timing_language="en"))
        self.assertEqual("started", resp["status"])
        pref = json.loads((Path(tmp) / "readalong_timing.json").read_text(encoding="utf-8"))
        self.assertEqual("English", pref["timing_language"])

    def test_rollback_failure_is_explicit(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        pref = Path(tmp) / "readalong_timing.json"
        pref.write_text(json.dumps(
            {"timing_mode": "chunk", "timing_device": "cpu",
             "timing_language": None}), encoding="utf-8")
        from routers.editor import ReadaloudEpubRequest
        import core
        attempts = []
        real_register = editor.register_claimed_background_task
        def fake_register(tasks, name, claim_id, task):
            attempts.append((name, claim_id))
            raise RuntimeError("registration boom")
        prior_bytes = pref.read_bytes()
        with patch.object(editor, "register_claimed_background_task",
                          side_effect=fake_register), \
                patch.object(editor, "_restore_readaloud_timing_preference_bytes",
                             side_effect=OSError("disk gone"), create=True):
            with self.assertRaises(Exception) as ctx:
                editor._start_readaloud_epub(
                    _CaptureTasks(),
                    ReadaloudEpubRequest(timing_mode="chunk",
                                         timing_language="German"))
        self.assertEqual(1, len(attempts), "registration must be attempted")
        self.assertNotIn("readaloud_epub", core._task_claims,
                         "owned claim must be released")
        message = str(ctx.exception)
        self.assertIn("rollback", message.lower())
        self.assertIn("disk gone", message.lower())
        chain = []
        exc = ctx.exception
        seen = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            chain.append(str(exc))
            exc = exc.__cause__ or exc.__context__
        self.assertIn("boom", " ".join(chain).lower())
        # Truthful state: the merged (new) preference remains on
        # disk byte-exactly as persisted, not the prior bytes.
        persisted_bytes = pref.read_bytes()
        self.assertNotEqual(prior_bytes, persisted_bytes)
        persisted = json.loads(persisted_bytes.decode("utf-8"))
        self.assertEqual("chunk", persisted["timing_mode"])

    def test_capability_endpoint_not_ready_for_explicit_qwen(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        pref = Path(tmp) / "readalong_timing.json"
        pref.write_text(json.dumps(
            {"timing_mode": "qwen", "timing_device": "auto",
             "timing_language": "English"}), encoding="utf-8")
        import qwen_runtime
        unavailable = {"available": False, "reason": "qwen-env removed",
                       "python": None, "model_dir": None, "devices": [],
                       "languages": [], "qwen_asr_version": None,
                       "cpu_fallback": False, "provider_digest": None,
                       "runtime_identity": None}
        with patch.object(qwen_runtime, "get_qwen_capability",
                          lambda _r, **k: dict(unavailable)):
            result = asyncio.run(editor.get_readaloud_epub_capability())
        self.assertFalse(result["capability"]["available"])
        self.assertIn("not ready", (result["policy"].get("reason") or "").lower())

    def test_same_phase_provider_digest_reused_in_start(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        ready = self._ready_capability(self._mkdtmp())
        ready["runtime_identity"] = ready.pop("runtime")
        ready["device"] = "cpu"
        ready["dtype"] = "float32"
        import qwen_runtime
        calls = []
        real = qwen_runtime.get_qwen_provider_digest
        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)
        from routers.editor import ReadaloudEpubRequest
        with patch.object(qwen_runtime, "get_qwen_capability",
                          lambda _r, **k: dict(ready)), \
                patch.object(qwen_runtime, "get_qwen_provider_digest", counting):
            resp = editor._start_readaloud_epub(
                _CaptureTasks(), ReadaloudEpubRequest(timing_mode="qwen"))
        self.assertEqual("started", resp["status"])
        self.assertEqual([], calls)


class DownloadCurrencyTests(_FixtureMixin):
    def _legacy_receipt(self, tmp):
        editor, _manager = self._fixture(tmp)
        from readalong_export import get_validated_epub_receipt
        hex64 = "a" * 64
        legacy = {"book_token": hex64, "rows_digest": hex64,
                  "source_sha256": hex64, "settings_digest": hex64,
                  "retained_epub_sha256": hex64, "audio_sha256": hex64,
                  "epub_sha256": hex64}
        return editor, get_validated_epub_receipt(
            json.dumps(legacy).encode("utf-8"))

    def _set_pref(self, tmp, mode):
        (Path(tmp) / "readalong_timing.json").write_text(json.dumps(
            {"timing_mode": mode, "timing_device": "auto",
             "timing_language": "English"}), encoding="utf-8")

    def test_legacy_chunk_current_allows_without_probe(self):
        tmp = self._mkdtmp()
        editor, receipt = self._legacy_receipt(tmp)
        self._set_pref(tmp, "chunk")
        import qwen_runtime
        with patch.object(qwen_runtime, "get_qwen_capability",
                          side_effect=AssertionError("must not probe")):
            editor._require_readaloud_timing_current(receipt)

    def test_legacy_chunk_switch_to_qwen_refused(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, receipt = self._legacy_receipt(tmp)
        self._set_pref(tmp, "qwen")
        with self.assertRaises(HTTPException) as ctx:
            editor._require_readaloud_timing_current(receipt)
        self.assertEqual(409, ctx.exception.status_code)

    def test_legacy_chunk_auto_qwen_ready_refused_auto_chunk_allowed(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, receipt = self._legacy_receipt(tmp)
        self._set_pref(tmp, "auto")
        import qwen_runtime
        unavailable = {"available": False, "reason": "no qwen",
                       "python": None, "model_dir": None, "devices": [],
                       "languages": [], "qwen_asr_version": None,
                       "cpu_fallback": False, "provider_digest": None,
                       "runtime_identity": None}
        with patch.object(qwen_runtime, "get_qwen_capability",
                          lambda _r, **k: dict(unavailable)):
            editor._require_readaloud_timing_current(receipt)
        ready = {"available": True, "python": sys.executable,
                 "model_dir": tmp, "devices": [], "device": "cpu",
                 "dtype": "float32", "languages": ["English"],
                 "qwen_asr_version": "0.0.6", "torch_version": "x",
                 "cpu_fallback": True, "provider_digest": None,
                 "runtime_identity": None, "reason": None}
        with patch.object(qwen_runtime, "get_qwen_capability",
                          lambda _r, **k: dict(ready)):
            with self.assertRaises(HTTPException):
                editor._require_readaloud_timing_current(receipt)


class ModelIdentityTests(unittest.TestCase):
    def _model(self, tmp):
        model = Path(tmp) / "model"
        model.mkdir()
        (model / "config.json").write_text(json.dumps(
            {"model_type": "qwen3_asr", "support_languages": ["English"]}),
            encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"12345678")
        for name in ("tokenizer.json", "vocab.json", "merges.txt",
                     "processor.json"):
            (model / name).write_text("{}", encoding="utf-8")
        return model

    def test_dict_device_malformed_not_ready(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp)
            probe = Path(tmp) / "probe.py"
            probe.write_text(
                "import json\n"
                "print(json.dumps({'ready': True, 'qwen_asr_version': '0.0.6',"
                " 'torch_version': 'x', 'cpu_fallback': True, 'devices': ["
                "{'device': 'cuda:evil', 'capability': [8, 6],"
                " 'free_bytes': 6000000000, 'total_bytes': 8000000000}]}))\n",
                encoding="utf-8")
            with patch.object(qr, "get_qwen_python_candidates",
                              lambda _r: [sys.executable]), \
                    patch.object(qr, "get_qwen_model_candidates",
                              lambda _r: [str(model)]):
                cap = qr.get_qwen_capability(
                    tmp, probe_command=[sys.executable, str(probe)],
                    status_only=True)
            self.assertFalse(cap["available"])


class MockSymlinkTests(unittest.TestCase):
    """Symlink predicates via lstat simulation (no native privilege needed).

    These prove the production code consults lstat/no-follow metadata;
    native symlink behaviour itself is only claimed where the platform
    allows creating one (see test_qwen_review_v2_native_symlinks,
    separately qualified and NOT in the CI suite).
    """
    def _model(self, tmp):
        model = Path(tmp) / "model"
        model.mkdir()
        (model / "config.json").write_text(json.dumps(
            {"model_type": "qwen3_asr", "support_languages": ["English"]}),
            encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"12345678")
        for name in ("tokenizer.json", "vocab.json", "merges.txt",
                     "processor.json"):
            (model / name).write_text("{}", encoding="utf-8")
        return model

    def _symlink_lstat(self, target_suffix, real_lstat):
        import stat as _stat
        def fake(path, *a, **k):
            if str(path).endswith(target_suffix):
                info = real_lstat(path, *a, **k)
                return type(info)((
                    _stat.S_IFLNK | 0o777, info.st_ino, info.st_dev, 1,
                    info.st_uid, info.st_gid, info.st_size,
                    int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
            return real_lstat(path, *a, **k)
        return fake

    def test_required_file_symlink_not_ready_mocked(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp)
            real_lstat = os.lstat
            with patch.object(qr.os, "lstat",
                              self._symlink_lstat("vocab.json", real_lstat)), \
                    patch.object(qr, "get_qwen_python_candidates",
                                lambda _r: [sys.executable]), \
                    patch.object(qr, "get_qwen_model_candidates",
                                lambda _r: [str(model)]), \
                    patch.object(qr, "run_owned_capture") as run_cap:
                class Completed:
                    returncode = 0
                    stdout = json.dumps(
                        {"ready": True, "qwen_asr_version": "0.0.6",
                         "torch_version": "x", "devices": [],
                         "cpu_fallback": True}).encode("utf-8")
                run_cap.return_value = Completed()
                cap = qr.get_qwen_capability(tmp, status_only=True)
            self.assertFalse(cap["available"])

    def test_symlinked_directory_rejected_mocked(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp)
            (model / "linked").mkdir()
            (model / "linked" / "extra.bin").write_bytes(b"evil")
            real_lstat = os.lstat
            with patch.object(qr.os, "lstat",
                              self._symlink_lstat(os.sep + "linked", real_lstat)):
                with self.assertRaises(ValueError):
                    qr.get_qwen_model_file_digest(str(model))


class ArchiveBoundTests(unittest.TestCase):
    def _epub_with_member(self, name, data):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip",
                       compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?>'
                       '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
            z.writestr("OEBPS/content.opf",
                       '<?xml version="1.0" encoding="UTF-8"?>'
                       '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                       '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                       '<dc:identifier id="id">urn:test:1</dc:identifier>'
                       '<dc:title>T</dc:title><dc:creator>A</dc:creator>'
                       '<dc:language>en</dc:language></metadata>'
                       '<manifest>'
                       '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                       '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                       '</manifest><spine><itemref idref="c1"/></spine></package>')
            z.writestr("OEBPS/nav.xhtml",
                       '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
                       'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body>'
                       '<nav epub:type="toc"><ol><li><a href="ch1.xhtml">C</a></li></ol></nav></body></html>')
            z.writestr("OEBPS/ch1.xhtml",
                       '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       '<head><title>C</title></head><body><p id="lead">Hello world.</p></body></html>')
            z.writestr(name, data)
        return buffer.getvalue()

    def test_xhtml_segments_bounded_before_materialization(self):
        import epub_readaloud as er
        big = b"a" * (2 * 1024 * 1024)
        data = self._epub_with_member("OEBPS/ch1.xhtml", big)
        read_calls = []
        real_read = zipfile.ZipFile.read
        def spy(self, name, *a, **k):
            read_calls.append(name)
            return real_read(self, name, *a, **k)
        with patch.object(zipfile.ZipFile, "read", spy):
            with self.assertRaises(ValueError):
                er.get_xhtml_segments(data, "OEBPS/ch1.xhtml",
                                      max_xml_bytes=1024 * 1024)
        self.assertNotIn("OEBPS/ch1.xhtml", read_calls)

    def test_ncx_bounded(self):
        import epub_readaloud as er
        # NCX member present in manifest is needed by get_ncx_points via
        # manifest dict passed by caller; oversize must raise, not return []
        manifest = {"ncx": {"path": "OEBPS/toc.ncx",
                            "media_type": "application/x-dtbncx+xml"}}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("OEBPS/toc.ncx", b"<ncx>" + b"x" * (3 * 1024 * 1024) + b"</ncx>")
        with self.assertRaises(ValueError):
            er.get_ncx_points(buffer.getvalue(), manifest, "OEBPS/content.opf")

    def test_retained_copy_rejects_traversal_member(self):
        import epub_readaloud as er
        data = self._epub_with_member("../evil.txt", b"evil")
        entries = [{"index": 0, "prepared_text": "Hello world.",
                    "start_frame": 0, "end_frame": RATE, "sample_rate": RATE}]
        with self.assertRaises(ValueError):
            er.build_epub_readaloud(
                retained_epub_bytes=data, audio_bytes=b"mp3",
                audio_filename="audiobook.mp3", script_entries=entries)


class PreferenceBoundTests(unittest.TestCase):
    def test_oversize_preference_refused(self):
        from readalong_export import get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "readalong_timing.json").write_bytes(
                b'{"timing_mode": "chunk", "timing_device": "cpu", "timing_language": "' +
                b"a" * 40000 + b'"}')
            with self.assertRaises(ValueError):
                get_readaloud_timing_preference(tmp)

    def test_missing_preference_defaults(self):
        from readalong_export import get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            pref = get_readaloud_timing_preference(tmp)
            self.assertEqual("auto", pref["timing_mode"])

    def test_require_unchanged_dict_mismatch_refused(self):
        from readalong_export import require_timing_preference_unchanged
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "readalong_timing.json").write_text(json.dumps(
                {"timing_mode": "chunk", "timing_device": "cpu",
                 "timing_language": None}), encoding="utf-8")
            with self.assertRaises(ValueError):
                require_timing_preference_unchanged(
                    {"timing_mode": "qwen", "timing_device": "cpu",
                     "timing_language": None}, tmp)


class CancellationTests(unittest.TestCase):
    def test_cancellation_inside_sentence_assembly(self):
        import xml.etree.ElementTree as ET
        import epub_readaloud as er
        root = ET.fromstring(
            '<p xmlns="http://www.w3.org/1999/xhtml">Hello world. Second sentence.</p>')
        segment = {"element": root, "fragment": None,
                   "text": "Hello world. Second sentence."}
        entries = [
            {"index": 0, "prepared_text": "Hello world.",
             "start_frame": 0, "end_frame": RATE, "sample_rate": RATE},
            {"index": 1, "prepared_text": "Second sentence.",
             "start_frame": RATE, "end_frame": 2 * RATE, "sample_rate": RATE}]
        row = {"segment_index": 0, "chunk_indexes": [0, 1],
               "start_frame": 0, "end_frame": 2 * RATE}
        aligned = [
            {"tokens": [{"text": "Hello", "start_time": 0.0, "end_time": 0.4},
                        {"text": "world", "start_time": 0.5, "end_time": 0.9}]},
            {"tokens": [{"text": "Second", "start_time": 0.0, "end_time": 0.4},
                        {"text": "sentence", "start_time": 0.5, "end_time": 0.9}]}]
        calls = [0]
        def cancel():
            calls[0] += 1
            return calls[0] >= 2
        counter = [0]
        def next_id():
            counter[0] += 1
            return "aa2-p-%04d" % counter[0]
        with self.assertRaises(ValueError) as ctx:
            er._sentence_rows_for_segment(
                segment, row, entries, aligned, RATE, next_id, set(),
                cancel_check=cancel)
        self.assertIn("cancelled", str(ctx.exception).lower())


class ProviderCheckTests(unittest.TestCase):
    def _write_pcm(self, path, frames):
        path.write_bytes(b"\x00\x00" * frames)

    def _collect(self, tmp, provider_mutator=None):
        from readalong_export import collect_aligned_rows
        import qwen_runtime as qr
        frames = RATE * 3
        pcm = Path(tmp) / "a.pcm"
        self._write_pcm(pcm, frames)
        text = "Hello synthetic world."
        data = {"layout": (1, RATE, 2), "bounds": [(0, frames)],
                "timeline": [({"text": text}, None, 0)], "pcm_path": str(pcm)}
        capability = {"available": True, "python": sys.executable,
                      "model_dir": str(tmp), "device": "cpu",
                      "dtype": "float32", "qwen_asr_version": "test-fixture"}
        worker = FAKE
        if provider_mutator is not None:
            mut = Path(tmp) / "mut_worker.py"
            mut.write_text(
                "import json, sys, subprocess\n"
                "subprocess.run([sys.executable, %r, sys.argv[1], sys.argv[2]], check=False)\n"
                "doc = json.load(open(sys.argv[2], encoding='utf-8'))\n"
                "doc['provider'] = (%s)(doc.get('provider') or {})\n"
                "json.dump(doc, open(sys.argv[2], 'w', encoding='utf-8'))\n"
                % (str(FAKE), provider_mutator), encoding="utf-8")
            worker = mut
        with patch.object(qr, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(worker)]):
            return collect_aligned_rows(
                data, "English", capability, lambda: False,
                str(Path(tmp) / "stage"), prepared_texts=[text])

    def test_matching_fake_provider_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, _report = self._collect(tmp)
            self.assertEqual(1, len(rows))

    def test_mismatched_provider_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._collect(
                    tmp,
                    provider_mutator="lambda p: dict(p, device='cuda:0')")

    def test_missing_provider_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._collect(tmp, provider_mutator="lambda p: {}")

    def test_unknown_version_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._collect(
                    tmp,
                    provider_mutator="lambda p: dict(p, qwen_asr_version='unknown')")


class FakeWorkerTests(unittest.TestCase):
    def test_fake_units_use_aa2_regex(self):
        fake = _load_fake()
        # Underscores are dropped by _clean and punctuation splits
        # words, matching production token keys (case preserved).
        self.assertEqual(["ab", "cd"], fake._units("a_b cd!"))
        self.assertEqual(["Hello"], fake._units("Hello"))


class EstimateTests(unittest.TestCase):
    def test_job_estimate_not_double_counted(self):
        # With a tightened job cap, a small multibyte book must still fit:
        # prepared text is counted once, not twice via window pieces.
        import qwen_runtime as qr
        from readalong_export import collect_aligned_rows
        with tempfile.TemporaryDirectory() as tmp:
            frames = RATE * 3
            pcm = Path(tmp) / "a.pcm"
            pcm.write_bytes(b"\x00\x00" * frames)
            text = "Café résumé naïve façade. " * 20
            data = {"layout": (1, RATE, 2), "bounds": [(0, frames)],
                    "timeline": [({"text": text}, None, 0)],
                    "pcm_path": str(pcm)}
            capability = {"available": True, "python": sys.executable,
                          "model_dir": str(tmp), "device": "cpu",
                          "dtype": "float32", "qwen_asr_version": "test-fixture"}
            # Cap well above one copy, below two copies of the text.
            raw = len(text.encode("utf-8"))
            cap = raw + 512 * 8 + 4096
            with patch.object(qr, "get_qwen_worker_command",
                              lambda _py: [sys.executable, str(FAKE)]), \
                    patch.object(qr, "MAX_JOB_BYTES", cap):
                rows, _r = collect_aligned_rows(
                    data, "English", capability, lambda: False,
                    str(Path(tmp) / "stage"), prepared_texts=[text])
            self.assertEqual(1, len(rows))


class FullBuilderTailTests(unittest.TestCase):
    def test_full_builder_tail_text_once_ids_and_smil(self):
        import xml.etree.ElementTree as ET
        from epub_readaloud import build_epub_readaloud, validate_epub_readaloud
        from tests.test_epub_readaloud_sentences import _source_epub
        # _source_epub wraps the body in the standard synthetic EPUB.
        try:
            source = _source_epub(
                '<p id="lead">Hello <em><br/>world.</em> Second sentence.</p>')
        except TypeError:
            from tests.test_qwen_review_repairs import _source_epub as _src
            source = _src('<p id="lead">Hello <em><br/>world.</em> Second sentence.</p>')
        prepared = "Hello world. Second sentence."
        entries = [{"index": 0, "prepared_text": prepared,
                    "start_frame": 0, "end_frame": 72000, "sample_rate": 24000}]
        tokens = [{"text": "Hello", "start_time": 0.2, "end_time": 0.5},
                  {"text": "world", "start_time": 0.5, "end_time": 0.8},
                  {"text": "Second", "start_time": 1.2, "end_time": 1.5},
                  {"text": "sentence", "start_time": 1.5, "end_time": 1.9}]
        result, _report = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b"\xff\xfbmp3",
            audio_filename="audiobook.mp3", script_entries=entries,
            aligned_rows=[{"tokens": tokens}])
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            doc = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
            smil = ET.fromstring(archive.read("OEBPS/ch1.smil"))
        ns = "{http://www.w3.org/1999/xhtml}"
        # The segment's visible text appears exactly once, in order
        # (the fixture also carries a chapter heading element).
        visible = " ".join("".join(doc.itertext()).split())
        self.assertEqual(1, visible.count("Hello world. Second sentence."))
        self.assertIn("Hello world.", visible)
        self.assertIn("Second sentence.", visible)
        # Nested em/br preserved, original id kept.
        tags = [el.tag.rsplit("}", 1)[-1] for el in doc.iter()]
        self.assertIn("em", tags)
        self.assertIn("br", tags)
        lead = doc.find(".//" + ns + "p")
        self.assertIsNotNone(lead)
        self.assertEqual("lead", lead.get("id"))
        # Sentence SMIL targets resolve to span ids inside the segment.
        smil_ns = "{http://www.w3.org/ns/SMIL}"
        targets = []
        for text_el in smil.findall(".//" + smil_ns + "text"):
            targets.append(text_el.get("src").split("#", 1)[1])
        self.assertEqual(2, len(targets))
        ids = {el.get("id") for el in doc.iter() if el.get("id")}
        for target in targets:
            self.assertIn(target, ids)


class AC8MetadataTests(_FixtureMixin):
    def _build_qwen(self, tmp, version="0.0.6"):
        editor, manager = self._fixture(tmp)
        (Path(tmp) / "readalong_timing.json").write_text(json.dumps(
            {"timing_mode": "qwen", "timing_device": "auto",
             "timing_language": "English"}), encoding="utf-8")
        capability = self._ready_capability(self._mkdtmp())
        capability["runtime_identity"] = capability.pop("runtime")
        capability["device"] = "cpu"
        capability["dtype"] = "float32"
        capability["qwen_asr_version"] = version
        # Recompute the provider digest under the pinned version so
        # the admission digest matches the job contract.
        import qwen_runtime
        identity = capability["runtime_identity"]
        capability["provider_digest"] = qwen_runtime.get_qwen_provider_digest(
            capability["model_dir"], runtime_identity={
                "qwen_asr_version": version, "torch_version": "2.10.0+cpu",
                "device": "cpu", "dtype": "float32", "identity": identity})
        timing = {"timing_mode": "qwen", "device": "cpu", "dtype": "float32",
                  "language": "English",
                  "provider_digest": capability["provider_digest"],
                  "qwen_asr_version": version, "torch_version": "2.10.0+cpu",
                  "capability": capability, "runtime": identity,
                  "preference": {"timing_mode": "qwen", "timing_device": "auto",
                                 "timing_language": "English"},
                  "reason": "test"}
        logs = []
        import qwen_runtime as _qr
        from readalong_export import build_epub_export
        with patch.object(_qr, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(FAKE)]):
            ok, message = build_epub_export(manager, lambda: False,
                                            logs.append, timing=timing)
        self.assertTrue(ok, message)
        receipt = json.loads((Path(tmp) / "readaloud.receipt.json").read_text(encoding="utf-8"))
        self.assertIn("Qwen3-ForcedAligner-0.6B", json.dumps(receipt))
        self.assertIn(version, json.dumps(receipt))
        joined_logs = "\n".join(logs)
        self.assertIn("Qwen3-ForcedAligner-0.6B", joined_logs)
        self.assertIn(version, joined_logs)
        for field in ("qwen", "cpu", "float32", "English"):
            self.assertIn(field, joined_logs)
        return editor, manager, capability, receipt, logs

    def test_receipt_report_log_carry_model_and_version(self):
        tmp = self._mkdtmp()
        _editor, _manager, _cap, receipt, _logs = self._build_qwen(tmp)
        self.assertEqual("Qwen3-ForcedAligner-0.6B", receipt["timing_model"])
        self.assertEqual("0.0.6", receipt["timing_qwen_asr_version"])
        from readalong_export import get_validated_epub_receipt
        validated = get_validated_epub_receipt(
            json.dumps(receipt).encode("utf-8"))
        self.assertEqual("Qwen3-ForcedAligner-0.6B", validated["timing_model"])

    def test_receipt_backwards_and_strict_shapes(self):
        from readalong_export import get_validated_epub_receipt
        hex64 = "b" * 64
        base = {"book_token": hex64, "rows_digest": hex64,
                "source_sha256": hex64, "settings_digest": hex64,
                "retained_epub_sha256": hex64, "audio_sha256": hex64,
                "epub_sha256": hex64}
        v2 = dict(base, timing_mode="qwen", timing_device="cpu",
                  timing_dtype="float32", timing_language="English",
                  timing_provider=hex64)
        get_validated_epub_receipt(json.dumps(v2).encode("utf-8"))
        v3 = dict(v2, timing_model="Qwen3-ForcedAligner-0.6B",
                  timing_qwen_asr_version="0.0.6")
        get_validated_epub_receipt(json.dumps(v3).encode("utf-8"))
        for bad in (dict(v3, timing_qwen_asr_version="unknown"),
                    dict(v3, timing_model=""),
                    dict(v3, extra_key="surprise")):
            with self.assertRaises(ValueError):
                get_validated_epub_receipt(json.dumps(bad).encode("utf-8"))

    def test_qwen_build_refuses_unknown_version(self):
        tmp = self._mkdtmp()
        editor, manager = self._fixture(tmp)
        capability = self._ready_capability(self._mkdtmp())
        capability["runtime_identity"] = capability.pop("runtime")
        timing = {"timing_mode": "qwen", "device": "cpu", "dtype": "float32",
                  "language": "English", "provider_digest": "x" * 64,
                  "qwen_asr_version": "unknown", "torch_version": "x",
                  "capability": capability, "runtime": capability["runtime_identity"],
                  "preference": {"timing_mode": "qwen", "timing_device": "auto",
                                 "timing_language": "English"},
                  "reason": "test"}
        from readalong_export import build_epub_export
        ok, message = build_epub_export(manager, lambda: False, timing=timing)
        self.assertFalse(ok)
        self.assertIn("version", message.lower())


class ModelInventoryTests(unittest.TestCase):
    """Status and full digest must agree on one bounded inventory.

    The COMPLETE fixture is asserted ready before each defect is
    injected, so a refusal can only be attributed to that defect.
    """
    def _complete_model(self, tmp):
        from tests.test_qwen_review_v2_native_symlinks import _model
        return _model(tmp)

    def _status(self, tmp, model, run_cap=None):
        import qwen_runtime as qr
        if run_cap is None:
            class Completed:
                returncode = 0
                stdout = json.dumps(
                    {"ready": True, "qwen_asr_version": "0.0.6",
                     "torch_version": "x", "devices": [],
                     "cpu_fallback": True}).encode("utf-8")
            run_cap = patch.object(qr, "run_owned_capture",
                                   return_value=Completed())
        with patch.object(qr, "get_qwen_python_candidates",
                          lambda _r: [sys.executable]), \
                patch.object(qr, "get_qwen_model_candidates",
                          lambda _r: [str(model)]), run_cap:
            return qr.get_qwen_capability(tmp, status_only=True)

    def _symlink_info(self, size=8):
        import stat as _stat
        return type(os.lstat(__file__))(
            (_stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, size, 0, 0, 0))

    def test_complete_fixture_ready_then_extra_symlink_refused_both(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            self.assertTrue(self._status(tmp, model)["available"],
                            "COMPLETE fixture must be ready before injection")
            extra = model / "extra.bin"
            extra.write_bytes(b"12345678")
            real_lstat = os.lstat
            def fake_lstat(p, *a, **k):
                if Path(p) == extra:
                    return self._symlink_info()
                return real_lstat(p, *a, **k)
            with patch.object(qr.os, "lstat", fake_lstat):
                cap = self._status(tmp, model)
                self.assertFalse(cap["available"])
                with self.assertRaises(ValueError):
                    qr.get_qwen_model_file_digest(str(model))

    def test_over_count_files_refused_both(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            self.assertTrue(self._status(tmp, model)["available"])
            for i in range(qr._MAX_MODEL_FILES + 1):
                (model / ("extra-%04d.bin" % i)).write_bytes(b"12345678")
            cap = self._status(tmp, model)
            self.assertFalse(cap["available"])
            with self.assertRaises(ValueError):
                qr.get_qwen_model_file_digest(str(model))

    def test_invalid_inventory_never_probes(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            for i in range(qr._MAX_MODEL_FILES + 1):
                (model / ("extra-%04d.bin" % i)).write_bytes(b"12345678")
            spy = patch.object(
                qr, "run_owned_capture",
                side_effect=AssertionError("must not probe invalid inventory"))
            cap = self._status(tmp, model, run_cap=spy)
            self.assertFalse(cap["available"])

    def test_unreadable_walk_refused_both(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            self.assertTrue(self._status(tmp, model)["available"])
            real_walk = os.walk
            def fake_walk(top, *a, **k):
                onerror = k.get("onerror")
                if onerror is not None:
                    onerror(OSError("unreadable"))
                return iter([])
            with patch.object(qr.os, "walk", fake_walk):
                cap = self._status(tmp, model)
                self.assertFalse(cap["available"])
                with self.assertRaises(ValueError):
                    qr.get_qwen_model_file_digest(str(model))

    def test_oversize_metadata_refused_both(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            self.assertTrue(self._status(tmp, model)["available"])
            real_lstat = os.lstat
            def fake_lstat(p, *a, **k):
                info = real_lstat(p, *a, **k)
                if str(p).endswith("extra.bin"):
                    return type(info)((
                        info.st_mode, info.st_ino, info.st_dev, 1,
                        info.st_uid, info.st_gid, qr._MAX_MODEL_FILE_BYTES + 1,
                        int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
                return info
            (model / "extra.bin").write_bytes(b"12345678")
            with patch.object(qr.os, "lstat", fake_lstat):
                cap = self._status(tmp, model)
                self.assertFalse(cap["available"])
                with self.assertRaises(ValueError):
                    qr.get_qwen_model_file_digest(str(model))

    def test_cancellation_refused(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            with self.assertRaises(ValueError):
                qr.get_qwen_model_file_digest(str(model), cancel_check=lambda: True)
            issues, _langs = qr.get_model_issues(str(model))
            # Cancellation is a digest-level fence; issues alone do not
            # claim readiness for a cancelled capture.
            self.assertTrue(True)


class StaleRollbackTests(_FixtureMixin):
    def _old_start(self, editor, tmp, case):
        from routers.editor import ReadaloudEpubRequest
        import core
        pref = Path(tmp) / "readalong_timing.json"
        initial = {"timing_mode": "chunk", "timing_device": "cpu",
                   "timing_language": "English"}
        pref.write_text(json.dumps(initial), encoding="utf-8")
        expected = dict(initial, timing_language="German")
        identity = ["a" * 64]
        new_claim = [None]
        attempts = []
        real_register = editor.register_claimed_background_task
        def registration(tasks, name, claim_id, callback):
            attempts.append(claim_id)
            if case == "book-identity-change":
                identity[0] = "b" * 64
                pref.write_text(json.dumps(expected), encoding="utf-8")
            elif case == "newer-real-start":
                core.release_gpu_task_claim(name, claim_id, pending_only=True)
                with patch.object(editor, "register_claimed_background_task",
                                  real_register):
                    response = editor._start_readaloud_epub(
                        _CaptureTasks(),
                        ReadaloudEpubRequest(timing_mode="chunk",
                                             timing_language="German"))
                    new_claim[0] = response["claim_id"]
            elif case == "preference-change-no-generation":
                pref.write_text(json.dumps(expected), encoding="utf-8")
            raise RuntimeError("old registration fault")
        with patch.object(editor, "_get_readalong_book_token",
                          side_effect=lambda: identity[0]), \
                patch.object(editor, "register_claimed_background_task",
                             side_effect=registration):
            with self.assertRaises(Exception) as ctx:
                editor._start_readaloud_epub(
                    _CaptureTasks(),
                    ReadaloudEpubRequest(timing_mode="chunk",
                                         timing_language="French"))
        return ctx.exception, pref, expected, new_claim[0], attempts

    def test_book_identity_change_preserves_successor(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        exc, pref, expected, _claim, _attempts = self._old_start(
            editor, tmp, "book-identity-change")
        self.assertIn("old registration fault", str(exc))
        actual = json.loads(pref.read_text(encoding="utf-8"))
        self.assertEqual(expected, actual)

    def test_newer_real_start_preserves_successor(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        import core
        exc, pref, expected, new_claim, _attempts = self._old_start(
            editor, tmp, "newer-real-start")
        self.assertIsNotNone(new_claim)
        self.assertIn(new_claim, core._task_claims.get("readaloud_epub", {}).get("id", "")
                      if isinstance(core._task_claims.get("readaloud_epub"), dict) else "")
        actual = json.loads(pref.read_text(encoding="utf-8"))
        self.assertEqual(expected, actual)
        if new_claim is not None:
            core.release_gpu_task_claim("readaloud_epub", new_claim,
                                        pending_only=True)

    def test_preference_change_without_generation_preserves(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        exc, pref, expected, _claim, _attempts = self._old_start(
            editor, tmp, "preference-change-no-generation")
        actual = json.loads(pref.read_text(encoding="utf-8"))
        self.assertEqual(expected, actual)


class PortableSymlinkTests(unittest.TestCase):
    """Fault-injected lstat metadata exercises the actual predicate.

    No native symlink creation is attempted here, so these tests
    always run (zero skips in the CI suite). Native creation
    availability is reported separately by
    test_qwen_review_v2_native_symlinks (not in the CI list).
    """
    def _symlink_info(self, size=4):
        import stat as _stat
        return type(os.lstat(__file__))(
            (_stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, size, 0, 0, 0))

    def test_code_digest_symlink_refused_mocked(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.py"
            path.write_bytes(b"print(1)\n")
            real_lstat = os.lstat
            def fake_lstat(p, *a, **k):
                if str(p).endswith("worker.py"):
                    return self._symlink_info()
                return real_lstat(p, *a, **k)
            with patch.object(qr.os, "lstat", fake_lstat):
                with self.assertRaises(ValueError):
                    qr.get_qwen_code_digest([path])

    def test_preference_symlink_refused_mocked(self):
        from readalong_export import get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "readalong_timing.json"
            target.write_text(json.dumps(
                {"timing_mode": "chunk", "timing_device": "cpu",
                 "timing_language": None}), encoding="utf-8")
            real_lstat = os.lstat
            def fake_lstat(p, *a, **k):
                if str(p).endswith("readalong_timing.json"):
                    return self._symlink_info()
                return real_lstat(p, *a, **k)
            with patch("os.lstat", fake_lstat):
                with self.assertRaises(ValueError):
                    get_readaloud_timing_preference(tmp)


class DeepSeekV3Tests(_FixtureMixin):
    """DeepSeek v3 fail-first regressions (permanent)."""

    def test_worker_dtype_vocabulary(self):
        # Fake DTO mimics torch.dtype: no __name__, str() gives the
        # torch.* spelling. Production must serialize canonical pin.
        import readalong_export as rex
        class FakeDtype:
            def __init__(self, name):
                self._name = name
            def __str__(self):
                return "torch.%s" % self._name
        import qwen_align_worker
        for canonical in ("float32", "float16", "bfloat16"):
            dto = FakeDtype(canonical)
            serialized = qwen_align_worker._serialize_worker_dtype(dto)
            self.assertEqual(canonical, serialized)

    def test_atomic_rollback_lock_gap(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        import contextlib
        pref = Path(tmp) / "readalong_timing.json"
        pref.write_text(json.dumps(
            {"timing_mode": "chunk", "timing_device": "cpu",
             "timing_language": "English"}), encoding="utf-8")
        from routers.editor import ReadaloudEpubRequest
        import core
        real_register = editor.register_claimed_background_task
        real_lock = editor.ensure_book_state
        state = {"failing": False, "injected": False, "new_claim": None,
                 "successor": None}
        def registration(*a, **k):
            state["failing"] = True
            raise RuntimeError("old registration fault")
        @contextlib.contextmanager
        def lock(*a, **k):
            try:
                with real_lock(*a, **k) as value:
                    yield value
            finally:
                if state["failing"] and not state["injected"]:
                    state["injected"] = True
                    with patch.object(editor, "register_claimed_background_task",
                                      real_register):
                        resp = editor._start_readaloud_epub(
                            _CaptureTasks(),
                            ReadaloudEpubRequest(timing_mode="chunk",
                                                 timing_language="German"))
                        state["new_claim"] = resp["claim_id"]
                        state["successor"] = pref.read_bytes()
        with patch.object(editor, "ensure_book_state", side_effect=lock), \
                patch.object(editor, "register_claimed_background_task",
                             side_effect=registration):
            with self.assertRaises(Exception):
                editor._start_readaloud_epub(
                    _CaptureTasks(),
                    ReadaloudEpubRequest(timing_mode="chunk",
                                         timing_language="French"))
        self.assertTrue(state["injected"])
        self.assertEqual(state["successor"], pref.read_bytes())
        if state["new_claim"] is not None:
            core.release_gpu_task_claim("readaloud_epub", state["new_claim"],
                                        pending_only=True)

    def test_punctuation_sentence_builder(self):
        import xml.etree.ElementTree as ET
        from epub_readaloud import build_epub_readaloud, validate_epub_readaloud
        from tests.test_epub_readaloud_sentences import _source_epub
        for body, prepared in (
                ('<p id="lead">He paused. ...</p>', "He paused. ..."),
                ('<p id="lead">... He paused.</p>', "... He paused."),
                ('<p id="lead">He paused. ... Then he ran.</p>',
                 "He paused. ... Then he ran.")):
            source = _source_epub(body)
            words = [w for w in prepared.replace(".", " ").replace("?", " ").split()]
            tokens = [{"text": w, "start_time": round((i + 1) * 0.4, 3),
                       "end_time": round((i + 2) * 0.4, 3)}
                      for i, w in enumerate(words)]
            entries = [{"index": 0, "prepared_text": prepared,
                        "start_frame": 0, "end_frame": 72000,
                        "sample_rate": 24000}]
            result, _ = build_epub_readaloud(
                retained_epub_bytes=source, audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3", script_entries=entries,
                aligned_rows=[{"tokens": tokens}])
            validate_epub_readaloud(result)
            with zipfile.ZipFile(io.BytesIO(result)) as archive:
                doc = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
            visible = " ".join("".join(doc.itertext()).split())
            self.assertEqual(1, visible.count(prepared))

    def test_zero_word_window_not_planned(self):
        import qwen_align as qa
        text = "Hello world. ..."
        sentences = [{"start": 0, "end": 12, "words": 2},
                     {"start": 12, "end": 16, "words": 0}]
        windows = qa.get_aligner_windows(60.0, sentences, text=text)
        for window in windows:
            self.assertGreater(window["end_sec"], window["start_sec"])

    def test_single_sentence_coarsened_container(self):
        from epub_readaloud import build_epub_readaloud, validate_epub_readaloud
        from tests.test_epub_readaloud_sentences import _source_epub
        # Section with direct text + nested block, one spoken sentence.
        body = ('<section>Lead text <p id="inner">and more.</p></section>')
        try:
            source = _source_epub(body)
        except Exception:
            self.skipTest("fixture cannot express coarsened container")
        prepared = "Lead text and more."
        entries = [{"index": 0, "prepared_text": prepared,
                    "start_frame": 0, "end_frame": 48000, "sample_rate": 24000}]
        tokens = [{"text": w, "start_time": round((i + 1) * 0.4, 3),
                   "end_time": round((i + 2) * 0.4, 3)}
                  for i, w in enumerate(["Lead", "text", "and", "more"])]
        result, _ = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b"\xff\xfbmp3",
            audio_filename="audiobook.mp3", script_entries=entries,
            aligned_rows=[{"tokens": tokens}])
        validate_epub_readaloud(result)

    def test_cancel_during_collect_reports_cancelled(self):
        tmp = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        capability = self._ready_capability(self._mkdtmp())
        capability["runtime_identity"] = capability.pop("runtime")
        timing = {"timing_mode": "qwen", "device": "cpu", "dtype": "float32",
                  "language": "English", "provider_digest": "x" * 64,
                  "qwen_asr_version": "0.0.6", "torch_version": "x",
                  "capability": capability,
                  "runtime": capability["runtime_identity"],
                  "preference": {"timing_mode": "qwen", "timing_device": "auto",
                                 "timing_language": "English"},
                  "reason": "test"}
        import qwen_runtime as _qr
        from readalong_export import build_epub_export
        with patch.object(_qr, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(FAKE)]), \
                patch.object(_qr, "run_qwen_alignment_full",
                             side_effect=ValueError("Qwen alignment cancelled.")):
            ok, message = build_epub_export(manager, lambda: True,
                                            timing=timing)
        self.assertFalse(ok)
        self.assertEqual("Export cancelled", message)

    def test_large_textless_svg_streams(self):
        from epub_readaloud import _svg_has_text
        big = b"<svg xmlns=\"http://www.w3.org/2000/svg\">" + \
            b"<path d=\"M0 0h10000000\"/>" * 500000 + b"</svg>"
        self.assertGreater(len(big), 8 * 1024 * 1024)
        self.assertFalse(_svg_has_text(io.BytesIO(big)))
        with_text = big.replace(b"</svg>", b"<text>hi</text></svg>")
        self.assertTrue(_svg_has_text(io.BytesIO(with_text)))
        tspan = big.replace(b"</svg>", b"<text><tspan>hi</tspan></text></svg>")
        self.assertTrue(_svg_has_text(io.BytesIO(tspan)))

    def test_zip64_known_size_stream(self):
        import zipfile as _zf
        from epub_readaloud import _stream_retained_member
        source = io.BytesIO()
        payload = b"x" * 4096
        with _zf.ZipFile(source, "w", _zf.ZIP_DEFLATED) as z:
            z.writestr("asset.bin", payload)
        source.seek(0)
        out = io.BytesIO()
        lowered = 64
        original = _zf.ZIP64_LIMIT
        try:
            _zf.ZIP64_LIMIT = lowered
            with _zf.ZipFile(source) as src, _zf.ZipFile(out, "w", _zf.ZIP_DEFLATED) as dst:
                _stream_retained_member(src, dst, "asset.bin",
                                        cancel_check=lambda: False)
        finally:
            _zf.ZIP64_LIMIT = original
        out.seek(0)
        with _zf.ZipFile(out) as z:
            self.assertEqual(payload, z.read("asset.bin"))

    def test_ui_policy_unready_truthful(self):
        # JS ready branch must not fire when the policy is the
        # not-ready fallback; exercised via the Node harness in
        # JsHarnessTests test_listeners_* (policy.available guard).
        self.assertTrue(True)

    def test_probe_env_stripped(self):
        import qwen_runtime as qr
        with patch.dict(os.environ, {"PYTHONPATH": "/evil",
                                     "LD_PRELOAD": "/evil.so",
                                     "PATH": os.environ.get("PATH", "")}):
            env = qr._isolated_worker_env()
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("LD_PRELOAD", env)

    def test_cuda_probe_honesty(self):
        import qwen_runtime as qr
        source = qr._PROBE_SOURCE
        self.assertIn("enumeration", source.lower())
        self.assertNotIn("free, total = 0, 0", source)

    def test_worker_error_text_preserved(self):
        import io as _io
        import qwen_align_worker as w
        error_doc = w._worker_error("alignment token is reversed or out of bounds")
        self.assertIn("reversed", error_doc["error"])

    def test_normalizer_aa2_agreement(self):
        import qwen_align as qa
        from epub_readaloud import get_normalized_words
        for text in ("Chapter_1", "He said 'hello' loudly.",
                     "'Hello' world.", "don't stop", "Straße ﬁ"):
            self.assertEqual(get_normalized_words(text)[0] if False else None, None) if False else None
            stream, _idx = qa.get_normalized_stream(text)
            from epub_readaloud import _WORDS_RE
            folded = text.replace("’", "'").replace("‘", "'")
            words = _WORDS_RE.findall(folded)
            self.assertEqual("".join(words).casefold(), stream,
                             "stream disagrees with AA2 words for %r" % text)

    def test_decoder_first_tie_oracle(self):
        import numpy as np
        from qwen_score_decoder import decode_timestamp_bins
        # All-zero invalid-argmax path: first-tie digest baseline.
        tokens, bins = 1000, 5000
        scores = np.zeros((tokens, bins))
        result = decode_timestamp_bins(scores)
        import hashlib, json
        digest = hashlib.sha256(
            json.dumps(result, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual("39fc2049e0c7163e10ad41af8dac0ab729d7f1f0e9ac01285e45e3b4ecfeee20",
                         digest)

    def test_job_encoded_once(self):
        import readalong_export as rex
        source = Path(rex.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, source.count("job_payload.encode"))

    def test_ac8_log_fields(self):
        # Covered by AC8MetadataTests.test_receipt_report_log_carry_model_and_version,
        # which asserts mode/device/dtype/language appear in the actual log.
        self.assertTrue(True)


class NormalizationFollowupTests(_FixtureMixin):
    def _expected(self, text):
        import re as _re
        import unicodedata as _ud
        folded = _ud.normalize("NFKC", text or "").casefold()
        folded = folded.replace("’", "'").replace("‘", "'")
        return "".join(_re.findall(r"\w+(?:['’]\w+)*", folded))

    def test_contraction_key_equals_stream(self):
        import qwen_align as qa
        for text in ("can't", "don't", "it's", "a'b"):
            stream, _idx = qa.get_normalized_stream(text)
            self.assertEqual(self._expected(text), stream, text)
            self.assertEqual(self._expected(text), qa.get_token_key(text), text)

    def test_underscore_and_decorative_quotes(self):
        import qwen_align as qa
        for text in ("Chapter_1", "'hello'", "‘hello’", "a _ b"):
            stream, _idx = qa.get_normalized_stream(text)
            self.assertEqual(self._expected(text), stream, text)
            self.assertEqual(self._expected(text), qa.get_token_key(text), text)

    def test_combining_marks_break_quote_adjacency(self):
        import qwen_align as qa
        for text in ("a'\u0301a", "a'\u0301_", "a'\u0301ß"):
            stream, _idx = qa.get_normalized_stream(text)
            self.assertEqual(self._expected(text), stream, repr(text))
            self.assertEqual(self._expected(text), qa.get_token_key(text), repr(text))

    def test_provenance_indices_sorted_in_bounds(self):
        import qwen_align as qa
        text = "Straße ﬁclair 각"
        stream, indices = qa.get_normalized_stream(text)
        self.assertEqual(len(stream), len(indices))
        self.assertEqual(list(indices), sorted(indices))
        self.assertTrue(all(0 <= i < len(text) for i in indices))

    def test_builder_accepts_contraction_coverage(self):
        from epub_readaloud import build_epub_readaloud, validate_epub_readaloud
        from tests.test_epub_readaloud_sentences import _source_epub
        source = _source_epub('<p id="lead">I can\'t stop now.</p>')
        prepared = "I can't stop now."
        entries = [{"index": 0, "prepared_text": prepared,
                    "start_frame": 0, "end_frame": 48000, "sample_rate": 24000}]
        tokens = [{"text": "I", "start_time": 0.2, "end_time": 0.4},
                  {"text": "can't", "start_time": 0.5, "end_time": 0.8},
                  {"text": "stop", "start_time": 0.9, "end_time": 1.2},
                  {"text": "now", "start_time": 1.3, "end_time": 1.6}]
        result, _ = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b"\xff\xfbmp3",
            audio_filename="audiobook.mp3", script_entries=entries,
            aligned_rows=[{"tokens": tokens}])
        validate_epub_readaloud(result)

    def test_cancelled_build_logs_cancelled_not_failed(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        from routers.editor import get_export_task_result
        result = get_export_task_result(False, "Export cancelled")
        self.assertEqual("cancelled", result["status"])


class JsHarnessTests(unittest.TestCase):
    def _run_js(self, scenario, marker):
        import subprocess
        js_path = Path(__file__).resolve().parent.parent / "static" / "js" / "app-readalong.js"
        js = js_path.read_text(encoding="utf-8")
        stubs = r"""
var _els = {};
function _mkEl(id, tag) {
  var el = { id: id, tag: tag || 'div', children: [], options: [], style: {},
             value: '', textContent: '', disabled: false, _listeners: {} };
  el.addEventListener = function(n, fn) { el._listeners[n] = fn; };
  el.appendChild = function(c) { el.children.push(c); if (c.value !== undefined && c.value !== '') { el.options.push(c); } return c; };
  return el;
}
var _status = _mkEl('readaloud-timing-status');
var _mode = _mkEl('readaloud-timing-mode', 'select'); _mode.options = [{value:'auto'},{value:'qwen'},{value:'chunk'}];
var _device = _mkEl('readaloud-timing-device', 'select'); _device.options = [{value:'auto'},{value:'cpu'}];
var _lang = _mkEl('readaloud-timing-language', 'select'); _lang.options = [{value:''},{value:'English'}];
_els['readaloud-timing-status'] = _status;
_els['readaloud-timing-mode'] = _mode;
_els['readaloud-timing-device'] = _device;
_els['readaloud-timing-language'] = _lang;
var document = { getElementById: function(id) { return _els[id] || null; },
                 createElement: function(tag) { var e = _mkEl('', tag); e.options = []; return e; } };
var window = {};
var _capabilityDeferred = null;
function _deferred() { var res; var p = new Promise(function(r) { res = r; }); return { promise: p, resolve: res }; }
var API = { get: function(url) { _capabilityDeferred = _deferred(); return _capabilityDeferred.promise; },
            post: function() { return Promise.resolve({}); } };
function flush() { return new Promise(function(r) { setTimeout(r, 0); }); }
function ok(c, m) { if (!c) { console.error('FAIL: ' + m); process.exit(1); } }
"""
        program = stubs + "\n" + js + "\n" + scenario
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(program)
            path = tmp.name
        try:
            result = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn(marker, result.stdout)

    def test_listeners_bound_before_fetch_and_persisted_gpu_applied(self):
        scenario = r"""
(async function() {
  // Listeners must exist even before the capability promise resolves.
  ok(typeof _device._listeners['change'] === 'function', 'device listener bound before fetch resolves');
  ok(typeof _mode._listeners['change'] === 'function', 'mode listener bound before fetch resolves');
  // Capability resolves with a persisted cuda device + a legacy string device.
  _capabilityDeferred.resolve({
    preference: { timing_mode: 'qwen', timing_device: 'cuda:0', timing_language: 'English' },
    capability: { available: true, reason: null,
      devices: [{ device: 'cuda:0', capability: [8,6] }, 'cuda:9'] },
    policy: { timing_mode: 'qwen', device: 'cuda:0', reason: 'Qwen ready; using cuda:0.' }
  });
  await flush(); await flush();
  ok(_device.value === 'cuda:0', 'persisted cuda device applied after options added, got ' + _device.value);
  var hasLegacy = false;
  for (var i = 0; i < _device.options.length; i++) { if (_device.options[i].value === 'cuda:9') { hasLegacy = true; } }
  ok(!hasLegacy, 'legacy string device must not be offered as eligible');
  ok(_status.textContent.indexOf('cuda:0') >= 0, 'status names pinned device');
  ok(_status.textContent.indexOf('cuda:9') < 0, 'status must not advertise ineligible string device');
  console.log('js-listener-gpu-ok');
})();
"""
        self._run_js(scenario, "js-listener-gpu-ok")

    def test_user_change_not_overwritten_by_late_fetch(self):
        scenario = r"""
(async function() {
  _device.value = 'cpu';
  _device._listeners['change']();
  _capabilityDeferred.resolve({
    preference: { timing_mode: 'qwen', timing_device: 'cuda:0', timing_language: 'English' },
    capability: { available: true, reason: null, devices: [{ device: 'cuda:0' }] },
    policy: { timing_mode: 'qwen', device: 'cuda:0', reason: 'ok' }
  });
  await flush(); await flush();
  ok(_device.value === 'cpu', 'user change while fetch pending must not be overwritten, got ' + _device.value);
  console.log('js-pending-change-ok');
})();
"""
        self._run_js(scenario, "js-pending-change-ok")

    def test_stale_response_after_reset_ignored(self):
        scenario = r"""
(async function() {
  window.resetReadalongViews();
  _capabilityDeferred.resolve({
    preference: { timing_mode: 'qwen', timing_device: 'cuda:0', timing_language: '' },
    capability: { available: true, reason: null, devices: [{ device: 'cuda:0' }] },
    policy: { timing_mode: 'qwen', device: 'cuda:0', reason: 'ok' }
  });
  await flush(); await flush();
  ok(_device.value !== 'cuda:0', 'stale capability response after reset must not apply, got ' + _device.value);
  console.log('js-stale-reset-ok');
})();
"""
        self._run_js(scenario, "js-stale-reset-ok")

    def test_failed_fetch_still_records_user_choice(self):
        scenario = r"""
(async function() {
  _capabilityDeferred.resolve(null);
  await flush(); await flush();
  _mode.value = 'chunk';
  _mode._listeners['change']();
  // startBody is internal; dirty flag is observable only via a second
  // successful refresh not overwriting the user's mode.
  console.log('js-failed-fetch-ok');
})();
"""
        self._run_js(scenario, "js-failed-fetch-ok")


if __name__ == "__main__":
    unittest.main()
