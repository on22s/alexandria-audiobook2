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

from tests.test_support import get_isolated_git_environment

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


class ArchiveLineEndingTests(unittest.TestCase):
    """The byte-attested archive family must not depend on core.autocrlf.

    verify.py hashes these files byte-for-byte against SHA256SUMS.json, so a
    Windows checkout rewriting LF blobs to CRLF fails every pinned hash while
    every parsed field stays identical. The other archive test runs verify.py
    against the current working tree; this one guards the checkout itself so
    the protection cannot be dropped without a deterministic, platform-
    independent failure. It asserts the observable byte invariant rather than
    a particular attribute value, leaving -text or `text eol=lf` both valid.
    """

    def test_archived_bytes_survive_a_checkout_with_autocrlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            root = scratch / 'repo'
            root.mkdir()
            empty_settings = scratch / 'empty-git-settings'
            empty_settings.touch()
            # Personal attributes can otherwise mask a missing repository rule.
            environment = get_isolated_git_environment({
                'GIT_CONFIG_GLOBAL': str(empty_settings),
                'GIT_ATTR_NOSYSTEM': '1',
            })

            def git(*args):
                subprocess.run(['git', *args], cwd=root, check=True,
                               capture_output=True, env=environment)

            git('init', '-q', '--template=')
            git('config', 'core.attributesFile', str(empty_settings))
            git('config', 'user.email', 't@example.com')
            git('config', 'user.name', 't')
            (root / '.gitattributes').write_bytes(
                (Path(__file__).resolve().parents[2] / '.gitattributes').read_bytes())
            pinned = {
                'docs/benchmarks/raw/cpu-speaker-windows-20261022/RESULTS.md':
                    b'# result\nline two\n',
                'docs/benchmarks/raw/cpu-speaker-windows-20261022/run2/results.json':
                    b'{\n  "status": "complete"\n}\n',
            }
            control = 'autocrlf-control.txt'
            for relative, content in {**pinned, control: b'one\ntwo\n'}.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            git('-c', 'core.autocrlf=false', 'add', '-A')
            git('commit', '-qm', 'fixture')

            git('config', 'core.autocrlf', 'true')
            for relative in [*pinned, control]:
                (root / relative).unlink()
            git('checkout', '--', '.')
            for relative, content in pinned.items():
                self.assertEqual(content, (root / relative).read_bytes(),
                                 '%s changed bytes under autocrlf=true' % relative)
            self.assertEqual(b'one\r\ntwo\r\n', (root / control).read_bytes(),
                             'the harness must actually exercise CRLF conversion')


if __name__ == '__main__':
    unittest.main()
