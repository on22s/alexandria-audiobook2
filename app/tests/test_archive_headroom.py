"""Actual ZIP extraction admission preserves disk headroom in both callers."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
from fastapi import HTTPException
import archive_utils
from routers import lora
from tests.test_voicelab_pipeline_scripts import batch_train


class ArchiveHeadroomTests(unittest.TestCase):
    def test_exact_free_space_and_one_byte_spare_refuse_before_either_extractor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'dataset.zip'
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('audio.wav', b'x'*1024)
            for free in (1024,1025):
                for caller in ('api','batch'):
                    with self.subTest(free=free,caller=caller):
                        destination=Path(tmp)/f'{caller}-{free}';destination.mkdir()
                        with patch.object(archive_utils.shutil,'disk_usage',return_value=SimpleNamespace(free=free)), \
                             patch.object(zipfile.ZipFile,'extractall',side_effect=AssertionError('must not extract')) as extract:
                            if caller=='api':
                                with zipfile.ZipFile(path) as zf, self.assertRaises(HTTPException) as error:
                                    lora._safe_extractall(zf,str(destination))
                                self.assertEqual(400,error.exception.status_code)
                            else:
                                with self.assertRaises(ValueError):
                                    batch_train.extract_zip(str(path),str(destination))
                        extract.assert_not_called()
                        self.assertEqual([],list(destination.iterdir()))

    def test_exact_reserved_space_extracts_known_bytes_without_changing_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'dataset.zip';destination=Path(tmp)/'out';destination.mkdir()
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('audio.wav',b'known bytes')
            before=path.read_bytes()
            free=len(b'known bytes')+archive_utils.MIN_EXTRACTION_HEADROOM_BYTES
            with patch.object(archive_utils.shutil,'disk_usage',return_value=SimpleNamespace(free=free)):
                batch_train.extract_zip(str(path),str(destination))
            self.assertEqual(b'known bytes',(destination/'audio.wav').read_bytes())
            self.assertEqual(before,path.read_bytes())

    def test_large_expansion_and_many_members_raise_reserve(self):
        for size,count,reserve in ((2*1024**3,1,(2*1024**3+19)//20),(0,20000,20000*4096)):
            with self.subTest(size=size,count=count),tempfile.TemporaryDirectory() as tmp:
                members=[SimpleNamespace(filename=f'{i}.wav',file_size=size if i==0 else 0,external_attr=0) for i in range(count)]
                zf=SimpleNamespace(infolist=lambda:members)
                with patch.object(archive_utils.shutil,'disk_usage',return_value=SimpleNamespace(free=size+reserve-1)):
                    with self.assertRaisesRegex(ValueError,'headroom'):
                        archive_utils.validate_zip_members(zf,tmp)
                with patch.object(archive_utils.shutil,'disk_usage',return_value=SimpleNamespace(free=size+reserve)):
                    archive_utils.validate_zip_members(zf,tmp)
