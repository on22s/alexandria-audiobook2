"""Actual mixed MP3 publication preserves old delivery on stale sources."""
import unittest
from unittest.mock import patch
import audio_pcm_mux
from project import ProjectManager
from tests import test_sound_design_approval as fixtures
from sound_design_delivery import save_sound_delivery_mp3


class SoundDeliveryExportTests(unittest.TestCase):
    setUp = fixtures.SoundApprovalTests.setUp
    run_owned = fixtures.SoundApprovalTests.run_owned
    approve = fixtures.SoundApprovalTests.approve

    def test_real_delivery_and_late_source_change_preserve_prior_output(self):
        def check(candidate, revision):
            approval = self.approve(candidate, revision, full_mix_reviewed=True)
            before = {p.name:p.read_bytes() for p in candidate.iterdir()}
            manager = ProjectManager(str(self.root))
            options = {'format':'mp3','mastering':None,'export_quality':None}
            def deliver():
                return save_sound_delivery_mp3(manager, candidate.name, revision,
                                               approval['approval_revision'], options)
            result = deliver()
            self.assertEqual('verified', result['quality']['status'])
            self.assertEqual('mixed_audiobook.mp3', result['file'])
            destination = self.root/result['file']
            old = destination.read_bytes()
            self.assertTrue(old)
            self.assertEqual(before, {p.name:p.read_bytes() for p in candidate.iterdir()})
            encode = audio_pcm_mux.export_bounded_pcm
            def revoke_after_encode(*args, **kwargs):
                report = encode(*args, **kwargs)
                (candidate/'approval.json').unlink()
                return report
            with patch.object(audio_pcm_mux,'export_bounded_pcm',side_effect=revoke_after_encode):
                with self.assertRaisesRegex(ValueError,'source changed'):
                    deliver()
            self.assertEqual(old,destination.read_bytes())
            self.assertFalse((candidate/'approval.json').exists())
            # Fresh explicit fixture consent restores the receipt for the
            # independent late-prose-edit control below.
            self.assertEqual(approval,self.approve(candidate,revision,full_mix_reviewed=True))
            def change_after_encode(*args, **kwargs):
                report = encode(*args, **kwargs)
                self.fixture.source.write_text('Source changed after encoding')
                return report
            with patch.object(audio_pcm_mux, 'export_bounded_pcm', side_effect=change_after_encode):
                with self.assertRaisesRegex(ValueError, 'source changed'):
                    deliver()
            self.assertEqual(old, destination.read_bytes())
            self.assertEqual(before, {p.name:p.read_bytes() for p in candidate.iterdir()})
        self.run_owned(check)

    def test_repeated_success_does_not_exhaust_retained_stage_slots(self):
        from sound_design_staging import MAX_STAGES
        def check(candidate, revision):
            approval = self.approve(candidate, revision, full_mix_reviewed=True)
            for _ in range(MAX_STAGES + 1):
                result = save_sound_delivery_mp3(ProjectManager(str(self.root)),candidate.name,
                    revision,approval['approval_revision'],
                    {'format':'mp3','mastering':None,'export_quality':None})
                self.assertEqual('published', result['status'])
                self.assertEqual([],result['warnings'])
                self.assertEqual([candidate],list(candidate.parent.glob('render-*')))
        self.run_owned(check)

    def test_postpublication_stat_failure_reports_published_without_stale_quality(self):
        import export_quality
        def check(candidate, revision):
            approval = self.approve(candidate, revision, full_mix_reviewed=True)
            read_version = export_quality.get_export_file_version
            def fail_final(path):
                if str(path).endswith('mixed_audiobook.mp3'):
                    from book_state_transaction import is_book_state_admitted
                    self.assertTrue(is_book_state_admitted(self.root))
                    raise OSError('synthetic final stat failure')
                return read_version(path)
            with patch.object(export_quality,'get_export_file_version',side_effect=fail_final):
                result = save_sound_delivery_mp3(ProjectManager(str(self.root)),candidate.name,
                    revision,approval['approval_revision'],
                    {'format':'mp3','mastering':None,'export_quality':None})
            self.assertEqual('published',result['status'])
            self.assertIsNone(result['quality'])
            self.assertEqual(1,len(result['warnings']))
            self.assertTrue((self.root/result['file']).is_file())
            self.assertEqual([candidate],list(candidate.parent.glob('render-*')))
        self.run_owned(check)

    def test_postpublication_cleanup_preserves_unexpected_stage_contents(self):
        import os
        from pathlib import Path
        def check(candidate, revision):
            approval = self.approve(candidate, revision, full_mix_reviewed=True)
            replace = os.replace
            retained = []
            def add_recovery_note(source,destination):
                replace(source,destination)
                if str(destination).endswith('mixed_audiobook.mp3'):
                    path = Path(source).parent/'recovery-note'
                    path.write_bytes(b'preserve for inspection')
                    retained.append(path)
            with patch('sound_design_delivery.os.replace',side_effect=add_recovery_note):
                result = save_sound_delivery_mp3(ProjectManager(str(self.root)),candidate.name,
                    revision,approval['approval_revision'],
                    {'format':'mp3','mastering':None,'export_quality':None})
            self.assertEqual('published',result['status'])
            self.assertEqual(1,len(result['warnings']))
            self.assertEqual(b'preserve for inspection',retained[0].read_bytes())
        self.run_owned(check)

    def test_approved_wav_delivers_exact_candidate_pcm(self):
        from sound_design_approval import save_sound_candidate_approval
        from sound_design_delivery import save_sound_delivery
        from pydub import AudioSegment
        def check(candidate, revision):
            options = {'format':'wav','mastering':None,'export_quality':None}
            approval = save_sound_candidate_approval(self.root,candidate.name,revision,
                options,full_mix_reviewed=True)
            result = save_sound_delivery(ProjectManager(str(self.root)), candidate.name,
                revision,approval['approval_revision'],options)
            self.assertEqual('mixed_audiobook.wav',result['file'])
            self.assertEqual('verified',result['quality']['status'])
            with (self.root/result['file']).open('rb') as stream:
                self.assertEqual((candidate/'mixed.pcm').read_bytes(), AudioSegment.from_wav(stream).raw_data)
        self.run_owned(check)

    def test_m4b_delivers_approved_metadata_and_rejects_changed_title(self):
        from sound_design_approval import save_sound_candidate_approval
        from sound_design_delivery import save_sound_delivery
        def check(candidate,revision):
            options = {'format':'m4b','mastering':None,'export_quality':None,
                'm4b':{'title':'Synthetic 雪 = title','author':'Synthetic author','per_chunk_chapters':True}}
            approval = save_sound_candidate_approval(self.root,candidate.name,revision,
                options,full_mix_reviewed=True)
            manager = ProjectManager(str(self.root))
            result = save_sound_delivery(manager,candidate.name,revision,approval['approval_revision'],options)
            self.assertEqual('mixed_audiobook.m4b',result['file'])
            self.assertEqual('verified',result['quality']['status'])
            self.assertEqual(2,result['quality']['chapters_checked'])
            self.assertEqual(6,result['quality']['tags_checked'])
            import json,subprocess
            document=json.loads((candidate/'candidate.json').read_bytes())
            plan=document['binding']['pcm_plan']
            boundary=plan['source_frame_ranges'][1][0]/plan['frame_rate']
            # The first chapter owns the intervening half-second pause.
            self.assertAlmostEqual(.7,boundary,delta=.001)
            probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_chapters',
                '-of','json',str(self.root/result['file'])],timeout=10))
            chapters=probe['chapters'];self.assertEqual(2,len(chapters))
            self.assertAlmostEqual(0,float(chapters[0]['start_time']),places=3)
            self.assertAlmostEqual(boundary,float(chapters[0]['end_time']),places=3)
            self.assertAlmostEqual(boundary,float(chapters[1]['start_time']),places=3)
            self.assertAlmostEqual(plan['frames']/plan['frame_rate'],float(chapters[1]['end_time']),places=3)
            before = (self.root/result['file']).read_bytes()
            changed = {**options,'m4b':{**options['m4b'],'title':'Unapproved title'}}
            with self.assertRaisesRegex(ValueError,'does not match'):
                save_sound_delivery(manager,candidate.name,revision,approval['approval_revision'],changed)
            self.assertEqual(before,(self.root/result['file']).read_bytes())
        from tests.test_export_pcm_plan import clip
        first={**self.fixture.row,'pause_after':500}
        second={**self.fixture.row,'uid':'synthetic-second','id':1,
                'text':'Another synthetic spoken line.','audio_path':'second.wav'}
        self.fixture.source.write_text(first['text']+'\n'+second['text'])
        self.fixture.write('annotated_script.json',[first,second])
        self.fixture.write('chunks.json',[first,second])
        with clip(24000,48000).export(self.root/'second.wav',format='wav'):pass
        self.run_owned(check)

    def test_uploaded_cover_is_approved_exactly_and_replacement_refuses_delivery(self):
        import hashlib
        import subprocess
        from sound_design_approval import save_sound_candidate_approval
        from sound_design_delivery import save_sound_delivery
        from tests.test_m4b_cover_admission import image_bytes
        def check(candidate,revision):
            cover = image_bytes('PNG')
            path = self.root/'m4b_cover.jpg'
            path.write_bytes(cover)
            options = {'format':'m4b','mastering':None,'export_quality':None,
                       'm4b':{'cover_sha256':hashlib.sha256(cover).hexdigest()}}
            approval = save_sound_candidate_approval(self.root,candidate.name,revision,
                options,full_mix_reviewed=True)
            manager = ProjectManager(str(self.root))
            result = save_sound_delivery(manager,candidate.name,revision,approval['approval_revision'],options)
            output = self.root/result['file']
            actual = subprocess.check_output(['ffmpeg','-v','error','-i',str(output),
                '-map','0:v:0','-c:v','copy','-f','image2pipe','-'],timeout=10)
            self.assertEqual(cover,actual)
            before = output.read_bytes()
            import audio_m4b_pcm
            encode = audio_m4b_pcm.run_owned_media_check
            for fault in ('missing','substituted'):
                def corrupt_cover(arguments,*args,**kwargs):
                    from pathlib import Path
                    import os
                    result = encode(arguments,*args,**kwargs)
                    directory = Path(arguments[-1])
                    final = directory/'output.m4b'
                    changed = directory/'fixture-corrupted.m4b'
                    command = ['ffmpeg','-nostdin','-v','error','-i',str(final)]
                    if fault == 'substituted':
                        replacement = directory/'fixture-cover.jpg'
                        replacement.write_bytes(image_bytes('JPEG'))
                        command += ['-i',str(replacement)]
                    command += ['-map','0:a:0']
                    if fault == 'substituted':
                        command += ['-map','1:v:0','-disposition:v:0','attached_pic']
                    command += ['-c','copy',str(changed)]
                    subprocess.run(command,check=True,timeout=10,capture_output=True)
                    os.replace(changed,final)
                    return result
                with self.subTest(fault=fault), patch.object(audio_m4b_pcm,'run_owned_media_check',side_effect=corrupt_cover):
                    with self.assertRaisesRegex(ValueError,'attached picture|cover differs'):
                        save_sound_delivery(manager,candidate.name,revision,approval['approval_revision'],options)
                self.assertEqual(before,output.read_bytes())
            path.write_bytes(image_bytes('JPEG'))
            with self.assertRaisesRegex(ValueError,'cover changed'):
                save_sound_delivery(manager,candidate.name,revision,approval['approval_revision'],options)
            self.assertEqual(before,output.read_bytes())
        self.run_owned(check)

    def test_source_cover_uses_charged_stage_and_late_companion_edit_refuses_publication(self):
        import hashlib,json,subprocess
        import audio_m4b_pcm
        from pathlib import Path
        from book_state_transaction import ensure_book_state
        from epub_publication import get_epub_publication_candidate
        from publication_preview import get_publication_review
        from publication_source import get_publication_source_path
        from tests.test_upload_publication_source import epub
        from tests.test_m4b_cover_admission import image_bytes
        from sound_design_approval import save_sound_candidate_approval
        from sound_design_delivery import save_sound_delivery
        cover = image_bytes('PNG')
        source = self.fixture.source.read_bytes()
        row = get_epub_publication_candidate(epub(cover=cover,text=source),filename='synthetic.epub',
                                             text_sha256=hashlib.sha256(source).hexdigest())
        companion = Path(get_publication_source_path(self.fixture.source))
        companion.write_text(json.dumps({'version':1,'binding':{'kind':'text',
            'sha256':hashlib.sha256(source).hexdigest()},'candidates':[row]}))
        def check(candidate,revision):
            with ensure_book_state(self.root,recover=False):
                review = get_publication_review(self.root)
            selection = dict(expected_book_token=review['book_token'],expected_revision=review['revision'],
                             origin_id=row['id'],reviewed=True)
            options = {'format':'m4b','mastering':None,'export_quality':None,
                'm4b':{'cover_sha256':row['cover']['sha256'],'cover_source':selection}}
            uploaded = self.root/'m4b_cover.jpg'
            uploaded.write_bytes(image_bytes('JPEG'))
            with ensure_book_state(self.root,recover=False):
                conflicting_review = get_publication_review(self.root)
            conflicting = {**options,'m4b':{'cover_sha256':hashlib.sha256(uploaded.read_bytes()).hexdigest(),
                'cover_source':{**selection,'expected_revision':conflicting_review['revision']}}}
            with self.assertRaisesRegex(ValueError,'uploaded cover takes precedence'):
                save_sound_candidate_approval(self.root,candidate.name,revision,conflicting,full_mix_reviewed=True)
            self.assertFalse((candidate/'approval.json').exists())
            uploaded.unlink()
            approved = save_sound_candidate_approval(self.root,candidate.name,revision,options,full_mix_reviewed=True)
            manager = ProjectManager(str(self.root))
            def deliver():
                return save_sound_delivery(manager,candidate.name,revision,approved['approval_revision'],options)
            result = deliver()
            output = self.root/result['file']
            actual = subprocess.check_output(['ffmpeg','-v','error','-i',str(output),
                '-map','0:v:0','-c:v','copy','-f','image2pipe','-'],timeout=10)
            self.assertEqual(cover,actual)
            self.assertFalse(list(self.root.rglob('.m4b-source-cover-*')))
            before = output.read_bytes()
            encode = audio_m4b_pcm.run_owned_media_check
            def late_edit(*args,**kwargs):
                result = encode(*args,**kwargs)
                companion.write_bytes(companion.read_bytes()+b' ')
                return result
            with patch.object(audio_m4b_pcm,'run_owned_media_check',side_effect=late_edit):
                with self.assertRaisesRegex(ValueError,'source changed'):
                    deliver()
            self.assertEqual(before,output.read_bytes())
        self.run_owned(check)
