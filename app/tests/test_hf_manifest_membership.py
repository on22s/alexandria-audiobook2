"""Built-in payload downloads require the same catalog as the HTTP route."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hf_utils


class ManifestMembershipTests(unittest.TestCase):
    def fixture(self, entries):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        cache = root/'cache'
        cache.mkdir()
        (cache/'manifest.json').write_text(json.dumps(entries))
        for name in hf_utils.REQUIRED_ADAPTER_FILES:
            (cache/name).write_bytes((name+' complete').encode())
        requests = []
        def download(**kwargs):
            requests.append((kwargs['repo_id'],kwargs['filename']))
            return str(cache/Path(kwargs['filename']).name)
        hub_patch = patch.dict(sys.modules, {'huggingface_hub':SimpleNamespace(hf_hub_download=download)})
        hub_patch.start()
        self.addCleanup(hub_patch.stop)
        for name,value in (('_manifest_cache',None),('_manifest_cache_key',None),('_manifest_cache_time',0)):
            cache_patch = patch.object(hf_utils,name,value)
            cache_patch.start()
            self.addCleanup(cache_patch.stop)
        return root, requests

    def test_unlisted_complete_remote_payload_is_not_installed(self):
        root, requests = self.fixture([{'id':'approved'}])
        builtin = root/'builtin'
        with self.assertRaisesRegex(ValueError,'Unknown built-in adapter'):
            hf_utils.download_builtin_adapter('builtin_unlisted',str(builtin),hf_repo='fixture/repo')
        self.assertEqual([('fixture/repo','manifest.json')],requests)
        self.assertFalse((builtin/'builtin_unlisted').exists())

    def test_bare_and_prefixed_catalog_ids_admit_complete_downloads(self):
        for entry in ('voice','builtin_voice'):
            with self.subTest(entry=entry):
                root, requests = self.fixture([{'id':entry}])
                builtin = root/'builtin'
                path = Path(hf_utils.download_builtin_adapter('builtin_voice',str(builtin),hf_repo='fixture/repo'))
                self.assertTrue(hf_utils.is_adapter_downloaded('builtin_voice',str(builtin)))
                for name in hf_utils.REQUIRED_ADAPTER_FILES:
                    self.assertEqual((root/'cache'/name).read_bytes(),(path/name).read_bytes())
                self.assertTrue(all(repo=='fixture/repo' for repo,_ in requests))
                self.assertEqual('manifest.json',requests[0][1])

    def test_offline_manifest_fallback_admits_known_adapter_only(self):
        root, requests = self.fixture([{'id':'unused'}])
        builtin = root/'builtin'
        builtin.mkdir()
        (builtin/'manifest.json').write_text(json.dumps([{'id':'voice'}]))
        hub = sys.modules['huggingface_hub']
        original = hub.hf_hub_download
        def offline_catalog(**kwargs):
            if kwargs['filename']=='manifest.json':
                raise OSError('catalog offline; cached payloads available')
            return original(**kwargs)
        with patch.object(hub,'hf_hub_download',side_effect=offline_catalog):
            hf_utils.download_builtin_adapter('builtin_voice',str(builtin))
            self.assertTrue(hf_utils.is_adapter_downloaded('builtin_voice',str(builtin)))
            with self.assertRaisesRegex(ValueError,'Unknown built-in adapter'):
                hf_utils.download_builtin_adapter('builtin_unknown',str(builtin))
        self.assertFalse((builtin/'builtin_unknown').exists())
        self.assertTrue(all(path.startswith('voice/') for _,path in requests))

    def test_missing_offline_catalog_cannot_authorize_cached_payload(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'huggingface_hub':SimpleNamespace(hf_hub_download=lambda **kwargs: (_ for _ in ()).throw(OSError('offline')))}), \
                patch.object(hf_utils,'_manifest_cache',None):
            with self.assertRaises(Exception) as failed:
                hf_utils.download_builtin_adapter('builtin_voice',tmp)
            self.assertIsInstance(failed.exception,ValueError)
            self.assertIn('Unknown built-in adapter',str(failed.exception))
            self.assertFalse((Path(tmp)/'builtin_voice').exists())
