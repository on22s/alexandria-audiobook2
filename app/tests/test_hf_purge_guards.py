"""tools/hf_purge_batch.py refuses every purge that would lose something still in use.

The purge is irreversible, so each guard is tested by a case it must REFUSE, and each
refusal is checked to have changed nothing: no commit, no deleted path, no purged object.
The fake Hub models the two behaviours the guards exist for: an LFS object is identified
by its sha256 wherever it is referenced, and deleting a path frees nothing until the
object is purged.
"""
import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).parent.parent.parent


def _load_tool():
    spec = importlib.util.spec_from_file_location("hf_purge_batch", REPO / "tools" / "hf_purge_batch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()
ARCHIVE = "Om22s/archive"


class FakeHub:
    """Repos map path -> bytes; LFS storage is per repo, keyed by sha256, never freed by a delete."""

    def __init__(self, tmp):
        self.tmp, self.repos, self.private, self.lfs, self.commits = tmp, {}, {}, {}, []
        self.repo_types = {}
        self.tamper_download = False

    def add_repo(self, repo, files, private=True, repo_type="model"):
        self.repos[repo], self.private[repo], self.lfs[repo] = {}, private, {}
        self.repo_types[repo] = repo_type
        for path, data in files.items():
            self._put(repo, path, data)

    def _put(self, repo, path, data):
        self.repos[repo][path] = data
        self.lfs[repo][hashlib.sha256(data).hexdigest()] = len(data)

    def model_info(self, repo):
        return SimpleNamespace(private=self.private[repo])

    def list_models(self, author):
        return [SimpleNamespace(id=r) for r in self.repos if r.startswith(author + "/")
                and self.repo_types[r] == "model"]

    def list_datasets(self, author):
        return [SimpleNamespace(id=r) for r in self.repos if r.startswith(author + "/")
                and self.repo_types[r] == "dataset"]

    def list_spaces(self, author):
        return [SimpleNamespace(id=r) for r in self.repos if r.startswith(author + "/")
                and self.repo_types[r] == "space"]

    def list_repo_files(self, repo, repo_type="model"):
        return list(self.repos[repo])

    def get_paths_info(self, repo, paths, repo_type="model"):
        return [SimpleNamespace(path=p, lfs=SimpleNamespace(sha256=hashlib.sha256(self.repos[repo][p]).hexdigest()))
                for p in paths if p in self.repos[repo]]

    def create_commit(self, repo, operations, commit_message):
        self.commits.append(commit_message)
        for op in operations:
            if hasattr(op, "path_or_fileobj"):
                with open(op.path_or_fileobj, "rb") as fh:
                    self._put(repo, op.path_in_repo, fh.read())
            else:
                del self.repos[repo][op.path_in_repo]

    def hf_hub_download(self, repo, path, force_download=True):
        out = os.path.join(self.tmp, "dl_" + path.replace("/", "_"))
        with open(out, "wb") as fh:
            fh.write(self.repos[repo][path] + (b"x" if self.tamper_download else b""))
        return out

    def list_lfs_files(self, repo):
        return [SimpleNamespace(file_oid=s, size=n) for s, n in self.lfs[repo].items()]

    def permanently_delete_lfs_files(self, repo, objs):
        for o in objs:
            del self.lfs[repo][o.file_oid]


class PurgeGuardTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.hub = FakeHub(self.tmp)
        self.hub.add_repo(ARCHIVE, {
            "old/adapter_model.safetensors": b"W" * 1000,
            "old/adapter_config.json": b"{}",
            "keep/adapter_model.safetensors": b"K" * 700,
        })
        self.lesson = os.path.join(self.tmp, "LESSONS.md")
        with open(self.lesson, "w") as fh:
            fh.write("# old - weights purged, lesson kept\n")
        self.spec = {"repo": ARCHIVE, "label": "t", "lessons": {"old/LESSONS.md": self.lesson},
                     "purge": ["old/adapter_model.safetensors"], "expect_bytes": 1000}

    def tearDown(self):
        self._td.cleanup()

    def _assert_nothing_changed(self):
        self.assertEqual(self.hub.commits, [])
        self.assertIn("old/adapter_model.safetensors", self.hub.repos[ARCHIVE])
        self.assertIn(hashlib.sha256(b"W" * 1000).hexdigest(), self.hub.lfs[ARCHIVE])

    def test_purges_exactly_the_target_and_keeps_the_lesson(self):
        freed = tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self.assertEqual(freed, 1000)
        self.assertNotIn("old/adapter_model.safetensors", self.hub.repos[ARCHIVE])
        self.assertIn("old/LESSONS.md", self.hub.repos[ARCHIVE])
        self.assertIn("old/adapter_config.json", self.hub.repos[ARCHIVE])
        self.assertIn(hashlib.sha256(b"K" * 700).hexdigest(), self.hub.lfs[ARCHIVE])

    def test_refuses_a_public_repo(self):
        self.hub.private[ARCHIVE] = False
        with self.assertRaisesRegex(tool.PurgeRefused, "not private"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_refuses_when_another_path_in_the_repo_holds_the_same_object(self):
        # The first-push-path trap: a copy under another folder is the same LFS object,
        # so purging it would break the copy.
        self.hub._put(ARCHIVE, "moved/adapter_model.safetensors", b"W" * 1000)
        with self.assertRaisesRegex(tool.PurgeRefused, "another path"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_refuses_when_the_object_is_published_in_another_repo(self):
        self.hub.add_repo("Om22s/public-collection", {"adapters/a.gguf": b"W" * 1000}, private=False)
        with self.assertRaisesRegex(tool.PurgeRefused, "another repo"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_another_authors_copy_does_not_block(self):
        self.hub.add_repo("someone/else", {"a.gguf": b"W" * 1000}, private=False)
        self.assertEqual(tool.run_batch(self.spec, self.hub, log=lambda *_: None), 1000)

    def test_refuses_to_delete_when_a_lesson_does_not_read_back(self):
        self.hub.tamper_download = True
        with self.assertRaisesRegex(tool.PurgeRefused, "read back differs"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self.assertEqual(len(self.hub.commits), 1)  # the lesson commit only
        self.assertIn("old/adapter_model.safetensors", self.hub.repos[ARCHIVE])
        self.assertIn(hashlib.sha256(b"W" * 1000).hexdigest(), self.hub.lfs[ARCHIVE])

    def test_refuses_a_purge_path_that_is_not_at_head(self):
        self.spec["purge"].append("old/gone.gguf")
        with self.assertRaisesRegex(tool.PurgeRefused, "not at HEAD"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_refuses_a_non_lfs_path(self):
        self.spec["purge"] = ["old/adapter_config.json"]
        # The fake stores all files as LFS, so model a normal Git blob here.
        original = self.hub.get_paths_info
        self.hub.get_paths_info = lambda repo, paths, repo_type="model": [
            info for info in original(repo, paths, repo_type)
            if info.path != "old/adapter_config.json"]
        with self.assertRaisesRegex(tool.PurgeRefused, "not an LFS file"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_a_wrong_expected_size_is_reported(self):
        self.spec["expect_bytes"] = 5_000_000_000
        with self.assertRaisesRegex(tool.PurgeRefused, "expected"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_missing_expected_size_refuses_before_any_commit(self):
        del self.spec["expect_bytes"]
        with self.assertRaisesRegex(tool.PurgeRefused, "expect_bytes"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_lesson_cannot_overlap_a_purge_path(self):
        self.spec["lessons"] = {"old/adapter_model.safetensors": self.lesson}
        with self.assertRaisesRegex(tool.PurgeRefused, "overlap"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_extensionless_head_reference_blocks_purge(self):
        self.hub._put(ARCHIVE, "other/weights", b"W" * 1000)
        with self.assertRaisesRegex(tool.PurgeRefused, "another path"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()

    def test_space_reference_blocks_purge(self):
        space = "Om22s/live-space"
        self.hub.add_repo(space, {"weights": b"W" * 1000}, private=False,
                          repo_type="space")
        with self.assertRaisesRegex(tool.PurgeRefused, "another repo"):
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self._assert_nothing_changed()


    def test_real_sdk_receives_lfs_records_and_emits_exact_target_ids(self):
        from unittest.mock import Mock, patch
        from huggingface_hub import HfApi
        from huggingface_hub.hf_api import LFSFileInfo
        import requests
        sdk=HfApi(endpoint='https://fixture.invalid',token=False)
        response=requests.Response();response.status_code=200;response.url='https://fixture.invalid'
        transport=Mock();transport.post.return_value=response
        original_list=self.hub.list_lfs_files
        original_delete=self.hub.permanently_delete_lfs_files
        def records(repo):
            return [LFSFileInfo(fileOid=item.file_oid,filename='fixture.wav',oid='fixture-git-oid',
                                pushedAt='2026-09-29T00:00:00Z',size=item.size)
                    for item in original_list(repo)]
        def delete(repo,objects):
            self.assertTrue(all(isinstance(item,LFSFileInfo) for item in objects))
            sdk.permanently_delete_lfs_files(repo,objects)
            original_delete(repo,objects)
        with patch.object(self.hub,'list_lfs_files',side_effect=records), \
             patch.object(self.hub,'permanently_delete_lfs_files',side_effect=delete), \
             patch('huggingface_hub.hf_api.get_session',return_value=transport):
            self.assertEqual(1000,tool.run_batch(self.spec,self.hub,log=lambda *_:None))
        transport.post.assert_called_once()
        args,kwargs=transport.post.call_args
        self.assertEqual('https://fixture.invalid/api/models/'+ARCHIVE+'/lfs-files/batch',args[0])
        self.assertEqual({'deletions':{'sha':[hashlib.sha256(b'W'*1000).hexdigest()],
                                      'rewriteHistory':True}},kwargs['json'])
        self.assertIn('keep/adapter_model.safetensors',self.hub.repos[ARCHIVE])
        self.assertIn('old/LESSONS.md',self.hub.repos[ARCHIVE])
        self.assertIn(hashlib.sha256(b'K'*700).hexdigest(),self.hub.lfs[ARCHIVE])

    def test_real_sdk_rejects_string_ids_before_any_transport_call(self):
        from unittest.mock import patch
        from huggingface_hub import HfApi
        sdk=HfApi(endpoint='https://fixture.invalid',token=False)
        with patch('huggingface_hub.hf_api.get_session') as session:
            with self.assertRaises(AttributeError):
                sdk.permanently_delete_lfs_files(ARCHIVE,[hashlib.sha256(b'W'*1000).hexdigest()])
        session.assert_not_called()
        self._assert_nothing_changed()


if __name__ == "__main__":
    unittest.main()
