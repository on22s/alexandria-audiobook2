"""Real private Git repositories prove fresh HEAD and tracked/untracked changes."""
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import runtime_info
from routers import system


class RuntimeInfoFreshnessTests(unittest.TestCase):
    def setUp(self):
        runtime_info.get_runtime_info.cache_clear()
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.git('init','-q');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        (self.root/'source.py').write_text('version=1\n')
        self.git('add','source.py');self.git('commit','-qm','initial fixture')
    def tearDown(self):
        self.tmp.cleanup();runtime_info.get_runtime_info.cache_clear()
    def git(self,*args):
        return subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True,text=True).stdout.strip()
    def observe(self):
        with patch.dict(os.environ,{'ALEXANDRIA_BUILD_COMMIT':''}):
            return runtime_info.get_runtime_info(str(self.root))

    def test_head_tracked_untracked_and_staged_changes_refresh_without_cache_clear(self):
        initial=self.observe()
        self.assertFalse(initial['dirty'])
        (self.root/'source.py').write_text('version=2\n')
        self.assertTrue(self.observe()['dirty'])
        self.git('add','source.py')
        self.assertTrue(self.observe()['dirty'])
        self.git('commit','-qm','next fixture')
        current=self.observe()
        self.assertNotEqual(initial['revision'],current['revision'])
        self.assertEqual(self.git('rev-parse','HEAD'),current['revision'])
        self.assertFalse(current['dirty'])
        self.assertEqual('checkout',current['revision_scope'])
        (self.root/'new.py').write_text('untracked=True\n')
        self.assertTrue(self.observe()['dirty'])
        (self.root/'new.py').unlink()
        self.assertFalse(self.observe()['dirty'])
        self.assertLessEqual(runtime_info.get_runtime_info.cache_info().currsize,4)

    def test_returned_metadata_is_owned_and_explicit_build_override_refreshes(self):
        a=self.observe();a['packages']['torch']='caller mutation';a['platform']['system']='changed'
        b=self.observe()
        self.assertNotEqual('caller mutation',b['packages']['torch'])
        self.assertNotEqual('changed',b['platform']['system'])
        for revision in ('build-one','build-two'):
            with patch.dict(os.environ,{'ALEXANDRIA_BUILD_COMMIT':revision}):
                report=runtime_info.get_runtime_info(str(self.root))
            self.assertEqual(revision,report['revision'])
            self.assertEqual('build',report['revision_scope'])

    def test_failed_git_probe_is_unknown_not_false_clean(self):
        with patch.object(runtime_info.subprocess,'run',side_effect=OSError('fixture git unavailable')):
            self.assertIsNone(self.observe()['dirty'])

    def test_actual_version_route_refreshes_same_process_after_commit_and_edit(self):
        with patch.object(system,'ROOT_DIR',str(self.root)),patch.dict(os.environ,{'ALEXANDRIA_BUILD_COMMIT':''}):
            first=asyncio.run(system.get_version())
            (self.root/'source.py').write_text('version=3\n')
            edited=asyncio.run(system.get_version())
            self.assertTrue(edited['dirty'])
            self.git('add','source.py');self.git('commit','-qm','third fixture')
            last=asyncio.run(system.get_version())
        self.assertNotEqual(first['revision'],last['revision'])
        self.assertFalse(last['dirty'])
