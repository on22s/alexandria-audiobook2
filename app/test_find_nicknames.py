import io
import json
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

    def test_near_miss_canonical_is_reported_but_mapping_kept(self):
        parsed, output = _parse({"SUBARU": "BEATRICES"})
        self.assertEqual({"SUBARU": "BEATRICES"}, parsed)
        self.assertIn("[near-miss] canonical 'BEATRICES'", output)
        self.assertIn("closest is 'BEATRICE'", output)

    def test_novel_canonical_without_near_miss_is_silent(self):
        parsed, output = _parse({"SUBARU": "NATSUKI"})
        self.assertEqual({"SUBARU": "NATSUKI"}, parsed)
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
