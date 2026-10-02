"""Rendered platform commands and native uv URL-hash enforcement on a CPU wheel."""
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlsplit
import zipfile

import tests.test_launcher_attention_python as attention

REVISION = '9756d71ff0d90075c1dc5c7e3108a97cc4479924'
HASHES = {
    'sageattention-2.1.1+cu128torch2.7.1-cp310-cp310-win_amd64.whl':
        'fde6a193b0a6101f3de8636819fbb0d4fcc53949c4845cbc041e9ccb19c56287',
    'flash_attn-2.8.2+cu128torch2.7-cp310-cp310-win_amd64.whl':
        '945ae2b3f140683406f07c8064d474b394063453031602adc5f5773f1d2d10f5',
    'sageattention-2.1.1+cu128torch2.7.1-cp310-cp310-linux_x86_64.whl':
        '53982ea8e5c4ee0d7dc3ef17319fbc3857e851e67776ced853439af8721851a6',
    'flash_attn-2.8.3+cu128torch2.7-cp310-cp310-linux_x86_64.whl':
        '6eb77a0b30963f1164df35dffdb347d299b0096220c6703cdc57a2e49208ba2c',
}


class LauncherAttentionPinTests(unittest.TestCase):
    def test_rendered_optional_downloads_name_exact_revision_and_hash(self):
        render = attention.LauncherAttentionPythonTests().commands
        observed = set()
        for platform in ('win32', 'linux'):
            for option in ('sageattention', 'flashattention'):
                commands = render(platform, **{option: True})
                urls = [word for command in commands for word in shlex.split(command)
                        if word.startswith('https://huggingface.co/')]
                self.assertEqual(1, len(urls))
                parsed = urlsplit(urls[0])
                self.assertEqual('huggingface.co', parsed.netloc)
                self.assertTrue(parsed.path.startswith(f'/cocktailpeanut/wheels/resolve/{REVISION}/'))
                name = unquote(parsed.path.rsplit('/', 1)[1])
                self.assertEqual('sha256=' + HASHES[name], parsed.fragment)
                observed.add(name)
            self.assertFalse(any('huggingface.co' in c for c in render(platform)))
        self.assertEqual(set(HASHES), observed)

    def test_native_uv_rejects_wrong_url_hash_before_installing_fixture(self):
        uv = shutil.which('uv')
        self.assertIsNotNone(uv, 'uv is required for the native launcher hash fixture')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = root / 'attention_fixture-0.0.1-py3-none-any.whl'
            metadata = 'attention_fixture-0.0.1.dist-info'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('attention_fixture.py', 'VALUE = "known CPU fixture"\n')
                archive.writestr(metadata + '/METADATA',
                                 'Metadata-Version: 2.1\nName: attention-fixture\nVersion: 0.0.1\n')
                archive.writestr(metadata + '/WHEEL',
                                 'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                archive.writestr(metadata + '/RECORD', '')
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            class Handler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for valid in (False, True):
                    target = root / ('good' if valid else 'bad')
                    url = f'http://127.0.0.1:{server.server_port}/{wheel.name}#sha256={digest if valid else "0" * 64}'
                    result = subprocess.run(
                        [uv, '--no-config', '--no-cache', 'pip', 'install', '--python', sys.executable,
                         '--target', str(target), '--no-deps', url],
                        env={**os.environ, 'UV_NO_PROGRESS': '1'}, capture_output=True, text=True, timeout=20)
                    if valid:
                        self.assertEqual(0, result.returncode, result.stderr)
                        self.assertEqual('VALUE = "known CPU fixture"\n',
                                         (target / 'attention_fixture.py').read_text())
                    else:
                        self.assertNotEqual(0, result.returncode)
                        self.assertIn('hash mismatch', result.stderr.lower())
                        self.assertFalse((target / 'attention_fixture.py').exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
