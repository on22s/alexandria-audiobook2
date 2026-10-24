"""Corrupt candidate metadata must fail even with recomputed checksums."""
import copy
import json
import unittest
from tests import test_sound_design_capture as fixtures
from book_state_transaction import get_snapshot_digest
from sound_design_render import save_sound_design_candidate
from sound_design_candidate import get_sound_candidate_document, ensure_sound_candidate_open


class SoundCandidateReadTests(unittest.TestCase):
    def setUp(self):
        fixtures.SoundCaptureCompositionTests.setUp(self)
        revision = fixtures.SoundCaptureCompositionTests.revision(self)
        self.result = save_sound_design_candidate(self.root, revision)
        self.stage = self.root / self.result['directory']
        self.raw = (self.stage / 'candidate.json').read_bytes()
        self.document = json.loads(self.raw)

    def open(self):
        return ensure_sound_candidate_open(self.root, self.stage.name, self.result['candidate_revision'])

    def test_exact_candidate_opens_read_only_and_is_preserved(self):
        with self.open() as (document, stream):
            self.assertEqual(self.document, document)
            self.assertEqual(self.audio.raw_data, stream.read())
            self.assertFalse(stream.writable())
        self.assertEqual(self.raw, (self.stage / 'candidate.json').read_bytes())

    def test_rehashed_invalid_geometry_duplicate_ids_and_partial_scope_refuse(self):
        mutations = [lambda b: b['mix'].update(bytes=1),
                     lambda b: b['mix'].update(scope=[False,b['mix']['frames']]),
                     lambda b: b['mix'].update(scope=[1,b['mix']['frames']]),
                     lambda b: b['mix'].update(prequantization_peak=1.1),
                     lambda b: b['dialogue'].append(copy.deepcopy(b['dialogue'][0])),
                     lambda b: b.update(cue_versions=['a'*64,'a'*64]),
                     lambda b: b['pcm_plan'].update(sample_width=True)]
        for mutate in mutations:
            document = copy.deepcopy(self.document); mutate(document['binding'])
            revision = get_snapshot_digest(document['binding']); document['candidate_revision'] = revision
            with self.subTest(document=document), self.assertRaises(ValueError):
                get_sound_candidate_document(json.dumps(document).encode(), revision)

    def test_changed_pcm_before_and_during_read_refuses(self):
        path = self.stage / 'mixed.pcm'; original = path.read_bytes()
        path.write_bytes(original[:1] + b'\x00' + original[2:])
        with self.assertRaisesRegex(ValueError, 'PCM changed'):
            with self.open(): self.fail('Corrupt audio admitted')
        path.write_bytes(original)
        with self.assertRaises(ValueError):
            with self.open(): path.write_bytes(original[:-2])
        self.assertEqual(original[:-2], path.read_bytes())

    def test_path_escape_and_changed_manifest_refuse(self):
        for name in ('../render-other', str(self.stage), 'render-a/b', 'render-a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                with ensure_sound_candidate_open(self.root, name, self.result['candidate_revision']):
                    self.fail('Unsafe candidate path admitted')
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            with self.open(): (self.stage / 'candidate.json').write_bytes(self.raw + b' ')
