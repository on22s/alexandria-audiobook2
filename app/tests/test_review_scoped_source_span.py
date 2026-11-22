"""Scoped literal-source review keeps verified source spans for split entries."""
import copy
import hashlib
import io
import json
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

from book_state_transaction import get_book_operation_token
from completed_review_receipt import get_review_entries_fingerprint
from dialogue_spans import _normalize, mark_entries
from generate_script import get_preprocessed_source
import literal_fidelity
from literal_fidelity import _words, iter_literal_words
from literal_policy import (SCRIPT_POLICY_KEY, bind_literal_chunks, bind_literal_policy,
                            get_literal_policy, get_literal_policy_identity, get_word_identity,
                            get_scope_coverage_digest, get_scope_coverage_intervals,
                            is_scope_cut_proof)
from project import ProjectManager, group_into_chunks
from project_scope_apply import (apply_source_scope, apply_source_scope_undo,
                                 get_source_scope_undo_preview, discard_source_scope_undo)
from project_scope_literal import (get_literal_scope, get_scope_cut_evidence,
                                   ScopeSpanContradiction)
from project_scope_partition import get_scope_partition
from project_scope_plan import get_scope_preview
from project_scope_admission import ensure_scope_admission
import review_script as review
from script_selection_binding import (BINDING_NAME, make_script_selection_binding,
                                      attach_scope_cut_evidence, parse_script_binding,
                                      get_scope_cut_from_binding, binding_for_controlled_edit,
                                      publish_controlled_script_edit)
from tests.test_pass_boundary_retry_regressions import client_for
from tests.test_review_source_binding import row
from tests.test_review_source_spans import create_book


RAW = ('The council adjourned.\n\nA bell rang.\n\nShe waited.\n\nNothing happened.\n\n'
       'The door closed.\n\nAt last the ledger was balanced.\n\n'
       'Afterward.\n\nHARK—!\n\nNight fell.\n\nA soft chime sounded.')
NARRATOR_TEXT = ('At last the ledger was balanced.\n\n'
                 'Afterward.\n\nHARK—!')
START_ENTRY = 1
NEW_SPEAKER = 'ARIA VANCE'
ANNOTATIONS = {'speaker_gender': 'male', 'speaker_age_group': 'adult', 'speaker_ageless': True}
RAW_ROWS = [
    row('The council adjourned.', 'NARRATOR', instruct='Even.', spoken=False),
    row('A bell rang.', 'NARRATOR', instruct='Even.', spoken=False),
    row('She waited.', 'NARRATOR', instruct='Even.', spoken=False),
    row('Nothing happened.', 'NARRATOR', instruct='Even.', spoken=False),
    row('The door closed.', 'NARRATOR', instruct='Even.', spoken=False),
    row(NARRATOR_TEXT, 'NARRATOR', instruct='Reflective.', spoken=False,
        provenance={'section': 7}),
    row('Night fell.', 'NARRATOR', instruct='Even.', spoken=False),
    row('A soft chime sounded.', 'NARRATOR', instruct='Even.', spoken=False),
]


def make_scoped_book():
    """A synthetic retained script with a verified selected-source policy."""
    source_rows = copy.deepcopy(RAW_ROWS)
    marked = mark_entries(source_rows, RAW)
    parent = get_literal_policy(bind_literal_policy(RAW, marked))
    words = list(_words(RAW))
    start = sum(len(list(_words(entry['text']))) for entry in marked[:START_ENTRY])
    selected = ' '.join(words[start:])
    retained = [{key: value for key, value in entry.items() if key != SCRIPT_POLICY_KEY}
                for entry in marked[START_ENTRY:]]
    policy = get_literal_policy(bind_literal_policy(selected, retained))
    policy.update(boundary='selected_source_words_v1',
                  scope_chunks={'chunk-1': {'version': 'literal_words_v1',
                                            'boundary': 'before_speech_preparation',
                                            'narration_words': get_word_identity([selected]),
                                            'projection': 'existing_project_speakable_entries_v1',
                                            'source_policy_sha256': get_literal_policy_identity(parent)}},
                  scope_source={'parent_policy_sha256': get_literal_policy_identity(parent),
                                'prepared_source_sha256': hashlib.sha256(RAW.encode()).hexdigest(),
                                'start_word': start, 'end_word': len(words),
                                'total_words': len(words),
                                'script_start_entry': START_ENTRY})
    get_literal_policy([{SCRIPT_POLICY_KEY: policy}])
    retained[0][SCRIPT_POLICY_KEY] = policy
    return retained, marked[START_ENTRY:]


def align_cut(rows, parent_rows):
    """Record the synthetic child's cut relative to its parent rows."""
    for index in range(len(parent_rows)):
        if [row['text'] for row in parent_rows[index:]] == [row['text'] for row in rows]:
            scope = rows[0][SCRIPT_POLICY_KEY]['scope_source']
            scope['script_start_entry'] = index
            return
    return


def split_reply(batch):
    """The reported shape: unchanged rows, narration, then attributed utterance."""
    narrator = {key: value for key, value in batch[4].items()
                if key not in ('text', SCRIPT_POLICY_KEY)}
    answer = [{key: value for key, value in entry.items()
               if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in batch[:4]]
    answer.append({**narrator,
                   'text': 'At last the ledger was balanced.\n\nAfterward.'})
    answer.append({**narrator, 'speaker': NEW_SPEAKER, 'text': 'HARK—!',
                   'instruct': 'Startled shout.', 'spoken': True, **ANNOTATIONS})
    answer.extend({key: value for key, value in entry.items()
                   if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in batch[5:])
    return answer


def run_cli(batch, replies, config=None, *, raw=RAW):
    """Drive the real review entry point with a mock provider."""
    client, calls = client_for([(reply, 'stop') for reply in replies])
    with tempfile.TemporaryDirectory() as directory:
        script, _, _ = create_book(directory, raw=raw, source=batch)
        config = config or {'llm_mode': 'remote',
                            'llm_remote': {'model_name': 'fixture',
                                           'base_url': 'https://api.example.invalid/v1'},
                            'generation': {'review_batch_size': len(batch) + 2,
                                           'merge_narrators': False}}
        stdout = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(patch.object(review.sys, 'argv', ['review', '--input', str(script)]))
            for name, value in (('get_runtime_data_dir', directory), ('load_app_config', config),
                                ('ensure_ideal_settings', (True, {}, 'fixture')),
                                ('get_active_llm_config', config['llm_remote']),
                                ('make_run_client', client),
                                ('ensure_cached_or_benchmarked_concurrency', 1),
                                ('get_current_status', {'loaded': False})):
                stack.enter_context(patch.object(review, name, return_value=value))
            stack.enter_context(patch.object(review, 'wait_for_vram_headroom',
                                             side_effect=AssertionError('No GPU calls')))
            stack.enter_context(patch('generate_script.time.sleep'))
            review.main()
        saved = json.loads(Path(script).read_text(encoding='utf-8'))
        completed = Path(str(script) + '.review_completed.json').exists()
        return saved, stdout.getvalue(), calls, completed


class ScopedReviewSourceSpanTests(unittest.TestCase):
    def test_scoped_policy_captures_the_full_source_for_span_verification(self):
        batch, _ = make_scoped_book()
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=RAW, source=batch)
            self.assertEqual(RAW, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
            snapshot = review.get_review_source_snapshot(
                directory, str(script), script.read_bytes(), entries)
            self.assertEqual(hashlib.sha256(RAW.encode()).hexdigest(),
                             snapshot['identity']['sources'][0]['source_sha256'])

    def test_cli_accepts_the_exact_attributed_partition_with_scoped_provenance(self):
        batch, _ = make_scoped_book()
        reply = split_reply(batch)
        saved, stdout, calls, completed = run_cli(batch, [reply])
        self.assertEqual(1, len(calls))
        self.assertNotIn('review_source_span_unverified', stdout)
        self.assertEqual([
            'A bell rang.', 'She waited.', 'Nothing happened.', 'The door closed.',
            'At last the ledger was balanced.\n\nAfterward.',
            'HARK—!', 'Night fell.', 'A soft chime sounded.',
        ], [entry['text'] for entry in saved])
        self.assertEqual(NEW_SPEAKER, saved[5]['speaker'])
        self.assertEqual({**ANNOTATIONS, 'spoken': True},
                         {key: saved[5][key] for key in (*ANNOTATIONS, 'spoken')})
        self.assertEqual('Reflective.', saved[4]['instruct'])
        self.assertIs(False, saved[4]['spoken'])
        self.assertEqual({'section': 7}, saved[4]['provenance'])
        self.assertEqual({'section': 7}, saved[5]['provenance'])
        self.assertIn(SCRIPT_POLICY_KEY, saved[0])
        self.assertEqual(batch[0][SCRIPT_POLICY_KEY], saved[0][SCRIPT_POLICY_KEY])
        parent = batch[4]['source_span']
        contained = [entry for entry in saved
                     if parent[0] <= entry['source_span'][0] < parent[1]]
        self.assertEqual(2, len(contained))
        for entry in contained:
            start, end = entry['source_span']
            self.assertLessEqual(parent[0], start)
            self.assertLessEqual(end, parent[1])
            self.assertEqual(_normalize(RAW[start:end]), _normalize(entry['text']))
        self.assertLessEqual(contained[0]['source_span'][1], contained[1]['source_span'][0])
        self.assertFalse(RAW[contained[0]['source_span'][1]:contained[1]['source_span'][0]].strip())
        self.assertTrue(completed)

    def test_binding_rejects_altered_reordered_duplicated_and_dropped_partitions(self):
        batch, _ = make_scoped_book()
        good = split_reply(batch)
        variants = {
            'altered': [good[0], good[1], good[2], good[3], good[4],
                        {**good[5], 'text': 'HARK—?'}, good[6], good[7]],
            'reordered': [good[0], good[1], good[2], good[3], good[5], good[4], good[6], good[7]],
            'duplicated': good[:6] + [copy.deepcopy(good[5])] + good[6:],
            'dropped': good[:5] + good[6:],
        }
        for name, reply in variants.items():
            with self.subTest(variant=name):
                output, quality = review.get_review_source_binding(batch, reply, source_text=RAW)
                self.assertIsNone(output)
                self.assertFalse(quality['passed'])
                self.assertTrue(quality['findings'])

    def test_stale_or_conflicting_scope_provenance_still_refuses_capture(self):
        batch, _ = make_scoped_book()
        mutations = {}
        stale_full = copy.deepcopy(batch)
        changed = stale_full[0][SCRIPT_POLICY_KEY]['scope_source']['prepared_source_sha256']
        stale_full[0][SCRIPT_POLICY_KEY]['scope_source']['prepared_source_sha256'] = (
            '0' * 64 if changed != '0' * 64 else '1' * 64)
        mutations['stale_full_source'] = stale_full
        stale_scope = copy.deepcopy(batch)
        stale_scope[0][SCRIPT_POLICY_KEY]['source_sha256'] = '2' * 64
        mutations['stale_selected_source'] = stale_scope
        absent_scope = copy.deepcopy(batch)
        del absent_scope[0][SCRIPT_POLICY_KEY]['scope_source']
        mutations['absent_scope'] = absent_scope
        conflicting_scope = copy.deepcopy(batch)
        conflicting_scope[0][SCRIPT_POLICY_KEY]['scope_source']['start_word'] = 0
        mutations['conflicting_scope'] = conflicting_scope
        for name, script in mutations.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory:
                script_path, _, entries = create_book(directory, raw=RAW, source=script)
                self.assertIsNone(review.get_review_source_text(
                    directory, str(script_path), script_path.read_bytes(), entries))

    def test_changed_source_file_refuses_capture_with_scope_policy(self):
        batch, _ = make_scoped_book()
        with tempfile.TemporaryDirectory() as directory:
            script, source_path, entries = create_book(directory, raw=RAW, source=batch)
            source_path.write_text(RAW.replace('A bell rang.', 'A bell tolled.'), encoding='utf-8')
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))


