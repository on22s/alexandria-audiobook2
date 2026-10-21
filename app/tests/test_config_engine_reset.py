"""CPU config-route races with recording engines; no model inference."""
import asyncio
import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import weakref

from fastapi import FastAPI
import httpx

from project import ProjectManager
import project as project_module
from routers import system
from utils import atomic_json_write


class SimpleEngine:
    def __init__(self, mode):
        self.mode = mode


class ConfigEngineResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ProjectManager(self.temp.name)
        self.path = Path(self.manager.config_path)
        self.profile = {"base_url": "http://localhost:1234/v1", "api_key": "synthetic", "model_name": "synthetic"}
        atomic_json_write(self.payload("local"), self.path)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.reset_entered = threading.Event()
        self.block = False
        self.second_write = threading.Event()
        writes = []

        def write_config(data, path):
            atomic_json_write(data, path)
            writes.append(data["tts"]["mode"])
            if len(writes) == 2:
                self.second_write.set()

        def construct(config):
            if self.block:
                self.entered.set()
                if not self.release.wait(5):
                    raise RuntimeError("constructor barrier timed out")
            return SimpleEngine(config["tts"]["mode"])

        original_reset = self.manager.reset_engine

        def reset():
            self.reset_entered.set()
            original_reset()

        for patcher in (patch.object(project_module, "TTSEngine", side_effect=construct),
                        patch.object(system, "project_manager", self.manager),
                        patch.object(system, "CONFIG_PATH", str(self.path)),
                        patch.object(system, "atomic_json_write", side_effect=write_config),
                        patch.object(self.manager, "reset_engine", side_effect=reset)):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(system.router)

        @app.get("/cpu-ping")
        async def ping():
            return {"ok": True}

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture")
        self.addAsyncCleanup(self.client.aclose)

    def payload(self, mode):
        return {"llm": self.profile, "llm_local": self.profile, "llm_mode": "local",
                "tts": {"mode": mode, "url": "http://localhost:7860"}}

    async def test_pending_constructor_is_reset_before_save_success_and_event_loop_stays_responsive(self):
        self.block = True
        getter = asyncio.create_task(asyncio.to_thread(self.manager.get_engine))
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 3))
        save = asyncio.create_task(self.client.post("/api/config", json=self.payload("external")))
        try:
            self.assertTrue(await asyncio.to_thread(self.reset_entered.wait, 3))
            response = await asyncio.wait_for(self.client.get("/cpu-ping"), 2)
            self.assertEqual({"ok": True}, response.json())
            self.assertFalse(save.done())
        finally:
            self.release.set()
        old = await getter
        response = await save
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("local", old.mode)
        self.block = False
        current = self.manager.get_engine()
        self.assertEqual("external", current.mode)
        self.assertIsNot(current, old)
        # A caller may retain its prior engine until its work returns.
        old_ref = weakref.ref(old)
        self.assertIsNotNone(old_ref())
        del old
        del getter
        await asyncio.sleep(0)
        gc.collect()
        self.assertIsNone(old_ref())

    async def test_same_mtime_save_invalidates_config_and_engine(self):
        old = self.manager.get_engine()
        stamp = self.path.stat().st_mtime_ns
        response = await self.client.post("/api/config", json=self.payload("external"))
        self.assertEqual(200, response.status_code, response.text)
        os.utime(self.path, ns=(stamp, stamp))
        self.assertEqual("external", self.manager.get_engine().mode)
        self.assertEqual("local", old.mode)

    async def test_failed_write_keeps_engine_and_cached_config(self):
        old = self.manager.get_engine()
        cached = self.manager._read_config()
        with patch.object(system, "atomic_json_write", side_effect=OSError("synthetic write failure")):
            with self.assertRaisesRegex(OSError, "synthetic write failure"):
                await self.client.post("/api/config", json=self.payload("external"))
        self.assertIs(old, self.manager.get_engine())
        self.assertIs(cached, self.manager._read_config())
        self.assertFalse(self.reset_entered.is_set())
        self.assertEqual("local", json.loads(self.path.read_text())["tts"]["mode"])

    async def test_overlapping_saves_complete_without_deadlock_and_engine_matches_disk(self):
        self.block = True
        getter = asyncio.create_task(asyncio.to_thread(self.manager.get_engine))
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 3))
        first = asyncio.create_task(self.client.post("/api/config", json=self.payload("external")))
        self.assertTrue(await asyncio.to_thread(self.reset_entered.wait, 3))
        second = asyncio.create_task(self.client.post("/api/config", json=self.payload("local")))
        try:
            # Config publication and reset hold book ownership together so a
            # pronunciation approval cannot observe new settings/old engine.
            self.assertFalse(await asyncio.to_thread(self.second_write.wait, .1))
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            await asyncio.wait_for(self.client.get("/cpu-ping"), 2)
        finally:
            self.release.set()
        await getter
        responses = await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertEqual([200, 200], [response.status_code for response in responses])
        self.assertTrue(self.second_write.is_set())
        self.block = False
        self.assertEqual(json.loads(self.path.read_text())["tts"]["mode"], self.manager.get_engine().mode)
