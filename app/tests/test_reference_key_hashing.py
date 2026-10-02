"""One content hash per endpoint, with the original ordered cache identity."""
from collections import Counter
import hashlib
import itertools
from pathlib import Path
import unittest
from unittest.mock import patch
from tests import test_reference_score_cache as support
import voice_reference as reference


class ReferenceKeyHashingTests(unittest.TestCase):
    setUp = support.ReferenceScoreCacheTests.setUp

    def test_twelve_clips_are_read_once_and_ordered_pair_hashes_are_unchanged(self):
        paths=[]
        for index in range(12):
            path=self.root/f'clip-{index}.wav';path.write_bytes(f'clip {index}'.encode());paths.append(str(path))
        pairs=list(itertools.combinations(paths,2));native_hash=reference.get_file_sha256
        with patch.object(reference,'get_file_sha256',wraps=native_hash) as hash_file:
            key=reference._get_reference_score_key(pairs,str(self.python),str(self.worker))
        counts=Counter(str(call.args[0]) for call in hash_file.call_args_list)
        self.assertEqual({path:1 for path in paths},{path:counts[path] for path in paths})
        expected=tuple((hashlib.sha256(Path(a).read_bytes()).hexdigest(),
                        hashlib.sha256(Path(b).read_bytes()).hexdigest()) for a,b in pairs)
        self.assertEqual(expected,key[-1])
        self.assertEqual(str(self.python),key[0])
        self.assertIn(('python',native_hash(self.python)),key[1])
        self.assertIn(('worker',native_hash(self.worker)),key[1])
        self.assertEqual(tuple(reversed(expected)),
                         reference._get_reference_score_key(list(reversed(pairs)),str(self.python),str(self.worker))[-1])

    def test_changed_clip_and_unreadable_endpoint_still_invalidate_identity(self):
        first=reference._get_reference_score_key(self.pairs,str(self.python),str(self.worker))
        Path(self.paths[0]).write_bytes(b'changed audio')
        second=reference._get_reference_score_key(self.pairs,str(self.python),str(self.worker))
        self.assertNotEqual(first,second)
        Path(self.paths[0]).unlink()
        with self.assertRaises(OSError):
            reference._get_reference_score_key(self.pairs,str(self.python),str(self.worker))
