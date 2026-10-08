import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class CorpusPlanFailureTests(unittest.TestCase):
    def test_discovery_and_display_failures_refuse_but_valid_empty_plan_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "build_test_corpus.sh"
            shutil.copy2(REPO / script.name, script)
            interpreter = root / "app/env/bin/python"
            interpreter.parent.mkdir(parents=True)
            audio, source = root / "audio", root / "source"
            audio.mkdir(); source.mkdir()
            for mode in ("discovery-failed", "invalid-json", "display-failed", "empty"):
                with self.subTest(mode=mode):
                    interpreter.write_text("#!/bin/sh\n"
                        'printf "%s\\n" "$1" >> "$CALLS"\n'
                        'if [ "$1" = -c ]; then\n'
                        '  if [ "$CORPUS_PROBE_MODE" = discovery-failed ]; then exit 3; fi\n'
                        '  if [ "$CORPUS_PROBE_MODE" = invalid-json ]; then echo broken-json; else echo "[]"; fi\n'
                        '  exit 0\nfi\n'
                        'if [ "$CORPUS_PROBE_MODE" = display-failed ]; then exit 4; fi\n'
                        'exec ' + shlex.quote(sys.executable) + ' "$@"\n')
                    interpreter.chmod(0o700)
                    calls = root / (mode + ".calls")
                    output = root / mode
                    env = dict(os.environ, CORPUS_PROBE_MODE=mode, CALLS=str(calls), AUDIO_DIR=str(audio),
                               SOURCE_DIR=str(source), OUT_DIR=str(output))
                    result = subprocess.run(["bash", str(script), "--plan"], env=env,
                                            capture_output=True, text=True, timeout=10)
                    if mode == "empty":
                        self.assertEqual(0, result.returncode, result.stderr)
                        self.assertEqual([], json.loads((output / "pairs.json").read_text()))
                        self.assertIn("Plan mode", result.stdout)
                    else:
                        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                        self.assertNotIn("Plan mode", result.stdout)
                    if mode == "discovery-failed":
                        self.assertEqual(["-c"], calls.read_text().splitlines())
