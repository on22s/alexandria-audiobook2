import json
import os
import tempfile
import unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch, Mock

from experiments import pdnc_context_evidence as evidence
from experiments.manifest import ExperimentRecord

from experiments.pdnc_context_evidence import (
    CONFIRMATORY_BOOKS, PILOT_BOOKS, TARGETED_CONFIRMATORY_BOOKS,
    TARGETED_PILOT_BOOKS, add_context_evidence_guidance,
    add_sequence_guidance, get_pilot_decision, isolate_failed_attribution,
    require_passing_pilot, select_targeted_sequence, summarize_paired_rows)
from three_pass_generate import PassExhausted


def rows(outcomes):
    result = []
    for index, (baseline, evidence) in enumerate(outcomes):
        for arm, correct in (("baseline", baseline), ("evidence", evidence)):
            result.append({"arm": arm, "id": str(index), "correct": correct})
    return result


class PdncContextEvidenceTests(unittest.TestCase):
    def run_main_with_attribution(self, attribute):
        records = []
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            (Path(tmp) / 'pdnc_inputs').mkdir()
            (Path(tmp) / 'experiments').mkdir()
            entries = [{'id': f'AnneOfGreenGables-{index}', 'line': text,
                        'expected_speaker': 'ALICE', 'prev_context': '',
                        'next_context': ''} for index, text in enumerate(('good', 'bad'))]
            fixture = {'entries': entries, 'roster': ['ALICE'], 'aliases': []}
            def record_factory(*args, **kwargs):
                record = ExperimentRecord(*args, **kwargs)
                records.append(record)
                return record
            stack.enter_context(patch.object(evidence, 'RUNTIME_ROOT', tmp))
            stack.enter_context(patch.object(evidence, 'PILOT_BOOKS', ('AnneOfGreenGables',)))
            stack.enter_context(patch.object(evidence, 'build_fixture', return_value=fixture))
            stack.enter_context(patch.object(evidence, 'ExperimentRecord', side_effect=record_factory))
            final_write = stack.enter_context(patch.object(ExperimentRecord, 'write', return_value='controlled-output'))
            stack.enter_context(patch('default_prompts.load_attribute_prompts', return_value=('BASE', '')))
            stack.enter_context(patch('experiments.pdnc_narrator_prior.get_llama_server_environment', return_value={'controlled': True}))
            stack.enter_context(patch('openai.OpenAI', return_value=Mock()))
            batch = stack.enter_context(patch('three_pass_generate.attribute_batch', side_effect=attribute))
            stack.enter_context(patch('sys.argv', ['pdnc_context_evidence', '--limit', '2', '--tag', 'error-fixture']))
            try:
                evidence.main()
            except Exception:
                if not records:
                    raise
                self.assertEqual([], records[0].rows)
                self.assertFalse(list(Path(tmp).rglob('*.ckpt')))
                final_write.assert_not_called()
                self.assertEqual(1, batch.call_count, 'non-quality failure was split or retried')
                raise
            return records[0]

    def test_main_transport_exhaustion_is_not_split_or_checkpointed(self):
        failure = PassExhausted('transport retry budget exhausted')
        def unavailable(*args, **kwargs):
            kwargs['attempt_observer']({'outcome': 'api_error'})
            raise failure
        with self.assertRaisesRegex(RuntimeError, 'LLM endpoint exhausted; refusing to split a transport failure') as raised:
            self.run_main_with_attribution(unavailable)
        self.assertIs(failure, raised.exception.__context__)

    def test_main_quality_exhaustion_still_isolates_and_records_unknown(self):
        calls = []
        def invalid(client, model, current, params, roster, **kwargs):
            calls.append([entry['text'] for entry in current])
            if any(entry['text'] == 'bad' for entry in current):
                kwargs['attempt_observer']({'outcome': 'quality_reject'})
                raise PassExhausted('response failed validation')
            return [{'text': entry['text'], 'speaker': 'ALICE'} for entry in current]
        record = self.run_main_with_attribution(invalid)
        self.assertEqual([['good', 'bad'], ['good'], ['bad']] * 2, calls)
        self.assertEqual(['ALICE', 'UNKNOWN'] * 2, [row['predicted'] for row in record.rows])
        self.assertEqual(2, len(record.meta['isolated_failures']))
        self.assertTrue(all('isolated_exhaustion' in row['candidate_provenance'] for row in record.rows if row['predicted'] == 'UNKNOWN'))

    def test_main_unrelated_exception_keeps_original_error(self):
        failure = ValueError('invalid fixture annotation')
        def unrelated(*args, **kwargs):
            raise failure
        with self.assertRaises(ValueError) as raised:
            self.run_main_with_attribution(unrelated)
        self.assertIs(failure, raised.exception)

    def test_book_split_is_frozen_and_disjoint(self):
        self.assertEqual(5, len(PILOT_BOOKS))
        self.assertEqual(20, len(CONFIRMATORY_BOOKS))
        self.assertFalse(set(PILOT_BOOKS) & set(CONFIRMATORY_BOOKS))
        self.assertEqual(5, len(TARGETED_PILOT_BOOKS))
        self.assertEqual(15, len(TARGETED_CONFIRMATORY_BOOKS))
        self.assertFalse(set(TARGETED_PILOT_BOOKS)
                         & set(TARGETED_CONFIRMATORY_BOOKS))

    def test_prompt_rejects_proximity_as_attribution(self):
        original = "BASE"
        guided = add_context_evidence_guidance(original)
        self.assertTrue(guided.startswith(original))
        self.assertIn("proximity alone", guided)
        self.assertIn("speech attribution", guided)

    def test_sequence_prompt_requires_supported_adjacency(self):
        guided = add_sequence_guidance("BASE")
        self.assertIn("chronological order", guided)
        self.assertIn("only when", guided)
        self.assertIn("Never alternate speakers mechanically", guided)
        self.assertIn("Explicit local attribution overrides", guided)

    def test_targeted_selector_defaults_to_baseline(self):
        entries = [{"id": str(index)} for index in range(3)]
        selected = select_targeted_sequence(
            entries, {"0": "ALICE", "1": "BOB", "2": "ALICE"},
            {"0": "BOB", "1": "ALICE", "2": "BOB"})
        self.assertEqual(["ALICE", "BOB", "ALICE"],
                         [selected[str(i)]["speaker"] for i in range(3)])

    def test_targeted_selector_accepts_a_named_upgrade_and_long_alternation(self):
        entries = [{"id": str(index)} for index in range(5)]
        baseline = {str(i): "THE CHILD" if i == 0 else "WRONG"
                    for i in range(5)}
        sequence = {str(i): "ALICE" if i % 2 == 0 else "BOB"
                    for i in range(5)}
        selected = select_targeted_sequence(entries, baseline, sequence)
        self.assertEqual([sequence[str(i)] for i in range(5)],
                         [selected[str(i)]["speaker"] for i in range(5)])

    def test_summary_uses_only_paired_rows(self):
        sample = rows([(True, True), (True, False), (False, True)])
        sample.append({"arm": "evidence", "id": "unpaired", "correct": True})
        self.assertEqual({"n": 3, "baseline_correct": 2,
                          "evidence_correct": 2, "delta_points": 0.0,
                          "gained": 1, "lost": 1, "p_value": 1.0},
                         summarize_paired_rows(sample))

    def test_summary_accepts_a_named_candidate_arm(self):
        sample = [
            {"arm": arm, "id": str(index), "correct": correct}
            for index, pair in enumerate(((True, False), (False, True)))
            for arm, correct in (("baseline", pair[0]), ("sequence", pair[1]))
        ]
        summary = summarize_paired_rows(sample, "sequence")
        self.assertEqual(2, summary["n"])
        self.assertEqual(1, summary["baseline_correct"])
        self.assertEqual(1, summary["evidence_correct"])

    def test_pilot_gate_requires_effect_size_and_significance(self):
        passing = rows([(False, True)] * 20 + [(True, True)] * 80)
        decision = get_pilot_decision(passing)
        self.assertTrue(decision["advance"])
        self.assertEqual(20.0, decision["delta_points"])
        no_effect = get_pilot_decision(rows([(True, True)] * 100))
        self.assertFalse(no_effect["advance"])

    def test_confirmatory_requires_explicit_passing_pilot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pilot.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"meta": {"phase": "pilot",
                                     "validation": "ok",
                                     "decision": {"advance": False}}}, handle)
            with self.assertRaisesRegex(ValueError, "did not pass"):
                require_passing_pilot(path)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"meta": {"phase": "pilot",
                                     "validation": "ok",
                                     "decision": {"advance": True}}}, handle)
            with self.assertRaises(ValueError):
                require_passing_pilot(path)

    def test_quality_failure_isolates_only_the_irrecoverable_row(self):
        calls = []

        def attribute(batch, contexts):
            calls.append([entry["text"] for entry in batch])
            if any(entry["text"] == "bad" for entry in batch):
                raise PassExhausted("quality")
            return [{"text": entry["text"], "speaker": "GOOD"}
                    for entry in batch]

        frozen = [{"text": text} for text in ("one", "bad", "three", "four")]
        output, failed = isolate_failed_attribution(
            attribute, frozen, [{}, {}, {}, {}])
        self.assertEqual({1}, failed)
        self.assertEqual(["GOOD", "UNKNOWN", "GOOD", "GOOD"],
                         [entry["speaker"] for entry in output])
        self.assertIn(["bad"], calls)

    def test_non_quality_exception_is_not_split(self):
        def unavailable(batch, contexts):
            raise RuntimeError("endpoint unavailable")

        with self.assertRaisesRegex(RuntimeError, "endpoint unavailable"):
            isolate_failed_attribution(
                unavailable, [{"text": "one"}, {"text": "two"}], [{}, {}])


if __name__ == "__main__":
    unittest.main()
