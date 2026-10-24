import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from experiments.speech_verification_faults import make_fault, get_cases


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
        for clip_id, line_id, seed, text, sha in rows:
            wav = folder / 'wav' / f'{clip_id}.wav'
            sf.write(str(wav), np.zeros(2400, dtype=np.int16), 24000, subtype='PCM_16')
            out.append(dict(clip_id=clip_id, line_id=line_id, seed=seed, wav=str(wav), wav_sha256=sha,
                            speech_input=dict(text=text, audio=dict(sha256=sha))))
        (folder / 'rows.json').write_text(json.dumps({'rows': out}))

    def test_fault_plan_per_group(self):
        # Long lines get the four long faults, short lines the two short ones, and
        # authored repetition only a control; other seeds are controls only.
        with tempfile.TemporaryDirectory() as root:
            self._run(root, 'alice', [('a1', 'a01', 1, 'long line', 'x'), ('a1b', 'a01', 2, 'long line', 'y')])
            self._run(root, 'verify', [('v', 'v01', 1, 'Yes.', 'z'), ('r', 'r01', 1, 'No, no!', 'w')])
            cases = get_cases(root, Path(root) / 'd', {1}, ('alice', 'verify'))
            got = {(c['case_id'].split('__')[0], c['fault'], c['expect']) for c in cases}
            self.assertEqual(got, {
                ('a1', 'control', 'clean'), ('a1', 'tail_cut', 'defect'), ('a1', 'head_cut', 'defect'),
                ('a1', 'tail_repeat', 'defect'), ('a1', 'mid_repeat', 'defect'),
                ('a1b', 'control', 'clean'),
                ('v', 'control', 'clean'), ('v', 'short_double', 'defect'), ('v', 'short_half', 'defect'),
                ('r', 'control', 'clean')})
            self.assertTrue(all(Path(c['wav']).is_file() for c in cases))

    def test_refuses_a_row_whose_proof_names_other_audio(self):
        with tempfile.TemporaryDirectory() as root:
            self._run(root, 'alice', [('a1', 'a01', 1, 'text', 'x')])
            path = Path(root) / 'alice' / 'rows.json'
            data = json.loads(path.read_text())
            data['rows'][0]['speech_input']['audio']['sha256'] = 'other'
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                get_cases(root, Path(root) / 'd', {1}, ('alice',))


if __name__ == '__main__':
    unittest.main()
