"""#2105: a NUL byte in a preparer filename is a clean 400.

A percent-encoded NUL used to reach realpath (POSIX ValueError) or Path.stat
(Windows ValueError) and surface as HTTP 500. Every preparer route that takes
a filename now goes through one rejection helper before a path is built.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

import routers.preparer as preparer


class PreparerNulFilenameTests(unittest.TestCase):
    def test_download_rejects_nul_before_realpath_is_called(self):
        def refuse(*args, **kwargs):
            raise AssertionError("realpath must not be reached with a NUL name")

        with patch.object(preparer.os.path, "realpath", side_effect=refuse):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(preparer.preparer_download("foo\0.zip"))
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("Invalid filename.", raised.exception.detail)

    def test_posix_style_realpath_value_error_cannot_escape_as_500(self):
        def posix_realpath(path):
            raise ValueError("embedded null byte in path")

        with patch.object(preparer.os.path, "realpath", side_effect=posix_realpath):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(preparer.preparer_download("foo\0.zip"))
        self.assertEqual(400, raised.exception.status_code)

    def test_normal_download_still_works(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "dataset.zip")
            target.write_bytes(b"zip")
            with patch.object(preparer, "PREPARER_OUTPUT_DIR", root):
                response = asyncio.run(preparer.preparer_download("dataset.zip"))
            self.assertEqual(str(target), response.path)

    def test_start_rejects_nul_in_audio_output_and_source_names(self):
        patches = (
            patch.object(preparer, "_resolve_preparer_interpreter", return_value="python"),
            patch.object(preparer, "check_global_gpu_lock"),
            patch.object(preparer, "check_disk_space", return_value=(True, 10)),
        )
        for field, detail in (("audio_filename", "Invalid audio filename"),
                              ("output_filename", "Invalid output filename")):
            with self.subTest(field=field):
                config = json.dumps({"audio_filename": "a.wav", field: "bad\0name"})
                with patches[0], patches[1], patches[2]:
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(preparer.preparer_start(
                            BackgroundTasks(), config_json=config, audio_file=None, source_file=None))
                self.assertEqual(400, raised.exception.status_code)
                self.assertEqual(detail, raised.exception.detail)

    def test_batch_rejects_nul_in_task_filenames_before_dispatch(self):
        request = preparer.BatchPreparerRequest(tasks=[
            {"audio_filename": "a.wav", "output_filename": "out\0.zip"},
        ])
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(preparer._start_preparer_batch(request, BackgroundTasks()))
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("Invalid output filename", raised.exception.detail)

    def test_all_filenames_share_one_nul_gate(self):
        source = Path(preparer.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, source.count('def _reject_nul_filename('))
        self.assertIn('if "\\0" in value:', source)
        self.assertGreaterEqual(source.count("_reject_nul_filename("), 7)


if __name__ == "__main__":
    unittest.main()
