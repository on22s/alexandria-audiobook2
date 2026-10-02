import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from fastapi import HTTPException
from routers import lora
import evaluation_reviews
from voice_manifest import validate_adapter_name


class LoraAdapterLocationTests(unittest.TestCase):
    def test_review_storage_uses_same_adapter_component_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            for identity in ('voice#one?two%three', 'Voice One', '声'):
                validate_adapter_name(identity)
                self.assertEqual([], evaluation_reviews.list_reviews(tmp, identity))
                self.assertEqual(Path(tmp) / (identity + '.json'),
                                 Path(evaluation_reviews._store_path(tmp, identity)))
            for identity in ('', '.', '..', '../voice', 'voice/other', 'voice\\other', 'voice\0other'):
                with self.subTest(identity=identity):
                    with self.assertRaises(ValueError):
                        validate_adapter_name(identity)
                    with self.assertRaises(evaluation_reviews.ReviewError):
                        evaluation_reviews._store_path(tmp, identity)

    def test_list_and_cached_preview_encode_user_and_builtin_adapter_identity(self):
        for builtin in (False,True):
            with self.subTest(builtin=builtin),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);models=root/'models';models.mkdir();builtins=root/'builtin';builtins.mkdir()
                identity='voice#one?two%three'
                folder=(builtins if builtin else models)/identity;folder.mkdir()
                sf.write(folder/'preview_sample.wav',np.full(2400,.1),24000)
                row={'id':identity,'builtin':builtin}
                manifest=models/'manifest.json';manifest.write_text(json.dumps([] if builtin else [row]))
                for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',str(manifest)),
                                   ('BUILTIN_LORA_DIR',str(builtins)),('EVALUATION_REVIEWS_DIR',str(root/'reviews'))):
                    stack.enter_context(patch.object(lora,name,value))
                stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[row] if builtin else []))
                stack.enter_context(patch.object(lora,'_load_voice_library',return_value={'favorites':[]}))
                listed=asyncio.run(lora.lora_list_models());cached=asyncio.run(lora.lora_preview(identity))
                prefix='/builtin_lora/' if builtin else '/lora_models/'
                expected=prefix+'voice%23one%3Ftwo%25three/preview_sample.wav'
                self.assertEqual(expected,listed[0]['preview_audio_url'])
                self.assertEqual(expected,cached['audio_url'])

    def test_listing_cannot_inspect_symlinked_adapter_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside,ExitStack() as stack:
            root=Path(tmp);other=Path(outside)
            manifest=root/'manifest.json';manifest.write_text('[{"id":"voice"}]')
            (root/'voice').symlink_to(other,target_is_directory=True)
            sf.write(other/'preview_sample.wav',np.full(2400,.1),24000)
            (other/'.checkpoint_swap.json').write_text('{"operation":"outside"}')
            before={p.name:p.read_bytes() for p in other.iterdir()}
            for name,value in (('LORA_MODELS_DIR',tmp),('LORA_MODELS_MANIFEST',str(manifest)),
                               ('EVALUATION_REVIEWS_DIR',str(root/'reviews'))):
                stack.enter_context(patch.object(lora,name,value))
            stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[]))
            stack.enter_context(patch.object(lora,'_load_voice_library',return_value={'favorites':[]}))
            journal=stack.enter_context(patch.object(lora,'_get_checkpoint_swap_journal',wraps=lora._get_checkpoint_swap_journal))
            with self.assertRaises(HTTPException) as raised:asyncio.run(lora.lora_list_models())
            self.assertEqual(400,raised.exception.status_code);journal.assert_not_called()
            self.assertEqual(before,{p.name:p.read_bytes() for p in other.iterdir()})
