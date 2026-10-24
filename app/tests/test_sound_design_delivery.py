"""Approved delivery staging charges the copy and preserves prior candidates."""
import unittest
from unittest.mock import patch
from project import ProjectManager
from tests import test_sound_design_approval as fixtures
from sound_design_delivery import (ensure_sound_delivery_stage, SoundDeliveryIntegrityRequired,
                                  get_sound_source_review)


class SoundDeliveryStageTests(unittest.TestCase):
    setUp = fixtures.SoundApprovalTests.setUp
    run_owned = fixtures.SoundApprovalTests.run_owned
    approve = fixtures.SoundApprovalTests.approve

    def stage(self, candidate, revision, approval, **kwargs):
        return ensure_sound_delivery_stage(ProjectManager(str(self.root)), candidate.name,
            revision, approval['approval_revision'],
            {'format':'mp3','export_quality':None,'mastering':None},
            minimum_output_bytes=100000, **kwargs)

    def test_copy_is_exact_and_allowance_charges_retained_candidate_and_raw(self):
        def check(candidate, revision):
            approval = self.approve(candidate, revision, full_mix_reviewed=True)
            before = {p.name:p.read_bytes() for p in candidate.iterdir()}
            charged = sum(len(value) for value in before.values())
            raw_size = len(before['mixed.pcm'])
            cap = charged + raw_size + 100000
            with self.stage(candidate, revision, approval, max_staged_bytes=cap) as staged:
                self.assertEqual(before['mixed.pcm'], staged['raw'].read_bytes())
                self.assertEqual(100000, staged['output_allowance'])
                self.assertEqual(raw_size, staged['pcm_plan']['frames'] *
                    staged['pcm_plan']['channels'] * staged['pcm_plan']['sample_width'])
                staged['source_snapshot'].verify_sources()
            self.assertEqual(before, {p.name:p.read_bytes() for p in candidate.iterdir()})
        self.run_owned(check)

    def test_insufficient_peak_quota_and_failed_encoder_preserve_candidate(self):
        def check(candidate, revision):
            approval = self.approve(candidate, revision, full_mix_reviewed=True)
            before = {p.name:p.read_bytes() for p in candidate.iterdir()}
            cap = sum(map(len, before.values())) + len(before['mixed.pcm']) + 99999
            with self.assertRaisesRegex(ValueError, 'quota'):
                with self.stage(candidate, revision, approval, max_staged_bytes=cap):
                    self.fail('Insufficient peak quota admitted')
            with self.assertRaisesRegex(RuntimeError, 'encoder failed'):
                with self.stage(candidate, revision, approval) as staged:
                    (staged['directory']/'output.mp3').write_bytes(b'partial output')
                    raise RuntimeError('encoder failed')
            self.assertEqual([candidate], list(candidate.parent.glob('render-*')))
            self.assertEqual(before, {p.name:p.read_bytes() for p in candidate.iterdir()})
        self.run_owned(check)

    def test_native_encoder_consumes_reserved_copy_and_late_source_edit_refuses_publication(self):
        from audio_pcm_mux import export_bounded_pcm
        from export_quality import get_pcm_export_encoding_plan, get_admitted_export_report
        def check(candidate, revision):
            approval = self.approve(candidate, revision, full_mix_reviewed=True)
            with self.stage(candidate, revision, approval) as staged:
                output = staged['directory']/'output.mp3'
                quality = get_pcm_export_encoding_plan(staged['pcm_plan'], format_name='mp3')
                reports = []
                export_bounded_pcm(staged['raw'], output, staged['pcm_plan'], staged['check_active'],
                    bitrate='128k', quality_plan=quality, quality_callback=reports.append,
                    max_output_bytes=staged['output_allowance'])
                report = get_admitted_export_report(output, quality, reports[-1])
                self.assertEqual('verified', report['status'])
                snapshot = staged['source_snapshot']
                snapshot.verify_sources()
                with snapshot.ensure_publication(str(output), report):
                    pass  # Exercise the guard without publishing a test artifact.
                self.fixture.source.write_text('Changed after encoding')
                with self.assertRaisesRegex(ValueError, 'source changed'):
                    with snapshot.ensure_publication(str(output), report):
                        self.fail('Stale approved export admitted')
        self.run_owned(check)

    def test_mix_approval_does_not_replace_source_integrity_confirmation(self):
        self.fixture.source.write_text('Different synthetic source prose.')
        def check(candidate, revision):
            approval = self.approve(candidate, revision, full_mix_reviewed=True)
            for confirmation in (None, '0'*64):
                with self.assertRaises(SoundDeliveryIntegrityRequired) as caught:
                    with self.stage(candidate, revision, approval,
                                    integrity_confirmation=confirmation):
                        self.fail('Unreviewed source difference admitted')
                report = caught.exception.report
                self.assertEqual('differences', report['status'])
                self.assertEqual([candidate], list(candidate.parent.glob('render-*')))
            with patch('sound_design_delivery.capture_sound_delivery_snapshot', side_effect=AssertionError('page read audio')):
                page = get_sound_source_review(ProjectManager(str(self.root)),candidate.name,
                    revision,approval['approval_revision'],report['snapshot'])
            self.assertEqual(report,page)
            self.assertNotIn('hunks',page)
            with self.stage(candidate, revision, approval,
                            integrity_confirmation=report['snapshot']) as staged:
                self.assertEqual(report, staged['integrity'])
        self.run_owned(check)

    def test_journal_review_uses_exact_native_rows_and_refuses_stale_metadata(self):
        import json
        from chunk_status_journal import ChunkStatusJournal
        from merge_integrity import get_source_integrity
        from project_scope_admission import ensure_audio_scope_admission
        from sound_design_context import capture_sound_design_context
        from sound_design_publication import capture_sound_delivery_snapshot
        self.fixture.source.write_text('Different synthetic source prose.')
        # Reachable unavailable report: readable source, stale scope authority.
        self.fixture.write('state.json', {**self.fixture.state, '_source_scope': {}})
        journal = ChunkStatusJournal(self.root/'chunks.json',
            {key:self.fixture.state[key] for key in ('active_book_id','book_generation')})
        journal.apply_update(self.fixture.row['uid'], {'error':'Synthetic retained journal metadata'})
        def check(candidate, revision):
            approval=self.approve(candidate,revision,full_mix_reviewed=True)
            manager=ProjectManager(str(self.root))
            snapshot=capture_sound_delivery_snapshot(manager,candidate.name,revision,
                approval['approval_revision'],approval['delivery'])
            with ensure_audio_scope_admission(self.root):
                context=capture_sound_design_context(self.root)
            self.assertEqual(snapshot.chunks,context['chunks'])
            report=get_source_integrity(json.loads((self.root/'state.json').read_bytes()),
                (self.root/'annotated_script.json').read_bytes(),snapshot.chunks)
            self.assertEqual('unavailable',report['status'])
            self.assertIn('missing or stale',report['reason'])
            before=self.fixture.files()
            with (patch('sound_design_delivery.capture_sound_delivery_snapshot',side_effect=AssertionError('publication capture')),
                 patch('sound_design_candidate.read_bounded_audio_hash',side_effect=AssertionError('PCM hash')),
                 patch.object(ProjectManager,'load_chunks',side_effect=AssertionError('row migration')),
                 patch.object(ProjectManager,'get_engine',side_effect=AssertionError('model initialization'))):
                page=get_sound_source_review(manager,candidate.name,revision,
                    approval['approval_revision'],report['snapshot'])
            self.assertEqual(report['snapshot'],page['snapshot'])
            self.assertEqual(before,self.fixture.files())
            with self.assertRaises(ValueError):
                get_sound_source_review(manager,candidate.name,revision,
                    approval['approval_revision'],'0'*64)
            journal.apply_update(self.fixture.row['uid'], {'error':'Changed non-text journal field'})
            with self.assertRaisesRegex(ValueError,'context changed'):
                get_sound_source_review(manager,candidate.name,revision,
                    approval['approval_revision'],report['snapshot'])
        self.run_owned(check)