def build_scoped_rows(raw, source_rows, start_entry, *, input_sha256=None, strip=None,
                      ancestor=None, chunks=None):
    """Synthetic retained rows and a scoped policy over a chosen prepared source."""
    marked = mark_entries(source_rows, raw)
    words = list(_words(raw))
    start = sum(len(list(_words(entry['text']))) for entry in marked[:start_entry])
    selected = ' '.join(words[start:])
    retained = [{key: value for key, value in entry.items() if key != SCRIPT_POLICY_KEY}
                for entry in marked[start_entry:]]
    parent = get_literal_policy(bind_literal_policy(raw, marked))
    policy = get_literal_policy(bind_literal_policy(
        selected, retained, input_sha256=input_sha256, strip_front_matter=strip))
    policy.update(boundary='selected_source_words_v1',
                  scope_chunks=chunks if chunks is not None else {
                      'chunk-1': {'version': 'literal_words_v1',
                                  'boundary': 'before_speech_preparation',
                                  'projection': 'existing_project_speakable_entries_v1',
                                  'source_policy_sha256': get_literal_policy_identity(ancestor or parent),
                                  'narration_words': get_word_identity([selected])}},
                  scope_source={'parent_policy_sha256': get_literal_policy_identity(ancestor or parent),
                                'prepared_source_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                                'start_word': start, 'end_word': len(words),
                                'total_words': len(words),
                                'script_start_entry': max(0, start_entry
                                - (ancestor['scope_source'].get('script_start_entry', 0)
                                   if ancestor and ancestor['boundary'] == 'selected_source_words_v1'
                                   else 0))})
    retained[0][SCRIPT_POLICY_KEY] = policy
    return retained, parent


def add_scope_evidence(script, raw, parent_rows, active_rows):
    """Attach producer-derived cut evidence the way scope apply does.

    The cut is located by matching the parent's ordered suffix to the child's
    rows, never by trusting the child's recorded ``script_start_entry``.
    """
    texts = [row['text'] for row in active_rows]
    cut = next((index for index in range(len(parent_rows))
                if [row['text'] for row in parent_rows[index:]] == texts), None)
    if cut is None:
        return None
    policy = get_literal_policy(active_rows)
    parent_policy = get_literal_policy(parent_rows)
    if parent_policy is None:
        parent_rows = bind_literal_policy(raw, parent_rows)
        parent_policy = get_literal_policy(parent_rows)
    prepared = (get_preprocessed_source(raw, strip_front_matter=True)[0]
                if policy is not None and policy.get('strip_front_matter') is True else raw)
    try:
        evidence = get_scope_cut_evidence(
            prepared, parent_rows, cut, parent_policy=parent_policy, policy=policy)
    except ValueError:
        return None
    if evidence is None:
        return None
    evidence['parent_policy_sha256'] = get_literal_policy_identity(parent_policy)
    binding_path = script.with_suffix('.selection_binding.json')
    binding_path.write_bytes(attach_scope_cut_evidence(binding_path.read_bytes(), evidence))
    return evidence


def create_scoped_book(directory, *, raw, parent_rows, active_rows, evidence_rows=None):
    """Direct book carrying genuine producer-derived frozen scope evidence."""
    script, source, entries = create_book(directory, raw=raw, source=active_rows)
    add_scope_evidence(script, raw, parent_rows,
                       active_rows if evidence_rows is None else evidence_rows)
    return script, source, entries


def make_saved_hop(directory, *, raw, original_rows, active_rows, evidence_rows=None):
    """A saved book whose selected source is another saved script.

    ``evidence_rows`` is the unmodified scoped child the scope-apply transaction
    would have certified. A later forged active script keeps that evidence, so
    tampering with the child cannot rewrite its own authority.
    """
    original_script, source_path, _ = create_book(
        directory, raw=raw, source=original_rows, saved=True)
    root = Path(directory)
    active = root / 'scripts' / 'active.json'
    published = json.dumps(active_rows).encode('utf-8')
    active.write_bytes(published)
    binding = make_script_selection_binding(
        get_book_operation_token(directory),
        hashlib.sha256(original_script.read_bytes()).hexdigest(),
        published, source_path=str(original_script))
    active.with_suffix('.selection_binding.json').write_text(json.dumps(binding), encoding='utf-8')
    state_path = root / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['input_file_path'] = str(original_script)
    state_path.write_text(json.dumps(state), encoding='utf-8')
    add_scope_evidence(active, raw, original_rows,
                       active_rows if evidence_rows is None else evidence_rows)
    return active, original_script, source_path


def run_cli_at(directory, script, replies, *, raw=RAW, config=None):
    """Review a prepared book in place, returning its saved script and output."""
    client, calls = client_for([(reply, 'stop') for reply in replies])
    config = config or {'llm_mode': 'remote',
                        'llm_remote': {'model_name': 'fixture',
                                       'base_url': 'https://api.example.invalid/v1'},
                        'generation': {'review_batch_size': 32, 'merge_narrators': False}}
    stdout = io.StringIO()
    before = script.read_bytes()
    with ExitStack() as stack:
        stack.enter_context(redirect_stdout(stdout))
        stack.enter_context(patch.object(review.sys, 'argv', ['review', '--input', str(script)]))
        for name, value in (('get_runtime_data_dir', directory), ('load_app_config', config),
                            ('ensure_ideal_settings', (True, {}, 'fixture')),
                            ('get_active_llm_config', config['llm_remote']),
                            ('make_run_client', client),
                            ('ensure_cached_or_benchmarked_concurrency', 1),
                            ('get_current_status', {'loaded': False})):
            stack.enter_context(patch.object(review, name, return_value=value))
        stack.enter_context(patch.object(review, 'wait_for_vram_headroom',
                                         side_effect=AssertionError('No GPU calls')))
        stack.enter_context(patch('generate_script.time.sleep'))
        review.main()
    return json.loads(script.read_bytes()), stdout.getvalue(), calls, script.read_bytes() != before


class ScopedRowBindingFindingsTests(unittest.TestCase):
    """Astra #2225 finding 1: rows bind to selected suffix word coordinates."""

    def test_duplicated_prefix_cannot_substitute_for_selected_suffix(self):
        raw = ('Repeat me.\n\nThe lantern glowed.\n\nRepeat me.\n\nAfterward.')
        rows = [row('Repeat me.', 'NARRATOR'), row('The lantern glowed.', 'NARRATOR'),
                row('Repeat me.', 'NARRATOR'), row('Afterward.', 'NARRATOR')]
        marked = mark_entries(rows, raw)
        self.assertNotEqual(marked[0]['source_span'], marked[2]['source_span'])
        retained, _ = build_scoped_rows(raw, rows, 2)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=retained)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
        substituted = copy.deepcopy(retained)
        substituted[0]['source_span'] = list(marked[0]['source_span'])
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=substituted)
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))

    def test_literal_offsets_survive_nfc_length_and_typographic_apostrophes(self):
        sample = 'Cafe\u0301 reopens.\n\nDon\u2019t stop.'
        words = list(iter_literal_words(sample))
        self.assertEqual('Caf\u00e9', words[0][0])
        self.assertEqual('Cafe\u0301', sample[words[0][1]:words[0][2]])
        self.assertIn("Don't", [word for word, _, _ in words])

    def test_punctuation_and_unicode_rows_capture_with_suffix_coordinates(self):
        raw = 'Cafe\u0301 reopens.\n\nDon\u2019t stop.\n\nThe bell rang.'
        rows = [row('Cafe\u0301 reopens.', 'NARRATOR'), row('Don\u2019t stop.', 'NARRATOR'),
                row('The bell rang.', 'NARRATOR')]
        retained, _ = build_scoped_rows(raw, rows, 1)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=retained)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))

    def test_inherited_intervals_resolve_before_suffix_binding(self):
        raw = 'Removed.\n\nFirst part.\nSecond part.\n\nAfterward.'
        rows = [row('Removed.', 'NARRATOR'), row('First part.', 'NARRATOR'),
                row('Second part.', 'NARRATOR'), row('Afterward.', 'NARRATOR')]
        retained, _ = build_scoped_rows(raw, rows, 1)
        inherited = copy.deepcopy(retained)
        parent = [raw.index('First part.'), raw.index('\n\nAfterward.')]
        inherited[0]['source_span'] = list(parent)
        inherited[1]['source_span'] = list(parent)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=inherited)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
            resolved = review.get_review_source_entries(entries, raw)
            self.assertNotEqual(resolved[0]['source_span'], resolved[1]['source_span'])


