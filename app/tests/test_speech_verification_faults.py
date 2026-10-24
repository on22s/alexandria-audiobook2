import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from experiments import speech_verification_faults as harness
from experiments.speech_verification_faults import (make_fault, get_cases, get_reusable_results,
                                                     get_sha256, publish_cases)
from experiments.speech_verification_faults import (_validate_result_row, _verify,
                                                     get_runtime_identity)

CLEAR = 'no_content_candidate'
RUNTIME = {'primary': {'content': 'primary-bytes'}, 'confirmation': {'content': 'confirmation-bytes'}}


def valid_row(case, **updates):
    row = {key: value for key, value in case.items() if not key.startswith('_')}
    row.update(status=CLEAR, seconds=1.0, duration=1.0, reason=None,
               primary='transcript', confirmation=None, corroborated=0)
    row.update(updates)
    return row


class MakeFaultTests(unittest.TestCase):
    pcm = np.arange(1000, dtype=np.int16)

    def test_cuts_remove_the_named_part(self):
        # The missing material is exactly the designed span, so a "missed defect"
        # can't come from a fault that was never actually cut.
        self.assertTrue(np.array_equal(make_fault(self.pcm, 'tail_cut'), self.pcm[:550]))
        self.assertTrue(np.array_equal(make_fault(self.pcm, 'head_cut'), self.pcm[350:]))
        self.assertTrue(np.array_equal(make_fault(self.pcm, 'short_half'), self.pcm[:500]))

    def test_repeats_say_the_span_twice_and_keep_everything_else(self):
        tail = make_fault(self.pcm, 'tail_repeat')
        self.assertTrue(np.array_equal(tail[:1000], self.pcm))
        self.assertTrue(np.array_equal(tail[1000:], self.pcm[600:]))
        mid = make_fault(self.pcm, 'mid_repeat')
        self.assertEqual(len(mid), 1200)
        self.assertTrue(np.array_equal(mid[600:800], self.pcm[400:600]))
        self.assertTrue(np.array_equal(mid[800:], self.pcm[600:]))
        self.assertTrue(np.array_equal(make_fault(self.pcm, 'short_double'),
                                       np.concatenate([self.pcm, self.pcm])))

    def test_input_is_not_changed_and_unknown_kind_refused(self):
        before = self.pcm.copy()
        for kind in ('tail_cut', 'head_cut', 'tail_repeat', 'mid_repeat', 'short_double', 'short_half'):
            make_fault(self.pcm, kind)
        self.assertTrue(np.array_equal(self.pcm, before))
        with self.assertRaises(ValueError):
            make_fault(self.pcm, 'reverse')


