"""Ordered source-word authority for an explicit script-scope selection."""
import hashlib
from itertools import chain, zip_longest
import os
from pathlib import Path
from text_diff import get_comparison_words
from merge_integrity import get_source_preparation, get_prepared_source
from project_scope_files import capture_scope_file
from source_encoding import get_decoded_source_text, get_normalized_source_newlines
from audio_take_history import _check_cancelled


def get_scope_source_snapshot(state, *, cancel_check=None):
    """Prepare one bounded source snapshot without rereading or writing it."""
    input_path, strip_front_matter = get_source_preparation(state)
    path = Path(os.path.abspath(input_path))
    proof, raw = capture_scope_file(path.parent, path.name, include_bytes=True,
                                    cancel_check=cancel_check)
    if not proof['exists']:
        raise ValueError('The selected original source is missing.')
    source, encoding = get_decoded_source_text(raw)
    source = get_normalized_source_newlines(source)
    if strip_front_matter is not None:
        source = get_prepared_source(source, strip_front_matter)
    _check_cancelled(cancel_check)
    return {**proof, 'absolute_path': str(path), 'encoding': encoding,
            'strip_front_matter': strip_front_matter,
            'prepared_text_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest()}, source


def get_source_scope(source_text, script, start_entry):
    """Bind a suffix to the current prepared source using AA2's word policy.

    This preserves source text and entries. It is a word-range proof, not a
    punctuation, generation-origin, audio or filesystem snapshot certificate.
    The outer planner must also bind raw file bytes and preparation settings.
    """
    if (not isinstance(source_text, str) or not source_text.strip()
            or not isinstance(script, list) or not script
            or any(not isinstance(row, dict) or not isinstance(row.get('text'), str) for row in script)
            or type(start_entry) is not int or not 0 < start_entry < len(script)):
        raise ValueError('A current source and explicit script boundary are required.')
    source_words = get_comparison_words(source_text)
    if not source_words:
        raise ValueError('The selected source has no verifiable words.')
    script_words = chain.from_iterable(get_comparison_words(row['text']) for row in script)
    missing = object()
    for index, (source, authored) in enumerate(zip_longest(source_words, script_words, fillvalue=missing)):
        if source != authored:
            raise ValueError(f'Script/source ordered words differ at word {index}.')
    start = sum(len(get_comparison_words(row['text'])) for row in script[:start_entry])
    if not 0 < start < len(source_words):
        raise ValueError('The selected boundary does not divide verifiable source words.')
    return {'version': 1, 'word_policy': 'AA2_text_diff_v1',
            'source_text_sha256': hashlib.sha256(source_text.encode('utf-8')).hexdigest(),
            'source_word_count': len(source_words), 'retained_start_word': start,
            'retained_end_word': len(source_words), 'script_start_entry': start_entry}