class SavedSourceHopFindingsTests(unittest.TestCase):
    """Astra #2225 finding 2: saved-source hop inherits verified policy lineage."""

    def setUp(self):
        self.rows = mark_entries(copy.deepcopy(RAW_ROWS), RAW)

    def test_valid_chain_carries_terminal_raw_identity_and_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            original, _ = build_scoped_rows(RAW, self.rows, 1, input_sha256=hashlib.sha256(RAW.encode()).hexdigest())
            active, saved, source = make_saved_hop(
                directory, raw=RAW, original_rows=original, active_rows=copy.deepcopy(original))
            snapshot = review.get_review_source_snapshot(
                directory, str(active), active.read_bytes(), copy.deepcopy(original))
            self.assertIsNotNone(snapshot)
            self.assertEqual(RAW, snapshot['text'])
            terminal = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertIn(terminal, [proof['source_sha256']
                                     for proof in snapshot['identity']['sources']])
            self.assertEqual(original[0]['source_span'],
                             review.get_review_source_entries(copy.deepcopy(original), RAW)[0]['source_span'])

    def test_descendant_policy_is_accepted_and_unscoped_legacy_chain_stays_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            original, _ = build_scoped_rows(RAW, self.rows, 1)
            child, _ = build_scoped_rows(RAW, self.rows, 2,
                                         ancestor=get_literal_policy(original))
            align_cut(child, original)
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=original, active_rows=child)
            self.assertEqual(RAW, review.get_review_source_text(
                directory, str(active), active.read_bytes(), child))
        with tempfile.TemporaryDirectory() as directory:
            legacy = [copy.deepcopy(entry) for entry in self.rows]
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=self.rows, active_rows=legacy)
            self.assertEqual(RAW, review.get_review_source_text(
                directory, str(active), active.read_bytes(), legacy))

    def test_absent_conflicting_or_stale_inherited_provenance_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            original, _ = build_scoped_rows(RAW, self.rows, 1)
            absent = [{key: value for key, value in entry.items() if key != SCRIPT_POLICY_KEY}
                      for entry in original]
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=original, active_rows=absent)
            self.assertIsNone(review.get_review_source_text(
                directory, str(active), active.read_bytes(), absent))
        with tempfile.TemporaryDirectory() as directory:
            original, _ = build_scoped_rows(RAW, self.rows, 1)
            conflicting = copy.deepcopy(original)
            conflicting[0][SCRIPT_POLICY_KEY]['source_sha256'] = '4' * 64
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=original, active_rows=conflicting)
            self.assertIsNone(review.get_review_source_text(
                directory, str(active), active.read_bytes(), conflicting))
        with tempfile.TemporaryDirectory() as directory:
            original, _ = build_scoped_rows(
                RAW, self.rows, 1, input_sha256=hashlib.sha256(RAW.encode()).hexdigest())
            stale = copy.deepcopy(original)
            stale[0][SCRIPT_POLICY_KEY]['input_sha256'] = '5' * 64
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=original, active_rows=stale)
            self.assertIsNone(review.get_review_source_text(
                directory, str(active), active.read_bytes(), stale))

    def test_refused_hop_does_not_publish_the_split(self):
        with tempfile.TemporaryDirectory() as directory:
            original, _ = build_scoped_rows(RAW, self.rows, 1)
            absent = [{key: value for key, value in entry.items() if key != SCRIPT_POLICY_KEY}
                      for entry in original]
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=original, active_rows=absent)
            reply = [{key: value for key, value in entry.items()
                      if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in absent[:1]]
            narrator = {key: value for key, value in absent[1].items() if key not in ('text',)}
            reply += [{**narrator, 'text': 'At last the ledger was balanced.\n\nAfterward.'},
                      {**narrator, 'speaker': NEW_SPEAKER, 'text': 'HARK—!', **ANNOTATIONS},
                      {key: value for key, value in absent[2].items() if key != 'source_span'}]
            saved, stdout, _, _ = run_cli_at(directory, active, [reply])
            self.assertNotIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
            self.assertEqual([entry['text'] for entry in absent],
                             [entry['text'] for entry in saved])


class ScopedPreparationFindingsTests(unittest.TestCase):
    """Astra #2225 finding 3: explicit strip choice, legacy tries both safely."""

    def setUp(self):
        self.header = ('Manifesto.\nA synthetic translator note.\n'
                       'Original Web Novel Chapter ― Complete.\n'
                       'Original Translation by Tester.\n\n')
        self.full = self.header + RAW
        self.prepared = get_preprocessed_source(self.full, strip_front_matter=True)[0]
        self.rows = [row(part, 'NARRATOR') for part in self.prepared.split('\n\n')]
        self.raw_rows = mark_entries(self.rows, self.prepared)

    def test_explicit_strip_true_captures_removable_header(self):
        retained, _ = build_scoped_rows(self.prepared, self.raw_rows, 1, strip=True)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=self.full, source=retained)
            self.assertEqual(self.prepared, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))

    def test_contradictory_strip_false_is_refused_when_only_stripped_matches(self):
        retained, _ = build_scoped_rows(self.prepared, self.raw_rows, 1, strip=False)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=self.full, source=retained)
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))

    def test_legacy_absence_tries_both_and_invalid_alternate_cannot_abort(self):
        retained, _ = build_scoped_rows(self.prepared, self.raw_rows, 1)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=self.full, source=retained)
            self.assertEqual(self.prepared, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
        full_rows = mark_entries([row(part, 'NARRATOR')
                                  for part in self.full.split('\n\n')], self.full)
        full_scoped, _ = build_scoped_rows(self.full, full_rows, 1)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=self.full, source=full_scoped)
            original_prepare = get_preprocessed_source

            def refusing_prepare(text, strip_front_matter=True):
                if strip_front_matter:
                    raise ValueError('invalid alternate preparation')
                return original_prepare(text, strip_front_matter=strip_front_matter)

            with patch('generate_script.get_preprocessed_source', side_effect=refusing_prepare):
                self.assertEqual(self.full, review.get_review_source_text(
                    directory, str(script), script.read_bytes(), entries))


class ScopedMarkerFindingsTests(unittest.TestCase):
    """Astra #2225 finding 4: scope-chunk markers require native shape."""

    def rows(self, raw):
        return [row(part, 'NARRATOR') for part in raw.split('\n\n')]

    def test_empty_and_malformed_markers_are_refused(self):
        raw = 'Removed line.\n\nFirst retained line.\n\nSecond retained line.\n\nAfterward.'
        variants = {}
        variants['empty'] = {}
        good = {'version': 'literal_words_v1', 'boundary': 'before_speech_preparation',
                'projection': 'existing_project_speakable_entries_v1',
                'source_policy_sha256': 'a' * 64,
                'narration_words': get_word_identity(['First retained line. Second retained line. Afterward.'])}
        for name, mutation in (('missing_version', {'version': None}),
                               ('wrong_boundary', {'boundary': 'narration'}),
                               ('wrong_projection', {'projection': 'other'}),
                               ('bad_digest', {'source_policy_sha256': 'xyz'}),
                               ('empty_words', {'narration_words': {'words': 0, 'sha256': 'a' * 64}}),
                               ('extra_field', {'extra': True})):
            marker = dict(good)
            marker.update(mutation)
            variants[name] = {'chunk-1': marker}
        for name, chunks in variants.items():
            with self.subTest(marker=name):
                retained, _ = build_scoped_rows(raw, self.rows(raw), 1, chunks=chunks)
                with tempfile.TemporaryDirectory() as directory:
                    script, _, entries = create_book(directory, raw=raw, source=retained)
                    self.assertIsNone(review.get_review_source_text(
                        directory, str(script), script.read_bytes(), entries))

    def test_repeated_scoping_keeps_ancestor_marker_hashes_valid(self):
        source = ('First line.\nSecond line.\nThird line.\n\n'
                  'Fourth line.\nFifth line.\nSixth line.')
        rows = mark_entries([row(text, 'NARRATOR') for text in
                             ('First line.', 'Second line.', 'Third line.',
                              'Fourth line.', 'Fifth line.', 'Sixth line.')], source)
        digest = hashlib.sha256(source.encode()).hexdigest()
        original = bind_literal_policy(source, rows, input_sha256=digest)
        chunks = bind_literal_chunks(
            [{'id': index, 'uid': f'uid-{index}', 'text': entry['text']}
             for index, entry in enumerate(rows)], get_literal_policy(original))
        first = get_literal_scope(source, original, chunks, 3, 3, input_sha256=digest)
        first_policy = get_literal_policy(first)
        marker = next(iter(first_policy['scope_chunks'].values()))
        self.assertNotEqual(marker['source_policy_sha256'],
                            get_literal_policy_identity(first_policy))
        second = get_literal_scope(source, first, chunks[3:], 2, 2, input_sha256=digest)
        second_policy = get_literal_policy(second)
        self.assertEqual('selected_source_words_v1', second_policy['boundary'])
        self.assertEqual(marker['source_policy_sha256'],
                         next(iter(second_policy['scope_chunks'].values()))['source_policy_sha256'])
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=source, source=second)
            self.assertEqual(source, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))


