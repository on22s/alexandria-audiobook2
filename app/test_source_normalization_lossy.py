import unittest

from source_normalization import repair_lossy_replacements


class RepairLossyReplacementsTest(unittest.TestCase):

    def test_apostrophe_between_letters(self):
        text, repairs = repair_lossy_replacements("don�t")
        self.assertEqual(text, "don’t")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["after"], "’")
        self.assertEqual(repairs[0]["offset"], 3)

    def test_open_quote_after_newline(self):
        text, _ = repair_lossy_replacements("\n�I was there")
        self.assertEqual(text, "\n“I was there")

    def test_close_quote_after_sentence_punctuation(self):
        text, _ = repair_lossy_replacements("he said.�\n")
        self.assertEqual(text, "he said.”\n")

    def test_em_dash_before_capital(self):
        text, _ = repair_lossy_replacements("Magic�Fiction")
        self.assertEqual(text, "Magic—Fiction")

    def test_en_dash_after_digit(self):
        text, _ = repair_lossy_replacements("Kiyotaka, 1973� illustrator")
        self.assertEqual(text, "Kiyotaka, 1973– illustrator")

    def test_copyright_before_year(self):
        text, _ = repair_lossy_replacements("translation � 2019 by Yen Press")
        self.assertEqual(text, "translation © 2019 by Yen Press")

    def test_multi_run_is_several_characters(self):
        text, _ = repair_lossy_replacements("\n�Hee-hee��\n")
        self.assertEqual(text, "\n“Hee-hee…”\n")

    def test_triple_run_is_quoted_ellipsis(self):
        text, _ = repair_lossy_replacements("\n���\n")
        self.assertEqual(text, "\n“…”\n")

    def test_clean_text_is_untouched(self):
        source = "Nothing wrong here — nothing at all."
        text, repairs = repair_lossy_replacements(source)
        self.assertEqual(text, source)
        self.assertEqual(repairs, [])


if __name__ == "__main__":
    unittest.main()
