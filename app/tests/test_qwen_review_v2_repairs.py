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
        rows, _clone_bytes = er._sentence_rows_for_segment(
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


class SentenceResourceTests(unittest.TestCase):
    def test_source_segment_budget_precedes_normalization(self):
        import epub_readaloud as er
        from tests.test_epub_readaloud_sentences import _source_epub
        source = _source_epub('<p id="lead">One. Two.</p>')
        entries = [dict(index=0, prepared_text="One. Two.",
                        start_frame=0, end_frame=RATE, sample_rate=RATE)]
        tokens = [dict(text="One", start_time=0.0, end_time=.4),
                  dict(text="Two", start_time=.5, end_time=.9)]
        with patch.object(er, "MAX_SENTENCE_SOURCE_CHARS", 4, create=True), \
                patch.object(er, "get_normalized_stream", side_effect=AssertionError("must not normalize")) as normalize:
            with self.assertRaisesRegex(ValueError, "segment.*budget"):
                er.build_epub_readaloud(retained_epub_bytes=source,
                    audio_bytes=b"\xff\xfbmp3", audio_filename="audiobook.mp3",
                    script_entries=entries, aligned_rows=[dict(tokens=tokens)])
        normalize.assert_not_called()

    def test_stack_snapshots_refuse_before_exceeding_budget(self):
        import xml.etree.ElementTree as ET
        import epub_readaloud as er
        nodes = [ET.Element("em") for _ in range(16)]
        events = [("open", node) for node in nodes] + [("text", "One.")]
        events += [("close", node) for node in reversed(nodes)]
        with patch.object(er, "MAX_INLINE_STACK_CELLS", 64, create=True):
            with self.assertRaisesRegex(ValueError, "stack.*budget"):
                er._event_index(events)

    def test_repeated_inline_attributes_refuse_before_clone_budget(self):
        import epub_readaloud as er
        from tests.test_epub_readaloud_sentences import _source_epub
        source = _source_epub('<p id="lead"><strong data-pad="' + 'a' * 512 + '">One. Two. Three.</strong></p>')
        entries = [dict(index=0, prepared_text="One. Two. Three.",
                        start_frame=0, end_frame=RATE * 2, sample_rate=RATE)]
        tokens = [dict(text=word, start_time=i * .5, end_time=i * .5 + .4)
                  for i, word in enumerate(("One", "Two", "Three"))]
        with patch.object(er, "MAX_SENTENCE_CLONE_BYTES", 1024, create=True):
            with self.assertRaisesRegex(ValueError, "clone.*budget"):
                er.build_epub_readaloud(retained_epub_bytes=source,
                    audio_bytes=b"\xff\xfbmp3", audio_filename="audiobook.mp3",
                    script_entries=entries, aligned_rows=[dict(tokens=tokens)])

    def test_alias_archive_names_are_refused(self):
        import epub_readaloud as er
        for name in ("EPUB/./x.bin", "EPUB//x.bin", "EPUB//"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                er._preflight_retained_members([zipfile.ZipInfo(name)])

    def test_special_archive_members_are_refused(self):
        import stat
        import epub_readaloud as er
        for kind in (stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK):
            info = zipfile.ZipInfo("EPUB/extra.bin")
            info.external_attr = (kind | 0o600) << 16
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                er._preflight_retained_members([info])


class DeepMetadataTests(unittest.TestCase):
    def test_early_language_checks_archive_budget_before_xml(self):
        import epub_readaloud as er
        import archive_utils
        from tests.test_epub_readaloud_sentences import _source_epub
        source = _source_epub('<p id="lead">One sentence.</p>')
        with patch.object(archive_utils, 'MAX_ARCHIVE_MEMBERS', 0), \
                patch.object(er, 'get_epub_package', side_effect=AssertionError('must not parse XML')) as parse:
            with self.assertRaisesRegex(ValueError, 'too many members'):
                er.get_epub_language_from_path(io.BytesIO(source))
        parse.assert_not_called()

    def test_unsupported_language_error_is_bounded(self):
        from qwen_align import get_aligner_language_name
        with self.assertRaises(ValueError) as caught:
            get_aligner_language_name('zz-' + 'a' * 1024)
        self.assertLess(len(str(caught.exception)), 512)

    def test_early_language_reader_uses_spine_validation(self):
        import epub_readaloud as er
        from tests.test_epub_readaloud_sentences import _source_epub
        original = _source_epub('<p id="lead">One sentence.</p>')
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, 'w') as target:
            for info in source.infolist():
                raw = source.read(info)
                if info.filename.endswith('.opf'):
                    raw = raw.replace(b'<dc:language>en</dc:language>', b'<dc:language>en-@</dc:language>')
                target.writestr(info, raw)
        bad = output.getvalue()
        with self.assertRaisesRegex(ValueError, 'language tag'):
            er.get_epub_language_from_path(io.BytesIO(bad))
        with self.assertRaisesRegex(ValueError, 'language tag'):
            er.get_spine_documents(bad)

    def test_deep_preference_is_explicit_corruption(self):
        from readalong_export import get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "readalong_timing.json").write_text("[" * 2000 + "0" + "]" * 2000)
            with self.assertRaisesRegex(ValueError, "corrupt"):
                get_readaloud_timing_preference(tmp)

    def test_deep_receipt_is_explicit_invalidity(self):
        from readalong_export import get_validated_epub_receipt
        with self.assertRaises(ValueError):
            get_validated_epub_receipt(b"[" * 2000 + b"0" + b"]" * 2000)

    def test_deep_probe_is_unknown_not_ready(self):
        from types import SimpleNamespace
        import qwen_runtime as qr
        from tests.test_qwen_review_v2_native_symlinks import _model
        with tempfile.TemporaryDirectory() as tmp:
            model = _model(tmp)
            output = SimpleNamespace(returncode=0, stdout=b"[" * 2000 + b"0" + b"]" * 2000)
            with patch.object(qr, "get_qwen_python_candidates", return_value=[sys.executable]), \
                    patch.object(qr, "get_qwen_model_candidates", return_value=[str(model)]), \
                    patch.object(qr, "run_owned_capture", return_value=output):
                result = qr.get_qwen_capability(tmp, status_only=True)
            self.assertFalse(result["available"])
            self.assertIn("malformed", result["reason"])