class ResidualAstraFindingsTests(unittest.TestCase):
    """CLI publication and raw-offset cases from the second Astra pass."""

    def setUp(self):
        self.rows = mark_entries(copy.deepcopy(RAW_ROWS), RAW)

    def split(self, batch, index=None):
        if index is None:
            index = next(position for position, entry in enumerate(batch)
                         if '\n\n' in entry['text'])
        narrator = {key: value for key, value in batch[index].items()
                    if key not in ('text', SCRIPT_POLICY_KEY)}
        paragraphs = batch[index]['text'].split('\n\n')
        reply = [{key: value for key, value in entry.items()
                  if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in batch[:index]]
        reply += [{**narrator, 'text': '\n\n'.join(paragraphs[:-1])},
                  {**narrator, 'speaker': NEW_SPEAKER, 'text': paragraphs[-1],
                   'instruct': 'Startled shout.', **ANNOTATIONS}]
        reply += [{key: value for key, value in entry.items()
                   if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in batch[index + 1:]]
        return reply

    def publish(self, directory, script, batch, *, expect_published):
        saved, stdout, _, _ = run_cli_at(directory, script, [self.split(batch)])
        published = NEW_SPEAKER in [entry['speaker'] for entry in saved]
        receipt = Path(str(script) + '.review_completed.json').exists()
        self.assertEqual(expect_published, published, stdout[-800:])
        self.assertEqual(expect_published, receipt, stdout[-800:])

    def test_child_range_widening_is_refused_without_split_or_receipt(self):
        parent_rows, _ = build_scoped_rows(RAW, self.rows, 2)
        parent = get_literal_policy(parent_rows)
        child_rows, _ = build_scoped_rows(RAW, self.rows, 1, ancestor=parent)
        align_cut(child_rows, parent_rows)
        parent_scope = parent['scope_source']
        child_scope = child_rows[0][SCRIPT_POLICY_KEY]['scope_source']
        self.assertLess(child_scope['start_word'], parent_scope['start_word'])
        with tempfile.TemporaryDirectory() as directory:
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=parent_rows, active_rows=child_rows)
            self.assertIsNone(review.get_review_source_text(
                directory, str(active), active.read_bytes(), child_rows))
            self.publish(directory, active, child_rows, expect_published=False)

    def test_unrelated_well_shaped_marker_is_refused_without_split_or_receipt(self):
        parent_rows, _ = build_scoped_rows(RAW, self.rows, 2)
        parent = get_literal_policy(parent_rows)
        selected = ' '.join(list(_words(RAW))[get_literal_policy(parent_rows)['scope_source']['start_word']:])
        forged = {'chunk-1': {'version': 'literal_words_v1',
                              'boundary': 'before_speech_preparation',
                              'projection': 'existing_project_speakable_entries_v1',
                              'source_policy_sha256': 'a' * 64,
                              'narration_words': get_word_identity([selected])}}
        child_rows, _ = build_scoped_rows(RAW, self.rows, 2, ancestor=parent, chunks=forged)
        with tempfile.TemporaryDirectory() as directory:
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=parent_rows, active_rows=child_rows)
            self.assertIsNone(review.get_review_source_text(
                directory, str(active), active.read_bytes(), child_rows))
            self.publish(directory, active, child_rows, expect_published=False)

    def test_native_grandchild_keeps_publication(self):
        digest = hashlib.sha256(RAW.encode()).hexdigest()
        original = bind_literal_policy(RAW, self.rows, input_sha256=digest)
        chunks = bind_literal_chunks(
            [{'id': index, 'uid': f'uid-{index}', 'text': entry['text']}
             for index, entry in enumerate(self.rows)], get_literal_policy(original))
        first = get_literal_scope(RAW, original, chunks, 1, 1, input_sha256=digest)
        second = get_literal_scope(RAW, first, chunks[1:], 1, 1, input_sha256=digest)
        with tempfile.TemporaryDirectory() as directory:
            active, _, _ = make_saved_hop(
                directory, raw=RAW, original_rows=first, active_rows=second)
            self.assertEqual(RAW, review.get_review_source_text(
                directory, str(active), active.read_bytes(), second))
            self.publish(directory, active, second, expect_published=True)

    def test_saved_child_strip_choice_must_derive_the_terminal_text(self):
        header = ('Manifesto.\nA synthetic translator note.\n'
                  'Original Web Novel Chapter ― Complete.\n'
                  'Original Translation by Tester.\n\n')
        full = header + RAW
        prepared = get_preprocessed_source(full, strip_front_matter=True)[0]
        source_rows = mark_entries([row(part, 'NARRATOR') for part in prepared.split('\n\n')], prepared)
        parent_rows, _ = build_scoped_rows(prepared, source_rows, 1, strip=True)
        conflicting, _ = build_scoped_rows(prepared, source_rows, 1, strip=False)
        with tempfile.TemporaryDirectory() as directory:
            active, _, _ = make_saved_hop(
                directory, raw=full, original_rows=parent_rows, active_rows=conflicting)
            self.assertIsNone(review.get_review_source_text(
                directory, str(active), active.read_bytes(), conflicting))
        matching, _ = build_scoped_rows(prepared, source_rows, 1, strip=True)
        with tempfile.TemporaryDirectory() as directory:
            active, _, _ = make_saved_hop(
                directory, raw=full, original_rows=parent_rows, active_rows=matching)
            self.assertEqual(prepared, review.get_review_source_text(
                directory, str(active), active.read_bytes(), matching))

    def test_legacy_parent_keeps_both_valid_strip_choices(self):
        source_rows = mark_entries(copy.deepcopy(RAW_ROWS), RAW)
        parent_rows, _ = build_scoped_rows(RAW, source_rows, 1)
        parent = get_literal_policy(parent_rows)
        for choice in (False, True):
            child_rows, _ = build_scoped_rows(RAW, source_rows, 1, strip=choice, ancestor=parent)
            align_cut(child_rows, parent_rows)
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as directory:
                active, _, _ = make_saved_hop(
                    directory, raw=RAW, original_rows=parent_rows, active_rows=child_rows)
                self.assertEqual(RAW, review.get_review_source_text(
                    directory, str(active), active.read_bytes(), child_rows))

    def test_selected_span_starts_after_excluded_words_including_wordless_rows(self):
        raw = '! Exclude me ! Keep the line.\n\nAfterward.'
        rows = [row('!', 'NARRATOR'), row('Exclude', 'NARRATOR'), row('me', 'NARRATOR'),
                row('!', 'NARRATOR'), row('Keep the line.', 'NARRATOR'),
                row('Afterward.', 'NARRATOR')]
        retained, _ = build_scoped_rows(raw, rows, 3)
        self.assertEqual(raw[13:14], '!')
        retained[0]['source_span'] = [13, 14]
        marked = mark_entries(rows, raw)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_scoped_book(
                directory, raw=raw, parent_rows=marked, active_rows=retained)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
        stale = copy.deepcopy(retained)
        stale[0]['source_span'] = [0, 1]
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_scoped_book(
                directory, raw=raw, parent_rows=marked, active_rows=stale,
                evidence_rows=retained)
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))

    def test_shared_iterator_handles_decomposed_hangul_and_other_scripts(self):
        decomposed = 'Exclude me \u1100\u1161 Keep.'
        tokens = list(iter_literal_words(decomposed))
        self.assertIn(('가', 11, 13), tokens)
        precomposed = 'Exclude me 가 Keep.'
        self.assertIn(('가', 11, 12), list(iter_literal_words(precomposed)))
        for sample in ('Caf\u00e9 x', 'Cafe\u0301 x', 'Don\u2019t x',
                       'दिल्ली 中文', 'HARK—!'):
            normalized, spans = literal_fidelity._normalized_with_offsets(sample)
            self.assertEqual(unicodedata.normalize('NFC', sample).replace('’', "'"), normalized)
            self.assertEqual(len(normalized), len(spans))
            self.assertEqual(list(literal_fidelity._words(sample)),
                             [word for word, _, _ in iter_literal_words(sample)])

    def test_decomposed_hangul_row_keeps_two_character_offset(self):
        raw = 'Exclude me \u1100\u1161 Keep.\n\nAfterward.'
        rows = [row('Exclude', 'NARRATOR'), row('me', 'NARRATOR'),
                row('\u1100\u1161', 'NARRATOR'), row('Keep.', 'NARRATOR'),
                row('Afterward.', 'NARRATOR')]
        retained, _ = build_scoped_rows(raw, rows, 2)
        self.assertEqual([11, 13], retained[0]['source_span'])
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=retained)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))


class FinalAstraFindingsTests(unittest.TestCase):
    """Third Astra pass: attached punctuation, legacy NFC, and word-only memory."""

    LEGACY_TIBETAN_DIGEST = 'e57601c64aec392c11e07b6eba00d786014343760613090dd0669bee9f02b442'

    def punctuation_book(self, lead, kept):
        rows = [row(lead, 'NARRATOR'), row(kept, 'NARRATOR'),
                row('Keep the line.', 'NARRATOR'), *copy.deepcopy(RAW_ROWS[5:])]
        raw = '\n\n'.join(entry['text'] for entry in rows)
        return raw, rows

    def publish(self, directory, script, batch, *, expected):
        saved, stdout, _, _ = run_cli_at(directory, script, [self.split(batch)])
        published = NEW_SPEAKER in [entry['speaker'] for entry in saved]
        receipt = Path(str(script) + '.review_completed.json').exists()
        self.assertEqual(expected, published, stdout[-900:])
        self.assertEqual(expected, receipt, stdout[-900:])
        return saved

    def split(self, batch):
        index = next(i for i, entry in enumerate(batch) if '\n\n' in entry['text'])
        narrator = {key: value for key, value in batch[index].items()
                    if key not in ('text', SCRIPT_POLICY_KEY)}
        paragraphs = batch[index]['text'].split('\n\n')
        reply = [{key: value for key, value in entry.items()
                  if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in batch[:index]]
        reply += [{**narrator, 'text': '\n\n'.join(paragraphs[:-1])},
                  {**narrator, 'speaker': NEW_SPEAKER, 'text': paragraphs[-1],
                   'instruct': 'Startled shout.', **ANNOTATIONS}]
        reply += [{key: value for key, value in entry.items()
                   if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in batch[index + 1:]]
        return reply

    def test_attached_exclamation_is_not_a_retained_row(self):
        raw, rows = self.punctuation_book('Exclude me!', '!')
        retained, _ = build_scoped_rows(raw, rows, 1)
        marked = mark_entries(rows, raw)
        self.assertEqual('!', raw[10:11])
        self.assertEqual('!', raw[13:14])
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_scoped_book(
                directory, raw=raw, parent_rows=marked, active_rows=retained)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
        tampered = copy.deepcopy(retained)
        tampered[0]['source_span'] = [10, 11]
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_scoped_book(
                directory, raw=raw, parent_rows=marked, active_rows=tampered,
                evidence_rows=retained)
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
            self.publish(directory, script, tampered, expected=False)
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_scoped_book(
                directory, raw=raw, parent_rows=marked, active_rows=retained)
            saved = self.publish(directory, script, retained, expected=True)
            self.assertEqual([13, 14], saved[0]['source_span'])

    def test_attached_exclamation_in_saved_child_is_not_a_retained_row(self):
        raw, rows = self.punctuation_book('Exclude me!', '!')
        parent_rows, _ = build_scoped_rows(raw, rows, 1)
        parent = get_literal_policy(parent_rows)
        child_rows, _ = build_scoped_rows(raw, rows, 1, ancestor=parent)
        align_cut(child_rows, parent_rows)
        marked = mark_entries(rows, raw)
        with tempfile.TemporaryDirectory() as directory:
            active, original, _ = make_saved_hop(
                directory, raw=raw, original_rows=parent_rows, active_rows=child_rows)
            add_scope_evidence(original, raw, marked, parent_rows)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(active), active.read_bytes(), child_rows))
        tampered = copy.deepcopy(child_rows)
        tampered[0]['source_span'] = [10, 11]
        with tempfile.TemporaryDirectory() as directory:
            active, original, _ = make_saved_hop(
                directory, raw=raw, original_rows=parent_rows, active_rows=tampered,
                evidence_rows=child_rows)
            add_scope_evidence(original, raw, marked, parent_rows)
            self.assertIsNone(review.get_review_source_text(
                directory, str(active), active.read_bytes(), tampered))
            self.publish(directory, active, tampered, expected=False)
        with tempfile.TemporaryDirectory() as directory:
            active, original, _ = make_saved_hop(
                directory, raw=raw, original_rows=parent_rows, active_rows=child_rows)
            add_scope_evidence(original, raw, marked, parent_rows)
            saved = self.publish(directory, active, child_rows, expected=True)
            self.assertEqual([13, 14], saved[0]['source_span'])

    def test_legacy_tibetan_digest_and_pre_upgrade_policy_publication(self):
        token = '\u0f40\u0f74\u0f73'
        self.assertEqual(['\u0f40\u0f71\u0f72\u0f74'], list(_words(token)))
        self.assertEqual(self.LEGACY_TIBETAN_DIGEST,
                         get_word_identity([token])['sha256'])
        self.assertEqual(self.LEGACY_TIBETAN_DIGEST,
                         get_word_identity([unicodedata.normalize('NFC', token)])['sha256'])
        rows = [row('Excluded.', 'NARRATOR'), row(token, 'NARRATOR'),
                *copy.deepcopy(RAW_ROWS[5:])]
        raw = '\n\n'.join(entry['text'] for entry in rows)
        retained, _ = build_scoped_rows(raw, rows, 1)
        self.assertEqual([11, 14], retained[0]['source_span'])
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=retained)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
            saved = self.publish(directory, script, retained, expected=True)
            self.assertEqual(raw[11:14], saved[0]['text'])
            self.assertEqual([11, 14], saved[0]['source_span'])

    def test_word_only_path_stays_allocation_light_and_exact(self):
        import tracemalloc
        text = ('The quick brown fox jumps over the lazy dog. ' * 24000)[:1_000_000]
        expected = '62e28bf5e571396901d071a689c6685a5377f9a400c572fd7516aa74af076f2f'
        tracemalloc.start()
        try:
            digest = get_word_identity([text])['sha256']
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(expected, digest)
        self.assertLess(peak, 16 * 1024 * 1024)
        self.assertEqual([('x', 0, 1)], list(iter_literal_words('x')))


