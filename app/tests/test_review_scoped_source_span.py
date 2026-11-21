"""Scoped literal-source review keeps verified source spans for split entries."""
import copy
import hashlib
import io
import json
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dialogue_spans import _normalize, mark_entries
from literal_fidelity import _words
from literal_policy import (SCRIPT_POLICY_KEY, bind_literal_policy, get_literal_policy,
                            get_literal_policy_identity, get_word_identity)
import review_script as review
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


def make_scoped_book():
    """A synthetic retained script with a verified selected-source policy."""
    source_rows = [
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
                                'total_words': len(words)})
    get_literal_policy([{SCRIPT_POLICY_KEY: policy}])
    retained[0][SCRIPT_POLICY_KEY] = policy
    return retained, marked[START_ENTRY:]


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


def run_cli(batch, replies, config=None):
    """Drive the real review entry point with a mock provider."""
    client, calls = client_for([(reply, 'stop') for reply in replies])
    with tempfile.TemporaryDirectory() as directory:
        script, _, _ = create_book(directory, raw=RAW, source=batch)
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
