"""Real-owner explicit approval, revision CAS and recoverable failures."""
import json
import unittest
import core
from tests import test_sound_design_owned_capture as fixtures
from tests.test_export_pcm_plan import clip
from project_scope_admission import ensure_audio_scope_admission
from sound_design_context import capture_sound_design_context
from sound_design_render import save_sound_design_candidate
from sound_design_approval import save_sound_candidate_approval, get_sound_approval
from book_state_transaction import get_snapshot_digest


class SoundApprovalTests(unittest.TestCase):
    setUp = fixtures.OwnedSoundCaptureTests.setUp

    def run_owned(self, callback):
        with clip(24000,4800).export(self.fixture.audio,format='wav'): pass
        def run():
            with ensure_audio_scope_admission(self.root):
                context = capture_sound_design_context(self.root)['context_revision']
            result = save_sound_design_candidate(self.root,context)
            stage = self.root/result['directory']
            callback(stage,result['candidate_revision'])
        core._run_owned_background_task('audio',self.claim,run)

    def approve(self, stage, revision, **kwargs):
        return save_sound_candidate_approval(self.root,stage.name,revision,
            {'format':'mp3','export_quality':None,'mastering':None},**kwargs)

    def test_explicit_confirmation_cas_and_idempotent_reapproval(self):
        def check(stage,revision):
            for confirmation in (False,1,'true',None):
                with self.subTest(confirmation=confirmation), self.assertRaises(ValueError):
                    self.approve(stage,revision,full_mix_reviewed=confirmation)
            self.assertFalse((stage/'approval.json').exists())
            approved = self.approve(stage,revision,full_mix_reviewed=True)
            raw = (stage/'approval.json').read_bytes()
            self.assertEqual(approved,get_sound_approval(raw))
            before = (stage/'approval.json').stat().st_mtime_ns
            with self.assertRaisesRegex(ValueError,'approval changed'):
                self.approve(stage,revision,full_mix_reviewed=True)
            again = self.approve(stage,revision,full_mix_reviewed=True,
                                 expected_approval=approved['approval_revision'])
            self.assertEqual(approved,again)
            self.assertEqual(before,(stage/'approval.json').stat().st_mtime_ns)
        self.run_owned(check)

    def test_stale_candidate_preserves_old_approval(self):
        def check(stage,revision):
            approved = self.approve(stage,revision,full_mix_reviewed=True)
            before = (stage/'approval.json').read_bytes()
            self.fixture.source.write_text('Changed source')
            with self.assertRaisesRegex(ValueError,'stale'):
                self.approve(stage,revision,full_mix_reviewed=True,
                             expected_approval=approved['approval_revision'])
            self.assertEqual(before,(stage/'approval.json').read_bytes())
        self.run_owned(check)

    def test_full_quota_refuses_receipt_without_removing_candidate(self):
        def check(stage,revision):
            charged = sum(p.stat().st_size for p in stage.iterdir())
            with self.assertRaisesRegex(ValueError,'quota'):
                self.approve(stage,revision,full_mix_reviewed=True,max_staged_bytes=charged)
            self.assertFalse((stage/'approval.json').exists())
            self.assertTrue((stage/'mixed.pcm').exists())
        self.run_owned(check)

    def test_rehashed_false_consent_and_unknown_settings_refuse(self):
        def check(stage,revision):
            approved = self.approve(stage,revision,full_mix_reviewed=True)
            approved['full_mix_reviewed'] = 1
            approved['approval_revision'] = get_snapshot_digest(
                {key:value for key,value in approved.items() if key != 'approval_revision'})
            with self.assertRaises(ValueError): get_sound_approval(json.dumps(approved).encode())
            with self.assertRaises(ValueError):
                save_sound_candidate_approval(self.root,stage.name,revision,
                    {'format':'mp3','export_quality':{'unknown':True},'mastering':None},full_mix_reviewed=True)
        self.run_owned(check)

    def test_delivery_requires_exact_approval_settings_and_unchanged_receipt(self):
        from sound_design_approval import ensure_approved_sound_candidate
        def check(stage, revision):
            approved = self.approve(stage, revision, full_mix_reviewed=True)
            options = {'format':'mp3','export_quality':None,'mastering':None}
            with ensure_approved_sound_candidate(self.root,stage.name,revision,
                    approved['approval_revision'],options) as (_,stream):
                self.assertEqual(clip(24000,4800).raw_data,stream.read())
            for changed in ({**options,'format':'wav'}, {**options,'mastering':{}},
                            {**options,'export_quality':{'reject_unexpected_silence':True}}):
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError,'does not match'):
                    with ensure_approved_sound_candidate(self.root,stage.name,revision,
                            approved['approval_revision'],changed): self.fail('Unapproved settings admitted')
            with self.assertRaisesRegex(ValueError,'approval changed'):
                with ensure_approved_sound_candidate(self.root,stage.name,revision,
                        approved['approval_revision'],options):
                    path = stage/'approval.json'; path.write_bytes(path.read_bytes()+b' ')
        self.run_owned(check)

    def test_historical_profile_remains_readable_but_requires_new_approval(self):
        from sound_design_approval import ensure_approved_sound_candidate
        def check(stage, revision):
            approved = self.approve(stage, revision, full_mix_reviewed=True)
            self.assertEqual('128k', approved['encoding']['bitrate'])
            approved['encoding']['bitrate'] = '192k'
            approved['approval_revision'] = get_snapshot_digest(
                {key:value for key,value in approved.items() if key != 'approval_revision'})
            raw = json.dumps(approved).encode()
            (stage/'approval.json').write_bytes(raw)
            self.assertEqual(approved, get_sound_approval(raw))
            with self.assertRaisesRegex(ValueError, 'encoder profile changed'):
                with ensure_approved_sound_candidate(self.root,stage.name,revision,
                        approved['approval_revision'],{'format':'mp3','export_quality':None,'mastering':None}):
                    self.fail('Historical encoder settings silently reinterpreted')
            current = self.approve(stage, revision, full_mix_reviewed=True,
                                   expected_approval=approved['approval_revision'])
            self.assertEqual('128k', current['encoding']['bitrate'])
            self.assertNotEqual(approved['approval_revision'], current['approval_revision'])
        self.run_owned(check)

    def test_stale_displayed_encoder_profile_cannot_record_approval(self):
        from sound_design_approval import get_sound_delivery_profiles
        def check(stage,revision):
            with self.assertRaisesRegex(ValueError,'encoder profile changed'):
                self.approve(stage,revision,full_mix_reviewed=True,expected_encoding='0'*64)
            self.assertFalse((stage/'approval.json').exists())
            profile = get_sound_delivery_profiles()['mp3']
            approved = self.approve(stage,revision,full_mix_reviewed=True,expected_encoding=profile['revision'])
            self.assertEqual(get_snapshot_digest(approved['encoding']),profile['revision'])
        self.run_owned(check)

    def test_historical_pcm_profiles_parse_without_runtime_and_require_fresh_approval(self):
        from unittest.mock import patch
        from sound_design_approval import ensure_approved_sound_candidate
        from pcm_mux_profile import get_pcm_mux_profile
        def check(stage,revision):
            previous = None
            for fmt in ('mp3','wav'):
                options={'format':fmt,'export_quality':None,'mastering':None}
                current=save_sound_candidate_approval(self.root,stage.name,revision,options,
                    full_mix_reviewed=True,expected_approval=previous)
                keys=(('version','format','codec','bitrate','id3v2_version') if fmt=='mp3' else
                      ('version','format','codec','rf64'))
                old=json.loads(json.dumps(current))
                old['encoding']={key:current['encoding'][key] for key in keys}
                old['encoding']['version']=1
                if fmt=='mp3':old['encoding']['id3v2_version']=3
                old['approval_revision']=get_snapshot_digest({k:v for k,v in old.items() if k!='approval_revision'})
                raw=json.dumps(old).encode();(stage/'approval.json').write_bytes(raw)
                with patch('audio_encoder_identity.get_ffmpeg_runtime_identity',side_effect=AssertionError('Historical parser probed runtime')), \
                        patch('pcm_mux_profile.get_pcm_mux_profile',return_value={'future':'policy'}):
                    for record in (old,current):
                        self.assertEqual(record,get_sound_approval(json.dumps(record).encode()))
                with self.assertRaisesRegex(ValueError,'encoder profile changed'):
                    with ensure_approved_sound_candidate(self.root,stage.name,revision,old['approval_revision'],options):
                        self.fail('Old PCM transport authorized a new encoder')
                self.assertEqual(raw,(stage/'approval.json').read_bytes())
                restored=save_sound_candidate_approval(self.root,stage.name,revision,options,
                    full_mix_reviewed=True,expected_approval=old['approval_revision'])
                self.assertEqual(current,restored)
                previous=current['approval_revision']
        self.run_owned(check)

    def test_historical_m4b_profile_parses_without_encoder_probe_but_requires_new_approval(self):
        from unittest.mock import patch
        from sound_design_approval import ensure_approved_sound_candidate
        from m4b_mux_profile import get_m4b_mux_profile
        def check(stage,revision):
            options={'format':'m4b','export_quality':None,'mastering':None}
            current=save_sound_candidate_approval(self.root,stage.name,revision,options,full_mix_reviewed=True)
            historical = [current]
            with patch('audio_encoder_identity.get_ffmpeg_runtime_identity',side_effect=AssertionError('Historical parser probed runtime')):
                self.assertEqual(current,get_sound_approval(json.dumps(current).encode()))
                for version in (1,2):
                    old=json.loads(json.dumps(current));old['encoding']['version']=version
                    del old['encoding']['ffmpeg_runtime']
                    if version==1:
                        for key in get_m4b_mux_profile():del old['encoding'][key]
                    old['approval_revision']=get_snapshot_digest({k:v for k,v in old.items() if k!='approval_revision'})
                    self.assertEqual(old,get_sound_approval(json.dumps(old).encode()))
                    historical.append(old)
            profile = get_m4b_mux_profile()
            for future in ({**profile,'future_mux_field':'new-policy'},
                           {key:value for key,value in profile.items() if key != 'muxer'}):
                with patch('m4b_mux_profile.get_m4b_mux_profile',return_value=future):
                    for record in historical:
                        self.assertEqual(record,get_sound_approval(json.dumps(record).encode()))
            raw=json.dumps(old).encode();(stage/'approval.json').write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'encoder profile changed'):
                with ensure_approved_sound_candidate(self.root,stage.name,revision,old['approval_revision'],options):
                    self.fail('Historical runtime silently reinterpreted')
            self.assertEqual(raw,(stage/'approval.json').read_bytes())
        self.run_owned(check)

    def test_changed_native_runtime_refuses_displayed_profile_and_saved_approval(self):
        from unittest.mock import patch
        from audio_encoder_identity import get_ffmpeg_runtime_identity,FFmpegRuntimeIdentity
        from sound_design_approval import get_sound_delivery_profiles,ensure_approved_sound_candidate
        def check(stage,revision):
            options={'format':'m4b','export_quality':None,'mastering':None}
            displayed=get_sound_delivery_profiles()['m4b']['revision']
            runtime=get_ffmpeg_runtime_identity().model_dump();runtime['version_sha256']='0'*64
            changed=FFmpegRuntimeIdentity.model_validate(runtime)
            with patch('audio_encoder_identity.get_ffmpeg_runtime_identity',return_value=changed):
                with self.assertRaisesRegex(ValueError,'encoder profile changed'):
                    save_sound_candidate_approval(self.root,stage.name,revision,options,
                        full_mix_reviewed=True,expected_encoding=displayed)
            self.assertFalse((stage/'approval.json').exists())
            approved=save_sound_candidate_approval(self.root,stage.name,revision,options,
                full_mix_reviewed=True,expected_encoding=displayed)
            before=(stage/'approval.json').read_bytes()
            with patch('audio_encoder_identity.get_ffmpeg_runtime_identity',return_value=changed):
                with self.assertRaisesRegex(ValueError,'encoder profile changed'):
                    with ensure_approved_sound_candidate(self.root,stage.name,revision,
                            approved['approval_revision'],options):self.fail('Changed runtime admitted')
            self.assertEqual(before,(stage/'approval.json').read_bytes())
        self.run_owned(check)
