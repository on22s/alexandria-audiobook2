"""Real gate input hashes, PCM and publisher admission, without inference."""
import json
import ast
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import gate_campaign as campaign
import promote_adapters
from tests.test_identity_gate_completion import make_inputs, measured_document


class GateCampaignTests(unittest.TestCase):
    def test_campaign_writers_serialize_and_preserve_durable_results(self):
        for operation in ('result', 'completion', 'restart'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                gates, queue, journal = self.prepare(Path(tmp))
                campaign.save_campaign_start(queue, journal)
                original_id = campaign.get_campaign_document(journal)['campaign_id']
                if operation == 'completion':
                    campaign.save_campaign_result(journal, 'b', 0, lines=10)
                measured = threading.Event()
                release = threading.Event()
                attempted = threading.Event()
                entered = threading.Event()
                errors = []
                measure = campaign.get_measured_campaign_member
                read = campaign.get_campaign_document
                write = campaign.atomic_json_write

                def blocked_measure(*args):
                    if threading.current_thread().name == 'campaign-first':
                        measured.set()
                        if not release.wait(5):
                            raise RuntimeError('fixture did not release measurement')
                    return measure(*args)

                def observed_read(*args):
                    if threading.current_thread().name == 'campaign-second':
                        entered.set()
                    return read(*args)

                def observed_write(*args):
                    if threading.current_thread().name == 'campaign-second':
                        entered.set()
                    return write(*args)

                def worker(first):
                    try:
                        if first:
                            campaign.save_campaign_result(journal, 'a', 0, lines=10)
                        else:
                            attempted.set()
                            if operation == 'result':
                                campaign.save_campaign_result(journal, 'b', 0, lines=10)
                            elif operation == 'completion':
                                campaign.save_campaign_completion(journal, lines=10)
                            else:
                                campaign.save_campaign_start(queue, journal)
                    except Exception as error:
                        errors.append(error)

                first = threading.Thread(target=worker, args=(True,), name='campaign-first')
                second = threading.Thread(target=worker, args=(False,), name='campaign-second')
                with patch.object(campaign, 'get_measured_campaign_member', blocked_measure), \
                     patch.object(campaign, 'get_campaign_document', observed_read), \
                     patch.object(campaign, 'atomic_json_write', observed_write):
                    first.start()
                    try:
                        self.assertTrue(measured.wait(2))
                        second.start()
                        self.assertTrue(attempted.wait(2))
                        self.assertFalse(entered.wait(.2), 'writer entered a live journal transaction')
                    finally:
                        release.set()
                        first.join(5)
                        if second.ident is not None:
                            second.join(5)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual([], errors)
                saved = campaign.get_campaign_document(journal)
                if operation == 'restart':
                    self.assertNotEqual(original_id, saved['campaign_id'])
                    self.assertTrue(all('rc' not in row for row in saved['members'].values()))
                else:
                    self.assertTrue(all(row['rc'] == 0 and row['sha256']
                                        for row in saved['members'].values()))
                    if operation == 'completion':
                        self.assertEqual('complete', saved['status'])

    def prepare(self, root):
        gates = root / 'gates'
        gates.mkdir()
        queue = root / 'queue.tsv'
        rows = []
        for name in ('a', 'b'):
            adapter, data = make_inputs(root / name)
            gate = measured_document(adapter, data)
            gate['provenance'] = {'commit': 'known-fixture'}
            (gates / f'gate_promote__{name}.json').write_text(json.dumps(gate))
            rows.append(f'{name}\t{adapter}\t{data}')
        queue.write_text('\n'.join(rows) + '\n')
        journal = gates / campaign.JOURNAL_NAME
        return gates, queue, journal

    def read(self, gates, name):
        reader = promote_adapters.get_gate_evidence
        baseline = os.environ.get('GATE_CAMPAIGN_BASELINE')
        if baseline:
            tree = ast.parse(Path(baseline).read_text())
            node = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'get_gate_evidence')
            namespace = dict(promote_adapters.__dict__)
            exec(compile(ast.Module(body=[node], type_ignores=[]), baseline, 'exec'), namespace)
            reader = namespace['get_gate_evidence']
        with patch.object(promote_adapters, 'GATES', str(gates)), \
             patch.object(promote_adapters, 'GATE_PREFIX', 'gate_promote__'), \
             patch.object(promote_adapters, 'get_gate_evidence', reader):
            return promote_adapters.gate_result(name)

    def test_interrupted_campaign_refuses_both_new_and_old_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            gates, queue, journal = self.prepare(Path(tmp))
            self.assertIsNotNone(self.read(gates, 'a'))  # legacy gate policy
            campaign.save_campaign_start(queue, journal)
            campaign.save_campaign_result(journal, 'a', 0, lines=10)
            self.assertIsNone(self.read(gates, 'a'))
            self.assertIsNone(self.read(gates, 'b'))
            before = journal.read_bytes()
            with self.assertRaises(ValueError):
                campaign.save_campaign_completion(journal, lines=10)
            self.assertEqual(before, journal.read_bytes())
            campaign.save_campaign_result(journal, 'b', 0, lines=10)
            campaign.save_campaign_completion(journal, lines=10)
            self.assertIsNotNone(self.read(gates, 'a'))
            self.assertIsNotNone(self.read(gates, 'b'))
            # Restart invalidates prior completion before any new measurement.
            campaign.save_campaign_start(queue, journal)
            self.assertIsNone(self.read(gates, 'a'))

    def test_changed_other_member_refuses_whole_completed_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            gates, queue, journal = self.prepare(Path(tmp))
            campaign.save_campaign_start(queue, journal)
            for name in ('a', 'b'):
                campaign.save_campaign_result(journal, name, 0, lines=10)
            campaign.save_campaign_completion(journal, lines=10)
            before = journal.read_bytes()
            path = gates / 'gate_promote__b.json'
            path.write_bytes(path.read_bytes() + b'\n')
            self.assertIsNone(self.read(gates, 'a'))
            self.assertIsNone(self.read(gates, 'b'))
            self.assertEqual(before, journal.read_bytes())

    def test_measured_rejection_completes_campaign_but_keeps_false_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            gates, queue, journal = self.prepare(Path(tmp))
            row = queue.read_text().splitlines()[0].split('\t')
            doc = measured_document(Path(row[1]), Path(row[2]), [0.4] * 10)
            doc['provenance'] = {'commit': 'known-fixture'}
            (gates / 'gate_promote__a.json').write_text(json.dumps(doc))
            campaign.save_campaign_start(queue, journal)
            with self.assertRaises(ValueError):
                campaign.save_campaign_result(journal, 'a', False, lines=10)
            campaign.save_campaign_result(journal, 'a', 3, lines=10)
            campaign.save_campaign_result(journal, 'b', 0, lines=10)
            campaign.save_campaign_completion(journal, lines=10)
            self.assertIs(self.read(gates, 'a')['passed'], False)
            self.assertEqual('complete', json.loads(journal.read_text())['status'])

    def test_view_record_cannot_describe_an_incomplete_campaign_as_measured(self):
        from experiments import record_views
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gates, queue, journal = self.prepare(root)
            destination = root / 'ab_test_runtime/experiments'
            destination.parent.mkdir()
            gates.rename(destination)
            journal = destination / campaign.JOURNAL_NAME
            campaign.save_campaign_start(queue, journal)
            with self.assertRaises(ValueError):
                record_views.gate_score('a', str(root))
            for name in ('a', 'b'):
                campaign.save_campaign_result(journal, name, 0, lines=10)
            campaign.save_campaign_completion(journal, lines=10)
            evidence = record_views.gate_score('a', str(root))
            self.assertEqual(0.8, evidence['median_ecapa'])
            self.assertIs(evidence['passed'], True)

    def test_failed_or_forged_measurement_cannot_complete(self):
        for kind in ('technical', 'wrong_status', 'missing_scores', 'provenance_error'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                gates, queue, journal = self.prepare(Path(tmp))
                campaign.save_campaign_start(queue, journal)
                path = gates / 'gate_promote__a.json'
                doc = json.loads(path.read_text())
                if kind == 'missing_scores':
                    doc.pop('ecapa_scores')
                elif kind == 'provenance_error':
                    doc['provenance'] = {'error': 'missing dependency'}
                path.write_text(json.dumps(doc))
                if kind == 'technical':
                    campaign.save_campaign_result(journal, 'a', 7, lines=10)
                else:
                    with self.assertRaises(ValueError):
                        campaign.save_campaign_result(journal, 'a', 3 if kind == 'wrong_status' else 0, lines=10)
                with self.assertRaises(ValueError):
                    campaign.save_campaign_completion(journal, lines=10)
                self.assertIsNone(self.read(gates, 'a'))

    def test_corrupt_journal_refuses_but_unrelated_standalone_gate_keeps_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            gates, queue, journal = self.prepare(Path(tmp))
            standalone = gates / 'gate_promote__standalone.json'
            standalone.write_text('{"passed": true}')
            campaign.save_campaign_start(queue, journal)
            self.assertIsNotNone(self.read(gates, 'standalone'))
            for contents in ('broken', '[]', '{"schema_version":1}'):
                journal.write_text(contents)
                self.assertIsNone(self.read(gates, 'a'))
                self.assertIsNone(self.read(gates, 'standalone'))
