"""Verify pinned UI dependencies and fonts through the real static-file server."""
import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parent.parent / 'static'


class Resources(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.resources = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.resources.append((attrs['src'], attrs.get('integrity')))
        if tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.resources.append((attrs['href'], attrs.get('integrity')))


class LocalUiAssetTests(unittest.TestCase):
    def test_page_dependencies_are_local_and_integrity_matches_served_bytes(self):
        resources = Resources((STATIC / 'index.html').read_text()).resources
        # Pin every original asset, reject duplicates/unknown additions, and
        # allow the reviewed feature scripts to merge independently.
        base_assets = {
            '/static/js/app-core.js?v=__APP_BUILD__',
            '/static/js/app-help.js?v=__APP_BUILD__',
            '/static/js/app-reports.js?v=__APP_BUILD__',
            '/static/js/app-scripts.js?v=__APP_BUILD__',
            '/static/js/app-speaker-review.js?v=__APP_BUILD__',
            '/static/js/app-training.js?v=__APP_BUILD__',
            '/static/js/app-voicelab.js?v=__APP_BUILD__',
            '/static/js/app-workbench.js?v=__APP_BUILD__',
            '/static/vendor/bootstrap-5.3.0/bootstrap.bundle.min.js',
            '/static/vendor/bootstrap-5.3.0/bootstrap.min.css',
            '/static/vendor/dompurify-3.4.16/purify.min.js',
            '/static/vendor/font-awesome-6.0.0/css/all.min.css',
            '/static/vendor/marked-12.0.2/marked.umd.min.js',
        }
        feature_assets = {'/static/js/app-settings.js?v=__APP_BUILD__',
                          '/static/js/app-editor-repair.js?v=__APP_BUILD__',
                          '/static/js/app-workspace.js?v=__APP_BUILD__',
                          '/static/js/app-editor-review.js?v=__APP_BUILD__'}
        urls = [url for url, _ in resources]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(base_assets.issubset(urls))
        self.assertFalse(set(urls) - base_assets - feature_assets)
        self.assertIn(('/static/js/app-speaker-review.js?v=__APP_BUILD__', None), resources)
        self.assertIn(('/static/js/app-help.js?v=__APP_BUILD__', None), resources)
        self.assertIn(('/static/js/app-settings.js?v=__APP_BUILD__', None), resources)
        app = FastAPI()
        app.mount('/static', StaticFiles(directory=STATIC), name='static')
        with TestClient(app) as client:
            for url, integrity in resources:
                with self.subTest(url=url):
                    self.assertTrue(url.startswith('/static/'), url)
                    response = client.get(url)
                    self.assertEqual(200, response.status_code)
                    self.assertTrue(response.content)
                    if integrity:
                        actual = base64.b64encode(hashlib.sha384(response.content).digest()).decode()
                        self.assertEqual('sha384-' + actual, integrity)
            self.assertEqual(404, client.get('/static/vendor/missing-font.woff2').status_code)

    def test_all_referenced_fonts_and_pinned_provenance_files_are_delivered(self):
        vendor = STATIC / 'vendor'
        manifest = json.loads((vendor / 'provenance.json').read_text())['assets']
        self.assertEqual(17, len(manifest))
        app = FastAPI()
        app.mount('/static', StaticFiles(directory=STATIC), name='static')
        with TestClient(app) as client:
            for row in manifest:
                with self.subTest(file=row['file']):
                    response = client.get('/static/vendor/' + row['file'])
                    self.assertEqual(200, response.status_code)
                    self.assertEqual(row['bytes'], len(response.content))
                    self.assertEqual(row['sha256'], hashlib.sha256(response.content).hexdigest())
            css = vendor / 'font-awesome-6.0.0/css/all.min.css'
            fonts = set(re.findall(r'url\(["\']?(\.\./webfonts/[^)"\']+)', css.read_text()))
            self.assertEqual(8, len(fonts))
            for font in fonts:
                path = (css.parent / font).resolve()
                self.assertTrue(path.is_relative_to(vendor.resolve()))
                response = client.get('/static/' + path.relative_to(STATIC).as_posix())
                self.assertEqual(200, response.status_code)
                self.assertTrue(response.content)
