"""A generation that raises must leave the reason in the artifact.

On 2026-09-04 an FP8 evaluation produced 766 rows, both arms 0 correct, and
428 `batch_failed` diagnostics whose `attempts` were all empty. The cause -
the model could not fetch `kernels-community/finegrained-fp8` with Hugging
Face forced offline - existed only in a log on one machine. diagnostics were
appended on the SUCCESS path only, so a raising generate() recorded nothing
and the failure reached the artifact as a bare `PassExhausted`.
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "app", "experiments", "distill_eval.py")


def _mod():
    spec = importlib.util.spec_from_file_location("de", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Boom:
    """A model whose generate() fails the way a missing kernel fails."""
    device = "cpu"

    def generate(self, **kw):
        raise RuntimeError(
            "Could not fetch 'kernels-community/finegrained-fp8' (offline mode)")


class _Tok:
    pad_token_id = 0
    eos_token_id = 1

    def apply_chat_template(self, messages, **kw):
        return "prompt"

    def __call__(self, text, return_tensors=None):
        class _Enc(dict):
            def to(self, _device):
                return self
        return _Enc({"input_ids": [[1, 2, 3]]})


class FailureRecordsItsCause(unittest.TestCase):
    def setUp(self):
        self.m = _mod()
        self.client = self.m.LocalClient(_Boom(), _Tok(), "off")

    def test_the_exception_reaches_diagnostics(self):
        with self.assertRaises(RuntimeError):
            self.client.create(model="m", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(1, len(self.client.diagnostics))
        d = self.client.diagnostics[0]
        self.assertIn("finegrained-fp8", d["error"])
        self.assertEqual("error", d["finish_reason"])

    def test_the_exception_is_still_raised(self):
        """Recording must not swallow. A caught-and-logged failure that returns
        normally is how an all-empty arm becomes a model result."""
        with self.assertRaises(RuntimeError):
            self.client.create(model="m", messages=[{"role": "user", "content": "x"}])

    def test_preflight_aborts_instead_of_evaluating(self):
        """428 batches x 4 retries against a model that cannot emit a token."""
        with self.assertRaises(SystemExit) as raised:
            self.m.preflight_generation(self.client, "m")
        self.assertIn("finegrained-fp8", str(raised.exception))
        self.assertIn("PREFLIGHT FAILED", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
