"""Pure cover decoding shared by uploads and source publication review."""
import hashlib
import io
import warnings

MAX_COVER_BYTES = 25 * 1024**2


class CoverValidationError(ValueError):
    pass


def _get_cover_image(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_COVER_BYTES:
        raise CoverValidationError('Cover must contain at most 25 MiB of image data')
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with io.BytesIO(raw) as source, Image.open(source) as image:
                image.verify()
            with io.BytesIO(raw) as source, Image.open(source) as image:
                image.load()
                source_format = image.format
                cover_format = 'PNG' if source_format == 'PNG' else 'JPEG'
                converted = ImageOps.exif_transpose(image)
                if cover_format == 'JPEG' and converted.mode != 'RGB':
                    try:
                        rgb = converted.convert('RGB')
                    finally:
                        converted.close()
                    converted = rgb
    except (UnidentifiedImageError, OSError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise CoverValidationError('Cover must contain a complete, decodable image') from error
    return converted, cover_format, source_format


def get_validated_cover_bytes(raw, media_type):
    """Recheck stored pixels without a second lossy JPEG normalization."""
    converted, _format, source_format = _get_cover_image(raw)
    try:
        expected = {'image/jpeg': 'JPEG', 'image/png': 'PNG'}.get(media_type)
        if expected is None or source_format != expected:
            raise CoverValidationError('Stored cover format does not match its image type')
    finally:
        converted.close()
    return raw


def get_normalized_cover(raw, *, max_output_bytes=MAX_COVER_BYTES):
    """Return complete JPEG/PNG pixels without trailing input payloads or I/O."""
    if type(max_output_bytes) is not int or not 0 < max_output_bytes <= MAX_COVER_BYTES:
        raise ValueError('Invalid normalized cover limit')
    converted, cover_format, _source_format = _get_cover_image(raw)

    class BoundedOutput(io.BytesIO):
        def write(self, data):
            if self.tell() + len(data) > max_output_bytes:
                raise CoverValidationError('Normalized cover exceeds the image byte limit')
            return super().write(data)

    try:
        with BoundedOutput() as output:
            converted.save(output, format=cover_format)
            data = output.getvalue()
    finally:
        converted.close()
    return {'data': data, 'media_type': 'image/png' if cover_format == 'PNG' else 'image/jpeg',
            'sha256': hashlib.sha256(data).hexdigest()}
