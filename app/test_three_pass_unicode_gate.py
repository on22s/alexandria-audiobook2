import unittest

from three_pass_generate import prepare_source_text


class PrepareSourceTextTest(unittest.TestCase):

    def test_clean_source_passes_through(self):
        text, report = prepare_source_text("A clean line.\n")
        self.assertEqual(text, "A clean line.\n")
        self.assertEqual(report["repaired"], 0)
        self.assertEqual(report["residual"], 0)

    def test_damaged_source_is_repaired_and_reported(self):
        # Padded to book-like length: density is only meaningful over a real
        # source, and one damaged character in a short string reads as 9%.
        padding = "filler text. " * 40
        text, report = prepare_source_text(padding + "don�t stop\n")
        self.assertEqual(text, padding + "don’t stop\n")
        self.assertEqual(report["repaired"], 1)
        self.assertEqual(report["residual"], 0)
        self.assertNotIn("�", text)

    def test_unsafe_control_characters_are_rejected(self):
        with self.assertRaises(ValueError) as caught:
            prepare_source_text("bad\x00byte\n")
        self.assertIn("control", str(caught.exception).lower())

    def test_density_above_threshold_is_rejected(self):
        # 3 of 10 characters destroyed = 30%, far above the 2% ceiling.
        with self.assertRaises(ValueError) as caught:
            prepare_source_text("ab�cd�ef�g")
        self.assertIn("density", str(caught.exception).lower())

    def test_index18_density_is_admitted(self):
        # index18 sits at 1.4%, below the 2% ceiling: 14 damaged in 1000.
        source = ("x" * 986) + ("�" * 14)
        text, report = prepare_source_text(source)
        self.assertNotIn("�", text)
        self.assertEqual(report["repaired"] + report["residual"], 14)


if __name__ == "__main__":
    unittest.main()
