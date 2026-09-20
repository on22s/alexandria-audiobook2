"""The roster line reads "EMMA (also: EMMA WOODHOUSE, MISS WOODHOUSE)" and rule 6
of the MICHEL2 prompt says answer with the main form. Measured 2026-09-20 on
the PDNC two-book fixture, the A3B IQ3_XXS adapter copied the whole entry on
79 of 663 rows and IQ1_M on 52. The prose never contains the parenthetical, so
is_attested_name rejected each batch, the retries burned, the lines shipped
as UNKNOWN. The strings below are the exact ones from that artifact."""
import json
import unittest
from types import SimpleNamespace

from pass_quality import strip_roster_alias_echo, validate_attribution
import three_pass_generate as tpg
from three_pass_generate import LLMGenParams


ECHOES = {
    "EMMA (also: EMMA WOODHOUSE, MISS WOODHOUSE)": "EMMA",
    "BRETT ASHLEY (also: BRETT, LADY ASHLEY, LADY BRETT, LADY BRETT ASHLEY)": "BRETT ASHLEY",
    "JAKE BARNES (also: BARNES, JACOB, MR BARNES, NARR)": "JAKE BARNES",
    "MR. WOODHOUSE (also: MR WOODHOUSE)": "MR. WOODHOUSE",
}


class StripRosterAliasEchoTests(unittest.TestCase):
    def test_measured_echoes_reduce_to_the_main_form(self):
        for echoed, main in ECHOES.items():
            self.assertEqual(main, strip_roster_alias_echo(echoed))

    def test_plain_names_and_placeholders_are_untouched(self):
        for s in ("EMMA", "NARRATOR", "UNKNOWN", "MR. WOODHOUSE", "  HARRIET SMITH ", ""):
            self.assertEqual(s, strip_roster_alias_echo(s))

    def test_parentheses_that_are_not_an_alias_tail_are_kept(self):
        # A name with its own parenthetical is not our roster format.
        self.assertEqual("KOYOMI (VAMPIRE)", strip_roster_alias_echo("KOYOMI (VAMPIRE)"))
        self.assertEqual("(also: X) EMMA", strip_roster_alias_echo("(also: X) EMMA"))

    def test_non_strings_pass_through(self):
        self.assertIsNone(strip_roster_alias_echo(None))
        self.assertEqual(3, strip_roster_alias_echo(3))


class GateAcceptsEchoTests(unittest.TestCase):
    SOURCE = ("Emma Woodhouse was handsome. " * 200 + "Emma said so. " * 50
              + "Mr. Woodhouse agreed. " * 50)

    def test_gate_reads_the_echo_as_the_main_form(self):
        frozen = [{"type": "SPOKEN", "text": "Poor Miss Taylor!"}]
        echoed = [{"n": 0, "head": "Poor Miss Taylor!",
                   "speaker": "EMMA (also: EMMA WOODHOUSE, MISS WOODHOUSE)"}]
        report = validate_attribution(frozen, echoed, source_text=self.SOURCE)
        self.assertTrue(report["passed"], report["findings"])
        # The reply itself is not rewritten (the gate is a pure read).
        self.assertEqual("EMMA (also: EMMA WOODHOUSE, MISS WOODHOUSE)", echoed[0]["speaker"])

    def test_an_invented_name_with_an_alias_tail_is_still_rejected(self):
        frozen = [{"type": "SPOKEN", "text": "Poor Miss Taylor!"}]
        report = validate_attribution(frozen, [{"n": 0, "head": "Poor Miss Taylor!",
                                                "speaker": "FUTURE_ME (also: ME)"}],
                                      source_text=self.SOURCE)
        self.assertFalse(report["passed"])
        self.assertIn("speaker_not_in_source", {f["code"] for f in report["findings"]})


class BindingStripsEchoTests(unittest.TestCase):
    """The real attribute_batch path: the stub model answers with the echo, the
    gate passes it, and the bound entry carries the main form - not the echo."""

    def test_attribute_batch_binds_the_main_form(self):
        frozen = [{"type": "NARRATOR", "text": "She sighed."},
                  {"type": "SPOKEN", "text": "Poor Miss Taylor!"}]
        reply = [{"n": 0, "speaker": "NARRATOR"},
                 {"n": 1, "speaker": "EMMA (also: EMMA WOODHOUSE, MISS WOODHOUSE)"}]

        class Client:
            def __init__(self):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

            def _create(self, **kw):
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(reply)), finish_reason="stop")],
                    usage=None)

        out = tpg.attribute_batch(Client(), "m", frozen, LLMGenParams(structured_output="off"),
                                  ["EMMA", "MR. WOODHOUSE"])
        self.assertEqual(["NARRATOR", "EMMA"], [e["speaker"] for e in out])
        self.assertEqual("Poor Miss Taylor!", out[1]["text"])   # text freeze intact


if __name__ == "__main__":
    unittest.main()
