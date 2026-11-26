import unittest

from experiments.build_accent_calibration import get_questions, render


class BuildAccentCalibrationTests(unittest.TestCase):
    def test_questions_separate_language_accuracy_from_delivery(self):
        questions = {key: (prompt, choices) for key, prompt, choices in get_questions()}
        assert "cannot tell" in questions["pronunciation"][1]
        assert "cannot tell" in questions["accent"][1]
        assert questions["delivery"][1] == ["1", "2", "3", "4", "5"]

    def test_page_warns_not_to_infer_correctness_from_quality(self):
        assert "Do not infer correctness from audio quality" in render({"items": []})
