"""Bounded structural/frame proof for the private, generated Audacity ZIP."""
import hashlib
import os
import stat
import struct
import time
import zipfile

from archive_utils import MAX_ARCHIVE_MEMBERS
from audio_streaming import ensure_riff_pcm_size
from export_quality import (ExportQualityError, MAX_DIAGNOSTICS, MAX_PROBE_BYTES,
    ensure_export_file, ensure_export_stage_current, get_export_file_identity,
    get_export_quality_plan_digest)
from tts import ExportCancelled, ensure_audio_export_active

READ_BYTES = 1024 * 1024
VERSION = 1
SCOPE = 'audacity_structure_and_frames'


def _get_plan(track_shapes, text_members):
    if (not isinstance(track_shapes, dict) or not track_shapes
            or len(track_shapes) + 2 > MAX_ARCHIVE_MEMBERS
            or set(text_members) != {'project.lof', 'labels.txt'}):
        raise ValueError('Invalid Audacity archive member plan.')
    tracks = {}
    for name, shape in track_shapes.items():
        if (not isinstance(name, str) or not name.endswith('.wav')
                or name != os.path.basename(name) or '\\' in name or '\0' in name
                or len(name.encode('utf-8')) > 65535):
            raise ValueError('Invalid Audacity track filename.')
        layout, frames = shape
        channels, rate, width = layout
        if (any(type(value) is not int or value <= 0 for value in (*layout, frames))
                or width not in (1, 2, 4)):
            raise ValueError('Audacity validation needs an independent positive PCM frame plan.')
        data_bytes = frames * channels * width
        ensure_riff_pcm_size(data_bytes)
        # These writers produce ordinary 44-byte RIFF integer PCM, including
        # the compatibility builder's plain-WAV export. Integer samples are
        # finite by construction; inactive-speaker silence is intentional.
        header = struct.pack('<4sL4s4sLHHLLHH4sL', b'RIFF', 36 + data_bytes,
            b'WAVE', b'fmt ', 16, 1, channels, rate, channels * rate * width,
            channels * width, width * 8, b'data', data_bytes)
        tracks[name] = dict(frames=frames, sample_rate=rate, channels=channels,
                            sample_width=width, output_bytes=44 + data_bytes,
                            header=header)
    texts = {}
    for name, content in text_members.items():
        if not isinstance(content, bytes) or len(content) > MAX_PROBE_BYTES:
            raise ValueError('Audacity LOF/labels metadata exceeds its byte limit.')
        texts[name] = dict(output_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    canonical = dict(version=VERSION, verification_scope=SCOPE,
        tracks={name: {key: value for key, value in track.items() if key != 'header'}
                for name, track in tracks.items()}, texts=texts)
    return tracks, texts, get_export_quality_plan_digest(canonical)


class _ArchiveInput:
    """Bound parser reads and pin checked metadata; PCM stays on the file."""
    def __init__(self, source, size):
        self.source, self.size, self.position = source, size, 0
        self.tail_start, self.tail = size, b''

    def tell(self):
        return self.position

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        if whence not in (0, 1, 2):
            raise ValueError('Invalid archive seek mode.')
        position = offset + (self.position if whence == 1 else self.size if whence == 2 else 0)
        if position < 0:
            raise OSError('Archive seek precedes the file.')
        self.position = position
        return position

    def read(self, size=-1):
        remaining = max(0, self.size - self.position)
        size = remaining if size is None or size < 0 else min(size, remaining)
        # The directory plus the maximum EOCD comment and ordinary ZIP64
        # records is the largest parser read. Member reads use READ_BYTES.
        if size > MAX_PROBE_BYTES + 65557 + 76:
            raise ValueError('Audacity ZIP parser read exceeds its metadata bound.')
        parts = []
        if self.position < self.tail_start and size:
            count = min(size, self.tail_start - self.position)
            self.source.seek(self.position)
            data = self.source.read(count)
            parts.append(data)
            self.position += len(data)
            size -= len(data)
            if len(data) != count:
                return data
        if size:
            offset = self.position - self.tail_start
            data = self.tail[offset:offset + size]
            parts.append(data)
            self.position += len(data)
        return b''.join(parts)


def _directory_end(source, end):
    # Python 3.10 patch releases disagree about _ECD_LOCATION for ZIP64:
    # older releases leave it at the ordinary EOCD, newer ones move it to
    # the ZIP64 record. Use physical signatures, retaining prefix support.
    location = end[zipfile._ECD_LOCATION]
    source.seek(location)
    signature = source.read(4)
    if signature == zipfile.stringEndArchive64:
        return location
    if signature != zipfile.stringEndArchive:
        raise ValueError('Audacity ZIP end-record signature is invalid.')
    if end[zipfile._ECD_SIGNATURE] != zipfile.stringEndArchive64:
        return location
    record_start = location - zipfile.sizeEndCentDir64Locator - zipfile.sizeEndCentDir64
    if record_start < 0:
        raise ValueError('Audacity ZIP64 end record is truncated.')
    source.seek(record_start)
    record = struct.unpack(zipfile.structEndArchive64,
                           source.read(zipfile.sizeEndCentDir64))
    locator = struct.unpack(zipfile.structEndArchive64Locator,
                            source.read(zipfile.sizeEndCentDir64Locator))
    # Our writer emits the fixed ZIP64 records, without extensible data.
    if (record[0] != zipfile.stringEndArchive64 or record[1] != 44
            or locator[0] != zipfile.stringEndArchive64Locator
            or record[7] != end[zipfile._ECD_ENTRIES_TOTAL]
            or record[8] != end[zipfile._ECD_SIZE]):
        raise ValueError('Audacity ZIP64 end record differs from its directory.')
    return record_start


def _check_directory(source, expected_names, cancel_check):
    # Reuse stdlib's bounded EOCD/ZIP64 discovery, then pin its directory and
    # trailer before any callback. ZipFile's second parse sees the exact same
    # metadata even if a private stage is rewritten; the final SHA still binds
    # all actual file bytes. No PCM is buffered by this view.
    end = zipfile._EndRecData(source)
    if (end is None or end[zipfile._ECD_SIZE] > MAX_PROBE_BYTES
            or end[zipfile._ECD_ENTRIES_TOTAL] != len(expected_names)):
        raise ValueError('Audacity ZIP directory differs from its bounded member plan.')
    size = end[zipfile._ECD_SIZE]
    start = _directory_end(source, end) - size
    tail_size = source.size - start
    if start < 0 or tail_size > MAX_PROBE_BYTES + 65557 + 76:
        raise ValueError('Audacity ZIP metadata exceeds its byte limit.')
    source.seek(start)
    tail = source.read(tail_size)
    if len(tail) != tail_size:
        raise ValueError('Audacity ZIP directory is truncated.')
    source.tail_start, source.tail = start, tail
    directory = memoryview(tail)[:size]
    offset, names = 0, set()
    while offset < size:
        ensure_audio_export_active(cancel_check)
        if len(names) >= len(expected_names) or size - offset < zipfile.sizeCentralDir:
            raise ValueError('Audacity ZIP contains an unexpected central-directory record.')
        entry = struct.unpack_from(zipfile.structCentralDir, directory, offset)
        if entry[zipfile._CD_SIGNATURE] != zipfile.stringCentralDir:
            raise ValueError('Audacity ZIP central-directory signature is invalid.')
        name_start = offset + zipfile.sizeCentralDir
        name_end = name_start + entry[zipfile._CD_FILENAME_LENGTH]
        offset = name_end + entry[zipfile._CD_EXTRA_FIELD_LENGTH] + entry[zipfile._CD_COMMENT_LENGTH]
        if offset > size:
            raise ValueError('Audacity ZIP central-directory record is truncated.')
        name = bytes(directory[name_start:name_end]).decode('utf-8' if entry[5] & 0x800 else 'cp437')
        if name not in expected_names or name in names:
            raise ValueError('Audacity ZIP contains duplicate or unexpected member names.')
        names.add(name)
    if names != expected_names:
        raise ValueError('Audacity ZIP contains missing members.')


def validate_audacity_export(path, track_shapes, text_members, cancel_check=None,
                             progress_callback=None):
    """Stream exact member/header/frame/CRC checks; never extract a PCM track.

    This scope certifies archive structure and independent frame geometry. It
    does not classify intended multitrack gaps as missing narration or certify
    speech content. The same ZIP bytes must survive validation and publication.
    """
    started = time.monotonic()
    report = dict(version=VERSION, status='failed', stage='plan', format='zip',
        verification_scope=SCOPE, diagnostics=[], tracks_checked=0, members_checked=0,
        track_summaries=[], track_summaries_abbreviated=False)
    try:
        ensure_audio_export_active(cancel_check)
        tracks, texts, digest = _get_plan(track_shapes, text_members)
        report['plan_sha256'] = digest
        report['stage'] = 'archive'
        report.update(get_export_file_identity(path, cancel_check))
        if progress_callback:
            progress_callback('Checking Audacity archive and PCM tracks')
        with ensure_export_file(path) as (source, version):
            if version != report['file_version']:
                raise ValueError('Audacity ZIP changed before validation.')
            bounded_source = _ArchiveInput(source, version[2])
            _check_directory(bounded_source, set(tracks) | set(texts), cancel_check)
            ensure_audio_export_active(cancel_check)
            with zipfile.ZipFile(bounded_source) as archive:
                members = archive.infolist()
                expected = set(tracks) | set(texts)
                if len(members) != len(expected) or {item.filename for item in members} != expected:
                    raise ValueError('Audacity ZIP contains missing, duplicate or unexpected members.')
                for item in members:
                    ensure_audio_export_active(cancel_check)
                    name = item.filename
                    report['member'] = name[:240]
                    report['stage'] = 'member'
                    kind = stat.S_IFMT(item.external_attr >> 16)
                    if (item.is_dir() or kind not in (0, stat.S_IFREG)
                            or item.flag_bits & 1
                            or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                        raise ValueError('Audacity member is not an ordinary readable file.')
                    planned = tracks.get(name, texts.get(name))
                    if item.file_size != planned['output_bytes']:
                        raise ValueError('Audacity member byte count differs from its independent plan.')
                    consumed = 0
                    content_hash = hashlib.sha256()
                    with archive.open(item) as member:
                        if name in tracks:
                            header = member.read(44)
                            if header != planned['header']:
                                raise ValueError('Audacity WAV header/layout/frames differ from its independent PCM plan.')
                            consumed = len(header)
                        while True:
                            ensure_audio_export_active(cancel_check)
                            block = member.read(READ_BYTES)
                            if not block:
                                break
                            consumed += len(block)
                            if consumed > planned['output_bytes']:
                                raise ValueError('Audacity member exceeds its independent byte plan.')
                            if name in texts:
                                content_hash.update(block)
                        # Reading through EOF also exercises ZIP's CRC check.
                    if consumed != planned['output_bytes']:
                        raise ValueError('Audacity member is truncated.')
                    if name in texts:
                        if content_hash.hexdigest() != planned['sha256']:
                            raise ValueError('Audacity LOF/labels differ from the source-derived plan.')
                    else:
                        report['tracks_checked'] += 1
                        if len(report['track_summaries']) < MAX_DIAGNOSTICS:
                            report['track_summaries'].append(dict(file=name,
                                **{key: planned[key] for key in ('frames', 'sample_rate', 'channels', 'sample_width')}))
                    report['members_checked'] += 1
        ensure_export_stage_current(path, report, cancel_check)
        report.update(status='verified', stage='complete',
            track_summaries_abbreviated=len(tracks) > MAX_DIAGNOSTICS)
        report.pop('member', None)
        return report
    except ExportCancelled:
        raise
    except Exception as error:
        ensure_audio_export_active(cancel_check)
        if isinstance(error, ExportQualityError):
            report.update(error.report)
        report['status'] = 'failed'
        report['reason'] = str(error)[:240]
        raise ExportQualityError('Audacity validation failed: ' + report['reason'], report) from error
    finally:
        report['seconds'] = time.monotonic() - started
