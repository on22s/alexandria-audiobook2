"""Source snapshots and short existing-owner/book/chunk publication admission."""
import copy
from collections import Counter
from contextlib import contextmanager, ExitStack
import os
import stat
import time

from audio_generation_owner import (AudioGenerationCancelled, ensure_audio_book_admission,
                                    ensure_audio_generation_book,
                                    ensure_audio_generation_publication)
from book_state_transaction import get_book_operation_identity
from chapter_timing_cache import read_source_identity
from editor_poll_snapshot import get_snapshot_digest
from export_quality import get_export_file_version
from tts import ExportCancelled, ensure_audio_export_active
from utils import file_lock


@contextmanager
def _ensure_export_book(root, timeout=10, cancel_check=None):
    try:
        with ensure_audio_generation_book(root, timeout=timeout,
                                          cancel_check=cancel_check):
            yield
    except AudioGenerationCancelled as error:
        raise ExportCancelled('Export cancelled') from error


@contextmanager
def _ensure_export_capture(root, *, recover=True, cancel_check=None):
    # Loaders and settings providers may themselves check the task owner. Admit
    # around capture, while holding only the book lock during those callbacks.
    # An owned capture may outwait a long book publication (see the shared
    # audio-book admission policy) instead of failing at the old try-lock bound.
    try:
        with ensure_audio_generation_publication():
            pass
        with ensure_audio_book_admission(root, recover=recover,
                                         cancel_check=cancel_check):
            yield
        with ensure_audio_generation_publication():
            pass
    except AudioGenerationCancelled as error:
        raise ExportCancelled('Export cancelled') from error


def _get_book_identity(root):
    return get_book_operation_identity(root)


def _get_rows_digest(rows):
    from project import get_chunk_generation_inputs
    fields = ('id', 'uid', 'status', 'audio_path', 'audio_revision', 'pause_after',
              '_chapter_start', 'chapter_origin', 'spoken')
    return get_snapshot_digest([{**get_chunk_generation_inputs(row),
        **{key: row[key] for key in fields if key in row}} for row in rows])


def get_export_editor_revision(book_token, rows):
    """Identify the exact book and rows used to admit an export."""
    return get_snapshot_digest([book_token, rows])


def _get_saved_rows(manager):
    # Caller owns both existing chunk locks. Do not regenerate/backfill rows here.
    try:
        return manager._get_saved_chunk_rows_locked()
    except FileNotFoundError:
        return None


def _get_saved_rows_digest(manager):
    rows = _get_saved_rows(manager)
    return _get_rows_digest(rows) if rows is not None else None


