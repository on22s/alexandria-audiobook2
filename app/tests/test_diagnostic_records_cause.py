"""A rejected batch must record WHY, not only that it failed.

WHAT THIS COST. An artifact from 2026-09-04 recorded 43 `batch_failed` windows.
Every attempt in every one of them returned a COMPLETE, well-formed,
EOS-terminated JSON array of exactly the right length - so index alignment
passed and nothing was truncated, and the rejection had to come from one of
`validate_attribution`'s speaker rules. Which one was unrecoverable from the
file. Answering it on 2026-09-06 took an hour of reading code to establish
something the artifact could have carried in a field.

The two halves already existed: `call_llm_for_entries` computes `outcome` and
`failure_codes` from the findings and hands them to `attempt_observer`, and
`distill_eval` built its own diagnostics and passed no observer. Computed, then
dropped.

Same family as goal 6.6: a diagnostic that records the symptom and discards the
cause cannot be checked against anything.
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

from experiments.distill_eval import merge_validation_into_diagnostics  # noqa: E402


class _Client:
    def __init__(self, diagnostics=None):
        self.diagnostics = diagnostics if diagnostics is not None else []


class DiagnosticRecordsCause(unittest.TestCase):

    def _client(self):
        return _Client([{"finish_reason": "stop", "raw_response": "[]"}])

    def test_the_rejection_codes_reach_the_diagnostic(self):
        client = self._client()
        merge_validation_into_diagnostics(client)(
            {"attempt": 1, "outcome": "quality_rejected",
             "failure_codes": ["speaker_not_in_source", "spoken_not_named"]})
        self.assertEqual(["speaker_not_in_source", "spoken_not_named"],
                         client.diagnostics[-1]["failure_codes"])
        self.assertEqual("quality_rejected", client.diagnostics[-1]["outcome"])

    def test_it_merges_and_does_not_append(self):
        """Appending would double-count attempts and make len(attempts) lie."""
        client = self._client()
        merge_validation_into_diagnostics(client)(
            {"outcome": "quality_rejected", "failure_codes": ["x"]})
        self.assertEqual(1, len(client.diagnostics))

    def test_it_keeps_what_the_client_already_recorded(self):
        client = self._client()
        merge_validation_into_diagnostics(client)({"outcome": "accepted"})
        self.assertEqual("stop", client.diagnostics[-1]["finish_reason"],
                         "the generation facts must survive the merge")

    def test_no_diagnostics_yet_is_survivable(self):
        """The observer can fire before any generation on an error path."""
        client = _Client([])
        merge_validation_into_diagnostics(client)({"failure_codes": ["x"]})
        self.assertEqual([], client.diagnostics)

    def test_a_non_dict_record_is_ignored(self):
        client = self._client()
        merge_validation_into_diagnostics(client)(None)
        self.assertNotIn("failure_codes", client.diagnostics[-1])

    def test_keys_the_observer_does_not_own_are_not_copied(self):
        """Only the validator's verdict; not the whole attempt record."""
        client = self._client()
        merge_validation_into_diagnostics(client)(
            {"outcome": "quality_rejected", "prompt_tokens": 999})
        self.assertNotIn("prompt_tokens", client.diagnostics[-1],
                         "generation facts come from the client, not the observer")


if __name__ == "__main__":
    unittest.main()
