"""Voice references and adapter trees are bounded byte snapshots, never loaded."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from project_scope_assets import get_scope_asset_snapshot, get_scope_voice_artifacts


class ScopeAssetTests(unittest.TestCase):
    def test_real_reference_and_adapter_tree_bind_changed_bytes_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); reference = root/'reference.wav'; reference.write_bytes(b'reference')
            adapter = root/'adapter'; adapter.mkdir(); (adapter/'weights.bin').write_bytes(b'weights')
            plan = {'retained':[{'effective_voices':{'A':{'ref_audio':str(reference),
                                                       'adapter_path':str(adapter)}}}]}
            before = {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
            snapshots = get_scope_voice_artifacts(plan)
            self.assertEqual({'file','directory'}, {s['kind'] for s in snapshots})
            # Canonical adapter alias resolution owns this ordinary portable
            # lock; it must not create or change a manifest or model asset.
            self.assertEqual({**before, 'manifest.json.lock':b''},
                             {p.relative_to(root).as_posix():p.read_bytes()
                              for p in root.rglob('*') if p.is_file()})
            (adapter/'weights.bin').write_bytes(b'changed')
            self.assertNotEqual(snapshots, get_scope_voice_artifacts(plan))

    def test_missing_alias_oversized_and_unreadable_subtree_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); file = root/'weights'; file.write_bytes(b'bounded')
            with self.assertRaises(ValueError):get_scope_asset_snapshot(root/'missing')
            alias = root/'alias'; alias.symlink_to(file)
            with self.assertRaises(ValueError):get_scope_asset_snapshot(alias)
            with patch('project_scope_assets.MAX_ASSET_BYTES', 2), self.assertRaises(ValueError):
                get_scope_asset_snapshot(file)
            with patch('project_scope_assets.os.walk', side_effect=lambda path, onerror:onerror(PermissionError('denied'))):
                with self.assertRaises(PermissionError):get_scope_asset_snapshot(root)
