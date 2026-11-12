import unittest

from source_normalization import neutralize_lossy_residue


class NeutralizeLossyResidueTest(unittest.TestCase):

    def test_residue_no_rule_can_restore_is_neutralized_and_counted(self):
        # "coup d<FFFD><FFFD>tat": the second FFFD was an "e-acute", a letter,
        # which no context rule can restore. The canonical repair must leave no
        # U+FFFD behind and report how many it removed.
        text = "coup d��tat"
        cleaned, residual = neutralize_lossy_residue(text)
        self.assertNotIn("�", cleaned)
        self.assertEqual(residual, text.count("�"))

    def test_neutralize_is_noop_on_clean_text(self):
        cleaned, residual = neutralize_lossy_residue("all clean")
        self.assertEqual(cleaned, "all clean")
        self.assertEqual(residual, 0)


if __name__ == "__main__":
    unittest.main()
