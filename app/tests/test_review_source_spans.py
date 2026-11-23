"""Review remaps verified source coordinates or leaves the original script intact."""
import copy
from contextlib import ExitStack, contextmanager, redirect_stdout
import hashlib
import io
from itertools import product
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from dialogue_spans import mark_entries, _normalize
from generate_script import LLMGenParams
import review_script as review
from script_selection_binding import make_script_selection_binding
from book_state_transaction import get_book_operation_token
from tests.test_pass_boundary_retry_regressions import client_for
from tests.test_review_source_binding import row


RAW = 'Rain fell. Please open the gate.\n\nTake the blue cup.'
SOURCE = [row('Rain fell. Please open the gate.', speaker='NARRATOR', spoken=False,
              source_span=[0, 32], provenance={'metadata': 'X'}, pause_after=700),
          row('Take the blue cup.', spoken=True, source_span=[34, 52],
              provenance={'metadata': 'Y'}, speaker_gender='female', pause_after=0)]
REPLY = [row('Rain fell.', speaker='NARRATOR'),
         row('Please open the gate. Take the blue cup.')]


def create_book(directory, *, saved=False, raw=RAW, source=None, upload_path=None):
    root = Path(directory)
    selected = upload_path or str(root / 'uploads' / 'fixture.txt')
    source_path = Path(selected)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(raw.encode('utf-8'))
    state = {'input_file_path': selected, 'active_book_id': 'fixture', 'book_generation': 1}
    (root / 'state.json').write_text(json.dumps(state), encoding='utf-8')
    script = root / 'scripts' / 'fixture.json' if saved else root / 'annotated_script.json'
    script.parent.mkdir(exist_ok=True)
    entries = copy.deepcopy(SOURCE if source is None else source)
    script_bytes = json.dumps(entries).encode('utf-8')
    script.write_bytes(script_bytes)
    binding = make_script_selection_binding(get_book_operation_token(directory),
        hashlib.sha256(source_path.read_bytes()).hexdigest(), script_bytes, source_path=selected)
    script.with_suffix('.selection_binding.json').write_text(json.dumps(binding), encoding='utf-8')
    return script, source_path, entries


