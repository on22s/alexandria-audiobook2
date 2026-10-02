import stat
from pathlib import Path
import tempfile
import unittest
import zipfile

from fastapi import HTTPException
import archive_utils
from routers import lora
from tests.test_training_extraction_symlinks import training


class ArchiveMemberTypeTests(unittest.TestCase):
    def make_archive(self, root, mode):
        path = root / 'fixture.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('metadata.jsonl', '{"text":"hello"}\n')
            member = zipfile.ZipInfo('train/clip.wav')
            member.create_system = 3
            member.external_attr = (mode | 0o600) << 16
            archive.writestr(member, b'known member bytes')
        return path

    def test_validator_rejects_every_special_member_type(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, 0o150000):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as root:
                root = Path(root); dest = root / 'dest'; dest.mkdir()
                path = self.make_archive(root, mode)
                with zipfile.ZipFile(path) as archive, self.assertRaisesRegex(ValueError, 'special file'):
                    archive_utils.validate_zip_members(archive, str(dest))
                self.assertEqual([], list(dest.iterdir()))

    def test_both_extractors_refuse_before_writing_even_earlier_regular_members(self):
        for kind in ('http', 'cli'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                root = Path(root); dest = root / 'dest'; dest.mkdir()
                (dest / 'keep.txt').write_bytes(b'keep')
                path = self.make_archive(root, stat.S_IFLNK)
                if kind == 'http':
                    with zipfile.ZipFile(path) as archive, self.assertRaises(HTTPException) as error:
                        lora._safe_extractall(archive, str(dest))
                    self.assertEqual(400, error.exception.status_code)
                else:
                    with self.assertRaisesRegex(ValueError, 'special file'):
                        training.extract_zip(str(path), str(dest))
                self.assertEqual(['keep.txt'], [p.name for p in dest.iterdir()])
                self.assertEqual(b'keep', (dest / 'keep.txt').read_bytes())

    def test_regular_unspecified_and_dos_types_and_directory_extract_unchanged(self):
        for mode in (0, stat.S_IFREG):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as root:
                root = Path(root); dest = root / 'dest'; dest.mkdir()
                path = self.make_archive(root, mode)
                with zipfile.ZipFile(path, 'a') as archive:
                    directory = zipfile.ZipInfo('empty/'); directory.create_system = 3
                    directory.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
                    archive.writestr(directory, b'')
                    dos = zipfile.ZipInfo('dos.txt'); dos.create_system = 0; dos.external_attr = 0x20
                    archive.writestr(dos, b'DOS bytes')
                with zipfile.ZipFile(path) as archive:
                    lora._safe_extractall(archive, str(dest))
                self.assertEqual(b'known member bytes', (dest / 'train/clip.wav').read_bytes())
                self.assertEqual(b'DOS bytes', (dest / 'dos.txt').read_bytes())
                self.assertTrue((dest / 'empty').is_dir())