def _get_source_version(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class ExportSourceSnapshot:
    """Keep immutable source facts; hash outside final publication locks."""
    def __init__(self, manager, chunks=None, cancel_check=None, additional_sources=(),
                 *, expected_editor_revision=None):
        self.manager, self.cancel_check = manager, cancel_check
        self.expected_editor_revision = expected_editor_revision
        self.path_contract = self._get_path_contract()
        self.root = self.path_contract['root_dir']
        self.real_root = os.path.realpath(self.root)
        ensure_audio_export_active(cancel_check)
        state_path = os.path.join(self.root, 'state.json')
        with _ensure_export_capture(self.root, recover=expected_editor_revision is None,
                                    cancel_check=self.cancel_check):
            # A retained script from an older selection must never certify this
            # capture. The request-time preflight checks the same fence, but the
            # shared capture is also entered by workers and other exporters, so
            # enforce it here where the rows and audio paths are actually taken.
            from script_selection_binding import require_published_selection
            require_published_selection(self.root)
            metadata_versions = {path: _get_source_version(path)
                for path in (state_path, manager.script_path, manager.config_path)}
            self.book = _get_book_identity(self.root)
            from project_scope_exports import read_book_state_bounded
            state = read_book_state_bounded(self.root)
            if expected_editor_revision is not None:
                with manager._chunks_lock, file_lock(manager.chunks_path):
                    current = _get_saved_rows(manager)
                    revision = get_export_editor_revision(get_snapshot_digest(self.book), current)
                    if revision != expected_editor_revision:
                        raise ValueError('Book or Editor rows changed after export admission. Retry the export.')
                    self.saved_rows_digest = _get_rows_digest(current) if current is not None else None
                if current is None:
                    current = []
            else:
                with manager._chunks_lock, file_lock(manager.chunks_path):
                    before_rows = _get_saved_rows_digest(manager)
                current = copy.deepcopy(manager.load_chunks())
                with manager._chunks_lock, file_lock(manager.chunks_path):
                    self.saved_rows_digest = _get_saved_rows_digest(manager)
                    if (self.saved_rows_digest != before_rows
                            and self.saved_rows_digest != _get_rows_digest(current)):
                        raise ValueError('Editor rows changed while capturing export sources. Retry the export.')
            self.chunks = current if chunks is None else copy.deepcopy(chunks)
            if chunks is not None and self.saved_rows_digest is not None:
                # Explicit subsets are allowed, but each supplied row must still
                # describe a live UID (or an exact legacy ID), with no stale take.
                live = Counter(_get_rows_digest([row]) for row in current)
                for row in self.chunks:
                    digest = _get_rows_digest([row])
                    if not live[digest]:
                        raise ValueError('Editor source changed after export admission. Retry the export.')
                    live[digest] -= 1
            config_version = metadata_versions[manager.config_path]
            self.pause_defaults = manager._load_pause_defaults()
            if _get_source_version(manager.config_path) != config_version:
                raise ValueError('Export pause settings changed during capture. Retry the export.')
        self.paths = {}
        for chunk in self.chunks:
            ensure_audio_export_active(cancel_check)
            path = chunk.get('audio_path')
            if path:
                try:
                    self.paths[path] = manager.get_chunk_audio_path(path)
                except ValueError:
                    # Existing loading reports these invalid/missing clips as skipped.
                    continue
        paths = set(self.paths.values()) | set(metadata_versions) | set(additional_sources)
        original = state.get('input_file_path')
        if isinstance(original, str) and original:
            paths.add(original)
        self.identities = {}
        for path in paths:
            ensure_audio_export_active(cancel_check)
            self.identities[path] = read_source_identity(path, cancel_check)
        for path, version in metadata_versions.items():
            identity = self.identities[path]
            if (identity['version'] if identity is not None else None) != version:
                raise ValueError('Export source or pause settings changed before source capture. Retry the export.')

    def _get_path_contract(self):
        return {key: os.path.abspath(getattr(self.manager, key))
                for key in ('root_dir', 'chunks_path', 'script_path', 'config_path')}

    def verify_sources(self):
        """Potentially long reads belong outside the owner/book/chunk locks."""
        for path, identity in self.identities.items():
            ensure_audio_export_active(self.cancel_check)
            if read_source_identity(path, self.cancel_check) != identity:
                raise ValueError('Export source changed during assembly or validation. Retry the export.')

    @contextmanager
    def ensure_publication(self, output_path, report):
        """Admit the same book/rows/sources and exact private stage for replacement."""
        with self.ensure_set_publication([(output_path, report)]):
            yield

    @contextmanager
    def ensure_set_publication(self, outputs):
        """Admit a prechecked staged/retained set in one publication transaction."""
        with self._ensure_set_admission(outputs,
                lambda remaining: _ensure_export_book(self.root, timeout=remaining,
                                                      cancel_check=self.cancel_check)):
            yield

    @contextmanager
    def ensure_publication_transaction(self, outputs):
        """Keep owner/book through short set publication and any rollback.

        Acquire owner before trying the book, releasing it before any retry.
        Each phase tries chunk locks once; contention aborts for rollback rather
        than waiting under the retained owner/book. No hashes/providers/progress
        belong here. Cancellation predicates may query the already held owner.
        """
        ensure_audio_export_active(self.cancel_check)
        with _ensure_export_book(self.root, cancel_check=self.cancel_check):
            active = True
            @contextmanager
            def admit():
                if not active:
                    raise ValueError('Export publication transaction has ended.')
                with ExitStack() as stack:
                    if not self.manager._chunks_lock.acquire(blocking=False):
                        raise TimeoutError('Editor rows are busy; retry chapter export.')
                    stack.callback(self.manager._chunks_lock.release)
                    stack.enter_context(file_lock(self.manager.chunks_path, timeout=0))
                    self._require_publication_current(tuple(outputs.items()))
                    yield
            try:
                yield admit
            finally:
                active = False

    @contextmanager
    def _ensure_set_admission(self, outputs, admit_book):
        manager = self.manager
        outputs = tuple(outputs)
        # Generic callbacks may themselves acquire the exact-owner mutex. Never
        # invoke them while holding owner admission or either chunk lock.
        deadline = time.monotonic() + 10
        while True:
            ensure_audio_export_active(self.cancel_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Could not admit export publication to the current Editor rows.')
            with ExitStack() as stack:
                stack.enter_context(admit_book(remaining))
                admitted = manager._chunks_lock.acquire(blocking=False)
                if admitted:
                    stack.callback(manager._chunks_lock.release)
                    try:
                        stack.enter_context(file_lock(manager.chunks_path, timeout=0))
                    except TimeoutError:
                        admitted = False
                if not admitted:
                    # All locks leave the stack before waiting or cancellation.
                    pass
                else:
                    self._require_publication_current(outputs)
                    # Body exceptions, including TimeoutError, must never retry a
                    # replacement or a partially committed chapter transaction.
                    yield
                    return
            time.sleep(min(.01, max(0, deadline - time.monotonic())))

    def _require_publication_current(self, outputs):
        manager = self.manager
        if (self._get_path_contract() != self.path_contract
                or os.path.realpath(self.root) != self.real_root):
            raise ValueError('Export project paths changed before publication. Retry the export.')
        book = _get_book_identity(self.root)
        if book != self.book:
            raise ValueError('Book or Editor rows changed during export. Retry the export.')
        if self.expected_editor_revision is not None:
            rows_current = (get_export_editor_revision(get_snapshot_digest(book), _get_saved_rows(manager))
                            == self.expected_editor_revision)
        else:
            rows_current = _get_saved_rows_digest(manager) == self.saved_rows_digest
        if not rows_current:
            raise ValueError('Book or Editor rows changed during export. Retry the export.')
        for path, resolved in self.paths.items():
            if manager.get_chunk_audio_path(path) != resolved:
                raise ValueError('Export audio source changed during assembly. Retry the export.')
        for path, identity in self.identities.items():
            if _get_source_version(path) != (identity['version'] if identity is not None else None):
                raise ValueError('Export source changed before publication. Retry the export.')
        for output_path, report in outputs:
            if get_export_file_version(output_path) != report['file_version']:
                raise ValueError('Finished export changed before publication. Retry the export.')