class OwnershipAndCompositionTests(unittest.TestCase):
    """Final Astra pass: punctuation ownership and combining-mark composition."""

    def build_contrasts(self, lead):
        rows = [row(lead, 'NARRATOR'), row('!', 'NARRATOR'),
                row('Keep the line.', 'NARRATOR'), *copy.deepcopy(RAW_ROWS[5:])]
        raw = '\n\n'.join(entry['text'] for entry in rows)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        whole = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [{'text': lead, 'uid': 'first'}, {'text': '! Keep the line.', 'uid': 'second'},
             *[{'text': entry['text'], 'uid': f'z{index}'}
               for index, entry in enumerate(whole[3:])]], get_literal_policy(whole))
        child = get_literal_scope(raw, whole, chunks, 1, 1, input_sha256=digest)
        return raw, whole, child

    def publish(self, directory, script, batch, *, expected):
        saved, stdout, _, _ = run_cli_at(directory, script, [self.split(batch)])
        published = NEW_SPEAKER in [entry['speaker'] for entry in saved]
        receipt = Path(str(script) + '.review_completed.json').exists()
        self.assertEqual(expected, published, stdout[-800:])
        self.assertEqual(expected, receipt, stdout[-800:])
        return saved

    def split(self, batch):
        index = next(i for i, entry in enumerate(batch) if '\n\n' in entry['text'])
        narrator = {key: value for key, value in batch[index].items()
                    if key not in ('text', SCRIPT_POLICY_KEY)}
        paragraphs = batch[index]['text'].split('\n\n')
        reply = [{key: value for key, value in entry.items()
                  if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in batch[:index]]
        reply += [{**narrator, 'text': '\n\n'.join(paragraphs[:-1])},
                  {**narrator, 'speaker': NEW_SPEAKER, 'text': paragraphs[-1],
                   'instruct': 'Startled shout.', **ANNOTATIONS}]
        reply += [{key: value for key, value in entry.items()
                   if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in batch[index + 1:]]
        return reply

    def test_whitespace_does_not_transfer_excluded_punctuation(self):
        for lead, wrong, right in (('Exclude me!', [10, 11], [13, 14]),
                                   ('Exclude me !', [11, 12], [14, 15])):
            raw, whole, child = self.build_contrasts(lead)
            self.assertEqual('!', raw[wrong[0]:wrong[1]])
            self.assertEqual('!', raw[right[0]:right[1]])
            for path, parent in (('direct', None), ('whole-parent', whole),
                                 ('scoped-parent', copy.deepcopy(child))):
                with self.subTest(lead=lead, path=path):
                    valid = copy.deepcopy(child)
                    with tempfile.TemporaryDirectory() as directory:
                        if parent is None:
                            script = create_scoped_book(
                                directory, raw=raw, parent_rows=whole,
                                active_rows=valid)[0]
                        else:
                            script, original, _ = make_saved_hop(
                                directory, raw=raw, original_rows=parent, active_rows=valid)
                            if path == 'scoped-parent':
                                add_scope_evidence(original, raw, whole, child)
                        saved = self.publish(directory, script, valid, expected=True)
                        self.assertEqual(right, saved[0]['source_span'])
                    tampered = copy.deepcopy(child)
                    tampered[0]['source_span'] = list(wrong)
                    with tempfile.TemporaryDirectory() as directory:
                        if parent is None:
                            script = create_scoped_book(
                                directory, raw=raw, parent_rows=whole,
                                active_rows=tampered, evidence_rows=child)[0]
                        else:
                            script, original, _ = make_saved_hop(
                                directory, raw=raw, original_rows=parent,
                                active_rows=tampered, evidence_rows=child)
                            if path == 'scoped-parent':
                                add_scope_evidence(original, raw, whole, child)
                        self.publish(directory, script, tampered, expected=False)

    def test_adjacent_retained_word_and_punctuation_is_valid(self):
        rows = [row(text, 'NARRATOR') for text in ('Excluded.', 'Stay', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace('Stay\n\n!', 'Stay!')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        whole = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [{'text': 'Excluded.', 'uid': 'first'},
             {'text': 'Stay! Keep the line.', 'uid': 'second'},
             *[{'text': entry['text'], 'uid': str(index)}
               for index, entry in enumerate(whole[4:])]], get_literal_policy(whole))
        child = get_literal_scope(raw, whole, chunks, 1, 1, input_sha256=digest)
        self.assertEqual([11, 15], child[0]['source_span'])
        self.assertEqual([15, 16], child[1]['source_span'])
        for path, parent in (('direct', None), ('saved', whole)):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                script = (create_book(directory, raw=raw, source=child)[0] if parent is None
                          else make_saved_hop(directory, raw=raw, original_rows=parent,
                                              active_rows=child)[0])
                saved = self.publish(directory, script, child, expected=True)
                self.assertEqual([15, 16], saved[1]['source_span'])

    def test_saved_parent_with_two_standalone_marks_keeps_only_retained_mark(self):
        rows = [row(text, 'NARRATOR') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace('Exclude me\n\n!', 'Exclude me !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        whole = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [{'text': 'Intro.', 'uid': 'intro'}, {'text': 'Exclude me !', 'uid': 'excluded'},
             {'text': '! Keep the line.', 'uid': 'retained'},
             *[{'text': entry['text'], 'uid': f'tail-{index}'}
               for index, entry in enumerate(whole[5:])]], get_literal_policy(whole))
        scoped = get_literal_scope(raw, whole, chunks, 1, 1, input_sha256=digest)
        for parent, parent_chunks, start, label in (
                (whole, chunks, 3, 'whole'), (scoped, chunks[1:], 2, 'scoped')):
            child = get_literal_scope(raw, parent, parent_chunks, start, start - 1,
                                      input_sha256=digest)
            excluded_span = list(parent[start - 1]['source_span'])
            retained_span = list(parent[start]['source_span'])
            self.assertEqual([19, 20], excluded_span)
            self.assertEqual([22, 23], retained_span)
            for span, expected in ((excluded_span, False), ([21, 23], False),
                                   ([22, 24], False), (retained_span, True)):
                with self.subTest(parent=label, span=span), tempfile.TemporaryDirectory() as directory:
                    entries = copy.deepcopy(child)
                    entries[0]['source_span'] = span
                    script, _, _ = make_saved_hop(
                        directory, raw=raw, original_rows=parent, active_rows=entries,
                        evidence_rows=child)
                    saved = self.publish(directory, script, entries, expected=expected)
                    if expected:
                        self.assertEqual(retained_span, saved[0]['source_span'])
                    else:
                        self.assertEqual(entries, saved)
            missing_parent = copy.deepcopy(parent)
            del missing_parent[start]['source_span']
            with self.subTest(parent=label, missing='parent'), tempfile.TemporaryDirectory() as directory:
                script, _, _ = make_saved_hop(
                    directory, raw=raw, original_rows=missing_parent, active_rows=child)
                self.publish(directory, script, child, expected=False)
        missing_child = copy.deepcopy(child)
        del missing_child[0]['source_span']
        with tempfile.TemporaryDirectory() as directory:
            script, _, _ = create_book(directory, raw=raw, source=missing_child)
            self.publish(directory, script, missing_child, expected=False)

    def test_combining_sequences_keep_legacy_digest_and_raw_offsets(self):
        for word, expected_span in (('a\u0327\u0301', [11, 14]), ('\u00e1\u0327', [11, 13])):
            with self.subTest(word=word):
                expected_digest = get_word_identity([word])['sha256']
                self.assertEqual(expected_digest, get_word_identity(
                    [unicodedata.normalize('NFC', word)])['sha256'])
                self.assertEqual(expected_span[1] - expected_span[0], len(word))
                rows = [row('Excluded.', 'NARRATOR'), row(word, 'NARRATOR'),
                        *copy.deepcopy(RAW_ROWS[5:])]
                raw = '\n\n'.join(entry['text'] for entry in rows)
                retained, _ = build_scoped_rows(raw, rows, 1)
                self.assertEqual(expected_span, retained[0]['source_span'])
                with tempfile.TemporaryDirectory() as directory:
                    script, _, entries = create_book(directory, raw=raw, source=retained)
                    self.assertEqual(raw, review.get_review_source_text(
                        directory, str(script), script.read_bytes(), entries))
                    saved = self.publish(directory, script, retained, expected=True)
                    self.assertEqual(word, saved[0]['text'])
                    self.assertEqual(expected_span, saved[0]['source_span'])


class CutEvidenceTests(unittest.TestCase):
    """Scope-recorded cut proves ownership of standalone marks."""

    def build(self, gap='\n\n'):
        rows = [row(text, 'NARRATOR', instruct='Neutral.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !').replace('!\n\nKeep', '!' + gap + 'Keep')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        whole = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = [dict(entry, id=index, uid=f'u{index}')
                  for index, entry in enumerate(group_into_chunks(whole))]
        first = get_scope_partition(whole, chunks, 1)['chunk_start']
        scoped = get_literal_scope(raw, whole, chunks, 1, first, input_sha256=digest)
        return raw, whole, chunks, scoped

    def publish(self, directory, script, entries, *, expected):
        saved, stdout, _, _ = run_cli_at(directory, script, [self.split(entries)])
        published = NEW_SPEAKER in [entry['speaker'] for entry in saved]
        receipt = Path(str(script) + '.review_completed.json')
        self.assertEqual(expected, published, stdout[-900:])
        self.assertEqual(expected, receipt.exists(), stdout[-900:])
        if receipt.exists():
            proof = json.loads(receipt.read_text(encoding='utf-8'))
            self.assertEqual(get_review_entries_fingerprint(entries), proof['input_sha256'])
            self.assertEqual(get_review_entries_fingerprint(saved), proof['output_sha256'])
        else:
            self.assertEqual(entries, saved)
        return saved

    def split(self, entries):
        index = next(i for i, entry in enumerate(entries) if '\n\n' in entry['text'])
        narrator = {key: value for key, value in entries[index].items()
                    if key not in ('text', SCRIPT_POLICY_KEY)}
        paragraphs = entries[index]['text'].split('\n\n')
        reply = [{key: value for key, value in entry.items()
                  if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in entries[:index]]
        reply += [{**narrator, 'text': '\n\n'.join(paragraphs[:-1])},
                  {**narrator, 'speaker': NEW_SPEAKER, 'text': paragraphs[-1],
                   'instruct': 'Startled shout.', **ANNOTATIONS}]
        reply += [{key: value for key, value in entry.items()
                   if key not in ('source_span', SCRIPT_POLICY_KEY)} for entry in entries[index + 1:]]
        return reply

    def test_inserted_excluded_mark_cannot_earn_a_receipt(self):
        raw, whole, chunks, scoped = self.build()
        for parent, parent_chunks, start, label in (
                (whole, chunks, 3, 'whole'), (scoped, chunks[1:], 2, 'scoped')):
            cut = get_scope_partition(parent, parent_chunks, start)['chunk_start']
            child = get_literal_scope(raw, parent, parent_chunks, start, cut,
                                      input_sha256=hashlib.sha256(raw.encode()).hexdigest())
            self.assertEqual([22, 23], child[0]['source_span'])
            inserted = copy.deepcopy(child)
            extra = copy.deepcopy(parent[start - 1])
            extra[SCRIPT_POLICY_KEY] = inserted[0].pop(SCRIPT_POLICY_KEY)
            inserted.insert(0, extra)
            self.assertEqual([19, 20], inserted[0]['source_span'])
            with self.subTest(parent=label), tempfile.TemporaryDirectory() as directory:
                script, _, _ = make_saved_hop(
                    directory, raw=raw, original_rows=parent, active_rows=inserted,
                    evidence_rows=child)
                self.assertIsNone(review.get_review_source_text(
                    directory, str(script), script.read_bytes(), inserted))
                self.publish(directory, script, inserted, expected=False)

    def test_retained_mark_without_gap_publishes(self):
        raw, whole, chunks, scoped = self.build(gap='')
        for parent, parent_chunks, start, label in (
                (whole, chunks, 3, 'whole'), (scoped, chunks[1:], 2, 'scoped')):
            cut = get_scope_partition(parent, parent_chunks, start)['chunk_start']
            child = get_literal_scope(raw, parent, parent_chunks, start, cut,
                                      input_sha256=hashlib.sha256(raw.encode()).hexdigest())
            self.assertEqual([22, 23], child[0]['source_span'])
            self.assertEqual([23, 37], child[1]['source_span'])
            for direct, path in ((True, 'direct'), (False, 'saved')):
                with self.subTest(parent=label, path=path), tempfile.TemporaryDirectory() as directory:
                    script = (create_scoped_book(directory, raw=raw, parent_rows=parent,
                                                 active_rows=child)[0] if direct else
                              make_saved_hop(directory, raw=raw, original_rows=parent,
                                             active_rows=child)[0])
                    saved = self.publish(directory, script, child, expected=True)
                    self.assertEqual([22, 23], saved[0]['source_span'])


class ScopeCutProofTests(unittest.TestCase):
    """The recorded cut is proven by native raw coverage, never by the child."""

    def setUp(self):
        self.cut = CutEvidenceTests()

    def build_child(self, raw, parent, parent_chunks, start, digest):
        cut = get_scope_partition(parent, parent_chunks, start)['chunk_start']
        return get_literal_scope(raw, parent, parent_chunks, start, cut,
                                 input_sha256=digest)

    def parents(self, raw, whole, chunks, scoped, digest):
        for parent, parent_chunks, start, label in (
                (whole, chunks, 3, 'whole'), (scoped, chunks[1:], 2, 'scoped')):
            yield parent, parent_chunks, start, label, self.build_child(
                raw, parent, parent_chunks, start, digest)

    def forged(self, parent, start, child, mode):
        entries = copy.deepcopy(child)
        scope = entries[0][SCRIPT_POLICY_KEY]['scope_source']
        extra = copy.deepcopy(parent[start - 1])
        extra[SCRIPT_POLICY_KEY] = entries[0].pop(SCRIPT_POLICY_KEY)
        entries.insert(0, extra)
        if mode == 'cut':
            scope['script_start_entry'] = start - 1
        elif mode == 'legacy':
            scope.pop('script_start_entry', None)
        return entries

    def test_forged_cut_cannot_publish_an_excluded_mark(self):
        raw, whole, chunks, scoped = self.cut.build()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        for parent, _chunks, start, label, child in self.parents(
                raw, whole, chunks, scoped, digest):
            for mode in ('cut', 'legacy', 'plain'):
                with self.subTest(parent=label, mode=mode), tempfile.TemporaryDirectory() as directory:
                    entries = self.forged(parent, start, child, mode)
                    script, _, _ = make_saved_hop(
                        directory, raw=raw, original_rows=parent, active_rows=entries,
                        evidence_rows=child)
                    self.assertIsNone(review.get_review_source_text(
                        directory, str(script), script.read_bytes(), entries))
                    self.cut.publish(directory, script, entries, expected=False)

    def test_direct_forged_cut_cannot_publish_an_excluded_mark(self):
        raw, whole, chunks, scoped = self.cut.build()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        _parent, _chunks, start, _label, child = next(self.parents(
            raw, whole, chunks, scoped, digest))
        entries = self.forged(whole, start, child, 'cut')
        with tempfile.TemporaryDirectory() as directory:
            script, _, _ = create_book(directory, raw=raw, source=entries)
            add_scope_evidence(script, raw, whole, child)
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
            self.cut.publish(directory, script, entries, expected=False)

    def test_valid_split_reuses_the_receipt_without_calls(self):
        raw, whole, chunks, scoped = self.cut.build()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        for parent, _chunks, _start, label, child in self.parents(
                raw, whole, chunks, scoped, digest):
            with self.subTest(parent=label), tempfile.TemporaryDirectory() as directory:
                script, _, _ = make_saved_hop(
                    directory, raw=raw, original_rows=parent, active_rows=child)
                saved, _out, _calls, _changed = run_cli_at(
                    directory, script, [self.cut.split(child)])
                self.assertIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
                self.assertEqual(raw, review.get_review_source_text(
                    directory, str(script), script.read_bytes(), saved))
                replay, output, calls, changed = run_cli_at(directory, script, [])
                self.assertIn('No changes since the completed review', output)
                self.assertEqual([], calls)
                self.assertFalse(changed)
                self.assertEqual(saved, replay)

    def test_zero_gap_retained_mark_publishes_and_reuses(self):
        raw, whole, chunks, scoped = self.cut.build(gap='')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        for parent, parent_chunks, start, label, child in self.parents(
                raw, whole, chunks, scoped, digest):
            self.assertEqual([22, 23], child[0]['source_span'])
            with self.subTest(parent=label), tempfile.TemporaryDirectory() as directory:
                script, _, _ = make_saved_hop(
                    directory, raw=raw, original_rows=parent, active_rows=child)
                saved = self.cut.publish(directory, script, child, expected=True)
                self.assertEqual([22, 23], saved[0]['source_span'])
                replay, output, calls, _changed = run_cli_at(directory, script, [])
                self.assertIn('No changes since the completed review', output)
                self.assertEqual([], calls)
                self.assertEqual(saved, replay)

    def test_malformed_proof_is_refused_without_downgrade(self):
        raw, whole, chunks, scoped = self.cut.build()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        _parent, _chunks, _start, _label, child = next(self.parents(
            raw, whole, chunks, scoped, digest))
        proof = child[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']
        self.assertEqual(1, proof['version'])
        variants = {}
        stale = copy.deepcopy(child)
        stale[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']['coverage_sha256'] = '0' * 64
        variants['stale_coverage'] = stale
        shifted = copy.deepcopy(child)
        shifted[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']['intervals'][0][0] -= 1
        variants['shifted_interval'] = shifted
        future = copy.deepcopy(child)
        future[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']['version'] = 2
        variants['future_version'] = future
        extra = copy.deepcopy(child)
        extra[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']['unexpected'] = True
        variants['extra_field'] = extra
        for name, entries in variants.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory:
                script, _, _ = create_book(directory, raw=raw, source=entries)
                self.assertIsNone(review.get_review_source_text(
                    directory, str(script), script.read_bytes(), entries))
        downgraded = copy.deepcopy(child)
        del downgraded[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']
        with tempfile.TemporaryDirectory() as directory:
            script, source, _ = create_book(directory, raw=raw, source=child)
            script.write_bytes(json.dumps(downgraded).encode('utf-8'))
            self.assertIsNone(review.get_review_source_text(
                directory, str(script), script.read_bytes(), downgraded))

    def test_coverage_digest_is_invariant_under_a_valid_split(self):
        raw, whole, chunks, scoped = self.cut.build()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        _parent, _chunks, _start, _label, child = next(self.parents(
            raw, whole, chunks, scoped, digest))
        proof = child[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']
        bound, quality = review.get_review_source_binding(
            child, self.cut.split(child), source_text=raw)
        self.assertTrue(quality['passed'])
        self.assertIsNotNone(bound)
        resolved = review.get_review_source_entries(copy.deepcopy(bound), raw)
        coverage = review.get_review_scope_coverage(raw, resolved)
        self.assertEqual(proof['coverage_sha256'], get_scope_coverage_digest(coverage))

    def test_real_scope_apply_persists_the_proof_and_review_still_captures_source(self):
        self.assert_scope_apply_and_undo(with_binding=True)

    def test_real_scope_apply_and_undo_with_no_selection_binding(self):
        self.assert_scope_apply_and_undo(with_binding=False)

    def assert_scope_apply_and_undo(self, *, with_binding):
        rows = [row(text, 'NARRATOR', instruct='Even.')
                for text in ('Intro.', 'Exclude me', 'Keep the line.', 'Afterward.')]
        raw = '\n\n'.join(entry['text'] for entry in rows)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        script = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(script))],
            get_literal_policy(script))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.txt'
            source.write_text(raw, encoding='utf-8')
            state = {'active_book_id': 'fixture', 'book_generation': 'g1',
                     'input_file_path': str(source)}
            for name, data in (('state.json', state), ('annotated_script.json', script),
                               ('voice_config.json', {}), ('chunks.json', chunks)):
                (root / name).write_text(json.dumps(data), encoding='utf-8')
            path = root / 'annotated_script.json'
            binding_path = root / BINDING_NAME
            if with_binding:
                binding = make_script_selection_binding(
                    get_book_operation_token(directory), digest, path.read_bytes(),
                    source_path=str(source))
                binding_path.write_text(json.dumps(binding), encoding='utf-8')
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 1)
                self.assertTrue(is_scope_cut_proof(plan['selected_script'][0][
                    SCRIPT_POLICY_KEY]['scope_source']['cut_proof']))
                apply_source_scope(root, 1, plan['revision'])
            applied = json.loads(path.read_bytes())
            proof = applied[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']
            self.assertTrue(is_scope_cut_proof(proof))
            if with_binding:
                self.assertEqual(raw, review.get_review_source_text(
                    directory, str(path), path.read_bytes(), applied))
            else:
                self.assertFalse(binding_path.exists())
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                from project_scope_apply import apply_source_scope_undo
                from project_scope_apply import get_source_scope_undo_preview
                undo = get_source_scope_undo_preview(root)
                apply_source_scope_undo(root, undo['revision'])
            self.assertEqual(script, json.loads(path.read_bytes()))
            if with_binding:
                self.assertIsNone(get_scope_cut_from_binding(
                    parse_script_binding(binding_path.read_bytes())))
            else:
                self.assertFalse(binding_path.exists())

    def test_scope_apply_evidence_gates_child_mutations_and_survives_controlled_edit(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me !', '!', 'Keep the line.', 'Afterward.')]
        raw = '\n\n'.join(entry['text'] for entry in rows)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        full = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(full))],
            get_literal_policy(full))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.txt'
            source.write_text(raw, encoding='utf-8')
            state = {'active_book_id': 'fixture', 'book_generation': 'g1',
                     'input_file_path': str(source)}
            for name, data in (('state.json', state), ('annotated_script.json', full),
                               ('voice_config.json', {}), ('chunks.json', chunks)):
                (root / name).write_text(json.dumps(data), encoding='utf-8')
            path = root / 'annotated_script.json'
            original_binding = make_script_selection_binding(
                get_book_operation_token(directory), digest, path.read_bytes(),
                source_path=str(source))
            (root / BINDING_NAME).write_bytes(
                json.dumps(original_binding, sort_keys=True, separators=(',', ':')).encode())
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 3)
                apply_source_scope(root, 3, plan['revision'])
            applied_bytes = path.read_bytes()
            applied_binding = (root / BINDING_NAME).read_bytes()
            applied = json.loads(applied_bytes)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(path), applied_bytes, applied))
            evidence = get_scope_cut_from_binding(
                parse_script_binding((root / BINDING_NAME).read_bytes()))
            self.assertIsNotNone(evidence)
            resolved = review.get_review_source_entries(copy.deepcopy(applied), raw)
            self.assertEqual(evidence['coverage_sha256'], get_scope_coverage_digest(
                review.get_review_scope_coverage(raw, resolved)))

            def reject(mutated):
                (root / BINDING_NAME).write_bytes(applied_binding)
                rebuilt = binding_for_controlled_edit(directory, applied_bytes,
                                                      json.dumps(mutated).encode())
                self.assertIsNotNone(get_scope_cut_from_binding(parse_script_binding(rebuilt)))
                (root / BINDING_NAME).write_bytes(rebuilt)
                self.assertIsNone(review.get_review_source_text(
                    directory, str(path), json.dumps(mutated).encode(), mutated))

            moved = copy.deepcopy(applied)
            scope = moved[0][SCRIPT_POLICY_KEY]['scope_source']
            del scope['cut_proof']
            scope['script_start_entry'] = 2
            insert = copy.deepcopy(full[2])
            insert[SCRIPT_POLICY_KEY] = moved[0].pop(SCRIPT_POLICY_KEY)
            moved.insert(0, insert)
            reject(moved)
            recomputed = copy.deepcopy(applied)
            recomputed[0]['source_span'] = [19, 20]
            proof = recomputed[0][SCRIPT_POLICY_KEY]['scope_source']['cut_proof']
            intervals = get_scope_coverage_intervals(
                raw, [row['source_span'] for row in recomputed])
            proof.update(intervals=intervals,
                         coverage_sha256=get_scope_coverage_digest(intervals))
            reject(recomputed)
            overrun = copy.deepcopy(applied)
            overrun[0]['source_span'][1] += 1
            reject(overrun)

            (root / BINDING_NAME).write_bytes(applied_binding)
            edited = copy.deepcopy(applied)
            edited[1]['instruct'] = 'Different.'
            rebuilt = binding_for_controlled_edit(directory, applied_bytes,
                                                  json.dumps(edited).encode())
            (root / BINDING_NAME).write_bytes(rebuilt)
            self.assertIsNotNone(get_scope_cut_from_binding(parse_script_binding(rebuilt)))
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(path), json.dumps(edited).encode(), edited))


    def test_evidence_requires_child_policy_even_for_equivalent_strip_choice(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        full = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(full))],
            get_literal_policy(full))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.txt'
            source.write_text(raw, encoding='utf-8')
            state = {'active_book_id': 'fixture', 'book_generation': 'g1',
                     'input_file_path': str(source)}
            for name, data in (('state.json', state), ('annotated_script.json', full),
                               ('voice_config.json', {}), ('chunks.json', chunks)):
                (root / name).write_text(json.dumps(data), encoding='utf-8')
            path = root / 'annotated_script.json'
            (root / BINDING_NAME).write_bytes(json.dumps(make_script_selection_binding(
                get_book_operation_token(directory), digest, path.read_bytes(),
                source_path=str(source)), sort_keys=True, separators=(',', ':')).encode())
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 3)
                apply_source_scope(root, 3, plan['revision'])
            applied_bytes = path.read_bytes()
            applied_binding = (root / BINDING_NAME).read_bytes()
            applied = json.loads(applied_bytes)
            self.assertEqual([22, 23], applied[0]['source_span'])
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(path), applied_bytes, applied))

            def refuse(mutated):
                path.write_bytes(applied_bytes)
                (root / BINDING_NAME).write_bytes(applied_binding)
                publish_controlled_script_edit(directory, applied_bytes,
                                               json.dumps(mutated).encode())
                self.assertIsNotNone(get_scope_cut_from_binding(
                    parse_script_binding((root / BINDING_NAME).read_bytes())))
                self.assertIsNone(review.get_review_source_text(
                    directory, str(path), json.dumps(mutated).encode(), mutated))
                saved, _out, _calls, _changed = run_cli_at(
                    directory, path, [CutEvidenceTests().split(mutated)])
                self.assertNotIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
                self.assertFalse(Path(str(path) + '.review_completed.json').exists())
                self.assertEqual(mutated, saved)

            removed = copy.deepcopy(applied)
            removed[0].pop(SCRIPT_POLICY_KEY)
            removed.insert(0, copy.deepcopy(full[2]))
            refuse(removed)

            toggled = copy.deepcopy(applied)
            policy = toggled[0][SCRIPT_POLICY_KEY]
            policy['strip_front_matter'] = not policy.get('strip_front_matter', False)
            refuse(toggled)

            inserted = copy.deepcopy(applied)
            extra = copy.deepcopy(full[2])
            extra[SCRIPT_POLICY_KEY] = inserted[0].pop(SCRIPT_POLICY_KEY)
            inserted.insert(0, extra)
            refuse(inserted)

            path.write_bytes(applied_bytes)
            (root / BINDING_NAME).write_bytes(applied_binding)
            genuine = copy.deepcopy(applied)
            genuine[1]['instruct'] = 'Different.'
            publish_controlled_script_edit(directory, applied_bytes, json.dumps(genuine).encode())
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(path), json.dumps(genuine).encode(), genuine))
            saved, _out, _calls, _changed = run_cli_at(
                directory, path, [CutEvidenceTests().split(genuine)])
            self.assertIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
            self.assertTrue(Path(str(path) + '.review_completed.json').exists())
            replay, output, calls, changed = run_cli_at(directory, path, [])
            self.assertEqual([], calls)
            self.assertFalse(changed)
            self.assertEqual(saved, replay)
            self.assertIn('No changes since the completed review', output)

    def test_producer_refuses_contradictory_parent_spans(self):
        self.assert_producer_refuses_parent_span(null_span=False)

    def test_producer_refuses_explicitly_null_discarded_span(self):
        self.assert_producer_refuses_parent_span(null_span=True)

    def assert_producer_refuses_parent_span(self, *, null_span):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        full = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        self.assertEqual([19, 20], full[2]['source_span'])
        if null_span:
            full[0]['source_span'] = None
        else:
            full[2]['source_span'] = [18, 19]
            full[3]['source_span'] = [19, 20]
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(full))],
            get_literal_policy(full))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.txt'
            source.write_text(raw, encoding='utf-8')
            state = {'active_book_id': 'fixture', 'book_generation': 'g1',
                     'input_file_path': str(source)}
            for name, data in (('state.json', state), ('annotated_script.json', full),
                               ('voice_config.json', {}), ('chunks.json', chunks)):
                (root / name).write_text(json.dumps(data), encoding='utf-8')
            path = root / 'annotated_script.json'
            (root / BINDING_NAME).write_bytes(json.dumps(make_script_selection_binding(
                get_book_operation_token(directory), digest, path.read_bytes(),
                source_path=str(source)), sort_keys=True, separators=(',', ':')).encode())
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                with self.assertRaises(ScopeSpanContradiction):
                    get_scope_preview(root, 3)
            self.assertEqual(full, json.loads(path.read_bytes()))
            self.assertFalse((root / '.project_scope_undo.json').exists())

    def _shared_span_fixture(self, directory, full, raw, chunks):
        root = Path(directory)
        source = root / 'source.txt'
        source.write_text(raw, encoding='utf-8')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        state = {'active_book_id': 'fixture', 'book_generation': 'g1',
                 'input_file_path': str(source)}
        for name, data in (('state.json', state), ('annotated_script.json', full),
                           ('voice_config.json', {}), ('chunks.json', chunks)):
            (root / name).write_text(json.dumps(data), encoding='utf-8')
        path = root / 'annotated_script.json'
        (root / BINDING_NAME).write_bytes(json.dumps(make_script_selection_binding(
            get_book_operation_token(directory), digest, path.read_bytes(),
            source_path=str(source)), sort_keys=True, separators=(',', ':')).encode())
        return path

    def test_shared_parent_span_straddling_cut_resolves_and_publishes(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        full = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        self.assertEqual([19, 20], full[2]['source_span'])
        self.assertEqual([22, 23], full[3]['source_span'])
        full[2]['source_span'] = [19, 23]
        full[3]['source_span'] = [19, 23]
        resolved = review.get_review_source_entries(full, raw)
        self.assertEqual([[19, 20], [22, 23]],
                         [entry['source_span'] for entry in resolved[2:4]])
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(full))],
            get_literal_policy(full))
        with tempfile.TemporaryDirectory() as directory:
            path = self._shared_span_fixture(directory, full, raw, chunks)
            root = Path(directory)
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 3)
                self.assertEqual(22, plan['scope_cut_evidence']['outer_start'])
                apply_source_scope(root, 3, plan['revision'])
            applied = json.loads(path.read_bytes())
            self.assertEqual([22, 23], applied[0]['source_span'])
            self.assertEqual('!', applied[0]['text'])
            self.assertEqual('!', raw[22:23])
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(path), path.read_bytes(), applied))
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                apply_source_scope_undo(root, get_source_scope_undo_preview(root)['revision'])
            self.assertEqual(full, json.loads(path.read_bytes()))
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                again = get_scope_preview(root, 3)
                apply_source_scope(root, 3, again['revision'])
            child = json.loads(path.read_bytes())
            saved, _out, _calls, _changed = run_cli_at(
                directory, path, [CutEvidenceTests().split(child)])
            self.assertIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
            self.assertTrue(Path(str(path) + '.review_completed.json').exists())
            replay, output, calls, changed = run_cli_at(directory, path, [])
            self.assertEqual([], calls)
            self.assertFalse(changed)
            self.assertEqual(saved, replay)
            self.assertIn('No changes since the completed review', output)

    def test_shared_parent_span_variants_are_refused(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        base = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        variants = {}
        clipped = copy.deepcopy(base)
        clipped[2]['source_span'] = [19, 22]
        clipped[3]['source_span'] = [19, 22]
        variants['clipped'] = clipped
        reordered = copy.deepcopy(base)
        reordered[2]['source_span'] = [22, 23]
        reordered[3]['source_span'] = [19, 20]
        variants['reordered'] = reordered
        overlap = copy.deepcopy(base)
        overlap[3]['source_span'] = [19, 20]
        variants['overlap'] = overlap
        for float_index in (2, 3):
            shared = copy.deepcopy(base)
            shared[2]['source_span'] = [19, 23]
            shared[3]['source_span'] = [19, 23]
            shared[float_index]['source_span'] = [19.0, 23.0]
            variants[f'float_member_{float_index}'] = shared
        for name, full in variants.items():
            with self.subTest(variant=name), tempfile.TemporaryDirectory() as directory:
                chunks = bind_literal_chunks(
                    [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
                     for index, entry in enumerate(group_into_chunks(full))],
                    get_literal_policy(full))
                self._shared_span_fixture(directory, full, raw, chunks)
                with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                        ensure_scope_admission(Path(directory)):
                    with self.assertRaises(ScopeSpanContradiction):
                        get_scope_preview(Path(directory), 3)

    def test_second_scope_without_new_evidence_keeps_prior_state(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        full = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(full))],
            get_literal_policy(full))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._shared_span_fixture(directory, full, raw, chunks)
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 1)
                apply_source_scope(root, 1, plan['revision'])
                discard_source_scope_undo(root, get_source_scope_undo_preview(root)['revision'])
            prior = json.loads((root / BINDING_NAME).read_bytes())['scope_cut']
            published = json.loads(path.read_bytes())
            saved_chunks = (root / 'chunks.json').read_bytes()
            missing = copy.deepcopy(published)
            missing[0].pop('source_span')
            publish_controlled_script_edit(directory, path.read_bytes(),
                                           json.dumps(missing).encode())
            (root / 'chunks.json').write_bytes(saved_chunks)
            ProjectManager(directory).load_chunks()
            watched = {name: (root / name).read_bytes()
                       for name in ('annotated_script.json', 'state.json', BINDING_NAME)}
            self.assertFalse((root / '.project_scope_undo.json').exists())
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                second = get_scope_preview(root, 2)
                self.assertIsNone(second['scope_cut_evidence'])
                with self.assertRaises(ValueError):
                    apply_source_scope(root, 2, second['revision'])
            self.assertEqual(watched, {name: (root / name).read_bytes()
                                       for name in watched})
            self.assertFalse((root / '.project_scope_undo.json').exists())
            self.assertEqual(prior, json.loads((root / BINDING_NAME).read_bytes())['scope_cut'])

    def test_second_scope_with_valid_evidence_publishes_and_replays(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        full = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(full))],
            get_literal_policy(full))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._shared_span_fixture(directory, full, raw, chunks)
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 1)
                apply_source_scope(root, 1, plan['revision'])
                discard_source_scope_undo(root, get_source_scope_undo_preview(root)['revision'])
            ProjectManager(directory).load_chunks()
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                second = get_scope_preview(root, 2)
                self.assertIsNotNone(second['scope_cut_evidence'])
                apply_source_scope(root, 2, second['revision'])
            child = json.loads(path.read_bytes())
            self.assertEqual([22, 23], child[0]['source_span'])
            saved, _out, _calls, _changed = run_cli_at(
                directory, path, [CutEvidenceTests().split(child)])
            self.assertIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
            replay, output, calls, changed = run_cli_at(directory, path, [])
            self.assertEqual([], calls)
            self.assertFalse(changed)
            self.assertEqual(saved, replay)

    def test_second_cut_requires_current_parent_to_match_frozen_evidence(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !') + '  ?'
        digest = hashlib.sha256(raw.encode()).hexdigest()
        base = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(base))],
            get_literal_policy(base))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._shared_span_fixture(directory, base, raw, chunks)
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 1)
                apply_source_scope(root, 1, plan['revision'])
                discard_source_scope_undo(root, get_source_scope_undo_preview(root)['revision'])
            prior = json.loads((root / BINDING_NAME).read_bytes())['scope_cut']
            variants = {}
            clipped = json.loads(path.read_bytes())
            clipped[3]['text'] = clipped[3]['text'][:-1]
            clipped[3]['source_span'][1] -= 1
            variants['clip-interior'] = clipped
            omitted = json.loads(path.read_bytes())
            del omitted[2]
            variants['omit-mark'] = omitted
            suffix = json.loads(path.read_bytes())
            suffix.append(dict(suffix[-1], text='?',
                               source_span=[len(raw) - 1, len(raw)]))
            variants['unexpected-suffix'] = suffix
            for name, mutated in variants.items():
                with self.subTest(variant=name):
                    publish_controlled_script_edit(directory, path.read_bytes(),
                                                   json.dumps(mutated).encode())
                    ProjectManager(directory).load_chunks()
                    watched = {item: (root / item).read_bytes() for item in
                               ('annotated_script.json', 'state.json', BINDING_NAME, 'chunks.json')}
                    with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                            ensure_scope_admission(root):
                        with self.assertRaises(ScopeSpanContradiction):
                            second = get_scope_preview(root, 2)
                            apply_source_scope(root, 2, second['revision'])
                    self.assertEqual(watched, {item: (root / item).read_bytes()
                                               for item in watched})
                    self.assertEqual(
                        prior, json.loads((root / BINDING_NAME).read_bytes())['scope_cut'])
                    self.assertFalse((root / '.project_scope_undo.json').exists())
    def test_two_leading_marks_in_distinct_intervals_publish(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me !', 'Exclude me\n\n!').replace('!\n\n!', '! : !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        base = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(base))],
            get_literal_policy(base))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._shared_span_fixture(directory, base, raw, chunks)
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 2)
                self.assertEqual(2, len(plan['selected_script'][0][
                    SCRIPT_POLICY_KEY]['scope_source']['cut_proof']['intervals']))
                apply_source_scope(root, 2, plan['revision'])
            child = json.loads(path.read_bytes())
            self.assertEqual('!', child[0]['text'])
            self.assertEqual('!', child[1]['text'])
            self.assertNotEqual(child[0]['source_span'][1], child[1]['source_span'][0])
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(path), path.read_bytes(), child))
            saved, _out, _calls, _changed = run_cli_at(
                directory, path, [CutEvidenceTests().split(child)])
            self.assertIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
            replay, output, calls, changed = run_cli_at(directory, path, [])
            self.assertEqual([], calls)
            self.assertFalse(changed)
            self.assertEqual(saved, replay)

    def test_unrelated_absent_span_keeps_resolved_shared_fragment(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        base = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        base[2]['source_span'] = [19, 23]
        base[3]['source_span'] = [19, 23]
        base[0].pop('source_span')
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(base))],
            get_literal_policy(base))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._shared_span_fixture(directory, base, raw, chunks)
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 3)
                self.assertIsNone(plan['scope_cut_evidence'])
                apply_source_scope(root, 3, plan['revision'])
            child = json.loads(path.read_bytes())
            self.assertEqual([22, 23], child[0]['source_span'])
        certified = copy.deepcopy(base)
        certified[0]['source_span'] = [0, 6]
        with tempfile.TemporaryDirectory() as directory:
            active, _, _ = create_scoped_book(
                directory, raw=raw, parent_rows=certified, active_rows=child)
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(active), active.read_bytes(), child))
            saved, _out, _calls, _changed = run_cli_at(
                directory, active, [CutEvidenceTests().split(child)])
            self.assertIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
            replay, output, calls, changed = run_cli_at(directory, active, [])
            self.assertEqual([], calls)
            self.assertFalse(changed)
            self.assertEqual(saved, replay)

    def test_valid_second_cut_survives_save_load_and_rebind(self):
        rows = [row(text, 'NARRATOR', instruct='Even.') for text in
                ('Intro.', 'Exclude me', '!', '!', 'Keep the line.')]
        rows += copy.deepcopy(RAW_ROWS[5:])
        raw = '\n\n'.join(entry['text'] for entry in rows).replace(
            'Exclude me\n\n!', 'Exclude me !') + '  ?'
        digest = hashlib.sha256(raw.encode()).hexdigest()
        base = bind_literal_policy(raw, mark_entries(rows, raw), input_sha256=digest)
        chunks = bind_literal_chunks(
            [dict(entry, id=index, uid=f'u{index}', status='pending', audio_path=None)
             for index, entry in enumerate(group_into_chunks(base))],
            get_literal_policy(base))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._shared_span_fixture(directory, base, raw, chunks)
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                plan = get_scope_preview(root, 1)
                apply_source_scope(root, 1, plan['revision'])
                discard_source_scope_undo(root, get_source_scope_undo_preview(root)['revision'])
            ProjectManager(directory).load_chunks()
            with patch('core.DATA_DIR', directory), patch('core.process_state', {}), \
                    ensure_scope_admission(root):
                control = get_scope_preview(root, 2)
                self.assertIsNotNone(control['scope_cut_evidence'])
                self.assertEqual((22, 129), (control['scope_cut_evidence']['outer_start'],
                                             control['scope_cut_evidence']['outer_end']))
                apply_source_scope(root, 2, control['revision'])
            child = json.loads(path.read_bytes())
            saved, _out, _calls, _changed = run_cli_at(
                directory, path, [CutEvidenceTests().split(child)])
            self.assertIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
            replay, output, calls, changed = run_cli_at(directory, path, [])
            self.assertEqual([], calls)
            self.assertFalse(changed)
            self.assertEqual(saved, replay)

    def test_legacy_direct_without_ambiguous_prefix_still_captures(self):
        rows = [row(text, 'NARRATOR') for text in
                ('Excluded.', 'Keep the line.\n\nAfterward.')]
        raw = '\n\n'.join(entry['text'] for entry in rows)
        retained, _ = build_scoped_rows(raw, rows, 1)
        self.assertTrue(any(True for _ in iter_literal_words(retained[0]['text'])))
        with tempfile.TemporaryDirectory() as directory:
            script, _, entries = create_book(directory, raw=raw, source=retained)
            self.assertIsNone(get_scope_cut_from_binding(
                parse_script_binding(script.with_suffix('.selection_binding.json').read_bytes())))
            self.assertEqual(raw, review.get_review_source_text(
                directory, str(script), script.read_bytes(), entries))
            saved, _out, _calls, _changed = run_cli_at(
                directory, script, [CutEvidenceTests().split(retained)])
            self.assertIn(NEW_SPEAKER, [entry['speaker'] for entry in saved])
            replay, output, calls, changed = run_cli_at(directory, script, [])
            self.assertEqual([], calls)
            self.assertFalse(changed)
            self.assertEqual(saved, replay)
