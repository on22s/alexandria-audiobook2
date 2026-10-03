"""Setup switch 'Listed groups answer for their members' (#653): opt-in, and
when on it sends exactly the measured rule-7 text."""
import re
import unittest
from pathlib import Path

import config_settings
import three_pass_generate as tp
from attribution_prompt_variants import (GROUP_RULE_7, MICHEL2_SYSTEM, UNKNOWN_RULE_7,
                                         _texts_for, get_group_rule_system)

STATIC = Path(__file__).resolve().parent.parent / "static"


class GroupRuleSettingTest(unittest.TestCase):
    def test_defaults_off_and_round_trips(self):
        self.assertFalse(config_settings.GenerationConfig().three_pass_group_rule)
        saved = config_settings.GenerationConfig(three_pass_group_rule=True).model_dump()
        self.assertTrue(config_settings.GenerationConfig(**saved).three_pass_group_rule)

    def test_switch_on_replaces_only_rule_seven_of_the_sent_system_prompt(self):
        variant, texts = tp.resolve_attribute_prompt({"generation": {"three_pass_group_rule": True}})
        sent = _texts_for(variant, texts)["system"]
        self.assertIn(GROUP_RULE_7, sent)
        self.assertNotIn(UNKNOWN_RULE_7, sent)
        self.assertEqual(MICHEL2_SYSTEM.replace(UNKNOWN_RULE_7, GROUP_RULE_7), sent)

    def test_switch_off_sends_the_builtin_unchanged(self):
        for generation in ({}, {"three_pass_group_rule": False}, {"three_pass_group_rule": "yes"}):
            self.assertIsNone(tp.resolve_attribute_prompt({"generation": generation})[1])

    def test_a_prompt_without_rule_seven_is_left_alone(self):
        self.assertEqual(("custom text", False), get_group_rule_system("custom text"))
        variant, texts = tp.resolve_attribute_prompt(
            {"generation": {"three_pass_group_rule": True}}, "default")
        self.assertIsNone(texts)

    def test_switch_changes_the_checkpoint_identity(self):
        on = tp.resolve_attribute_prompt({"generation": {"three_pass_group_rule": True}})[1]
        fp = lambda texts: tp.three_pass_fingerprint("text", "m", 3000, attribute_prompt_texts=texts)
        self.assertNotEqual(fp(None), fp(on))

    def test_page_switch_is_off_by_default_and_saved(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        js = (STATIC / "js" / "app-core.js").read_text(encoding="utf-8")
        tag = re.search(r'<input\b[^>]*id="tp-group-rule"[^>]*>', html)
        self.assertIsNotNone(tag)
        self.assertNotIn("checked", tag.group(0))
        self.assertIn("three_pass_group_rule: document.getElementById('tp-group-rule').checked", js)
        self.assertIn("getElementById('tp-group-rule').checked = g.three_pass_group_rule === true", js)
        self.assertIn("'three_pass_group_rule'].some(", js)


if __name__ == "__main__":
    unittest.main()
