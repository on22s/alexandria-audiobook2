"""Issue #1719: report-only internal sentence gaps. Synthetic audio only."""
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

import sentence_gap_report as report


RATE = 16000
TEXT = "Hello world. Second sentence here."


def make_pcm(gap_start=0.5, gap_end=1.0, channels=1, duration=2.0, loud=0.5):
    frames = int(RATE * duration)
    pcm = np.full((frames, channels), loud, dtype=np.float64)
    lo, hi = int(gap_start * RATE), int(gap_end * RATE)
    pcm[lo:hi] = 0.0
    return pcm


def make_spans(text=TEXT):
    # Words: Hello(0-5) world(6-11). Second(13-19) sentence(20-28) here(29-33).
    return [
        {'from': 0, 'to': 5, 'start': 0.1, 'end': 0.4},
        {'from': 6, 'to': 11, 'start': 0.41, 'end': 0.45},
        {'from': 13, 'to': 19, 'start': 1.05, 'end': 1.3},
        {'from': 20, 'to': 28, 'start': 1.31, 'end': 1.6},
        {'from': 29, 'to': 33, 'start': 1.61, 'end': 1.9},
    ]


def make_segments(spans=None, text=TEXT):
    """Synthetic OBSERVED ASR words (as heard) corresponding to make_spans."""
    spans = make_spans(text) if spans is None else spans
    return [{'word': text[s['from']:s['to']], 'start': s['start'], 'end': s['end']} for s in spans]


