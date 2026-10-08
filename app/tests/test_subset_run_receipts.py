import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class SubsetRunReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = (REPO / "run_subset.sh").read_text()
        start = source.index("PAIRS=(")
        end = source.index("\n)", start) + 2
        source = source[:start] + 'PAIRS=("$AUDIO_DIR/Synthetic Narrator Book One.wav|$SOURCE_DIR/Synthetic Book.txt")' + source[end:]
        (self.root / "run_subset.sh").write_text(source)
        shutil.copy2(REPO / "watch_subset.sh", self.root / "watch_subset.sh")
        helper = REPO / "run_chains/lib/subset_outputs.sh"
        if helper.exists():
            destination = self.root / "run_chains/lib/subset_outputs.sh"
            destination.parent.mkdir(parents=True)
            shutil.copy2(helper, destination)
        worker = self.root / "run_with_restart.sh"
        worker.write_text('#!' + sys.executable + '\nimport os,sys,zipfile\n'
                          'code=int(os.environ.get("SUBSET_PROBE_EXIT","0"))\n'
                          'if not code:\n'
                          ' with zipfile.ZipFile(sys.argv[sys.argv.index("--output")+1],"w") as archive:archive.writestr("fixture.txt","synthetic")\n'
                          'raise SystemExit(code)\n')
        worker.chmod(0o700)
        binary = self.root / "bin"
        binary.mkdir()
        for name, text in (("notify-send", '#!/bin/sh\nexit 0\n'),
                           ("tmux", '#!/bin/sh\nif [ ! -f "$SUBSET_TMUX_STATE" ]; then touch "$SUBSET_TMUX_STATE"; exit 0; fi; exit 1\n')):
            path = binary / name
            path.write_text(text)
            path.chmod(0o700)
        self.env = dict(os.environ, AUDIO_DIR=str(self.root), SOURCE_DIR=str(self.root),
                        PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        SUBSET_TMUX_STATE=str(self.root / "tmux-seen"))
        self.output = self.root / "test_corpus_output"
        self.output.mkdir()

    def run_script(self, script, **env):
        return subprocess.run(["bash", str(self.root / script)], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=10)

    def test_both_completion_receipts_preserve_full_zip_name_and_actual_size(self):
        result = self.run_script("run_subset.sh")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        archive = self.output / "dataset_Synthetic Narrator Book One.zip"
        self.assertTrue(archive.exists())
        for script in (None, "watch_subset.sh"):
            if script:
                result = self.run_script(script)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            receipt = (self.output / "DONE.flag").read_text()
            self.assertIn(archive.name, receipt)
            self.assertIn(str(archive.stat().st_size), receipt)

    def test_each_run_clears_prior_status_and_preserves_prior_zip_artifacts(self):
        previous = self.output / "prior.zip"
        previous.write_bytes(b"prior archive")
        for code in (1, 130, 0):
            with self.subTest(code=code):
                for name in ("DONE.flag", "ABORTED.flag"):
                    (self.output / name).write_text("previous status")
                result = self.run_script("run_subset.sh", SUBSET_PROBE_EXIT=str(code))
                self.assertEqual(code, result.returncode, result.stdout + result.stderr)
                self.assertEqual(code == 0, (self.output / "DONE.flag").exists())
                self.assertEqual(code == 130, (self.output / "ABORTED.flag").exists())
                self.assertEqual(b"prior archive", previous.read_bytes())
