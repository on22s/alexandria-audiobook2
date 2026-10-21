"""Finite cached English CPU verifier identity; no downloads or GPU discovery."""
import hashlib
import importlib
import os
from pathlib import Path
import shutil
import stat
import struct
import sys

from whisper_cpp_runtime import get_whisper_cpp_runtime
from word_timing import get_asr_options

LIBRARIES = ('libwhisper.so.1', 'libggml.so.0', 'libggml-cpu.so.0',
             'libggml-hip.so.0', 'libggml-base.so.0')
LOADER_OVERRIDES = ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'LD_AUDIT', 'GGML_BACKEND_PATH',
                    'DYLD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'DYLD_FRAMEWORK_PATH')
CODE_FILES = ('speech_verification_runtime.py', 'speech_verification_compare.py',
              'speech_verification_acoustic.py', 'word_timing.py', 'cpu_asr_scratch.py',
              'subprocess_ownership.py', 'speech_verification.py')


def get_verification_runtime(root, *, confirmation=False):
    runtime = get_whisper_cpp_runtime(root)
    if confirmation:
        runtime['model'] = os.environ.get('ALEXANDRIA_WHISPER_CPP_CONFIRM_MODEL') or str(
            Path(root) / 'whisper.cpp/models/ggml-large-v3.bin')
    return {'binary': runtime['binary'], 'model': runtime['model'],
            'decoder': shutil.which('ffmpeg') or ''}


def _version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def get_verifier_file_identity(path, limit, cancel_check):
    if cancel_check():
        raise ValueError('Speech verification cancelled.')
    requested = Path(path)
    resolved = requested.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(resolved, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ValueError('Verifier asset is not a bounded nonempty regular file.')
        digest, size = hashlib.sha256(), 0
        for block in iter(lambda: os.read(descriptor, 1024**2), b''):
            if cancel_check():
                raise ValueError('Speech verification cancelled.')
            size += len(block)
            if size > limit:
                raise ValueError('Verifier asset grew beyond its bound.')
            digest.update(block)
        if (size != before.st_size or _version(before) != _version(os.fstat(descriptor))
                or requested.resolve(strict=True) != resolved or _version(before) != _version(resolved.stat())):
            raise ValueError('Verifier asset changed during identity capture.')
    finally:
        os.close(descriptor)
    return dict(path=str(requested), resolved_path=str(resolved), sha256=digest.hexdigest(),
        size=size, file_version=list(_version(before)))


def get_verifier_model_architecture(path, expected_identity=None):
    # whisper.cpp's GGML_FILE_MAGIC + eleven int32 hparams, in native GGML LE.
    # Architecture is evidence of the requested model role, not proof of ASR accuracy.
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Verifier model is not a regular file.')
        if expected_identity is not None and list(_version(before)) != expected_identity['file_version']:
            raise ValueError('Verifier model changed before architecture inspection.')
        header = os.read(descriptor, 48)
        if _version(before) != _version(os.fstat(descriptor)):
            raise ValueError('Verifier model changed during architecture inspection.')
    finally:
        os.close(descriptor)
    if len(header) != 48:
        raise ValueError('Verifier GGML model header is incomplete.')
    magic, vocab, audio_ctx, state, heads, layers, text_ctx, text_state, text_heads, text_layers, mels, ftype = struct.unpack('<12i', header)
    if (magic != 0x67676d6c or state != text_state or layers != text_layers or
            heads != text_heads or audio_ctx != 1500 or text_ctx != 448):
        raise ValueError('Unsupported verifier GGML model architecture.')
    return dict(vocab=vocab, state=state, heads=heads, layers=layers, mels=mels, ftype=ftype)


def get_verifier_identity(runtime, role, cancel_check=lambda: False):
    """Attest the finite declared native bundle and adapter; not the whole OS loader.

    This initial contract deliberately names the qualified Linux build layout.
    Missing/other bundles are unavailable; ordinary synthesis remains usable.
    """
    if role not in ('primary', 'confirmation'):
        raise ValueError('Unknown speech verifier role.')
    if sys.platform != 'linux' or sys.byteorder != 'little':
        raise ValueError('This speech-verification provider bundle is qualified only for little-endian Linux.')
    if any(os.environ.get(name) for name in LOADER_OVERRIDES):
        raise ValueError('Dynamic loader overrides prevent the declared speech-verifier identity.')
    binary = get_verifier_file_identity(runtime['binary'], 64 * 1024**2, cancel_check)
    model = get_verifier_file_identity(runtime['model'], 4 * 1024**3, cancel_check)
    decoder = get_verifier_file_identity(runtime['decoder'], 64 * 1024**2, cancel_check)
    architecture = get_verifier_model_architecture(model['resolved_path'], model)
    expected = (51864, 768, 12, 12, 80) if role == 'primary' else (51866, 1280, 20, 32, 128)
    if tuple(architecture[key] for key in ('vocab', 'state', 'heads', 'layers', 'mels')) != expected:
        raise ValueError('Verification requires Small.en primary and distinct Large-v3 confirmation architectures.')
    directory = Path(binary['resolved_path']).parent
    libraries = [get_verifier_file_identity(directory / name, 512 * 1024**2, cancel_check)
                 for name in LIBRARIES]
    if sum(row['size'] for row in libraries) > 1024**3:
        raise ValueError('Verifier native library bundle exceeds 1 GiB.')
    code = [get_verifier_file_identity(Path(__file__).parent / name, 1024**2, cancel_check)
            for name in CODE_FILES]
    loaded = {}
    for name in CODE_FILES:
        module = importlib.import_module(Path(name).stem)
        digest = getattr(module, '_LOADED_SPEECH_VERIFICATION_CODE_SHA256', None)
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError('Loaded verifier adapter code identity is unavailable.')
        loaded[module.__name__] = digest
    return dict(version=1, role=role, binary=binary, model=model, decoder=decoder,
        architecture=architecture, libraries=libraries, adapter_files=code,
        loaded_adapter_modules=loaded,
        options=get_asr_options(), language='en', device='cpu',
        dependency_scope='Selected model, CLI, five declared local native libraries, decoder binary, loaded AA2 module code and separate disk source snapshots. System shared dependencies and OS are outside this identity.')


def ensure_distinct_verifier_confirmation(primary, confirmation):
    if (primary['role'] != 'primary' or confirmation['role'] != 'confirmation'
            or primary['model']['sha256'] == confirmation['model']['sha256']):
        raise ValueError('Confirmation needs a distinct stronger verifier checkpoint.')

from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_verification_code
_LOADED_SPEECH_VERIFICATION_CODE_SHA256 = _get_loaded_verification_code()
