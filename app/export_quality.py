"""Bounded, owned checks of finished media against an independent PCM plan."""
from collections import deque
from contextlib import contextmanager
import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictBool

from audio_encoding_layout import get_audio_encoding_layout
from audio_export_plan import MAX_PLAN_REGIONS
from subprocess_ownership import start_owned_subprocess, stop_owned_subprocess
from tts import ensure_audio_export_active

MAX_PROBE_BYTES = 32 * 1024 * 1024
MAX_DIAGNOSTICS = 16
VERSION = 1


class ExportQualityOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_default=True)
    silence_dbfs: FiniteFloat = Field(default=-50, ge=-90, le=-20)
    max_unexpected_silence_seconds: FiniteFloat = Field(default=5, ge=.1, le=3600)
    reject_unexpected_silence: StrictBool = False


class ExportQualityError(ValueError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def get_export_quality_options(value=None):
    return ExportQualityOptions.model_validate({} if value is None else value).model_dump()


def get_export_file_version(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Finished export must be a regular private file.')
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def ensure_export_file(path):
    """Open only the same regular private file, without blocking on a FIFO race."""
    version = get_export_file_version(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        source = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    with source:
        admitted = os.fstat(source.fileno())
        if (not stat.S_ISREG(admitted.st_mode) or
                [admitted.st_dev, admitted.st_ino, admitted.st_size,
                 admitted.st_mtime_ns, admitted.st_ctime_ns] != version):
            raise ValueError('Finished export changed before capture.')
        yield source, version
    if get_export_file_version(path) != version:
        raise ValueError('Finished export changed during capture.')


def get_export_file_identity(path, cancel_check=None):
    digest = hashlib.sha256()
    with ensure_export_file(path) as (source, version):
        if not version[2]:
            raise ValueError('Finished export is empty.')
        for block in iter(lambda: source.read(1024 * 1024), b''):
            ensure_audio_export_active(cancel_check)
            digest.update(block)
    return dict(sha256=digest.hexdigest(), output_bytes=version[2], file_version=version)


def ensure_export_stage_current(path, report, cancel_check=None):
    # Rehash as well as checking metadata: Windows ctime is creation time on
    # supported Python versions, and coarse filesystem clocks can hide writes.
    try:
        identity = get_export_file_identity(path, cancel_check)
    except (OSError, ValueError) as error:
        ensure_audio_export_active(cancel_check)
        raise ExportQualityError('Finished export changed after validation.',
                                 {**report, 'status': 'failed', 'reason': 'changed_after_validation'}) from error
    if any(identity[key] != report[key] for key in identity):
        raise ExportQualityError('Finished export changed after validation.',
                                 {**report, 'status': 'failed', 'reason': 'changed_after_validation'})


def get_export_quality_plan(frames, sample_rate, channels, *, format_name, intentional_pauses=(),
                            chapters=None, options=None, chapter_time_base=1000, source_shape=None,
                            expected_tags=None):
    if any(type(value) is not int or value <= 0 for value in (frames, sample_rate, channels)):
        raise ValueError('Export validation requires a positive exact PCM frame plan.')
    if format_name not in ('wav', 'mp3', 'm4b'):
        raise ValueError('Export validation requires an explicit supported delivery format.')
    if source_shape is not None:
        if (not isinstance(source_shape, dict) or set(source_shape) != {'frames', 'sample_rate', 'channels'}
                or any(type(value) is not int or value <= 0 for value in source_shape.values())):
            raise ValueError('Invalid export source PCM shape.')
        source_shape = dict(source_shape)
    if expected_tags is not None:
        if (format_name != 'm4b' or not isinstance(expected_tags, dict)
                or not set(expected_tags) <= {'title', 'artist', 'album_artist', 'date', 'comment', 'genre'}
                or any(not isinstance(value, str) or len(value) > 20000 for value in expected_tags.values())):
            raise ValueError('Invalid expected audiobook metadata.')
        expected_tags = {key: value.replace('\r', ' ').replace('\n', ' ')
                         for key, value in expected_tags.items()}
    if len(intentional_pauses) > MAX_PLAN_REGIONS or chapters is not None and len(chapters) > MAX_PLAN_REGIONS:
        raise ValueError('Export validation plan exceeds its region limit.')
    if chapter_time_base not in (1, 1000):
        raise ValueError('Invalid export chapter clock.')
    duration = frames / sample_rate
    pauses = []
    for start, end in sorted(intentional_pauses):
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start <= end <= duration + .002):
            raise ValueError('Invalid intentional export pause.')
        if pauses and start <= pauses[-1][1]:
            pauses[-1][1] = max(end, pauses[-1][1])
        else:
            pauses.append([start, end])
    chapter_plan = None
    if chapters is not None:
        chapter_plan = []
        previous = 0.
        for title, chapter_start, chapter_end in chapters:
            start, end = chapter_start / chapter_time_base, chapter_end / chapter_time_base
            if (not isinstance(title, str) or len(title) > 20000 or
                    not math.isfinite(start) or not math.isfinite(end) or
                    not 0 <= previous <= start < end <= duration + .002):
                raise ValueError('Invalid frame-derived export chapter plan.')
            chapter_plan.append([title.replace('\r', ' ').replace('\n', ' '), start, end])
            previous = end
    plan = dict(version=VERSION, format=format_name, frames=frames, sample_rate=sample_rate, channels=channels,
                intentional_pauses=pauses, chapters=chapter_plan,
                options=get_export_quality_options(options), source_shape=source_shape, expected_tags=expected_tags)
    return plan


def get_export_encoding_plan(frames, sample_rate, channels, *, format_name, **kwargs):
    """Choose and pin a codec-supported output clock before encoding happens.

    Use the same layout policy as mastering. A build lacking the encoder fails
    explicitly instead of deriving
    the expected layout from the bytes it happened to produce.
    """
    get_export_quality_plan(frames, sample_rate, channels, format_name=format_name)
    source = dict(frames=frames, sample_rate=sample_rate, channels=channels)
    layout = get_audio_encoding_layout(format_name, sample_rate, channels)
    output_rate, output_channels = layout['frame_rate'], layout['channels']
    output_frames = (frames * output_rate + sample_rate // 2) // sample_rate
    return get_export_quality_plan(output_frames, output_rate, output_channels,
                                   format_name=format_name, source_shape=source, **kwargs)


def get_export_quality_plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def get_pcm_export_encoding_plan(pcm_plan, *, format_name, **kwargs):
    """Translate independently assembled PCM geometry into a delivery plan."""
    rate = pcm_plan['frame_rate']
    return get_export_encoding_plan(pcm_plan['frames'], rate, pcm_plan['channels'],
        format_name=format_name,
        intentional_pauses=[[start / rate, end / rate]
                            for start, end in pcm_plan['intentional_pause_frames']], **kwargs)


def _get_reusable_export_report(report, plan):
    """Accept only the finite, bounded schema emitted by the completed check."""
    if not isinstance(report, dict):
        return None
    expected = dict(version=VERSION, stage='complete',
        plan_sha256=get_export_quality_plan_digest(plan), expected_format=plan['format'],
        source_shape=plan['source_shape'], expected_frames=plan['frames'],
        sample_rate=plan['sample_rate'], channels=plan['channels'], options=plan['options'], diagnostics=[])
    fields = ('status', 'sha256', 'output_bytes', 'file_version', 'codec', 'format',
        'probed_duration_seconds', 'duration_tolerance_seconds', 'decoded_frames', 'duration_delta_seconds',
        'silence_intervals', 'unexpected_silence_count', 'unexpected_silence_seconds',
        'unexpected_silence_ranges', 'longest_unexpected_silence', 'diagnostics_abbreviated', 'seconds')
    allowed = set(expected) | set(fields) | {'chapters_checked', 'tags_checked', 'file', 'book_token'}
    if len(report) > len(allowed) or not set(report) <= allowed:
        return None
    try:
        if (any(report.get(key) != value for key, value in expected.items())
                or any(type(report.get(key)) is not int for key in
                       ('version', 'expected_frames', 'sample_rate', 'channels'))):
            return None
        if (not isinstance(report['sha256'], str) or re.fullmatch('[0-9a-f]{64}', report['sha256']) is None
                or any(type(report[key]) is not int or report[key] < minimum for key, minimum in
                    (('decoded_frames', 1), ('output_bytes', 1), ('silence_intervals', 0),
                     ('unexpected_silence_count', 0)))
                or not isinstance(report['file_version'], list) or len(report['file_version']) != 5
                or any(type(value) is not int for value in report['file_version'])
                or report['file_version'][2] != report['output_bytes']):
            return None
        if any(not isinstance(report[key], str) or len(report[key]) > 128 for key in ('codec', 'format')):
            return None
        if not get_export_format_matches(plan['format'], report['codec'], report['format']):
            return None
        tolerance = {'mp3': 2304, 'aac': 2048}.get(report['codec'], 0) / plan['sample_rate'] + 1e-6
        duration = plan['frames'] / plan['sample_rate']
        decoded_duration = report['decoded_frames'] / plan['sample_rate']
        for key in ('probed_duration_seconds', 'duration_tolerance_seconds', 'duration_delta_seconds',
                    'unexpected_silence_seconds', 'seconds'):
            if type(report[key]) not in (float, int) or not math.isfinite(report[key]) or report[key] < 0:
                return None
        if (report['duration_tolerance_seconds'] != tolerance
                or abs(report['probed_duration_seconds'] - duration) > tolerance
                or abs(decoded_duration - duration) > tolerance
                or abs(report['duration_delta_seconds'] - abs(decoded_duration - duration)) > 1e-9):
            return None
        count = report['unexpected_silence_count']
        if (report['status'] != ('review' if count else 'verified')
                or count and (plan['options']['reject_unexpected_silence'] or not report['silence_intervals'])
                or not isinstance(report['unexpected_silence_ranges'], list)
                or len(report['unexpected_silence_ranges']) != min(count, MAX_DIAGNOSTICS)
                or type(report['diagnostics_abbreviated']) is not bool
                or report['diagnostics_abbreviated'] != (count > MAX_DIAGNOSTICS)
                or report['unexpected_silence_seconds'] > decoded_duration + 1e-6):
            return None
        ranges = report['unexpected_silence_ranges']
        longest = report['longest_unexpected_silence']
        if (count == 0 and (longest is not None or report['unexpected_silence_seconds'] != 0)
                or count and not isinstance(longest, dict)):
            return None
        for region in ranges + ([longest] if count else []):
            if (not isinstance(region, dict) or set(region) != {'start_seconds', 'end_seconds'}
                    or any(type(value) not in (float, int) or not math.isfinite(value)
                           for value in region.values())
                    or not 0 <= region['start_seconds'] < region['end_seconds'] <= decoded_duration + .002
                    or region['end_seconds'] - region['start_seconds'] + 1e-6 <
                       plan['options']['max_unexpected_silence_seconds']):
                return None
        if count:
            lengths = [region['end_seconds'] - region['start_seconds'] for region in ranges]
            longest_length = longest['end_seconds'] - longest['start_seconds']
            if (max(lengths) > longest_length + 1e-6
                    or max(sum(lengths), longest_length) > report['unexpected_silence_seconds'] + 1e-6):
                return None
        if plan['chapters'] is not None:
            if type(report.get('chapters_checked')) is not int or report['chapters_checked'] != len(plan['chapters']):
                return None
            expected['chapters_checked'] = len(plan['chapters'])
        if plan['expected_tags'] is not None:
            if type(report.get('tags_checked')) is not int or report['tags_checked'] != len(plan['expected_tags']):
                return None
            expected['tags_checked'] = len(plan['expected_tags'])
        return {**expected, **{key: report[key] for key in fields}}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def get_admitted_export_report(path, plan, report=None, cancel_check=None, progress_callback=None):
    """Admit byte/plan-bound evidence across a private rename, or check again.

    An encoder callback happens before its private stage is renamed. Its inode
    timestamps therefore cannot be reused as the final stage identity. Hash the
    current bytes, then bind a copy to the current version without another decode.
    Invalid, missing, or stale evidence always takes the actual validation path.
    """
    plan = get_export_quality_plan_from_record(plan)
    ensure_audio_export_active(cancel_check)
    reusable = _get_reusable_export_report(report, plan)
    if reusable is not None:
        try:
            identity = get_export_file_identity(path, cancel_check)
        except (OSError, ValueError):
            ensure_audio_export_active(cancel_check)
            identity = None
        if identity is not None and all(identity[key] == reusable[key] for key in ('sha256', 'output_bytes')):
            admitted = copy.deepcopy(reusable)
            admitted.update(identity)
            return admitted
    return validate_finished_export(path, plan, cancel_check, progress_callback)


def get_export_quality_plan_from_record(plan):
    """Reconstruct only a known-schema, independently recorded expectation."""
    if not isinstance(plan, dict) or type(plan.get('version')) is not int or plan['version'] != VERSION:
        raise ValueError('Unsupported export quality plan version; rebuild this output.')
    normalized = get_export_quality_plan(plan['frames'], plan['sample_rate'], plan['channels'],
        format_name=plan['format'], intentional_pauses=plan['intentional_pauses'], chapters=plan['chapters'],
        options=plan['options'], chapter_time_base=1, source_shape=plan.get('source_shape'),
        expected_tags=plan.get('expected_tags'))
    source = normalized['source_shape']
    if source is not None:
        expected = get_export_encoding_plan(source['frames'], source['sample_rate'], source['channels'],
                                            format_name=normalized['format'])
        if any(normalized[key] != expected[key] for key in ('frames', 'sample_rate', 'channels')):
            raise ValueError('Recorded source frames disagree with the export encoding plan; rebuild this output.')
    return normalized


def notify_completed_export(path, filename, report, callback):
    """Deliver current-file evidence without undoing an already durable success."""
    if callback is None:
        return
    try:
        version = get_export_file_version(path)
        before = report['file_version']
        # Rename may update ctime; the same admitted inode and payload must remain.
        if version[:4] != before[:4]:
            raise ValueError('Completed export changed before evidence delivery.')
        callback({**copy.deepcopy(report), 'file': filename, 'file_version': version})
    except Exception:
        logging.getLogger('AlexandriaUI').warning(
            'Finished export was published, but its evidence callback failed.', exc_info=True)


def get_export_chapter_expectation(chapters, format_name):
    """MP4 chapter tracks represent a chapter's end by the next start marker."""
    mp4 = bool({'mov', 'mp4'} & set(format_name.split(',')))
    return [[title, start, chapters[index + 1][1] if mp4 and index + 1 < len(chapters) else end]
            for index, (title, start, end) in enumerate(chapters)]


def get_export_format_matches(expected, codec, container):
    containers = set(container.split(','))
    if expected == 'wav':
        return 'wav' in containers and codec.startswith('pcm_')
    if expected == 'mp3':
        return 'mp3' in containers and codec == 'mp3'
    return expected == 'm4b' and bool({'mov', 'mp4'} & containers) and codec == 'aac'


def _run_owned_check(command, cancel_check, timeout, *, output_limit=0,
                     consume_line=None, progress_callback=None):
    """Drain finite chunks on both pipes while the caller controls lifetime."""
    output, errors = bytearray(), []
    diagnostics = deque(maxlen=12)
    process = None
    readers = []
    started = time.monotonic()

    def read_output():
        try:
            while block := process.stdout.read(65536):
                if len(output) + len(block) > output_limit:
                    if not errors:
                        errors.append('Media probe exceeded its bounded output allowance.')
                elif not errors:
                    output.extend(block)
        except BaseException as error:
            if not errors:
                errors.append(str(error)[:240])

    def read_errors():
        try:
            while raw := process.stderr.readline(4097):
                line = raw.decode('utf-8', errors='replace').rstrip()
                diagnostics.append(line[-512:])
                if consume_line is not None:
                    consume_line(line)
        except BaseException as error:
            if not errors:
                errors.append(str(error)[:240])

    try:
        ensure_audio_export_active(cancel_check)
        process = start_owned_subprocess(command, stdout=subprocess.PIPE if output_limit else subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, start_new_session=True)
        for function in ([read_output, read_errors] if output_limit else [read_errors]):
            thread = threading.Thread(target=function, daemon=True)
            readers.append(thread)
            thread.start()
        next_progress = started + 2
        while True:
            ensure_audio_export_active(cancel_check)
            if errors:
                raise ValueError(errors[0])
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            if progress_callback and time.monotonic() >= next_progress:
                progress_callback(f'Checking finished export: {elapsed:.1f} seconds elapsed')
                next_progress = time.monotonic() + 2
            try:
                result = process.wait(timeout=min(.1, timeout - elapsed))
                break
            except subprocess.TimeoutExpired:
                continue
        for thread in readers:
            thread.join(timeout=2)
        if errors or any(thread.is_alive() for thread in readers):
            raise ValueError(errors[0] if errors else 'Media check retained an incomplete diagnostic stream.')
        ensure_audio_export_active(cancel_check)
        if result:
            raise ValueError('Finished export could not be decoded: ' + '\n'.join(diagnostics)[-2048:])
        return bytes(output)
    finally:
        if process is not None:
            try:
                if process.poll() is None or any(thread.is_alive() for thread in readers):
                    stop_owned_subprocess(process, timeout=1, force_after_grace=True)
                for thread in readers:
                    thread.join(timeout=2)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None and not any(thread.is_alive() for thread in readers):
                        pipe.close()
            finally:
                control = getattr(process, '_alexandria_control', None)
                if control is not None:
                    control.close()


class _DecodeEvidence:
    def __init__(self, plan):
        self.plan = plan
        self.frames = None
        self.nan_records = self.inf_records = 0
        self.nonfinite = False
        self.silence_start = None
        self.silence_count = self.unexpected_count = 0
        self.unexpected_seconds = 0.
        self.ranges = []
        self.longest_range = None
        self.pause_index = 0

    def _add_unexpected(self, start, end):
        if end - start + 1e-6 < self.plan['options']['max_unexpected_silence_seconds']:
            return
        self.unexpected_count += 1
        self.unexpected_seconds += end - start
        if self.longest_range is None or end - start > self.longest_range['end_seconds'] - self.longest_range['start_seconds']:
            self.longest_range = dict(start_seconds=start, end_seconds=end)
        if len(self.ranges) < MAX_DIAGNOSTICS:
            self.ranges.append(dict(start_seconds=start, end_seconds=end))

    def _finish_silence(self, end):
        if self.silence_start is None or end < self.silence_start:
            raise ValueError('Incomplete or unordered silence evidence.')
        start = self.silence_start
        self.silence_start = None
        self.silence_count += 1
        pauses = self.plan['intentional_pauses']
        while self.pause_index < len(pauses) and pauses[self.pause_index][1] <= start:
            self.pause_index += 1
        index = self.pause_index
        while index < len(pauses) and pauses[index][0] < end:
            left, right = pauses[index]
            # Only the authored interval gets a small codec/resampling edge
            # allowance. Extra silence outside it remains independently visible.
            self._add_unexpected(start, min(max(0., left - .025), end))
            start = max(start, right + .025)
            index += 1
        self._add_unexpected(start, end)

    def consume(self, line):
        stat = re.fullmatch(r'\[Parsed_astats_\d+ @ [^]]+\] Number of (samples|NaNs|Infs): (\S+)', line)
        if stat:
            name, value = stat.group(1), float(stat.group(2))
            if not math.isfinite(value) or value < 0:
                raise ValueError('Invalid decoder sample statistics.')
            if name == 'samples':
                if self.frames is not None or not value.is_integer():
                    raise ValueError('Ambiguous decoder frame count.')
                self.frames = int(value)
            else:
                if name == 'NaNs':
                    self.nan_records += 1
                else:
                    self.inf_records += 1
                self.nonfinite |= value > 0
        if re.match(r'\[silencedetect @ [^]]+\]|\[Parsed_silencedetect_\d+ @ [^]]+\]', line):
            match = re.search(r'silence_(start|end):\s*(\S+)', line)
            if not match and ('silence_start' in line or 'silence_end' in line):
                raise ValueError('Malformed decoder silence evidence.')
            if match:
                kind, value = match.group(1), float(match.group(2))
                if not math.isfinite(value) or value < 0:
                    raise ValueError('Invalid decoded silence interval.')
                if kind == 'start':
                    if self.silence_start is not None:
                        raise ValueError('Overlapping decoded silence intervals.')
                    self.silence_start = value
                else:
                    self._finish_silence(value)


def validate_finished_export(path, plan, cancel_check=None, progress_callback=None):
    """Return finite finished-byte evidence, or refuse publication explicitly."""
    started = time.monotonic()
    # Revalidate caller data before it enters command options or evidence.
    plan = get_export_quality_plan_from_record(plan)
    expected_seconds = plan['frames'] / plan['sample_rate']
    timeout = max(60., expected_seconds * 2 + 30)
    report = dict(version=VERSION, status='failed', plan_sha256=get_export_quality_plan_digest(plan),
                  expected_format=plan['format'],
                  source_shape=plan['source_shape'],
                  expected_frames=plan['frames'], sample_rate=plan['sample_rate'], channels=plan['channels'],
                  options=plan['options'], diagnostics=[], stage='identity')
    try:
        report.update(get_export_file_identity(path, cancel_check))
        report['stage'] = 'probe'
        payload = _run_owned_check(['ffprobe', '-v', 'error', '-show_entries',
            'stream=codec_type,codec_name,sample_rate,channels:format=format_name,duration:format_tags=title,artist,album_artist,date,comment,genre:chapter=start_time,end_time:chapter_tags=title',
            '-of', 'json', str(path)], cancel_check, min(60., timeout), output_limit=MAX_PROBE_BYTES)
        metadata = json.loads(payload)
        audio = [stream for stream in metadata['streams'] if stream.get('codec_type') == 'audio']
        if (len(audio) != 1 or int(audio[0]['sample_rate']) != plan['sample_rate']
                or int(audio[0]['channels']) != plan['channels']):
            raise ValueError('Finished export does not match its planned audio layout.')
        codec = audio[0]['codec_name']
        report.update(codec=codec, format=metadata['format']['format_name'])
        if not get_export_format_matches(plan['format'], codec, report['format']):
            raise ValueError('Finished media does not match the requested export format.')
        # Lossy tail padding is bounded by two codec frames, independent of book length.
        tolerance = {'mp3': 2304, 'aac': 2048}.get(codec, 0)
        tolerance_seconds = tolerance / plan['sample_rate'] + 1e-6
        probed_duration = float(metadata['format']['duration'])
        report.update(probed_duration_seconds=probed_duration if math.isfinite(probed_duration) else None,
                      duration_tolerance_seconds=tolerance_seconds)
        if (not math.isfinite(probed_duration) or probed_duration <= 0 or
                abs(probed_duration - expected_seconds) > tolerance_seconds):
            raise ValueError('Finished export container duration differs from its PCM plan.')
        if plan['expected_tags'] is not None:
            tags = metadata['format'].get('tags', {})
            if any(tags.get(key, '') != value for key, value in plan['expected_tags'].items()):
                raise ValueError('Finished export book metadata differs from its plan.')
            report['tags_checked'] = len(plan['expected_tags'])
        if plan['chapters'] is not None:
            actual = metadata.get('chapters', [])
            if len(actual) != len(plan['chapters']):
                raise ValueError('Finished export has the wrong chapter count.')
            expected_chapters = get_export_chapter_expectation(plan['chapters'], report['format'])
            for index, (item, wanted) in enumerate(zip(actual, expected_chapters)):
                title, start, end = wanted
                observed = float(item['start_time']), float(item['end_time'])
                if (not all(math.isfinite(value) for value in observed)
                        or abs(observed[0] - start) > .002 or abs(observed[1] - end) > .002
                        or item.get('tags', {}).get('title', '') != title):
                    report['diagnostics'].append(dict(kind='chapter', chapter=index,
                        expected_start_seconds=start, expected_end_seconds=end,
                        actual_start_seconds=observed[0] if math.isfinite(observed[0]) else None,
                        actual_end_seconds=observed[1] if math.isfinite(observed[1]) else None))
                    raise ValueError(f'Finished export chapter {index + 1} differs from its plan.')
            report['chapters_checked'] = len(actual)
        report['stage'] = 'decode'
        evidence = _DecodeEvidence(plan)
        settings = plan['options']
        filters = ('aformat=sample_fmts=fltp,astats=metadata=0:reset=0:measure_perchannel=none:'
                   'measure_overall=Number_of_samples+Number_of_NaNs+Number_of_Infs,'
                   f"silencedetect=noise={settings['silence_dbfs']}dB:d={settings['max_unexpected_silence_seconds']}")
        _run_owned_check(['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-xerror',
            '-threads', '1', '-filter_threads', '1', '-i', str(path), '-map', '0:a:0',
            '-vn', '-sn', '-dn', '-af', filters, '-f', 'null', '-'], cancel_check,
            timeout, consume_line=evidence.consume, progress_callback=progress_callback)
        if evidence.frames is None or not evidence.nan_records or not evidence.inf_records:
            raise ValueError('Finished decoder did not provide complete sample evidence.')
        if evidence.nonfinite:
            raise ValueError('Finished export contains non-finite audio samples.')
        if evidence.silence_start is not None:
            raise ValueError('Finished decoder did not close its final silence interval.')
        report.update(decoded_frames=evidence.frames,
            duration_delta_seconds=abs(evidence.frames / plan['sample_rate'] - expected_seconds),
            duration_tolerance_seconds=tolerance_seconds, silence_intervals=evidence.silence_count,
            unexpected_silence_count=evidence.unexpected_count,
            unexpected_silence_seconds=evidence.unexpected_seconds,
            unexpected_silence_ranges=evidence.ranges,
            longest_unexpected_silence=evidence.longest_range,
            diagnostics_abbreviated=evidence.unexpected_count > len(evidence.ranges))
        if not evidence.frames or report['duration_delta_seconds'] > tolerance_seconds:
            raise ValueError('Finished export duration differs from its PCM plan.')
        ensure_export_stage_current(path, report, cancel_check)
        if evidence.unexpected_count and settings['reject_unexpected_silence']:
            raise ValueError('Finished export contains an unexpected long silent interval.')
        report.update(stage='complete', status='review' if evidence.unexpected_count else 'verified')
        return report
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if isinstance(error, ExportQualityError):
            report.update(error.report)
            error.report = report
            raise
        ensure_audio_export_active(cancel_check)
        report['reason'] = str(error)[:240]
        raise ExportQualityError('Export validation failed: ' + report['reason'], report) from error
    finally:
        report['seconds'] = time.monotonic() - started
