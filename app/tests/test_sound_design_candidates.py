"""Inventory and explicit discard preserve neighboring candidates and sources."""
import tempfile
from pathlib import Path
import unittest
from sound_design_candidates import list_sound_candidates, discard_sound_candidate
from sound_design_staging import DIRECTORY


class SoundCandidateRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.source = self.root/'source.wav'; self.source.write_bytes(b'original')
        self.parent = self.root/DIRECTORY; self.parent.mkdir()
        self.stage = self.parent/'render-crashed'; self.stage.mkdir()
        (self.stage/'partial.pcm').write_bytes(b'partial')

    def test_incomplete_and_invalid_metadata_remain_visible_unverified(self):
        row = list_sound_candidates(self.root)['candidates'][0]
        self.assertEqual('incomplete',row['status']); self.assertFalse(row['media_verified'])
        (self.stage/'candidate.json').write_bytes(b'not JSON')
        row = list_sound_candidates(self.root)['candidates'][0]
        self.assertEqual('invalid_metadata',row['status'])
        self.assertEqual(b'not JSON',(self.stage/'candidate.json').read_bytes())

    def test_changed_stage_rejects_old_discard_then_explicit_refresh_removes_only_stage(self):
        before = list_sound_candidates(self.root)['candidates'][0]
        neighbor = self.parent/'render-neighbor'; neighbor.mkdir()
        (neighbor/'keep').write_bytes(b'keep')
        (self.stage/'partial.pcm').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):
            discard_sound_candidate(self.root,self.stage.name,before['recovery_revision'])
        row = next(row for row in list_sound_candidates(self.root)['candidates'] if row['stage']==self.stage.name)
        discard_sound_candidate(self.root,self.stage.name,row['recovery_revision'])
        self.assertFalse(self.stage.exists())
        self.assertEqual(b'keep',(neighbor/'keep').read_bytes())
        self.assertEqual(b'original',self.source.read_bytes())

    def test_alias_refuses_inventory_and_discard_without_touching_source(self):
        row = list_sound_candidates(self.root)['candidates'][0]
        (self.stage/'alias').symlink_to(self.source)
        with self.assertRaises(ValueError): list_sound_candidates(self.root)
        with self.assertRaises(ValueError): discard_sound_candidate(self.root,self.stage.name,row['recovery_revision'])
        self.assertEqual(b'original',self.source.read_bytes())

    def test_unavailable_encoder_preserves_inventory_and_revision_bound_recovery(self):
        from unittest.mock import patch
        from tts import ExportCancelled
        for error in (ValueError('private encoder detail'), OSError('private path')):
            with patch(
                    'audio_encoder_identity.get_ffmpeg_runtime_identity',side_effect=error):
                inventory = list_sound_candidates(self.root)
            self.assertEqual(1,len(inventory['candidates']))
            self.assertFalse(inventory['delivery_profiles']['m4b']['available'])
            self.assertIsNone(inventory['delivery_profiles']['m4b']['revision'])
            self.assertNotIn('private',str(inventory['delivery_profiles']))
            for fmt in ('mp3','wav'):
                self.assertFalse(inventory['delivery_profiles'][fmt]['available'])
                self.assertIsNone(inventory['delivery_profiles'][fmt]['revision'])
        with patch('audio_encoder_identity.get_ffmpeg_runtime_identity',side_effect=ExportCancelled):
            with self.assertRaises(ExportCancelled):list_sound_candidates(self.root)
        row = inventory['candidates'][0]
        discard_sound_candidate(self.root,self.stage.name,row['recovery_revision'])
        self.assertFalse(self.stage.exists())
        self.assertEqual(b'original',self.source.read_bytes())
