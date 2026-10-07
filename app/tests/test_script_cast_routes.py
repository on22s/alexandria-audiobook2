"""The Script tab's cast list reaches generation (#653).

What matters is the command three_pass_generate receives: --cast-file when the
selected book has a list and only then; a resumed run keeps the list it started
with, because the list's hash is part of the checkpoint identity and an edited
list would otherwise send Retry back to chunk 1; and a batch builds a list per
book only when asked.
"""
import asyncio
import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import core
from cast_list import get_cast_list_path
from routers import script as script_module
from utils import atomic_json_write

CAST = [{"name": "THE DETECTIVE", "aliases": []},
        {"name": "MAURICE OAKLEY", "aliases": ["MR. OAKLEY"]}]


def _cast_file_of(command):
    return command[command.index("--cast-file") + 1] if "--cast-file" in command else None


class CastRoutesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.source = os.path.join(self.dir, "book.txt")
        with open(self.source, "w", encoding="utf-8") as handle:
            handle.write('"Who was it?" asked the detective.')
        atomic_json_write({"input_file_path": self.source}, os.path.join(self.dir, "state.json"))
        self.list_path = get_cast_list_path(self.source, self.dir)
        self.scheduled = []
        self.patches = [
            patch.object(script_module, "DATA_DIR", self.dir),
            patch.object(script_module, "SCRIPT_PATH", os.path.join(self.dir, "annotated_script.json")),
            patch.object(script_module, "check_global_gpu_lock"),
            patch.object(script_module, "three_pass_refusal", return_value=None),
            patch.object(script_module, "get_active_reasoning_effort", return_value=None),
            patch.object(script_module, "ensure_script_recovery_manifest", return_value={}),
            patch.object(script_module, "get_script_recovery_manifest", return_value={}),
            patch.object(script_module, "reserve_background_task", return_value="cast-fixture-claim"),
            patch.object(script_module, "register_claimed_background_task",
                         side_effect=lambda _tasks, name, _claim, _run, command, *_: self.scheduled.append(command)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def write_list(self, cast=CAST):
        os.makedirs(os.path.dirname(self.list_path), exist_ok=True)
        atomic_json_write({"cast": cast, "provenance": {}}, self.list_path)

    def start(self, recovery=False):
        script_module.start_script_generation(
            script_module.BackgroundTasks(), self.source, None, require_recovery=recovery)
        return self.scheduled[-1]

    def stored_options(self):
        with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as handle:
            return json.load(handle)["script_generation_options"]

    def test_command_carries_cast_file_only_when_given(self):
        self.assertIsNone(_cast_file_of(script_module.build_generate_script_command(self.source)))
        command = script_module.build_generate_script_command(self.source, cast_file="c.json")
        self.assertEqual("c.json", _cast_file_of(command))

    def test_a_book_without_a_list_generates_without_one(self):
        self.assertIsNone(_cast_file_of(self.start()))
        self.assertIsNone(self.stored_options()["cast_sha256"])

    def test_a_book_with_a_list_generates_from_a_frozen_copy(self):
        self.write_list()
        command = self.start()
        frozen = script_module.get_run_cast_path()
        self.assertEqual(frozen, _cast_file_of(command))
        with open(self.list_path, "rb") as a, open(frozen, "rb") as b:
            listed, copied = a.read(), b.read()
        self.assertEqual(listed, copied)
        self.assertEqual(hashlib.sha256(copied).hexdigest(), self.stored_options()["cast_sha256"])

    def test_retry_keeps_the_list_the_run_started_with(self):
        self.write_list()
        self.start()
        with open(script_module.get_run_cast_path(), "rb") as handle:
            started_with = handle.read()
        self.write_list(CAST + [{"name": "THE CLERK", "aliases": []}])     # edited after a failure
        command = self.start(recovery=True)
        self.assertEqual(script_module.get_run_cast_path(), _cast_file_of(command))
        with open(script_module.get_run_cast_path(), "rb") as handle:
            self.assertEqual(started_with, handle.read())

    def test_retry_refuses_when_the_frozen_list_changed(self):
        self.write_list()
        self.start()
        atomic_json_write({"cast": [{"name": "SOMEONE ELSE", "aliases": []}]},
                          script_module.get_run_cast_path())
        with self.assertRaises(HTTPException) as caught:
            self.start(recovery=True)
        self.assertEqual(409, caught.exception.status_code)

    def test_saving_a_bad_list_is_refused_and_writes_nothing(self):
        update = script_module.CastListUpdate(cast=[{"aliases": ["X"]}])
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(script_module.save_cast_list_endpoint(update))
        self.assertEqual(400, caught.exception.status_code)
        self.assertFalse(os.path.exists(self.list_path))
        asyncio.run(script_module.save_cast_list_endpoint(script_module.CastListUpdate(cast=CAST)))
        got = asyncio.run(script_module.get_cast_list())
        self.assertEqual((CAST, 2, True), (got["cast"], got["count"], got["edited"]))

    def test_cast_list_is_an_llm_task_under_the_lock(self):
        self.assertIn("cast_list", core.LLM_TASKS)
        self.assertIn("cast_list", core.GPU_TASKS)


class BatchCastTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.source = os.path.join(self.dir, "book.txt")
        with open(self.source, "w", encoding="utf-8") as handle:
            handle.write("text")
        self.list_path = get_cast_list_path(self.source, self.dir)
        self.commands = []
        self.state = {"tasks": [{"status": "pending"}], "logs": [], "cancel": False}

    def tearDown(self):
        self._tmp.cleanup()

    def run_job(self, build, cast_rc=0):
        def stream(command, *_args, **_kwargs):
            self.commands.append(command)
            if "cast_list.py" in command:
                if cast_rc == 0:
                    os.makedirs(os.path.dirname(self.list_path), exist_ok=True)
                    atomic_json_write({"cast": CAST}, self.list_path)
                return cast_rc, []
            return 1, []
        job = {"index": 0, "filename": "book.txt", "input_path": self.source,
               "output_path": os.path.join(self.dir, "out.json"), "safe_stem": "out",
               "strip_front_matter": True, "build_cast_list": build}
        with patch.object(script_module, "DATA_DIR", self.dir), \
                patch.object(script_module, "get_active_reasoning_effort", return_value=None), \
                patch.object(script_module, "_stream_subprocess_to_logs", side_effect=stream):
            script_module._run_claimed_batch_script_job(job, self.state, None, 1)

    def test_asked_to_build_builds_then_generates_with_it(self):
        self.run_job(build=True)
        self.assertEqual(2, len(self.commands))
        self.assertIn("cast_list.py", self.commands[0])
        self.assertEqual(self.list_path, _cast_file_of(self.commands[1]))

    def test_not_asked_builds_nothing(self):
        self.run_job(build=False)
        self.assertEqual(1, len(self.commands))
        self.assertIsNone(_cast_file_of(self.commands[0]))

    def test_a_failed_build_fails_the_book_without_generating(self):
        self.run_job(build=True, cast_rc=1)
        self.assertEqual(1, len(self.commands))
        self.assertEqual("failed", self.state["tasks"][0]["status"])


if __name__ == "__main__":
    unittest.main()
