"""Chapter delivery proofs cover selected renders and retained old renders."""
import copy
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydub import AudioSegment
from pydub.generators import Sine

import export_quality as quality
import chapter_export_quality
import project
from tests import test_chapter_output_integrity as fixtures


class ChapterExportQualityTests(unittest.TestCase):
    def fixture(self, root, *, publish=False):
        return fixtures.RetainedChapterIntegrityTests().manager(Path(root), publish=publish)[:2]

    def manifest(self, out):
        return json.loads((out / 'manifest.json').read_text())

    def saved(self, out):
        return {path.name: path.read_bytes() for path in out.iterdir() if path.is_file()}

    def test_selected_native_fallback_and_mastered_have_one_scan_and_reuse_has_none(self):
        for fallback, mastering in ((False, None), (True, None), (False, {})):
            with self.subTest(fallback=fallback, mastering=mastering), tempfile.TemporaryDirectory() as root:
                manager, out = self.fixture(root)
                reports = []
                from chapter_timing_cache import ensure_chapter_timing_facts
                with patch('chapter_timing_cache.ensure_chapter_timing_facts',
                        side_effect=(lambda *a, **kw: None) if fallback else ensure_chapter_timing_facts), \
                        patch.object(quality, 'validate_finished_export', wraps=quality.validate_finished_export) as scan:
                    result = manager.export_chapters(fmt='wav', mastering=mastering,
                        export_quality_callback=reports.append)
                    self.assertTrue(result[0], result)
                    self.assertEqual(2, scan.call_count)
                    self.assertEqual(2, len(reports))
                    for row, report in zip(self.manifest(out)['chapters'], reports):
                        source = row['export_quality_plan']['source_shape']
                        self.assertEqual(row['duration_ms'], 1000 * source['frames'] / source['sample_rate'])
                        self.assertEqual(source['frames'], report['decoded_frames'])
                        self.assertEqual('verified', report['status'])
                        self.assertEqual(quality.get_export_file_version(out / row['file']), report['file_version'])
                        self.assertEqual(row['output_sha256'], report['sha256'])
                    scan.reset_mock()
                    reports.clear()
                    with patch.object(manager, 'get_chapter_audio', side_effect=AssertionError('unneeded render')):
                        result = manager.export_chapters(fmt='wav', changed_only=True, mastering=mastering,
                            export_quality_callback=reports.append)
                    self.assertTrue(result[0], result)
                    self.assertEqual(0, scan.call_count, 'valid byte/plan receipts avoid a second PCM scan')
                    self.assertEqual(2, len(reports))

    def test_zero_exit_short_output_without_transport_cannot_publish_any_chapter(self):
        with tempfile.TemporaryDirectory() as root:
            manager, out = self.fixture(root, publish=True)
            before = self.saved(out)
            short = Sine(440, sample_rate=24000).to_audio_segment(duration=200)
            def short_encoder(piece, path, fmt, **kwargs):
                short.export(path, format=fmt).close()
            with patch.object(project, '_export_audio_segment', side_effect=short_encoder):
                with self.assertRaises(quality.ExportQualityError):
                    manager.export_chapters(fmt='wav')
            self.assertEqual(before, self.saved(out))
            self.assertFalse(list(out.glob('.chapter-export-*')))

    def test_missing_receipt_is_validated_and_current_policy_can_reject_retained_gap(self):
        with tempfile.TemporaryDirectory() as root:
            manager, out = self.fixture(root)
            rows = manager.load_chunks()
            audio = Sine(440, sample_rate=24000).to_audio_segment(duration=400).apply_gain(-24)
            audio += AudioSegment.silent(duration=1200, frame_rate=24000) + audio
            audio.export(Path(root) / rows[0]['audio_path'], format='wav').close()
            reports = []
            self.assertTrue(manager.export_chapters(fmt='wav', export_quality={
                'max_unexpected_silence_seconds': .5}, export_quality_callback=reports.append)[0])
            self.assertEqual('review', reports[0]['status'])
            before = self.saved(out)
            with self.assertRaises(quality.ExportQualityError):
                manager.export_chapters(fmt='wav', changed_only=True, export_quality={
                    'max_unexpected_silence_seconds': .5, 'reject_unexpected_silence': True})
            self.assertEqual(before, self.saved(out))
            manifest = self.manifest(out)
            manifest['chapters'][1].pop('export_quality_report')
            (out / 'manifest.json').write_text(json.dumps(manifest))
            with patch.object(quality, 'validate_finished_export', wraps=quality.validate_finished_export) as scan:
                result = manager.export_chapters(fmt='wav', changed_only=True,
                    export_quality={'max_unexpected_silence_seconds': .5})
            self.assertTrue(result[0], result)
            self.assertEqual(1, scan.call_count)

    def test_historical_plan_survives_changed_unselected_sources_and_legacy_needs_independent_proof(self):
        with tempfile.TemporaryDirectory() as root:
            manager, out = self.fixture(root, publish=True)
            manifest = self.manifest(out)
            old = copy.deepcopy(manifest['chapters'][1])
            rows = manager.load_chunks()
            Sine(880, sample_rate=24000).to_audio_segment(duration=1700).export(
                Path(root) / rows[1]['audio_path'], format='wav').close()
            result = manager.export_chapters(fmt='wav', chapters=[0])
            self.assertTrue(result[0], result)
            retained = self.manifest(out)['chapters'][1]
            self.assertEqual(old['export_quality_plan'], retained['export_quality_plan'])
            self.assertEqual(old['output_sha256'], retained['output_sha256'])
            manifest = self.manifest(out)
            manifest['chapters'][1].pop('export_quality_plan')
            (out / 'manifest.json').write_text(json.dumps(manifest))
            before = self.saved(out)
            result = manager.export_chapters(fmt='wav', chapters=[0])
            self.assertFalse(result[0], result)
            self.assertIn('independent frame proof', result[1])
            self.assertEqual(before, self.saved(out))
            # Selecting the changed chapter rebuilds its proof from its sources.
            self.assertTrue(manager.export_chapters(fmt='wav', chapters=[1])[0])
            self.assertNotEqual(old['export_quality_plan'], self.manifest(out)['chapters'][1]['export_quality_plan'])

    def test_legacy_plan_can_migrate_only_from_current_matching_indexed_source_facts(self):
        with tempfile.TemporaryDirectory() as root:
            manager, out = self.fixture(root, publish=True)
            manifest = self.manifest(out)
            for row in manifest['chapters']:
                row.pop('export_quality_plan')
                row.pop('export_quality_report')
            (out / 'manifest.json').write_text(json.dumps(manifest))
            with patch.object(manager, 'get_chapter_audio', side_effect=AssertionError('unneeded render')), \
                    patch.object(quality, 'validate_finished_export', wraps=quality.validate_finished_export) as scan:
                result = manager.export_chapters(fmt='wav', changed_only=True)
            self.assertTrue(result[0], result)
            self.assertEqual(2, scan.call_count)
            self.assertTrue(all(row['export_quality_plan']['source_shape'] for row in self.manifest(out)['chapters']))

    def test_inconsistent_recorded_source_clock_cannot_shift_retained_offsets(self):
        with tempfile.TemporaryDirectory() as root:
            manager, out = self.fixture(root, publish=True)
            manifest = self.manifest(out)
            expected = copy.deepcopy(manifest)
            manifest['chapters'][0]['export_quality_plan']['source_shape']['frames'] *= 2
            (out / 'manifest.json').write_text(json.dumps(manifest))
            with patch.object(manager, 'get_chapter_audio', side_effect=AssertionError('unneeded render')):
                result = manager.export_chapters(fmt='wav', changed_only=True)
            self.assertTrue(result[0], result)
            current = self.manifest(out)
            for before, after in zip(expected['chapters'], current['chapters']):
                for key in ('duration_ms', 'start_ms', 'end_ms', 'output_sha256', 'export_quality_plan'):
                    self.assertEqual(before[key], after[key])

    def test_mixed_rate_first_subset_uses_each_skipped_chapters_independent_clock(self):
        rates = (96000, 8000, 8000, 16000, 24000, 8000)
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as root:
                manager = project.ProjectManager(root)
                rows = []
                for index, rate in enumerate(rates):
                    path = Path(root) / f'source-{index}.wav'
                    frames = rate // 10 + 13 + index * 7
                    AudioSegment((2000).to_bytes(2, 'little') * frames, sample_width=2,
                        frame_rate=rate, channels=1).export(path, format='wav').close()
                    rows.append(dict(id=index, uid=f'line-{index}', speaker='NARRATOR', status='done',
                        text=f'Chapter {index // 2 + 1}' if index % 2 == 0 else
                            'Ordinary prose with enough words to be narrative.',
                        audio_path=path.name, pause_after=(17, 75, 61, 43, 29, 0)[index]))
                manager.save_chunks(rows)
                from chapter_timing_cache import ensure_chapter_timing_facts
                with patch('chapter_timing_cache.ensure_chapter_timing_facts',
                        side_effect=(lambda *a, **kw: None) if fallback else ensure_chapter_timing_facts):
                    self.assertTrue(manager.export_chapters(fmt='wav')[0])
                    out = Path(root) / project.CHAPTER_EXPORT_DIR
                    full = self.manifest(out)['chapters'][2]
                    shutil.rmtree(out)
                    result = manager.export_chapters(fmt='wav', chapters=[2])
                self.assertTrue(result[0], result)
                subset = self.manifest(out)['chapters'][0]
                for key in ('index', 'start_ms', 'end_ms', 'duration_ms', 'export_quality_plan', 'output_sha256'):
                    self.assertEqual(full[key], subset[key])

    def test_fallback_cancellation_during_unselected_frame_planning_returns_cancelled(self):
        with tempfile.TemporaryDirectory() as root:
            manager, out = self.fixture(root)
            rows = manager.load_chunks()
            # Two clips force promotion/gap planning to check cancellation in
            # the unselected chapter, before any output directory is created.
            middle = {**rows[0], 'id': 2, 'uid': 'extra-first-chapter-line',
                      'text': 'Ordinary narration within the first chapter.'}
            manager.save_chunks([rows[0], middle, rows[1]])
            cancelled = False
            durations = chapter_export_quality.get_chapter_durations_ms
            def cancel_during_planning(*args, **kwargs):
                nonlocal cancelled
                cancelled = True
                return durations(*args, **kwargs)
            with patch('chapter_timing_cache.ensure_chapter_timing_facts', return_value=None), \
                    patch.object(chapter_export_quality, 'get_chapter_durations_ms', side_effect=cancel_during_planning), \
                    patch.object(project, '_export_audio_segment') as encode:
                result = manager.export_chapters(fmt='wav', chapters=[1], cancel_check=lambda: cancelled)
            self.assertEqual((False, 'Export cancelled'), result)
            encode.assert_not_called()
            self.assertFalse(out.exists())

    def test_source_edit_between_phases_rolls_back_set_and_observer_failure_stays_successful(self):
        with tempfile.TemporaryDirectory() as root:
            manager, out = self.fixture(root, publish=True)
            before = self.saved(out)
            replace, moved = project.os.replace, False
            def observe_move(source, target):
                nonlocal moved
                result = replace(source, target)
                if Path(target).parent == out and Path(target).suffix == '.wav':
                    moved = True
                return result
            edited = False
            def edit_between_phases():
                nonlocal edited
                if moved and not edited:
                    edited = True
                    rows = manager.load_chunks()
                    rows[0]['uid'] = 'new-editor-identity'
                    manager.save_chunks(rows)
                return False
            with patch.object(project.os, 'replace', side_effect=observe_move):
                result = manager.export_chapters(fmt='wav', cancel_check=edit_between_phases)
            self.assertFalse(result[0], result)
            self.assertIn('rows changed', result[1])
            self.assertEqual(before, self.saved(out))
            self.assertEqual('new-editor-identity', manager.load_chunks()[0]['uid'])
            def broken(report):
                raise RuntimeError('observer failed')
            with self.assertLogs('AlexandriaUI', 'WARNING'):
                result = manager.export_chapters(fmt='wav', export_quality_callback=broken)
            self.assertTrue(result[0], result)


if __name__ == '__main__':
    unittest.main()