class RetainedReadResourceTests(_FixtureMixin):
    def test_canonical_source_obeys_upload_bound_without_second_read(self):
        import readalong_export as rexp
        tmp = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        original, _source = rexp._locate_retained_epub(manager, lambda: False)
        self.assertGreater(len(original), 64)
        with patch.object(rexp, "MAX_RETAINED_EPUB_BYTES", 64, create=True):
            with self.assertRaisesRegex(ValueError, "Retained EPUB.*bound"):
                rexp._locate_retained_epub(manager, lambda: False)


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
    def test_state_change_while_reserving_releases_claim_without_persisting(self):
        import core
        from fastapi import HTTPException
        from routers.editor import ReadaloudEpubRequest
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        preference = Path(tmp) / "readalong_timing.json"
        before = preference.read_bytes() if preference.exists() else None
        real = editor.reserve_background_task
        def reserve(*args, **kwargs):
            claim = real(*args, **kwargs)
            with editor.ensure_book_state(tmp):
                path = Path(tmp) / "chunks.json"
                rows = json.loads(path.read_text(encoding="utf-8"))
                rows[0]["text"] = "Changed while GPU reservation was outside the book lock."
                path.write_text(json.dumps(rows), encoding="utf-8")
            return claim
        tasks = _CaptureTasks()
        with patch.object(editor, "reserve_background_task", side_effect=reserve):
            with self.assertRaises(HTTPException) as caught:
                editor._start_readaloud_epub(tasks, ReadaloudEpubRequest(timing_mode="chunk"))
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual([], tasks.registered)
        self.assertNotIn("readaloud_epub", core._task_claims)
        self.assertEqual(before, preference.read_bytes() if preference.exists() else None)

    def test_task_reservation_does_not_hold_book_lock(self):
        import contextlib
        import core
        import qwen_runtime as qr
        from routers.editor import ReadaloudEpubRequest
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        capability = self._ready_capability(tmp)
        capability["devices"] = [dict(device="cuda:0", capability=[8, 6],
            free_bytes=8 * 1024**3, total_bytes=8 * 1024**3)]
        held = 0
        real_lock = editor.ensure_book_state
        real_reserve = editor.reserve_background_task
        reservations = []
        @contextlib.contextmanager
        def book_lock(*args, **kwargs):
            nonlocal held
            with real_lock(*args, **kwargs) as value:
                held += 1
                try:
                    yield value
                finally:
                    held -= 1
        def reserve(*args, **kwargs):
            self.assertEqual(0, held, "GPU admission/model release must run outside the book lock")
            reservations.append(kwargs["cpu_only"])
            return real_reserve(*args, **kwargs)
        with patch.object(editor, "ensure_book_state", book_lock), \
                patch.object(editor, "reserve_background_task", side_effect=reserve), \
                patch.object(qr, "get_qwen_capability", return_value=capability), \
                patch.object(core.project_manager, "ensure_idle_local_models_released", return_value=True):
            result = editor._start_readaloud_epub(_CaptureTasks(),
                ReadaloudEpubRequest(timing_mode="qwen", timing_language="English"))
        self.assertEqual("started", result["status"])
        self.assertEqual([False], reservations)
        core.release_gpu_task_claim("readaloud_epub", result["claim_id"], pending_only=True)

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
    def test_explicit_chunk_choice_refuses_qwen_receipt_without_probe(self):
        from fastapi import HTTPException
        import qwen_runtime as qr
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        self._set_pref(tmp, "chunk")
        with patch.object(qr, "get_qwen_capability", side_effect=AssertionError("must not probe")):
            with self.assertRaises(HTTPException) as caught:
                editor._require_readaloud_timing_current({"timing_mode": "qwen"})
        self.assertEqual(409, caught.exception.status_code)

    def test_retained_lookup_outside_lock_and_changed_provenance_refused(self):
        from contextlib import contextmanager
        from fastapi import HTTPException
        from routers.editor import ReadaloudEpubRequest
        import publication_source
        import readalong_export
        editor, _manager = self._fixture(self._mkdtmp())
        tasks = _CaptureTasks()
        editor._start_readaloud_epub(tasks, ReadaloudEpubRequest(timing_mode="chunk"))
        function, args, kwargs = tasks.registered[0]
        function(*args, **kwargs)
        self.assertEqual("done", editor.process_state["readaloud_epub"]["result"]["status"])
        held = []
        capture = publication_source.get_publication_source_capture
        locate = readalong_export.get_retained_epub_candidate_path
        lock = editor.ensure_book_state
        changed = []
        @contextmanager
        def observed_lock(*a, **k):
            with lock(*a, **k):
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()
        def observed_lookup(*a, **k):
            self.assertFalse(held, "large retained-source scan must not hold book lock")
            path = locate(*a, **k)
            changed.append(True)
            return path
        def changed_capture(*a, **k):
            value = capture(*a, **k)
            return {"changed_publication": value} if changed else value
        with patch.object(editor, "ensure_book_state", observed_lock), \
                patch.object(readalong_export, "get_retained_epub_candidate_path", observed_lookup), \
                patch.object(publication_source, "get_publication_source_capture", changed_capture):
            with self.assertRaises(HTTPException) as caught:
                editor._validated_epub_bytes()
        self.assertEqual(409, caught.exception.status_code)
        self.assertIn("provenance changed", caught.exception.detail)
        self.assertTrue(changed)

    def test_explicit_gpu_receipt_download_does_not_require_free_vram(self):
        import qwen_runtime as qr
        from readalong_export import save_readaloud_timing_preference
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        cap = self._ready_capability(tmp)
        cap["runtime_identity"] = cap["runtime"]
        cap["model_digest"] = qr.get_qwen_model_file_digest(cap["model_dir"])
        cap["devices"] = [dict(device="cuda:0", capability=[8, 6], free_bytes=0, total_bytes=8 * 1024**3)]
        provider = qr.get_qwen_provider_digest(cap["model_dir"], runtime_identity=dict(
            qwen_asr_version="0.0.6", torch_version=cap["torch_version"],
            device="cuda:0", dtype="bfloat16", identity=cap["runtime"]))
        receipt = dict(timing_mode="qwen", timing_device="cuda:0", timing_dtype="bfloat16",
                       timing_language="English", timing_provider=provider)
        save_readaloud_timing_preference(dict(timing_mode="qwen", timing_device="cuda:0", timing_language="English"), tmp)
        with patch.object(qr, "get_qwen_capability", return_value=cap), \
                patch.object(qr, "get_qwen_model_candidates", return_value=[cap["model_dir"]]):
            editor._require_readaloud_timing_current(receipt)
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy("qwen", "cuda:0", cap)

    def test_book_change_during_timing_check_refuses_download(self):
        from fastapi import HTTPException
        from routers.editor import ReadaloudEpubRequest
        editor, _manager = self._fixture(self._mkdtmp())
        tasks = _CaptureTasks()
        editor._start_readaloud_epub(tasks, ReadaloudEpubRequest(timing_mode="chunk"))
        function, args, kwargs = tasks.registered[0]
        function(*args, **kwargs)
        self.assertEqual("done", editor.process_state["readaloud_epub"]["result"]["status"])
        receipt, raw = editor._validated_epub_bytes()
        self.assertEqual(receipt["epub_sha256"], hashlib.sha256(raw).hexdigest())
        real = editor._require_readaloud_timing_current
        def change_book(value):
            real(value)
            chunks_path = Path(editor.DATA_DIR) / "chunks.json"
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            chunks[0]["text"] = "An edited narration row."
            chunks_path.write_text(json.dumps(chunks), encoding="utf-8")
        with patch.object(editor, "_require_readaloud_timing_current", side_effect=change_book) as probe:
            with self.assertRaises(HTTPException) as caught:
                editor._validated_epub_bytes()
        self.assertEqual(1, probe.call_count)
        self.assertEqual(409, caught.exception.status_code)

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
    def test_interpreter_replaced_during_query_is_not_bound_as_the_old_runtime(self):
        import types
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "interpreter.exe"
            executable.write_bytes(b"old-interpreter")
            def query(*args, **kwargs):
                replacement = Path(tmp) / "replacement.exe"
                replacement.write_bytes(b"new-interpreter")
                os.replace(replacement, executable)
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(dict(
                    python_version="3.12.13", prefix=tmp, packages={})).encode())
            with patch.object(qr, "run_owned_capture", side_effect=query):
                with self.assertRaisesRegex(ValueError, "changed"):
                    qr.get_qwen_runtime_identity(str(executable))

    def _model(self, tmp):
        model = Path(tmp) / "model"
        model.mkdir()
        (model / "config.json").write_text(json.dumps(
            {"model_type": "qwen3_asr", "thinker_config": {"model_type": "qwen3_forced_aligner"}, "support_languages": ["English"]}),
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
            {"model_type": "qwen3_asr", "thinker_config": {"model_type": "qwen3_forced_aligner"}, "support_languages": ["English"]}),
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
    def test_validator_checks_id_and_xml_id_as_one_namespace(self):
        import xml.etree.ElementTree as ET
        import epub_readaloud as er
        from tests.test_epub_readaloud_sentences import _source_epub
        output = self._build_auxiliary(_source_epub('<p id="source">Hello world.<span id="mark"/></p>'))
        for xml_id in ("mark", "source"):
            with self.subTest(xml_id=xml_id):
                buffer = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(output)) as src, zipfile.ZipFile(buffer, "w") as dst:
                    for info in src.infolist():
                        data = src.read(info.filename)
                        if info.filename == "OEBPS/ch1.xhtml":
                            root = ET.fromstring(data)
                            node = next(n for n in root.iter() if n.get("id") == "mark")
                            node.set("{http://www.w3.org/XML/1998/namespace}id", xml_id)
                            data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                        dst.writestr(info, data)
                if xml_id == "source":
                    with self.assertRaisesRegex(ValueError, "duplicate id"):
                        er.validate_epub_readaloud(buffer.getvalue())
                else:
                    er.validate_epub_readaloud(buffer.getvalue())

    def _source_with_auxiliary(self, count=1, padding=0, large_nav=False):
        from tests.test_epub_readaloud_sentences import _source_epub
        source = _source_epub('<p id="source">Hello world.</p>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as src, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename.endswith(".opf"):
                    items = ''.join('<item id="aux-%d" href="aux%d.xhtml" media-type="application/xhtml+xml"/>' % (i, i) for i in range(count))
                    data = data.replace(b'</manifest>', items.encode() + b'</manifest>')
                if large_nav and info.filename == "OEBPS/nav.xhtml":
                    data = data.replace(b'</body>', b' ' * (9 * 1024**2) + b'</body>')
                dst.writestr(info, data)
            for i in range(count):
                dst.writestr("OEBPS/aux%d.xhtml" % i,
                    b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Appendix</title></head><body><p>'
                    + b' ' * padding + b'An appendix.</p></body></html>')
        return buffer.getvalue()

    def _build_auxiliary(self, source):
        from epub_readaloud import build_epub_readaloud
        return build_epub_readaloud(retained_epub_bytes=source,
            audio_bytes=b"\xff\xfbmp3", audio_filename="narration.mp3",
            script_entries=[dict(index=0, prepared_text="Hello world.",
                start_frame=0, end_frame=24000, sample_rate=24000)])[0]

    def test_large_unmodified_xhtml_and_navigation_keep_previous_acceptance(self):
        from epub_readaloud import validate_epub_readaloud, _ensure_html5_doctype
        for padding, large_nav in [(9 * 1024**2, False), (0, True)]:
            with self.subTest(padding=padding, large_nav=large_nav):
                source = self._source_with_auxiliary(padding=padding, large_nav=large_nav)
                output = self._build_auxiliary(source)
                validate_epub_readaloud(output)
                with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(io.BytesIO(output)) as built:
                    self.assertEqual(_ensure_html5_doctype(original.read("OEBPS/aux0.xhtml")), built.read("OEBPS/aux0.xhtml"))

    def test_large_ncx_and_missing_optional_ncx_keep_previous_behavior(self):
        from epub_readaloud import get_ncx_points, NCX_MEDIA_TYPE
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("OEBPS/toc.ncx", b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
                + b' ' * (3 * 1024**2)
                + b'<navMap><navPoint id="one"><navLabel><text>One</text></navLabel><content src="ch1.xhtml#source"/></navPoint></navMap></ncx>')
        manifest = {"ncx": dict(media_type=NCX_MEDIA_TYPE, path="OEBPS/toc.ncx")}
        self.assertEqual("One", get_ncx_points(buffer.getvalue(), manifest, "OEBPS/content.opf")[0]["label"])
        manifest["ncx"]["path"] = "OEBPS/missing.ncx"
        self.assertEqual([], get_ncx_points(buffer.getvalue(), manifest, "OEBPS/content.opf"))

    def test_many_auxiliary_documents_do_not_rescan_directory_per_member(self):
        source = self._source_with_auxiliary(count=100)
        real = zipfile.ZipFile.infolist
        calls = []
        def directory(archive):
            calls.append(1)
            return real(archive)
        with patch.object(zipfile.ZipFile, "infolist", directory):
            self._build_auxiliary(source)
        self.assertLess(len(calls), 80, "Repeated directory scans grow quadratically with auxiliary XHTML count")

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
        # The retained NCX uses the archive budget, not the unrelated
        # package/container cap. Scale the real guard rather than allocate20GiB.
        import archive_utils
        with patch.object(archive_utils, "MAX_ARCHIVE_BYTES", 2 * 1024**2):
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
    def test_oversized_device_refused_before_persistence(self):
        from pydantic import ValidationError
        from routers.editor import ReadaloudEpubRequest
        with self.assertRaises(ValidationError):
            ReadaloudEpubRequest(timing_mode="chunk", timing_device="cuda:" + "9" * 17000)

    def test_save_validates_before_replacing_previous_settings(self):
        from readalong_export import save_readaloud_timing_preference, get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "readalong_timing.json"
            previous = b'{"timing_mode":"chunk","timing_device":"cpu","timing_language":null}'
            path.write_bytes(previous)
            invalid = dict(timing_mode="chunk", timing_device="cuda:" + "9" * 17000, timing_language=None)
            with self.assertRaises(ValueError):
                save_readaloud_timing_preference(invalid, tmp)
            self.assertEqual(previous, path.read_bytes())
            invalid["timing_device"] = "cuda:" + "9" * 4000
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(ValueError):
                get_readaloud_timing_preference(tmp)

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
    def test_asr_model_role_is_not_advertised_as_a_forced_aligner(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            config = json.loads((model / "config.json").read_text(encoding="utf-8"))
            config["thinker_config"] = {"model_type": "qwen3_asr_thinker"}
            (model / "config.json").write_text(json.dumps(config), encoding="utf-8")
            self.assertFalse(self._status(tmp, model)["available"])

    def test_sharded_weights_require_every_indexed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            (model / "model.safetensors").unlink()
            (model / "model-00001-of-00002.safetensors").write_bytes(b"first shard")
            index = model / "model.safetensors.index.json"
            index.write_text(json.dumps({"weight_map": {"one": "model-00001-of-00002.safetensors",
                "two": "model-00002-of-00002.safetensors"}}), encoding="utf-8")
            self.assertFalse(self._status(tmp, model)["available"])
            (model / "model-00002-of-00002.safetensors").write_bytes(b"second shard")
            self.assertTrue(self._status(tmp, model)["available"])
            index.write_text('{"weight_map":{"unsafe":"../outside.safetensors"}}', encoding="utf-8")
            self.assertFalse(self._status(tmp, model)["available"])

    def test_status_refuses_missing_worker_before_runtime_probe(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            real = qr.os.lstat
            def missing(path, *args, **kwargs):
                if Path(path).name == "qwen_align_worker.py":
                    raise FileNotFoundError("missing worker")
                return real(path, *args, **kwargs)
            with patch.object(qr.os, "lstat", side_effect=missing):
                self.assertFalse(self._status(tmp, model)["available"])

    def test_hf_local_download_bookkeeping_is_not_model_content(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            before = qr.get_qwen_model_file_digest(str(model))
            metadata = model / (".CACHE" if os.name == "nt" else ".cache") / "huggingface" / "download"
            metadata.mkdir(parents=True)
            (metadata / "config.json.lock").write_bytes(b"")
            (metadata / "config.json.metadata").write_text("download bookkeeping", encoding="utf-8")
            self.assertTrue(self._status(tmp, model)["available"])
            self.assertEqual(before, qr.get_qwen_model_file_digest(str(model)))
            (model / ".cache" / "actual-model-data.bin").write_bytes(b"model content")
            self.assertNotEqual(before, qr.get_qwen_model_file_digest(str(model)))

    def test_model_digest_frames_names_and_sizes_unambiguously(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = self._complete_model(a), self._complete_model(b)
            (first / "a1").write_bytes(b"ABC")
            (first / "b").write_bytes(b"1234567")
            (second / "a").write_bytes(b"ABCb7\0" + b"1234567")
            self.assertNotEqual(qr.get_qwen_model_file_digest(str(first)),
                                qr.get_qwen_model_file_digest(str(second)))

    def test_invalid_model_config_fails_closed_without_probe(self):
        import qwen_runtime as qr
        invalid = [b'{"model_type":"qwen3_asr","thinker_config":{"model_type":"qwen3_forced_aligner"},"support_languages":[{},"English"]}',
                   b'{"deep":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}']
        for data in invalid:
            with self.subTest(data=data[:50]), tempfile.TemporaryDirectory() as tmp:
                model = self._complete_model(tmp)
                (model / "config.json").write_bytes(data)
                spy = patch.object(qr, "run_owned_capture", side_effect=AssertionError("invalid metadata must not probe"))
                self.assertFalse(self._status(tmp, model, run_cap=spy)["available"])

    def test_skipped_names_still_obey_inventory_entry_budget(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = self._complete_model(tmp)
            for i in range(20):
                (model / (".ignored-%d" % i)).write_bytes(b"bookkeeping")
            with patch.object(qr, "_MAX_MODEL_FILES", 16):
                self.assertFalse(self._status(tmp, model)["available"])
                with self.assertRaises(ValueError):
                    qr.get_qwen_model_file_digest(str(model))

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
            with patch.object(qr.os, "scandir", side_effect=OSError("unreadable")):
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
            original = qr.get_qwen_model_file_digest(str(model))
            with self.assertRaises(ValueError):
                qr.get_qwen_model_file_digest(str(model), cancel_check=lambda: True)
            self.assertEqual(original, qr.get_qwen_model_file_digest(str(model)))


class StaleRollbackTests(_FixtureMixin):
    def test_rollback_does_not_claim_an_existing_fixed_temporary_file(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        path = Path(tmp) / "readalong_timing.json"
        collision = Path(tmp) / ".readalong_timing.restore.tmp"
        collision.write_bytes(b"unrelated existing file")
        previous = b'{"timing_mode":"chunk","timing_device":"cpu","timing_language":null}'
        path.write_bytes(b"new preference")
        editor._restore_readaloud_timing_preference_bytes(str(path), previous)
        self.assertEqual(previous, path.read_bytes())
        self.assertEqual(b"unrelated existing file", collision.read_bytes())

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
        source = _source_epub(body)
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
        calls = []
        cancelled = [False]
        def fake_run(*a, **k):
            calls.append(1)
            cancelled[0] = True
            raise ValueError("Qwen alignment cancelled.")
        with patch.object(_qr, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(FAKE)]), \
                patch.object(_qr, "run_qwen_alignment_full",
                             side_effect=fake_run):
            ok, message = build_epub_export(manager, lambda: cancelled[0],
                                            timing=timing)
        self.assertEqual(1, len(calls), "worker bridge must be reached")
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

    def test_probe_env_stripped(self):
        import qwen_runtime as qr
        with patch.dict(os.environ, {"PYTHONPATH": "/evil",
                                     "LD_PRELOAD": "/evil.so",
                                     "PATH": os.environ.get("PATH", "")}):
            env = qr._isolated_worker_env()
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("LD_PRELOAD", env)



    def test_normalizer_aa2_agreement(self):
        import qwen_align as qa
        from epub_readaloud import get_normalized_words
        for text in ("Chapter_1", "He said 'hello' loudly.",
                     "'Hello' world.", "don't stop", "Straße ﬁ"):
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
        from routers.editor import ReadaloudEpubRequest
        cases = [(False, "Export cancelled", "cancelled", "EPUB read-aloud cancelled"),
                 (False, "deliberate failure", "failed", "EPUB read-aloud failed"),
                 (True, "completed fixture", "done", "EPUB read-aloud complete")]
        for success, message, status, log in cases:
            with self.subTest(status=status):
                editor, _manager = self._fixture(self._mkdtmp())
                try:
                    tasks = _CaptureTasks()
                    editor._start_readaloud_epub(tasks, ReadaloudEpubRequest(timing_mode="chunk"))
                    self.assertEqual(1, len(tasks.registered))
                    function, args, kwargs = tasks.registered[0]
                    with patch("readalong_export.build_epub_export", return_value=(success, message)) as build:
                        function(*args, **kwargs)
                    build.assert_called_once()
                    state = editor.process_state["readaloud_epub"]
                    self.assertEqual(status, state["result"]["status"])
                    self.assertIn(log, state["logs"][-1])
                    self.assertFalse(state["running"])
                finally:
                    self.doCleanups()



class PubPhaseTests(_FixtureMixin):
    def test_prepublication_fresh_hash_after_worker(self):
        from tests.test_readalong_qwen import QwenBuildTests
        import qwen_runtime as qr
        import epub_readaloud as er
        fixture = QwenBuildTests()
        tmp = fixture._mkdtmp()
        upstream = fixture._mkdtmp()
        editor, manager = fixture._fixture(tmp)
        model_dir = Path(upstream) / "model"
        model_dir.mkdir(exist_ok=True)
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        cap = fixture._capability(tmp, upstream)
        cap["model_digest"] = qr.get_qwen_model_file_digest(str(model_dir))
        cap["provider_digest"] = qr.get_qwen_provider_digest(
            str(model_dir), runtime_identity=dict(
                qwen_asr_version="0.0.6", torch_version="2.10.0+cpu",
                device="cpu", dtype="float32", identity=cap["runtime"]))
        mutations = []
        real_packager = er.build_epub_readaloud
        def package(**kwargs):
            output = real_packager(**kwargs)
            (model_dir / "config.json").write_text(
                '{"synthetic":"changed"}', encoding="utf-8")
            mutations.append(True)
            return output
        tasks = _CaptureTasks()
        from routers.editor import ReadaloudEpubRequest
        with patch.object(qr, "get_qwen_capability", lambda *a, **kw: cap), \
                patch.object(qr, "get_qwen_worker_command",
                             lambda py: [sys.executable, str(FAKE)]), \
                patch.object(er, "build_epub_readaloud", side_effect=package):
            editor._start_readaloud_epub(
                tasks, ReadaloudEpubRequest(timing_mode="qwen",
                                            timing_device="cpu"))
            func, args, kwargs = tasks.registered[0]
            func(*args, **kwargs)
        state = editor.process_state["readaloud_epub"]
        self.assertTrue(mutations)
        self.assertEqual("failed", state["result"]["status"])
        self.assertIn("provider changed", state["result"]["message"].lower())
        fixture.doCleanups()


class RetainedSourceResolutionTests(_FixtureMixin):
    def test_fallback_budgets_and_cancellation_do_not_return_partial_matches(self):
        import archive_utils
        import readalong_export as rexp
        from tts import ExportCancelled
        tmp = self._mkdtmp()
        self._fixture(tmp)
        source = str(Path(tmp) / "book.txt")
        retained = Path(tmp) / "book.source.epub"
        retained.rename(retained.with_name("renamed.source.epub"))
        with patch.object(archive_utils, "MAX_ARCHIVE_MEMBERS", 1):
            with self.assertRaisesRegex(ValueError, "entry budget"):
                rexp.get_retained_epub_candidate_path(source)
        with patch.object(archive_utils, "MAX_ARCHIVE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "byte budget"):
                rexp.get_retained_epub_candidate_path(source)
        real = rexp.get_audio_file_sha256
        inside = False
        polls = 0
        def cancel():
            nonlocal polls
            if inside:
                polls += 1
                return polls >= 2
            return False
        def hashing(*args, **kwargs):
            nonlocal inside
            inside = True
            try:
                return real(*args, **kwargs)
            finally:
                inside = False
        with patch.object(rexp, "get_audio_file_sha256", side_effect=hashing):
            with self.assertRaises(ExportCancelled):
                rexp.get_retained_epub_candidate_path(source, cancel_check=cancel)
        self.assertEqual(2, polls, "Cancellation must reach the actual streamed hash")
        with self.assertRaises(ValueError):
            real(str(Path(tmp) / "absent"), limit=-1)

    def test_renamed_fallback_matches_builder_and_ambiguous_refuses(self):
        from readalong_export import get_retained_epub_candidate_path, _locate_retained_epub
        tmp = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        source = str(Path(tmp) / "book.txt")
        retained = Path(tmp) / "book.source.epub"
        expected = retained.read_bytes()
        renamed = retained.with_name("renamed.source.epub")
        retained.rename(renamed)
        self.assertEqual(str(renamed), get_retained_epub_candidate_path(source))
        self.assertEqual((expected, source), _locate_retained_epub(manager, lambda: False))
        renamed.with_name("duplicate.source.epub").write_bytes(expected)
        with self.assertRaisesRegex(ValueError, "Multiple retained"):
            get_retained_epub_candidate_path(source)
        with self.assertRaisesRegex(ValueError, "Multiple retained"):
            _locate_retained_epub(manager, lambda: False)

    def test_language_uses_bounded_archive_reader(self):
        from epub_readaloud import get_epub_language_from_path
        tmp = self._mkdtmp()
        self._fixture(tmp)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole archive read")):
            self.assertEqual("en", get_epub_language_from_path(Path(tmp) / "book.source.epub"))


class SvgFirstTests(unittest.TestCase):
    def test_estimated_window_float_endpoint_obeys_strict_cap(self):
        from qwen_align import get_aligner_windows
        duration = 270.0171231911396
        sentences = [dict(start=0, end=160, words=80),
                     dict(start=160, end=516, words=178),
                     dict(start=516, end=536, words=10)]
        windows = get_aligner_windows(duration, sentences)
        self.assertEqual(3, len(windows))
        start = windows[1]["start_sec"]
        self.assertGreater((start + 180) - start, 180)
        for window in windows:
            self.assertGreater(window["end_sec"], window["start_sec"])
            self.assertLessEqual(window["end_sec"] - window["start_sec"], 180)
        self.assertEqual(0, windows[0]["char_start"])
        self.assertEqual(536, windows[-1]["char_end"])

    def _source_with_svg(self, svg):
        from tests.test_epub_readaloud_sentences import _source_epub
        source = _source_epub('<p id="lead">Hello world.</p>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as src, zipfile.ZipFile(buffer, "w") as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename.endswith(".opf"):
                    data = data.decode().replace('</manifest>',
                        '<item id="first-svg" href="first.svg" media-type="image/svg+xml"/></manifest>').replace(
                        '<spine>', '<spine><itemref idref="first-svg"/>').encode()
                dst.writestr(info, data)
            dst.writestr("OEBPS/first.svg", svg)
        return buffer.getvalue()

    def _build(self, source, **kwargs):
        from epub_readaloud import build_epub_readaloud
        return build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b"\xff\xfbmp3",
            audio_filename="audiobook.mp3", script_entries=[dict(
                index=0, prepared_text="Hello world.", start_frame=0,
                end_frame=24000, sample_rate=24000)], **kwargs)

    def test_text_first_large_svg_refused(self):
        import xml.etree.ElementTree as ET
        for text in ('<text>spoken</text>', '<text><tspan><![CDATA[spoken]]></tspan></text>'):
            with self.subTest(text=text):
                svg = ('<svg xmlns="http://www.w3.org/2000/svg">' + text
                       + '<path d="M0 0h1"/>' * 20000 + '</svg>')
                ET.fromstring(svg)
                with self.assertRaisesRegex(ValueError, "SVG"):
                    self._build(self._source_with_svg(svg))

    def test_archive_budget_refused_before_svg_scan(self):
        import archive_utils
        import epub_readaloud as er
        svg = '<svg xmlns="http://www.w3.org/2000/svg">' + '<path d="M0 0h1"/>' * 20000 + '</svg>'
        with patch.object(archive_utils, "MAX_ARCHIVE_BYTES", 4096), \
                patch.object(er, "_svg_has_text", wraps=er._svg_has_text) as probe:
            with self.assertRaises(ValueError):
                self._build(self._source_with_svg(svg))
        self.assertEqual(0, probe.call_count)

    def test_textless_svg_accepted_and_cancel_during_scan_refused(self):
        import epub_readaloud as er
        svg = '<svg xmlns="http://www.w3.org/2000/svg">' + '<path d="M0 0h1"/>' * 20000 + '</svg>'
        source = self._source_with_svg(svg)
        built, _ = self._build(source)
        er.validate_epub_readaloud(built)
        real = er._svg_has_text
        inside = False
        polls = 0
        def cancel():
            nonlocal polls
            if inside:
                polls += 1
                return polls >= 2
            return False
        def scan(*args, **kwargs):
            nonlocal inside
            inside = True
            try:
                return real(*args, **kwargs)
            finally:
                inside = False
        with patch.object(er, "_svg_has_text", side_effect=scan) as probe:
            with self.assertRaisesRegex(ValueError, "[Cc]ancelled"):
                self._build(source, cancel_check=cancel)
        self.assertEqual(1, probe.call_count)
        self.assertGreaterEqual(polls, 2)

    def test_multi_leading_punctuation_preserved(self):
        from epub_readaloud import build_epub_readaloud, validate_epub_readaloud
        from tests.test_epub_readaloud_sentences import _source_epub
        source = _source_epub('<p id="lead">! ? Hello world. We read.</p>')
        prepared = "! ? Hello world. We read."
        entries = [{"index": 0, "prepared_text": prepared,
                    "start_frame": 0, "end_frame": 48000, "sample_rate": 24000}]
        tokens = [{"text": w, "start_time": round((i + 1) * 0.4, 3),
                   "end_time": round((i + 2) * 0.4, 3)}
                  for i, w in enumerate(["Hello", "world", "We", "read"])]
        result, _ = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b"\xff\xfbmp3",
            audio_filename="audiobook.mp3", script_entries=entries,
            aligned_rows=[{"tokens": tokens}])
        validate_epub_readaloud(result)
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            doc = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
        visible = "".join(doc.itertext())
        self.assertIn("! ? Hello world. We read.", visible, repr(visible))
        self.assertEqual(1, visible.count("Hello world. We read."))


class DevicePolicyContractTests(unittest.TestCase):
    def test_automatic_mode_respects_an_explicit_device(self):
        import qwen_runtime as qr
        devices = [dict(device="cuda:0", capability=[8, 6], free_bytes=6 * 1024**3, total_bytes=8 * 1024**3),
                   dict(device="cuda:1", capability=[7, 5], free_bytes=4 * 1024**3, total_bytes=6 * 1024**3)]
        cap = dict(available=True, cpu_fallback=True, devices=devices)
        self.assertEqual("cpu", qr.get_qwen_device_policy("auto", "cpu", cap)["device"])
        self.assertEqual("cuda:1", qr.get_qwen_device_policy("auto", "cuda:1", cap)["device"])
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy("auto", "cuda:9", cap)
        self.assertEqual("chunk", qr.get_qwen_device_policy("auto", "auto", ["malformed"])["timing_mode"])
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy("qwen", "auto", ["malformed"])


class NvmlProbeTests(unittest.TestCase):
    def _probe(self, visible=(0, 1), cuda_build=True, mask=None,
               memory_errors=(), driver_error=False, missing_nvml=False):
        import contextlib
        import importlib.machinery
        import importlib.metadata
        import types
        import uuid
        import qwen_runtime as qr
        trace = []
        uuids = [uuid.UUID(int=i + 1) for i in range(2)]
        torch = types.ModuleType("torch")
        torch.version = types.SimpleNamespace(cuda="12.8" if cuda_build else None)
        class ForbiddenCuda:
            def __getattr__(self, name):
                trace.append(("forbidden-torch-cuda", name))
                raise AssertionError("CUDA context API called")
        torch.cuda = ForbiddenCuda()
        qwen = types.ModuleType("qwen_asr")
        qwen.__spec__ = importlib.machinery.ModuleSpec("qwen_asr", loader=None)
        qwen.Qwen3ForcedAligner = object
        driver = types.ModuleType("cuda.bindings.driver")
        driver.CUresult = types.SimpleNamespace(CUDA_SUCCESS=0)
        def init(flags):
            trace.append(("driver-init", flags))
            return (1 if driver_error else 0,)
        def count():
            trace.append(("driver-count",))
            return (0, len(visible))
        def device(index):
            trace.append(("driver-device", index))
            return (0, visible[index])
        def device_uuid(handle):
            trace.append(("driver-uuid", handle))
            return (0, types.SimpleNamespace(bytes=uuids[handle].bytes))
        driver.cuInit, driver.cuDeviceGetCount = init, count
        driver.cuDeviceGet, driver.cuDeviceGetUuid = device, device_uuid
        def forbidden_driver(name):
            if not name.startswith("__"):
                trace.append(("forbidden-driver", name))
            raise AttributeError(name)
        driver.__getattr__ = forbidden_driver
        cuda = types.ModuleType("cuda")
        cuda.__path__ = []
        bindings = types.ModuleType("cuda.bindings")
        bindings.__path__ = []
        cuda.bindings, bindings.driver = bindings, driver
        nvml = types.ModuleType("pynvml")
        nvml.nvmlInit = lambda: trace.append(("nvml-init",))
        nvml.nvmlShutdown = lambda: trace.append(("nvml-shutdown",))
        def handle(value):
            trace.append(("nvml-uuid", value))
            return ["GPU-" + str(u) for u in uuids].index(value)
        def memory(handle):
            if handle in memory_errors:
                raise RuntimeError("free memory unavailable")
            return types.SimpleNamespace(free=6 * 1024**3, total=8 * 1024**3)
        nvml.nvmlDeviceGetHandleByUUID = handle
        nvml.nvmlDeviceGetName = lambda h: ("GPU%d" % h).encode()
        nvml.nvmlDeviceGetMemoryInfo = memory
        nvml.nvmlDeviceGetCudaComputeCapability = lambda h: (8, 6)
        modules = {"torch": torch, "qwen_asr": qwen, "cuda": cuda,
                   "cuda.bindings": bindings, "cuda.bindings.driver": driver,
                   "pynvml": None if missing_nvml else nvml}
        versions = {"qwen-asr": "0.0.6", "torch": "2.10.0",
                    "nvidia-ml-py": "13.615.71", "cuda-bindings": "13.4.4"}
        env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID")
        if mask is None:
            env.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            env["CUDA_VISIBLE_DEVICES"] = mask
        output = io.StringIO()
        with patch.dict(sys.modules, modules), \
                patch.object(importlib.metadata, "version", side_effect=versions.__getitem__), \
                patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output):
            exec(compile(qr._PROBE_SOURCE, "qwen-capability-probe", "exec"), {})
        payload = json.loads(output.getvalue().strip())
        self.assertFalse([t for t in trace if t[0].startswith("forbidden")], trace)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["cpu_fallback"])
        return payload, trace

    def test_cpu_build_does_not_advertise_physical_cuda(self):
        payload, trace = self._probe(cuda_build=False)
        self.assertEqual([], payload["devices"])
        self.assertEqual([], trace)

    def test_empty_visibility_queries_no_nvml_devices(self):
        payload, trace = self._probe(visible=(), mask="")
        self.assertEqual([], payload["devices"])
        self.assertIn(("driver-count",), trace)
        self.assertNotIn(("nvml-init",), trace)

    def test_driver_order_and_uuid_are_authoritative(self):
        payload, trace = self._probe(visible=(1, 0))
        self.assertEqual([("cuda:0", "GPU1"), ("cuda:1", "GPU0")],
                         [(d["device"], d["name"]) for d in payload["devices"]])
        self.assertTrue(all(d["capability"] == [8, 6] for d in payload["devices"]))
        self.assertEqual(2, sum(t[0] == "driver-uuid" for t in trace))
        self.assertIn(("nvml-shutdown",), trace)

    def test_visibility_is_delegated_to_driver(self):
        # Expected visible devices come from the CUDA driver contract,
        # not another Python visibility parser mirroring production.
        masks = ("1,-1,0", "1,99,0", "GPU-00000000-0000-0000-0000-000000000002")
        for mask in masks:
            with self.subTest(mask=mask):
                payload, trace = self._probe(visible=(1,), mask=mask)
                self.assertEqual([("cuda:0", "GPU1")],
                                 [(d["device"], d["name"]) for d in payload["devices"]])
                self.assertEqual(1, sum(t[0] == "driver-device" for t in trace))

    def test_unknown_free_preserves_logical_ordinal_and_cpu(self):
        payload, trace = self._probe(memory_errors=(0,))
        self.assertEqual(["cuda:1"], [d["device"] for d in payload["devices"]])
        self.assertIn(("nvml-shutdown",), trace)

    def test_driver_failure_remains_cpu_only(self):
        payload, trace = self._probe(driver_error=True)
        self.assertEqual([], payload["devices"])
        self.assertNotIn(("nvml-init",), trace)
        self.assertIn("metadata unavailable", payload["reason"])

    def test_missing_nvml_remains_cpu_only(self):
        payload, trace = self._probe(missing_nvml=True)
        self.assertEqual([], payload["devices"])
        self.assertIn(("driver-count",), trace)
        self.assertIn("metadata unavailable", payload["reason"])




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
  if (tag === 'select') {
    var selected = null;
    Object.defineProperty(el, 'value', {
      get: function() { return selected === null ? (el.options.length ? el.options[0].value : '') : selected; },
      set: function(value) { selected = el.options.some(function(o) { return o.value === value; }) ? value : ''; }
    });
    el.remove = function(index) { var removed = el.options.splice(index, 1)[0]; if (removed && removed.value === selected) { selected = null; } };
  }
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

    def test_reset_discards_old_book_dirty_choices(self):
        self._run_js(r"""
(async function() {
  _mode.value = 'chunk'; _mode._listeners.change();
  window.resetReadalongViews();
  _capabilityDeferred.resolve({preference:{timing_mode:'qwen',timing_device:'cpu',timing_language:'English'},
    capability:{available:true,devices:[]},policy:{timing_mode:'qwen',device:'cpu',available:true,reason:'ready'}});
  await flush(); await flush();
  ok(_mode.value === 'qwen', 'new book preference must replace historical dirty mode');
  console.log('reset-dirty-ok');
})();
""", "reset-dirty-ok")

    def test_missing_persisted_device_remains_visible_as_unavailable(self):
        self._run_js(r"""
(async function() {
  _capabilityDeferred.resolve({preference:{timing_mode:'qwen',timing_device:'cuda:9',timing_language:'English'},
    capability:{available:true,devices:[]},policy:{timing_mode:'qwen',device:'cuda:9',available:false,reason:'unavailable'}});
  await flush(); await flush();
  ok(_device.value === 'cuda:9', 'absent saved device must not silently become a blank selection');
  ok(_device.options.some(function(o) { return o.value === 'cuda:9' && o.disabled && o.textContent.indexOf('unavailable') >= 0; }), 'missing device labeled unavailable');
  window.resetReadalongViews();
  _capabilityDeferred.resolve({preference:{timing_mode:'chunk',timing_device:'cpu'},capability:{available:false,devices:[]},policy:{timing_mode:'chunk',device:'cpu',reason:'chunk'}});
  await flush(); await flush();
  ok(!_device.options.some(function(o) { return o.value === 'cuda:9'; }), 'old dynamic options must be removed');
  console.log('unavailable-device-ok');
})();
""", "unavailable-device-ok")

    def test_synchronous_capability_failure_does_not_break_reset(self):
        self._run_js(r"""
API.get = function() { throw new Error('synchronous transport fault'); };
window.resetReadalongViews();
ok(_status.textContent.indexOf('unknown') >= 0, 'transport failure must render unknown');
ok(typeof window.buildReadaloudEpub === 'function', 'handlers must remain installed');
console.log('sync-fault-ok');
""", "sync-fault-ok")

    def test_accepted_start_acknowledges_only_submitted_edits(self):
        for late in (False, True):
            with self.subTest(late=late):
                scenario = r"""
(async function() {
  _els['readaloud-status'] = _mkEl('readaloud-status');
  window.ensureReadalongIntegrityApproval = function() { return Promise.resolve('a'.repeat(64)); };
  var posted = [], first = _deferred();
  var started = {status:'started',claim_id:'c'.repeat(32),book_token:'b'.repeat(64),preference:{timing_mode:'chunk',timing_device:'auto',timing_language:null}};
  API.post = function(url, body) { posted.push(body); return posted.length === 1 ? first.promise : Promise.resolve(started); };
  API.get = function() { return Promise.resolve({running:false,result:{status:'failed',message:'controlled terminal fixture'}}); };
  _mode.value = 'chunk'; _mode._listeners.change();
  window.buildReadaloudEpub(); await flush();
  LATE_EDIT
  first.resolve(started); await flush(); await flush();
  window.buildReadaloudEpub(); await flush(); await flush();
  ok(posted.length === 2, 'both real start handlers must post');
  EXPECTATION
  console.log('acknowledged-edits-ok');
})();
"""
                scenario = scenario.replace('LATE_EDIT', "_mode.value = 'qwen'; _mode._listeners.change();" if late else '')
                scenario = scenario.replace('EXPECTATION', "ok(posted[1].timing_mode === 'qwen', 'late edits must remain pending');" if late else "ok(!Object.prototype.hasOwnProperty.call(posted[1], 'timing_mode'), 'already accepted choice must not be resent as a new edit');")
                self._run_js(scenario, "acknowledged-edits-ok")

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

    def test_policy_unready_not_rendered_ready(self):
        scenario = r"""
(async function() {
  _capabilityDeferred.resolve({
    preference: { timing_mode: 'qwen', timing_device: 'cuda:0', timing_language: 'English' },
    capability: { available: true, reason: null, devices: [{ device: 'cuda:0', capability: [8,6] }] },
    policy: { timing_mode: 'qwen', device: 'cuda:0', available: false,
              reason: 'Qwen not ready: Device cuda:0 does not report enough free VRAM' }
  });
  await flush(); await flush();
  ok(_status.textContent.indexOf('Qwen3 ready') < 0, 'unready policy must not render ready, got: ' + _status.textContent);
  ok(_status.textContent.indexOf('not ready') >= 0, 'not-ready guidance must show, got: ' + _status.textContent);
  console.log('js-policy-unready-ok');
})();
"""
        self._run_js(scenario, "js-policy-unready-ok")

    def test_stale_response_after_reset_ignored(self):
        # Reset bumps the generation (discarding the in-flight
        # response) AND re-probes: the new response must apply.
        scenario = r"""
(async function() {
  window.resetReadalongViews();
  _capabilityDeferred.resolve({
    preference: { timing_mode: 'qwen', timing_device: 'cuda:0', timing_language: '' },
    capability: { available: true, reason: null, devices: [{ device: 'cuda:0' }] },
    policy: { timing_mode: 'qwen', device: 'cuda:0', reason: 'ok' }
  });
  await flush(); await flush();
  ok(_device.value === 'cuda:0', 'new-generation capability after reset must apply, got ' + _device.value);
  console.log('js-stale-reset-ok');
})();
"""
        self._run_js(scenario, "js-stale-reset-ok")

    def test_reset_refreshes_and_new_generation_can_complete(self):
        scenario = r"""
(async function() {
  var old = _capabilityDeferred;
  window.resetReadalongViews();
  var fresh = _capabilityDeferred;
  ok(fresh !== old, 'reset must issue a fresh status request');
  old.resolve({ preference: {timing_device:'cuda:9'}, capability: {available:true}, policy:{device:'cuda:9'} });
  await flush(); await flush();
  ok(_device.value !== 'cuda:9', 'old generation cannot paint');
  fresh.resolve({ preference: {timing_mode:'qwen', timing_device:'cuda:0', timing_language:'English'},
    capability:{available:true, devices:[{device:'cuda:0',capability:[8,6]}]},
    policy:{timing_mode:'qwen',device:'cuda:0',available:true,reason:'ready'} });
  await flush(); await flush();
  ok(_device.value === 'cuda:0', 'fresh generation populates controls');
  ok(_status.textContent.indexOf('ready') >= 0, 'fresh generation completes status');
  console.log('reset-new-generation-ok');
})();
"""
        self._run_js(scenario, "reset-new-generation-ok")

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


class CommittedPreferenceBytesTests(_FixtureMixin):
    def test_atomic_writer_returns_actual_platform_bytes(self):
        from utils import atomic_json_write
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "preference.json"
            document = {"language": "日本語", "value": "a\nb"}
            self.assertIsNone(atomic_json_write(document, path))
            data = atomic_json_write(document, path, newline="\r\n", return_written_bytes=True)
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(document, json.loads(data))
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_successful_commit_needs_no_fallible_destination_reread(self):
        from routers.editor import ReadaloudEpubRequest
        editor, _manager = self._fixture(self._mkdtmp())
        real_read = editor._read_bounded_preference_bytes
        real_save = editor._save_readaloud_timing_preference
        saved = False
        def save(preference):
            nonlocal saved
            data = real_save(preference)
            saved = True
            return data
        def read(path):
            if saved:
                raise OSError("mutable destination cannot be reopened")
            return real_read(path)
        tasks = _CaptureTasks()
        with patch.object(editor, "_save_readaloud_timing_preference", side_effect=save), \
                patch.object(editor, "_read_bounded_preference_bytes", side_effect=read):
            response = editor._start_readaloud_epub(tasks, ReadaloudEpubRequest(timing_mode="chunk"))
        self.assertTrue(saved)
        self.assertEqual("started", response["status"])
        self.assertEqual(1, len(tasks.registered))


class ResultBudgetTests(unittest.TestCase):
    def test_impossible_score_geometry_refuses_before_conversion(self):
        from qwen_score_decoder import decode_timestamp_bins
        class Impossible:
            shape = (8192, 1953)
            def __array__(self, *args, **kwargs):
                raise AssertionError("impossible geometry must not allocate an array")
        with self.assertRaises(ValueError):
            decode_timestamp_bins(Impossible())
        with self.assertRaises(ValueError):
            decode_timestamp_bins([[10**1000, 0], [0, 1]])

    def test_valid_sentence_at_call_cap_does_not_require_unavailable_tail(self):
        import wave
        import qwen_align as qa
        import qwen_align_worker as worker
        text = " ".join(["word"] * 179) + ". Last word."
        spans = qa.cover_ranges(text, qa.split_sentences(text))
        plans = qa.get_aligner_windows(181, [dict(start=a, end=b,
            words=qa.count_units(text[a:b])) for a, b in spans], text=text)
        self.assertEqual(180, plans[0]["end_sec"])
        windows = [dict(char_start=p["char_start"], char_end=p["char_end"],
            est_start_sec=p["start_sec"], est_end_sec=p["end_sec"]) for p in plans]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "row.wav"
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(1000)
                out.writeframes(b"\x00\x00" * 181000)
            def measured(_model, _path, piece, _language, _decode, *args):
                words = piece.split()
                return [dict(text=w, start_time=float(i), end_time=float(i + 1))
                        for i, w in enumerate(words)]
            with patch.object(worker, "_align_slice", side_effect=measured) as align:
                tokens, accepted = worker._align_row(object(), dict(text=text,
                    wav_path=str(path), duration_sec=181, windows=windows), "English", None, root)
        self.assertEqual(2, align.call_count)
        self.assertEqual(181, len(tokens))
        self.assertEqual(179, tokens[178]["end_time"])
        self.assertEqual(179000, accepted[1]["start_frame"])
        self.assertEqual(181, tokens[-1]["end_time"])
        self.assertTrue(all(w["slice_duration_sec"] <= 180 for w in accepted))

    def test_row_byte_accounting_matches_complete_json(self):
        import qwen_align_worker as worker
        import itertools
        alphabet = ["", "a", "é", "\n", "\"", "界"]
        for pieces in itertools.product(alphabet, repeat=2):
            doc = {"ok": True, "tokens": {}, "windows": {}, "provider": {"device": "cpu"}}
            size = len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
            for i, text in enumerate(pieces):
                key = "r%d" % i
                tokens = [{"text": text, "start_time": 0.01, "end_time": 0.3}]
                windows = [{"tokens": tokens, "offset_sec": i / 3}]
                size += worker._get_result_row_bytes(key, tokens, windows, bool(doc["tokens"]))
                doc["tokens"][key] = tokens
                doc["windows"][key] = windows
                self.assertEqual(len(json.dumps(doc, ensure_ascii=False).encode("utf-8")), size)

    def test_worker_stops_when_actual_output_budget_is_exhausted(self):
        import importlib.metadata
        import types
        import wave
        import qwen_align_worker as worker
        import qwen_score_decoder  # Import before the sys.modules dependency boundary.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "model"
            model.mkdir()
            wav = root / "row.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(b"\0\0" * 16000)
            rows = [{"id": "r%d" % i, "text": "Hello.", "wav_path": str(wav),
                     "duration_sec": 1.0, "windows": [{"char_start": 0, "char_end": 6,
                         "est_start_sec": 0.0, "est_end_sec": 1.0}]} for i in range(3)]
            scratch = root / "slices"
            sentinel = scratch / "parent-owned.txt"
            real_mkdtemp = tempfile.mkdtemp
            job = root / "job.json"
            job.write_text(json.dumps({"model_dir": str(model), "device": "cpu",
                "dtype": "float32", "language": "English", "qwen_asr_version": "0.0.6",
                "rows": rows, "slice_dir": str(scratch)}), encoding="utf-8")
            result = root / "result.json"
            torch = types.SimpleNamespace(float32="float32", float16="float16", bfloat16="bfloat16")
            qwen = types.SimpleNamespace(Qwen3ForcedAligner=types.SimpleNamespace(
                from_pretrained=lambda *a, **kw: object()))
            for case, text, limit, expected_exit, expected_calls in [
                    ("budget", "x" * 2000, 512, 1, 1), ("success", "Hello", 4096, 0, 3),
                    ("row-error", None, 4096, 1, 1), ("adapter-error", "Hello", 4096, 1, 0),
                    ("scratch-error", "Hello", 4096, 1, 0)]:
                with self.subTest(case=case):
                    scratch.mkdir(exist_ok=True)
                    sentinel.write_text("owned-by-parent", encoding="utf-8")
                    tokens = [{"text": text, "start_time": 0.1, "end_time": 0.8}]
                    windows = [{"tokens": tokens, "char_start": 0, "char_end": 6,
                        "start_frame": 0, "end_frame": 16000, "offset_sec": 0.0,
                        "slice_duration_sec": 1.0, "calls": 1}]
                    with patch.dict(sys.modules, {"torch": torch, "qwen_asr": qwen}), \
                            patch.object(importlib.metadata, "version", return_value="0.0.6"), \
                            patch.object(worker, "_apply_encoder_mask_adapter", return_value="sdpa",
                                side_effect=TypeError("precise adapter cause") if case == "adapter-error" else None), \
                            patch.object(worker.tempfile, "mkdtemp", wraps=real_mkdtemp,
                                side_effect=OSError("precise scratch cause") if case == "scratch-error" else None), \
                            patch.object(worker, "_align_row", return_value=(tokens, windows),
                                side_effect=ValueError("precise decoder cause") if text is None else None) as align, \
                            patch.object(worker, "MAX_RESULT_BYTES", limit):
                        code = worker.main(["worker", str(job), str(result)])
                    self.assertEqual("owned-by-parent", sentinel.read_text(encoding="utf-8"))
                    for call in align.call_args_list:
                        self.assertEqual(scratch, call.args[-1].parent)
                        self.assertFalse(call.args[-1].exists(), "Only the worker-created scratch child is reclaimed")
                    self.assertEqual(expected_exit, code, result.read_text())
                    self.assertEqual(expected_calls, align.call_count)
                    self.assertLessEqual(result.stat().st_size, limit)
                    self.assertEqual(expected_exit == 0, json.loads(result.read_text())["ok"])
                    if text is None:
                        self.assertIn("precise decoder cause", json.loads(result.read_text())["error"])

                    if case in ("adapter-error", "scratch-error"):
                        self.assertIn("precise " + case.split("-")[0] + " cause", json.loads(result.read_text())["error"])

    def test_result_writer_keeps_the_existing_byte_cap(self):
        import qwen_align_worker as worker
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            with patch.object(worker, "MAX_RESULT_BYTES", 512):
                self.assertTrue(worker._write_result(path, {"ok": True, "text": "small"}))
                self.assertFalse(worker._write_result(path, {"ok": True, "text": "x" * 2000}))
            data = path.read_bytes()
            self.assertLessEqual(len(data), 512)
            self.assertFalse(json.loads(data)["ok"])
            self.assertIn("size limit", json.loads(data)["error"])


if __name__ == "__main__":
    unittest.main()
