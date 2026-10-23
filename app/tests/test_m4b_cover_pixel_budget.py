"""Synthetic image limits must hold independently of warning-filter timing."""
import io
import math
import struct
import zlib
import threading
import unittest
import warnings
from unittest.mock import patch

from PIL import Image
import m4b_cover


def image_bytes(size, fmt='PNG'):
    with io.BytesIO() as output:
        Image.new('RGB', size, (30, 100, 170)).save(output, format=fmt)
        return output.getvalue()


class CoverPixelBudgetTests(unittest.TestCase):
    def test_pixel_boundary_formats_and_disabled_limit(self):
        for fmt in ('PNG', 'JPEG', 'GIF'):
            raw = image_bytes((32, 24), fmt)
            for limit, accepted in ((400, False), (767, False), (768, True), (None, True)):
                with self.subTest(format=fmt, limit=limit), warnings.catch_warnings(), \
                        patch.object(Image, 'MAX_IMAGE_PIXELS', limit):
                    warnings.simplefilter('ignore', Image.DecompressionBombWarning)
                    if accepted:
                        result = m4b_cover.get_normalized_cover(raw)
                        self.assertTrue(result['data'])
                    else:
                        with self.assertRaises(m4b_cover.CoverValidationError):
                            m4b_cover.get_normalized_cover(raw)
            for limit in (0, -1):
                with self.subTest(format=fmt, limit=limit), patch.object(Image, 'MAX_IMAGE_PIXELS', limit):
                    with self.assertRaises(m4b_cover.CoverValidationError):
                        m4b_cover.get_normalized_cover(image_bytes((1, 1), fmt))

    def test_overlapping_validation_cannot_restore_a_permissive_pixel_filter(self):
        small, large = image_bytes((1, 1)), image_bytes((32, 24))
        names = ('cover-first', 'cover-second')
        arrived = {name: threading.Event() for name in names}
        release = {name: threading.Event() for name in names}
        done = {name: threading.Event() for name in names}
        opened, results = set(), {}
        original = Image.open
        def opening(*args, **kwargs):
            name = threading.current_thread().name
            if name in arrived and name not in opened:
                opened.add(name)
                arrived[name].set()
                if not release[name].wait(5):
                    raise TimeoutError('Synthetic image gate timed out')
            return original(*args, **kwargs)
        def decode(name, raw):
            try:
                m4b_cover.get_normalized_cover(raw)
                results[name] = None
            except BaseException as error:
                results[name] = error
            finally:
                done[name].set()
        threads = [threading.Thread(target=decode, args=(name, raw), name=name)
                   for name, raw in zip(names, (small, large))]
        with warnings.catch_warnings(), patch.object(Image, 'MAX_IMAGE_PIXELS', 400), \
                patch.object(Image, 'open', side_effect=opening):
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            try:
                threads[0].start()
                self.assertTrue(arrived[names[0]].wait(5))
                threads[1].start()
                self.assertTrue(arrived[names[1]].wait(5))
                release[names[0]].set()
                self.assertTrue(done[names[0]].wait(5))
                release[names[1]].set()
            finally:
                for event in release.values():
                    event.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(5)
                self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertIsNone(results[names[0]])
        self.assertIsInstance(results[names[1]], m4b_cover.CoverValidationError)

    def test_oversized_image_is_refused_before_verification_and_pixel_decode(self):
        for fmt in ('PNG', 'JPEG', 'GIF'):
            raw = image_bytes((32, 24), fmt)
            with Image.open(io.BytesIO(raw)) as probe:
                image_type = type(probe)
            with self.subTest(format=fmt), warnings.catch_warnings(), \
                    patch.object(Image, 'MAX_IMAGE_PIXELS', 400), \
                    patch.object(image_type, 'verify', side_effect=AssertionError('Over-limit verification')) as verify, \
                    patch.object(image_type, 'load', side_effect=AssertionError('Over-limit pixel decode')) as load:
                warnings.simplefilter('ignore', Image.DecompressionBombWarning)
                with self.assertRaises(m4b_cover.CoverValidationError):
                    m4b_cover.get_normalized_cover(raw)
                verify.assert_not_called()
                load.assert_not_called()

    def test_validated_source_bytes_remain_exact_and_corrupt_images_fail(self):
        for fmt, media_type in (('PNG', 'image/png'), ('JPEG', 'image/jpeg')):
            raw = image_bytes((1, 1), fmt)
            with self.subTest(format=fmt), patch.object(Image, 'MAX_IMAGE_PIXELS', 1):
                self.assertEqual(raw, m4b_cover.get_validated_cover_bytes(raw, media_type))
        with self.assertRaises(m4b_cover.CoverValidationError):
            m4b_cover.get_normalized_cover(b'not an image')

    def test_explicit_budget_is_captured_once_for_both_open_passes(self):
        raw = image_bytes((32, 24))
        with Image.open(io.BytesIO(raw)) as probe:
            image_type = type(probe)
        original_verify = image_type.verify
        def verify(image):
            result = original_verify(image)
            Image.MAX_IMAGE_PIXELS = 400
            return result
        with warnings.catch_warnings(), patch.object(Image, 'MAX_IMAGE_PIXELS', None), \
                patch.object(image_type, 'verify', side_effect=verify, autospec=True):
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            self.assertTrue(m4b_cover.get_normalized_cover(raw)['data'])

    def test_shipping_default_limit_rejects_large_header_without_decoding_pixels(self):
        raw = image_bytes((1, 1))
        with Image.open(io.BytesIO(raw)) as probe:
            image_type = type(probe)
        limit = Image.MAX_IMAGE_PIXELS
        self.assertIsInstance(limit, int)
        edge = math.isqrt(limit) + 1
        header = struct.pack('>II', edge, edge) + raw[24:29]
        declared = raw[:16] + header + struct.pack('>I', zlib.crc32(b'IHDR' + header) & 0xffffffff) + raw[33:]
        self.assertLess(len(declared), 1024)
        with warnings.catch_warnings(), \
                patch.object(image_type, 'verify', side_effect=AssertionError('Large header verify')) as verify, \
                patch.object(image_type, 'load', side_effect=AssertionError('Large header decode')) as load:
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            with self.assertRaises(m4b_cover.CoverValidationError):
                m4b_cover.get_normalized_cover(declared)
            verify.assert_not_called()
            load.assert_not_called()

    def test_media_type_mismatch_and_normalized_output_limit_remain_rejected(self):
        for fmt, media in (('PNG', 'image/jpeg'), ('JPEG', 'image/png'), ('PNG', 'image/gif')):
            with self.subTest(format=fmt, media=media):
                with self.assertRaises(m4b_cover.CoverValidationError):
                    m4b_cover.get_validated_cover_bytes(image_bytes((1, 1), fmt), media)
        with self.assertRaises(m4b_cover.CoverValidationError):
            m4b_cover.get_normalized_cover(image_bytes((1, 1)), max_output_bytes=1)
