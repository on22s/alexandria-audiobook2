"""Relative runtime TTS assets must stay within the chosen data root."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

import numpy as np
import soundfile as sf
import tts


class TTSAssetPathTests(unittest.TestCase):
    def test_runtime_and_builtin_roots_and_normalized_separators(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(tts,'_get_runtime_data_dir',return_value=tmp):
            self.assertEqual(str(Path(tmp)/'clone_voices/ref.wav'),tts._resolve_asset_path('clone_voices/ref.wav'))
            self.assertEqual(str(Path(tmp)/'clone_voices/ref.wav'),tts._resolve_asset_path('clone_voices\\ref.wav'))
            repo=Path(tts.__file__).resolve().parent.parent
            self.assertEqual(str(repo/'builtin_lora/builtin_a'),tts._resolve_asset_path('builtin_lora/builtin_a'))
            self.assertEqual(str(repo/'builtin_lora/builtin_a'),tts._resolve_asset_path('./builtin_lora/builtin_a'))

    def test_absolute_drive_traversal_and_empty_inputs_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(tts,'_get_runtime_data_dir',return_value=tmp):
            for value in ('../outside.wav','clone_voices/../../outside.wav','clone_voices/../ref.wav','builtin_lora/../outside','/tmp/outside.wav','C:\\outside.wav','C:outside.wav','\\\\server\\share\\file.wav','','.'):
                with self.subTest(value=value),self.assertRaisesRegex(ValueError,'relative asset'):
                    tts._resolve_asset_path(value)

    def test_symlink_escape_is_refused_even_with_a_safe_lexical_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);runtime=root/'runtime';runtime.mkdir();outside=root/'outside';outside.mkdir()
            (runtime/'clone_voices').symlink_to(outside,target_is_directory=True)
            with patch.object(tts,'_get_runtime_data_dir',return_value=str(runtime)),self.assertRaisesRegex(ValueError,'relative asset'):
                tts._resolve_asset_path('clone_voices/ref.wav')

    def test_actual_clone_path_refuses_traversal_before_loading_and_keeps_explicit_absolute_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);runtime=root/'runtime';runtime.mkdir();outside=root/'outside.wav';sf.write(outside,np.zeros(240),24000)
            engine=tts.TTSEngine({'tts':{'mode':'local'}});create=Mock(return_value=object());model=SimpleNamespace(create_voice_clone_prompt=create)
            with patch.object(tts,'_get_runtime_data_dir',return_value=str(runtime)),patch.object(engine,'_init_local_clone',return_value=model) as load:
                with self.assertRaisesRegex(ValueError,'relative asset'):
                    engine._get_clone_prompt('A',{'A':{'ref_audio':'../outside.wav','ref_text':'Reference'}})
                load.assert_not_called()
                prompt=engine._get_clone_prompt('A',{'A':{'ref_audio':str(outside),'ref_text':'Reference'}})
                self.assertIs(prompt,create.return_value);load.assert_called_once()
            self.assertEqual(240,len(create.call_args.kwargs['ref_audio'][0]))
