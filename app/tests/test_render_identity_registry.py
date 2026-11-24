"""Versioned take voice identity: real admission, restoration, and legacy reads."""
import copy
import json
from pathlib import Path
import unittest

from audio_take_history import AudioTakeHistory
from project import get_chunk_generation_inputs
from synthesis_recovery_identity import get_recovery_digest
from take_publication import (KEY, get_take_publication_receipt, get_take_voice_identity,
                              validate_take_publication)
from take_restoration import capture_restore_target, restore_editor_take
from tests import test_take_publication, test_take_restoration
import tts
from tts import (get_chunk_voice_inputs, get_custom_voice_request,
                 get_pronunciation_render_context)

VOICES = {'ALICE': {'type': 'custom', 'voice': 'Ryan', 'seed': 1, 'description': 'd',
                    'character_style': 'calm', 'ref_audio': 'a.wav'}}
CHUNK = {'text': 'Hello.', 'speaker': 'ALICE', 'instruct': 'happy', 'take': 0}
MODEL = {'model_name': 'm'}


class TakeReceiptProjectionTests(unittest.TestCase):
    def do_restore(self, manager, request):
        return restore_editor_take(manager, 0, request,
                                   capture_restore_target(manager, 0, request))

    def edit_voice(self, manager, changes):
        path = Path(manager.voice_config_path)
        voices = json.loads(path.read_text())
        voices['NARRATOR'].update(changes)
        path.write_text(json.dumps(voices))
        manager.invalidate_config_cache()

    def test_emitted_digest_comes_from_actual_admission(self):
        cases = test_take_publication.TakePublicationTests()
        with cases.fixture() as (root, manager, _, _), cases.owner():
            engine = cases.engine()
            chunk = manager.load_chunks()[0]
            voice = {'type': 'custom', 'voice': 'Ryan', 'seed': 42,
                     'gender': 'male', 'character_style': 'calm'}
            parent, admitted = cases.admission(manager, engine, chunk, {'NARRATOR': voice})
            projected = get_take_voice_identity(voice, admitted.seed)
            self.assertEqual(admitted.voice_identity_sha256, get_recovery_digest(projected))
            self.assertNotEqual(get_recovery_digest({**voice, 'seed': admitted.seed}),
                                admitted.voice_identity_sha256)
            pcm, preparation, synthesis, observed = cases.render(root, engine, parent, chunk)
            revision = 'e' * 32
            synthesis = {**synthesis, 'audio_revision': revision, 'scope': 'pre-encoding PCM'}
            receipt, _ = get_take_publication_receipt(
                admitted, get_chunk_generation_inputs(chunk), preparation, observed,
                synthesis, pcm, pcm, revision)
            self.assertEqual(receipt['version'], 3)
            self.assertEqual(receipt['dependencies']['voice_sha256'],
                             admitted.voice_identity_sha256)
            self.assertEqual(validate_take_publication(
                {'uid': admitted.uid, 'audio_revision': revision, KEY: receipt}), receipt)

    def test_new_receipt_restores_metadata_only_edit_but_refuses_effective_change(self):
        fixture = test_take_restoration.TakeRestorationTests()
        with fixture.prepared() as (root, manager, engine, request, origin,
                                    original, current, displaced, saved):
            before = manager.get_voice_config_for_update()
            target = capture_restore_target(manager, 0, request)
            inputs_before = get_chunk_voice_inputs(before, target, 0)
            request_before = get_custom_voice_request(before, 'NARRATOR', 'x')
            self.edit_voice(manager, {'gender': 'female'})
            after = manager.get_voice_config_for_update()
            self.assertEqual(inputs_before, get_chunk_voice_inputs(after, target, 0))
            self.assertEqual(request_before, get_custom_voice_request(after, 'NARRATOR', 'x'))
            self.assertEqual(self.do_restore(manager, request)['uid'], target['uid'])
        with fixture.prepared() as (root, manager, engine, request, origin,
                                    original, current, displaced, saved):
            self.edit_voice(manager, {'voice': 'Jenny'})
            with self.assertRaisesRegex(ValueError, 'voice or seed selection'):
                self.do_restore(manager, request)
            self.assertEqual((root / current['audio_path']).read_bytes(), displaced)

    def test_legacy_receipts_keep_full_config_digest_and_stay_strict(self):
        fixture = test_take_restoration.TakeRestorationTests()
        for version in (1, 2):
            with self.subTest(version=version), fixture.prepared() as (
                    root, manager, engine, request, origin, original,
                    current, displaced, saved):
                history = AudioTakeHistory(root)
                with history._locked() as document:
                    record = next(row for row in document['records']
                                  if row['version'] == saved['version'])
                    legacy = fixture.legacy_receipt(manager, record['captured'][KEY], version)
                    record['captured'][KEY] = legacy
                    history._save(document)
                self.edit_voice(manager, {'gender': 'female'})
                with self.assertRaisesRegex(ValueError, 'voice or seed selection'):
                    self.do_restore(manager, request)
                self.assertEqual((root / current['audio_path']).read_bytes(), displaced)

    def test_unknown_and_tampered_receipts_stay_unproven(self):
        fixture = test_take_restoration.TakeRestorationTests()
        with fixture.prepared() as (root, manager, engine, request, origin,
                                    original, current, displaced, saved):
            history = AudioTakeHistory(root)
            with history._locked() as document:
                record = next(row for row in document['records']
                              if row['version'] == saved['version'])
                bad = copy.deepcopy(record['captured'][KEY])
                bad['version'] = 4
                with self.assertRaisesRegex(ValueError, 'unsupported receipt version'):
                    validate_take_publication({'uid': record['uid'],
                        'audio_revision': record['audio_revision'], KEY: bad})
                bad = copy.deepcopy(record['captured'][KEY])
                bad['dependencies']['voice_sha256'] = 'f' * 64
                record['captured'][KEY] = bad
                history._save(document)
            with self.assertRaisesRegex(ValueError, 'voice or seed selection'):
                self.do_restore(manager, request)
            self.assertEqual((root / current['audio_path']).read_bytes(), displaced)

    def test_centralized_fields_preserve_exact_identity_digests(self):
        self.assertEqual(tts.VOICE_PRONUNCIATION_EXCLUSIONS,
                         frozenset(('character_style', 'default_style', 'seed')))
        self.assertEqual(tts.VOICE_RENDER_IDENTITY_FIELDS, (
            'type', 'voice', 'character_style', 'default_style', 'seed',
            'ref_audio', 'ref_text', 'description', 'adapter_id', 'adapter_path',
            'adapter_generation_sha256', 'members'))
        self.assertEqual(get_chunk_voice_inputs(VOICES, CHUNK, 0), {'ALICE': {
            'type': 'custom', 'voice': 'Ryan', 'character_style': 'calm',
            'default_style': None, 'seed': 1, 'ref_audio': 'a.wav',
            'ref_text': None, 'description': None, 'adapter_id': None,
            'adapter_path': None, 'adapter_generation_sha256': None,
            'members': None}})
        context = get_pronunciation_render_context(VOICES, CHUNK, 0, MODEL)
        self.assertEqual(context['voice_sha256'],
                         'cac0dafc97460292e85233ccc8f50e0eb2ce7a1ba0f07c339e3f626b0a67a3ca')
        self.assertEqual(context['voice_sha256_legacy'],
                         '0f33d037204696344599ef2245bb658c35d039b9f616dbdee88e93361861c973')
        self.assertEqual(get_recovery_digest({**VOICES['ALICE'], 'seed': 1}),
                         '6eb0dbb95cdb884d56656542f7b71508f1811ec123cb97f47e83462455f697c6')


if __name__ == '__main__':
    unittest.main()
