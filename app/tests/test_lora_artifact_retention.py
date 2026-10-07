import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from routers import lora
from tests.test_lora_candidate_promotion import _write_real_checkpoint


class LoraArtifactRetentionTests(unittest.TestCase):
    def test_promoting_one_candidate_keeps_other_records_and_exact_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'voice'
            _write_real_checkpoint(adapter,'production')
            _write_real_checkpoint(adapter/'candidates/epoch_002','candidate')
            _write_real_checkpoint(adapter/'candidates/epoch_003','older')
            other={'id':'epoch_003','epoch':3,'keep':'human metadata'}
            manifest=root/'manifest.json';manifest.write_text(json.dumps([{'id':'voice',
                'evaluation':{'recommended_candidate':'epoch_002'},
                'evaluation_candidates':[{'id':'epoch_002'},other]}]))
            originals={p.name:p.read_bytes() for p in (adapter/'candidates/epoch_003').iterdir()}
            result=lora._promote_lora_candidate('voice',tmp,str(manifest))
            saved=json.loads(manifest.read_text())[0]
            self.assertEqual([other],saved['evaluation_candidates'])
            self.assertEqual(originals,{p.name:p.read_bytes() for p in (adapter/'candidates/epoch_003').iterdir()})
            self.assertFalse((adapter/'candidates/epoch_002').exists())
            self.assertEqual('promoted',result['status'])
            self.assertEqual(1,lora._get_candidate_summary(saved,str(adapter))['retained_count'])

    def test_auditions_remain_bounded_without_pruning_other_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'voice';adapter.mkdir()
            (root/'manifest.json').write_text('[{"id":"voice"}]')
            protected=['preview_sample.wav','ref_sample.wav','test_notes.wav','test_other_12.wav','test_voice_manual.wav']
            for name in protected:(adapter/name).write_bytes(name.encode())
            original={name:(adapter/name).read_bytes() for name in protected}
            with patch.object(lora,'LORA_MODELS_DIR',tmp):
                for i in range(25):
                    source=root/f'stage_{i}.wav';sf.write(source,np.full(2400,i/30),24000)
                    result=lora.apply_lora_audio_publication('voice',False,str(source),f'test_voice_{i}_'+f'{i:032x}'+'.wav')
                    self.assertTrue((root/result.split('/lora_models/')[1]).is_file())
            trials=sorted(adapter.glob('test_voice_*'))
            generated=[p for p in trials if p.name not in protected]
            self.assertEqual(20,len(generated))
            self.assertTrue((adapter/('test_voice_24_'+f'{24:032x}'+'.wav')).exists())
            self.assertEqual(original,{name:(adapter/name).read_bytes() for name in protected})

    def test_legacy_generated_names_are_pruned_but_symlink_targets_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside:
            root=Path(tmp);adapter=root/'voice';adapter.mkdir()
            (root/'manifest.json').write_text('[{"id":"voice"}]')
            target=Path(outside)/'keep.wav';target.write_bytes(b'keep original')
            (adapter/'test_voice_1.wav').symlink_to(target)
            for i in range(2,25):
                path=adapter/f'test_voice_{i}.wav';sf.write(path,np.zeros(2400),24000)
            source=root/'stage.wav';sf.write(source,np.full(2400,.1),24000)
            with patch.object(lora,'LORA_MODELS_DIR',tmp):
                lora.apply_lora_audio_publication('voice',False,str(source),'test_voice_25_'+('a'*32)+'.wav')
            self.assertEqual(21,len(list(adapter.glob('test_voice_*'))))
            self.assertTrue((adapter/'test_voice_1.wav').is_symlink())
            self.assertEqual(b'keep original',target.read_bytes())

    def test_historical_adapter_auditions_share_bound_after_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'new';adapter.mkdir()
            (root/'manifest.json').write_text('[{"id":"new","previous_ids":["old"]}]')
            for i in range(22):sf.write(adapter/f'test_old_{i}.wav',np.zeros(2400),24000)
            source=root/'stage.wav';sf.write(source,np.full(2400,.15),24000)
            with patch.object(lora,'LORA_MODELS_DIR',tmp):
                url=lora.apply_lora_audio_publication('old',False,str(source),'test_old_23_'+('b'*32)+'.wav')
            self.assertEqual(20,len(list(adapter.glob('test_old_*'))))
            self.assertTrue(url.startswith('/lora_models/new/'))
            self.assertEqual(2400,sf.info(root/url.split('/lora_models/')[1]).frames)

    def test_cleanup_io_failure_is_loud_and_keeps_new_published_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'voice';adapter.mkdir()
            (root/'manifest.json').write_text('[{"id":"voice"}]')
            for i in range(22):sf.write(adapter/f'test_voice_{i}.wav',np.zeros(2400),24000)
            source=root/'stage.wav';sf.write(source,np.full(2400,.15),24000)
            filename='test_voice_23_'+('c'*32)+'.wav'
            with patch.object(lora,'LORA_MODELS_DIR',tmp),patch.object(lora.os,'remove',side_effect=PermissionError('fixture cleanup denied')):
                with self.assertLogs(lora.logger, level='WARNING') as warnings:
                    url = lora.apply_lora_audio_publication('voice',False,str(source),filename)
                self.assertTrue(url.endswith('/'+filename))
                self.assertIn('retention cleanup failed', warnings.output[0])
                self.assertIn('cleanup denied', warnings.output[0])
            self.assertEqual(2400,sf.info(adapter/filename).frames)
            self.assertEqual(23,len(list(adapter.glob('test_voice_*'))))
