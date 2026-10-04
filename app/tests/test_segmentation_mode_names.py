"""The Pass 1 modes have a stored value (what config.json and the CLI hold) and a name users see. They drifted before:
`auto` was shown as "Auto", which hid that it mostly does not use the model.

WHAT THIS VERIFIES. Every stored value in `SEGMENTATION_MODES` has a user-facing name in RECIPES.md's Pass 1 table, the
Setup dropdown shows exactly that name for it (no extra or missing option), and the code that uses the `auto` value names its
user-facing text in a comment. It does NOT check the help paragraph's wording.
"""
import os
import re
import unittest

from three_pass_generate import SEGMENTATION_MODES

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(APP)
AUTO_NAME_STEM = "Quote marks first, model for the rest"


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


def recipes_names(text):
    """{stored value: name users see} from the Pass 1 table: rows `| `value` ... | name | ...`."""
    section = text.split("## Pass 1: dialogue detection", 1)[1].split("\n## ", 1)[0]
    names = {}
    for line in section.splitlines():
        match = re.match(r"\|\s*`(\w+)`[^|]*\|\s*([^|]+?)\s*\|", line)
        if match:
            names[match.group(1)] = match.group(2)
    return names


def dropdown_names(html):
    select = re.search(r'<select[^>]*id="tp-segmentation"[^>]*>(.*?)</select>', html, re.S).group(1)
    return dict(re.findall(r'<option value="(\w+)"[^>]*>([^<]+)</option>', select))


class SegmentationModeNamesTest(unittest.TestCase):
    def setUp(self):
        self.recipes = recipes_names(read(ROOT, "RECIPES.md"))
        self.dropdown = dropdown_names(read(APP, "static", "index.html"))

    def test_every_stored_value_has_a_user_facing_name(self):
        self.assertEqual(sorted(self.recipes), sorted(SEGMENTATION_MODES))

    def test_dropdown_shows_exactly_the_documented_names(self):
        self.assertEqual(self.dropdown, self.recipes)

    def test_auto_is_not_shown_as_plain_auto(self):
        self.assertTrue(self.dropdown["auto"].startswith(AUTO_NAME_STEM), self.dropdown["auto"])

    def test_code_that_uses_the_auto_value_names_its_user_facing_text(self):
        for name in ("three_pass_generate.py", "config_settings.py"):
            self.assertIn(AUTO_NAME_STEM, read(APP, name), name)

    def test_the_checker_can_fail(self):
        # a table row with no matching dropdown option, and a renamed option, must both be detected
        drift = dict(self.recipes, extra="Extra mode")
        self.assertNotEqual(self.dropdown, drift)
        self.assertNotEqual(recipes_names("## Pass 1: dialogue detection\n| `auto` | Auto | x | y |\n"), self.recipes)


if __name__ == "__main__":
    unittest.main()
