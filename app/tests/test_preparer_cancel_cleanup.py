"""Deterministic cleanup-window regression for batch-preparer cancel.

The claimed worker sets raw process_state running=False BEFORE
finish_run/cleanup and claim release, while core.is_task_running keeps
reporting owned liveness through cleanup (required safety). The live
smoke's instantaneous idle inference raced that window. This regression
holds the REAL claimed-worker cleanup on an Event (finish_run gated,
nothing else mocked) and drives the REAL test_api smoke function plus
real FastAPI cancel/status handlers, under the same global isolation
as PreparerAdmissionCancelTests (fresh deepcopy state shared by core +
routers, fresh claims/leases, temp data dirs; only physical GPU
acquisition/discovery patched).
"""
import copy
import threading
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.background import BackgroundTasks

import core
from routers import preparer as preparer_router
from routers import script as script_router
from tests import test_api as api


class PreparersCancelCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = copy.deepcopy(core.process_state)
        for row in self.state.values():
            row["running"] = False
        for patcher in (
            patch.object(core, "process_state", self.state),
            patch.object(preparer_router, "process_state", self.state),
            patch.object(script_router, "process_state", self.state),
            patch.object(core, "_task_claims", {}),
            patch.object(core, "_gpu_leases", {}),
            patch.object(core, "DATA_DIR", str(self.root)),
            patch.object(core, "RUN_HISTORY_DIR", str(self.root / "runs")),
            patch.object(script_router, "DATA_DIR", str(self.root)),
            patch.object(core, "acquire_gpu_lock", return_value=None),
            patch.object(core, "llm_is_on_this_gpu", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._release_claims)

    def _release_claims(self):
        for name, owner in list(core._task_claims.items()):
            core.release_gpu_task_claim(name, owner["id"])

    def _client(self):
        app = FastAPI()
        app.include_router(preparer_router.router)
        app.include_router(script_router.router)
        return TestClient(app, raise_server_exceptions=False)

    def _wire_smoke(self, client, observed=None):
        def fake_get(path, **kwargs):
            kwargs.pop("timeout", None)
            kwargs.pop("headers", None)
            response = client.get(path)
            if observed is not None and path == "/api/status/batch_preparer":
                try:
                    if response.status_code == 200 and response.json().get("running") is True:
                        observed.set()
                except Exception:
                    pass
            return response

        def fake_post(path, **kwargs):
            kwargs.pop("timeout", None)
            kwargs.pop("headers", None)
            return client.post(path, json=kwargs.get("json"))

        return fake_get, fake_post

    def _start_held_worker(self):
        """Real reserve/register/owned-worker lifecycle in a thread."""
        claim_id = core.reserve_background_task("batch_preparer")
        background = BackgroundTasks()
        core.register_claimed_background_task(
            background, "batch_preparer", claim_id,
            core._run_claimed_background_task, "batch_preparer", lambda: None)
        task = background.tasks[0]
        worker = threading.Thread(
            target=lambda: task.func(*task.args, **task.kwargs), daemon=True)
        return worker

    def test_cleanup_window_cancel_refuses_but_smoke_waits_for_claim_release(self):
        client = self._client()
        cleanup_held = threading.Event()
        release_cleanup = threading.Event()
        real_finish = core.finish_run

        def gated_finish(*args, **kwargs):
            cleanup_held.set()
            release_cleanup.wait(timeout=30)
            return real_finish(*args, **kwargs)

        worker = self._start_held_worker()
        smoke_errors = []
        smoke_thread = None
        try:
            with patch.object(core, "finish_run", gated_finish):
                worker.start()
                try:
                    self.assertTrue(cleanup_held.wait(timeout=10),
                                    "cleanup window must be reached")
                    # Legitimate cleanup window: raw flag already False,
                    # owned liveness still True.
                    self.assertFalse(core.process_state["batch_preparer"]["running"])
                    self.assertTrue(core.is_task_running("batch_preparer"))
                    observed = threading.Event()
                    fake_get, fake_post = self._wire_smoke(client, observed=observed)
                    real_wait = api.wait_for_task
                    with patch.object(api, "post", fake_post), patch.object(api, "get", fake_get), \
                            patch.object(api, "wait_for_task",
                                         side_effect=lambda task: real_wait(
                                             task, timeout=5, poll_interval=0.1)):
                        def run_smoke():
                            try:
                                api.test_batch_preparer_cancel()
                            except BaseException as exc:  # noqa: BLE001 - re-raised below
                                smoke_errors.append(exc)

                        smoke_thread = threading.Thread(target=run_smoke, daemon=True)
                        try:
                            smoke_thread.start()
                            # Deterministic: observe a REAL status poll reporting
                            # owned liveness while the idle-cancel 400 occurred.
                            self.assertTrue(observed.wait(timeout=10),
                                            "smoke must poll owned liveness during cleanup")
                            self.assertTrue(smoke_thread.is_alive(),
                                            "smoke must still be waiting for claim release")
                            self.assertTrue(core.is_task_running("batch_preparer"))
                            release_cleanup.set()
                            smoke_thread.join(timeout=30)
                            self.assertFalse(smoke_thread.is_alive(),
                                             "smoke must finish after claim release")
                            if smoke_errors:
                                raise smoke_errors[0]
                        finally:
                            # Join the live smoke INSIDE all patch
                            # contexts: it must never resume original
                            # api.get/post/finish_run while unwinding.
                            release_cleanup.set()
                            smoke_thread.join(timeout=30)
                finally:
                    # Join the owned worker INSIDE the finish_run patch
                    # context for the same lifetime guarantee.
                    release_cleanup.set()
                    worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertFalse(core.is_task_running("batch_preparer"))
        finally:
            # Fallback only: release own claims if an assertion above
            # already joined both threads inside their contexts.
            release_cleanup.set()
            if smoke_thread is not None and smoke_thread.is_alive():
                smoke_thread.join(timeout=30)
            worker.join(timeout=10)
            self._release_claims()

    def test_stuck_cleanup_smoke_times_out_and_idle_cancel_is_immediate(self):
        client = self._client()
        fake_get, fake_post = self._wire_smoke(client)
        with patch.object(api, "post", fake_post), patch.object(api, "get", fake_get):
            # Genuine idle: exact 400 detail, status bool False, wait
            # returns True immediately.
            r = client.post("/api/preparer/batch/cancel", json={})
            self.assertEqual(400, r.status_code)
            self.assertEqual("No batch preparer is currently running.",
                             r.json().get("detail"))
            self.assertTrue(api.wait_for_task(
                "batch_preparer", timeout=5, poll_interval=0.1))
            # Stuck cleanup: the REAL smoke function must surface its
            # GPU-lock TestFailure after the real bounded wait times
            # out (shortened TEST timeout/poll only; real polling).
            cleanup_held = threading.Event()
            release_cleanup = threading.Event()
            real_finish = core.finish_run

            def gated_finish(*args, **kwargs):
                cleanup_held.set()
                release_cleanup.wait(timeout=30)
                return real_finish(*args, **kwargs)

            worker = self._start_held_worker()
            try:
                with patch.object(core, "finish_run", gated_finish):
                    worker.start()
                    try:
                        self.assertTrue(cleanup_held.wait(timeout=10))
                        real_wait = api.wait_for_task
                        with patch.object(api, "wait_for_task",
                                          side_effect=lambda task: real_wait(
                                              task, timeout=3, poll_interval=0.1)):
                            with self.assertRaises(api.TestFailure) as caught:
                                api.test_batch_preparer_cancel()
                        self.assertIn("GPU lock not released", str(caught.exception))
                        release_cleanup.set()
                        worker.join(timeout=10)
                        self.assertFalse(worker.is_alive())
                        self.assertFalse(core.is_task_running("batch_preparer"))
                    finally:
                        # Join INSIDE the finish_run patch context.
                        release_cleanup.set()
                        worker.join(timeout=10)
            finally:
                release_cleanup.set()
                worker.join(timeout=10)
                self._release_claims()

    def test_active_cancel_still_requests_cancellation(self):
        client = self._client()
        callback_held = threading.Event()
        release_callback = threading.Event()

        def blocking_callback():
            callback_held.set()
            release_callback.wait(timeout=30)

        claim_id = core.reserve_background_task("batch_preparer")
        background = BackgroundTasks()
        core.register_claimed_background_task(
            background, "batch_preparer", claim_id,
            core._run_claimed_background_task, "batch_preparer", blocking_callback)
        task = background.tasks[0]
        worker = threading.Thread(
            target=lambda: task.func(*task.args, **task.kwargs), daemon=True)
        try:
            worker.start()
            self.assertTrue(callback_held.wait(timeout=10))
            r = client.post("/api/preparer/batch/cancel", json={})
            self.assertEqual(200, r.status_code, r.text)
            self.assertEqual("cancel_requested", r.json().get("status"))
        finally:
            release_callback.set()
            worker.join(timeout=10)
            self._release_claims()
        self.assertFalse(core.is_task_running("batch_preparer"))

    def test_wait_for_task_requires_actual_bool_false(self):
        # Malformed 200 bodies (missing/null/non-bool running) must NOT
        # pass as owned-idle: real wait_for_task times out on them,
        # while a genuine False succeeds and a genuine True times out.
        class FakeResponse:
            def __init__(self, body):
                self.status_code = 200
                self._body = body
                self.text = repr(body)

            def json(self):
                return self._body

        for body in ({}, {"running": None}, {"running": "false"}, {"running": 0}):
            with self.subTest(body=body):
                with patch.object(api, "get", return_value=FakeResponse(body)):
                    self.assertFalse(api.wait_for_task(
                        "batch_preparer", timeout=1, poll_interval=0.1),
                        f"malformed status {body!r} must not pass as idle")
        with patch.object(api, "get", return_value=FakeResponse({"running": False})):
            self.assertTrue(api.wait_for_task(
                "batch_preparer", timeout=5, poll_interval=0.1))
        with patch.object(api, "get", return_value=FakeResponse({"running": True})):
            self.assertFalse(api.wait_for_task(
                "batch_preparer", timeout=1, poll_interval=0.1))


if __name__ == "__main__":
    unittest.main()
