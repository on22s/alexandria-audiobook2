"""A gate verdict must not read as false on its own numbers.

`husky_baritone_40s_m_military` produced:

    FAIL - 0.450 is below 0.45

which is untrue as written. The comparison was right - the median is 0.4499 -
but the message rounded to three places while the threshold shows two, so a
reader checking the gate is sent hunting a comparison bug that does not exist.
The artifact already stored four places; only the human-facing string lied.

Display precision must EXCEED comparison precision, and a near miss should say
how near: 0.4499 against 0.45 is one ten-thousandth, which is a different
statement from 0.062 against 0.45 and should not read the same.
"""
import os
import re
import unittest

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "experiments", "verify_adapter_identity.py")


def _source():
    with open(MODULE, encoding="utf-8") as fh:
        return fh.read()


def render(median, threshold=0.45):
    """Reproduce the verdict string the module builds."""
    ok = median >= threshold
    margin = threshold - median
    near = (" - a near miss, short by %.4f" % margin) if 0 < margin <= 0.005 else ""
    if ok:
        return f"PASS - the adapter sounds like its narrator ({median:.4f})"
    return (f"FAIL - {median:.4f} is below {threshold:.2f}{near}. Working "
            f"adapters reach 0.65-0.74")


@unittest.skipUnless(os.path.exists(MODULE), "verify_adapter_identity absent")
class VerdictPrecisionTests(unittest.TestCase):
    def test_the_module_prints_four_places(self):
        source = _source()
        self.assertNotIn("{median:.3f} is below", source,
                         "three-place display against a two-place threshold "
                         "is what produced '0.450 is below 0.45'")
        self.assertIn("{median:.4f} is below", source)

    def test_the_real_near_miss_no_longer_reads_as_false(self):
        # The actual value from the 2026-08-18 gate run.
        text = render(0.4499)
        self.assertIn("0.4499", text)
        self.assertNotIn("0.450 is below", text)

    def test_a_near_miss_says_how_near(self):
        self.assertIn("near miss", render(0.4499))
        self.assertIn("0.0001", render(0.4499))

    def test_a_clear_failure_is_not_dressed_up_as_a_near_miss(self):
        # 0.062 against 0.45 must not acquire softening language.
        self.assertNotIn("near miss", render(0.0622))

    def test_the_boundary_value_passes(self):
        # >= threshold, so exactly 0.45 is a PASS and must not be a near miss.
        self.assertTrue(render(0.45).startswith("PASS"))
        self.assertNotIn("near miss", render(0.45))

    def test_a_pass_reports_four_places_too(self):
        self.assertIn("0.6340", render(0.634))
