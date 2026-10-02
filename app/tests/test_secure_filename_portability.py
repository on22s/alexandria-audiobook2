"""Portable names stay byte-bounded, collision-aware, and filesystem-usable."""
from pathlib import Path
import tempfile
import unittest

from utils import secure_filename


class PortableFilenameTests(unittest.TestCase):
    def test_utf8_byte_cap_preserves_complete_codepoints_and_actual_staging_file(self):
        names=['声'*150,'Ж'*150,'abc声'*70,'a'*150,'声'*49+'ab','声'*49+'abc']
        with tempfile.TemporaryDirectory() as tmp:
            for name in names:
                with self.subTest(name=name[:12]):
                    safe=secure_filename(name)
                    self.assertLessEqual(len(safe.encode()),150)
                    self.assertEqual(safe,secure_filename(safe))
                    path=Path(tmp)/(safe+'.json.upload.'+'f'*32)
                    path.write_bytes(b'preserved staging payload')
                    self.assertEqual(b'preserved staging payload',path.read_bytes())
        self.assertNotEqual(secure_filename('声'*150+'one'),secure_filename('声'*150+'two'))
        self.assertEqual('a'*150,secure_filename('a'*150))
        self.assertEqual('声'*49+'abc',secure_filename('声'*49+'abc'))

    def test_reserved_devices_and_trailing_dots_spaces_are_normalized(self):
        devices=['CON','prn','AuX','NUL']
        devices += [prefix+str(n) for prefix in ('COM','LPT') for n in range(1,10)]
        devices += ['COM¹','COM²','LPT³']
        for device in devices:
            for name in (device,device+'.wav',device+' .txt'):
                with self.subTest(name=name):
                    safe=secure_filename(name)
                    self.assertTrue(safe.startswith('_'))
                    self.assertEqual(safe,secure_filename(safe))
        for name in ('adapter.','voice ','voice. .'):
            with self.subTest(name=name):
                safe=secure_filename(name)
                self.assertFalse(safe.endswith(('.', ' ')))
                self.assertTrue(safe)
        for name in ('CONSOLE','COM0','COM10','LPT10','声.wav','ordinary file.json'):
            self.assertEqual(name,secure_filename(name))
