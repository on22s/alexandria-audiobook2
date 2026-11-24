"""Round-5 repairs for #2257: snapshot authentication, checkpoint authority,
linear lineage, and force/reuse merge-layout agreement."""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import review_script as rev
from completed_review_receipt import get_review_progress_paths
from tests import test_review_round4_independent as h
from utils import atomic_json_write, file_lock


def keyrow(text, layout, speaker='NARRATOR', instruct='Reviewed.'):
    values = {'text': text, 'zeta': 'v', 'speaker': speaker, 'instruct': instruct}
    return {key: values[key] for key in layout}


LAYOUTS = [['text', 'zeta', 'speaker', 'instruct'],
           ['speaker', 'zeta', 'text', 'instruct'],
           ['instruct', 'zeta', 'speaker', 'text']]


class SnapshotWindow(unittest.TestCase):
    setUp = h.Independent.setUp
    invoke = h.Independent.invoke
    seed = h.Independent.seed
    completed = h.Independent.completed

    def _run_edit(self, mutate):
        self.seed()
        original = rev.get_coherent_output_snapshot
        fired, edited = [], []

        def edit_then_return(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            if not fired:
                with file_lock(str(self.out)):
                    mutate(json.loads(self.out.read_bytes()))
                    edited.append(self.out.read_bytes())
                fired.append(True)
            return snapshot

        with patch.object(rev, 'get_coherent_output_snapshot', side_effect=edit_then_return):
            count, error, success = self.invoke([], 'window')
        self.assertEqual([True], fired)
        return count, error, success, edited[0]

    def test_out_of_place_value_edit_in_snapshot_window_refused(self):
        def mutate(doc):
            doc[0]['instruct'] = 'Editor wrote this after the snapshot.'
            atomic_json_write(doc, str(self.out), allow_nonatomic_fallback=False)
        count, error, success, edited = self._run_edit(mutate)
        self.assertEqual(0, count)
        self.assertIsInstance(error, ValueError)
        self.assertFalse(success)
        self.assertEqual(edited, self.out.read_bytes())
        self.assertIn('Editor wrote this', self.out.read_text())
        self.assertNotIn('Task review completed successfully.',
                         (self.root / 'window.log').read_text())

    def test_out_of_place_key_order_edit_in_snapshot_window_refused(self):
        def mutate(doc):
            editor = [dict(reversed(list(item.items()))) for item in doc]
            atomic_json_write(editor, str(self.out), allow_nonatomic_fallback=False)
        count, error, success, edited = self._run_edit(mutate)
        self.assertEqual(0, count)
        self.assertIsInstance(error, ValueError)
        self.assertFalse(success)
        self.assertEqual(edited, self.out.read_bytes())
        self.assertNotIn('Task review completed successfully.',
                         (self.root / 'window.log').read_text())

    def test_checkpoint_that_appears_after_match_is_refused(self):
        self.seed()
        receipt = Path(get_review_progress_paths(str(self.out))[1])
        fingerprint = json.loads(receipt.read_bytes())['facts']['forward']['fingerprint']
        checkpoint = Path(get_review_progress_paths(str(self.out))[0])
        before = self.out.read_bytes()

        def write_checkpoint():
            checkpoint.write_text(json.dumps({
                'execution_sha256': fingerprint,
                'completed_batches': 0,
                'all_corrected': [],
            }), encoding='utf-8')

        count, error, success = self.invoke([], 'checkpoint', on_match=write_checkpoint)
        self.assertEqual(0, count)
        self.assertIsInstance(error, ValueError)
        self.assertFalse(success)
        self.assertEqual(before, self.out.read_bytes())


class LineageScale(unittest.TestCase):
    def _rows(self, count):
        return [{'text': f'word{index} follows a distinct clause', 'speaker': 'MIRA',
                 'instruct': ''} for index in range(count)]

    def _timed(self, count):
        source = self._rows(count)
        output = [dict(item) for item in source]
        start = time.perf_counter()
        templates = rev.get_review_source_order_templates(output, source)
        elapsed = time.perf_counter() - start
        self.assertIsNotNone(templates)
        self.assertEqual([list(item) for item in source],
                         [list(template) for template in templates])
        return elapsed

    def test_lineage_scan_is_subquadratic(self):
        small = self._timed(400)
        large = self._timed(1600)
        # Quadratic rescanning grows ~16x for 4x rows; linear grows ~4x.
        self.assertLess(large, 6 * small + 0.10,
                        f'lineage scan looks quadratic: {small:.3f}s -> {large:.3f}s')


class MergeLayout(unittest.TestCase):
    setUp = h.Independent.setUp
    invoke = h.Independent.invoke
    completed = h.Independent.completed

    def test_character_merge_takes_last_parent_not_a_middle_parent(self):
        source = [keyrow('Alpha.', LAYOUTS[0], 'MIRA'),
                  keyrow('Bravo.', LAYOUTS[1], 'MIRA'),
                  keyrow('Charlie.', LAYOUTS[2], 'MIRA')]
        # A character merge whose output happens to carry the first parent's
        # layout must still adopt the binder's last-parent layout.
        output = [keyrow('Alpha. Bravo. Charlie.', LAYOUTS[0], 'MIRA')]
        self.assertEqual([LAYOUTS[2]], rev.get_review_source_order_templates(output, source))

    def test_narrator_merge_keeps_its_own_first_row_layout(self):
        entries = [keyrow('Alpha.', LAYOUTS[0]), keyrow('Bravo.', LAYOUTS[1])]
        merged, count = rev.merge_consecutive_narrators(entries)
        self.assertEqual(1, count)
        self.assertEqual(LAYOUTS[0], list(merged[0]))

    def test_narrator_run_starting_with_model_merge_agrees_on_force_and_reuse(self):
        source = h.rows(['Ash. Bell.', 'Cedar.', 'Dew.'], 'NARRATOR')
        h.dump(self.src, source)
        answers = [[h.reply('Ash. Bell. Cedar.', 'NARRATOR')],
                   [h.reply(source[2]['text'], 'NARRATOR')]]
        first = self.completed(answers, 'fresh', count=2)
        # The run starts with a model merge, so its authentic layout is the
        # middle parent's (not the first or last). Reuse must preserve it and
        # --force-review must reproduce it, not fall back to the last parent.
        self.assertEqual([list(source[1])], [list(item) for item in json.loads(first)])
        self.assertEqual(first, self.completed([], 'reuse', count=0))
        self.assertEqual(first, self.completed(answers, 'force', '--force-review', count=2))


class _CountedToken:
    """A token that records how often the lineage query hashes/compares it."""

    __slots__ = ('text',)
    hashes = 0
    equals = 0

    def __init__(self, text):
        self.text = text

    def __hash__(self):
        type(self).hashes += 1
        return hash(self.text)

    def __eq__(self, other):
        type(self).equals += 1
        return isinstance(other, _CountedToken) and self.text == other.text


class _CountedText:
    def __init__(self, text):
        self.text = text

    def split(self):
        return [_CountedToken(part) for part in self.text.split()]


class _CountedLayoutRow(dict):
    """A source row that records how often its key order is walked."""

    iterations = 0

    def __iter__(self):
        type(self).iterations += 1
        return super().__iter__()


def counted_normalize(real):
    def counted(text):
        return _CountedText(real(text))
    return counted


def counted_hash_and_equals(output, source):
    _CountedToken.hashes = 0
    _CountedToken.equals = 0
    with patch.object(rev, 'normalize_text', counted_normalize(rev.normalize_text)):
        templates = rev.get_review_source_order_templates(output, source)
    return _CountedToken.hashes, _CountedToken.equals, templates


class LineageOperationScaling(unittest.TestCase):
    """#2257 round 7: merge/split/duplicate work must grow subquadratically.

    These assert operation counts, not wall-clock time, so they are
    deterministic: on the round-6 candidate each shape grew ~4x per doubling.
    Both token hashes and token comparisons are measured so a repair cannot
    move the cost from equality to hashing and pass unnoticed.
    """

    def _count(self, output, source):
        return counted_hash_and_equals(output, source)

    def _grows_subquadratically(self, shape):
        small_hashes, small, small_templates = self._count(*shape(400))
        large_hashes, large, large_templates = self._count(*shape(800))
        # Linear work at most doubles; the candidate's growing list
        # concatenations and growing-prefix comparisons quadrupled.
        self.assertLessEqual(
            large, 3 * small + 200,
            f'comparison count grew superlinearly: {small} -> {large} for 2x input')
        self.assertLessEqual(
            large_hashes, 3 * small_hashes + 200,
            f'hash count grew superlinearly: {small_hashes} -> {large_hashes} for 2x input')
        return small_templates, large_templates

    def test_split_one_source_row_into_many_output_rows(self):
        def shape(size):
            text = ' '.join(f'w{index % 5}' for index in range(size))
            output = [keyrow(f'w{index % 5}', LAYOUTS[0], 'MIRA')
                      for index in range(size)]
            return output, [keyrow(text, LAYOUTS[1], 'MIRA')]
        small, large = self._grows_subquadratically(shape)
        self.assertIsNotNone(small)
        self.assertIsNotNone(large)

    def test_merge_many_distinct_rows_into_one_output_row(self):
        def shape(size):
            source = [keyrow(f'u{index}', LAYOUTS[index % 3], 'MIRA')
                      for index in range(size)]
            output = [keyrow(' '.join(f'u{index}' for index in range(size)),
                             LAYOUTS[0], 'MIRA')]
            return output, source
        small, large = self._grows_subquadratically(shape)
        self.assertIsNotNone(small)
        self.assertIsNotNone(large)

    def test_duplicate_surplus_candidate_extension(self):
        def shape(size):
            # Identical text and layout keep every candidate extension alive
            # (differing layouts would fail closed after two candidates). The
            # surplus duplicate rows later make the overall derivation ambiguous,
            # so templates are None; the operation count is the subject here.
            source = [keyrow('yes no', LAYOUTS[0], 'MIRA')
                      for index in range(size)]
            output = [keyrow('yes no ' * (size // 2), LAYOUTS[0], 'MIRA')]
            return output, source
        self._grows_subquadratically(shape)


class LineageDuplicateLayoutScaling(unittest.TestCase):
    """#2257 round 8: repeated identical rows cost one layout probe each.

    The old exact-length branch re-derived every duplicate row's layout on
    every query, so R identical one-to-one rows walked ~R^2 key orders. This
    counter follows the source row's ``__iter__`` (what ``list(row)`` uses),
    which is deterministic and invisible to token-operation counters.
    """

    def _layout_work(self, count):
        source = [_CountedLayoutRow(keyrow('yes no', LAYOUTS[0], 'MIRA'))
                  for _ in range(count)]
        output = [keyrow('yes no', LAYOUTS[0], 'MIRA') for _ in range(count)]
        _CountedLayoutRow.iterations = 0
        templates = rev.get_review_source_order_templates(output, source)
        iterations = _CountedLayoutRow.iterations
        self.assertIsNotNone(templates)
        self.assertEqual([list(row) for row in source],
                         [list(template) for template in templates])
        return iterations

    def test_identical_rows_derive_layouts_linearly(self):
        small = self._layout_work(400)
        large = self._layout_work(800)
        self.assertLessEqual(
            large, 3 * small + 50,
            f'layout derivations grew superlinearly: {small} -> {large} for 2x rows')


class LineageDistinctLengthScaling(unittest.TestCase):
    """#2257 round 8: prefix lookup hashes the target once, not per length.

    Counting token ``__hash__`` (not only ``__eq__``) exposes the round-7
    per-length ``target[:length]`` slicing. Rows of lengths 1..D make the source
    token count grow quadratically with D while a linear query only hashes each
    target token once, so the old code's hash count grew ~8x per 2x D.
    """

    def _hash_count(self, output, source):
        hashes, _equals, templates = counted_hash_and_equals(output, source)
        return hashes, templates

    def test_one_to_one_distinct_lengths_hash_linearly(self):
        def shape(depth):
            source = [keyrow(' '.join(f't{index}_{part}' for part in range(index)),
                             LAYOUTS[index % 3], 'MIRA')
                      for index in range(1, depth + 1)]
            return [dict(row) for row in source], source
        small, small_templates = self._hash_count(*shape(128))
        large, large_templates = self._hash_count(*shape(256))
        self.assertIsNotNone(small_templates)
        self.assertIsNotNone(large_templates)
        self.assertLessEqual(
            large, 5 * small + 2000,
            f'prefix hash count grew superlinearly: {small} -> {large} for 2x lengths')

    def test_long_rows_with_many_distinct_lengths_hash_linearly(self):
        def shape(width):
            source = [keyrow(' '.join(f's{length}_{part}' for part in range(length)),
                             LAYOUTS[length % 3], 'MIRA')
                      for length in range(1, width)]
            source.extend(
                keyrow(' '.join(f'm{index}_{part}' for part in range(width)),
                       LAYOUTS[0], 'MIRA')
                for index in range(200))
            return [dict(row) for row in source], source
        small, small_templates = self._hash_count(*shape(100))
        large, large_templates = self._hash_count(*shape(200))
        self.assertIsNotNone(small_templates)
        self.assertIsNotNone(large_templates)
        self.assertLessEqual(
            large, 3 * small + 1000,
            f'long-row prefix hash count grew superlinearly: {small} -> {large}')


class LineageAuditScaling(unittest.TestCase):
    """Retained long-token and row-count coverage from round 6."""

    def test_40000_token_entry_completes_within_generous_bound(self):
        text = ' '.join(f'word{index}' for index in range(40000))
        source = [keyrow(text, LAYOUTS[0], 'MIRA')]
        output = [keyrow(text, LAYOUTS[1], 'MIRA')]
        start = time.perf_counter()
        templates = rev.get_review_source_order_templates(output, source)
        elapsed = time.perf_counter() - start
        self.assertEqual([LAYOUTS[0]], [list(item) for item in templates])
        # 40k tokens took ~6.9s with prefix-copy enumeration and ~0.04s now;
        # 2s keeps generous headroom while still failing the former.
        self.assertLess(elapsed, 2.0, f'40k-token lineage took {elapsed:.3f}s')

    def test_2000_repeated_identical_rows_merge_to_last_parent(self):
        source = [keyrow('yes no', LAYOUTS[index % 3], 'MIRA')
                  for index in range(2000)]
        output = [keyrow('yes no ' * 2000, LAYOUTS[0], 'MIRA')]
        start = time.perf_counter()
        templates = rev.get_review_source_order_templates(output, source)
        elapsed = time.perf_counter() - start
        # A character merge takes the last parent's layout, never a middle one.
        self.assertEqual([LAYOUTS[1999 % 3]], [list(item) for item in templates])
        self.assertLess(elapsed, 5.0, f'2000-row merge took {elapsed:.3f}s')


class LineageRepeatedMergeScaling(unittest.TestCase):
    """#2257 round 10: repeated identical multi-row merges verify each group once.

    R identical two-token source rows with R//2 identical four-token merges made
    the round-9 tree rescan the same candidate region and rebuild the Z
    verification for every output row, so source key-order walks grew ~R^2/4.
    One exact verification per distinct target makes the work linear; the
    counter follows the source row's ``__iter__`` (what ``list(row)`` uses).
    """

    def _iterations(self, count):
        source = [_CountedLayoutRow(keyrow('yes no', LAYOUTS[0], 'MIRA'))
                  for _ in range(count)]
        output = [keyrow('yes no yes no', LAYOUTS[0], 'MIRA')
                  for _ in range(count // 2)]
        before = ([dict(row) for row in output],
                  [dict(row) for row in source])
        _CountedLayoutRow.iterations = 0
        templates = rev.get_review_source_order_templates(output, source)
        iterations = _CountedLayoutRow.iterations
        self.assertIsNotNone(templates)
        self.assertEqual([LAYOUTS[0]] * len(output),
                         [list(template) for template in templates])
        self.assertEqual(before, ([dict(row) for row in output],
                                  [dict(row) for row in source]))
        return iterations

    def test_repeated_identical_merges_verify_each_group_once(self):
        small = self._iterations(500)
        large = self._iterations(1000)
        self.assertLessEqual(
            large, 3 * small + 50,
            f'repeated merge key-order walks grew superlinearly: {small} -> {large}')


class LineageInterleavedRepeatScaling(unittest.TestCase):
    """#2257 round 11: ordinary rows cannot evict a repeated merge analysis.

    A repeated identical two-row merge separated by four ordinary outputs on a
    variable-length source made the round-10 four-entry recency window forget the
    merge target, so every occurrence re-verified the same region. The retained
    per-call analysis now survives any number of intervening single-use rows.
    """

    interleave = 4

    def _shape(self, cycles):
        source, output = [], []
        for index in range(cycles):
            source.append(_CountedLayoutRow(keyrow('a1 a2', LAYOUTS[0], 'MIRA')))
            source.append(_CountedLayoutRow(keyrow('a3 a4', LAYOUTS[0], 'MIRA')))
            ordinary = []
            for offset in range(self.interleave):
                text = f'u{index}_{offset} w{offset}'
                if offset % 2:
                    text += ' extra'
                ordinary.append(text)
                source.append(_CountedLayoutRow(keyrow(text, LAYOUTS[0], 'MIRA')))
            output.append(keyrow('a1 a2 a3 a4', LAYOUTS[0], 'MIRA'))
            output += [keyrow(text, LAYOUTS[0], 'MIRA') for text in ordinary]
        return output, source

    def _iterations(self, cycles):
        output, source = self._shape(cycles)
        before = ([dict(row) for row in output], [dict(row) for row in source])
        _CountedLayoutRow.iterations = 0
        templates = rev.get_review_source_order_templates(output, source)
        iterations = _CountedLayoutRow.iterations
        self.assertIsNotNone(templates)
        self.assertEqual([LAYOUTS[0]] * len(output),
                         [list(template) for template in templates])
        self.assertEqual(before, ([dict(row) for row in output],
                                  [dict(row) for row in source]))
        return iterations

    def test_interleaved_repeat_keeps_its_analysis(self):
        small = self._iterations(400)
        large = self._iterations(800)
        self.assertLessEqual(
            large, 3 * small + 50,
            f'interleaved repeated merge key-order walks grew superlinearly: '
            f'{small} -> {large}')

    def test_production_resume_wrapper_keeps_interleaved_repeat_linear(self):
        def iterations(cycles):
            output, source = self._shape(cycles)
            _CountedLayoutRow.iterations = 0
            bound = rev.get_review_rows_bound_to_source_order(
                output, source, len(source))
            count = _CountedLayoutRow.iterations
            self.assertEqual([list(row) for row in output],
                             [list(row) for row in bound])
            return count
        small = iterations(400)
        large = iterations(800)
        self.assertLessEqual(
            large, 3 * small + 50,
            f'resume wrapper key-order walks grew superlinearly: {small} -> {large}')

    def test_interleaved_repeat_conflicting_layouts_fail_closed(self):
        output, source = self._shape(8)
        source[1] = _CountedLayoutRow(keyrow('a3 a4', LAYOUTS[1], 'MIRA'))
        self.assertIsNone(
            rev.get_review_source_order_templates(output, source))


class LineageAlternatingTargetScaling(unittest.TestCase):
    """#2257 round 11: more alternating repeated targets than a fixed window.

    Five distinct repeated two-row merges cycling made the round-10 four-entry
    window evict the fifth target every cycle. Retained per-call analyses keep
    every repeated target's summary for the whole call.
    """

    targets = 5

    def _shape(self, cycles):
        pairs = [(f'p{index} q{index}', f'r{index} s{index}')
                 for index in range(self.targets)]
        source, output = [], []
        for _ in range(cycles):
            for first, second in pairs:
                source.append(_CountedLayoutRow(keyrow(first, LAYOUTS[0], 'MIRA')))
                source.append(_CountedLayoutRow(keyrow(second, LAYOUTS[0], 'MIRA')))
            for first, second in pairs:
                output.append(keyrow(f'{first} {second}', LAYOUTS[0], 'MIRA'))
        return output, source

    def _iterations(self, cycles):
        output, source = self._shape(cycles)
        before = ([dict(row) for row in output], [dict(row) for row in source])
        _CountedLayoutRow.iterations = 0
        templates = rev.get_review_source_order_templates(output, source)
        iterations = _CountedLayoutRow.iterations
        self.assertIsNotNone(templates)
        self.assertEqual([LAYOUTS[0]] * len(output),
                         [list(template) for template in templates])
        self.assertEqual(before, ([dict(row) for row in output],
                                  [dict(row) for row in source]))
        return iterations

    def test_more_alternating_targets_than_a_fixed_window_stay_linear(self):
        small = self._iterations(200)
        large = self._iterations(400)
        self.assertLessEqual(
            large, 3 * small + 50,
            f'alternating repeated merge key-order walks grew superlinearly: '
            f'{small} -> {large}')


class LineageTargetAnalysisCache(unittest.TestCase):
    """#2257 round 10: the per-call target cache keeps exact lineage semantics."""

    def _templates(self, output, source, limit=None):
        return rev.get_review_source_order_templates(output, source, limit)

    def _layouts(self, output, source, limit=None):
        templates = self._templates(output, source, limit)
        return None if templates is None else [list(item) for item in templates]

    def test_repeated_identical_targets_take_last_parent(self):
        source = [keyrow('yes no', LAYOUTS[2], 'MIRA') for _ in range(6)]
        output = [keyrow('yes no yes no', LAYOUTS[0], 'MIRA') for _ in range(3)]
        self.assertEqual([LAYOUTS[2]] * 3, self._layouts(output, source))

    def test_repeated_identical_targets_with_conflicting_layouts_fail_closed(self):
        source = [keyrow('yes no', LAYOUTS[index % 3], 'MIRA')
                  for index in range(6)]
        output = [keyrow('yes no yes no', LAYOUTS[0], 'MIRA') for _ in range(3)]
        self.assertIsNone(self._layouts(output, source))

    def test_alternating_targets_keep_each_layout(self):
        source, output = [], []
        for _ in range(3):
            source += [keyrow('a1 a2', LAYOUTS[0], 'MIRA'),
                       keyrow('a3 a4', LAYOUTS[0], 'MIRA'),
                       keyrow('b1 b2', LAYOUTS[1], 'MIRA'),
                       keyrow('b3 b4', LAYOUTS[1], 'MIRA')]
            output += [keyrow('a1 a2 a3 a4', LAYOUTS[0], 'MIRA'),
                       keyrow('b1 b2 b3 b4', LAYOUTS[1], 'MIRA')]
        self.assertEqual([LAYOUTS[0], LAYOUTS[1]] * 3,
                         self._layouts(output, source))

    def test_source_start_filters_reachable_layouts(self):
        # A merge before the first target query contains an earlier group of the
        # target in another layout. The first query starts after that merge, and
        # the later repeat must not resurrect the earlier layout.
        source = [keyrow('p q', LAYOUTS[0], 'MIRA'),
                  keyrow('r s', LAYOUTS[1], 'MIRA'),
                  keyrow('t u', LAYOUTS[2], 'MIRA'),
                  keyrow('v w', LAYOUTS[2], 'MIRA'),
                  keyrow('p q', LAYOUTS[0], 'MIRA'),
                  keyrow('r s', LAYOUTS[0], 'MIRA'),
                  keyrow('p q', LAYOUTS[0], 'MIRA'),
                  keyrow('r s', LAYOUTS[0], 'MIRA')]
        output = [keyrow('p q r s t u v w', LAYOUTS[0], 'MIRA'),
                  keyrow('p q r s', LAYOUTS[0], 'MIRA'),
                  keyrow('p q r s', LAYOUTS[0], 'MIRA')]
        self.assertEqual([LAYOUTS[2], LAYOUTS[0], LAYOUTS[0]],
                         self._layouts(output, source))

    def test_source_limit_boundaries_are_exact(self):
        source = [keyrow('yes no', LAYOUTS[0], 'MIRA') for _ in range(6)]
        output = [keyrow('yes no yes no', LAYOUTS[0], 'MIRA') for _ in range(3)]
        for limit in (-1, 0, 1, 2, 5):
            self.assertIsNone(self._layouts(output, source, limit))
        for limit in (6, 7, 99):
            self.assertEqual([LAYOUTS[0]] * 3,
                             self._layouts(output, source, limit))

    def test_partial_retention_keeps_consumed_prefix(self):
        source = [keyrow('yes no', LAYOUTS[0], 'MIRA') for _ in range(4)]
        source.append(keyrow('tail', LAYOUTS[2], 'MIRA'))
        output = [keyrow('yes no yes no', LAYOUTS[0], 'MIRA') for _ in range(2)]
        self.assertEqual([LAYOUTS[0], LAYOUTS[0]],
                         self._layouts(output, source))

    def test_character_merge_takes_last_parent_across_repeats(self):
        # Two reachable groups share the same last-parent layout but differ in
        # their first-parent layout: last-parent keeps the derivation
        # unambiguous, first-parent would fail closed.
        source = [keyrow('a b', LAYOUTS[1], 'MIRA'),
                  keyrow('c d', LAYOUTS[0], 'MIRA'),
                  keyrow('a b', LAYOUTS[2], 'MIRA'),
                  keyrow('c d', LAYOUTS[0], 'MIRA')]
        output = [keyrow('a b c d', LAYOUTS[1], 'MIRA'),
                  keyrow('a b c d', LAYOUTS[1], 'MIRA')]
        self.assertEqual([LAYOUTS[0]] * 2, self._layouts(output, source))

    def test_narrator_run_keeps_first_row_layout_across_repeats(self):
        source = [keyrow('a b', LAYOUTS[1], 'NARRATOR'),
                  keyrow('c d', LAYOUTS[2], 'NARRATOR')] * 3
        output = [keyrow('a b c d', LAYOUTS[1], 'NARRATOR') for _ in range(3)]
        self.assertEqual([LAYOUTS[1]] * 3, self._layouts(output, source))

    def test_unicode_punctuation_and_empty_rows_merge_correctly(self):
        source = [keyrow('é ñ', LAYOUTS[0], 'MIRA'),
                  keyrow('中文 字', LAYOUTS[1], 'MIRA'),
                  keyrow('... ;', LAYOUTS[2], 'MIRA'),
                  keyrow('é ñ', LAYOUTS[0], 'MIRA'),
                  keyrow('中文 字', LAYOUTS[1], 'MIRA')]
        output = [keyrow('é ñ 中文 字', LAYOUTS[0], 'MIRA'),
                  keyrow('é ñ 中文 字', LAYOUTS[0], 'MIRA')]
        self.assertEqual([LAYOUTS[1], LAYOUTS[1]], self._layouts(output, source))

    def test_calls_do_not_share_source_identity_or_limit(self):
        first = self._templates(
            [keyrow('x', LAYOUTS[1], 'MIRA')],
            [keyrow('x', LAYOUTS[1], 'MIRA')])
        second = self._templates(
            [keyrow('x', LAYOUTS[0], 'MIRA')],
            [keyrow('x', LAYOUTS[0], 'MIRA')])
        self.assertEqual([LAYOUTS[1]], [list(item) for item in first])
        self.assertEqual([LAYOUTS[0]], [list(item) for item in second])
        source = [keyrow('x', LAYOUTS[1], 'MIRA'),
                  keyrow('x', LAYOUTS[0], 'MIRA')]
        output = [keyrow('x', LAYOUTS[0], 'MIRA')]
        self.assertEqual([LAYOUTS[1]], self._layouts(output, source, 1))
        # With the whole source the same output is ambiguous; a cache that
        # leaked across the two calls would wrongly answer the earlier one.
        self.assertIsNone(self._layouts(output, source))

    def test_calls_do_not_mask_a_later_ambiguous_source(self):
        # The first call verifies a two-row merge twice, so the second (later
        # start) builds an amortized summary. A later call reaches the same
        # target with two reachable last-parent layouts: it is ambiguous there
        # and must fail closed, not reuse the earlier call's answer.
        target = 'p q r s'
        prior = self._templates(
            [keyrow(target, LAYOUTS[1], 'MIRA') for _ in range(2)],
            [keyrow('p q', LAYOUTS[1], 'MIRA'),
             keyrow('r s', LAYOUTS[1], 'MIRA')] * 2)
        self.assertEqual([LAYOUTS[1]] * 2, [list(item) for item in prior])
        source = [keyrow('u1 u2', LAYOUTS[0], 'MIRA'),
                  keyrow('v1 v2', LAYOUTS[0], 'MIRA'),
                  keyrow('p q', LAYOUTS[1], 'MIRA'),
                  keyrow('r s', LAYOUTS[1], 'MIRA'),
                  keyrow('p q', LAYOUTS[0], 'MIRA'),
                  keyrow('r s', LAYOUTS[2], 'MIRA')]
        output = [keyrow('u1 u2 v1 v2', LAYOUTS[0], 'MIRA'),
                  keyrow(target, LAYOUTS[1], 'MIRA')]
        self.assertIsNone(self._layouts(output, source))

    def test_target_cache_does_not_mask_another_targets_ambiguity(self):
        # 'c...' is ambiguous (two reachable last-parent layouts). The earlier,
        # unambiguous entries must not be reused for it.
        source = [keyrow('a1 a2', LAYOUTS[0], 'MIRA'),
                  keyrow('a3 a4', LAYOUTS[0], 'MIRA'),
                  keyrow('b1 b2', LAYOUTS[0], 'MIRA'),
                  keyrow('b3 b4', LAYOUTS[1], 'MIRA'),
                  keyrow('c1 c2', LAYOUTS[0], 'MIRA'),
                  keyrow('c3 c4', LAYOUTS[1], 'MIRA'),
                  keyrow('c1 c2', LAYOUTS[0], 'MIRA'),
                  keyrow('c3 c4', LAYOUTS[2], 'MIRA')]
        output = [keyrow('a1 a2 a3 a4', LAYOUTS[0], 'MIRA'),
                  keyrow('b1 b2 b3 b4', LAYOUTS[0], 'MIRA'),
                  keyrow('c1 c2 c3 c4', LAYOUTS[0], 'MIRA')]
        self.assertIsNone(self._layouts(output, source))


class _CollidingToken:
    """A token with a constant hash but exact equality, to force collisions."""

    __slots__ = ('text',)

    def __init__(self, text):
        self.text = text

    def __hash__(self):
        return 7

    def __eq__(self, other):
        return isinstance(other, _CollidingToken) and self.text == other.text

    def __ne__(self, other):
        return not self.__eq__(other)


class _CollidingText:
    def __init__(self, text):
        self.text = text

    def split(self):
        return [_CollidingToken(part) for part in self.text.split()]


class LineageCollidingTargetCache(unittest.TestCase):
    """#2257 round 10: hashing filters, exact equality decides cached groups."""

    def test_total_hash_collisions_do_not_corrupt_cached_groups(self):
        source = [keyrow('yes no', LAYOUTS[0], 'MIRA') for _ in range(6)]
        source += [keyrow('é ñ', LAYOUTS[2], 'MIRA'),
                   keyrow('... ;', LAYOUTS[1], 'MIRA')]
        output = [keyrow('yes no yes no', LAYOUTS[0], 'MIRA')
                  for _ in range(3)]
        output.append(keyrow('é ñ ... ;', LAYOUTS[2], 'MIRA'))
        real_normalize = rev.normalize_text
        try:
            rev.normalize_text = lambda text: _CollidingText(real_normalize(text))
            templates = rev.get_review_source_order_templates(output, source)
        finally:
            rev.normalize_text = real_normalize
        self.assertEqual([LAYOUTS[0]] * 3 + [LAYOUTS[2]],
                         [list(item) for item in templates])


if __name__ == '__main__':
    unittest.main()
