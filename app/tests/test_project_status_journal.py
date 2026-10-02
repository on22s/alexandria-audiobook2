"""Journal integration preserves live edits, published PCM and book ownership."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import soundfile as sf
from project import ProjectManager
from chunk_status_journal import get_chunk_status_journal_path, remove_chunk_snapshot


class ProjectStatusJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manager = ProjectManager(str(self.root))
        self.rows = [{"id": i, "uid": str(i), "speaker": "Narrator", "text": "line " + str(i),
                      "status": "pending", "audio_path": None} for i in range(2)]
        self.manager.save_chunks(self.rows)
        self.journal = Path(get_chunk_status_journal_path(self.manager.chunks_path))
        def export(source, name):
            relative = "voicelines/" + name + ".wav"
            shutil.copyfile(source, self.root / relative)
            return relative
        self.manager._export_chunk_audio = export

    def run_during_generation(self, edit):
        started, proceed = threading.Event(), threading.Event()
        class Engine:
            def generate_voice(self, text, instruct, speaker, config, output):
                started.set()
                if not proceed.wait(10):
                    raise RuntimeError("test worker release timed out")
                sf.write(output, [.1] * 2400, 24000)
                return True
        self.manager.engine = Engine()
        result, errors = [], []
        def run():
            try:
                result.append(self.manager.generate_chunks_parallel([0], max_workers=1))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(started.wait(10))
            self.assertEqual(ProjectManager(str(self.root)).load_chunks()[0]["status"], "generating")
            edit()
        finally:
            proceed.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        return result, errors

    def test_second_manager_edit_preserves_deltas_and_same_uid_generation(self):
        result, errors = self.run_during_generation(
            lambda: ProjectManager(str(self.root)).update_chunk(1, {"text": "edited second"}))
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["completed"], [0])
        final = json.loads(Path(self.manager.chunks_path).read_text())
        self.assertEqual(final[0]["status"], "done")
        self.assertEqual(final[1]["text"], "edited second")
        self.assertFalse(self.journal.exists())
        audio, rate = sf.read(self.root / final[0]["audio_path"])
        self.assertEqual((len(audio), rate), (2400, 24000))

    def test_editing_target_rejects_old_audio_without_reverting_edit(self):
        result, errors = self.run_during_generation(
            lambda: ProjectManager(str(self.root)).update_chunk(0, {"text": "new input"}))
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["completed"], [])
        self.assertEqual(len(result[0]["failed"]), 1)
        row = self.manager.load_chunks()[0]
        self.assertEqual(row["text"], "new input")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["audio_path"])
        self.assertEqual(list((self.root / "voicelines").iterdir()), [])

    def test_changed_book_refuses_publication_and_preserves_old_journal(self):
        def change_book():
            (self.root / "state.json").write_text('{"active_book_id":"different"}')
        result, errors = self.run_during_generation(change_book)
        self.assertEqual(result, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("generation inputs changed", str(errors[0]))
        self.assertTrue(self.journal.exists())
        self.assertEqual(list((self.root / "voicelines").iterdir()), [])
        (self.root / "state.json").unlink()
        self.assertEqual(self.manager.load_chunks()[0]["status"], "generating")

    @unittest.skipIf(os.name == "nt", "Native SIGKILL test requires POSIX")
    def test_process_death_after_real_pcm_publication_reloads_and_compacts(self):
        code = """import os,signal,sys,shutil
from pathlib import Path
import soundfile as sf
from project import ProjectManager
root=Path(sys.argv[1]);m=ProjectManager(str(root))
class Engine:
 def generate_voice(self,text,instruct,speaker,config,output):
  sf.write(output,[.1]*2400,24000);return True
m.engine=Engine()
def export(source,name):
 relative='voicelines/'+name+'.wav';shutil.copyfile(source,root/relative);return relative
m._export_chunk_audio=export
publish=m._publish_generated_chunk_audio
def kill_after_publication(*args):
 publish(*args);os.kill(os.getpid(),signal.SIGKILL)
m._publish_generated_chunk_audio=kill_after_publication
m.generate_chunks_parallel([0],max_workers=1)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, text=True)
        self.assertEqual(child.returncode, -9, child.stderr)
        manager = ProjectManager(str(self.root))
        rows = manager.load_chunks()
        self.assertEqual(rows[0]["status"], "done")
        audio, rate = sf.read(self.root / rows[0]["audio_path"])
        self.assertEqual((len(audio), rate), (2400, 24000))
        manager.update_chunk(1, {"text": "after recovery"})
        self.assertFalse(self.journal.exists())
        saved = json.loads(Path(manager.chunks_path).read_text())
        self.assertEqual(saved[0], rows[0])
        self.assertEqual(saved[1]["text"], "after recovery")

    def test_script_replacement_retires_snapshot_and_journal_only(self):
        self.manager._get_status_journal_locked().apply_update("0", {"status": "done"})
        sentinel = self.root / "keep.json"
        sentinel.write_text("keep")
        self.assertTrue(remove_chunk_snapshot(self.manager.chunks_path))
        self.assertFalse(self.journal.exists())
        self.assertFalse(Path(self.manager.chunks_path).exists())
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse(remove_chunk_snapshot(self.manager.chunks_path))

    def test_oom_retry_steps_down_and_finally_compacts_after_cancel_or_callback_failure(self):
        for mode in ("oom", "cancel", "callback_error"):
            with self.subTest(mode=mode):
                self.manager.save_chunks(copy.deepcopy(self.rows))
                attempts = {}
                class Engine:
                    def generate_voice(self, text, instruct, speaker, config, output):
                        attempts[text] = attempts.get(text, 0) + 1
                        if mode == "oom" and attempts[text] == 1:
                            raise RuntimeError("CUDA out of memory")
                        sf.write(output, [.1] * 2400, 24000)
                        return True
                self.manager.engine = Engine()
                def callback(*args):
                    if mode == "callback_error":
                        raise OSError("progress callback failed")
                with patch("gc.collect", wraps=__import__("gc").collect) as collect:
                    if mode == "callback_error":
                        with self.assertRaisesRegex(OSError, "progress callback"):
                            self.manager.generate_chunks_parallel([0, 1], max_workers=2,
                                                                  progress_callback=callback)
                    else:
                        result = self.manager.generate_chunks_parallel(
                            [0, 1], max_workers=2, cancel_check=lambda: mode == "cancel")
                        self.assertEqual(len(result["completed"]) + len(result["failed"])
                                         + result["cancelled"], 2)
                    if mode == "oom":
                        self.assertEqual(sorted(attempts.values()), [2, 2])
                        self.assertEqual(sorted(result["completed"]), [0, 1])
                        self.assertEqual(result["failed"], [])
                        collect.assert_called_once()
                self.assertFalse(self.journal.exists())
                rows = json.loads(Path(self.manager.chunks_path).read_text())
                self.assertNotIn("generating", [row["status"] for row in rows])
                for row in rows:
                    if row["status"] == "done":
                        audio, rate = sf.read(self.root / row["audio_path"])
                        self.assertEqual((len(audio), rate), (2400, 24000))


if __name__ == "__main__":
    unittest.main()
