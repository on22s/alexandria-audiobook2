"""A finished EPUB read-aloud build keeps its log on disk (#1844).

The progress panel only shows the live tail; a refusal's full reason (the
grouping rule and tag outline) must be in a file a tester can attach.
"""
import os
import tempfile
import unittest
import unittest.mock

import core
from routers import editor


class ReadaloudBuildLogTests(unittest.TestCase):
    def test_finished_build_log_is_written_and_panel_outcome_stays_last(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {"logs": ["Starting EPUB read-aloud export...",
                              "EPUB read-aloud failed: Layout inside body: h2 + div.text"]}
            with unittest.mock.patch.object(core, "API_LOG_DIR", directory):
                editor._save_readaloud_log(state)
            path = os.path.join(directory, "readaloud_epub-latest.log")
            with open(path, encoding="utf-8") as handle:
                saved = handle.read()
        self.assertTrue(saved.startswith("# readaloud_epub log"))
        self.assertIn("EPUB read-aloud failed: Layout inside body: h2 + div.text\n", saved)
        # The panel's last line is the build outcome; saving must not move it.
        self.assertEqual("EPUB read-aloud failed: Layout inside body: h2 + div.text",
                         state["logs"][-1])

    def test_unwritable_log_does_not_raise_or_touch_the_panel(self):
        state = {"logs": ["EPUB read-aloud failed: x"]}
        missing = os.path.join(tempfile.gettempdir(), "aa2-no-such-dir", "nested")
        with unittest.mock.patch.object(core, "API_LOG_DIR", missing):
            editor._save_readaloud_log(state)
        self.assertEqual(["EPUB read-aloud failed: x"], state["logs"])


if __name__ == "__main__":
    unittest.main()
