"""HTTP translation preview: pairs list, create/read proposal, stale flag."""
from contextlib import ExitStack
import json
import tempfile
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


if __name__ == "__main__":
    unittest.main()
