import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import voice_reference as reference


class ReferenceAudioBoundaryTests(unittest.TestCase):
    def paths(self, root):
        paths=[]
        for i in range(3):
            path=root/f'{i}.wav';path.write_bytes(b'fixture audio');paths.append(str(path))
        return paths

    def test_ranking_refuses_outside_paths_before_similarity_even_if_they_exist(self):
        for mode in ('absolute', 'parent', 'symlink'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);dataset=root/'dataset';dataset.mkdir();paths=self.paths(dataset)
                secret=root/'outside.wav';secret.write_bytes(b'outside fixture')
                (dataset/'link.wav').symlink_to(secret)
                bad={'absolute':str(secret),'parent':'../outside.wav','symlink':str(dataset/'link.wav')}[mode]
                paths[1]=bad;before=paths.copy()
                with patch.object(reference,'_speaker_similarities',return_value=[.9]*3) as model:
                    with self.assertRaises(ValueError): reference.rank_reference_samples(paths,dataset_root=str(dataset))
                    model.assert_not_called()
                self.assertEqual(before,paths);self.assertEqual(b'outside fixture',secret.read_bytes())

    def test_worker_revalidates_pairs_before_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dataset=root/'dataset';dataset.mkdir();paths=self.paths(dataset)
            secret=root/'outside.wav';secret.write_bytes(b'outside fixture')
            pairs=[(paths[0],str(secret))];before=copy.deepcopy(pairs)
            with patch.object(reference,'get_speaker_model_python',return_value='fixture-python'), \
                    patch.object(reference.subprocess,'run') as process:
                with self.assertRaises(ValueError): reference._speaker_similarities(pairs,dataset_root=str(dataset))
                process.assert_not_called()
            self.assertEqual(before,pairs)

    def test_missing_and_directory_entries_preserve_original_indices_and_internal_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths=self.paths(root);(root/'inside.wav').symlink_to(root/'0.wav')
            values=[str(root/'missing.wav'),str(root),str(root/'inside.wav'),*paths[1:]];before=values.copy()
            with patch.object(reference,'_speaker_similarities',return_value=[.9,.8,.85]) as model:
                ranked=reference.rank_reference_samples(values,dataset_root=str(root))
            self.assertEqual([(3,.875),(2,.85),(4,.825)],ranked)
            self.assertEqual(str(root),model.call_args.kwargs['dataset_root'])
            self.assertEqual([(paths[0],paths[1]),(paths[0],paths[2]),(paths[1],paths[2])],model.call_args.args[0])
            self.assertEqual(before,values)

    def test_runtime_root_default_worker_payload_contains_only_resolved_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths=self.paths(root)
            with patch.dict('os.environ',ALEXANDRIA_DATA_DIR=tmp), \
                    patch.object(reference,'get_speaker_model_python',return_value='fixture-python'), \
                    patch.object(reference.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='banner\n[0.9]',stderr='')) as process:
                self.assertEqual([.9],reference._speaker_similarities([('0.wav','1.wav')]))
            self.assertEqual([[paths[0],paths[1]]],json.loads(process.call_args.kwargs['input']))
