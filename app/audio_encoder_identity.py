"""Bound the invoked FFmpeg executable and its observed version output.

This fingerprints the invoked file and reported runtime, not transitive shared
libraries or wrapper targets. It is not protection from a hostile local loader.
Only the parent probes version; isolated workers verify the known executable
without creating another process owner. version_sha256 is enforced by the
parent before and after worker execution; the worker verifies binary identity
without launching a second version probe.
"""
import hashlib
from pathlib import Path
import shutil
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from json_metadata import get_metadata_json_bytes
from media_process import run_owned_media_check
from project_scope_files import capture_scope_file

# Accommodate static native distributions; hash in bounded blocks, never load
# the executable into RAM. This is separate from the metadata byte allowance.
MAX_EXECUTABLE_BYTES = 512 * 1024**2
MAX_VERSION_BYTES = 64 * 1024
VersionPart = Annotated[StrictInt, Field(ge=0, le=2**64-1)]
Digest = Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{64}$')]


class FFmpegBinaryIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: StrictStr = Field(min_length=1, max_length=4096)
    version: list[VersionPart] = Field(min_length=5, max_length=5)
    sha256: Digest


class FFmpegRuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    binary: FFmpegBinaryIdentity
    version_sha256: Digest


def get_ffmpeg_binary_identity(check_active=lambda: None):
    check_active()
    selected = shutil.which('ffmpeg')
    if selected is None:
        raise ValueError('Native audio encoder is unavailable')
    path = Path(selected).resolve(strict=True)
    facts, _ = capture_scope_file(path.parent, path.name,
        limit=MAX_EXECUTABLE_BYTES, cancel_check=check_active)
    if not facts['exists']:
        raise ValueError('Native audio encoder changed during capture')
    return FFmpegBinaryIdentity(path=str(path),version=facts['file_version'],sha256=facts['sha256'])


def require_ffmpeg_binary_identity(expected, check_active=lambda: None):
    """Resolve trusted PATH independently; never execute a request-supplied path."""
    expected = FFmpegBinaryIdentity.model_validate(expected)
    current = get_ffmpeg_binary_identity(check_active)
    if current != expected:
        raise ValueError('Native audio encoder differs from its approved identity')
    return current.path


def get_ffmpeg_runtime_identity(check_active=lambda: None):
    before = get_ffmpeg_binary_identity(check_active)
    raw = run_owned_media_check([before.path,'-version'],check_active,10,
                               output_limit=MAX_VERSION_BYTES)
    if not raw.startswith(b'ffmpeg version '):
        raise ValueError('Native audio encoder returned an invalid version record')
    require_ffmpeg_binary_identity(before,check_active)
    return FFmpegRuntimeIdentity(binary=before,version_sha256=hashlib.sha256(raw).hexdigest())


def get_ffmpeg_runtime_digest(identity):
    identity = FFmpegRuntimeIdentity.model_validate(identity)
    raw = get_metadata_json_bytes(identity.model_dump(),16384,'Native encoder identity')
    return hashlib.sha256(raw).hexdigest()
