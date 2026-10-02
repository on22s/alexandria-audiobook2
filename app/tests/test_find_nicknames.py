from tests.test_support import assert_file_lock_released
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

import find_nicknames


SPEAKERS = ["NARRATOR", "BEATRICE", "SUBARU"]


def _parse(aliases):
    raw = json.dumps({"aliases": aliases, "evidence": {}})
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        parsed, _evidence = find_nicknames._parse_alias_response(raw, SPEAKERS)
    return parsed, buffer.getvalue()


class ParseAliasResponseTests(unittest.TestCase):
    def test_alias_free_discovery_preserves_human_registry(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "aliases.json")
            path.write_text(json.dumps({"Betty": "Beatrice"}))
            self.assertEqual({"Betty": "Beatrice"},
                             find_nicknames.save_discovered_aliases(str(path), {}))
            self.assertEqual({"Betty": "Beatrice"}, json.loads(path.read_text()))

    def test_discovery_merges_latest_registry_and_keeps_existing_conflicts(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "aliases.json")
            path.write_text(json.dumps({"Betty": "BEATRICE"}))
            result = find_nicknames.save_discovered_aliases(
                str(path), {"Betty": "BETTY", "Puck": "PACK"}, roster=["PACK"])
            self.assertEqual({"Betty": "BEATRICE", "Puck": "PACK"}, result)
            self.assertEqual(result, json.loads(path.read_text()))

    def test_discovery_refuses_to_replace_unreadable_human_registry(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "aliases.json")
            path.write_text('{broken')
            with self.assertRaisesRegex(ValueError, "valid JSON object"):
                find_nicknames.save_discovered_aliases(str(path), {"Puck": "PACK"})
            self.assertEqual('{broken', path.read_text())

    def test_exact_variant_resolves_case_insensitively(self):
        parsed, output = _parse({"Subaru": "BEATRICE"})
        self.assertEqual({"SUBARU": "BEATRICE"}, parsed)
        self.assertNotIn("[near-miss]", output)

    def test_near_miss_variant_is_dropped_but_reported(self):
        parsed, output = _parse({"BEATRICES": "SUBARU"})
        self.assertEqual({}, parsed)
        self.assertIn("[near-miss] variant 'BEATRICES'", output)
        self.assertIn("closest is 'BEATRICE'", output)
        self.assertIn("not merged", output)

    def test_clean_miss_variant_is_dropped_silently(self):
        parsed, output = _parse({"PUCK": "SUBARU"})
        self.assertEqual({}, parsed)
        self.assertNotIn("[near-miss]", output)

    def test_near_miss_canonical_is_reported_and_unapproved_mapping_dropped(self):
        parsed, output = _parse({"SUBARU": "BEATRICES"})
        self.assertEqual({}, parsed)
        self.assertIn("[near-miss] canonical 'BEATRICES'", output)
        self.assertIn("closest is 'BEATRICE'", output)

    def test_novel_canonical_without_near_miss_is_dropped(self):
        parsed, output = _parse({"SUBARU": "NATSUKI"})
        self.assertEqual({}, parsed)
        self.assertNotIn("[near-miss]", output)

    def test_punctuation_variant_resolves_via_shared_normalization(self):
        # "MR SMITH" and "MR. SMITH" share the same _identity_key (casefold +
        # strip non-word chars), so the shared resolver must match them even
        # though a plain .strip().lower() comparison would not.
        raw = json.dumps({"aliases": {"MR SMITH": "SUBARU"}, "evidence": {}})
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            parsed, _evidence = find_nicknames._parse_alias_response(
                raw, ["NARRATOR", "SUBARU", "MR. SMITH"])
        self.assertEqual({"MR. SMITH": "SUBARU"}, parsed)
        self.assertNotIn("[near-miss]", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()


class ConcurrentAliasPublicationTests(unittest.TestCase):
    def test_http_correction_and_discovery_share_lock_and_latest_human_values(self):
        import copy
        import threading
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import script
        from utils import file_lock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "aliases.json")
            path.write_text(json.dumps({"Betty": "OLD_ROOT", "Removed": "OLD"}))
            before = path.read_bytes()
            manual_input = {" Betty ": " HUMAN_ROOT ", "Human": "ROOT", " ": "discard"}
            discovered = {"Betty": "MODEL_ROOT", "Auto": "AUTO_ROOT"}
            original_inputs = copy.deepcopy((manual_input, discovered))
            manual_publisher_entered = threading.Event()
            release_manual = threading.Event()
            discovery_attempted = threading.Event()
            discovery_done = threading.Event()
            errors, responses, outputs = [], [], []
            original_write = script.atomic_json_write

            def pause_manual_publication(value, destination):
                self.assertEqual(str(path), destination)
                self.assertTrue(Path(str(path) + '.lock').is_file())
                manual_publisher_entered.set()
                if not release_manual.wait(3):
                    raise AssertionError('Manual fixture publication was not released')
                return original_write(value, destination)

            def observe_discovery_lock(destination):
                self.assertEqual(str(path), destination)
                discovery_attempted.set()
                return file_lock(destination)

            app = FastAPI()
            app.include_router(script.router)
            with patch.object(script, "CHARACTER_ALIASES_PATH", str(path)), \
                 patch.object(script, "atomic_json_write", side_effect=pause_manual_publication), \
                 patch.object(find_nicknames, "file_lock", side_effect=observe_discovery_lock), \
                 TestClient(app) as client:
                def manual_worker():
                    try:
                        responses.append(client.post('/api/character_aliases', json=manual_input))
                    except BaseException as error:
                        errors.append(error)

                def discovery_worker():
                    try:
                        outputs.append(find_nicknames.save_discovered_aliases(str(path), discovered, roster=["AUTO_ROOT"]))
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        discovery_done.set()

                manual = threading.Thread(target=manual_worker)
                discovery = threading.Thread(target=discovery_worker)
                try:
                    manual.start()
                    self.assertTrue(manual_publisher_entered.wait(3))
                    discovery.start()
                    self.assertTrue(discovery_attempted.wait(3))
                    self.assertFalse(discovery_done.wait(0.05), 'Discovery bypassed the held manual-publication lock')
                    self.assertEqual(before, path.read_bytes())
                    self.assertTrue(Path(str(path) + '.lock').is_file())
                finally:
                    release_manual.set()
                    for thread in (manual, discovery):
                        if thread.ident is not None:
                            thread.join(5)
                self.assertFalse(manual.is_alive())
                self.assertFalse(discovery.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(200, responses[0].status_code, responses[0].text)
            self.assertEqual({'status':'saved', 'count':2}, responses[0].json())
            expected = {'Betty':'HUMAN_ROOT', 'Human':'ROOT', 'Auto':'AUTO_ROOT'}
            self.assertEqual([expected], outputs)
            self.assertEqual(expected, json.loads(path.read_text()))
            assert_file_lock_released(str(path))
            self.assertEqual(original_inputs, (manual_input, discovered))
