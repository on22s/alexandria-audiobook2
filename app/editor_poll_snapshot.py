"""Bounded comparison evidence for editor polling; never persists book data."""
from collections import OrderedDict
import hashlib
import json
import threading
from chunk_status_journal import FIELDS


def get_snapshot_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class EditorPollSnapshots:
    def __init__(self):
        self._lock = threading.Lock()
        self._book = None
        self._history = OrderedDict()

    def ensure_snapshot(self, chunks, book_identity, previous_revision=None):
        """Retain at most eight fingerprint maps; return only changed rows."""
        structure = [(row.get('uid'), row.get('id')) for row in chunks]
        row_hashes = [get_snapshot_digest(row) for row in chunks]
        content = get_snapshot_digest([{key: value for key, value in row.items()
            if key not in FIELDS} for row in chunks])
        book = get_snapshot_digest(book_identity)
        revision = get_snapshot_digest([book, structure, row_hashes])
        with self._lock:
            if book != self._book:
                self._history.clear()
                self._book = book
            previous = self._history.get(previous_revision)
            full = previous is None or previous[0] != structure or previous[1] != content
            changed = chunks if full else [row for index, row in enumerate(chunks)
                if row_hashes[index] != previous[2][index]]
            self._history[revision] = (structure, content, row_hashes)
            self._history.move_to_end(revision)
            while len(self._history) > 8:
                self._history.popitem(last=False)
        return {'revision': revision, 'full': full, 'changed_ids': [row['id'] for row in changed],
            'running_count': sum(row.get('status') == 'generating' for row in chunks),
            'total': len(chunks), 'chunks': changed}
