import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from experiments.speech_verification_faults import (make_fault, get_cases, get_reusable_results,
                                                     get_sha256)


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
            cases = get_cases(root, Path(root) / 'd', {1}, ('alice', 'verify'))
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
            again = next(c for c in get_cases(root, Path(root) / 'd', {1}, ('alice', 'verify'))
                         if c['fault'] == 'tail_cut')
            pcm, _rate = sf.read(again['wav'], dtype='int16')
            self.assertTrue(np.array_equal(pcm, make_fault(np.full(2400, 7, dtype=np.int16), 'tail_cut')))
            self.assertEqual(again['source_sha256'], get_sha256(Path(root) / 'alice' / 'wav' / 'a1.wav'))
            self.assertNotEqual(again['input_sha256'], tail['input_sha256'])

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

    def test_resume_keys_on_run_and_reuses_only_identical_inputs(self):
        # The U and W arms render the same clips, so their case ids collide; one
        # finished arm must not mark the other as done.
        with tempfile.TemporaryDirectory() as root:
            self._run(root, 'w1435_U', [('w1', 'w01', 1, 'same line', 1)])
            self._run(root, 'w1435_W', [('w1', 'w01', 1, 'same line', 2)])
            cases = [dict(c, runtime={'m': 1}) for c in get_cases(root, Path(root) / 'd', set(),
                                                                  ('w1435_U', 'w1435_W'))]
            self.assertEqual(['w1__control', 'w1__control'], [c['case_id'] for c in cases])
            results = Path(root) / 'results.jsonl'
            results.write_text(json.dumps(dict(cases[0], status='no_content_candidate')) + '\n')
            self.assertEqual({('w1435_U', 'w1__control')}, get_reusable_results(results, cases))
            for stale in (dict(cases[0], input_sha256='0' * 64), dict(cases[0], runtime={'m': 2}),
                          {k: v for k, v in cases[0].items() if k not in ('input_sha256', 'runtime')}):
                results.write_text(json.dumps(stale) + '\n')
                with self.assertRaises(ValueError):
                    get_reusable_results(results, cases)
            results.write_text((json.dumps(cases[0]) + '\n') * 2)
            with self.assertRaisesRegex(ValueError, 'twice'):
                get_reusable_results(results, cases)


if __name__ == '__main__':
    unittest.main()
