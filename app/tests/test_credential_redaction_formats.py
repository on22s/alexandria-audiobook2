"""Known credential failures exercise saved support and release artifacts."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import verify_release


class CredentialFormatTests(unittest.TestCase):
    def test_hyphenated_keys_are_scrubbed_without_mutating_input(self):
        raw = {'headers': {'x-api-key': 'opaque-value', 'Private-Key': 'private material',
                           'model-name': 'gemma'}, 'safe': 17}
        before = json.dumps(raw)
        result = diagnostics.build_diagnostics(raw)
        self.assertEqual('[REDACTED]', result['sections']['headers']['x-api-key'])
        self.assertEqual('[REDACTED]', result['sections']['headers']['Private-Key'])
        self.assertEqual('gemma', result['sections']['headers']['model-name'])
        self.assertEqual(before, json.dumps(raw))

    def test_json_and_escaped_json_credentials_are_fully_scrubbed(self):
        plain = json.dumps({'api_key': 'opaque credential, with spaces', 'safe': 'keep me'})
        cases = [plain, json.dumps(plain), 'request '+plain+' failed']
        for value in cases:
            with self.subTest(value=value):
                out = diagnostics.redact_text(value)
                self.assertNotIn('opaque', out)
                self.assertNotIn('with spaces', out)
                self.assertIn('keep me', out)
        self.assertEqual('[REDACTED]', json.loads(diagnostics.redact_text(plain))['api_key'])
        self.assertEqual('[REDACTED]', json.loads(json.loads(diagnostics.redact_text(json.dumps(plain))))['api_key'])

    def test_quoted_escaped_and_unterminated_secrets_keep_only_safe_context(self):
        cases = ["password='correct horse battery staple' timeout",
                 'secret="correct \\"horse\\" battery staple" timeout',
                 "token='correct horse battery staple",
                 'password="correct horse\nbattery staple" timeout',
                 'token="correct horse" password=opaque-value timeout']
        for value in cases:
            with self.subTest(value=value):
                for out in (diagnostics.redact_text(value), verify_release.get_concise_error(ValueError(value))):
                    self.assertNotIn('correct', out)
                    self.assertNotIn('battery', out)
                    self.assertNotIn('opaque-value', out)
                    self.assertIn('[REDACTED]', out)
        self.assertEqual('safe message second', verify_release.get_concise_error(ValueError('safe message second\nlater')))

    def test_saved_release_report_and_bundle_have_no_secret_tail(self):
        secret = 'correct horse battery staple'
        message = "password='"+secret+"' failed with "+json.dumps({'api_key': 'opaque credential'})
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp)/'release.json'
            with patch.object(verify_release, 'compile_python_files', side_effect=ValueError(message)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(1, verify_release.main(['--json-report',str(report_path)]))
            bundle_path = Path(tmp)/'diagnostics.json'
            bundle_path.write_text(json.dumps(diagnostics.build_diagnostics({'error': message})))
            for p in (report_path,bundle_path):
                artifact=json.loads(p.read_text())
                rendered=json.dumps(artifact)
                for fragment in ('correct','horse','battery','staple','opaque credential'):
                    self.assertNotIn(fragment,rendered)
                self.assertIn('[REDACTED]',rendered)

    def test_safe_json_prose_and_hashes_remain_unchanged(self):
        for value in ('{"model":"gemma", "count":3}', 'ordinary prose', 'a'*40, 'f'*64):
            self.assertEqual(value, diagnostics.redact_text(value))
