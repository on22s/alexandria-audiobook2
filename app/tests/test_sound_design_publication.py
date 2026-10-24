"""Native final publication guard includes cue/approval/candidate files."""
import unittest
from unittest.mock import patch
from tests import test_sound_design_approval as fixtures
from sound_design_publication import capture_sound_delivery_snapshot
from export_quality import get_export_file_version
from project import ProjectManager


class SoundPublicationTests(unittest.TestCase):
    setUp = fixtures.SoundApprovalTests.setUp
    run_owned = fixtures.SoundApprovalTests.run_owned
    approve = fixtures.SoundApprovalTests.approve

    def test_native_snapshot_refuses_late_approval_edit_after_content_hash(self):
        def check(stage,revision):
            approval = self.approve(stage,revision,full_mix_reviewed=True)
            options = {'format':'mp3','export_quality':None,'mastering':None}
            snapshot = capture_sound_delivery_snapshot(ProjectManager(str(self.root)),stage.name,
                revision,approval['approval_revision'],options)
            self.assertIn(str(stage/'approval.json'),snapshot.identities)
            self.assertIn(str(stage/'mixed.pcm'),snapshot.identities)
            output = self.root/'fixture.pending'; output.write_bytes(b'private output')
            report = {'file_version':get_export_file_version(output)}
            snapshot.verify_sources()
            with snapshot.ensure_publication(str(output),report): pass
            path = stage/'approval.json'; path.write_bytes(path.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'source changed'):
                with snapshot.ensure_publication(str(output),report): self.fail('Changed approval published')
            self.assertEqual(b'private output',output.read_bytes())
        self.run_owned(check)

    def test_native_snapshot_refuses_candidate_pcm_edit_during_encoding(self):
        def check(stage,revision):
            approval = self.approve(stage,revision,full_mix_reviewed=True)
            snapshot = capture_sound_delivery_snapshot(ProjectManager(str(self.root)),stage.name,
                revision,approval['approval_revision'],{'format':'mp3','export_quality':None,'mastering':None})
            path = stage/'mixed.pcm'; raw = bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'source changed'): snapshot.verify_sources()
        self.run_owned(check)

    def test_snapshot_refuses_transient_candidate_change_before_encoding(self):
        import sound_design_publication
        def check(stage, revision):
            approval = self.approve(stage, revision, full_mix_reviewed=True)
            options = {'format':'mp3','export_quality':None,'mastering':None}
            path = stage/'mixed.pcm'
            before = path.read_bytes()
            snapshot_type = sound_design_publication.ExportSourceSnapshot
            def transient(*args, **kwargs):
                changed = bytearray(before); changed[-1] ^= 1; path.write_bytes(changed)
                try:
                    return snapshot_type(*args, **kwargs)
                finally:
                    path.write_bytes(before)
            with patch.object(sound_design_publication, 'ExportSourceSnapshot', side_effect=transient):
                with self.assertRaisesRegex(ValueError, 'publication sources changed before capture'):
                    capture_sound_delivery_snapshot(ProjectManager(str(self.root)), stage.name,
                        revision, approval['approval_revision'], options)
        self.run_owned(check)

    def test_snapshot_must_include_expected_missing_cover_source(self):
        import sound_design_publication
        def check(stage, revision):
            approval = self.approve(stage, revision, full_mix_reviewed=True)
            options = {'format':'mp3','export_quality':None,'mastering':None}
            path = str(self.root/'m4b_cover.jpg')
            snapshot_type = sound_design_publication.ExportSourceSnapshot
            def omitted(*args, **kwargs):
                snapshot = snapshot_type(*args, **kwargs)
                snapshot.identities.pop(path)
                return snapshot
            with patch.object(sound_design_publication, 'capture_sound_delivery_cover',
                    return_value={'data':None, 'expected_sources':{path:None}}), patch.object(
                    sound_design_publication, 'ExportSourceSnapshot', side_effect=omitted):
                with self.assertRaisesRegex(ValueError, 'publication sources changed before capture'):
                    capture_sound_delivery_snapshot(ProjectManager(str(self.root)), stage.name,
                        revision, approval['approval_revision'], options)
        self.run_owned(check)
