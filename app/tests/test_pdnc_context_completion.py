"""Final context evidence must cover its hashed input sample in every arm."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from experiments import pdnc_context_evidence as context

REPO = Path(__file__).resolve().parents[2]
CHAIN = REPO / 'run_chains/pdnc_context_evidence.sh'


class ContextCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'pdnc_inputs').mkdir()
        (self.root / 'app/env/bin').mkdir(parents=True)
        (self.root / 'app/env/bin/python').symlink_to(sys.executable)
        (self.root / 'app/experiments').mkdir()
        (self.root / 'app/experiments/pdnc_context_evidence.py').symlink_to(REPO / 'app/experiments/pdnc_context_evidence.py')
        self.path = self.root / 'result.json'
        self.make_fixture('evidence')

    def make_fixture(self, intervention):
        books = context.get_context_books('confirmatory', intervention)
        arms = context.get_context_arm_names(intervention)
        self.bundle = self.root / f'pdnc_inputs/pdnc_{intervention}__confirmatory.json'
        entries = [dict(id=book+'-00001', line='Hello '+book, expected_speaker='Alice') for book in books]
        raw = json.dumps(dict(books=list(books), limit=120, entries=entries)).encode()
        self.bundle.write_bytes(raw)
        self.document = dict(meta=dict(phase='confirmatory', intervention=intervention,
            finished=1, validation='ok', gold_sha256=hashlib.sha256(raw).hexdigest(),
            decoding=dict(books=list(books), limit=120)),
            summary={arm: dict(n=len(books), correct=len(books)) for arm in arms},
            rows=[dict(arm=arm, id=book+':'+entry['id'], line=entry['line'], expected='Alice',
                       predicted='Alice', correct=True) for arm in arms for book,entry in zip(books,entries)])
        self.path.write_text(json.dumps(self.document))

    def validate(self, intervention='evidence', limit=120):
        return context.get_completed_context_result(self.path, self.bundle, 'confirmatory', intervention, limit)

    def test_all_three_interventions_accept_exact_complete_sample_without_writing(self):
        for intervention in ('evidence', 'sequence', 'targeted_sequence'):
            with self.subTest(intervention=intervention):
                self.make_fixture(intervention)
                before = self.path.read_bytes(), self.bundle.read_bytes()
                self.assertEqual(self.document, self.validate(intervention))
                self.assertEqual(before, (self.path.read_bytes(), self.bundle.read_bytes()))
                self.assertEqual(3 if intervention == 'targeted_sequence' else 2, len(self.document['summary']))

    def test_partial_wrong_phase_wrong_sample_and_inconsistent_counts_refuse(self):
        changes = [
            lambda d: d['meta'].pop('finished'),
            lambda d: d['meta'].update(finished=True),
            lambda d: d['meta'].update(finished=float('nan')),
            lambda d: d['meta'].update(validation=['partial']),
            lambda d: d['meta'].update(phase='pilot'),
            lambda d: d['meta'].update(intervention='sequence'),
            lambda d: d['meta']['decoding'].update(limit=119),
            lambda d: d['meta']['decoding']['books'].pop(),
            lambda d: d['rows'].pop(),
            lambda d: d['rows'].append(copy.deepcopy(d['rows'][0])),
            lambda d: d['rows'][0].update(id='unknown'),
            lambda d: d['rows'][0].update(arm='base'),
            lambda d: d['rows'][0].update(correct=1),
            lambda d: d['rows'][0].update(predicted=[]),
            lambda d: d['rows'][0].update(line='Another novel'),
            lambda d: d['summary']['baseline'].update(n=1000),
            lambda d: d['summary']['baseline'].update(correct=0),
            lambda d: d['summary']['baseline'].update(n=True),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                document = copy.deepcopy(self.document)
                change(document)
                self.path.write_text(json.dumps(document))
                with self.assertRaises(ValueError): self.validate()

    def test_changed_input_bundle_cannot_reuse_old_final_result(self):
        self.bundle.write_bytes(self.bundle.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'input bundle'): self.validate()

    def test_malformed_or_missing_book_sample_refuses_even_with_matching_hash(self):
        original = json.loads(self.bundle.read_bytes())
        for defect in ('empty', 'duplicate', 'other-book', 'bad-speaker', 'bool-limit'):
            with self.subTest(defect=defect):
                bundle = copy.deepcopy(original)
                if defect == 'empty': bundle['entries'].pop()
                elif defect == 'duplicate': bundle['entries'].append(copy.deepcopy(bundle['entries'][0]))
                elif defect == 'other-book': bundle['entries'][0]['id'] = 'Other-00001'
                elif defect == 'bad-speaker': bundle['entries'][0]['expected_speaker'] = None
                elif defect == 'bool-limit': bundle['limit'] = True
                raw = json.dumps(bundle).encode()
                self.bundle.write_bytes(raw)
                document = copy.deepcopy(self.document)
                document['meta']['gold_sha256'] = hashlib.sha256(raw).hexdigest()
                self.path.write_text(json.dumps(document))
                with self.assertRaises(ValueError): self.validate()

    def run_tail(self, cached, generated=None, worker_code=0):
        if cached is None: self.path.unlink(missing_ok=True)
        else: self.path.write_bytes(cached)
        generated_path = self.root / 'generated.json'
        generated_path.write_bytes(generated if generated is not None else json.dumps(self.document).encode())
        marker = self.root / 'started'
        marker.unlink(missing_ok=True)
        source = CHAIN.read_text()
        tail = source[source.index('if [ -f "$confirmatory" ]'):]
        script = ('set -uo pipefail\nrepo=' + shlex.quote(str(self.root)) + '\npilot=unused\n'
            'confirmatory=' + shlex.quote(str(self.path)) + '\n'
            'run_dir=' + shlex.quote(str(self.root)) + '\nintervention=evidence\n'
            'timeout() { cp ' + shlex.quote(str(generated_path)) + ' "$confirmatory"; touch '
            + shlex.quote(str(marker)) + '; return ' + str(worker_code) + '; }\n' + tail)
        result = subprocess.run(['bash', '-c', script], cwd=self.root / 'app',
            env=dict(os.environ, ALEXANDRIA_RUNTIME_ROOT=str(self.root), PYTHONPATH=str(REPO / 'app')),
            capture_output=True, text=True, timeout=30)
        return result, marker.exists()

    def test_actual_chain_regenerates_truncated_cache_and_checks_final_evidence(self):
        result, started = self.run_tail(b'{"meta":')
        self.assertTrue(started, result.stdout + result.stderr)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('ALREADY_COMPLETE', result.stdout)
        self.assertIn('CAMPAIGN_DONE', result.stdout)
        self.validate()

    def test_actual_chain_valid_cache_is_byte_identical_and_skips_worker(self):
        before = self.path.read_bytes()
        result, started = self.run_tail(before)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(started)
        self.assertIn('ALREADY_COMPLETE', result.stdout)
        self.assertEqual(before, self.path.read_bytes())

    def test_actual_chain_success_without_complete_evidence_refuses_done(self):
        result, started = self.run_tail(None, generated=b'{"meta":{}}')
        self.assertTrue(started, result.stdout + result.stderr)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('CAMPAIGN_DONE', result.stdout)
        self.assertIn('REFUSING incomplete context result', result.stderr)

    def test_actual_chain_keeps_worker_failure_status(self):
        result, started = self.run_tail(None, generated=b'{}', worker_code=7)
        self.assertTrue(started, result.stdout + result.stderr)
        self.assertEqual(7, result.returncode)
        self.assertIn('CONFIRMATORY_FAILED rc=7', result.stdout)


if __name__ == '__main__':
    unittest.main()
