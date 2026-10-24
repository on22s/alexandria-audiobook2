"""Real book capture under the existing CPU audio task claim."""
from contextlib import ExitStack
from contextvars import copy_context
import threading
import unittest
from unittest.mock import patch
import core
from audio_generation_owner import AudioGenerationCancelled
from project_scope_admission import ensure_audio_scope_admission, ensure_scope_admission, is_scope_admitted
from sound_design_context import capture_sound_design_context, capture_sound_design_anchor
from tests import test_sound_design_context as fixture_module
from fastapi import HTTPException


class OwnedSoundCaptureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.SoundDesignContextTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.states = {'audio': {'running': True, 'cancel': False}}
        self.claim = 'a' * 32
        self.claims = {'audio': {'id': self.claim, 'phase': 'pending'}}
        for name,value in [('DATA_DIR',str(self.root)),('process_state',self.states),
                           ('_task_claims',self.claims),('_gpu_leases',{})]:
            self.stack.enter_context(patch.object(core,name,value))

    def test_owned_capture_reads_real_context_while_idle_only_edits_still_refuse(self):
        def run():
            with self.assertRaises(HTTPException):
                with ensure_scope_admission(self.root):self.fail('Ordinary edit admitted')
            before = self.fixture.files()
            with ensure_audio_scope_admission(self.root):
                snapshot = capture_sound_design_context(self.root)
                anchor = capture_sound_design_anchor(self.root,snapshot,'synthetic-uid')
                self.assertEqual('synthetic-uid',anchor['selection']['chunk_uid'])
                self.assertTrue(is_scope_admitted(self.root))
                copied = copy_context(); observed = []
                worker = threading.Thread(target=lambda: observed.append(copied.run(is_scope_admitted,self.root)))
                worker.start(); worker.join(5)
                self.assertFalse(worker.is_alive()); self.assertEqual([False],observed)
            self.assertFalse(is_scope_admitted(self.root))
            after = self.fixture.files()
            self.assertEqual(before,{name:after[name] for name in before})
            self.assertLessEqual(set(after)-set(before),
                {'chunks.json.lock','voice_config.json.lock','annotated_script.json.lock','.active_book_transaction.json.lock'})
            self.assertTrue(self.states['audio']['running'])
            self.assertEqual(self.claim,self.claims['audio']['id'])
        core._run_owned_background_task('audio',self.claim,run)
        self.assertFalse(self.states['audio']['running'])

    def test_unowned_and_foreign_root_cannot_capture(self):
        with self.assertRaisesRegex(ValueError,'existing audio task owner'):
            with ensure_audio_scope_admission(self.root):self.fail('Unowned capture admitted')
        def run():
            with self.assertRaisesRegex(ValueError,'configured project'):
                with ensure_audio_scope_admission(self.root.parent):self.fail('Foreign root admitted')
        core._run_owned_background_task('audio',self.claim,run)

    def test_cancel_or_replaced_owner_refuses_before_and_after_capture(self):
        for fault in ('cancel','replacement'):
            for timing in ('before','during'):
                with self.subTest(fault=fault,timing=timing):
                    self.states['audio'].update(running=True,cancel=False)
                    self.claims['audio']={'id':self.claim,'phase':'pending'}
                    def invalidate():
                        if fault=='cancel':self.states['audio']['cancel']=True
                        else:self.claims['audio']['id']='b'*32
                    def run():
                        if timing=='before':invalidate()
                        with self.assertRaises(AudioGenerationCancelled):
                            with ensure_audio_scope_admission(self.root):
                                capture_sound_design_context(self.root)
                                invalidate()
                        self.assertFalse(is_scope_admitted(self.root))
                    core._run_owned_background_task('audio',self.claim,run)
                    if fault=='replacement':
                        self.assertEqual('b'*32,self.claims['audio']['id'])
                        self.assertTrue(self.states['audio']['running'])

    def test_real_owner_renders_then_admits_current_candidate_and_cancels_on_exit(self):
        from sound_design_render import save_sound_design_candidate
        from sound_design_current import ensure_current_sound_candidate
        from tests.test_export_pcm_plan import clip
        audio = clip(24000,4800)
        with audio.export(self.fixture.audio,format='wav'): pass
        retained = []
        def run():
            with ensure_audio_scope_admission(self.root):
                revision = capture_sound_design_context(self.root)['context_revision']
            result = save_sound_design_candidate(self.root,revision)
            stage = self.root/result['directory']; retained.append(stage)
            with ensure_current_sound_candidate(self.root,stage.name,result['candidate_revision']) as (_,stream):
                self.assertEqual(audio.raw_data,stream.read())
                with self.assertRaises(HTTPException):
                    with ensure_scope_admission(self.root): self.fail('Edit bypassed audio owner')
            with self.assertRaises(AudioGenerationCancelled):
                with ensure_current_sound_candidate(self.root,stage.name,result['candidate_revision']):
                    self.states['audio']['cancel'] = True
            self.assertTrue(self.states['audio']['running'])
            self.assertEqual(self.claim,self.claims['audio']['id'])
        core._run_owned_background_task('audio',self.claim,run)
        self.assertFalse(self.states['audio']['running'])
        self.assertEqual(audio.raw_data,(retained[0]/'mixed.pcm').read_bytes())
        self.assertFalse(is_scope_admitted(self.root))

    def test_replaced_owner_during_candidate_use_is_not_released_by_old_worker(self):
        from sound_design_render import save_sound_design_candidate
        from sound_design_current import ensure_current_sound_candidate
        from tests.test_export_pcm_plan import clip
        with clip(24000,4800).export(self.fixture.audio,format='wav'): pass
        def run():
            with ensure_audio_scope_admission(self.root):
                revision = capture_sound_design_context(self.root)['context_revision']
            result = save_sound_design_candidate(self.root,revision)
            stage = self.root/result['directory']
            with self.assertRaises(AudioGenerationCancelled):
                with ensure_current_sound_candidate(self.root,stage.name,result['candidate_revision']):
                    self.claims['audio']['id'] = 'b'*32
        core._run_owned_background_task('audio',self.claim,run)
        self.assertTrue(self.states['audio']['running'])
        self.assertEqual('b'*32,self.claims['audio']['id'])
        self.assertFalse(is_scope_admitted(self.root))
