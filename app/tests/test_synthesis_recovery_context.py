"""Captured parents stay independent across worker threads and native rows."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import unittest

from audio_generation_owner import AudioGenerationOwner, ensure_audio_generation_owner, run_with_audio_generation_owner
from synthesis_recovery_context import (RecoveryParent, get_recovery_claim_digest,
    get_synthesis_recovery_parent, ensure_synthesis_recovery_parent,
    ensure_synthesis_recovery_batch, ensure_synthesis_recovery_batch_parent)


class RecoveryContextTests(unittest.TestCase):
    def owner(self, claim):
        return AudioGenerationOwner(claim, lambda **kwargs: nullcontext())

    def parent(self, claim, uid='b' * 64):
        return RecoveryParent('/private/project', uid, 'c' * 64, 'd' * 64,
                              hashlib.sha256(claim.encode('ascii')).hexdigest(), nullcontext)

    def test_restart_claim_is_separate_from_semantic_parent_identity(self):
        first, second = self.parent('a' * 32), self.parent('f' * 32)
        self.assertEqual(first.get_semantic_identity(), second.get_semantic_identity())
        self.assertNotEqual(first.claim_sha256, second.claim_sha256)
        self.assertNotIn('project_root', first.get_semantic_identity())
        with ensure_audio_generation_owner(self.owner('a' * 32)):
            with ensure_synthesis_recovery_parent(first):
                self.assertIs(first, get_synthesis_recovery_parent())
                with ensure_audio_generation_owner(self.owner('f' * 32)):
                    self.assertIsNone(get_synthesis_recovery_parent())
                self.assertIs(first, get_synthesis_recovery_parent())
        self.assertIsNone(get_synthesis_recovery_parent())
        self.assertIsNone(get_recovery_claim_digest())

    def test_native_missing_uid_clears_outer_context_and_rows_do_not_leak(self):
        first = self.parent('a' * 32)
        second = self.parent('a' * 32, 'e' * 64)
        parents = {'first': first, 'second': second}
        with ensure_audio_generation_owner(self.owner('a' * 32)):
            with ensure_synthesis_recovery_parent(first), ensure_synthesis_recovery_batch(parents):
                parents.clear()  # Captured map is private.
                for uid, expected in (('second', second), ('first', first), ('missing', None), (None, None)):
                    with ensure_synthesis_recovery_batch_parent(uid):
                        self.assertIs(expected, get_synthesis_recovery_parent())
                    self.assertIs(first, get_synthesis_recovery_parent())
                with ensure_synthesis_recovery_batch({}):
                    with ensure_synthesis_recovery_batch_parent('first'):
                        self.assertIsNone(get_synthesis_recovery_parent())
        self.assertIsNone(get_synthesis_recovery_parent())

    def test_parallel_workers_install_only_their_own_parent_and_shared_owner(self):
        import threading
        owner = self.owner('a' * 32)
        barrier = threading.Barrier(2)
        parents = [self.parent('a' * 32), self.parent('a' * 32, 'e' * 64)]
        def worker(parent):
            self.assertIsNone(get_synthesis_recovery_parent())
            with ensure_synthesis_recovery_parent(parent):
                barrier.wait(timeout=3)
                self.assertIs(parent, get_synthesis_recovery_parent())
            self.assertIsNone(get_synthesis_recovery_parent())
            return parent.uid_sha256
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_with_audio_generation_owner, owner, worker, parent) for parent in parents]
            self.assertEqual([parent.uid_sha256 for parent in parents], [future.result(timeout=5) for future in futures])

    def test_invalid_parent_fields_and_unowned_legacy_callable_cannot_qualify(self):
        with self.assertRaises(ValueError):
            RecoveryParent('/root', '../escape', 'b' * 64, 'c' * 64, 'd' * 64, nullcontext)
        parent = self.parent('a' * 32)
        with ensure_audio_generation_owner(lambda **kwargs: nullcontext()):
            with ensure_synthesis_recovery_parent(parent):
                self.assertIsNone(get_synthesis_recovery_parent())
        with self.assertRaises(ValueError):
            with ensure_synthesis_recovery_batch({'first': object()}):
                pass
