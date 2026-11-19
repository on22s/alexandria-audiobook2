"""Voice-config remap is order-independent for chains and fails loud on cycles."""
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
                review._remapped_voice_config(cfg, dict(order))
                self.assertEqual({'C': {'v': 'c'}}, cfg)

    def test_four_chain_all_permutations_converge(self):
        values = {'A': 'a', 'B': 'b', 'C': 'c', 'D': 'd'}
        for order in itertools.permutations([('A', 'B'), ('B', 'C'), ('C', 'D')]):
            with self.subTest(order=order):
                cfg = _cfg(values)
                review._remapped_voice_config(cfg, dict(order))
                self.assertEqual({'D': {'v': 'd'}}, cfg)

    def test_merge_precedence_when_root_exists(self):
        cfg = {'A': {'voice': 'va', 'extra': 1}, 'B': {'voice': 'vb'}}
        moved = review._remapped_voice_config(cfg, {'A': 'B'})
        self.assertEqual({'B': {'voice': 'vb', 'extra': 1}}, cfg)
        self.assertEqual('{"B": {"extra": 1, "voice": "vb"}}', json.dumps(cfg, sort_keys=True))
        self.assertEqual(1, moved)

    def test_cycles_raise_value_error_naming_the_cycle(self):
        for mapping, names in (
                ({'A': 'B', 'B': 'A'}, ('A', 'B')),
                ({'A': 'B', 'B': 'C', 'C': 'A'}, ('A', 'B', 'C'))):
            with self.subTest(mapping=mapping):
                cfg = _cfg({'A': 'a', 'B': 'b', 'C': 'c'})
                with self.assertRaises(ValueError) as context:
                    review._remapped_voice_config(cfg, mapping)
                message = str(context.exception)
                self.assertIn('cycle', message.lower())
                for name in names:
                    self.assertIn(name, message)
                self.assertEqual(_cfg({'A': 'a', 'B': 'b', 'C': 'c'}), cfg,
                                 'a detected cycle must not leave a half-applied remap')

    def test_flattened_map_matches_main_byte_for_byte(self):
        cfg = _cfg({'A': 'a', 'B': 'b', 'C': 'c'})
        moved = review._remapped_voice_config(cfg, {'A': 'C', 'B': 'C'})
        self.assertEqual('{"C": {"v": "c"}}', json.dumps(cfg, sort_keys=True))
        self.assertEqual(2, moved)


if __name__ == '__main__':
    unittest.main()
