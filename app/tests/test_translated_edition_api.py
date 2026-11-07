"""HTTP translation preview: pairs list, create/read proposal, stale flag."""
import asyncio
from contextlib import ExitStack
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import translated_edition as router_module
from tests.test_translated_edition import PAIR, get_book, get_bytes


class TranslatedEditionAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        get_book(self.root, [{"speaker": "ALICE", "text": "Synthetic line one."},
                             {"speaker": "BOB", "text": "Synthetic line two."}])
        self.before = get_bytes(self.root)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(router_module, "DATA_DIR", str(self.root)))
        stack.enter_context(patch.object(
            router_module.translated_edition, "get_installed_pairs",
            lambda: {"available": True, "reason": "ready", "error": None,
                     "pairs": [PAIR], "runtime_version": "1.9.0"}))
        stack.enter_context(patch.object(
            router_module.translated_edition, "build_argos_translator",
            lambda from_code, to_code: (lambda text: "FR: " + text)))
        app = FastAPI()
        app.include_router(router_module.router)
        self.client = stack.enter_context(TestClient(app, raise_server_exceptions=False))

    def test_pairs_create_and_read_round_trip(self):
        pairs = self.client.get('/api/translated_edition/pairs')
        self.assertEqual(200, pairs.status_code, pairs.text)
        self.assertEqual([PAIR], pairs.json()["pairs"])
        created = self.client.post('/api/translated_edition/proposals',
                                   json={"source_language": "en",
                                         "target_language": "fr"})
        self.assertEqual(200, created.status_code, created.text)
        proposal = created.json()
        self.assertEqual("complete", proposal["status"])
        self.assertEqual("proposal, not approved", proposal["label"])
        self.assertFalse(proposal["stale"])
        self.assertEqual("1.9.0", proposal["runtime_version"])
        self.assertEqual("1.0", proposal["package_version"])
        read = self.client.get(
            f"/api/translated_edition/proposals/{proposal['proposal_id']}")
        self.assertEqual(200, read.status_code, read.text)
        self.assertEqual(proposal["proposal_id"], read.json()["proposal_id"])
        self.assertEqual(self.before, get_bytes(self.root))

    def test_stale_flag_after_script_change(self):
        proposal = self.client.post('/api/translated_edition/proposals', json={
            "source_language": "en", "target_language": "fr"}).json()
        (self.root / "annotated_script.json").write_text(
            json.dumps([{"speaker": "ALICE", "text": "Changed synthetic line."}]),
            encoding="utf-8")
        read = self.client.get(
            f"/api/translated_edition/proposals/{proposal['proposal_id']}")
        self.assertEqual(200, read.status_code, read.text)
        self.assertTrue(read.json()["stale"])

    def test_unsupported_pair_and_bad_id_are_refused(self):
        response = self.client.post('/api/translated_edition/proposals', json={
            "source_language": "en", "target_language": "de"})
        self.assertEqual(409, response.status_code, response.text)
        self.assertIn("not installed", response.json()["detail"])
        missing = self.client.get('/api/translated_edition/proposals/' + '0' * 32)
        self.assertEqual(409, missing.status_code, missing.text)
        self.assertIn("not found", missing.json()["detail"])
        malformed = self.client.get('/api/translated_edition/proposals/../../state')
        self.assertIn(malformed.status_code, (409, 404))

    def test_blocking_translation_runs_off_the_event_loop_thread(self):
        seen = {}

        def translate(text):
            seen["on_main_thread"] = threading.current_thread() is threading.main_thread()
            return "FR: " + text

        with patch.object(router_module.translated_edition, "build_argos_translator",
                          lambda from_code, to_code: translate):
            request = router_module.ProposalRequest(
                source_language="en", target_language="fr")
            proposal = asyncio.run(
                router_module.create_translated_edition_proposal(request))
        self.assertEqual("complete", proposal["status"])
        self.assertEqual(False, seen["on_main_thread"])

    def test_cancel_endpoint_reaches_a_running_preview(self):
        token = "a" * 32
        event = router_module._begin_preview(token)
        try:
            response = self.client.post(
                f'/api/translated_edition/proposals/{token}/cancel')
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()["cancelled"])
            self.assertTrue(event.is_set())
        finally:
            router_module._end_preview(token, event)
        self.assertIsNone(router_module._CANCEL_TOKENS.get(token))
        gone = self.client.post(f'/api/translated_edition/proposals/{token}/cancel')
        self.assertEqual(200, gone.status_code, gone.text)
        self.assertFalse(gone.json()["cancelled"])
        bad = self.client.post('/api/translated_edition/proposals/not-hex/cancel')
        self.assertEqual(409, bad.status_code, bad.text)
        malformed = self.client.post('/api/translated_edition/proposals', json={
            "source_language": "en", "target_language": "fr",
            "cancel_token": "not-a-token"})
        self.assertEqual(422, malformed.status_code, malformed.text)

    def test_cancel_token_stops_the_build_and_is_saved_as_cancelled(self):
        token = "b" * 32

        def translate_and_cancel(text):
            router_module._cancel_preview(token)
            return "FR: " + text

        with patch.object(router_module.translated_edition, "build_argos_translator",
                          lambda from_code, to_code: translate_and_cancel):
            created = self.client.post('/api/translated_edition/proposals', json={
                "source_language": "en", "target_language": "fr",
                "cancel_token": token})
        self.assertEqual(200, created.status_code, created.text)
        proposal = created.json()
        self.assertEqual("cancelled", proposal["status"])
        self.assertEqual(["translated", "cancelled"],
                         [row["status"] for row in proposal["rows"]])
        saved = (self.root / "translation_proposals" / (proposal["proposal_id"] + ".json"))
        self.assertEqual("cancelled", json.loads(saved.read_text())["status"])
        self.assertIsNone(router_module._CANCEL_TOKENS.get(token))

    def test_duplicate_cancel_token_is_refused_while_running(self):
        token = "c" * 32
        event = router_module._begin_preview(token)
        try:
            duplicate = self.client.post('/api/translated_edition/proposals', json={
                "source_language": "en", "target_language": "fr",
                "cancel_token": token})
            self.assertEqual(409, duplicate.status_code, duplicate.text)
            self.assertIn("already running", duplicate.text)
        finally:
            router_module._end_preview(token, event)
        self.assertIsNone(router_module._CANCEL_TOKENS.get(token))
        # The same token is free again once the first preview has ended.
        retry = router_module._begin_preview(token)
        router_module._end_preview(token, retry)

    def test_busy_book_during_stale_recheck_is_a_refusal_not_a_500(self):
        proposal = self.client.post('/api/translated_edition/proposals', json={
            "source_language": "en", "target_language": "fr"}).json()
        module = router_module.translated_edition
        real_ensure = module.ensure_book_state
        calls = []

        def busy_second(data_dir, *args, **kwargs):
            calls.append(data_dir)
            if len(calls) == 2:
                raise TimeoutError("fixture lock busy")
            return real_ensure(data_dir, *args, **kwargs)

        with patch.object(module, 'ensure_book_state', side_effect=busy_second):
            read = self.client.get(
                f"/api/translated_edition/proposals/{proposal['proposal_id']}")
        self.assertEqual(409, read.status_code, read.text)
        self.assertIn("busy", read.json()["detail"])


if __name__ == "__main__":
    unittest.main()