class GetCasesTests(unittest.TestCase):
    def _run(self, root, name, rows):
        folder = Path(root) / name
        (folder / 'wav').mkdir(parents=True)
        out = []
        for clip_id, line_id, seed, text, level in rows:
            wav = folder / 'wav' / f'{clip_id}.wav'
            sf.write(str(wav), np.full(2400, level, dtype=np.int16), 24000, subtype='PCM_16')
            sha = get_sha256(wav)
            out.append(dict(clip_id=clip_id, line_id=line_id, seed=seed, wav=str(wav), wav_sha256=sha,
                            speech_input=dict(text=text, audio=dict(sha256=sha))))
        (folder / 'rows.json').write_text(json.dumps({'rows': out}))

    def test_fault_plan_per_group(self):
        # Long lines get the four long faults, short lines the two short ones, and
        # authored repetition only a control; other seeds are controls only.
        with tempfile.TemporaryDirectory() as root:
            self._run(root, 'alice', [('a1', 'a01', 1, 'long line', 1), ('a1b', 'a01', 2, 'long line', 2)])
            self._run(root, 'verify', [('v', 'v01', 1, 'Yes.', 3), ('r', 'r01', 1, 'No, no!', 4)])
            derived = Path(root) / 'd'
            cases = get_cases(root, derived, {1}, ('alice', 'verify'))
            self.assertTrue(all(not isinstance(value, np.ndarray)
                                for case in cases for value in case.values()))
            # Authored repetitions produce controls without decoding unused faults.
            with patch.object(harness, '_read_control', wraps=harness._read_control) as decode:
                get_cases(root, derived, {1}, ('verify',))
            self.assertEqual(decode.call_count, 1)  # v, never r
            publish_cases(cases, derived)
            got = {(c['case_id'].split('__')[0], c['fault'], c['expect']) for c in cases}
            self.assertEqual(got, {
                ('a1', 'control', 'clean'), ('a1', 'tail_cut', 'defect'), ('a1', 'head_cut', 'defect'),
                ('a1', 'tail_repeat', 'defect'), ('a1', 'mid_repeat', 'defect'),
                ('a1b', 'control', 'clean'),
                ('v', 'control', 'clean'), ('v', 'short_double', 'defect'), ('v', 'short_half', 'defect'),
                ('r', 'control', 'clean')})
            self.assertTrue(all(Path(c['wav']).is_file() for c in cases))
            self.assertTrue(all(c['input_sha256'] == get_sha256(c['wav']) for c in cases))
            # A derived file left from an older source is rebuilt, not reused by name.
            tail = next(c for c in cases if c['fault'] == 'tail_cut')
            sf.write(tail['wav'], np.full(10, 9, dtype=np.int16), 24000, subtype='PCM_16')
            self._rewrite(root, 'alice', 'a1', np.full(2400, 7, dtype=np.int16))
            again = next(c for c in get_cases(root, derived, {1}, ('alice', 'verify'))
                         if c['fault'] == 'tail_cut')
            publish_cases([again], derived)
            pcm, _rate = sf.read(again['wav'], dtype='int16')
            self.assertTrue(np.array_equal(pcm, make_fault(np.full(2400, 7, dtype=np.int16), 'tail_cut')))
            self.assertEqual(again['source_sha256'], get_sha256(Path(root) / 'alice' / 'wav' / 'a1.wav'))
            self.assertNotEqual(again['input_sha256'], tail['input_sha256'])
            # OS/user aliases in ancestors are canonicalized; the evidence entry
            # itself must still be plain (macOS /var has this shape).
            ancestor = Path(root) / 'ancestor'
            ancestor.symlink_to(Path(root), target_is_directory=True)
            alias_derived = ancestor / 'alias-derived'
            aliased = get_cases(ancestor, alias_derived, {1}, ('alice',))
            publish_cases(aliased, alias_derived)
            self.assertTrue(all(Path(c['wav']).is_file() for c in aliased))
            linked_derived = Path(root) / 'linked-derived'
            linked_derived.symlink_to(derived, target_is_directory=True)
            with self.assertRaises(ValueError):
                publish_cases(cases, linked_derived)
            # Reject an oversized designed WAV before creating its staged file.
            with patch('word_timing.MAX_AUDIO_BYTES', 200), \
                 patch.object(harness.tempfile, 'TemporaryDirectory',
                              side_effect=AssertionError('oversized fault staged')):
                with self.assertRaisesRegex(ValueError, 'Designed fault exceeds'):
                    harness._planned_fault_identity(np.zeros(100, dtype=np.int16), 24000)
            # A clip id with path components is refused before any write.
            rows_path = Path(root) / 'alice' / 'rows.json'
            data = json.loads(rows_path.read_text())
            data['rows'][0]['clip_id'] = 'x/../../escaped'
            rows_path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'clip_id'):
                get_cases(root, derived, {1}, ('alice',))
            self.assertFalse((Path(root) / 'escaped__tail_cut.wav').exists())
            with self.assertRaisesRegex(ValueError, 'unknown run'):
                get_cases(root, derived, {1}, ('../alice',))

    def _rewrite(self, root, name, clip_id, pcm, update_proof=True):
        """Replace one clip's WAV; optionally record the new hash in its proof."""
        wav = Path(root) / name / 'wav' / f'{clip_id}.wav'
        sf.write(str(wav), pcm, 24000, subtype='PCM_16')
        if update_proof:
            path = Path(root) / name / 'rows.json'
            data = json.loads(path.read_text())
            for row in data['rows']:
                if row['clip_id'] == clip_id:
                    row['wav_sha256'] = row['speech_input']['audio']['sha256'] = get_sha256(wav)
            path.write_text(json.dumps(data))

    def test_refuses_a_row_whose_proof_names_other_audio(self):
        with tempfile.TemporaryDirectory() as root:
            self._run(root, 'alice', [('a1', 'a01', 1, 'text', 1)])
            path = Path(root) / 'alice' / 'rows.json'
            original = path.read_text()
            # Even a matching proof cannot authorize audio outside this run.
            outside = Path(root) / 'outside.wav'
            outside.write_bytes((Path(root) / 'alice' / 'wav' / 'a1.wav').read_bytes())
            external = json.loads(original)
            external['rows'][0]['wav'] = str(outside)
            path.write_text(json.dumps(external))
            with self.assertRaisesRegex(ValueError, 'inside its pilot run'):
                get_cases(root, Path(root) / 'd', {1}, ('alice',))
            path.write_text(original)
            # JSON admission reads from its original bounded descriptor.
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('pathname reopened')):
                self.assertEqual(harness._read_bounded_json(path, harness.MAX_ROWS_BYTES),
                                 json.loads(original))
            # Replacement after the descriptor opens is refused without reopening.
            import project_scope_files as scope_files
            real_open = scope_files._open_regular
            admitted = Path(root) / 'admitted.json'
            admitted.write_text('{}')
            def replace_after_open(*args, **kwargs):
                stream, info = real_open(*args, **kwargs)
                admitted.unlink()
                os.mkfifo(admitted)
                return stream, info
            with patch.object(scope_files, '_open_regular', side_effect=replace_after_open):
                with self.assertRaises(ValueError):
                    harness._read_bounded_json(admitted, 1024)
            data = json.loads(original)
            data['rows'][0]['speech_input']['audio']['sha256'] = 'other'
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                get_cases(root, Path(root) / 'd', {1}, ('alice',))
            # Metadata agrees with itself, but the WAV on disk was swapped.
            path.write_text(original)
            self._rewrite(root, 'alice', 'a1', np.full(2400, 5, dtype=np.int16), update_proof=False)
            with self.assertRaisesRegex(ValueError, 'on disk'):
                get_cases(root, Path(root) / 'd', {1}, ('alice',))
            # A FIFO named by a control row is refused without blocking on it.
            wav = Path(root) / 'alice' / 'wav' / 'a1.wav'
            wav.unlink()
            os.mkfifo(wav)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                get_cases(root, Path(root) / 'd', {1}, ('alice',))

    def test_resume_keys_on_run_and_reuses_only_identical_inputs(self):
        # The U and W arms render the same clips, so their case ids collide; one
        # finished arm must not mark the other as done.
        with tempfile.TemporaryDirectory() as root:
            self._run(root, 'w1435_U', [('w1', 'w01', 1, 'same line', 1)])
            self._run(root, 'w1435_W', [('w1', 'w01', 1, 'same line', 2)])
            cases = [dict(c, runtime=RUNTIME) for c in get_cases(root, Path(root) / 'd', set(),
                                                                  ('w1435_U', 'w1435_W'))]
            self.assertEqual(['w1__control', 'w1__control'], [c['case_id'] for c in cases])
            self.assertEqual(set(), get_reusable_results(Path(root) / 'fresh' / 'nested' / 'results.jsonl', cases))
            # The actual CLI path must accept a fresh nested output directory.
            from unittest.mock import MagicMock
            pool = MagicMock()
            pool.__enter__.return_value = pool
            pool.imap_unordered.side_effect = lambda function, jobs: (valid_row(j[0]) for j in jobs)
            argv = ['--pilot-root', root, '--asr-root', 'unused', '--out-dir',
                    str(Path(root) / 'fresh' / 'nested'), '--runs', 'w1435_U',
                    '--fault-seeds', '999', '--workers', '1']
            with patch.object(harness, 'get_runtime_identity', return_value=RUNTIME), \
                 patch.object(harness.multiprocessing, 'Pool', return_value=pool):
                harness.main(argv)
            self.assertEqual({('w1435_U', 'w1__control')}, get_reusable_results(
                Path(root) / 'fresh' / 'nested' / 'results.jsonl', cases))
            bad_out = Path(root) / 'bad-result'
            argv[argv.index('--out-dir') + 1] = str(bad_out)
            pool.imap_unordered.side_effect = lambda function, jobs: (
                valid_row(j[0], seconds=float('nan')) for j in jobs)
            with patch.object(harness, 'get_runtime_identity', return_value=RUNTIME), \
                 patch.object(harness.multiprocessing, 'Pool', return_value=pool):
                with self.assertRaises(ValueError):
                    harness.main(argv)
            self.assertEqual((bad_out / 'results.jsonl').read_bytes(), b'')
            results = Path(root) / 'results.jsonl'
            results.write_text(json.dumps(valid_row(cases[0])) + '\n')
            results.write_text('')
            self.assertEqual(set(), get_reusable_results(results, cases))
            results.write_text(json.dumps(valid_row(cases[0])) + '\n')
            self.assertEqual({('w1435_U', 'w1__control')}, get_reusable_results(results, cases))
            for stale in (dict(valid_row(cases[0], input_sha256='0' * 64)),
                          dict(valid_row(cases[0], runtime={'primary': {'content': 'other'},
                                                            'confirmation': RUNTIME['confirmation']})),
                          {k: v for k, v in cases[0].items() if k not in ('input_sha256', 'input_size', 'runtime')}):
                results.write_text(json.dumps(stale) + '\n')
                with self.assertRaises(ValueError):
                    get_reusable_results(results, cases)
            results.write_text((json.dumps(valid_row(cases[0])) + '\n') * 2)
            with self.assertRaisesRegex(ValueError, 'twice'):
                get_reusable_results(results, cases)
            # A syntactically valid but incomplete completed row is rejected.
            incomplete = {k: cases[0][k] for k in ('run', 'case_id', 'text', 'fault', 'expect',
                                                    'input_sha256', 'input_size', 'runtime')}
            results.write_text(json.dumps(incomplete) + '\n')
            with self.assertRaisesRegex(ValueError, 'status'):
                get_reusable_results(results, cases)
            # Failure reason text cannot stand in for completed ASR evidence.
            for malformed in (valid_row(cases[0], primary=None, reason='failed'),
                              {k: v for k, v in valid_row(cases[0]).items() if k != 'duration'},
                              valid_row(cases[0], status='corroborated_content_difference',
                                        confirmation=None, corroborated=0)):
                results.write_text(json.dumps(malformed) + '\n')
                with self.assertRaises(ValueError):
                    get_reusable_results(results, cases)
            # A rejected resume must not overwrite prior derived evidence.
            with tempfile.TemporaryDirectory() as artifact_root:
                self._run(artifact_root, 'w1435_U', [('w1', 'w01', 1, 'same line', 1)])
                derived = Path(artifact_root) / 'd'
                planned = [dict(c, runtime=RUNTIME)
                           for c in get_cases(artifact_root, derived, {1}, ('w1435_U',))]
                publish_cases(planned, derived)
                fault = next(c for c in planned if c['fault'] == 'tail_cut')
                before = Path(fault['wav']).read_bytes()
                artifact_results = Path(artifact_root) / 'results.jsonl'
                artifact_results.write_text(json.dumps(valid_row(fault)) + '\n')
                self._rewrite(artifact_root, 'w1435_U', 'w1', np.full(2400, 7, dtype=np.int16))
                changed = [dict(c, runtime=RUNTIME)
                           for c in get_cases(artifact_root, derived, {1}, ('w1435_U',))]
                with self.assertRaises(ValueError):
                    get_reusable_results(artifact_results, changed)
                self.assertEqual(before, Path(fault['wav']).read_bytes())
                # A late completed-artifact failure cannot replace earlier pending files.
                pending, completed = [c for c in planned if c['fault'] != 'control'][:2]
                Path(pending['wav']).write_bytes(b'pending evidence')
                Path(completed['wav']).write_bytes(b'changed completed evidence')
                with self.assertRaisesRegex(ValueError, 'prior derived evidence changed'):
                    publish_cases(planned, derived, {(completed['run'], completed['case_id'])})
                self.assertEqual(Path(pending['wav']).read_bytes(), b'pending evidence')
                self._rewrite(artifact_root, 'w1435_U', 'w1', np.full(2400, 1, dtype=np.int16))
                publish_cases([completed], derived)
                outside = Path(artifact_root) / 'external.wav'
                outside.write_bytes(Path(completed['wav']).read_bytes())
                Path(completed['wav']).unlink()
                for alias in ('symbolic', 'hard'):
                    if alias == 'symbolic':
                        Path(completed['wav']).symlink_to(outside)
                    else:
                        os.link(outside, completed['wav'])
                    with self.assertRaises(ValueError):
                        publish_cases(planned, derived, {(completed['run'], completed['case_id'])})
                    self.assertEqual(Path(pending['wav']).read_bytes(), b'pending evidence')
                    Path(completed['wav']).unlink()
            # The archived 334 rows keep their schema; they predate input/runtime binding.
            archive = Path(__file__).resolve().parent.parent.parent / 'docs' / 'benchmarks' / 'raw' / \
                'speech-verification-1434-20261024' / 'results.jsonl'
            archived = [json.loads(line) for line in archive.read_text().splitlines()]
            for row in archived:
                _validate_result_row(row, str(archive))
            self.assertEqual(334, len(archived))
            # The runtime identity is the verifier's content identity, not path/size/mtime.
            import speech_verification_runtime as runtime_module
            with patch.object(runtime_module, 'get_verification_runtime',
                              side_effect=lambda root, confirmation=False: {
                                  'role': 'confirmation' if confirmation else 'primary'}), \
                 patch.object(runtime_module, 'get_verifier_identity',
                              side_effect=lambda runtime, role, cancel_check: {
                                  'role': role, 'content': runtime['role'] + '-bytes'}):
                identity = get_runtime_identity('root')
            self.assertEqual(identity['primary']['content'], 'primary-bytes')
            self.assertEqual(identity['confirmation']['content'], 'confirmation-bytes')
        # A fake provider returns the changed audio hash while the plan names the old one.
        with tempfile.TemporaryDirectory() as root:
            self._run(root, 'alice', [('a1', 'a01', 1, 'text', 1)])
            case = get_cases(root, Path(root) / 'd', set(), ('alice',))[0]
            case = dict(case, runtime={'primary': {'id': 1}, 'confirmation': {'id': 2}},
                        input_sha256='a' * 64, input_size=100)
            report = {'status': CLEAR, 'seconds': 0.1, 'duration': 1.0, 'reason': None,
                      'audio': {'sha256': 'b' * 64, 'size': 100},
                      'primary': {'identity': {'id': 1}, 'transcript': 'text'}}
            with patch('speech_verification.verify_finished_speech', return_value=report), \
                 patch('speech_verification_runtime.get_verification_runtime', return_value={}):
                with self.assertRaisesRegex(ValueError, 'different audio'):
                    _verify((case, 'root', 'scratch'))
                matched_audio = dict(report, audio={'sha256': case['input_sha256'],
                                                    'size': case['input_size']})
                changed_identity = dict(matched_audio,
                                        primary={'identity': {'id': 99}, 'transcript': 'text'})
                with patch('speech_verification.verify_finished_speech', return_value=changed_identity):
                    with self.assertRaisesRegex(ValueError, 'identity changed'):
                        _verify((case, 'root', 'scratch'))
                accepted = dict(matched_audio,
                                primary={'identity': {'id': 1}, 'transcript': 'text'})
                with patch('speech_verification.verify_finished_speech', return_value=accepted):
                    row = _verify((case, 'root', 'scratch'))
                self.assertEqual(CLEAR, row['status'])
                self.assertEqual(case['input_sha256'], row['input_sha256'])
                for changes in ({'status': 'unknown'}, {'seconds': float('nan')},
                                {'duration': float('inf')}, {'primary': None, 'reason': None}):
                    bad = dict(accepted, **changes)
                    # primary is a report object; a missing provider result needs unavailable.
                    with patch('speech_verification.verify_finished_speech', return_value=bad):
                        with self.assertRaises(ValueError):
                            _verify((case, 'root', 'scratch'))
                unavailable = dict(matched_audio, status='unavailable', primary=None,
                                   reason='Identity was unavailable')
                unavailable.pop('duration')
                with patch('speech_verification.verify_finished_speech', return_value=unavailable):
                    row = _verify((case, 'root', 'scratch'))
                self.assertIsNone(row['duration'])
                checkpoint = Path(root) / 'results.jsonl'
                checkpoint.write_text(json.dumps(row, allow_nan=False) + '\n')
                self.assertEqual({('alice', 'a1__control')}, get_reusable_results(checkpoint, [case]))
