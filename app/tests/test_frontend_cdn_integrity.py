"""Protect all external executable scripts, preserving local bundle ordering."""
import base64
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
    def test_every_external_script_has_exact_version_sha384_and_cors(self):
        scripts = Scripts(SOURCE.read_text()).scripts
        external = [s for s in scripts if urlsplit(s.get('src', '')).netloc]
        self.assertEqual(3, len(external))
        for script in external:
            with self.subTest(src=script['src']):
                self.assertEqual('https', urlsplit(script['src']).scheme)
                self.assertRegex(script['src'], r'@[0-9]+\.[0-9]+\.[0-9]+/')
                self.assertEqual('anonymous', script.get('crossorigin'))
                integrity = script.get('integrity', '')
                self.assertTrue(integrity.startswith('sha384-'))
                self.assertEqual(48, len(base64.b64decode(integrity[7:], validate=True)))
        sources = [s['src'] for s in scripts if 'src' in s]
        self.assertEqual([s['src'] for s in external], sources[:3])
        self.assertTrue(all(src.startswith('/static/js/') for src in sources[3:]))
