import hashlib
import io
import unittest
from unittest.mock import patch

from PIL import Image
from m4b_cover import CoverValidationError, get_normalized_cover


class M4bCoverBytesTests(unittest.TestCase):
    def image(self, fmt='PNG'):
        with Image.new('RGBA' if fmt == 'PNG' else 'RGB', (8, 6), (30, 100, 170)) as image:
            out = io.BytesIO()
            image.save(out, format=fmt)
            return out.getvalue()

    def test_pure_normalization_strips_payload_preserves_png_pixels_and_hashes_result(self):
        raw = self.image()
        with patch('builtins.open', side_effect=AssertionError('Must not touch files')):
            result = get_normalized_cover(raw + b'trailing fixture payload')
        self.assertEqual(raw, result['data'])
        self.assertEqual('image/png', result['media_type'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['sha256'])

    def test_small_output_budget_refuses_before_returning_a_cover(self):
        with self.assertRaisesRegex(CoverValidationError, 'byte limit'):
            get_normalized_cover(self.image(), max_output_bytes=20)

    def test_bad_or_truncated_bytes_cannot_become_cover_candidates(self):
        for raw in (b'', b'not an image', self.image()[:-20]):
            with self.subTest(length=len(raw)), self.assertRaises(CoverValidationError):
                get_normalized_cover(raw)

    def test_non_png_image_is_normalized_as_rgb_jpeg(self):
        result = get_normalized_cover(self.image('GIF'))
        self.assertEqual('image/jpeg', result['media_type'])
        with Image.open(io.BytesIO(result['data'])) as image:
            self.assertEqual('JPEG', image.format)
            self.assertEqual('RGB', image.mode)
            self.assertEqual((8, 6), image.size)


if __name__ == '__main__':
    unittest.main()
