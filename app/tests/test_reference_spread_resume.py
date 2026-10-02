"""Known valid PCM/artifacts discriminate every receipt dependency."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import wave

from experiments import reference_spread_resume as resume


class ReferenceSpreadResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'app/experiments').mkdir(parents=True)
        for name in ('app/config.json', 'app/experiments/ljspeech_generate.py',
                     'app/experiments/ljspeech_score.py', 'app/experiments/generation.py',
                     'app/tts.py', 'app/audio_validation.py'):
            (self.root / name).write_text('{}')
        for name in ('ref.wav', 'human.wav', 'clone.wav'):
            with wave.open(str(self.root / name), 'wb') as handle:
                handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
                handle.writeframes(b'\x00\x10' * 4800)
        self.build = self.root / 'build.json'
        self.generated = self.root / 'gen.json'
        self.score = self.root / 'score.json'
        self.receipt = self.root / 'receipt.json'
        self.write(self.build, {'ref_sample': 'ref.wav', 'ref_source_id': 'ref1',
            'test': [{'id': 'line1', 'book': 'voice', 'text': 'actual text',
                      'human_wav': 'human.wav', 'seconds': .2}]})
        self.write(self.generated, {'arms': ['clone'], 'seed': 1234, 'failures': [],
            'reference_id': 'ref1', 'rows': [{'id': 'line1', 'book': 'voice',
            'text': 'actual text', 'human_wav': 'human.wav', 'human_seconds': .2,
            'clone_wav': 'clone.wav'}]})
        self.write(self.score, {'arms': ['clone'], 'source': 'gen.json', 'ecapa_error': None,
            'summary': {'clone': {'ecapa': .75, 'n': 1}}, 'rows': [{'id': 'line1',
            'book': 'voice', 'human_seconds': .2, 'clone': {'ecapa': .75}}]})

    def write(self, path, document):
        path.write_text(json.dumps(document))

    def begin(self):
        resume.save_reference_begin(self.root, self.build, self.receipt)
        resume.save_reference_score_begin(self.root, self.build, self.generated, self.receipt)

    def finish(self):
        resume.save_reference_completion(self.root, self.build, self.generated, self.score, self.receipt)

    def check(self):
        return resume.get_completed_reference_arm(self.root, self.build, self.generated, self.score, self.receipt)

    def test_complete_receipt_and_audio_are_read_only(self):
        self.begin(); self.finish()
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertTrue(self.check())
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_changed_content_in_every_dependency_rejects_existing_receipt(self):
        self.begin(); self.finish()
        paths = list(self.root.rglob('*'))
        for path in paths:
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b' changed')
                with self.assertRaises((OSError, ValueError, RuntimeError)):
                    self.check()
                path.write_bytes(before)

    def test_changed_generation_after_score_start_cannot_publish_receipt(self):
        self.begin()
        self.generated.write_text(self.generated.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            self.finish()
        self.assertFalse(self.receipt.exists())

    def test_changed_inputs_during_generation_cannot_start_scoring(self):
        resume.save_reference_begin(self.root, self.build, self.receipt)
        (self.root / 'app/config.json').write_text('{"different":true}')
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            resume.save_reference_score_begin(self.root, self.build, self.generated, self.receipt)
        self.assertFalse(self.receipt.exists())

    def test_malformed_partial_or_wrong_outputs_cannot_publish_receipt(self):
        gen = json.loads(self.generated.read_text())
        score = json.loads(self.score.read_text())
        cases = []
        for mutation in ('rows', 'failures', 'seed', 'text', 'reference'):
            doc = copy.deepcopy(gen)
            if mutation == 'rows': doc['rows'] = []
            elif mutation == 'failures': doc['failures'] = [{'id': 'line1'}]
            elif mutation == 'seed': doc['seed'] = 2
            elif mutation == 'text': doc['rows'][0]['text'] = 'wrong text'
            else: doc['reference_id'] = 'other'
            cases.append((doc, score))
        for mutation in ('rows', 'summary', 'source', 'error', 'bool', 'duplicate'):
            doc = copy.deepcopy(score)
            if mutation == 'rows': doc['rows'] = []
            elif mutation == 'summary': doc['summary']['clone']['ecapa'] = .5
            elif mutation == 'source': doc['source'] = 'other.json'
            elif mutation == 'error': doc['ecapa_error'] = 'provider unavailable'
            elif mutation == 'bool': doc['rows'][0]['clone']['ecapa'] = True
            else: doc['rows'].append(copy.deepcopy(doc['rows'][0]))
            cases.append((gen, doc))
        for index, (generated, scored) in enumerate(cases):
            with self.subTest(index=index):
                self.write(self.generated, generated); self.write(self.score, scored)
                self.begin()
                with self.assertRaises(ValueError):
                    self.finish()
                self.assertFalse(self.receipt.exists())
