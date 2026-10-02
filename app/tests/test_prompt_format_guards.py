"""Both experimental formats preserve rules and refuse ambiguous JSON demands."""
import unittest
from response_codecs import PromptShapeError, build_line_format_prompt
from two_step import build_freeform_prompt


class PromptFormatGuardTests(unittest.TestCase):
    def test_actual_shipped_rules_remain_byte_identical_in_both_formats(self):
        from default_prompts import DEFAULT_SYSTEM_PROMPT
        self.assertTrue(DEFAULT_SYSTEM_PROMPT)
        tail = DEFAULT_SYSTEM_PROMPT[DEFAULT_SYSTEM_PROMPT.index('RULES:'):]
        for builder in (build_line_format_prompt, build_freeform_prompt):
            with self.subTest(builder=builder.__name__):
                result = builder(DEFAULT_SYSTEM_PROMPT)
                self.assertTrue(result.endswith(tail))
                self.assertNotIn('Output ONLY valid JSON arrays', result)

    def test_both_formats_preserve_exact_rules_across_case_and_spacing_changes(self):
        for builder in (build_line_format_prompt, build_freeform_prompt):
            for format_label, rules_label in (('FORMAT:', 'RULES:'), ('format :', 'rules :'),
                                              ('  Format:', '\tRules:')):
                with self.subTest(builder=builder.__name__, labels=(format_label, rules_label)):
                    tail = rules_label + '\nKeep every original source word.\nDo not invent speakers.\n'
                    source = 'You are a script writer. Return Json only.\n\n' + format_label + '\n[old JSON shape]\n\n' + tail
                    result = builder(source)
                    self.assertTrue(result.endswith(tail))
                    self.assertNotIn('[old JSON shape]', result)
                    self.assertNotIn('Return Json only.', result)

    def test_json_demands_outside_replaced_region_are_rejected(self):
        for builder in (build_line_format_prompt, build_freeform_prompt):
            for demand in ('Return JSON only', 'Output Json objects', 'emit json', 'VALID JSON ARRAY'):
                for where in ('header', 'rules'):
                    with self.subTest(builder=builder.__name__, demand=demand, where=where):
                        head = 'You are a writer. Output JSON.\n'
                        tail = 'RULES:\nKeep all source words.\n'
                        if where == 'header':
                            head += demand + '\n'
                        else:
                            tail += demand + '\n'
                        with self.assertRaises(PromptShapeError):
                            builder(head + 'FORMAT:\n[old shape]\n' + tail)

    def test_ambiguous_or_missing_sections_fail_without_converting(self):
        for builder in (build_line_format_prompt, build_freeform_prompt):
            for source in (None, 8, '', 'Output JSON.\nRULES:\nFORMAT:\n',
                           'Output JSON.\nFORMAT:\nFORMAT:\nRULES:\n',
                           'Output JSON.\nFORMAT:\nRULES:\nRULES:\n',
                           'Unrecognized header.\nFORMAT:\nRULES:\n'):
                with self.subTest(builder=builder.__name__, source=source):
                    with self.assertRaises(PromptShapeError):
                        builder(source)
