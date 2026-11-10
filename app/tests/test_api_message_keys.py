"""AppError adds a translatable key and leaves every English detail byte intact.

Design section 4.3: the browser needs a key, and every existing test, log line
and non-browser client needs the same English sentence. These tests pin both
halves on the real exception handler, and one test proves a plain
HTTPException still goes through FastAPI's untouched default handler so the
800+ unconverted raise sites cannot change shape by accident.
"""
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import core


def build_app():
    api = FastAPI()
    api.add_exception_handler(core.AppError, core.handle_app_error)

    @api.get('/app-error')
    def app_error():
        raise core.AppError(409, "Active book changed; reload voices before saving",
                            key='safety.voice.book_changed_before_save')

    @api.get('/app-error-values')
    def app_error_values():
        raise core.AppError(400, "Script is already running.",
                            key='error.task.already_running', values={'task': 'Script'})

    @api.get('/app-error-headers')
    def app_error_headers():
        raise core.AppError(401, "Authentication required.",
                            key='error.auth.required', headers={'WWW-Authenticate': 'Basic'})

    @api.get('/http-exception')
    def http_exception():
        raise HTTPException(409, detail="Plain refusal.")

    return api


class AppErrorHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_app())

    def test_the_body_keeps_the_exact_english_detail_and_adds_only_the_key(self):
        response = self.client.get('/app-error')
        self.assertEqual(409, response.status_code)
        self.assertEqual({"detail": "Active book changed; reload voices before saving",
                          "message_key": "safety.voice.book_changed_before_save"},
                         response.json())

    def test_message_values_are_sent_when_present_and_omitted_when_empty(self):
        self.assertEqual({"detail": "Script is already running.",
                          "message_key": "error.task.already_running",
                          "message_values": {"task": "Script"}},
                         self.client.get('/app-error-values').json())
        self.assertNotIn('message_values', self.client.get('/app-error').json())

    def test_the_english_detail_is_still_a_substring_of_the_raw_body(self):
        """The 198 assertIn(... detail) assertions keep working because of this."""
        body = self.client.get('/app-error').text
        self.assertIn('"detail":"Active book changed; reload voices before saving"', body)

    def test_headers_from_the_refusal_survive_the_handler(self):
        response = self.client.get('/app-error-headers')
        self.assertEqual(401, response.status_code)
        self.assertEqual('Basic', response.headers['www-authenticate'])

    def test_a_plain_http_exception_keeps_fastapis_default_body(self):
        self.assertEqual({"detail": "Plain refusal."},
                         self.client.get('/http-exception').json())

    def test_app_error_is_an_http_exception_so_unmigrated_code_still_catches_it(self):
        self.assertTrue(issubclass(core.AppError, HTTPException))
        error = core.AppError(400, "Refused.", key='error.test.refused')
        self.assertEqual("Refused.", error.detail)
        self.assertEqual({}, error.message_values)

    def test_the_application_registers_the_handler_for_app_error(self):
        import app as application
        self.assertIn(core.AppError, application.app.exception_handlers)
        self.assertEqual(core.handle_app_error,
                         application.app.exception_handlers[core.AppError])


if __name__ == '__main__':
    unittest.main()
