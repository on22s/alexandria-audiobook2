import ast
import copy
from contextlib import ExitStack
import core
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

import app as app_module
import routers.editor as editor_router
import routers.preparer as preparer_router
import routers.dataset_builder as dataset_builder_router
import update_api_contract_snapshots as api_contract


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        # Admission tests reserve real kernel task leases but never run workers.
        # Keep their resource namespace and simulated shared state disposable.
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        snapshots = {name: copy.deepcopy(state) for name, state in core.process_state.items()}
        def restore_state():
            for name, saved in snapshots.items():
                core.process_state[name].clear()
                core.process_state[name].update(saved)
        self.addCleanup(restore_state)
        for state in core.process_state.values():
            state['running'] = False
        self.stack.enter_context(patch.object(core, 'DATA_DIR', self.root.name))
        self.stack.enter_context(patch.object(core, '_task_claims', {}))
        self.stack.enter_context(patch.object(core, '_gpu_leases', {}))
        self.stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
        acquire = core.acquire_task_lease
        def acquire_owned(*args, **kwargs):
            lease = acquire(*args, **kwargs)
            self.addCleanup(lease.close)
            return lease
        self.stack.enter_context(patch.object(core, 'acquire_task_lease', side_effect=acquire_owned))

    def test_empty_preparer_batch_is_rejected_before_any_resource_or_background_work(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(preparer_router.router)
        with patch.object(preparer_router, "_resolve_preparer_interpreter", return_value="fixture-python") as resolve, \
             patch.object(preparer_router, "check_global_gpu_lock") as check, \
             patch.object(preparer_router, "check_disk_space", return_value=(True, 10)) as disk, \
             patch.object(core, "claim_gpu_task", wraps=core.claim_gpu_task) as claim, \
             patch.object(core, "_task_claims", {}), \
             patch.object(core, "_gpu_leases", {}), \
             patch.object(core, "acquire_gpu_lock", return_value=None), \
             patch.object(core, "llm_is_on_this_gpu", return_value=True), \
             patch.object(preparer_router, "_run_claimed_background_task") as run, \
             TestClient(app) as client:
            response = client.post("/api/preparer/batch/start", json={"tasks": []})
            self.assertEqual(422, response.status_code, response.text)
            self.assertTrue(any(error["loc"] == ["body", "tasks"] for error in response.json()["detail"]))
            for operation in (resolve, check, disk, claim, run):
                operation.assert_not_called()

    def test_nonempty_preparer_batch_still_claims_schedules_once_and_honors_gpu_conflict(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(preparer_router.router)
        request = {"tasks": [{"audio_filename": "clip.wav", "output_filename": "dataset.zip"}]}
        with patch.object(preparer_router, "_resolve_preparer_interpreter", return_value="fixture-python"), \
             patch.object(preparer_router, "check_global_gpu_lock") as check, \
             patch.object(preparer_router, "check_disk_space", return_value=(True, 10)), \
             patch.object(core, "claim_gpu_task", wraps=core.claim_gpu_task) as claim, \
             patch.object(core, "_task_claims", {}), \
             patch.object(core, "_gpu_leases", {}), \
             patch.object(core, "acquire_gpu_lock", return_value=None), \
             patch.object(core, "llm_is_on_this_gpu", return_value=True), \
             patch.object(preparer_router, "_run_claimed_background_task") as run, \
             TestClient(app) as client:
            response = client.post("/api/preparer/batch/start", json=request)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual({"status": "started", "task_count": 1}, response.json())
            check.assert_called_once_with("batch_preparer")
            claim.assert_called_once_with("batch_preparer")
            run.assert_called_once()
            self.assertEqual("batch_preparer", run.call_args.args[0])
            self.assertTrue(callable(run.call_args.args[1]))
            check.side_effect = HTTPException(status_code=400, detail="GPU task already running")
            claim.reset_mock()
            run.reset_mock()
            response = client.post("/api/preparer/batch/start", json=request)
            self.assertEqual(400, response.status_code)
            claim.assert_not_called()
            run.assert_not_called()

    def test_dataset_builder_thread_start_failure_releases_gpu_claim(self):
        import asyncio
        state = dataset_builder_router.process_state['dataset_builder']
        original = state['running'];state['running'] = False
        request = dataset_builder_router.DatasetBatchGenRequest(
            name='test', description='voice',
            samples=[dataset_builder_router.LoraDatasetSample(text='hello')])
        try:
            with tempfile.TemporaryDirectory() as root, \
                 patch.object(dataset_builder_router, 'DATASET_BUILDER_DIR', root), \
                 patch.object(dataset_builder_router, 'check_global_gpu_lock'), \
                 patch.object(core, '_task_claims', {}), \
                 patch.object(core, '_gpu_leases', {}), \
                 patch.object(core, 'acquire_gpu_lock', return_value=None), \
                 patch.object(core, 'llm_is_on_this_gpu', return_value=True), \
                 patch.object(core, 'release_gpu_task_claim', wraps=core.release_gpu_task_claim) as released, \
                 patch.object(dataset_builder_router.threading.Thread, 'start', side_effect=RuntimeError('thread unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'thread unavailable'):
                    asyncio.run(dataset_builder_router.dataset_builder_generate_batch(request))
                self.assertFalse(core.is_task_running('dataset_builder'))
                self.assertEqual({},core._task_claims)
            self.assertFalse(state['running']);self.assertEqual(1,released.call_count)
            self.assertEqual('dataset_builder',released.call_args.args[0]);self.assertIsInstance(released.call_args.args[1],str)
        finally:state['running'] = original

    def test_preparer_publication_failure_restores_previous_uploads(self):
        with tempfile.TemporaryDirectory() as root:
            staged = {}
            for name in ('audio.wav', 'source.txt'):
                final = Path(root, name)
                final.write_text('old ' + name)
                pending = Path(root, name + '.pending')
                pending.write_text('new ' + name)
                staged[str(final)] = str(pending)
            original_replace = preparer_router.os.replace

            def fail_second(source, target):
                if source.endswith('source.txt.pending'):
                    raise OSError('publish failed')
                return original_replace(source, target)

            with patch.object(preparer_router.os, 'replace', side_effect=fail_second):
                with self.assertRaisesRegex(OSError, 'publish failed'):
                    preparer_router.publish_preparer_uploads(
                        staged, lambda: self.fail('scheduled incomplete upload'))
            for final in staged:
                self.assertEqual('old ' + Path(final).name, Path(final).read_text())
            self.assertEqual(['audio.wav', 'source.txt'],
                             sorted(path.name for path in Path(root).iterdir()))

    def test_missing_contract_snapshot_is_a_clear_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "snapshots are missing or invalid"):
                api_contract.check_snapshots(app_module.app, tmp)

    def test_drift_check_claims_before_background_schedule(self):
        """A second request cannot pass while the first is still queued."""
        import asyncio

        state = editor_router.process_state["drift_check"]
        original_running = state["running"]
        state["running"] = False

        async def invoke():
            first_background = BackgroundTasks()
            request = editor_router.DriftCheckRequest(indices=[1])
            response = await editor_router.drift_check_endpoint(request, first_background)
            with self.assertRaises(HTTPException) as raised:
                await editor_router.drift_check_endpoint(request, BackgroundTasks())
            return response, first_background, raised.exception

        try:
            with patch.object(editor_router, "load_app_config", return_value={}), \
                    patch.object(editor_router.voice_drift, "get_drift_threshold", return_value=0.5), \
                    patch.object(editor_router.voice_drift, "get_speaker_model_python", return_value=None), \
                    patch.object(editor_router, "_load_voicelab_config", return_value={}):
                response, background, error = asyncio.run(invoke())
        finally:
            state["running"] = original_running

        self.assertEqual("started", response["status"])
        self.assertEqual(1, len(background.tasks))
        self.assertEqual(400, error.status_code)

    def test_preparer_download_rejects_output_directory(self):
        """Directory paths must not reach FileResponse as downloadable files."""
        import asyncio

        with tempfile.TemporaryDirectory() as output_dir:
            valid_file = Path(output_dir, "dataset.zip")
            valid_file.write_bytes(b"zip")
            Path(output_dir, "directory.zip").mkdir()
            with patch.object(preparer_router, "PREPARER_OUTPUT_DIR", output_dir):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(preparer_router.preparer_download("."))
                with self.assertRaises(HTTPException) as directory_error:
                    asyncio.run(preparer_router.preparer_download("directory.zip"))
                response = asyncio.run(preparer_router.preparer_download("dataset.zip"))

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("Only dataset ZIP", raised.exception.detail)
        self.assertEqual(404, directory_error.exception.status_code)
        self.assertEqual(str(valid_file), response.path)

    def test_preparer_rejects_source_and_audio_upload_name_collision_before_writing(self):
        import asyncio
        import io
        from unittest.mock import AsyncMock
        from starlette.datastructures import UploadFile as StarletteUpload
        with tempfile.TemporaryDirectory() as tmp:
            upload_path = Path(tmp, "book.wav")
            upload_path.write_bytes(b"existing audio")
            config = json.dumps({"audio_filename": "book.wav", "source_filename": "book.wav"})
            audio = StarletteUpload(io.BytesIO(b"audio"), filename="book.wav")
            source = StarletteUpload(io.BytesIO(b"source"), filename="book.wav")
            with patch.object(preparer_router, "UPLOADS_DIR", tmp), \
                 patch.object(preparer_router, "_resolve_preparer_interpreter", return_value="python"), \
                 patch.object(preparer_router, "check_global_gpu_lock"), \
                 patch.object(preparer_router, "check_disk_space", return_value=(True, 10)), \
                 patch.object(preparer_router, "claim_gpu_task") as claim, \
                 patch.object(preparer_router, "_save_upload_limited", new_callable=AsyncMock,
                              wraps=preparer_router._save_upload_limited) as save:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(preparer_router.preparer_start(
                        BackgroundTasks(), config_json=config, audio_file=audio, source_file=source))
            self.assertEqual(400, raised.exception.status_code)
            self.assertIn("different", raised.exception.detail)
            save.assert_not_called()
            claim.assert_not_called()
            self.assertEqual(b"existing audio", upload_path.read_bytes())
            self.assertEqual([upload_path], list(Path(tmp).iterdir()))

    def test_concurrent_same_name_preparer_uploads_do_not_overwrite_each_other(self):
        """Two overlapping starts with the same filename: one starts, one is
        refused, and the published upload holds the winner's bytes. Before
        stage-then-publish both wrote straight to UPLOADS_DIR/<name>."""
        import asyncio
        import io
        from starlette.datastructures import UploadFile as StarletteUpload

        state = preparer_router.process_state["preparer"]
        original_running = state["running"]
        state["running"] = False
        config = json.dumps({"audio_filename": "book.wav", "output_filename": "out.zip"})
        real_save = preparer_router._save_upload_limited

        async def slow_save(upload, path, max_bytes):
            await asyncio.sleep(0.05)   # let the other request's upload overlap
            await real_save(upload, path, max_bytes)

        async def start(payload):
            upload = StarletteUpload(io.BytesIO(payload), filename="book.wav")
            try:
                return payload, await preparer_router.preparer_start(
                    BackgroundTasks(), config_json=config, audio_file=upload, source_file=None)
            except HTTPException as exc:
                return payload, exc

        async def both():
            return await asyncio.gather(start(b"first"), start(b"second"))

        with tempfile.TemporaryDirectory() as uploads:
            with patch.object(preparer_router, "UPLOADS_DIR", uploads), \
                    patch.object(preparer_router, "_resolve_preparer_interpreter", return_value="/usr/bin/python3"), \
                    patch.object(preparer_router, "_save_upload_limited", slow_save):
                try:
                    results = asyncio.run(both())
                finally:
                    state["running"] = original_running
            started = [payload for payload, r in results if isinstance(r, dict)]
            refused = [r for _, r in results if isinstance(r, HTTPException)]
            self.assertEqual(1, len(started), results)
            self.assertEqual(1, len(refused), results)
            self.assertEqual(400, refused[0].status_code)
            self.assertEqual(["book.wav"], os.listdir(uploads), "no staged copies may remain")
            with open(os.path.join(uploads, "book.wav"), "rb") as fh:
                self.assertEqual(started[0], fh.read(),
                                 "the request that holds the task must own the published upload")

    def test_app_py_contains_no_http_route_decorators(self):
        app_path = Path(__file__).parent.parent.joinpath("app.py")
        tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
        http_decorators = {
            "api_route", "delete", "get", "head", "options", "patch", "post",
            "put", "trace", "websocket", "websocket_route",
        }
        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                target = decorator.func
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "app"
                        and target.attr in http_decorators):
                    violations.append((node.name, target.attr, node.lineno))

        self.assertEqual([], violations)

    def test_app_router_registration_order_is_stable(self):
        app_path = Path(__file__).parent.parent.joinpath("app.py")
        tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
        registered = []
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if (isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "app"
                    and call.func.attr == "include_router"
                    and call.args
                    and isinstance(call.args[0], ast.Name)):
                registered.append(call.args[0].id)

        self.assertEqual([
            "system_router",
            "script_router",
            "voices_router",
            "editor_router",
            "scripts_library_router",
            "voice_library_router",
            "voice_design_router",
            "lora_router",
            "dataset_builder_router",
            "preparer_router",
            "voicelab_router",
            "benchmark_router",
        ], registered)

    def test_api_routes_have_no_duplicate_method_path_pairs(self):
        seen = {}
        duplicates = []
        for route in app_module.app.routes:
            for method in sorted(getattr(route, "methods", []) or []):
                key = (method, route.path)
                if key in seen:
                    duplicates.append((key, seen[key], route.name))
                else:
                    seen[key] = route.name

        self.assertEqual([], duplicates)

    def test_openapi_operation_ids_are_unique(self):
        seen = {}
        duplicates = []
        for path, path_item in app_module.app.openapi()["paths"].items():
            for method, operation in path_item.items():
                if not isinstance(operation, dict) or "operationId" not in operation:
                    continue
                operation_id = operation["operationId"]
                if operation_id in seen:
                    duplicates.append((operation_id, seen[operation_id], (method, path)))
                else:
                    seen[operation_id] = (method, path)

        self.assertEqual([], duplicates)

    def test_api_contract_snapshots_match_and_regenerate_deterministically(self):
        expected_openapi = json.loads(api_contract.OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
        expected_routes = json.loads(api_contract.ROUTES_SNAPSHOT.read_text(encoding="utf-8"))
        differences = api_contract.compare_contracts(
            expected_openapi, expected_routes,
            app_module.app.openapi(), api_contract.get_route_manifest(app_module.app),
        )
        self.assertEqual([], differences, "API contract drift:\n" + "\n".join(differences))

        with tempfile.TemporaryDirectory() as tmp:
            openapi_path, routes_path = api_contract.write_snapshots(app_module.app, tmp)
            self.assertEqual(api_contract.OPENAPI_SNAPSHOT.read_bytes(), openapi_path.read_bytes())
            self.assertEqual(api_contract.ROUTES_SNAPSHOT.read_bytes(), routes_path.read_bytes())

    def test_api_contract_diff_summarizes_routes_operations_and_schemas(self):
        route = lambda path, name: {
            "index": 0, "methods": ["GET"], "name": name, "path": path, "type": "APIRoute"
        }
        expected_routes = [route("/a", "a"), route("/b", "b")]
        actual_routes = [route("/b", "b"), route("/a", "a")]
        expected_openapi = {
            "paths": {"/a": {"get": {"summary": "old"}}},
            "components": {"schemas": {
                "Thing": {"required": ["x"], "properties": {"x": {"type": "integer", "minimum": 0}}},
                "Removed": {"properties": {}},
            }},
        }
        actual_openapi = {
            "paths": {
                "/a": {"get": {"summary": "new"}},
                "/new": {"post": {"summary": "added"}},
            },
            "components": {"schemas": {
                "Thing": {"required": ["y"], "properties": {
                    "x": {"type": "integer", "minimum": 1}, "y": {"type": "string"},
                }},
                "Added": {"properties": {}},
            }},
        }

        differences = api_contract.compare_contracts(
            expected_openapi, expected_routes, actual_openapi, actual_routes
        )

        self.assertIn("Route reordered: GET /a 0 -> 1", differences)
        self.assertIn("Operation changed: GET /a", differences)
        self.assertIn("Operation added: POST /new", differences)
        self.assertIn("Schema removed: Removed", differences)
        self.assertIn("Schema added: Added", differences)
        self.assertIn("Required field removed: Thing.x", differences)
        self.assertIn("Required field added: Thing.y", differences)
        self.assertIn("Schema property added: Thing.y", differences)
        self.assertIn("Schema constraint changed: Thing.x.minimum: 0 -> 1", differences)

        route_differences = api_contract.compare_contracts(
            {}, [route("/old", "old")], {}, [route("/new", "new")]
        )
        self.assertIn("Route removed: GET /old (old)", route_differences)
        self.assertIn("Route added: GET /new (new)", route_differences)

    def test_api_contract_diff_reports_only_relative_reorders_of_shared_routes(self):
        route = lambda path: {
            "index": 0, "methods": ["GET"], "name": path, "path": path, "type": "APIRoute"
        }

        differences = api_contract.compare_contracts(
            {},
            [route("/removed"), route("/a"), route("/b")],
            {},
            [route("/b"), route("/a"), route("/added")],
        )

        self.assertIn("Route removed: GET /removed (/removed)", differences)
        self.assertIn("Route added: GET /added (/added)", differences)
        self.assertIn("Route reordered: GET /a 0 -> 1", differences)
        self.assertIn("Route reordered: GET /b 1 -> 0", differences)

        offset_only_differences = api_contract.compare_contracts(
            {}, [route("/removed"), route("/a"), route("/b")],
            {}, [route("/added"), route("/a"), route("/b")],
        )
        self.assertFalse(any("Route reordered:" in line for line in offset_only_differences))

    def test_api_contract_check_mode_is_read_only(self):
        before = (
            api_contract.OPENAPI_SNAPSHOT.read_bytes(),
            api_contract.ROUTES_SNAPSHOT.read_bytes(),
        )
        result = subprocess.run(
            [sys.executable, "update_api_contract_snapshots.py", "--check"],
            cwd=Path(__file__).parent.parent, capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("API contract snapshots match.", result.stdout)
        self.assertEqual(before, (
            api_contract.OPENAPI_SNAPSHOT.read_bytes(),
            api_contract.ROUTES_SNAPSHOT.read_bytes(),
        ))
        with patch.object(api_contract, "check_snapshots", return_value=["drift"]), \
             patch.object(sys, "argv", ["update_api_contract_snapshots.py", "--check"]), \
             patch("builtins.print"):
            with self.assertRaises(SystemExit) as raised:
                api_contract.main()
        self.assertEqual(1, raised.exception.code)
