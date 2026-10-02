"""Exact UTF-8 budget equivalence and bounded serialization work."""
import copy
import json
import random
import unittest
from unittest.mock import patch

import diagnostics


class DiagnosticSizingTests(unittest.TestCase):
    def test_oversized_bundle_is_serialized_whole_only_once(self):
        original = json.dumps
        whole = []
        def record(value, *args, **kwargs):
            if isinstance(value, dict) and 'sections' in value:
                whole.append(value)
            return original(value, *args, **kwargs)
        with patch.object(diagnostics, 'MAX_TOTAL_BYTES', 1000), \
                patch.object(diagnostics.json, 'dumps', side_effect=record):
            result = diagnostics.build_diagnostics({str(i): '音' * 250 for i in range(12)})
        self.assertLessEqual(len(original(result, ensure_ascii=False).encode()), 1000)
        self.assertGreater(sum(str(v).startswith('[omitted:') for v in result['sections'].values()), 8)
        self.assertEqual(1, len(whole))

    def test_exact_legacy_omission_order_and_byte_boundaries(self):
        rng = random.Random(514)
        for case in range(100):
            sections = {str(i): {'text': rng.choice(('音', 'é', 'x')) * rng.randint(0, 300),
                                 'api_key': 'fixture secret', 'list': [None, True, 2]}
                        for i in range(rng.randint(1, 15))}
            budget = rng.choice((100, 600, 1000, 2000, 10000))
            with patch.object(diagnostics, 'MAX_TOTAL_BYTES', budget):
                actual = diagnostics.build_diagnostics(sections)
                expected = copy.deepcopy(actual)
                expected['sections'] = {name: diagnostics.redact(value) for name, value in sections.items()}
                def size(value):
                    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
                if size(expected) > budget:
                    largest = sorted(expected['sections'].items(), key=lambda item: size(item[1]), reverse=True)
                    for name, _ in largest:
                        expected['sections'][name] = f'[omitted: exceeded {budget}-byte bundle budget]'
                        if size(expected) <= budget:
                            break
                with self.subTest(case=case, budget=budget):
                    self.assertEqual(expected, actual)
                    self.assertEqual(size(expected), size(actual))
                    self.assertNotIn('fixture secret', json.dumps(actual))
