"""Voice-config remap is order-independent for chains and fails loud on cycles."""
import copy
import itertools
import json
import unittest

import review_script as review


def _cfg(values):
    return {name: {'v': value} for name, value in values.items()}


class VoiceConfigRemapTests(unittest.TestCase):
    def test_three_chain_all_permutations_converge(self):
        values = {'A': 'a', 'B': 'b', 'C': 'c'}
        for order in itertools.permutations([('A', 'B'), ('B', 'C')]):
            with self.subTest(order=order):
                cfg = _cfg(values)
                before = copy.deepcopy(cfg)
                result, moved = review._remapped_voice_config(cfg, dict(order))
                self.assertEqual({'C': {'v': 'c'}}, result)
                self.assertEqual(2, moved)
                self.assertEqual(before, cfg, 'caller config must be left untouched')

    def test_four_chain_all_permutations_converge(self):
        values = {'A': 'a', 'B': 'b', 'C': 'c', 'D': 'd'}
        for order in itertools.permutations([('A', 'B'), ('B', 'C'), ('C', 'D')]):
            with self.subTest(order=order):
                cfg = _cfg(values)
                before = copy.deepcopy(cfg)
                result, moved = review._remapped_voice_config(cfg, dict(order))
                self.assertEqual({'D': {'v': 'd'}}, result)
                self.assertEqual(3, moved)
                self.assertEqual(before, cfg, 'caller config must be left untouched')

    def test_merge_precedence_when_root_exists(self):
        cfg = {'A': {'voice': 'va', 'extra': 1}, 'B': {'voice': 'vb'}}
        before = copy.deepcopy(cfg)
        result, moved = review._remapped_voice_config(cfg, {'A': 'B'})
        self.assertEqual({'B': {'voice': 'vb', 'extra': 1}}, result)
        self.assertEqual('{"B": {"extra": 1, "voice": "vb"}}', json.dumps(result, sort_keys=True))
        self.assertEqual(1, moved)
        self.assertEqual(before, cfg, 'caller config must be left untouched')

    def test_input_and_nested_values_are_never_mutated(self):
        # Merging A's extra key into an existing B used to write through the
        # shallow copy into cfg['B'] itself. Keep a fixture that discriminates
        # the Rule 17 fix: the returned config has the merged value, while the
        # caller's dict and its nested dicts stay byte-for-byte identical.
        cfg = {'A': {'voice': 'va', 'extra': 1}, 'B': {'voice': 'vb'}}
        original_a = copy.deepcopy(cfg['A'])
        original_b = copy.deepcopy(cfg['B'])
        before = copy.deepcopy(cfg)
        result, moved = review._remapped_voice_config(cfg, {'A': 'B'})
        self.assertEqual(1, moved)
        self.assertEqual({'voice': 'vb', 'extra': 1}, result['B'])
        self.assertEqual(before, cfg)
        self.assertEqual(original_a, cfg['A'])
        self.assertEqual(original_b, cfg['B'])
        self.assertIsNot(result['B'], cfg['B'], 'returned nested values must not be shared')

    def test_cycles_raise_value_error_naming_the_cycle(self):
        for mapping, names in (
                ({'A': 'B', 'B': 'A'}, ('A', 'B')),
                ({'A': 'B', 'B': 'C', 'C': 'A'}, ('A', 'B', 'C'))):
            with self.subTest(mapping=mapping):
                cfg = _cfg({'A': 'a', 'B': 'b', 'C': 'c'})
                before = copy.deepcopy(cfg)
                with self.assertRaises(ValueError) as context:
                    review._remapped_voice_config(cfg, mapping)
                message = str(context.exception)
                self.assertIn('cycle', message.lower())
                for name in names:
                    self.assertIn(name, message)
                self.assertEqual(before, cfg,
                                 'a detected cycle must not leave a half-applied remap')

    def test_flattened_map_matches_main_byte_for_byte(self):
        cfg = _cfg({'A': 'a', 'B': 'b', 'C': 'c'})
        before = copy.deepcopy(cfg)
        result, moved = review._remapped_voice_config(cfg, {'A': 'C', 'B': 'C'})
        self.assertEqual('{"C": {"v": "c"}}', json.dumps(result, sort_keys=True))
        self.assertEqual(2, moved)
        self.assertEqual(before, cfg, 'caller config must be left untouched')


if __name__ == '__main__':
    unittest.main()
