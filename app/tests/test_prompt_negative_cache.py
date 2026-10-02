"""Malformed contents stay fail-loud while unchanged files avoid reparsing."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import prompt_loader


class PromptNegativeCacheTests(unittest.TestCase):
    def test_bad_delimiters_and_empty_parts_read_once_until_actual_repair(self):
        for bad in ('no delimiter','system---SEPARATOR--- ', 's---SEPARATOR---u---SEPARATOR---extra'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'prompt.txt';path.write_text(bad)
                cache={}
                real_open=open
                with patch.object(prompt_loader,'open',wraps=real_open,create=True) as read:
                    for n in range(4):
                        with self.assertRaisesRegex(RuntimeError,'malformed'):
                            prompt_loader.load_prompts_file(path,2,'missing','malformed',cache)
                    self.assertEqual(1,read.call_count)
                    previous=os.path.getmtime(path)
                    path.write_text(' system ---SEPARATOR--- user ')
                    os.utime(path,(previous+2,previous+2))
                    self.assertEqual(('system','user'),prompt_loader.load_prompts_file(path,2,'missing','malformed',cache))
                    self.assertEqual(('system','user'),prompt_loader.load_prompts_file(path,2,'missing','malformed',cache))
                    self.assertEqual(2,read.call_count)

    def test_repaired_file_with_preserved_mtime_is_reparsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prompt.txt';path.write_text('bad')
            old=path.stat()
            cache={}
            with self.assertRaisesRegex(RuntimeError,'malformed'):
                prompt_loader.load_prompts_file(path,2,'missing','malformed',cache)
            path.write_text('system---SEPARATOR---user')
            os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
            self.assertEqual(('system','user'),prompt_loader.load_prompts_file(path,2,'missing','malformed',cache))

    def test_transient_read_failure_is_not_cached_at_unchanged_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prompt.txt';path.write_text('system---SEPARATOR---user')
            cache={}
            with patch.object(prompt_loader,'open',side_effect=PermissionError('fixture denied'),create=True):
                with self.assertRaisesRegex(RuntimeError,'Error reading'):
                    prompt_loader.load_prompts_file(path,2,'missing','malformed',cache)
            self.assertEqual({},cache)
            self.assertEqual(('system','user'),prompt_loader.load_prompts_file(path,2,'missing','malformed',cache))

    def test_last_good_prompt_cache_remains_retained_but_not_used_for_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prompt.txt';path.write_text('system---SEPARATOR---user')
            cache={}
            prompt_loader.load_prompts_file(path,2,'missing','malformed',cache)
            old=dict(cache)
            path.write_text('bad')
            os.utime(path,(old['mtime']+2,old['mtime']+2))
            with self.assertRaisesRegex(RuntimeError,'malformed'):
                prompt_loader.load_prompts_file(path,2,'missing','malformed',cache)
            self.assertEqual(old['mtime'],cache['mtime'])
            self.assertEqual(old['prompts'],cache['prompts'])
            with patch.object(prompt_loader,'open',side_effect=AssertionError('unchanged bad file should not reread'),create=True):
                with self.assertRaisesRegex(RuntimeError,'new caller message'):
                    prompt_loader.load_prompts_file(path,2,'missing','new caller message',cache)
