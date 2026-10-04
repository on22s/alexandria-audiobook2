"""Protect pinned vendor executable bytes and preserve local bundle ordering."""
import base64
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('CDN_HTML_SOURCE', ROOT / 'app/static/index.html'))


class Scripts(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.scripts = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.scripts.append(dict(attrs))


class FrontendCdnIntegrityTests(unittest.TestCase):
    def test_windows_style_checkout_preserves_vendor_script_integrity(self):
        vendors = [s for s in Scripts(SOURCE.read_text()).scripts
                   if s.get('src', '').startswith('/static/vendor/')]
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
            env.pop(key, None)
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)

            def git(*args):
                return subprocess.run(['git', '-C', tmp, *args], env=env,
                                      check=True, capture_output=True, timeout=15)

            git('init')
            git('config', 'core.autocrlf', 'false')
            attributes = checkout / '.gitattributes'
            attributes.write_bytes((ROOT / '.gitattributes').read_bytes())
            paths = []
            for script in vendors:
                path = Path('app') / script['src'].lstrip('/')
                paths.append(path.as_posix())
                target = checkout / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / path).read_bytes())
            git('add', '-f', '.gitattributes', *paths)
            git('config', 'core.autocrlf', 'true')
            for path in paths:
                (checkout / path).unlink()
            git('checkout-index', '--force', '--', *paths)
            for script, path in zip(vendors, paths):
                with self.subTest(src=script['src']):
                    digest = hashlib.sha384((checkout / path).read_bytes()).digest()
                    self.assertEqual(base64.b64decode(script['integrity'][7:]), digest)

            # Prove this fixture actually exercises conversion when protection is removed.
            attributes.write_text('')
            bootstrap = checkout / paths[0]
            bootstrap.unlink()
            git('checkout-index', '--force', '--', paths[0])
            self.assertIn(b'\r\n', bootstrap.read_bytes())
            self.assertNotEqual(base64.b64decode(vendors[0]['integrity'][7:]),
                                hashlib.sha384(bootstrap.read_bytes()).digest())

            # Existing users need changed blobs, not just new checkout attributes.
            git('config', 'core.autocrlf', 'false')
            for path in paths:
                (checkout / path).write_bytes((ROOT / path).read_bytes()[:-1])
            git('add', '-f', '.gitattributes', *paths)
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                'commit', '-m', 'Unprotected vendor files')
            old = git('rev-parse', 'HEAD').stdout.decode().strip()
            attributes.write_bytes((ROOT / '.gitattributes').read_bytes())
            for path in paths:
                (checkout / path).write_bytes((ROOT / path).read_bytes())
            git('add', '-f', '.gitattributes', *paths)
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                'commit', '-m', 'Protect and refresh vendor files')
            new = git('rev-parse', 'HEAD').stdout.decode().strip()
            git('config', 'core.autocrlf', 'true')
            git('checkout', old)
            self.assertIn(b'\r\n', bootstrap.read_bytes())
            git('checkout', new)
            for script, path in zip(vendors, paths):
                with self.subTest(upgraded=script['src']):
                    self.assertEqual(base64.b64decode(script['integrity'][7:]),
                                     hashlib.sha384((checkout / path).read_bytes()).digest())

    def test_every_vendor_script_has_exact_version_sha384_and_cors(self):
        scripts = Scripts(SOURCE.read_text()).scripts
        external = [s for s in scripts if urlsplit(s.get('src', '')).netloc]
        self.assertEqual([], external)
        vendors = [s for s in scripts if s.get('src', '').startswith('/static/vendor/')]
        self.assertEqual([
            '/static/vendor/bootstrap-5.3.0/bootstrap.bundle.min.js',
            '/static/vendor/marked-12.0.2/marked.umd.min.js',
            '/static/vendor/dompurify-3.4.16/purify.min.js',
        ], [s['src'] for s in vendors])
        for script in vendors:
            with self.subTest(src=script['src']):
                self.assertEqual('anonymous', script.get('crossorigin'))
                integrity = script.get('integrity', '')
                self.assertTrue(integrity.startswith('sha384-'))
                self.assertEqual(hashlib.sha384((ROOT / 'app' / script['src'].lstrip('/')).read_bytes()).digest(),
                                 base64.b64decode(integrity[7:], validate=True))
        sources = [s['src'] for s in scripts if 'src' in s]
        self.assertEqual([s['src'] for s in vendors], sources[:3])
        self.assertTrue(all(src.startswith('/static/js/') for src in sources[3:]))
