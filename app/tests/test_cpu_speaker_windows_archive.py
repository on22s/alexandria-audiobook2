"""The archived evidence checker must reject semantic corruption even with -O."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ARCHIVE = Path(__file__).resolve().parents[2] / 'docs/benchmarks/raw/cpu-speaker-windows-20261022'


class CpuSpeakerWindowsArchiveTests(unittest.TestCase):
    def run_checker(self, folder, optimize):
        environment = dict(os.environ, PYTHONOPTIMIZE=str(optimize))
        return subprocess.run([sys.executable, str(folder / 'verify.py')], env=environment,
                              capture_output=True, text=True, timeout=30)

    def test_actual_archive_passes_with_and_without_optimization(self):
        for optimize in (0, 1):
            with self.subTest(optimize=optimize):
                result = self.run_checker(ARCHIVE, optimize)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('384 cells', result.stdout)

    def test_duplicate_or_missing_cell_is_rejected_even_if_private_copy_checksum_is_updated(self):
        for corruption in ('extra_row', 'replaced_cell'):
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp) / 'archive'; shutil.copytree(ARCHIVE, folder)
                relative = 'run2/test_rows.json'; path = folder / relative
                rows = json.loads(path.read_text())
                if corruption == 'extra_row':
                    rows.append(rows[0])
                else:
                    rows[-1] = rows[0]
                path.write_text(json.dumps(rows))
                checksums = json.loads((folder / 'SHA256SUMS.json').read_text())
                checksums[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
                (folder / 'SHA256SUMS.json').write_text(json.dumps(checksums))
                for optimize in (0, 1):
                    with self.subTest(corruption=corruption, optimize=optimize):
                        result = self.run_checker(folder, optimize)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn('PASS:', result.stdout)
                        self.assertIn('Archive semantic check failed', result.stderr)


if __name__ == '__main__':
    unittest.main()