def make_provenance(pcm, text=TEXT):
    return {'audio_sha256': hashlib.sha256(np.asarray(pcm, dtype=np.float64).tobytes()).hexdigest(),
            'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'provider': 'synthetic-test', 'language': 'en'}


def make_evidence(pcm, text=TEXT, spans=None, segments=None, duration=2.0):
    """Build a valid evidence envelope with the REAL existing validator."""
    from word_timing import get_timing_fingerprint, get_validated_word_spans
    segments = make_segments(spans, text) if segments is None else segments
    validated = get_validated_word_spans(text, segments, duration)
    binding = {'audio_sha256': hashlib.sha256(np.asarray(pcm, dtype=np.float64).tobytes()).hexdigest(),
               'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
               'provider': 'synthetic-test', 'language': 'en'}
    evidence = {'display_text': text, 'segments': segments, 'spans': validated,
                'duration': duration, 'provider': 'synthetic-test', 'language': 'en'}
    binding['payload_sha256'] = report._get_timing_payload_digest(evidence)
    return {**evidence, 'binding': binding, 'fingerprint': get_timing_fingerprint(binding)}


class SentenceGapReportTests(unittest.TestCase):
    def build(self, pcm=None, text=TEXT, spans=None, **kw):
        pcm = make_pcm() if pcm is None else pcm
        return report.get_sentence_gap_report(
            text=text,
            word_spans=make_evidence(pcm, text, spans) if spans is None else spans,
            pcm=pcm, sample_rate=RATE, provenance=make_provenance(pcm, text), **kw)

    def build_with(self, pcm, text, segments=None, spans=None, **kw):
        """Build from explicit observed segments (not from spans)."""
        pcm = make_pcm() if pcm is None else pcm
        duration = len(pcm) / RATE
        return report.get_sentence_gap_report(
            text=text, word_spans=make_evidence(pcm, text, spans, segments, duration),
            pcm=pcm, sample_rate=RATE, provenance=make_provenance(pcm, text), **kw)

    def test_good_synthetic_audio_yields_proposal_with_all_fields(self):
        result = self.build()
        self.assertEqual('sentence_gap_report_only', result['kind'])
        self.assertFalse(result['mutates_audio'])
        self.assertFalse(result['authorizes_edit'])
        prop = result['proposals'][0]
        self.assertEqual('proposal', prop['status'])
        self.assertIsNotNone(prop['source_boundary']['reason'])
        self.assertEqual(0, prop['original_coordinates']['left_sentence']['char_start'])
        self.assertIsNotNone(prop['asr_search_window'])
        self.assertIsNotNone(prop['measured_quiet_interval'])
        self.assertGreater(prop['proposed_extension_ms'], 0)
        self.assertIsNone(prop['skip_reason'])

    def test_explicit_user_pause_is_preserved_and_skipped(self):
        result = self.build(explicit_pause_after_sentence=(0,))
        prop = result['proposals'][0]
        self.assertEqual('skipped', prop['status'])
        self.assertEqual('explicit_user_pause', prop['skip_reason'])
        self.assertEqual(0, prop['proposed_extension_ms'])

    def test_no_quiet_audio_is_skipped(self):
        result = self.build(pcm=np.full((RATE * 2, 1), 0.5))
        self.assertEqual('no_quiet_interval_in_search_window', result['proposals'][0]['skip_reason'])

    def test_channel_aware_silence_requires_all_channels_quiet(self):
        pcm = make_pcm(channels=2)
        pcm[:, 1] = 0.5  # right channel loud throughout the gap
        result = self.build(pcm=pcm)
        self.assertNotEqual('proposal', result['proposals'][0]['status'])

    def test_wrong_transcript_yields_skip_not_proposal(self):
        # ASR heard 'banana' where the authored word is 'world': the real
        # validator drops that token, so the report must skip, never propose.
        segments = make_segments()
        segments[1] = {'word': 'banana', 'start': 0.41, 'end': 0.45}
        result = self.build_with(pcm=make_pcm(), text=TEXT, segments=segments)
        self.assertNotEqual('proposal', result['proposals'][0]['status'])
        self.assertIn(result['proposals'][0]['skip_reason'],
                      ('missing_asr_timing_for_neighbouring_words',
                       'transcript_mismatch_neighbouring_word'))

    def test_missing_provenance_is_rejected(self):
        pcm = make_pcm()
        with self.assertRaises(ValueError):
            report.get_sentence_gap_report(text=TEXT,
                                           word_spans=make_evidence(pcm), pcm=pcm,
                                           sample_rate=RATE, provenance={'provider': 'x'})

    def test_stale_provenance_is_rejected(self):
        pcm = make_pcm()
        prov = make_provenance(pcm)
        prov['audio_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            report.get_sentence_gap_report(text=TEXT, word_spans=make_evidence(pcm), pcm=pcm,
                                           sample_rate=RATE, provenance=prov)

    def test_nonfinite_and_invalid_inputs_rejected(self):
        pcm = make_pcm()
        pcm[10, 0] = float('nan')
        with self.assertRaises(ValueError):
            self.build(pcm=pcm)
        with self.assertRaises(ValueError):
            self.build(pcm=np.full((RATE * 121, 1), 0.1))  # oversized duration
        with self.assertRaises(ValueError):
            self.build(text='x' * 20001)
        bad = make_evidence(make_pcm())
        bad['spans'] = [{'from': 0, 'to': 5, 'start': float('inf'), 'end': 1.0}]
        pcm2 = make_pcm()
        with self.assertRaises(ValueError):
            report.get_sentence_gap_report(text=TEXT, word_spans=bad, pcm=pcm2,
                                           sample_rate=RATE, provenance=make_provenance(pcm2))

    def test_bare_span_list_fails_closed(self):
        pcm = make_pcm()
        with self.assertRaises(ValueError):
            report.get_sentence_gap_report(text=TEXT, word_spans=make_spans(), pcm=pcm,
                                           sample_rate=RATE, provenance=make_provenance(pcm))

    def test_overlapping_spans_rejected(self):
        # Overlapping observed segments are rejected by the real validator.
        segments = make_segments()
        segments[1] = {'word': 'world', 'start': 0.3, 'end': 0.45}
        with self.assertRaises(ValueError):
            self.build_with(pcm=make_pcm(), text=TEXT, segments=segments)

    def test_uncertain_boundary_is_skipped(self):
        text = "Mr. Smith left. He went home."
        segments = [
            {'word': 'Mr', 'start': 0.1, 'end': 0.3},
            {'word': 'Smith left', 'start': 0.31, 'end': 1.0},
            {'word': 'He went home', 'start': 1.01, 'end': 1.9},
        ]
        result = self.build_with(pcm=make_pcm(), text=text, segments=segments)
        self.assertIn(result['proposals'][0]['skip_reason'],
                      ('uncertain_sentence_boundary', 'missing_asr_timing_for_neighbouring_words'))

    def test_prof_abbreviation_is_uncertain_not_proposal(self):
        text = "Prof. Smith left. He went home."
        result = self.build_with(pcm=make_pcm(), text=text)
        self.assertNotEqual('proposal', result['proposals'][0]['status'])
        self.assertEqual('uncertain_sentence_boundary', result['proposals'][0]['skip_reason'])
        spans = report.get_sentence_spans(text)
        self.assertFalse(spans[0]['certain'])

    def test_grouped_crossboundary_span_is_ambiguous_skip(self):
        segments = [
            {'word': 'Hello', 'start': 0.1, 'end': 0.4},
            {'word': 'world. Second', 'start': 0.41, 'end': 1.3},
            {'word': 'sentence', 'start': 1.31, 'end': 1.6},
            {'word': 'here', 'start': 1.61, 'end': 1.9},
        ]
        result = self.build_with(pcm=make_pcm(), text=TEXT, segments=segments)
        self.assertNotEqual('proposal', result['proposals'][0]['status'])
        self.assertEqual('ambiguous_grouped_word_timing_at_boundary',
                         result['proposals'][0]['skip_reason'])

    def test_unconfined_neighbour_span_has_distinct_safe_skip_reason(self):
        text = 'One two. Three four. Five six.'
        segments = [
            {'word': 'One', 'start': 0.10, 'end': 0.20},
            {'word': 'two. Three four', 'start': 0.21, 'end': 0.50},
            {'word': 'Five', 'start': 0.51, 'end': 0.60},
            {'word': 'six', 'start': 0.61, 'end': 0.70},
        ]
        result = self.build_with(pcm=make_pcm(), text=text, segments=segments)
        boundary = result['proposals'][1]
        self.assertEqual('skipped', boundary['status'])
        self.assertEqual('neighbouring_span_not_confined_to_sentence', boundary['skip_reason'])
        self.assertIsNone(boundary['asr_search_window'])
        self.assertEqual([], result['coordinate_map']['insertions'])

    def test_missing_neighbour_timing_is_skipped(self):
        # 'world'/'Second' untimed (as the validator omits bad tokens): the
        # intra-sentence quiet must not become a boundary proposal.
        segments = [
            {'word': 'Hello', 'start': 0.1, 'end': 0.4},
            {'word': 'sentence', 'start': 1.31, 'end': 1.6},
            {'word': 'here', 'start': 1.61, 'end': 1.9},
        ]
        pcm = make_pcm(gap_start=0.5, gap_end=0.9)
        result = self.build_with(pcm=pcm, text=TEXT, segments=segments)
        self.assertNotEqual('proposal', result['proposals'][0]['status'])
        self.assertEqual('missing_asr_timing_for_neighbouring_words',
                         result['proposals'][0]['skip_reason'])

    def test_internal_dip_without_between_word_quiet_is_skipped(self):
        # Quiet only INSIDE 'world' (0.41-0.44) and loud between words.
        pcm = np.full((RATE * 2, 1), 0.5)
        pcm[int(0.41 * RATE):int(0.44 * RATE)] = 0.0
        result = self.build(pcm=pcm)
        self.assertNotEqual('proposal', result['proposals'][0]['status'])

    def test_abutting_silence_yields_safe_interior_proposal(self):
        # Silence exactly from prev.end to nxt.start: interior with 30ms
        # margins qualifies and must be proposed (V11).
        pcm = np.full((RATE * 2, 1), 0.5)
        pcm[int(0.45 * RATE):int(1.05 * RATE)] = 0.0
        result = self.build(pcm=pcm)
        prop = result['proposals'][0]
        self.assertEqual('proposal', prop['status'])
        mi = prop['safe_quiet_interval']
        self.assertGreaterEqual(mi['start'] - 0.45, 0.03 - 1e-9)
        self.assertGreaterEqual(1.05 - mi['end'], 0.03 - 1e-9)

    def test_stale_take_evidence_rejected(self):
        pcm = make_pcm()
        evidence = make_evidence(pcm)
        evidence['binding']['audio_sha256'] = '0' * 64
        from word_timing import get_timing_fingerprint
        evidence['fingerprint'] = get_timing_fingerprint(evidence['binding'])
        with self.assertRaises(ValueError):
            report.get_sentence_gap_report(text=TEXT, word_spans=evidence, pcm=pcm,
                                           sample_rate=RATE, provenance=make_provenance(pcm))

    def test_wrong_text_evidence_rejected(self):
        pcm = make_pcm()
        evidence = make_evidence(pcm)
        evidence['display_text'] = "Completely different text here."
        with self.assertRaises(ValueError):
            report.get_sentence_gap_report(text=TEXT, word_spans=evidence, pcm=pcm,
                                           sample_rate=RATE, provenance=make_provenance(pcm))

    def test_valid_coalesced_multiword_span_proposal_remains(self):
        # Coalesced "Hello world" segment wholly inside the left sentence.
        segments = [
            {'word': 'Hello world', 'start': 0.1, 'end': 0.45},
            {'word': 'Second sentence here', 'start': 1.05, 'end': 1.9},
        ]
        pcm = make_pcm()
        result = self.build_with(pcm=pcm, text=TEXT, segments=segments)
        self.assertEqual('proposal', result['proposals'][0]['status'])

    def test_cancellation_before_and_after_processing(self):
        with self.assertRaises(ValueError):
            self.build(cancel_check=lambda: True)
        calls = {'n': 0}

        def cancel_late():
            calls['n'] += 1
            return calls['n'] > 3

        with self.assertRaises(ValueError):
            self.build(cancel_check=cancel_late)

    def test_explicit_scalar_falsy_rejected_truthy_parity(self):
        for scalar in (0, False, '', 1):
            with self.assertRaises(ValueError, msg=repr(scalar)):
                self.build(explicit_pause_after_sentence=scalar)
        result = self.build(explicit_pause_after_sentence=[0])
        self.assertEqual('explicit_user_pause', result['proposals'][0]['skip_reason'])

    def test_complex_pcm_rejected(self):
        pcm = make_pcm().astype(np.complex128)
        with self.assertRaises(ValueError):
            report.get_sentence_gap_report(text=TEXT, word_spans=make_evidence(make_pcm()),
                                           pcm=pcm, sample_rate=RATE,
                                           provenance=make_provenance(make_pcm()))

    def test_unverified_file_sha256_rejected(self):
        pcm = make_pcm()
        prov = make_provenance(pcm)
        prov['file_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            report.get_sentence_gap_report(text=TEXT, word_spans=make_evidence(pcm), pcm=pcm,
                                           sample_rate=RATE, provenance=prov)

    def test_public_api_cannot_assert_verified_file_identity(self):
        pcm = make_pcm()
        with self.assertRaises(TypeError):
            report.get_sentence_gap_report(
                text=TEXT, word_spans=make_evidence(pcm), pcm=pcm, sample_rate=RATE,
                provenance=make_provenance(pcm), verified_file_sha256='a' * 64)

    def test_cli_rejects_invalid_analysis_inputs(self):
        for kind in ('nan_pcm', 'inf_pcm', 'high_rate', 'nan_quiet', 'zero_quiet', 'large_quiet'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                audio, pcm = self._cli_fixture(root)
                rate = 384001 if kind == 'high_rate' else RATE
                if kind == 'high_rate':
                    pcm = np.repeat(pcm, 25, axis=0)
                if kind in ('nan_pcm', 'inf_pcm'):
                    pcm[0, 0] = float('nan') if kind == 'nan_pcm' else float('inf')
                sf.write(audio, pcm, rate, subtype='DOUBLE')
                evidence = make_evidence(pcm, duration=len(pcm) / rate)
                (root / 'timings.json').write_text(json.dumps(evidence))
                prov = make_provenance(pcm)
                prov['audio_sha256'] = hashlib.sha256(audio.read_bytes()).hexdigest()
                (root / 'prov.json').write_text(json.dumps(prov))
                args = ['--audio', str(audio), '--text', TEXT,
                        '--word-timings', str(root / 'timings.json'),
                        '--provenance', str(root / 'prov.json'), '--out', str(root / 'out.json')]
                quiet = {'nan_quiet': 'nan', 'zero_quiet': '0', 'large_quiet': '11'}
                if kind in quiet:
                    args.extend(['--min-quiet-seconds', quiet[kind]])
                with self.assertRaises(ValueError):
                    report.main(args)
                self.assertFalse((root / 'out.json').exists())

    def test_partial_frame_bounds_are_clipped(self):
        # Window ending mid-frame; measured end must stay inside window/audio.
        pcm = np.zeros((int(RATE * 0.507), 1))
        segments = [
            {'word': 'Hello', 'start': 0.01, 'end': 0.1},
            {'word': 'world', 'start': 0.1, 'end': 0.2},
            {'word': 'Second', 'start': 0.505, 'end': 0.507},
        ]
        result = self.build_with(pcm=pcm, text=TEXT, segments=segments)
        for prop in result['proposals']:
            mi = prop['measured_quiet_interval']
            if mi is not None:
                self.assertLessEqual(mi['end'], prop['asr_search_window']['end'] + 1e-9)
                self.assertLessEqual(mi['end'], result['duration_seconds'] + 1e-9)

    def test_coordinate_map_identity_and_composition(self):
        identity = report.GapCoordinateMap(2.0)
        self.assertTrue(identity.is_identity())
        self.assertEqual(1.25, identity.map_original_to_derived(1.25))
        self.assertEqual(1.25, identity.map_derived_to_original(1.25))
        m1 = report.GapCoordinateMap(2.0, [(0.5, 0.25)])
        self.assertEqual(2.25, m1.derived_duration)
        self.assertEqual(1.0 + 0.25, m1.map_original_to_derived(1.0))
        self.assertEqual(1.0, m1.map_derived_to_original(1.25))
        m2 = report.GapCoordinateMap(2.25, [(1.25, 0.5)])
        composed = report.compose_coordinate_maps(m1, m2)
        self.assertEqual(m2.map_original_to_derived(m1.map_original_to_derived(1.0)),
                         composed.map_original_to_derived(1.0))
        self.assertAlmostEqual(1.0, composed.map_derived_to_original(
            composed.map_original_to_derived(1.0)))
        with self.assertRaises(ValueError):
            identity.map_original_to_derived(2.5)
        with self.assertRaises(ValueError):
            report.compose_coordinate_maps(m1, report.GapCoordinateMap(9.0))

    def test_coordinate_map_overflow_rejected(self):
        with self.assertRaises(ValueError):
            report.GapCoordinateMap(2.0, [(0.5, 1e308), (1.0, 1e308)])
        with self.assertRaises(ValueError):
            report.GapCoordinateMap(2.0, [(0.5, 1e308)])
        # Finite-but-huge maps remain representable and invertible.
        big = report.GapCoordinateMap(2.0, [(1.0, 1e9)])
        self.assertEqual(1000000002.0, big.derived_duration)

    def test_output_bytes_limit_enforced_on_exact_bytes(self):
        # The CLI file bytes (indent=2, LF) must respect MAX_REPORT_BYTES.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            out = root / 'report.json'
            report.main(['--audio', str(audio), '--text', TEXT,
                         '--word-timings', str(root / 'timings.json'),
                         '--provenance', str(root / 'prov.json'),
                         '--out', str(out)])
            self.assertLessEqual(len(out.read_bytes()), report.MAX_REPORT_BYTES)
            self.assertNotIn(b'\r\n', out.read_bytes())

    def test_cli_writes_report_and_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            original = audio.read_bytes()
            file_digest = hashlib.sha256(original).hexdigest()
            out = root / 'report.json'
            rc = report.main(['--audio', str(audio), '--text', TEXT,
                              '--word-timings', str(root / 'timings.json'),
                              '--provenance', str(root / 'prov.json'),
                              '--out', str(out)])
            self.assertEqual(0, rc)
            data = json.loads(out.read_text())
            self.assertFalse(data['mutates_audio'])
            self.assertEqual('proposal', data['proposals'][0]['status'])
            self.assertEqual(file_digest, data['provenance']['file_sha256'])
            self.assertEqual(original, audio.read_bytes(), 'original take bytes must not change')

    def _cli_fixture(self, root):
        audio = root / 'take.wav'
        pcm = make_pcm()
        sf.write(audio, pcm, RATE, subtype='FLOAT')
        (root / 'timings.json').write_text(json.dumps(make_evidence(pcm)))
        (root / 'prov.json').write_text(json.dumps({
            'audio_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(),
            'text_sha256': hashlib.sha256(TEXT.encode('utf-8')).hexdigest(),
            'provider': 'synthetic-test', 'language': 'en'}))
        return audio, pcm

    def test_cli_out_aliasing_audio_rejected_and_bytes_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            before = audio.read_bytes()
            for alias in (str(audio), str(root / '.' / 'take.wav')):
                with self.assertRaises(ValueError):
                    report.main(['--audio', str(audio), '--text', TEXT,
                                 '--word-timings', str(root / 'timings.json'),
                                 '--provenance', str(root / 'prov.json'),
                                 '--out', alias])
            self.assertEqual(before, audio.read_bytes())

    def test_cli_out_aliasing_textfile_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            text_file = root / 'text.txt'
            text_file.write_text(TEXT)
            before = text_file.read_bytes()
            with self.assertRaises(ValueError):
                report.main(['--audio', str(audio), '--text-file', str(text_file),
                             '--word-timings', str(root / 'timings.json'),
                             '--provenance', str(root / 'prov.json'),
                             '--out', str(text_file)])
            self.assertEqual(before, text_file.read_bytes())

    def test_cli_out_aliasing_timings_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            before = (root / 'timings.json').read_bytes()
            with self.assertRaises(ValueError):
                report.main(['--audio', str(audio), '--text', TEXT,
                             '--word-timings', str(root / 'timings.json'),
                             '--provenance', str(root / 'prov.json'),
                             '--out', str(root / 'timings.json')])
            self.assertEqual(before, (root / 'timings.json').read_bytes())

    def test_cli_stale_cache_fails_closed_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, pcm = self._cli_fixture(root)
            stale = make_evidence(pcm)
            stale['display_text'] = 'Completely different text from another take.'
            (root / 'timings.json').write_text(json.dumps(stale))
            out = root / 'report.json'
            with self.assertRaises(ValueError):
                report.main(['--audio', str(audio), '--text', TEXT,
                             '--word-timings', str(root / 'timings.json'),
                             '--provenance', str(root / 'prov.json'),
                             '--out', str(out)])
            self.assertFalse(out.exists())

    def test_cli_bare_spans_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            (root / 'timings.json').write_text(json.dumps(make_spans()))
            out = root / 'report.json'
            with self.assertRaises(ValueError):
                report.main(['--audio', str(audio), '--text', TEXT,
                             '--word-timings', str(root / 'timings.json'),
                             '--provenance', str(root / 'prov.json'),
                             '--out', str(out)])
            self.assertFalse(out.exists())

    def test_cli_cancellation_after_tmp_write_leaves_no_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            out = root / 'report.json'
            # Pre-existing predictable tmp sentinel must be untouched.
            sentinel = root / 'report.json.tmp'
            sentinel.write_bytes(b'SENTINEL-PREEXISTING-TMP')
            observed = {'written_temp': False}

            def cancel_after_tmp_write():
                temps = list(root.glob('.sentence-gap-*.json'))
                if temps:
                    self.assertTrue(temps[0].read_bytes())
                    observed['written_temp'] = True
                return observed['written_temp']

            with self.assertRaises(ValueError):
                report.main(['--audio', str(audio), '--text', TEXT,
                             '--word-timings', str(root / 'timings.json'),
                             '--provenance', str(root / 'prov.json'),
                             '--out', str(out)], cancel_check=cancel_after_tmp_write)
            self.assertTrue(observed['written_temp'])
            self.assertFalse(out.exists())
            self.assertEqual(b'SENTINEL-PREEXISTING-TMP', sentinel.read_bytes())
            owned = list(root.glob('.sentence-gap-*.json'))
            self.assertEqual([], owned)

    def test_cli_cancellation_after_processing_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            out = root / 'report.json'
            from unittest.mock import patch
            original_build = report._build_sentence_gap_report
            observed = {'built': False}

            def build_then_cancel(**kwargs):
                result = original_build(**kwargs)
                self.assertEqual('proposal', result['proposals'][0]['status'])
                observed['built'] = True
                return result

            with patch.object(report, '_build_sentence_gap_report', side_effect=build_then_cancel):
                with self.assertRaises(ValueError):
                    report.main(['--audio', str(audio), '--text', TEXT,
                                 '--word-timings', str(root / 'timings.json'),
                                 '--provenance', str(root / 'prov.json'),
                                 '--out', str(out)], cancel_check=lambda: observed['built'])
            self.assertTrue(observed['built'])
            self.assertFalse(out.exists())
            self.assertEqual([], list(root.glob('.sentence-gap-*.json')))

    def test_prior_good_report_preserved_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            out = root / 'report.json'
            out.write_bytes(b'PRIOR-GOOD-REPORT')

            def cancel_after_processing():
                temps = list(root.glob('.sentence-gap-*.json'))
                if temps:
                    self.assertTrue(temps[0].read_bytes())
                    return True
                return False

            with self.assertRaises(ValueError):
                report.main(['--audio', str(audio), '--text', TEXT,
                             '--word-timings', str(root / 'timings.json'),
                             '--provenance', str(root / 'prov.json'),
                             '--out', str(out)], cancel_check=cancel_after_processing)
            self.assertEqual(b'PRIOR-GOOD-REPORT', out.read_bytes())
            self.assertEqual([], list(root.glob('.sentence-gap-*.json')))

    def test_bounded_text_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            big = root / 'big.txt'
            big.write_bytes(b'x' * (report.MAX_TEXT_BYTES + 1))
            out = root / 'report.json'
            with self.assertRaises(ValueError):
                report.main(['--audio', str(audio), '--text-file', str(big),
                             '--word-timings', str(root / 'timings.json'),
                             '--provenance', str(root / 'prov.json'),
                             '--out', str(out)])
            self.assertFalse(out.exists())

    def test_bounded_json_rejected_without_full_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            big = root / 'big.json'
            big.write_bytes(b' ' * (1024**2 + 100))
            with self.assertRaises(ValueError):
                report._load_json_bounded(str(big))

    def test_publish_replace_failure_leaves_no_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / 'outdir'
            out_dir.mkdir()
            # --out is an existing directory: publication must fail loudly,
            # preserve the directory, and remove the unique owned temp.
            with self.assertRaises(OSError):
                report._publish_report({'k': 'v'}, str(out_dir))
            self.assertTrue(out_dir.is_dir())
            self.assertEqual([], list(root.glob('.sentence-gap-*.json')))

    def test_cli_out_symlink_to_input_rejected(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _pcm = self._cli_fixture(root)
            link = root / 'link.json'
            try:
                os.symlink(audio, link)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks unavailable')
            before = audio.read_bytes()
            with self.assertRaises(ValueError):
                report.main(['--audio', str(audio), '--text', TEXT,
                             '--word-timings', str(root / 'timings.json'),
                             '--provenance', str(root / 'prov.json'),
                             '--out', str(link)])
            self.assertEqual(before, audio.read_bytes())

    def test_frame_rms_parity_small_cases(self):
        # Chunked RMS must match the direct per-frame definition (stereo max,
        # partial last frame, silent/tiny).
        for pcm in (make_pcm(channels=2), np.zeros((123, 1)), np.full((5, 1), 0.25)):
            db, hop = report._frame_rms_db(pcm, RATE)
            import math
            frames = (len(pcm) + hop - 1) // hop
            for i in range(frames):
                block = pcm[i * hop:(i + 1) * hop]
                rms = np.sqrt(np.mean(block ** 2, axis=0))
                peak = float(np.max(rms))
                expected = 20 * math.log10(peak) if peak > 0 else -np.inf
                if expected == -np.inf:
                    self.assertEqual(-np.inf, db[i])
                else:
                    self.assertAlmostEqual(expected, float(db[i]), delta=1e-9)

    def test_asr_timestamps_alone_do_not_authorize(self):
        # Even with wide ASR gap, loud audio (no quiet) yields no proposal.
        result = self.build(pcm=np.full((RATE * 2, 1), 0.5))
        self.assertEqual(0, result['proposals'][0]['proposed_extension_ms'])
        self.assertFalse(result['authorizes_edit'])


    def test_unmatched_observed_speech_cannot_become_quiet_proposal(self):
        segments = make_segments()
        segments.insert(2, {'word': 'banana', 'start': 0.60, 'end': 0.90})
        result = self.build_with(make_pcm(), TEXT, segments=segments)
        prop = result['proposals'][0]
        self.assertEqual('unmatched_observed_speech_in_search_window', prop['skip_reason'])
        self.assertEqual([], result['coordinate_map']['insertions'])

    def test_left_boundary_certainty_does_not_depend_on_right_terminator(self):
        text = TEXT.rstrip('.')
        result = self.build_with(make_pcm(), text, segments=make_segments(text=text))
        self.assertEqual('proposal', result['proposals'][0]['status'])
        self.assertEqual('terminal_punctuation', result['proposals'][0]['source_boundary']['reason'])

    def test_abbreviations_and_ellipsis_are_uncertain(self):
        for text in ('Dept. Adams left.', 'Messrs. Adams left.', 'Smith et al. agreed.',
                     'Wait… Then go.', 'See item. below here.'):
            with self.subTest(text=text):
                self.assertFalse(report.get_sentence_spans(text)[0]['certain'])

    def test_longest_eligible_interior_wins_over_longer_edge_run(self):
        pcm = make_pcm(gap_start=0.45, gap_end=0.59)
        pcm[int(0.70 * RATE):int(0.83 * RATE)] = 0
        prop = self.build(pcm=pcm)['proposals'][0]
        self.assertEqual('proposal', prop['status'])
        self.assertAlmostEqual(0.70, prop['safe_quiet_interval']['start'])
        self.assertEqual(prop['measured_quiet_interval'], prop['safe_quiet_interval'])

    def test_raw_and_safe_intervals_are_distinct_and_map_uses_safe_start(self):
        result = self.build(pcm=make_pcm(gap_start=0.45, gap_end=1.05))
        prop = result['proposals'][0]
        self.assertAlmostEqual(0.45, prop['measured_quiet_interval']['start'])
        self.assertAlmostEqual(0.48, prop['safe_quiet_interval']['start'])
        self.assertEqual(prop['safe_quiet_interval']['start'],
                         result['coordinate_map']['insertions'][0]['at_original'])
        self.assertEqual(set(prop['measured_quiet_interval']), set(prop['safe_quiet_interval']))

    def test_uniform_low_level_takes_skip_with_or_without_actual_silence(self):
        for gap in (False, True):
            pcm = np.full((RATE * 2, 1), 10 ** (-50 / 20))
            if gap:
                pcm[RATE // 2:RATE] = 0
            prop = self.build(pcm=pcm)['proposals'][0]
            self.assertEqual('take_below_quiet_threshold', prop['skip_reason'])
            self.assertIsNone(prop['measured_quiet_interval'])

    def test_zero_asr_window_is_not_labelled_overlap(self):
        segments = make_segments()
        segments[2]['start'] = segments[1]['end']
        prop = self.build_with(make_pcm(), TEXT, segments=segments)['proposals'][0]
        self.assertEqual('no_gap_in_asr_search_window', prop['skip_reason'])
        self.assertEqual(0, prop['asr_search_window']['seconds'])

    def test_submillisecond_extension_skips_instead_of_invalid_map(self):
        segments = make_segments()
        segments[2]['start'] = 0.5104
        pcm = make_pcm(gap_start=0.45, gap_end=0.52)
        result = self.build_with(pcm, TEXT, segments=segments, min_quiet_seconds=0.0001)
        self.assertEqual('quiet_interval_rounds_to_zero_ms', result['proposals'][0]['skip_reason'])
        self.assertEqual([], result['coordinate_map']['insertions'])

    def test_every_boundary_has_one_record_and_explicit_pause_uses_exported_indices(self):
        text = 'Amber arrives. Birch bends. Cedar calls. Delta departs.'
        segments = [{'word': word, 'start': 0.1 + i * 0.2, 'end': 0.19 + i * 0.2}
                    for i, (word, _, _) in enumerate(__import__('word_timing').get_word_tokens(text))]
        result = self.build_with(make_pcm(), text, segments=segments,
                                 explicit_pause_after_sentence=np.array([1], dtype=np.int64))
        self.assertEqual(list(range(len(report.get_sentence_spans(text)) - 1)),
                         [p['sentence_index'] for p in result['proposals']])
        self.assertEqual('explicit_user_pause', result['proposals'][1]['skip_reason'])
        self.assertTrue(all(p['status'] == 'proposal' or p['skip_reason'] for p in result['proposals']))

    def test_integer_pcm_rejected_and_numpy_real_scalars_normalized(self):
        for dtype in (np.int16, np.uint8):
            with self.assertRaisesRegex(ValueError, 'real float'):
                self.build(pcm=make_pcm().astype(dtype))
        pcm = make_pcm()
        result = report.get_sentence_gap_report(
            text=TEXT, pcm=pcm, sample_rate=np.int64(RATE), word_spans=make_evidence(pcm),
            provenance=make_provenance(pcm), min_quiet_seconds=np.float64(0.12))
        self.assertIs(type(result['sample_rate']), int)
        for value in (True, np.bool_(True)):
            with self.assertRaises(ValueError):
                report._validate_sample_rate(value)

    def test_pcm_digest_known_answer_and_c_order_layout_parity(self):
        arr = np.array([[0., 0.5], [-0.25, 1.]], dtype=np.float64)
        expected = '2e124ad8dcab7f03f7d646a8ce8433c9f08c13faa09f4c2a10c96523b055f036'
        self.assertEqual(expected, report.get_pcm_digest(arr))
        for layout in (arr, np.asfortranarray(arr), arr.astype('>f8'), arr[::-1], arr[:, ::-1]):
            canonical = np.ascontiguousarray(layout, dtype='<f8').tobytes()
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), report.get_pcm_digest(layout))
        self.assertEqual(report.get_pcm_digest(arr[:, 0]), report.get_pcm_digest(arr[:, :1]))

    def test_pcm16_identity_is_decoded_samples_not_authoring_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / 'take.wav'
            authored = np.full((32, 1), 0.123456789)
            sf.write(audio, authored, RATE, subtype='PCM_16')
            decoded, _ = sf.read(audio, dtype='float64', always_2d=True)
            self.assertNotEqual(report.get_pcm_digest(authored), report.get_pcm_digest(decoded))
            self.assertNotEqual(hashlib.sha256(audio.read_bytes()).hexdigest(), report.get_pcm_digest(decoded))

    def test_payload_integrity_covers_observations_and_recomputed_spans(self):
        from word_timing import get_timing_fingerprint, get_validated_word_spans
        pcm = make_pcm()
        evidence = report.get_word_timing_evidence(text=TEXT, pcm=pcm, sample_rate=RATE,
                                                 segments=make_segments(), provider='synthetic-test')
        evidence['segments'][1]['end'] = 0.46
        evidence['spans'] = get_validated_word_spans(TEXT, evidence['segments'], 2.0)
        with self.assertRaisesRegex(ValueError, 'payload'):
            self.build(pcm=pcm, spans=evidence)
        evidence['binding']['payload_sha256'] = report._get_timing_payload_digest(evidence)
        evidence['fingerprint'] = get_timing_fingerprint(evidence['binding'])
        # Consistent regeneration is integrity, never producer attestation.
        self.assertEqual('proposal', self.build(pcm=pcm, spans=evidence)['proposals'][0]['status'])

    def test_finite_validation_is_bounded_and_covers_every_chunk_edge(self):
        from unittest.mock import patch
        original = np.isfinite
        for index in (0, 65535, 65536, 65537):
            pcm = np.ones((65538, 2), dtype=np.float64)
            pcm[index, 1] = np.nan
            sizes = []
            def finite(block):
                sizes.append(len(block))
                return original(block)
            with patch.object(np, 'isfinite', side_effect=finite):
                with self.assertRaisesRegex(ValueError, 'non-finite'):
                    report._prepare_analysis_inputs(TEXT, np.asfortranarray(pcm), RATE, 0.12)
            self.assertLessEqual(max(sizes), 65536)

    def test_window_rms_preserves_global_phase_and_partial_stereo_frame(self):
        rng = np.random.default_rng(1719)
        for rate in (8000, 11025, 16000, 22050, 44100, 384000):
            arr = rng.normal(0, .1, (int(rate * .037) + 7, 2))
            full, hop = report._frame_rms_db(arr, rate)
            for first, last in ((0, 1), (1, len(full)), (len(full) - 1, len(full))):
                window, actual_hop = report._frame_rms_db(arr, rate, first, last)
                self.assertEqual(hop, actual_hop)
                np.testing.assert_array_equal(full[first:last], window)

    def test_sparse_and_full_rms_report_outputs_have_exact_parity(self):
        from unittest.mock import patch
        original = report._frame_rms_db
        baseline = self.build()
        def full_then_slice(arr, rate, first=0, last=None, cancel_check=None):
            values, hop = original(arr, rate, cancel_check=cancel_check)
            return values[first:last], hop
        with patch.object(report, '_frame_rms_db', side_effect=full_then_slice):
            self.assertEqual(report._serialize_report_bytes(baseline), report._serialize_report_bytes(self.build()))

    def test_report_cap_rejects_build_and_publish_preserving_prior_bytes(self):
        from unittest.mock import patch
        result = self.build()
        size = len(report._serialize_report_bytes(result))
        with patch.object(report, 'MAX_REPORT_BYTES', size - 1):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.build()
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / 'report.json'
                out.write_bytes(b'PRIOR')
                with self.assertRaisesRegex(ValueError, 'size limit'):
                    report._publish_report(result, out)
                self.assertEqual(b'PRIOR', out.read_bytes())
                self.assertEqual([], list(Path(tmp).glob('.sentence-gap-*.json')))

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_constants(self):
        for raw in (b'{"binding":{"a":1,"a":2}}', b'{"a":NaN}', b'{"a":Infinity}'):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'bad.json'
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    report._load_json_bounded(path)

    def test_common_120_second_stereo_take_fits_decoded_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / 'take.wav'
            block = np.full((48000, 2), 0.25)
            with sf.SoundFile(audio, 'w', samplerate=48000, channels=2, subtype='PCM_16') as stream:
                for _ in range(120):
                    stream.write(block)
            before = hashlib.sha256(audio.read_bytes()).hexdigest()
            pcm, rate, digest = report._read_wav_bounded(audio, expected_digest=before)
            self.assertEqual(48000, rate)
            self.assertEqual((120 * 48000, 2), pcm.shape)
            self.assertEqual(92160000, pcm.nbytes)
            self.assertEqual(0.25, pcm[-1, 1])
            self.assertEqual(before, digest)
            self.assertEqual(before, hashlib.sha256(audio.read_bytes()).hexdigest())

    def test_compressed_take_above_real_decoded_budget_rejected_before_read(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / 'take.flac'
            with sf.SoundFile(audio, 'w', samplerate=96000, channels=2, format='FLAC') as stream:
                block = np.zeros((96000, 2))
                for _ in range(120):
                    stream.write(block)
            self.assertLess(audio.stat().st_size, report.MAX_AUDIO_BYTES)
            self.assertGreater(120 * 96000 * 2 * 8, report.MAX_DECODED_BYTES)
            with patch.object(sf.SoundFile, 'read') as read:
                with self.assertRaisesRegex(ValueError, 'Decoded audio'):
                    report._read_wav_bounded(audio)
                read.assert_not_called()

    def test_predecode_limits_and_truncated_container_do_not_read_samples(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / 'take.flac'
            sf.write(audio, np.zeros((RATE, 2)), RATE, format='FLAC')
            with patch.object(report, 'MAX_DECODED_BYTES', 100), patch.object(sf.SoundFile, 'read') as read:
                with self.assertRaisesRegex(ValueError, 'Decoded audio'):
                    report._read_wav_bounded(audio)
                read.assert_not_called()
            wav = Path(tmp) / 'broken.wav'
            sf.write(wav, make_pcm(), RATE)
            wav.write_bytes(wav.read_bytes()[:-20])
            with patch.object(sf.SoundFile, 'read') as read:
                with self.assertRaisesRegex(ValueError, 'truncated'):
                    report._read_wav_bounded(wav)
                read.assert_not_called()

    def test_stale_file_provenance_fails_before_decode_and_preserves_prior(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _ = self._cli_fixture(root)
            prov = json.loads((root / 'prov.json').read_text())
            prov['audio_sha256'] = '0' * 64
            (root / 'prov.json').write_text(json.dumps(prov))
            out = root / 'report.json'
            out.write_bytes(b'PRIOR')
            # Stale identity wins even if the underlying audio is also malformed.
            audio.write_bytes(b'not a sound file')
            with patch.object(sf.SoundFile, 'read') as read:
                with self.assertRaisesRegex(ValueError, 'does not match the audio file bytes'):
                    report.main(['--audio', str(audio), '--text', TEXT,
                                 '--word-timings', str(root / 'timings.json'),
                                 '--provenance', str(root / 'prov.json'), '--out', str(out)])
                read.assert_not_called()
            self.assertEqual(b'PRIOR', out.read_bytes())
            self.assertEqual([], list(root.glob('.sentence-gap-*.json')))

    def test_cli_text_presence_and_invalid_text_fail_before_audio(self):
        from unittest.mock import patch
        argv = ['--audio', 'unused.wav', '--word-timings', 'unused.json',
                '--provenance', 'unused-prov.json', '--out', 'unused-report.json']
        with patch.object(report, '_read_wav_bounded') as read:
            with self.assertRaises(SystemExit):
                report.main(argv + ['--text', '', '--text-file', 'also-unused.txt'])
            with self.assertRaises(SystemExit) as empty_file:
                report.main(argv + ['--text-file', ''])
            self.assertEqual(2, empty_file.exception.code)
            with self.assertRaisesRegex(ValueError, 'nonempty text'):
                report.main(argv + ['--text', ''])
            read.assert_not_called()

    def test_windows_replace_retry_and_cancellation_preserve_prior(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'report.json'
            out.write_bytes(b'PRIOR')
            locked = PermissionError('Access is denied')
            actual_replace = report.os.replace
            calls = []
            def retry_then_replace(source, destination):
                calls.append(source)
                if len(calls) == 1:
                    raise locked
                return actual_replace(source, destination)
            with patch.object(report.os, 'replace', side_effect=retry_then_replace) as replace, \
                    patch.object(report.time, 'sleep') as sleep:
                report._publish_report({'ok': True}, out)
                self.assertEqual(2, replace.call_count)
                sleep.assert_called_once_with(0.05)
            self.assertEqual({'ok': True}, json.loads(out.read_bytes()))
            out.write_bytes(b'PRIOR')
            with patch.object(report.os, 'replace', side_effect=locked) as replace, \
                    patch.object(report.time, 'sleep') as sleep:
                with self.assertRaisesRegex(ValueError, 'cancelled'):
                    report._publish_report({'ok': True}, out, cancel_check=lambda: sleep.called)
                self.assertEqual(1, replace.call_count)
            self.assertEqual(b'PRIOR', out.read_bytes())
            self.assertEqual([], list(Path(tmp).glob('.sentence-gap-*.json')))

    def test_cleanup_failure_is_diagnosed_without_masking_publish_failure(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'report.json'
            out.write_bytes(b'PRIOR')
            with patch.object(report.os, 'replace', side_effect=OSError('publish failure')), \
                    patch.object(Path, 'unlink', side_effect=PermissionError('locked temp')), \
                    patch.object(report.sys, 'stderr') as stderr:
                with self.assertRaisesRegex(OSError, 'publish failure'):
                    report._publish_report({'ok': True}, out)
                self.assertTrue(stderr.write.called)
            self.assertEqual(b'PRIOR', out.read_bytes())
            residue = list(Path(tmp).glob('.sentence-gap-*.json'))
            self.assertEqual(1, len(residue))
            residue[0].unlink()


    def test_public_preparation_roundtrips_pcm16_through_real_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _ = self._cli_fixture(root)
            sf.write(audio, make_pcm(loud=0.123456789), RATE, subtype='PCM_16')
            before = audio.read_bytes()
            pcm, rate = sf.read(audio, dtype='float64', always_2d=True)
            evidence = report.get_word_timing_evidence(text=TEXT, pcm=pcm, sample_rate=rate,
                                                     segments=make_segments(), provider='synthetic-test')
            (root / 'timings.json').write_text(json.dumps(evidence))
            prov = make_provenance(pcm)
            prov['audio_sha256'] = hashlib.sha256(before).hexdigest()
            (root / 'prov.json').write_text(json.dumps(prov))
            out = root / 'report.json'
            self.assertEqual(0, report.main(['--audio', str(audio), '--text', TEXT,
                                            '--word-timings', str(root / 'timings.json'),
                                            '--provenance', str(root / 'prov.json'), '--out', str(out)]))
            result = json.loads(out.read_bytes())
            self.assertEqual('proposal', result['proposals'][0]['status'])
            self.assertEqual(prov['audio_sha256'], result['provenance']['file_sha256'])
            self.assertEqual(report.get_pcm_digest(pcm), result['provenance']['audio_sha256'])
            self.assertNotIn('pcm_sha256', result)
            self.assertNotIn('text_sha256', result)
            self.assertEqual(before, audio.read_bytes())

    def test_accepted_identity_still_rehashes_and_rejects_inplace_mutation(self):
        from unittest.mock import patch
        import audio_source_io
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _ = self._cli_fixture(root)
            original_hash = audio_source_io.read_bounded_audio_hash
            calls = []
            def hash_then_change(stream, limit, cancel):
                result = original_hash(stream, limit, cancel)
                calls.append(result[0])
                if len(calls) == 1:
                    with audio.open('r+b') as writer:
                        writer.seek(-4, 2)
                        writer.write(b'\x00\x00\x00\x00')
                return result
            out = root / 'report.json'
            out.write_bytes(b'PRIOR')
            with patch.object(audio_source_io, 'read_bounded_audio_hash', side_effect=hash_then_change):
                with self.assertRaisesRegex(ValueError, 'source changed'):
                    report.main(['--audio', str(audio), '--text', TEXT,
                                 '--word-timings', str(root / 'timings.json'),
                                 '--provenance', str(root / 'prov.json'), '--out', str(out)])
            self.assertEqual(2, len(calls))
            self.assertNotEqual(calls[0], calls[1])
            self.assertEqual(b'PRIOR', out.read_bytes())

    def test_matcher_cancellation_observed_immediately_on_return(self):
        from unittest.mock import patch
        import word_timing
        pcm = make_pcm()
        evidence = make_evidence(pcm)
        original = word_timing.get_validated_word_spans
        matched = []
        def match_then_cancel(*args):
            spans = original(*args)
            matched.append(True)
            return spans
        with patch.object(word_timing, 'get_validated_word_spans', side_effect=match_then_cancel), \
                patch.object(report, '_frame_rms_db') as rms:
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                report.get_sentence_gap_report(text=TEXT, pcm=pcm, sample_rate=RATE,
                                               word_spans=evidence, provenance=make_provenance(pcm),
                                               cancel_check=lambda: bool(matched))
            rms.assert_not_called()
        self.assertEqual([True], matched)

    def test_text_reader_requests_only_byte_limit_plus_one(self):
        import io
        from unittest.mock import patch
        reads = []
        class Reader(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)
        with patch.object(Path, 'open', return_value=Reader(b'x' * (report.MAX_TEXT_BYTES + 2))):
            with self.assertRaises(ValueError):
                report._read_text_bounded('unused')
        self.assertEqual([report.MAX_TEXT_BYTES + 1], reads)

    def test_obviously_oversized_report_rejected_before_acoustics(self):
        from unittest.mock import patch
        # Many certain sentence boundaries, each skipped explicitly, remain
        # subject to the exact final size cap even before acoustic work.
        text = ' '.join('Word%d.' % i for i in range(80))
        tokens = __import__('word_timing').get_word_tokens(text)
        segments = [{'word': word, 'start': i * 0.02, 'end': i * 0.02 + 0.01}
                    for i, (word, _, _) in enumerate(tokens)]
        evidence = make_evidence(make_pcm(), text=text, segments=segments)
        with patch.object(report, 'MAX_REPORT_BYTES', 1000), \
                patch.object(report, '_frame_rms_db', side_effect=AssertionError('acoustics must not run')) as rms:
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.build(pcm=make_pcm(), text=text, spans=evidence)
            rms.assert_not_called()

    def test_rms_cancels_between_chunks_before_processing_next_chunk(self):
        from unittest.mock import patch
        pcm = np.full((RATE * 6, 1), 0.5)
        polls = []
        def cancel_on_second_chunk():
            polls.append(True)
            return len(polls) == 2
        original = np.mean
        with patch.object(np, 'mean', wraps=original) as mean:
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                report._frame_rms_db(pcm, RATE, cancel_check=cancel_on_second_chunk)
            self.assertEqual(1, mean.call_count)
        self.assertEqual(2, len(polls))


if __name__ == '__main__':
    unittest.main()
