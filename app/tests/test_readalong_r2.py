"""R2 bounded repair slice (#1720/#1779): task ownership + download currentness.

Real-handler tests: real editor endpoints (direct _start_* calls through the
real scheduler claim path with a capturing BackgroundTasks), real production
builders for artifacts, real route callables for downloads. Fake builders
are injected only at the builder seam to control worker timing; assertions
concern scheduler/claim/state behavior, never a stub's return value.

RED before R2 implementation; GREEN after.
"""
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydub import AudioSegment

from project import ProjectManager


def _clip(tmp, name, frames=4800):
    data = b"\x00\x20" * frames
    seg = AudioSegment(data=data, sample_width=2, frame_rate=24000, channels=1)
    with seg.export(str(Path(tmp) / name), format="wav"):
        pass


def _setup_book(tmp, texts=("Hello world.", "Second line.")):
    chunks = []
    for i, text in enumerate(texts):
        name = f"clip{i}.wav"
        _clip(tmp, name)
        chunks.append({"id": i, "uid": f"u{i}", "speaker": "Narrator",
                       "text": text, "audio_path": name,
                       "audio_revision": f"r{i}", "status": "done"})
    (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    book = Path(tmp) / "book.txt"
    book.write_bytes((" ".join(texts)).encode("utf-8"))
    (Path(tmp) / "state.json").write_text(
        json.dumps({"input_file_path": str(book), "active_book_id": "b1",
                    "book_generation": 1}), encoding="utf-8")
    (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
    return chunks


class _CaptureTasks:
    """BackgroundTasks stand-in recording registrations without running."""

    def __init__(self):
        self.registered = []

    def add_task(self, func, *args, **kwargs):
        self.registered.append((func, args, kwargs))


class _FixtureMixin:
    def _mkdtmp(self):
        tmp = tempfile.mkdtemp(prefix="r2-")
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp

    def _release_claims(self):
        import core
        for name in ("readalong_sidecar", "readaloud_epub"):
            if name in core._task_claims:
                try:
                    core.release_gpu_task_claim(name)
                except Exception:
                    core._task_claims.pop(name, None)

    def _fixture(self, tmp, epub=False):
        from routers import editor
        import core
        if epub:
            from tests.test_readalong_r4 import _setup_epub_book
            texts = ["Chapter One", "Hello synthetic world.", "Second paragraph here.",
                     "Quoted line here.", "First list item."]
            _setup_epub_book(tmp, texts)
        else:
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
        for name in ("readalong_sidecar", "readaloud_epub"):
            editor.process_state[name].update({"running": False, "cancel": False,
                                               "result": None, "logs": []})
        self._release_claims()
        self.addCleanup(self._release_claims)
        return editor, manager

    def _build_sidecar(self, tmp, manager):
        from readalong_export import build_sidecar_export
        ok, msg = build_sidecar_export(manager, lambda: False)
        self.assertTrue(ok, msg)


class ReadalongR2OwnershipTests(_FixtureMixin, unittest.TestCase):
    def test_start_returns_owner_identity_and_duplicate_refused(self):
        from fastapi import HTTPException
        editor, _manager = self._fixture(self._mkdtmp())
        import readalong_export

        def fake_build(*a, **k):
            return (True, "fake done")

        with patch.object(readalong_export, "build_sidecar_export",
                         side_effect=fake_build):
            tasks = _CaptureTasks()
            from routers.editor import ReadalongSidecarRequest
            response = editor._start_readalong_sidecar(
                tasks, ReadalongSidecarRequest(include_text=False))
            self.assertEqual("started", response["status"])
            self.assertEqual("readalong_sidecar", response["task_name"])
            self.assertIn("claim_id", response)
            self.assertRegex(response["claim_id"], r"^[0-9a-f]{32}$")
            self.assertIn("book_token", response)
            self.assertRegex(response["book_token"], r"^[0-9a-f]{64}$")
            state = editor.process_state["readalong_sidecar"]
            self.assertIsNone(state["result"])
            self.assertEqual(response["claim_id"], state.get("task_claim_id"))
            self.assertEqual(response["book_token"], state.get("readalong_book_token"))
            with self.assertRaises(HTTPException) as ctx:
                editor._start_readalong_sidecar(
                    _CaptureTasks(), ReadalongSidecarRequest(include_text=False))
            self.assertIn(ctx.exception.status_code, (400, 409))
            func, args, kwargs = tasks.registered[0]
            func(*args, **kwargs)
            self.assertEqual("done", state["result"]["status"])
            self.assertFalse(state["running"])

    def test_eager_worker_completion_not_cleared_by_start(self):
        """A worker completing during registration must not lose its result
        to a result=None assigned after scheduling."""
        editor, _manager = self._fixture(self._mkdtmp())
        import readalong_export

        def fake_build(*a, **k):
            return (True, "instant")

        class _EagerTasks:
            def __init__(self):
                self.registered = []

            def add_task(self, func, *args, **kwargs):
                func(*args, **kwargs)

        with patch.object(readalong_export, "build_sidecar_export",
                         side_effect=fake_build):
            from routers.editor import ReadalongSidecarRequest
            editor._start_readalong_sidecar(
                _EagerTasks(), ReadalongSidecarRequest(include_text=False))
        state = editor.process_state["readalong_sidecar"]
        self.assertIsNotNone(state["result"],
                             "terminal result was cleared after the worker finished")
        self.assertEqual("done", state["result"]["status"])

    def test_stale_worker_cannot_overwrite_newer_owner(self):
        editor, _manager = self._fixture(self._mkdtmp())
        import readalong_export
        counter = {"n": 0}

        def fake_build(*a, **k):
            counter["n"] += 1
            return (True, f"done-{counter['n']}")

        with patch.object(readalong_export, "build_sidecar_export",
                         side_effect=fake_build):
            from routers.editor import ReadalongSidecarRequest
            tasks1 = _CaptureTasks()
            editor._start_readalong_sidecar(
                tasks1, ReadalongSidecarRequest(include_text=False))
            func1, args1, kwargs1 = tasks1.registered[0]
            func1(*args1, **kwargs1)
            self.assertEqual("done-1",
                             editor.process_state["readalong_sidecar"]["result"]["message"])
            tasks2 = _CaptureTasks()
            editor._start_readalong_sidecar(
                tasks2, ReadalongSidecarRequest(include_text=False))
            func2, args2, kwargs2 = tasks2.registered[0]
            func2(*args2, **kwargs2)
            self.assertEqual("done-2",
                             editor.process_state["readalong_sidecar"]["result"]["message"])
            state = editor.process_state["readalong_sidecar"]
            before = dict(state["result"])
            func1(*args1, **kwargs1)  # stale replay
            self.assertEqual(before, state["result"],
                             "stale worker overwrote the newer owner's result")

    def test_cancel_requires_matching_owner(self):
        from fastapi import HTTPException
        editor, _manager = self._fixture(self._mkdtmp())
        import readalong_export

        def fake_build(manager, cancel_check, *a, **k):
            # Mirror the production builder contract: honor cancellation.
            if cancel_check():
                from tts import ExportCancelled
                raise ExportCancelled("Export cancelled")
            return (True, "done")

        with patch.object(readalong_export, "build_sidecar_export",
                         side_effect=fake_build):
            from routers.editor import ReadalongSidecarRequest
            tasks = _CaptureTasks()
            response = editor._start_readalong_sidecar(
                tasks, ReadalongSidecarRequest(include_text=False))
            with self.assertRaises(HTTPException):
                editor._cancel_cpu_export(
                    "readalong_sidecar", "none",
                    expected_claim_id="0" * 32,
                    expected_book_token=response["book_token"])
            self.assertFalse(editor.process_state["readalong_sidecar"]["cancel"])
            result = editor._cancel_cpu_export(
                "readalong_sidecar", "none",
                expected_claim_id=response["claim_id"],
                expected_book_token=response["book_token"])
            self.assertEqual("cancelling", result["status"])
            self.assertTrue(editor.process_state["readalong_sidecar"]["cancel"])
            func, args, kwargs = tasks.registered[0]
            func(*args, **kwargs)
            self.assertEqual("cancelled",
                             editor.process_state["readalong_sidecar"]["result"]["status"])

    def test_admitted_snapshot_change_before_worker_fails_closed(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        import readalong_export
        called = []

        def fake_build(*a, **k):
            called.append(True)
            return (True, "done")

        with patch.object(readalong_export, "build_sidecar_export",
                         side_effect=fake_build):
            from routers.editor import ReadalongSidecarRequest
            tasks = _CaptureTasks()
            editor._start_readalong_sidecar(
                tasks, ReadalongSidecarRequest(include_text=False))
            chunks_path = Path(tmp) / "chunks.json"
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            chunks[0]["text"] = "Changed after admission."
            chunks_path.write_text(json.dumps(chunks), encoding="utf-8")
            func, args, kwargs = tasks.registered[0]
            func(*args, **kwargs)
            self.assertEqual([], called, "builder ran despite snapshot change")
            result = editor.process_state["readalong_sidecar"]["result"]
            self.assertEqual("failed", result["status"])
            self.assertIn("changed after admission", result["message"])


class ReadalongR2DownloadTests(_FixtureMixin, unittest.TestCase):
    def test_sidecar_download_serves_admitted_bytes_and_rejects_tampering(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, manager = self._fixture(tmp)
        self._build_sidecar(tmp, manager)
        response = asyncio.run(editor.download_readalong_sidecar())
        sidecar_bytes = (Path(tmp) / "readalong.sidecar.json").read_bytes()
        self.assertEqual(sidecar_bytes, getattr(response, "body", None),
                         "download must serve exactly the validated bytes")
        mp3 = Path(tmp) / "cloned_audiobook.mp3"
        mp3.write_bytes(b"tampered audio bytes")
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(editor.download_readalong_sidecar())
        self.assertEqual(409, ctx.exception.status_code)

    def test_sidecar_download_rejects_source_rows_and_config_changes(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, manager = self._fixture(tmp)
        self._build_sidecar(tmp, manager)
        self.assertIsNotNone(asyncio.run(editor.download_readalong_sidecar()))
        book = Path(tmp) / "book.txt"
        original = book.read_bytes()
        book.write_bytes(original + b" extra")
        with self.assertRaises(HTTPException):
            asyncio.run(editor.download_readalong_sidecar())
        book.write_bytes(original)
        asyncio.run(editor.download_readalong_sidecar())
        chunks_path = Path(tmp) / "chunks.json"
        saved = chunks_path.read_bytes()
        chunks = json.loads(saved)
        chunks[0]["text"] = "Edited line text here."
        chunks_path.write_text(json.dumps(chunks), encoding="utf-8")
        with self.assertRaises(HTTPException):
            asyncio.run(editor.download_readalong_sidecar())
        chunks_path.write_bytes(saved)
        asyncio.run(editor.download_readalong_sidecar())
        config_path = manager.config_path
        cfg = Path(config_path).read_bytes() if os.path.exists(config_path) else None
        Path(config_path).write_bytes((cfg or b"{}") + b"\n# tamper")
        with self.assertRaises(HTTPException):
            asyncio.run(editor.download_readalong_sidecar())
        if cfg is not None:
            Path(config_path).write_bytes(cfg)

    def test_downloads_reject_take_and_book_switches_over_http(self):
        import httpx
        from fastapi import FastAPI
        from readalong_export import build_epub_export

        for epub in (False, True):
            with self.subTest(epub=epub):
                tmp = self._mkdtmp()
                editor, manager = self._fixture(tmp, epub=epub)
                if epub:
                    ok, message = build_epub_export(manager, lambda: False)
                    self.assertTrue(ok, message)
                else:
                    self._build_sidecar(tmp, manager)
                app = FastAPI()
                app.include_router(editor.router)
                endpoint = "/api/readaloud_epub" if epub else "/api/readalong/sidecar"

                async def check():
                    async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                            base_url="http://fixture") as client:
                        original = await client.get(endpoint)
                        self.assertEqual(200, original.status_code, original.text)
                        rows_path = Path(tmp) / "chunks.json"
                        rows_bytes = rows_path.read_bytes()
                        rows = json.loads(rows_bytes)
                        rows[0]["audio_revision"] = "replacement-take"
                        rows_path.write_text(json.dumps(rows), encoding="utf-8")
                        changed_take = await client.get(endpoint)
                        self.assertEqual(409, changed_take.status_code, changed_take.text)
                        rows_path.write_bytes(rows_bytes)
                        restored = await client.get(endpoint)
                        self.assertEqual(200, restored.status_code, restored.text)
                        self.assertEqual(original.content, restored.content)
                        state_path = Path(tmp) / "state.json"
                        state = json.loads(state_path.read_bytes())
                        state["active_book_id"] = "different-book"
                        state["book_generation"] = state.get("book_generation", 0) + 1
                        state_path.write_text(json.dumps(state), encoding="utf-8")
                        changed_book = await client.get(endpoint)
                        self.assertEqual(409, changed_book.status_code, changed_book.text)

                asyncio.run(check())

    def test_unreadable_download_settings_return_rebuild_response(self):
        import httpx
        from fastapi import FastAPI
        from readalong_export import build_epub_export

        for epub in (False, True):
            with self.subTest(epub=epub):
                tmp = self._mkdtmp()
                editor, manager = self._fixture(tmp, epub=epub)
                config_path = Path(manager.config_path)
                if not config_path.exists():
                    config_path.write_text("{}", encoding="utf-8")
                if epub:
                    ok, message = build_epub_export(manager, lambda: False)
                    self.assertTrue(ok, message)
                else:
                    self._build_sidecar(tmp, manager)
                app = FastAPI()
                app.include_router(editor.router)
                endpoint = "/api/readaloud_epub" if epub else "/api/readalong/sidecar"
                read_bytes = Path.read_bytes

                def deny_config(path):
                    if path == config_path:
                        raise PermissionError("Synthetic unreadable config")
                    return read_bytes(path)

                async def check():
                    async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                            base_url="http://fixture") as client:
                        original = await client.get(endpoint)
                        self.assertEqual(200, original.status_code, original.text)
                        with patch.object(Path, "read_bytes", deny_config):
                            refused = await client.get(endpoint)
                        self.assertEqual(409, refused.status_code, refused.text)
                        self.assertIn("settings", refused.json()["detail"].lower())
                        restored = await client.get(endpoint)
                        self.assertEqual(200, restored.status_code, restored.text)
                        self.assertEqual(original.content, restored.content)

                asyncio.run(check())

    def test_sidecar_download_rejects_legacy_unbound_artifact(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        from readalong_sidecar import build_readalong_sidecar
        from readalong_export import get_sidecar_output_path
        _doc, raw = build_readalong_sidecar(
            entries=[{"index": 0, "section": "sec-0001", "speaker": "Narrator",
                      "text": None, "start_frame": 0, "end_frame": 24000,
                      "pause_frames_after": 0, "status": "missing", "words": None}],
            binding={"audio_sha256": hashlib.sha256(b"x").hexdigest(),
                     "audio_bytes": 1, "sample_rate": 24000, "channels": 1,
                     "sample_width": 2, "frames": 48000},
            includes_text=False, source_receipt=None)
        Path(get_sidecar_output_path(tmp)).write_bytes(raw)
        with self.assertRaises(HTTPException):
            asyncio.run(editor.download_readalong_sidecar())

    def test_epub_download_requires_receipt_and_matching_audio(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, manager = self._fixture(tmp, epub=True)
        from readalong_export import build_epub_export
        ok, msg = build_epub_export(manager, lambda: False)
        self.assertTrue(ok, msg)
        response = asyncio.run(editor.download_readaloud_epub())
        epub_bytes = (Path(tmp) / "readaloud.epub").read_bytes()
        self.assertEqual(epub_bytes, getattr(response, "body", None),
                         "download must serve exactly the validated bytes")
        receipt_path = Path(tmp) / "readaloud.receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["audio_sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(HTTPException):
            asyncio.run(editor.download_readaloud_epub())

    def test_pair_audio_serves_exact_receipt_bound_mp3(self):
        tmp = self._mkdtmp()
        editor, manager = self._fixture(tmp)
        self._build_sidecar(tmp, manager)
        response = asyncio.run(editor.download_readalong_sidecar_audio())
        mp3_bytes = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()

        async def collect(resp):
            out = bytearray()
            async for chunk in resp.body_iterator:
                out.extend(chunk)
            return bytes(out)

        collected = asyncio.run(collect(response))
        self.assertEqual(mp3_bytes, collected)


class ReadalongR2RouteTests(unittest.TestCase):
    """Real HTTP through registered route handlers (ASGITransport)."""

    def test_start_status_cancel_over_http(self):
        import httpx
        from fastapi import FastAPI
        from routers import editor as editor_module
        from routers import script as script_module
        import readalong_export

        tmp = tempfile.mkdtemp(prefix="r2route-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _setup_book(tmp)
        manager = ProjectManager(tmp)
        app = FastAPI()
        app.include_router(editor_module.router)
        app.include_router(script_module.router)
        patches = [
            patch.object(editor_module, "DATA_DIR", tmp),
            patch.object(editor_module, "SCRIPT_PATH", str(Path(tmp) / "annotated_script.json")),
            patch.object(editor_module, "AUDIOBOOK_PATH", str(Path(tmp) / "cloned_audiobook.mp3")),
            patch.object(editor_module, "project_manager", manager),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        import core
        blocker = core.reserve_background_task("readalong_sidecar", cpu_only=True)
        self.addCleanup(lambda: core.release_gpu_task_claim("readalong_sidecar", blocker))

        def fake_build(mgr, cancel_check, *a, **k):
            if cancel_check():
                from tts import ExportCancelled
                raise ExportCancelled("Export cancelled")
            return (True, "http done")

        async def run():
            with patch.object(readalong_export, "build_sidecar_export", side_effect=fake_build):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                        base_url="http://fixture") as client:
                    # A claim already owned blocks a new start (duplicate).
                    blocked = await client.post("/api/readalong/sidecar", json={"include_text": False})
                    self.assertIn(blocked.status_code, (400, 409), blocked.text)
                    core.release_gpu_task_claim("readalong_sidecar", blocker)
                    r = await client.post("/api/readalong/sidecar", json={"include_text": False})
                    self.assertEqual(200, r.status_code, r.text)
                    body = r.json()
                    self.assertEqual("started", body["status"])
                    self.assertEqual("readalong_sidecar", body["task_name"])
                    claim = body["claim_id"]
                    book = body["book_token"]
                    # First worker already completed in-request (background is
                    # awaited), so a fresh start is a new build, not a dup.
                    r2 = await client.post("/api/readalong/sidecar", json={"include_text": False})
                    self.assertEqual(200, r2.status_code, r2.text)
                    body = r2.json()
                    claim = body["claim_id"]
                    book = body["book_token"]
                    # Status exposes the same owner identity.
                    s = await client.get("/api/status/readalong_sidecar")
                    self.assertEqual(200, s.status_code)
                    stored = s.json()
                    self.assertEqual(claim, stored["task_claim_id"])
                    self.assertEqual(book, stored["readalong_book_token"])
                    # The worker completed in-request; cancel now reports the
                    # exact not-running state (owner cancel is covered by the
                    # direct scheduler test while a build is live).
                    wrong = await client.post("/api/readalong/sidecar/cancel", json={
                        "claim_id": "0" * 32, "book_token": book})
                    self.assertEqual(400, wrong.status_code, wrong.text)

        asyncio.run(run())


class ReadalongR2JsTests(unittest.TestCase):
    """Stateful Node harness driving the ACTUAL app-readalong.js handlers
    (not the portable player). Covers late terminal results, bounded
    poll/error budgets, duplicate-start prevention, owner-fenced cancel,
    and reset invalidation. No browser/media claims.
    """

    def _run_node(self, script_body):
        """script_body is an async function body (await _drain() between steps)."""
        import subprocess
        js = (Path(__file__).parent.parent / "static" / "js"
              / "app-readalong.js").read_text(encoding="utf-8")
        runner = (_JS_STUBS + "\n" + js + "\nasync function _scenario() {\n"
                  + script_body + "\n}\n_scenario().then(function () {}, function (e) {"
                  " console.error('FAIL scenario: ' + (e && e.stack || e)); process.exit(1); });\n")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(runner)
            tmp.flush()
            res = subprocess.run(["node", tmp.name], capture_output=True,
                                 text=True, timeout=60)
        self.assertEqual(0, res.returncode,
                         f"node failed:\n{res.stdout}\n{res.stderr}")

    def test_late_done_is_handled_and_downloads(self):
        # Poll "running" 12 times (past the old 5-minute gate) then a late
        # typed done must still complete and trigger exactly one download.
        self._run_node(r"""
            var polls = 0;
            API._queue = [{ running: true, logs: ['Working...'] }];
            API.get = function (url) {
              if (url.indexOf('/api/status/') === 0) {
                polls++;
                if (polls > 12) {
                  return Promise.resolve({ running: false, result: { status: 'done', message: 'ok' },
                    task_claim_id: _started.claim_id, readalong_book_token: _started.book_token });
                }
                return Promise.resolve({ running: true, logs: ['Working...'],
                  task_claim_id: _started.claim_id, readalong_book_token: _started.book_token });
              }
              return Promise.resolve({});
            };
            window.ensureReadalongIntegrityApproval = function () { return Promise.resolve('a'.repeat(64)); };
            var _started = { status: 'started', claim_id: 'b'.repeat(32), book_token: 'c'.repeat(64) };
            API.post = function (url) { return Promise.resolve(_started); };
            window.buildReadalongSidecar();
            await _drain();
            ok(polls >= 13, 'late done was not polled, polls=' + polls);
            ok(document.getElementById('readalong-status').textContent.indexOf('ready') !== -1,
              'late done not surfaced: ' + document.getElementById('readalong-status').textContent);
            ok(_clicks.length === 1, 'expected exactly one download, got ' + _clicks.length);
            ok(_clicks[0].href === '/api/readalong/sidecar', 'wrong download href');
            console.log('r2-js-late-done-ok');
        """)

    def test_malformed_terminal_budget_stops_polling(self):
        self._run_node(r"""
            API.get = function () {
              return Promise.resolve({ running: false, result: null, logs: ['?'] });
            };
            window.ensureReadalongIntegrityApproval = function () { return Promise.resolve('a'.repeat(64)); };
            API.post = function () {
              return Promise.resolve({ status: 'started', claim_id: 'b'.repeat(32), book_token: 'c'.repeat(64) });
            };
            window.buildReadalongSidecar();
            await _drain();
            ok(document.getElementById('readalong-status').textContent.indexOf('No usable result') !== -1,
              'malformed terminal never surfaced a bounded stop');
            ok(document.getElementById('readalong-build-btn').disabled === false,
              'build control stayed disabled after bounded stop');
            console.log('r2-js-malformed-ok');
        """)

    def test_network_error_budget_stops_polling(self):
        self._run_node(r"""
            API.get = function () { return Promise.reject(new Error('network down')); };
            window.ensureReadalongIntegrityApproval = function () { return Promise.resolve('a'.repeat(64)); };
            API.post = function () {
              return Promise.resolve({ status: 'started', claim_id: 'b'.repeat(32), book_token: 'c'.repeat(64) });
            };
            window.buildReadalongSidecar();
            await _drain();
            ok(document.getElementById('readalong-status').textContent.indexOf('Could not check') !== -1,
              'network errors never surfaced a bounded stop');
            console.log('r2-js-network-ok');
        """)

    def test_duplicate_start_and_reset_fencing(self):
        self._run_node(r"""
            var posts = 0;
            API.post = function (url) {
              posts++;
              return new Promise(function () {}); // never resolves: build is in flight
            };
            API.get = function () { return Promise.resolve({ running: true, logs: ['Working'] }); };
            window.ensureReadalongIntegrityApproval = function () { return new Promise(function (res) { _approvals.push(res); }); };
            window.buildReadalongSidecar();
            window.buildReadalongSidecar();
            await _drain();
            ok(posts === 0, 'duplicate start issued a second POST before approval resolved');
            ok(document.getElementById('readalong-build-btn').disabled === true,
              'build control not disabled while busy');
            // Reset (book switch) must fence the in-flight approval promise.
            window.resetReadalongViews();
            _approvals.forEach(function (res) { res('a'.repeat(64)); });
            await _drain();
            ok(posts === 0, 'reset late promise still posted a start for the new book');
            console.log('r2-js-reset-ok');
        """)

    def test_cancel_sends_exact_owner(self):
        self._run_node(r"""
            var cancelBody = null;
            API.post = function (url, body) {
              if (url.indexOf('cancel') !== -1) { cancelBody = body; return Promise.resolve({ status: 'cancelling' }); }
              return Promise.resolve({ status: 'started', claim_id: 'b'.repeat(32), book_token: 'c'.repeat(64) });
            };
            API.get = function () {
              return Promise.resolve({ running: true, logs: ['Working'],
                task_claim_id: 'b'.repeat(32), readalong_book_token: 'c'.repeat(64) });
            };
            window.ensureReadalongIntegrityApproval = function () { return Promise.resolve('a'.repeat(64)); };
            window.buildReadalongSidecar();
            await _drain();
            window.cancelReadalongSidecar();
            await _drain();
            ok(cancelBody && cancelBody.claim_id === 'b'.repeat(32)
              && cancelBody.book_token === 'c'.repeat(64),
              'cancel did not send the exact owner identity');
            console.log('r2-js-cancel-owner-ok');
        """)

    def test_app_html_wiring_and_reset_hooks(self):
        # Structural wiring check (labelled as such): real IDs + reset hooks.
        index = Path(__file__).parent.parent / "static" / "index.html"
        html = index.read_text(encoding="utf-8")
        for token in ('id="readalong-status"', 'id="readaloud-status"',
                      'id="readalong-build-btn"', 'id="readaloud-build-btn"',
                      'id="readalong-cancel-btn"', 'id="readaloud-cancel-btn"',
                      '/api/readalong/sidecar/audio', '/api/readaloud_epub/audio',
                      'app-readalong.js'):
            self.assertIn(token, html, f"index.html missing {token}")
        self.assertNotIn("Build the sidecar after Merge All", html)
        core_js = (Path(__file__).parent.parent / "static" / "js"
                   / "app-core.js").read_text(encoding="utf-8")
        self.assertEqual(3, core_js.count("window.resetReadalongViews?.()"),
                         "reset invalidation must be wired at all three book paths")
        self.assertIn("window.ensureReadalongIntegrityApproval", core_js)


_JS_STUBS = r"""
'use strict';
var _realSetTimeout = require('timers').setTimeout;
var _pendingTimers = [];
var setTimeout = function (fn, ms) { _pendingTimers.push(fn); return _pendingTimers.length; };
var clearTimeout = function (id) { _pendingTimers.splice(0); };
function _flushAll() {
  var guard = 0;
  while (_pendingTimers.length && guard < 5000) {
    var fns = _pendingTimers.splice(0);
    guard++;
    for (var i = 0; i < fns.length; i++) { fns[i](); }
  }
}
function _waitMicro() {
  return new Promise(function (r) { _realSetTimeout(r, 0); });
}
// Run queued fake timers, awaiting microtasks between rounds, until idle.
async function _drain() {
  for (var i = 0; i < 200; i++) {
    _flushAll();
    await _waitMicro();
    if (!_pendingTimers.length) { break; }
  }
  await _waitMicro();
}
var _clicks = [];
var _approvals = [];
var _els = {};
var _createdCounter = 0;
function _el(id) {
  if (_els[id]) { return _els[id]; }
  var el = { id: id, children: [], style: {}, disabled: false, textContent: '',
    innerHTML: '', checked: false, href: '',
    classList: { toggle: function(){}, add: function(){}, remove: function(){} },
    setAttribute: function(){}, appendChild: function (c) { this.children.push(c); },
    addEventListener: function(){}, querySelectorAll: function () { return []; } };
  _els[id] = el;
  return el;
}
var document = {
  getElementById: _el,
  createElement: function (tag) {
    var e = _el('_created_' + (_createdCounter++));
    e.tag = tag;
    e.click = function () { _clicks.push(e); };
    return e;
  },
};
var window = {};
var API = {
  get: function () { return Promise.resolve({}); },
  post: function () { return Promise.resolve({}); },
};
var ok = function (cond, msg) { if (!cond) { console.error('FAIL: ' + msg); process.exit(1); } };
"""


if __name__ == "__main__":
    unittest.main()
