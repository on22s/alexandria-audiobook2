"""stage1_only must read the JSON the model actually produces.

On 2026-09-04 the stage1_only arm scored 0 of 88 in BOTH arms and answered
UNKNOWN on every line. The model had not failed - it emitted

    [{"n": 0, "speaker": "CARISSA"}, {"n": 2, "speaker": "KNIGHT LEADER"}, ...]

clean, well formed, with plausible speakers. parse_decisions looked only for
`ENTRY 0: CARISSA`, matched nothing, and every entry fell through to UNKNOWN.

An adapter trained to emit JSON emitting JSON is the EXPECTED case. Reading it
is what makes stage1_only a test of the reasoning rather than of one output
convention.
"""
import unittest

from experiments.two_step_attribution import parse_decisions


REAL = ('[{"n": 0, "speaker": "CARISSA"}, {"n": 1, "speaker": "CARISSA"}, '
        '{"n": 2, "speaker": "KNIGHT LEADER"}, {"n": 5, "speaker": "VILIAN"}]')


class ParsesBothShapes(unittest.TestCase):
    def test_the_entry_line_shape_still_works(self):
        self.assertEqual({0: "CARISSA", 1: "VILIAN"},
                         parse_decisions("ENTRY 0: CARISSA\nENTRY 1: VILIAN"))

    def test_the_json_shape_the_model_actually_emitted(self):
        got = parse_decisions(REAL)
        self.assertEqual({0: "CARISSA", 1: "CARISSA", 2: "KNIGHT LEADER",
                          5: "VILIAN"}, got)

    def test_json_wrapped_in_prose_is_still_found(self):
        text = "Here is my answer:\n" + REAL + "\nThat is all."
        self.assertEqual("KNIGHT LEADER", parse_decisions(text)[2])

    def test_entry_lines_win_when_both_are_present(self):
        """The documented format takes precedence; JSON is the fallback."""
        text = "ENTRY 0: TOUMA\n" + REAL
        self.assertEqual("TOUMA", parse_decisions(text)[0])

    def test_text_with_no_decisions_yields_nothing(self):
        """Empty must stay empty - a parser that invents entries is worse
        than one that finds none."""
        self.assertEqual({}, parse_decisions("I am not sure who speaks here."))
        self.assertEqual({}, parse_decisions(""))
        self.assertEqual({}, parse_decisions("[1, 2, 3]"))

    def test_malformed_json_does_not_raise(self):
        self.assertEqual({}, parse_decisions('[{"n": 0, "speaker": '))


if __name__ == "__main__":
    unittest.main()
