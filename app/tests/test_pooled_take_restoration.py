"""Actual encoded restore and hard exits through pooled manifests; CPU identity."""
from contextlib import contextmanager
import copy
import json
import unittest
from unittest.mock import patch

from audio_take_history import AudioTakeHistory
from book_state_transaction import get_book_operation_token
from synthesis_workspace import is_synthesis_workspace_supported
from tests import test_take_restoration as restore_cases
from tests import test_take_restoration_recovery as recovery_cases


def install_pooled_fixture(root, origin, current, saved):
    """Add real, unproven snapshots sharing the proven source's policy value."""
    history = AudioTakeHistory(root)
    text = 'The synthetic visitor walked through the quiet garden gate. ' * 50
    for index in range(4):
        history.capture(root / current['audio_path'], get_book_operation_token(root),
                        f'pool-fixture-{index}', dict(text=text,
                            generation_inputs=dict(text=text, speaker='NARRATOR'),
                            synthesis_receipt=dict(policy=copy.deepcopy(origin['synthesis_receipt']['policy']))))
    raw = json.loads(history.manifest.read_bytes())
    if raw['version'] != 3:
        raise AssertionError('Fixture did not use actual pooled storage')
    target = next(entry for entry in raw['records'] if entry['record']['version'] == saved['version'])
    if 'synthesis_policy' not in target['references']:
        raise AssertionError('Originating synthesis policy was not pooled')
    with history._locked() as document:
        if next(row for row in document['records'] if row['version'] == saved['version']) != saved:
            raise AssertionError('Pooling changed originating evidence')


class PooledTakeRestorationTests(unittest.TestCase):
    fixture = restore_cases.TakeRestorationTests.fixture
    owner = restore_cases.TakeRestorationTests.owner
    engine = restore_cases.TakeRestorationTests.engine
    admission = restore_cases.TakeRestorationTests.admission
    render = restore_cases.TakeRestorationTests.render
    _base_prepared = restore_cases.TakeRestorationTests.prepared

    @contextmanager
    def prepared(self, *args, **kwargs):
        with self._base_prepared(*args, **kwargs) as fixture:
            root, _, _, _, origin, _, current, _, saved = fixture
            install_pooled_fixture(root, origin, current, saved)
            yield fixture

    # Reuse the existing behavioral assertions against a different real disk
    # representation. No provider/model/native qualification is implied.
    test_original_bytes_and_full_proof = restore_cases.TakeRestorationTests.test_restore_selects_original_bytes_counter_and_complete_proof_in_both_stores
    test_failed_selection_keeps_current = restore_cases.TakeRestorationTests.test_failed_final_metadata_keeps_displaced_bytes_revision_and_take
    test_cancelled_and_replaced_owner = restore_cases.TakeRestorationTests.test_cancelled_or_replaced_owner_at_final_capture_cannot_select
    test_saved_speech_receipt_binding = restore_cases.TakeRestorationTests.test_saved_speech_evidence_rebinds_only_verified_installed_file_version
    test_changed_dependencies_refuse = restore_cases.TakeRestorationTests.test_changed_dependencies_and_corrupt_saved_bindings_never_replace_selected_audio

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_hard_exits_recover_pooled_proofs_and_actual_archives_in_both_stores(self):
        needle = ' lease=acquire_task_lease(root,\'audio\',set())'
        self.assertEqual(recovery_cases._CHILD.count(needle), 1)
        child = recovery_cases._CHILD.replace(needle,
            ' from tests.test_pooled_take_restoration import install_pooled_fixture\n'
            ' install_pooled_fixture(root,origin,current,saved)\n' + needle)
        with patch.object(recovery_cases, '_CHILD', child):
            recovery_cases.TakeRestorationRecoveryTests.test_hard_exits_preserve_selected_and_archived_bytes_then_allow_idle_cleanup_and_retry(self)
