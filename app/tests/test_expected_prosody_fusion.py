import unittest

from experiments.expected_prosody_fusion import (get_chinese_template,
                                                  get_japanese_template)


class ExpectedProsodyFusionTests(unittest.TestCase):
    def test_japanese_accent_templates_place_the_fall_after_the_nucleus(self):
        assert get_japanese_template([{"moras": 3, "accent": 2}]) == [0, 1, 0]
        assert get_japanese_template([{"moras": 3, "accent": 0}]) == [0, 1, 1]

    def test_chinese_templates_keep_canonical_directions(self):
        values = get_chinese_template("24")
        assert values[0] < values[4]
        assert values[5] > values[9]
