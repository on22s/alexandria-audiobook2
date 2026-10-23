"""Bounded cancellable content identity for admitted audio descriptors."""
import hashlib


def read_bounded_audio_hash(stream, limit, check_active):
    digest, size = hashlib.sha256(), 0
    stream.seek(0)
    while True:
        check_active()
        block = stream.read(1024**2)
        if not block:
            break
        size += len(block)
        if size > limit:
            raise ValueError('Take audio grew beyond its size limit.')
        digest.update(block)
    return digest.hexdigest(), size
