"""Goal 5.4, Japanese alignment: ReadAlong forced alignment on the 272 ms instrument.

The open axis of 5.4 is Japanese boundary alignment: 272 ms median against a 150 ms
target on 50 real audiobook clips (kokoro_ja_asr_eval, row offset 0, all 50 aligned;
asr_silero_whisper_ja_confirmation.json). Every ASR route tried segments from audio
alone. The preparer, however, HAS the book text, and ReadAlong Studio (SoundSwallower +
G2P, zero-shot) aligns known text to audio: on the ASR bench it found 10/10 Japanese
boundaries with a constant ~0.28 s early bias. It was never run on this instrument.

Same instrument, unchanged: asr_backends.build_alignment_probe concatenates the same 50
clips with 0.5 s gaps (truth is arithmetic) and asr_backends.score_alignment scores the
nearest predicted start per true clip. ReadAlong gets the 50 reference texts, one line
each, and each sentence's first-word time is its predicted start.

Reported RAW, as the target is written. The signed-error distribution is stored too, so a
constant bias is visible; a bias-corrected figure is a calibration fitted on the same
clips and is labelled as such, never substituted for the raw one.
"""
import argparse, json, os, statistics, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET

REPO = sys.argv[sys.argv.index("--repo") + 1] if "--repo" in sys.argv else os.getcwd()
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
sys.path.insert(0, os.path.join(REPO, "app"))
import asr_backends as ab  # noqa: E402  build_alignment_probe / score_alignment, unchanged
from experiments.provenance import provenance  # noqa: E402


def read_starts(readalong_xml):
    """Start time of each <s> sentence = the time of its first timed <w>."""
    root = ET.parse(readalong_xml).getroot()
    starts = []
    for s in root.iter():
        if s.tag.split('}')[-1] != 's':
            continue
        t = [float(w.get('time')) for w in s.iter() if w.tag.split('}')[-1] == 'w' and w.get('time')]
        if t:
            starts.append(min(t))
    return starts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', required=True)
    ap.add_argument('--build', default='ab_test_runtime/kokoro_ja_asr_eval/build.json')
    ap.add_argument('--limit', type=int, default=50)
    ap.add_argument('--row-offset', type=int, default=0)
    ap.add_argument('--lang', nargs='+', default=['jpn', 'und'])
    ap.add_argument('--readalongs', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    build = json.load(open(os.path.join(REPO, a.build), encoding='utf-8'))
    rows = build['test'][a.row_offset:a.row_offset + a.limit]
    work = tempfile.mkdtemp(prefix='readalong_ja_')
    wav, truth = ab.build_alignment_probe(rows, os.path.join(work, 'probe.wav'))
    txt = os.path.join(work, 'probe.txt')
    # one reference text per line; a blank line between them keeps each clip its own sentence
    open(txt, 'w', encoding='utf-8').write('\n\n'.join(t['text'] for t in truth) + '\n')
    out = {'instrument': {'build': a.build, 'row_offset': a.row_offset, 'limit': a.limit,
                          'clips': len(truth), 'gap_s': 0.5,
                          'baseline': 'asr_silero_whisper_ja_confirmation.json: median 0.272 s, 58.0% within 0.3 s'},
           'arms': {}}
    for lang in a.lang:
        od = os.path.join(work, f'out_{lang}')
        t0 = time.time()
        p = subprocess.run([a.readalongs, 'align', '-f', '-l', lang, txt, wav, od], capture_output=True, text=True)
        secs = time.time() - t0
        xml = [os.path.join(dp, f) for dp, _, fs in os.walk(od) for f in fs if f.endswith('.readalong')]
        if p.returncode != 0 or not xml:
            out['arms'][lang] = {'failed': True, 'rc': p.returncode, 'stderr_tail': p.stderr[-800:]}
            print(lang, 'FAILED', p.stderr[-300:]); continue
        starts = read_starts(xml[0])
        segs = [(s, s) for s in starts]
        raw = ab.score_alignment(truth, segs)
        signed = sorted(min(starts, key=lambda s: abs(s - t['start'])) - t['start'] for t in truth)
        bias = statistics.median(signed)
        corr = ab.score_alignment(truth, [(s - bias, s - bias) for s in starts])
        out['arms'][lang] = {'raw': raw, 'sentences_found': len(starts), 'expected': len(truth),
                             'signed_error_median_s': round(bias, 3),
                             'signed_error_p10_p90_s': [round(signed[len(signed) // 10], 3), round(signed[len(signed) * 9 // 10 - 1], 3)],
                             'bias_corrected_CALIBRATION_same_clips': corr,
                             'seconds': round(secs, 1), 'audio_seconds': round(truth[-1]['end'], 1)}
        print(f"{lang}: {len(starts)}/{len(truth)} sentences; RAW median {raw.get('median_error_s')} s, "
              f"within 0.3 s {raw.get('within_tolerance_pct')}%; signed median {bias:+.3f} s; "
              f"bias-corrected (calibration) median {corr.get('median_error_s')} s; {secs:.0f} s")
    out['provenance'] = provenance(__file__, a, readalongs=subprocess.run(
        [a.readalongs, '--version'], capture_output=True, text=True).stdout.strip())
    json.dump(out, open(a.out, 'w'), indent=1, ensure_ascii=False)


if __name__ == '__main__':
    main()