class ReviewSourceSpanTests(unittest.TestCase):
    def test_heading_split_passes_when_real_repair_differs_outside_saved_span(self):
        heading, narration = 'Chapter 1: The Gate', 'Dawn broke.'
        covered = f'{heading}\n\n{narration}'
        # A damaged character in an uncovered tail makes the real preprocessing
        # produce two prepared variants (one still damaged, one repaired) that
        # both verify at the saved span. Capture must still succeed so the
        # production split can bind exact coordinates.
        raw = covered + '\nUnreviewed appendix with a bad \ufffd character.'
        source = [row(covered, speaker='NARRATOR', source_span=[0, len(covered)])]
        reply = [row(heading, speaker='NARRATOR'), row(narration, speaker='NARRATOR')]
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=source)
            verified = review.get_review_source_text(directory, str(script), script.read_bytes(), entries)
            self.assertIsNotNone(verified)
            bound, report = review.get_review_source_binding(entries, reply, source_text=verified)
            self.assertTrue(report['passed'], report)
            self.assert_mapped(bound, verified)
            self.assertEqual([[0, len(heading)], [len(heading) + 2, len(covered)]],
                             [entry['source_span'] for entry in bound])
            # Altered, reordered and omitted text stay refused even when capture works.
            for invalid in (reply[::-1], [reply[0], row('Dusk fell.')], [reply[0]]):
                self.assertFalse(review.get_review_source_binding(
                    entries, invalid, source_text=verified)[1]['passed'])

    def test_capture_keeps_ambiguity_when_covered_bytes_differ(self):
        covered = 'He said "go" now. Please open the gate.'
        raw = covered + '\nUnreviewed appendix.'
        source = [row(covered, speaker='NARRATOR', source_span=[0, len(covered)])]
        # Same-length quote swap normalizes identically and keeps coordinates,
        # but the raw bytes differ at the covered interval; the fix must not
        # collapse that ambiguity into a plausible-looking match.
        covered_variant = covered.replace('"', '“')
        variant = covered_variant + '\nRepaired appendix.'
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=source)
            with patch('generate_script.get_preprocessed_source', return_value=(raw, {})), \
                    patch('three_pass_generate.prepare_source_text', return_value=(variant, {})):
                self.assertIsNone(review.get_review_source_text(
                    directory, str(script), script.read_bytes(), entries))

    def test_reports_all_independent_split_errors_in_one_attempt(self):
        reply = [row(text) for text in ('Rain fell.', 'Please open the gate.',
                                       'Take the', 'blue cup.')]
        original = copy.deepcopy(SOURCE)
        client, calls = client_for([(reply, 'stop')])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            output = review.review_batch(client, 'fixture', SOURCE, 1, 1,
                                         LLMGenParams(), max_retries=0, source_text=None)
        self.assertIsNone(output)
        self.assertEqual(original, SOURCE)
        message = stdout.getvalue()
        self.assertEqual(1, message.count('entry=1; fragment=1; reason=source_unavailable'))
        self.assertEqual(1, message.count('entry=2; fragment=1; reason=source_unavailable'))
        self.assertLess(message.index('entry=1;'), message.index('entry=2;'))

    def test_saved_parent_cannot_launder_an_unproven_child_interval(self):
        raw = ('Manifesto.\nStone bell  rang.\n'
               'Original Web Novel Chapter - Complete.\n'
               'Original Translation by Tester.\n'
               'Manifesto.\nStone  bell rang.\n')
        parent = [row('Manifesto.', speaker='NARRATOR', source_span=[0, 10]),
                  row('Stone bell rang.', speaker='NARRATOR')]
        child = copy.deepcopy(parent)
        child[1]['source_span'] = [11, 28]
        with tempfile.TemporaryDirectory() as directory:
            saved, _, _ = create_book(directory, saved=True, raw=raw, source=parent)
            state_path = Path(directory, 'state.json')
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['input_file_path'] = str(saved)
            state_path.write_text(json.dumps(state))
            script = Path(directory, 'annotated_script.json')
            script.write_text(json.dumps(child))
            binding = make_script_selection_binding(get_book_operation_token(directory),
                hashlib.sha256(saved.read_bytes()).hexdigest(), script.read_bytes(),
                source_path=str(saved))
            script.with_suffix('.selection_binding.json').write_text(json.dumps(binding))
            # The parent proved only [0,10]; the two prepared candidates disagree
            # at [11,28], so the child's new interval must not be inherited.
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), child))
            original = script.read_bytes()
            reply = [row(text, speaker='NARRATOR') for text in
                     ('Manifesto.', 'Stone', 'bell rang.')]
            client, calls = client_for([(reply, 'stop')] * 3)
            with self.cli(directory, client):
                review.main()
            self.assertEqual(original, script.read_bytes())
            self.assertFalse(Path(str(script) + '.review_completed.json').exists())
            self.assertTrue(Path(str(script) + '.review_checkpoint.json').exists())

    def test_saved_parent_child_offsets_cannot_select_their_own_witness(self):
        raw = ('Manifesto.\nStone bell  rang.\n'
               'Original Web Novel Chapter - Complete.\n'
               'Original Translation by Tester.\n'
               'Manifesto.\nStone bell rang.\n')
        parent = [row('Manifesto.', speaker='NARRATOR', source_span=[0, 10]),
                  row('Stone bell rang.', speaker='NARRATOR')]
        child = copy.deepcopy(parent)
        child[1]['source_span'] = [11, 27]
        with tempfile.TemporaryDirectory() as directory:
            saved, _, _ = create_book(directory, saved=True, raw=raw, source=parent)
            state_path = Path(directory, 'state.json')
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['input_file_path'] = str(saved)
            state_path.write_text(json.dumps(state))
            script = Path(directory, 'annotated_script.json')
            script.write_text(json.dumps(child))
            binding = make_script_selection_binding(get_book_operation_token(directory),
                hashlib.sha256(saved.read_bytes()).hexdigest(), script.read_bytes(),
                source_path=str(saved))
            script.with_suffix('.selection_binding.json').write_text(json.dumps(binding))
            # One parent-verified variant disagrees at [11,27]. The child must
            # not drop it and publish offsets from the surviving witness alone;
            # the contradiction is a distinct signal, not ordinary "no source".
            with self.assertRaises(review.ReviewSourceContradiction):
                review.get_review_source_snapshot(
                    directory, str(script), script.read_bytes(), child)
            original = script.read_bytes()
            reply = [row(text, speaker='NARRATOR') for text in
                     ('Manifesto.', 'Stone', 'bell rang.')]
            client, calls = client_for([(reply, 'stop')] * 3)
            with self.assertRaisesRegex(ValueError, 'contradicts'):
                with self.cli(directory, client):
                    review.main()
            self.assertEqual(original, script.read_bytes())
            self.assertEqual(0, len(calls))
            self.assertFalse(Path(str(script) + '.review_completed.json').exists())
            self.assertFalse(Path(str(script) + '.review_checkpoint.json').exists())

    def test_historical_receipt_from_unproven_witness_is_not_reused(self):
        raw = ('Manifesto.\nStone bell  rang.\n'
               'Original Web Novel Chapter - Complete.\n'
               'Original Translation by Tester.\n'
               'Manifesto.\nStone  bell rang.\n')
        parent = [row('Manifesto.', speaker='NARRATOR', source_span=[0, 10]),
                  row('Stone bell rang.', speaker='NARRATOR')]
        child = [row('Manifesto.', speaker='NARRATOR', source_span=[0, 10]),
                 row('bell rang.', speaker='NARRATOR', source_span=[18, 28])]
        with tempfile.TemporaryDirectory() as directory:
            saved, _, _ = create_book(directory, saved=True, raw=raw, source=parent)
            state_path = Path(directory, 'state.json')
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['input_file_path'] = str(saved)
            state_path.write_text(json.dumps(state))
            script = Path(directory, 'annotated_script.json')
            script.write_text(json.dumps(child))
            binding = make_script_selection_binding(get_book_operation_token(directory),
                hashlib.sha256(saved.read_bytes()).hexdigest(), script.read_bytes(),
                source_path=str(saved))
            script.with_suffix('.selection_binding.json').write_text(json.dumps(binding))
            parent_snapshot = review.get_review_source_snapshot(
                directory, str(saved), saved.read_bytes(), parent)
            self.assertIsNotNone(parent_snapshot)
            laundered = next(text for text in parent_snapshot['candidates']
                             if _normalize(text[18:28]) == 'bell rang.')
            reply = [row(text, speaker='NARRATOR', instruct='Softly.')
                     for text in ('Manifesto.', 'bell rang.')]
            client, calls = client_for([(reply, 'stop')] * 4)
            # Recreate the historical receipt that a child-selected witness
            # would have published, then check the corrected code refuses it
            # even when the reply only edits annotations.
            with patch.object(review, 'get_review_source_snapshot',
                              return_value={**parent_snapshot, 'text': laundered}):
                with self.cli(directory, client):
                    review.main()
            receipt = Path(str(script) + '.review_completed.json')
            self.assertTrue(receipt.exists())
            self.assertEqual(1, len(calls))
            original = script.read_bytes()
            receipt_bytes = receipt.read_bytes()
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'contradicts'):
                    with self.cli(directory, client) as log:
                        review.main()
                self.assertNotIn('No changes since the completed review', log.getvalue())
                self.assertEqual(original, script.read_bytes())
                self.assertEqual(receipt_bytes, receipt.read_bytes())
                self.assertEqual(1, len(calls))

    def test_conflicting_child_refuses_annotation_edits_over_repeated_runs(self):
        raw = ('Manifesto.\nStone bell  rang.\n'
               'Original Web Novel Chapter - Complete.\n'
               'Original Translation by Tester.\n'
               'Manifesto.\nStone bell rang.\n')
        parent = [row('Manifesto.', speaker='NARRATOR', source_span=[0, 10]),
                  row('Stone bell rang.', speaker='NARRATOR')]
        child = copy.deepcopy(parent)
        child[1]['source_span'] = [11, 27]
        with tempfile.TemporaryDirectory() as directory:
            saved, _, _ = create_book(directory, saved=True, raw=raw, source=parent)
            state_path = Path(directory, 'state.json')
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['input_file_path'] = str(saved)
            state_path.write_text(json.dumps(state))
            script = Path(directory, 'annotated_script.json')
            script.write_text(json.dumps(child))
            binding = make_script_selection_binding(get_book_operation_token(directory),
                hashlib.sha256(saved.read_bytes()).hexdigest(), script.read_bytes(),
                source_path=str(saved))
            script.with_suffix('.selection_binding.json').write_text(json.dumps(binding))
            reply = [row(text, speaker='NARRATOR', instruct='Softly.')
                     for text in ('Manifesto.', 'Stone bell rang.')]
            client, calls = client_for([(reply, 'stop')] * 4)
            original = script.read_bytes()
            for _ in range(3):
                with self.assertRaisesRegex(ValueError, 'contradicts'):
                    with self.cli(directory, client):
                        review.main()
                self.assertEqual(original, script.read_bytes())
            self.assertEqual(0, len(calls))
            self.assertFalse(Path(str(script) + '.review_completed.json').exists())
            self.assertFalse(Path(str(script) + '.review_checkpoint.json').exists())

    def test_literal_and_scoped_contradictions_refuse_in_both_cli_modes(self):
        from literal_policy import SCRIPT_POLICY_KEY, bind_literal_policy, get_literal_policy
        from dialogue_spans import mark_entries
        from script_selection_binding import (get_scope_cut_from_binding, parse_script_binding,
                                              publish_saved_script_edit)
        from tests.test_review_scoped_source_span import (
            add_scope_evidence, align_cut, build_scoped_rows, make_saved_hop,
            FinalAstraFindingsTests, RAW as scoped_raw, RAW_ROWS as scoped_rows,
            get_preprocessed_source)

        def literal_book(directory):
            raw = 'Dawn broke. Night came.'
            parent = bind_literal_policy(raw, [
                row('Dawn broke.', speaker='NARRATOR', source_span=[0, 11]),
                row('Night came.', speaker='NARRATOR', source_span=[12, 23])])
            child = copy.deepcopy(parent)
            child[1]['source_span'] = [11, 21]
            saved, _, _ = create_book(directory, saved=True, raw=raw, source=parent)
            state_path = Path(directory, 'state.json')
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['input_file_path'] = str(saved)
            state_path.write_text(json.dumps(state))
            script = Path(directory, 'annotated_script.json')
            script.write_text(json.dumps(child))
            binding = make_script_selection_binding(get_book_operation_token(directory),
                hashlib.sha256(saved.read_bytes()).hexdigest(), script.read_bytes(),
                source_path=str(saved))
            script.with_suffix('.selection_binding.json').write_text(json.dumps(binding))
            return script, child

        def scoped_book(directory):
            raw, rows = FinalAstraFindingsTests().punctuation_book('Exclude me!', '!')
            parent, _ = build_scoped_rows(raw, rows, 1)
            child, _ = build_scoped_rows(raw, rows, 1, ancestor=get_literal_policy(parent))
            align_cut(child, parent)
            bad = copy.deepcopy(child)
            bad[0]['source_span'] = [10, 11]
            script, saved, _ = make_saved_hop(directory, raw=raw, original_rows=parent,
                                              active_rows=bad, evidence_rows=child)
            add_scope_evidence(saved, raw, mark_entries(rows, raw), parent)
            return script, bad

        def stripped_scoped_book(directory):
            raw, rows = FinalAstraFindingsTests().punctuation_book('Exclude me!', '!')
            parent, _ = build_scoped_rows(raw, rows, 1)
            child = copy.deepcopy(parent)
            child[0].pop(SCRIPT_POLICY_KEY)
            self.assertEqual([13, 14], child[0]['source_span'])
            child[0]['source_span'] = [10, 11]
            script, saved, _ = make_saved_hop(
                directory, raw=raw, original_rows=parent, active_rows=child)
            add_scope_evidence(saved, raw, mark_entries(rows, raw), parent)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(saved), saved.read_bytes(), parent))
            return script, child

        def stripped_scoped_book_with_frozen_cut(directory):
            raw, rows = FinalAstraFindingsTests().punctuation_book('Exclude me!', '!')
            parent, _ = build_scoped_rows(raw, rows, 1)
            child, _ = build_scoped_rows(raw, rows, 1, ancestor=get_literal_policy(parent))
            align_cut(child, parent)
            script, saved, _ = make_saved_hop(directory, raw=raw, original_rows=parent,
                                              active_rows=child)
            add_scope_evidence(saved, raw, mark_entries(rows, raw), parent)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(saved), saved.read_bytes(), parent))
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), child))
            binding = script.with_suffix('.selection_binding.json')
            scope_cut = get_scope_cut_from_binding(parse_script_binding(binding.read_bytes()))
            self.assertIsNotNone(scope_cut)

            edited = copy.deepcopy(child)
            edited[0].pop(SCRIPT_POLICY_KEY)
            self.assertEqual([13, 14], edited[0]['source_span'])
            self.assertEqual(raw[13:14], raw[10:11])
            edited[0]['source_span'] = [10, 11]
            publish_saved_script_edit(directory, str(script.parent), str(script),
                                      script.read_bytes(), json.dumps(edited).encode('utf-8'))
            self.assertEqual(scope_cut, get_scope_cut_from_binding(
                parse_script_binding(binding.read_bytes())))
            return script, edited

        def legacy_scoped_book(directory, field):
            header = ('Manifesto.\nTranslator note.\nOriginal Web Novel Chapter ― Complete.\n'
                      'Original Translation by Tester.\n\n')
            source = header + scoped_raw
            raw, _ = get_preprocessed_source(source, strip_front_matter=True)
            digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
            parent, _ = build_scoped_rows(raw, scoped_rows, 1, strip=True, input_sha256=digest)
            child, _ = build_scoped_rows(raw, scoped_rows, 2, ancestor=get_literal_policy(parent),
                                        strip=True, input_sha256=digest)
            align_cut(child, parent)
            script, saved, _ = make_saved_hop(directory, raw=source, original_rows=parent,
                                              active_rows=child, evidence_rows=[])
            self.assertIsNotNone(add_scope_evidence(saved, raw, mark_entries(scoped_rows, raw), parent))
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), child))
            binding = script.with_suffix('.selection_binding.json')
            self.assertIsNone(get_scope_cut_from_binding(parse_script_binding(binding.read_bytes())))

            edited = copy.deepcopy(child)
            edited[0][SCRIPT_POLICY_KEY][field] = '5' * 64 if field == 'input_sha256' else False
            publish_saved_script_edit(directory, str(script.parent), str(script),
                                      script.read_bytes(), json.dumps(edited).encode('utf-8'))
            self.assertIsNone(get_scope_cut_from_binding(parse_script_binding(binding.read_bytes())))
            return script, edited

        fixtures = ((literal_book, {}), (scoped_book, {}), (stripped_scoped_book, {}),
                    (stripped_scoped_book_with_frozen_cut, {}),
                    (legacy_scoped_book, {'field': 'input_sha256'}),
                    (legacy_scoped_book, {'field': 'strip_front_matter'}))
        for build, options in fixtures:
            for labels in (False, True):
                with self.subTest(fixture=build.__name__, labels=labels, **options), \
                        tempfile.TemporaryDirectory() as directory:
                    script, entries = build(directory, **options)
                    original = script.read_bytes()
                    binding = script.with_suffix('.selection_binding.json')
                    original_binding = binding.read_bytes()
                    if labels:
                        replies = [[dict(n=index, speaker=entry['speaker'], instruct='Softly.')
                                    for index, entry in enumerate(entries[start:start + 2])]
                                   for start in range(0, len(entries), 2)]
                    else:
                        replies = [[row(entry['text'], speaker=entry['speaker'], instruct='Softly.')
                                    for entry in entries[start:start + 2]]
                                   for start in range(0, len(entries), 2)]
                    client, calls = client_for([(reply, 'stop') for reply in replies] * 4)
                    arg = None if script.name == 'annotated_script.json' else script
                    for _ in range(2):
                        with self.assertRaisesRegex(ValueError, 'contradicts'):
                            with self.cli(directory, client, script=arg, labels_only=labels):
                                review.main()
                        self.assertEqual(original, script.read_bytes())
                        self.assertEqual(original_binding, binding.read_bytes())
                        self.assertFalse(Path(str(script) + '.review_completed.json').exists())
                        self.assertFalse(Path(str(script) + '.review_checkpoint.json').exists())
                    self.assertEqual(0, len(calls))

    def test_saved_parent_unambiguous_child_split_publishes_and_reuses_receipt(self):
        heading, narration = 'Chapter 7', 'Snow covered the steps.'
        raw = ('Intro.\n' + heading + '\n\n' + narration +
               '\nLong appendix with a damaged \ufffd glyph outside the script.')
        start = raw.index(heading)
        end = raw.index(narration) + len(narration)
        parent = [row(heading + ' ' + narration, speaker='NARRATOR', source_span=[start, end])]
        child = copy.deepcopy(parent)
        reply = [row(heading, speaker='NARRATOR'), row(narration, speaker='NARRATOR')]
        with tempfile.TemporaryDirectory() as directory:
            saved, _, _ = create_book(directory, saved=True, raw=raw, source=parent)
            state_path = Path(directory, 'state.json')
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['input_file_path'] = str(saved)
            state_path.write_text(json.dumps(state))
            script = Path(directory, 'annotated_script.json')
            script.write_text(json.dumps(child))
            binding = make_script_selection_binding(get_book_operation_token(directory),
                hashlib.sha256(saved.read_bytes()).hexdigest(), script.read_bytes(),
                source_path=str(saved))
            script.with_suffix('.selection_binding.json').write_text(json.dumps(binding))
            verified = review.get_review_source_text(directory, str(script), script.read_bytes(), child)
            self.assertIsNotNone(verified)
            client, calls = client_for([(reply, 'stop')])
            with self.cli(directory, client) as log:
                review.main()
                published = script.read_bytes()
                review.main()
            self.assertEqual(published, script.read_bytes())
            self.assertIn('No changes since the completed review', log.getvalue())
            output = json.loads(published)
            self.assertEqual([heading, narration], [entry['text'] for entry in output])
            self.assertEqual([[start, start + len(heading)], [start + len(heading) + 2, end]],
                             [entry['source_span'] for entry in output])
            self.assert_mapped(output, raw)
            self.assertTrue(Path(str(script) + '.review_completed.json').exists())

    def test_mixed_findings_report_in_source_entry_order(self):
        source = [row('A blue bird.', source_span=[0, 12]),
                  row('A green bird.', source_span=[13, 26]),
                  row('A quiet pond.', source_span=[27, 51]),
                  row('Two frogs.', source_span=[27, 51])]
        reply = [row(text) for text in ('A blue', 'bird.', 'A green', 'bird.',
                                       'A quiet', 'pond. Two frogs.')]
        client, calls = client_for([(reply, 'stop')])
        log = io.StringIO()
        with redirect_stdout(log):
            output = review.review_batch(client, 'fixture', source, 1, 1,
                                         LLMGenParams(), max_retries=0)
        self.assertIsNone(output)
        warning = next(line for line in log.getvalue().splitlines()
                       if 'failed quality validation' in line)
        self.assertEqual(['1', '2', '3', '4'], re.findall(r'entry=(\d+);', warning))
        self.assertEqual(4, warning.count('Review fragments require verified'))
        clean = [row('A blue bird.', source_span=[0, 12]),
                 row('A green bird.', source_span=[13, 26])]
        clean_reply = [row(text) for text in ('A blue', 'bird.', 'A green bird.')]
        client, calls = client_for([(clean_reply, 'stop')])
        with redirect_stdout(io.StringIO()):
            bound = review.review_batch(client, 'fixture', clean, 1, 1, LLMGenParams(),
                                        max_retries=0, source_text='A blue bird. A green bird.')
        self.assertIsNotNone(bound)
        self.assertEqual([[0, 6], [7, 12], [13, 26]], [entry['source_span'] for entry in bound])

    def test_cli_publishes_verified_heading_split(self):
        heading, narration = 'Chapter 1: The Gate', 'Dawn broke.'
        covered = f'{heading}\n\n{narration}'
        raw = covered + '\nUnreviewed appendix with a bad \ufffd character.'
        source = [row(covered, speaker='NARRATOR', source_span=[0, len(covered)])]
        reply = [row(heading, speaker='NARRATOR'), row(narration, speaker='NARRATOR')]
        with tempfile.TemporaryDirectory() as directory:
            script, _, _ = create_book(directory, raw=raw, source=source)
            client, calls = client_for([(reply, 'stop')])
            with self.cli(directory, client):
                review.main()
            output = json.loads(script.read_bytes())
            self.assertEqual(2, len(output))
            self.assert_mapped(output, raw)
            self.assertEqual([[0, len(heading)], [len(heading) + 2, len(covered)]],
                             [entry['source_span'] for entry in output])
            self.assertTrue(Path(str(script) + '.review_completed.json').exists())

    def inherited_partition(self):
        prefix = 'Clouds gathered.\nChapter 7\nThe lantern glowed.'
        raw = prefix + '\n\n' + RAW
        source = mark_entries([row('Clouds gathered.', speaker='NARRATOR'),
                               row('Chapter 7', speaker='NARRATOR'),
                               row('The lantern glowed.', speaker='NARRATOR')] + SOURCE, raw)
        for entry in source[:3]:
            entry['source_span'] = [0, len(prefix)]
        return raw, source, source[:3] + REPLY

    def test_shared_parent_spans_capture_exact_partition_for_active_and_saved_books(self):
        raw, source, _ = self.inherited_partition()
        for saved in (False, True):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as directory:
                script, _, entries = create_book(directory, raw=raw, source=source, saved=saved)
                before = script.read_bytes()
                self.assertEqual(raw, review.get_review_source_text(
                    directory, str(script), before, entries))
                self.assertEqual(before, script.read_bytes())
                self.assertEqual(source, entries)

    def test_shared_parent_spans_do_not_block_unrelated_speaker_reassignment(self):
        raw, source, reply = self.inherited_partition()
        for literal in (False, True):
            with self.subTest(literal=literal):
                output, attempts = self.run_reply(source=source, reply=reply,
                                                 source_text=raw, literal=literal)
                self.assert_mapped(output, raw)
                self.assertEqual([[raw.index(entry['text']), raw.index(entry['text']) + len(entry['text'])]
                                  for entry in output],
                                 [entry['source_span'] for entry in output])
                self.assertEqual(['NARRATOR', 'NARRATOR', 'NARRATOR', 'NARRATOR', 'ALICE', 'ALICE'],
                                 [entry['speaker'] for entry in output])
                self.assertEqual('accepted', attempts[0]['outcome'])

    def test_cli_resolves_shared_parent_spans_before_batch_boundaries(self):
        raw, source, _ = self.inherited_partition()
        for context_window in (0, 1):
            with self.subTest(context_window=context_window), tempfile.TemporaryDirectory() as directory:
                script, _, _ = create_book(directory, raw=raw, source=source)
                client, calls = client_for([(source[:2], 'stop'),
                    ([source[2], REPLY[0], row('Please open the gate.')], 'stop'),
                    (source[4:], 'stop')])
                with self.cli(directory, client), patch.object(review.sys, 'argv',
                        ['review', '--context-window', str(context_window)]):
                    review.main()
                output = json.loads(script.read_bytes())
                self.assert_mapped(output, raw)
                self.assertEqual(3, len(calls))
                self.assertEqual('ALICE', output[4]['speaker'])
                self.assertTrue(Path(str(script) + '.review_completed.json').exists())

    def test_shared_parent_span_recovery_refuses_unproved_partitions(self):
        for mutation in ('missing', 'reordered', 'duplicated', 'different_span'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                raw, source, _ = self.inherited_partition()
                if mutation == 'missing':
                    del source[1]
                elif mutation == 'reordered':
                    source[0], source[1] = source[1], source[0]
                elif mutation == 'duplicated':
                    source.insert(1, copy.deepcopy(source[0]))
                else:
                    source[1]['source_span'][1] -= 1
                script, _, entries = create_book(directory, raw=raw, source=source)
                self.assertIsNone(review.get_review_source_text(
                    directory, str(script), script.read_bytes(), entries))
                output, attempts = self.run_reply(source=source, reply=source[:-2] + REPLY,
                                                 source_text=raw)
                self.assertIsNone(output)
                self.assertEqual('quality_rejected', attempts[0]['outcome'])

    def test_span_rejection_identifies_entry_fragment_and_reason_without_content(self):
        _, report = review.get_review_source_binding(SOURCE, REPLY)
        finding = report['findings'][0]
        self.assertEqual((1, 1, 'source_unavailable'),
                         (finding['entry_number'], finding['fragment_number'], finding['reason_code']))
        self.assertIn('entry=1; fragment=1; reason=source_unavailable', finding['message'])
        self.assertNotIn('Rain', json.dumps(report))
        self.assertNotIn('ALICE', json.dumps(report))
        from dialogue_spans import get_source_span_fragments, SourceSpanError
        with self.assertRaises(SourceSpanError) as caught:
            get_source_span_fragments(RAW, SOURCE[0]['text'], SOURCE[0]['source_span'],
                                      ['Rain fell.', 'Please close the gate.'])
        self.assertEqual(('fragment_partition_mismatch', 2),
                         (caught.exception.reason_code, caught.exception.fragment_number))
        self.assertNotIn('gate', str(caught.exception))

    def test_shared_parent_span_resolution_does_not_relax_output_verification(self):
        raw, source, _ = self.inherited_partition()
        with self.assertRaises(ValueError):
            review.verify_review_source_spans(source, raw)
        resolved = review.get_review_source_entries(source, raw)
        review.verify_review_source_spans(resolved, raw)
        self.assertEqual([[0, 16], [17, 26], [27, 46]],
                         [entry['source_span'] for entry in resolved[:3]])
        self.assertEqual([[0, 46]] * 3, [entry['source_span'] for entry in source[:3]])

    @contextmanager
    def cli(self, directory, client, *, script=None, labels_only=False):
        config = {'llm_mode': 'remote',
                  'llm_remote': {'model_name': 'fixture', 'base_url': 'https://api.example.invalid/v1'},
                  'generation': {'review_batch_size': 2, 'merge_narrators': True}}
        with ExitStack() as stack:
            stdout = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch('generate_script.time.sleep'))
            argv = ['review'] + (['--input', str(script)] if script else [])
            if labels_only:
                argv.append('--labels-only')
            stack.enter_context(patch.object(review.sys, 'argv', argv))
            for name, value in (('get_runtime_data_dir', directory), ('load_app_config', config),
                                ('ensure_ideal_settings', (True, {}, 'fixture')),
                                ('get_active_llm_config', config['llm_remote']),
                                ('make_run_client', client), ('ensure_cached_or_benchmarked_concurrency', 1),
                                ('get_current_status', {'loaded': False})):
                stack.enter_context(patch.object(review, name, return_value=value))
            stack.enter_context(patch.object(review, 'wait_for_vram_headroom',
                                            side_effect=AssertionError('No GPU calls')))
            yield stdout

    def test_publication_refuses_identical_script_from_replaced_source_book(self):
        for saved in (False, True):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as directory:
                source = copy.deepcopy(SOURCE)
                source[0]['source_span'] = [0, 33]
                source[1]['source_span'] = [35, 53]
                raw_a = 'Rain  fell. Please open the gate.\n\nTake the blue cup.'
                raw_b = 'Rain fell. Please  open the gate.\n\nTake the blue cup.'
                script, _, _ = create_book(directory, saved=saved, raw=raw_a, source=source)
                original = script.read_bytes()
                chunks = Path(directory, 'chunks.json')
                chunks.write_bytes(b'[{"book":"B"}]')
                client, calls = client_for([(REPLY, 'stop')])
                create = client.chat.completions.create
                replacement_files = {}
                def replace_book(**kwargs):
                    response = create(**kwargs)
                    state_path = Path(directory, 'state.json')
                    state = json.loads(state_path.read_text(encoding='utf-8'))
                    state.update(active_book_id='book-B', book_generation=2)
                    source_path = Path(directory, 'uploads', 'book-B.txt')
                    source_path.write_bytes(raw_b.encode('utf-8'))
                    state['input_file_path'] = str(source_path)
                    state_path.write_text(json.dumps(state), encoding='utf-8')
                    binding = make_script_selection_binding(get_book_operation_token(directory),
                        hashlib.sha256(source_path.read_bytes()).hexdigest(), original,
                        source_path=str(source_path))
                    script.with_suffix('.selection_binding.json').write_text(json.dumps(binding), encoding='utf-8')
                    for path in (state_path, source_path, script.with_suffix('.selection_binding.json')):
                        replacement_files[path] = path.read_bytes()
                    return response
                client.chat.completions.create = replace_book
                with self.cli(directory, client, script=script if saved else None), \
                        patch('review_startup.record_failure') as diagnostic:
                    with self.assertRaisesRegex(ValueError, 'source.*changed during review'):
                        review.main()
                self.assertEqual(1, len(calls))
                self.assertEqual(original, script.read_bytes())
                self.assertEqual(b'[{"book":"B"}]', chunks.read_bytes())
                self.assertFalse(Path(str(script) + '.review_completed.json').exists())
                for path, expected in replacement_files.items():
                    self.assertEqual(expected, path.read_bytes())
                diagnostic.assert_called_once_with('review_source_identity_changed', task='review')

    def test_publication_refuses_each_captured_identity_change(self):
        for saved, mutation in product((False, True), ('token', 'path', 'bytes', 'binding')):
            with self.subTest(saved=saved, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                script, source_path, _ = create_book(directory, saved=saved)
                original = script.read_bytes()
                client, _ = client_for([(REPLY, 'stop')])
                create = client.chat.completions.create
                def mutate(**kwargs):
                    response = create(**kwargs)
                    state_path = Path(directory, 'state.json')
                    state = json.loads(state_path.read_text(encoding='utf-8'))
                    if mutation == 'token':
                        state['book_generation'] += 1
                    elif mutation == 'path':
                        replacement = source_path.with_name('replacement.txt')
                        replacement.write_bytes(source_path.read_bytes())
                        state['input_file_path'] = str(replacement)
                    elif mutation == 'bytes':
                        source_path.write_bytes(RAW.replace('Rain', 'Hail').encode('utf-8'))
                    state_path.write_text(json.dumps(state), encoding='utf-8')
                    binding = make_script_selection_binding(get_book_operation_token(directory),
                        hashlib.sha256(Path(state['input_file_path']).read_bytes()).hexdigest(), original,
                        source_path=state['input_file_path'])
                    binding_path = script.with_suffix('.selection_binding.json')
                    if mutation != 'bytes':
                        binding_path.write_text(json.dumps(binding) + (' ' if mutation == 'binding' else ''), encoding='utf-8')
                    return response
                client.chat.completions.create = mutate
                with self.cli(directory, client, script=script if saved else None):
                    with self.assertRaisesRegex(ValueError, 'source.*changed during review'):
                        review.main()
                self.assertEqual(original, script.read_bytes())
                self.assertFalse(Path(str(script) + '.review_completed.json').exists())

    def test_span_free_receipts_keep_the_version_11_fingerprint_and_skip_the_model(self):
        for labels_only in (False, True):
            with self.subTest(labels_only=labels_only), tempfile.TemporaryDirectory() as directory:
                source = copy.deepcopy(SOURCE)
                for entry in source:
                    del entry['source_span']
                script, _, _ = create_book(directory, source=source)
                reply = ([{'n': index, 'speaker': entry['speaker'], 'instruct': entry['instruct']}
                          for index, entry in enumerate(source)] if labels_only else source)
                client, calls = client_for([(reply, 'stop'), (reply, 'stop')])
                with self.cli(directory, client, labels_only=labels_only) as stdout:
                    fingerprint_identity = review.get_run_fingerprint_identity
                    def legacy_identity(identity):
                        identity = {key: value for key, value in identity.items()
                                    if key != 'review_source_sha256'}
                        identity['algorithm'] = 11
                        return fingerprint_identity(identity)
                    with patch.object(review, 'get_run_fingerprint_identity', side_effect=legacy_identity):
                        review.main()
                    self.assertEqual(1, len(calls))
                    receipt_path = Path(str(script) + '.review_completed.json')
                    receipt = receipt_path.read_bytes()
                    review.main()
                self.assertEqual(1, len(calls))
                self.assertEqual(receipt, receipt_path.read_bytes())
                self.assertIn('No changes since the completed review', stdout.getvalue())

    def test_loaded_saved_source_lineage_is_fenced_through_publication(self):
        for mutation in ('unchanged', 'bytes', 'binding'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                saved, source_path, _ = create_book(directory, saved=True)
                state_path = Path(directory, 'state.json')
                state = json.loads(state_path.read_text(encoding='utf-8'))
                state['input_file_path'] = str(saved)
                state_path.write_text(json.dumps(state), encoding='utf-8')
                script = Path(directory, 'annotated_script.json')
                original = saved.read_bytes()
                script.write_bytes(original)
                binding = make_script_selection_binding(get_book_operation_token(directory),
                    hashlib.sha256(original).hexdigest(), original, source_path=str(saved))
                script.with_suffix('.selection_binding.json').write_text(json.dumps(binding), encoding='utf-8')
                client, _ = client_for([(REPLY, 'stop')])
                create = client.chat.completions.create
                def mutate(**kwargs):
                    response = create(**kwargs)
                    if mutation == 'bytes':
                        source_path.write_bytes(RAW.replace('Rain', 'Hail').encode('utf-8'))
                    elif mutation == 'binding':
                        binding_path = saved.with_suffix('.selection_binding.json')
                        binding_path.write_bytes(binding_path.read_bytes() + b' ')
                    return response
                client.chat.completions.create = mutate
                with self.cli(directory, client):
                    if mutation == 'unchanged':
                        review.main()
                    else:
                        with self.assertRaisesRegex(ValueError, 'source.*changed during review'):
                            review.main()
                if mutation == 'unchanged':
                    self.assert_mapped(json.loads(script.read_bytes()), RAW)
                else:
                    self.assertEqual(original, script.read_bytes())
                self.assertEqual(mutation == 'unchanged', Path(str(script) + '.review_completed.json').exists())

    def test_prior_span_receipts_are_invalidated_and_new_receipts_are_reused(self):
        for version in (11, 12):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                script, _, _ = create_book(directory)
                client, calls = client_for([(SOURCE, 'stop'), (SOURCE, 'stop')])
                with self.cli(directory, client) as stdout:
                    fingerprint_identity = review.get_run_fingerprint_identity
                    def legacy_identity(identity):
                        identity = dict(identity)
                        identity['algorithm'] = version
                        if version == 11:
                            identity.pop('review_source_sha256', None)
                        return fingerprint_identity(identity)
                    with patch.object(review, 'get_run_fingerprint_identity', side_effect=legacy_identity):
                        review.main()
                    self.assertEqual(1, len(calls))
                    review.main()
                    self.assertEqual(2, len(calls))
                    receipt_path = Path(str(script) + '.review_completed.json')
                    receipt = receipt_path.read_bytes()
                    review.main()
                    self.assertEqual(2, len(calls))
                    self.assertEqual(receipt, receipt_path.read_bytes())
                self.assertIn('No changes since the completed review', stdout.getvalue())

    def run_reply(self, source=SOURCE, reply=REPLY, source_text=None, literal=False):
        original = copy.deepcopy(source)
        client, calls = client_for([(reply, 'stop')])
        attempts = []
        with redirect_stdout(io.StringIO()):
            output = review.review_batch(client, 'fixture', source, 1, 1,
                LLMGenParams(literal_source_preserving=literal), max_retries=0,
                source_text=source_text, attempt_observer=attempts.append)
        self.assertEqual(original, source)
        self.assertEqual(1, len(calls))
        return output, attempts

    def assert_mapped(self, output, raw):
        self.assertIsNotNone(output)
        for entry in output:
            start, end = entry['source_span']
            self.assertEqual(_normalize(entry['text']), _normalize(raw[start:end]))
        for previous, current in zip(output, output[1:]):
            self.assertLessEqual(previous['source_span'][1], current['source_span'][0])

    def test_missing_source_refuses_split_in_ordinary_and_literal_modes(self):
        for literal in (False, True):
            with self.subTest(literal=literal):
                output, attempts = self.run_reply(literal=literal)
                self.assertIsNone(output)
                self.assertEqual('quality_rejected', attempts[0]['outcome'])
                self.assertIn('review_source_span_unverified', attempts[0]['failure_codes'])

    def test_invalid_source_or_original_spans_refuse_instead_of_inventing_offsets(self):
        invalid_sources = [None, RAW[:10], 'X' + RAW[1:]]
        invalid_spans = [[0, 31], [0, 1000], [-1, 31], [True, 32], {'start': 0, 'end': 32}]
        cases = [(copy.deepcopy(SOURCE), raw) for raw in invalid_sources]
        for span in invalid_spans:
            source = copy.deepcopy(SOURCE)
            source[0]['source_span'] = span
            cases.append((source, RAW))
        for source, raw in cases:
            for literal in (False, True):
                with self.subTest(span=source[0]['source_span'], raw=raw, literal=literal):
                    output, attempts = self.run_reply(source=source, source_text=raw, literal=literal)
                    self.assertIsNone(output)
                    self.assertEqual('quality_rejected', attempts[0]['outcome'])

    def test_single_original_entry_split_also_gets_disjoint_exact_spans(self):
        reply = [REPLY[0], row('Please open the gate.')]
        for literal in (False, True):
            with self.subTest(literal=literal):
                output, _ = self.run_reply(source=SOURCE[:1], reply=reply,
                                          source_text=RAW, literal=literal)
                self.assert_mapped(output, RAW)
                self.assertEqual([[0, 10], [11, 32]], [entry['source_span'] for entry in output])
                self.assertNotIn('pause_after', output[0])
                self.assertEqual(700, output[1]['pause_after'])

    def test_quote_and_whitespace_offsets_use_the_existing_dialogue_map(self):
        raw_sources = ['Rain  fell. Please open the gate.\n\nTake the blue cup.',
                       'Rain fell. Please  open the gate.\n\nTake the blue cup.',
                       '“Rain\t  fell.” Please\nopen  the gate.\n\n“Take the blue cup.”']
        for raw in raw_sources:
            for literal in (False, True):
                with self.subTest(raw=raw, literal=literal):
                    source = mark_entries(SOURCE, raw)
                    output, _ = self.run_reply(source=source, source_text=raw, literal=literal)
                    self.assert_mapped(output, raw)
                    expected = mark_entries([REPLY[0], row('Please open the gate.'), SOURCE[1]], raw)
                    self.assertEqual([entry['source_span'] for entry in expected],
                                     [entry['source_span'] for entry in output])
                    self.assertEqual(['X', 'X', 'Y'],
                                     [entry['provenance']['metadata'] for entry in output])

    def test_repeated_text_cannot_remap_outside_the_original_span(self):
        raw = 'Rain fell. ' + RAW
        source = mark_entries(SOURCE, raw)
        output, _ = self.run_reply(source=source, source_text=raw)
        self.assert_mapped(output, raw)
        self.assertEqual([[11, 21], [22, 43], [45, 63]],
                         [entry['source_span'] for entry in output])

    def test_overlapping_original_spans_are_not_published_as_a_remapped_result(self):
        source = copy.deepcopy(SOURCE)
        source[1]['source_span'] = [20, 38]
        output, attempts = self.run_reply(source=source, source_text=RAW)
        self.assertIsNone(output)
        self.assertEqual('quality_rejected', attempts[0]['outcome'])

    def test_rejected_split_records_content_free_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = Path(directory, 'diagnostics.log')
            with patch('review_startup.get_failure_log_path', return_value=diagnostic):
                self.run_reply()
            records = [json.loads(line) for line in diagnostic.read_text(encoding='utf-8').splitlines()]
            self.assertIn('review_source_span_rejected', [record['stage'] for record in records])
            self.assertTrue(all(record['task'] == 'review' for record in records))
            self.assertNotIn('Rain', diagnostic.read_text(encoding='utf-8'))
            self.assertNotIn('ALICE', diagnostic.read_text(encoding='utf-8'))

    def test_capture_requires_exact_source_and_script_identity_for_active_and_saved_books(self):
        for saved in (False, True):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as directory:
                script, source_path, source = create_book(directory, saved=saved)
                self.assertEqual(RAW, review.get_review_source_text(
                    directory, str(script), script.read_bytes(), source))
                source_path.write_text('X' + RAW[1:], encoding='utf-8')
                self.assertIsNone(review.get_review_source_text(
                    directory, str(script), script.read_bytes(), source))

    def test_capture_refuses_unbound_or_changed_scripts(self):
        for mutation in ('missing_binding', 'changed_script', 'literal_source_identity'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                source = copy.deepcopy(SOURCE)
                if mutation == 'literal_source_identity':
                    from literal_policy import bind_literal_policy
                    source = bind_literal_policy(RAW, source, input_sha256='a' * 64)
                script, _, source = create_book(directory, source=source)
                if mutation == 'missing_binding':
                    script.with_suffix('.selection_binding.json').unlink()
                elif mutation == 'changed_script':
                    script.write_bytes(script.read_bytes() + b' ')
                self.assertIsNone(review.get_review_source_text(
                    directory, str(script), script.read_bytes(), source))

    def test_capture_refuses_a_different_active_book_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            script, _, source = create_book(directory)
            state_path = Path(directory, 'state.json')
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['book_generation'] += 1
            state_path.write_text(json.dumps(state), encoding='utf-8')
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), source))

    def test_loaded_saved_book_follows_only_its_verified_original_source_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            saved_script, _, source = create_book(directory, saved=True)
            root = Path(directory)
            state_path = root / 'state.json'
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['input_file_path'] = str(saved_script)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            script = root / 'annotated_script.json'
            script.write_bytes(saved_script.read_bytes())
            binding = make_script_selection_binding(get_book_operation_token(directory),
                hashlib.sha256(saved_script.read_bytes()).hexdigest(), script.read_bytes(),
                source_path=str(saved_script))
            script.with_suffix('.selection_binding.json').write_text(json.dumps(binding), encoding='utf-8')
            captured = review.get_review_source_text(directory, str(script), script.read_bytes(), source)
            self.assertEqual(RAW, captured)
            output, _ = self.run_reply(source=source, source_text=captured)
            self.assert_mapped(output, RAW)
            saved_script.with_suffix('.selection_binding.json').unlink()
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), source))

    def test_contextual_review_finds_default_upload_for_saved_and_loaded_scripts(self):
        intro = 'The watch began.'
        context = [row(intro, source_span=[0, len(intro)]), row('Everyone waited.')]
        prefix = intro + '\n\nEveryone waited.\n\n'
        raw = prefix + RAW
        entries = context + copy.deepcopy(SOURCE)
        for entry in entries[2:]:
            entry['source_span'] = [offset + len(prefix) for offset in entry['source_span']]
        for loaded, forward_slashes in product((False, True), repeat=2):
            with self.subTest(loaded=loaded, forward_slashes=forward_slashes), \
                    tempfile.TemporaryDirectory() as directory:
                app_dir = Path(directory, 'app')
                upload = app_dir / 'uploads' / 'fixture.txt'
                saved, _, _ = create_book(directory, saved=True, source=entries,
                    raw=raw.replace('\n', '\r\n'),
                    upload_path=upload.as_posix() if forward_slashes else str(upload))
                state_path = Path(directory, 'state.json')
                state = json.loads(state_path.read_bytes())
                state['input_file_path'] = str(saved) if loaded else str(saved.with_name('other.json'))
                state_path.write_text(json.dumps(state), encoding='utf-8')
                script = saved
                if loaded:
                    script = Path(directory, 'annotated_script.json')
                    original = saved.read_bytes()
                    script.write_bytes(original)
                    binding = make_script_selection_binding(get_book_operation_token(directory),
                        hashlib.sha256(original).hexdigest(), original, source_path=str(saved))
                    script.with_suffix('.selection_binding.json').write_text(
                        json.dumps(binding), encoding='utf-8')
                client, calls = client_for([(context, 'stop'), *((REPLY, 'stop'),) * 3])
                with self.cli(directory, client, script=None if loaded else saved), \
                        patch.multiple('core', BASE_DIR=str(app_dir), ROOT_DIR=directory), \
                        patch.object(review, 'record_failure') as diagnostic:
                    review.sys.argv.extend(['--context-window', '1'])
                    review.main()
                diagnostic.assert_not_called()
                self.assertEqual(2, len(calls))
                output = json.loads(script.read_bytes())
                self.assertEqual([[start + len(prefix), end + len(prefix)]
                                  for start, end in ((0, 10), (11, 32), (34, 52))],
                                 [entry['source_span'] for entry in output[2:]])
                self.assertTrue(Path(str(script) + '.review_completed.json').is_file())
                self.assertEqual(raw.replace('\n', '\r\n').encode(), upload.read_bytes())

    def test_capture_reuses_generation_decoding_and_newline_coordinates(self):
        for encoding, text in (('utf-8', RAW.replace('\n', '\r\n')),
                               ('cp1252', RAW.replace('Rain', 'Café'))):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                prepared = text.replace('\r\n', '\n')
                source = copy.deepcopy(SOURCE)
                source[0]['text'] = source[0]['text'].replace('Rain', 'Café') if encoding == 'cp1252' else source[0]['text']
                script, source_path, source = create_book(directory, raw=text, source=source)
                raw = text.encode(encoding)
                source_path.write_bytes(raw)
                binding = make_script_selection_binding(get_book_operation_token(directory),
                    hashlib.sha256(raw).hexdigest(), script.read_bytes(), source_path=str(source_path))
                script.with_suffix('.selection_binding.json').write_text(json.dumps(binding), encoding='utf-8')
                self.assertEqual(prepared, review.get_review_source_text(
                    directory, str(script), script.read_bytes(), source))
                self.assertEqual(raw, source_path.read_bytes())

    def test_cli_remaps_verified_source_or_keeps_original_entries_with_checkpoint(self):
        for context_window in (0, 1):
            for condition, literal in product(('missing', 'bad_span', 'verified'), (False, True)):
                with self.subTest(context_window=context_window, condition=condition, literal=literal), \
                        tempfile.TemporaryDirectory() as directory:
                    original = copy.deepcopy(SOURCE)
                    if condition == 'bad_span':
                        original[0]['source_span'] = [0, 31]
                    if literal:
                        from literal_policy import bind_literal_policy
                        original = bind_literal_policy(RAW, original)
                    script, source_path, source = create_book(directory, source=original)
                    available = condition == 'verified'
                    if condition == 'missing':
                        source_path.unlink()
                        script.with_suffix('.selection_binding.json').unlink()
                    client, calls = client_for([(REPLY, 'stop')] * 3)
                    config = {'llm_mode': 'remote',
                              'llm_remote': {'model_name': 'fixture', 'base_url': 'https://api.example.invalid/v1'},
                              'generation': {'review_batch_size': 2, 'merge_narrators': True}}
                    stdout = io.StringIO()
                    with ExitStack() as stack:
                        stack.enter_context(redirect_stdout(stdout))
                        stack.enter_context(patch('generate_script.time.sleep'))
                        stack.enter_context(patch.object(review.sys, 'argv',
                            ['review', '--context-window', str(context_window)]))
                        for name, value in (('get_runtime_data_dir', directory), ('load_app_config', config),
                                            ('ensure_ideal_settings', (True, {}, 'fixture')),
                                            ('get_active_llm_config', config['llm_remote']),
                                            ('make_run_client', client), ('ensure_cached_or_benchmarked_concurrency', 1),
                                            ('get_current_status', {'loaded': False})):
                            stack.enter_context(patch.object(review, name, return_value=value))
                        stack.enter_context(patch.object(review, 'wait_for_vram_headroom',
                                                        side_effect=AssertionError('No GPU calls')))
                        review.main()
                        if available:
                            first_result = script.read_bytes()
                            review.main()
                            self.assertEqual(first_result, script.read_bytes())
                            self.assertIn('No changes since the completed review', stdout.getvalue())
                    saved = json.loads(script.read_text(encoding='utf-8'))
                    if available:
                        self.assert_mapped(saved, RAW)
                        self.assertEqual([[0, 10], [11, 32], [34, 52]],
                                         [entry['source_span'] for entry in saved])
                    else:
                        self.assertEqual(source, saved)
                        self.assertIn('FAILED_SECTIONS_JSON', stdout.getvalue())
                    self.assertEqual(not available, Path(str(script) + '.review_checkpoint.json').exists())
                    self.assertEqual(available, Path(str(script) + '.review_completed.json').exists())
                    self.assertEqual(1 if available else 3, len(calls))
