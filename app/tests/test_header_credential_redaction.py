import json
from pathlib import Path
import tempfile
import unittest

import diagnostics
import verify_release


class HeaderCredentialRedactionTests(unittest.TestCase):
    def test_raw_headers_scrub_all_cookie_pairs_and_basic_credentials(self):
        cases = ('Authorization: Basic dXNlcjpwYXNz', 'authorization: basic YTo=',
                 'Proxy-Authorization: Basic cHJveHk6cGFzcw==',
                 'Cookie: session=opaque-session; refresh=opaque-refresh; theme=dark',
                 'Set-Cookie: session=opaque-session; HttpOnly; Expires=Wed, 01 Oct 2026 01:00:00 GMT')
        for value in cases:
            with self.subTest(value=value):
                for out in (diagnostics.get_redacted_credentials(value), diagnostics.redact_text(value),
                            verify_release.get_concise_error(ValueError(value))):
                    for fragment in ('dXNlcjpwYXNz', 'YTo=', 'cHJveHk6cGFzcw==', 'opaque-session', 'opaque-refresh'):
                        self.assertNotIn(fragment, out)
                    self.assertIn('[REDACTED]', out)

    def test_json_and_escaped_json_keep_valid_structure_and_safe_context(self):
        for key, value in (('Authorization', 'Basic dXNlcjpwYXNz'),
                           ('Cookie', 'session=opaque-session; refresh=opaque-refresh'),
                           ('Set-Cookie', 'session=opaque-session; Expires=Wed, 01 Oct 2026')):
            raw = json.dumps({key: value, 'safe': 'keep me'})
            with self.subTest(key=key):
                for escaped in (False, True):
                    text = json.dumps(raw) if escaped else raw
                    cleaned = diagnostics.get_redacted_credentials(text)
                    decoded = json.loads(json.loads(cleaned)) if escaped else json.loads(cleaned)
                    self.assertEqual({key: '[REDACTED]', 'safe': 'keep me'}, decoded)

    def test_multiline_headers_keep_following_nonsecret_lines_and_saved_bundle(self):
        text = ('request headers\r\nAuthorization: Basic dXNlcjpwYXNz\r\n'
                'Cookie: session=opaque-session; refresh=opaque-refresh\r\nContent-Type: text/plain\r\nstatus=200')
        clean = diagnostics.get_redacted_credentials(text)
        self.assertIn('Content-Type: text/plain\r\nstatus=200', clean)
        self.assertEqual(text.count('\r\n'), clean.count('\r\n'))
        source = {'error': text, 'hash': 'f' * 64}
        before = json.dumps(source)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'diagnostics.json'
            path.write_text(json.dumps(diagnostics.build_diagnostics(source)))
            saved = json.loads(path.read_text())
            for fragment in ('dXNlcjpwYXNz', 'opaque-session', 'opaque-refresh'):
                self.assertNotIn(fragment, json.dumps(saved))
            self.assertEqual('f' * 64, saved['sections']['hash'])
        self.assertEqual(before, json.dumps(source))

    def test_ordinary_prose_and_noncredential_fields_remain_unchanged(self):
        for text in ('Basic configuration works', 'cookie recipes are useful',
                     '{"model":"gemma", "count":3}', 'a' * 40, 'f' * 64):
            self.assertEqual(text, diagnostics.get_redacted_credentials(text))
