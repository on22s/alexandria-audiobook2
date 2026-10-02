import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integration_corpus import build_manifest


class CorpusReadEfficiencyTests(unittest.TestCase):
    def test_casefold_ties_have_identical_order_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); paths=[root/'a.txt',root/'A.txt',root/'b.txt']
            for path in paths:path.write_text(path.name+' text')
            with patch.object(Path,'iterdir',return_value=iter(paths)):
                first=build_manifest(tmp,max_books=1)
            with patch.object(Path,'iterdir',return_value=iter(reversed(paths))):
                second=build_manifest(tmp,max_books=1)
            self.assertEqual(first,second)
            self.assertEqual('A.txt',first['books'][0]['name'])

    def test_txt_manifest_uses_one_disk_read_and_preserves_decoding_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp,'book.txt');raw=b'First\r\nSecond\rThird\nBad \xff caf\xc3\xa9.';path.write_bytes(raw)
            opened=[];original_open=Path.open
            def tracked_open(file,*args,**kwargs):
                mode=args[0] if args else kwargs.get('mode','r')
                if 'r' in mode:opened.append(str(file))
                return original_open(file,*args,**kwargs)
            with patch.object(Path,'open',tracked_open):manifest=build_manifest(tmp)
            self.assertEqual([],manifest['errors'])
            self.assertEqual(1,len(opened))
            book=manifest['books'][0]
            self.assertEqual(hashlib.sha256(raw).hexdigest(),book['source_sha256'])
            self.assertEqual(len(raw),book['source_size_bytes'])
            self.assertEqual(len('First\nSecond\nThird\nBad � café.'),book['text_characters'])
            self.assertEqual('First Second Third Bad � café.',book['passages'][-1]['text'])
            self.assertEqual(raw,path.read_bytes())
