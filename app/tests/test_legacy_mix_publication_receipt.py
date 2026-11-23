"""A real mixed-delivery publication is current even under a legacy scope record."""
import hashlib
import json
import unittest
from unittest.mock import patch

import core
from book_state_transaction import get_book_operation_token
from project import ProjectManager
from project_scope_apply import UNDO, apply_source_scope
from project_scope_exports import BASE_EXPORTS, LEGACY_EXPORTS
from project_scope_plan import get_scope_preview
from project_scope_admission import ensure_audio_scope_admission
from sound_design_context import capture_sound_design_context
from sound_design_delivery import save_sound_delivery_mp3
from sound_design_download import open_sound_delivery
from sound_design_render import save_sound_design_candidate
from tests import test_sound_design_approval as fixtures
from tests.test_export_pcm_plan import clip


class LegacyMixedPublicationTests(unittest.TestCase):
    approve = fixtures.SoundApprovalTests.approve

    def setUp(self):
        fixtures.SoundApprovalTests.setUp(self)
        row = json.loads((self.root / 'annotated_script.json').read_text())[0]
        row['text'] = 'Synthetic spoken line. ' * 40
        second = {**row, 'id': 1, 'uid': 'synthetic-uid-2',
                  'text': 'Second synthetic spoken line. ' * 40}
        script = [row, second]
        (self.root / 'annotated_script.json').write_text(json.dumps(script))
        (self.root / 'source.txt').write_text('\n'.join(item['text'] for item in script))
        from project import group_into_chunks
        grouped = group_into_chunks([row, second], include_source_indices=True)
        (self.root / 'chunks.json').write_text(json.dumps([
            {**entry, 'id': index, 'uid': f'uid-{index}', 'status': 'done',
             'audio_path': 'audio.wav', 'audio_revision': None}
            for index, entry in enumerate(grouped)]))

    def _scope_with_self_hold(self, keys):
        import project_scope_exports as exports
        with patch.object(exports, 'EXPORTS', keys):
            plan = get_scope_preview(self.root, 1)
            apply_source_scope(self.root, 1, plan['revision'])
        (self.root / UNDO).unlink(missing_ok=True)
        old = {}
        for fmt in ('wav', 'm4b'):
            payload = ('OLD CURRENT-SCOPE ' + fmt).encode()
            (self.root / ('mixed_audiobook.' + fmt)).write_bytes(payload)
            old[fmt] = payload
        return old

    def _deliver(self, keys):
        with clip(24000, 4800).export(self.fixture.audio, format='wav'):
            pass

        def run():
            with ensure_audio_scope_admission(self.root):
                old = self._scope_with_self_hold(keys)
                context = capture_sound_design_context(self.root)['context_revision']
            result = save_sound_design_candidate(self.root, context)
            stage = self.root / result['directory']
            approval = self.approve(stage, result['candidate_revision'], full_mix_reviewed=True)
            published = save_sound_delivery_mp3(ProjectManager(str(self.root)), stage.name,
                    result['candidate_revision'], approval['approval_revision'],
                    {'format': 'mp3', 'export_quality': None, 'mastering': None})
            self.assertEqual('verified', published['quality']['status'])
            digest = published['quality']['sha256']
            stream, size, _suffix = open_sound_delivery(self.root, 'mp3', digest)
            try:
                self.assertEqual(published['quality']['output_bytes'], size)
                self.assertTrue(stream.read(1))
            finally:
                stream.close()
            for fmt, payload in old.items():
                with self.subTest(format=fmt), self.assertRaisesRegex(ValueError, 'previous'):
                    open_sound_delivery(self.root, fmt, hashlib.sha256(payload).hexdigest())
            from project_scope_exports import read_publication_receipts
            receipts = read_publication_receipts(self.root)
            self.assertEqual(get_book_operation_token(str(self.root)),
                             receipts['mixed_audiobook.mp3']['book_token'])

        core._run_owned_background_task('audio', self.claim, run)

    def test_real_publication_under_legacy_four_key_record(self):
        self._deliver(LEGACY_EXPORTS)

    def test_real_publication_under_legacy_seven_key_record(self):
        self._deliver(BASE_EXPORTS)
