"""Tests for the Pass 3 delivery-direction setting in Setup (#1258).

The backend already exists and must not change: pass_quality.DELIVERY_DIRECTIONS
lists the values, config_settings.GenerationConfig validates the config key and
generation refuses a custom Pass 3 prompt preset with a non-off mode. This
module only guards the UI that exposes the setting, using synthetic markup/JS
fragments for the rejection cases.
"""

import re
import unittest
from pathlib import Path

import pass_quality

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
INDEX_HTML_PATH = STATIC_DIR / "index.html"
APP_CORE_PATH = STATIC_DIR / "js" / "app-core.js"
APP_HELP_PATH = STATIC_DIR / "js" / "app-help.js"

SELECT_ID = "tp-delivery-direction"
SETTINGS_KEY = "three_pass_delivery_direction"


def get_text(path):
    return path.read_text(encoding="utf-8")


def get_select_block(markup, select_id):
    pattern = r'<select[^>]*\bid="%s"[^>]*>(.*?)</select>' % re.escape(select_id)
    match = re.search(pattern, markup, re.DOTALL)
    if match is None:
        raise AssertionError("no <select id=%r> in the markup" % select_id)
    return match.group(1)


def get_option_tags(select_block):
    return re.findall(r"<option\b[^>]*>", select_block)


def get_attribute(tag, name):
    match = re.search(r'\b%s="([^"]*)"' % re.escape(name), tag)
    if match is None:
        return None
    return match.group(1)


def get_option_values(select_block):
    return [get_attribute(tag, "value") for tag in get_option_tags(select_block)]


def get_selected_option_values(select_block):
    return [
        get_attribute(tag, "value")
        for tag in get_option_tags(select_block)
        if re.search(r"\bselected\b", tag) is not None
    ]


def get_object_block(text, marker):
    start = text.find(marker)
    if start < 0:
        raise AssertionError("marker %r not found" % marker)
    end = text.find("};", start)
    if end < 0:
        raise AssertionError("block after %r is not closed" % marker)
    return text[start:end + 2]


def get_string_property(block, key):
    pattern = r"""['"]?%s['"]?\s*:\s*['"]([^'"]*)['"]""" % re.escape(key)
    match = re.search(pattern, block)
    if match is None:
        return None
    return match.group(1)


def get_delivery_direction_payload(js):
    pattern = (
        r"three_pass_delivery_direction\s*:\s*document\.getElementById\(\s*"
        r"['\"]%s['\"]\s*\)\.value\s*\|\|\s*['\"]off['\"]" % re.escape(SELECT_ID)
    )
    match = re.search(pattern, js)
    if match is None:
        return None
    return match.group(0)


def get_help_entry(block, field_id):
    pattern = r"""['"]%s['"]\s*:\s*\{([^{}]*)\}""" % re.escape(field_id)
    match = re.search(pattern, block)
    if match is None:
        return None
    return match.group(1)


def get_help_what(entry):
    match = re.search(r"""\bwhat\s*:\s*['"](.*?)['"]\s*,?""", entry, re.DOTALL)
    if match is None:
        return None
    return match.group(1)


class DeliveryDirectionSelectTests(unittest.TestCase):
    def setUp(self):
        self.markup = get_text(INDEX_HTML_PATH)
        self.select_block = get_select_block(self.markup, SELECT_ID)

    def test_option_values_match_backend_directions_in_order(self):
        self.assertEqual(
            get_option_values(self.select_block), list(pass_quality.DELIVERY_DIRECTIONS)
        )

    def test_off_is_selected_by_default(self):
        self.assertEqual(get_selected_option_values(self.select_block), ["off"])

    def test_experimental_modes_are_not_preselected(self):
        selected = get_selected_option_values(self.select_block)
        self.assertNotIn("gaps", selected)
        self.assertNotIn("directed", selected)
        self.assertNotIn("action_only", selected)

    def test_option_values_are_unique(self):
        values = get_option_values(self.select_block)
        self.assertEqual(len(values), len(set(values)))

    def test_select_follows_the_step_one_column(self):
        self.assertGreater(
            self.markup.index('id="%s"' % SELECT_ID),
            self.markup.index('id="tp-pass1-format"'),
        )


class SelectParsingTests(unittest.TestCase):
    def test_missing_select_is_rejected(self):
        with self.assertRaises(AssertionError):
            get_select_block("<div>no select here</div>", SELECT_ID)

    def test_missing_value_attribute_returns_none(self):
        self.assertIsNone(get_attribute("<option selected>", "value"))


class GenerationControlMappingTests(unittest.TestCase):
    def setUp(self):
        self.js = get_text(APP_CORE_PATH)
        self.block = get_object_block(self.js, "const generationControlFields = {")

    def test_maps_field_to_config_key(self):
        self.assertEqual(get_string_property(self.block, SELECT_ID), SETTINGS_KEY)

    def test_save_payload_sends_the_choice(self):
        self.assertIsNotNone(get_delivery_direction_payload(self.js))

    def test_save_payload_defaults_to_off(self):
        expression = get_delivery_direction_payload(self.js)
        self.assertIsNotNone(expression)
        self.assertRegex(expression, r"\|\|\s*['\"]off['\"]")

    def test_unknown_field_has_no_mapping(self):
        self.assertIsNone(get_string_property(self.block, "tp-not-a-field"))


class FieldHelpTests(unittest.TestCase):
    def setUp(self):
        self.js = get_text(APP_HELP_PATH)
        self.block = get_object_block(self.js, "FIELD_HELP")

    def test_has_entry(self):
        self.assertIsNotNone(get_help_entry(self.block, SELECT_ID))

    def test_what_text_is_filled_in(self):
        what = get_help_what(get_help_entry(self.block, SELECT_ID))
        self.assertIsNotNone(what)
        self.assertTrue(what.strip())
        self.assertIn("Step 3", what)

    def test_unknown_field_has_no_entry(self):
        self.assertIsNone(get_help_entry(self.block, "tp-not-a-field"))

    def test_entry_without_what_returns_none(self):
        self.assertIsNone(get_help_what("recommend: 'nothing here'"))


if __name__ == "__main__":
    unittest.main()
