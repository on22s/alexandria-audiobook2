"""Protect pinned vendor executable bytes and preserve local bundle ordering."""
import base64
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
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
