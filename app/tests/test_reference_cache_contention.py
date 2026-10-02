"""A valid native-fixture cache hit needs no ECAPA worker admission."""
import unittest
from tests import test_reference_score_cache as support
import voice_reference as reference

class ReferenceCacheContentionTests(unittest.TestCase):
    setUp = support.ReferenceScoreCacheTests.setUp
    score = support.ReferenceScoreCacheTests.score
    calls = support.ReferenceScoreCacheTests.calls
    def test_cached_hit_finishes_while_worker_lock_is_held(self):
        self.assertEqual([.9]*3,self.score())
        reference._REFERENCE_WORKER_LOCK.acquire()
        try:
            result=reference._speaker_similarities(self.pairs,timeout=.1,dataset_root=self.root)
            self.assertEqual([.9]*3,result)
            result[0]=0
            self.assertEqual([.9]*3,reference._speaker_similarities(self.pairs,timeout=.1,dataset_root=self.root))
            self.assertEqual(1,self.calls())
            self.assertTrue(reference._REFERENCE_WORKER_LOCK.locked())
        finally:
            reference._REFERENCE_WORKER_LOCK.release()

    def test_changed_bytes_cannot_use_cached_hit_while_worker_busy(self):
        self.score()
        from pathlib import Path
        Path(self.paths[0]).write_bytes(b'changed source')
        reference._REFERENCE_WORKER_LOCK.acquire()
        try:
            self.assertIsNone(reference._speaker_similarities(self.pairs,timeout=.01,dataset_root=self.root))
            self.assertEqual(1,self.calls())
        finally:
            reference._REFERENCE_WORKER_LOCK.release()
