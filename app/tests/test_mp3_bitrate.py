"""Exported MP3s must carry a speech-grade bitrate - measured on the file, not
inferred from the argument. pydub/ffmpeg's unspecified default for 24 kHz mono
was 32 kbps (ffprobe, 2026-09-11)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent.parent))

import project as project_module
from project import ProjectManager

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _bit_rate(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=bit_rate",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return int(out.stdout.strip().splitlines()[0])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not available")
class Mp3BitrateTests(unittest.TestCase):
    def test_chunk_export_is_at_least_96_kbps(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            os.makedirs(pm.voicelines_dir, exist_ok=True)
            rate = 24000
            t = np.arange(3 * rate) / rate
            wav = os.path.join(tmp, "temp.wav")
            sf.write(wav, (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), rate)
            rel = pm._export_chunk_audio(wav, "probe")
            self.assertTrue(rel.endswith(".mp3"), rel)
            rate_measured = _bit_rate(os.path.join(tmp, rel))
            self.assertGreaterEqual(rate_measured, 96000, f"exported at {rate_measured} bps")
            self.assertEqual("128k", project_module.MP3_BITRATE)


if __name__ == "__main__":
    unittest.main()
