"""An unanswered window must record WHY it was unanswered.

lora_serving_eval wrote the same `<arm>|batch_failed` provenance for two
opposite failures:

    PassExhausted, nothing bindable   the MODEL broke the output contract -- a
                                      failure a user hits, which an adapter can
                                      genuinely repair
    any other exception               the REQUEST failed (dead server, refused
                                      connection) -- it says nothing about the
                                      model at all

WHAT THAT COST. On 2026-09-26 the Muse Q3_K_XL nine-novel cell reported the
largest adapter win on record, +6.06 at p=1.5e-20, repairing 72.4% of the base's
errors. The whole effect was 178 base rows of Pride and Prejudice recorded as
`batch_failed` in a checkpoint stitched across resumed attempts; an earlier
complete run of the same cell had that book's base at 93.2%, 0 unanswered.
Without the lost rows the cell is -0.28, p=0.62. The Muse IQ2_XXS gold cell's 133
unanswered base rows carry the identical tag, and there they are real contract
collapse (every failed window in its log is PassExhausted). No rule over the
artifact could separate them; only the run log could.

These tests pin the tag (with real exception objects), that both failure paths
in main() use it, and the per-book cause note that makes it visible in the log.
"""
import ast
import os
import sys
import unittest
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from three_pass_generate import PassExhausted  # noqa: E402
from experiments.lora_serving_eval import get_batch_failed_provenance  # noqa: E402

SOURCE = os.path.join(REPO, "app", "experiments", "lora_serving_eval.py")


class TheTagSeparatesModelFromTransportFailure(unittest.TestCase):

    def test_contract_failure_is_tagged_as_such(self):
        tag = get_batch_failed_provenance("base", PassExhausted("x", last_entries=None))
        self.assertEqual("base|batch_failed=PassExhausted", tag)

    def test_transport_failure_is_tagged_as_such(self):
        refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.assertEqual("lora|batch_failed=URLError",
                         get_batch_failed_provenance("lora", refused))

    def test_the_two_kinds_never_share_a_tag(self):
        """The old harness wrote the same string for both; that is the bug."""
        model = get_batch_failed_provenance("base", PassExhausted("x"))
        transport = get_batch_failed_provenance("base", TimeoutError("read timed out"))
        self.assertNotEqual(model, transport)

    def test_tag_still_contains_batch_failed(self):
        """Readers that look for `batch_failed` in a row keep working."""
        for exc in (PassExhausted("x"), ConnectionError("gone")):
            self.assertIn("batch_failed", get_batch_failed_provenance("base", exc))


class MainUsesTheTagOnEveryFailurePath(unittest.TestCase):

    def setUp(self):
        with open(SOURCE, encoding="utf-8") as handle:
            self.src = handle.read()
        self.tree = ast.parse(self.src)

    def test_no_bare_batch_failed_provenance_remains(self):
        """A plain f"{arm}|batch_failed" anywhere reintroduces the ambiguity."""
        self.assertNotIn('provenance=f"{arm}|batch_failed")', self.src)

    def test_both_except_branches_set_the_tag(self):
        handlers = [h for h in ast.walk(self.tree) if isinstance(h, ast.ExceptHandler)
                    and isinstance(h.type, ast.Name)
                    and h.type.id in ("PassExhausted", "Exception")]
        self.assertEqual(2, len(handlers),
                         "expected the PassExhausted and Exception handlers in main(); "
                         "this test has stopped discriminating")
        for handler in handlers:
            calls = [n for n in ast.walk(handler) if isinstance(n, ast.Call)
                     and getattr(n.func, "id", None) == "get_batch_failed_provenance"]
            self.assertTrue(calls, f"except {handler.type.id}: does not record the cause")

    def test_the_tag_is_reset_every_window(self):
        """Otherwise a window that fails without raising inherits the last cause."""
        reset = self.src.index('failed = f"{arm}|batch_failed=NoOutput"')
        call = self.src.index("out = attribute_batch(client")
        self.assertLess(reset, call)
        self.assertLess(call - reset, 400, "the reset must sit just before the call")

    def test_per_book_line_reports_the_causes(self):
        self.assertIn('partition("batch_failed=")', self.src)
        self.assertIn("unanswered {unanswered}{cause_note}", self.src)


if __name__ == "__main__":
    unittest.main()
