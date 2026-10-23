"""Pure cover decoding shared by uploads and source publication review."""
import hashlib
import io
import json
import re
import warnings

MAX_COVER_BYTES = 25 * 1024**2


class CoverValidationError(ValueError):
    pass


def ensure_m4b_output_cover(path, expected_sha256, cancel_check=None):
    """Bind the exact attached picture (or its absence) to full-file identity."""
    from export_quality import get_export_file_identity, get_export_file_version
    from json_metadata import get_unique_json_object
    from media_process import run_owned_media_check
    from tts import ensure_audio_export_active
    if expected_sha256 is not None and (not isinstance(expected_sha256,str)
            or re.fullmatch(r'[0-9a-f]{64}',expected_sha256) is None):
        raise CoverValidationError('Invalid expected M4B cover digest')
    check = lambda: ensure_audio_export_active(cancel_check)
    identity = get_export_file_identity(path,cancel_check)
    raw = run_owned_media_check(['ffprobe','-v','error','-select_streams','v',
        '-show_entries','stream=index,codec_type:stream_disposition=attached_pic',
        '-of','json',str(path)],check,30,output_limit=1024**2)
    try:
        document = json.loads(raw,object_pairs_hook=get_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite cover probe')))
    except (ValueError,UnicodeError,RecursionError) as error:
        raise CoverValidationError('Invalid M4B cover probe') from error
    if not isinstance(document,dict) or not isinstance(document.get('streams'),list):
        raise CoverValidationError('Invalid M4B cover streams')
    streams = document['streams']
    if expected_sha256 is None:
        if streams:
            raise CoverValidationError('Unapproved M4B cover stream')
    else:
        if (len(streams) != 1 or not isinstance(streams[0],dict)
                or streams[0].get('codec_type') != 'video'
                or not isinstance(streams[0].get('disposition'),dict)
                or type(streams[0]['disposition'].get('attached_pic')) is not int
                or streams[0]['disposition']['attached_pic'] != 1):
            raise CoverValidationError('M4B must contain exactly one approved attached picture')
        digest = hashlib.sha256()
        run_owned_media_check(['ffmpeg','-nostdin','-v','error','-i',str(path),
            '-map','0:v:0','-c:v','copy','-f','image2pipe','-'],check,60,
            output_limit=MAX_COVER_BYTES,consume_output=digest.update)
        if digest.hexdigest() != expected_sha256:
            raise CoverValidationError('Finished M4B cover differs from approval')
    if get_export_file_version(path) != identity['file_version']:
        raise CoverValidationError('M4B changed during cover validation')
    return identity

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
