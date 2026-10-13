import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from routers import editor
from tests import test_chapter_export as fixtures
from tests.test_m4b_cover_admission import image_bytes


def get_minimal_jpeg():
    def marker(code, payload):
        return bytes([255, code]) + struct.pack('>H', len(payload) + 2) + payload
    return (b'\xff\xd8'
            + marker(0xdb, b'\x00' + bytes([1]) * 64)
            + marker(0xc0, b'\x08\x00\x01\x00\x01\x01\x01\x11\x00')
            + marker(0xc4, b'\x00\x01' + bytes(15) + b'\x00'
                     + b'\x10\x01' + bytes(15) + b'\x00')
            + marker(0xda, b'\x01\x01\x00\x00\x3f\x00')
            + b'\x3f\xff\xd9')


class M4bCoverTrailingPayloadTests(unittest.TestCase):
    def test_jpeg_orientation_is_applied_to_pixels_before_actual_m4b_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = io.BytesIO()
            with Image.new('RGB', (32, 24), (30, 100, 170)) as image:
                exif = image.getexif()
                exif[274] = 6
                image.save(data, format='JPEG', exif=exif)
            app = FastAPI()
            app.include_router(editor.router)
            with patch.object(editor, 'DATA_DIR', str(root)), TestClient(app) as client:
                response = client.post('/api/m4b_cover', files={
                    'file': ('portrait.jpg', data.getvalue(), 'image/jpeg')})
            self.assertEqual(200, response.status_code, response.text)
            cover = root / 'm4b_cover.jpg'
            with Image.open(cover) as image:
                self.assertEqual((24, 32), image.size)
                self.assertNotIn(274, image.getexif())
            pm, chunks = fixtures._project(str(root))
            pm.save_chunks(chunks)
            ok, message = pm.merge_m4b(metadata={'cover_path': str(cover)})
            self.assertTrue(ok, message)
            exported = subprocess.run(['ffprobe', '-v', 'error', '-show_streams',
                                       '-of', 'json', str(root / 'audiobook.m4b')],
                                      capture_output=True, text=True, check=True, timeout=10)
            pictures = [row for row in json.loads(exported.stdout)['streams']
                        if row.get('disposition', {}).get('attached_pic')]
            self.assertEqual(1, len(pictures))
            self.assertEqual((24, 32), (pictures[0]['width'], pictures[0]['height']))

    def test_trailing_video_is_retired_before_actual_m4b_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tail = root / 'tail.ts'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi',
                            '-i', 'color=c=black:s=16x16:r=5:d=1', '-c:v', 'libx264',
                            '-threads', '1', '-preset', 'ultrafast', '-f', 'mpegts', str(tail)],
                           check=True, capture_output=True, timeout=10)
            video = tail.read_bytes()
            pm, chunks = fixtures._project(str(root))
            pm.save_chunks(chunks)
            app = FastAPI()
            app.include_router(editor.router)
            with patch.object(editor, 'DATA_DIR', str(root)), TestClient(app) as client:
                for fmt, payload, size in (('JPEG', get_minimal_jpeg(), (1, 1)),
                                           ('PNG', image_bytes('PNG'), (32, 24))):
                    with self.subTest(format=fmt):
                        response = client.post('/api/m4b_cover', files={
                            'file': ('cover.' + fmt.lower(), payload + video, 'image/' + fmt.lower())})
                        self.assertEqual(200, response.status_code, response.text)
                        cover = root / 'm4b_cover.jpg'
                        self.assertFalse(video in cover.read_bytes(), 'published cover retained video payload')
                        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams',
                                                '-show_format', '-of', 'json', str(cover)],
                                               capture_output=True, text=True, check=True, timeout=10)
                        details = json.loads(probe.stdout)
                        self.assertNotEqual('mpegts', details['format']['format_name'])
                        self.assertEqual(['mjpeg' if fmt == 'JPEG' else 'png'],
                                         [row['codec_name'] for row in details['streams']])
                        ok, message = pm.merge_m4b(metadata={'cover_path': str(cover)})
                        self.assertTrue(ok, message)
                        exported = subprocess.run(['ffprobe', '-v', 'error', '-show_streams',
                                                   '-of', 'json', str(root / 'audiobook.m4b')],
                                                  capture_output=True, text=True, check=True, timeout=10)
                        streams = json.loads(exported.stdout)['streams']
                        pictures = [row for row in streams if row.get('disposition', {}).get('attached_pic')]
                        self.assertEqual(1, len(pictures))
                        self.assertEqual(size, (pictures[0]['width'], pictures[0]['height']))
                        self.assertTrue(any(row['codec_type'] == 'audio' for row in streams))
                        self.assertFalse(list(root.glob('m4b_cover.jpg.upload.*')))

    def test_png_pixels_and_alpha_survive_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = io.BytesIO()
            expected = Image.new('RGBA', (2, 3), (30, 100, 170, 90))
            expected.save(data, format='PNG')
            upload = root / 'cover.upload'
            upload.write_bytes(data.getvalue() + b'trailing fixture bytes')
            cover = root / 'cover.jpg'
            editor._apply_m4b_cover(str(upload), str(cover))
            with Image.open(cover) as actual:
                self.assertEqual('PNG', actual.format)
                self.assertEqual(expected.tobytes(), actual.convert('RGBA').tobytes())
            self.assertNotIn(b'trailing fixture bytes', cover.read_bytes())
            self.assertFalse(upload.exists())
            expected.close()

    def test_normalization_failure_keeps_prior_cover_and_cleans_owned_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cover = root / 'cover.jpg'
            original = image_bytes('JPEG')
            cover.write_bytes(original)
            foreign = root / 'foreign.upload'
            foreign.write_bytes(b'keep')
            for fmt in ('JPEG', 'PNG'):
                with self.subTest(format=fmt):
                    upload = root / 'cover.upload'
                    upload.write_bytes(image_bytes(fmt))
                    with patch.object(Image.Image, 'save', side_effect=OSError('fixture normalization failure')):
                        with self.assertRaisesRegex(OSError, 'fixture normalization failure'):
                            editor._apply_m4b_cover(str(upload), str(cover))
                    self.assertEqual(original, cover.read_bytes())
                    self.assertFalse(upload.exists())
                    self.assertFalse(Path(str(upload) + '.jpg').exists())
                    self.assertEqual(b'keep', foreign.read_bytes())
