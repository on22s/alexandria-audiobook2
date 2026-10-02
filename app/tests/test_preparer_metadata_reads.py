import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tests import test_preparer_run_state as support

p = support.preparer


class StopBeforeModel(RuntimeError):
    pass


class PreparerMetadataReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, old)
        Path('book.wav').write_bytes(b'fixture audio identity only')
        epub = p.alignment.epub
        book = epub.EpubBook()
        book.set_identifier('metadata-read-fixture')
        book.set_title('A Native Title')
        book.add_author('The Author')
        chapter = epub.EpubHtml(title='Chapter', file_name='chapter.xhtml', lang='en')
        chapter.content = '<h1>Chapter</h1><p>Fixture words.</p>'
        book.add_item(chapter)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = [chapter]
        book.spine = ['nav', chapter]
        epub.write_epub('book.epub', book)

    def run_main(self, extra=(), orchestrate=True):
        records = []
        def admit(directory, identity, fresh=False):
            phase = None
            for index, value in enumerate(sys.argv):
                if value == '--phase':
                    phase = sys.argv[index + 1]
            records.append({'phase': phase, 'identity': identity})
            if phase is not None:
                raise StopBeforeModel()
        def child(command, **kwargs):
            with patch.object(sys, 'argv', command[1:]):
                with self.assertRaises(StopBeforeModel):
                    p.main()
            return SimpleNamespace(returncode=0)
        argv = ['preparer', '--audio', 'book.wav', '--source', 'book.epub',
                '--model', 'model.gguf', '--enrich-with-llm', '--llm-model-path', 'llm.gguf', *extra]
        with patch.object(p, 'LLAMA_CPP_AVAILABLE', True), patch.object(p, 'acquire_run_lock', return_value=1), patch.object(p, 'ensure_run_manifest', side_effect=admit), patch.object(p, 'cleanup_run_artifacts'), patch.object(p.subprocess, 'run', side_effect=child), patch.object(p.alignment.epub, 'read_epub', wraps=p.alignment.epub.read_epub) as reads, patch.object(sys, 'argv', argv):
            if orchestrate:
                with self.assertRaises(SystemExit) as exit_code:
                    p.main()
                self.assertEqual(0, exit_code.exception.code)
            else:
                with self.assertRaises(StopBeforeModel):
                    p.main()
        return records, reads.call_count

    def test_orchestration_reads_only_for_output_identity_and_annotation_title(self):
        records, count = self.run_main()
        self.assertEqual(2, count)
        self.assertEqual([None, 'asr', 'enrich', 'annotate'], [row['phase'] for row in records])
        self.assertTrue(all(row['identity'] == records[0]['identity'] for row in records))
        self.assertEqual('A_Native_Title_The_Author.zip', records[0]['identity']['options']['output'])
        self.assertIsNone(records[0]['identity']['options']['book_title'])

    def test_pinned_output_and_explicit_title_do_not_read_epub_metadata(self):
        records, count = self.run_main(['--output=folder/pinned.zip', '--book-title', 'Explicit Title'])
        self.assertEqual(0, count)
        self.assertTrue(all(row['identity'] == records[0]['identity'] for row in records))
        self.assertEqual('folder/pinned.zip', records[-1]['identity']['options']['output'])
        self.assertEqual('Explicit Title', records[-1]['identity']['options']['book_title'])

    def test_standalone_generic_phase_still_preserves_derived_identity(self):
        for phase in ('asr', 'enrich', 'annotate'):
            with self.subTest(phase=phase):
                records, count = self.run_main(['--phase', phase, '--output', 'folder/output.zip'], orchestrate=False)
                self.assertEqual(1, count)
                self.assertEqual('folder/A_Native_Title_The_Author.zip', records[0]['identity']['options']['output'])

    def test_phase_builder_can_forward_resolved_output_without_changing_other_values(self):
        command = p.build_phase_command('asr', ['--output=output.zip', '--character', 'annotate'], output='folder/pinned.zip')
        self.assertEqual(['--output', 'folder/pinned.zip'], command[-2:])
        self.assertIn('annotate', command)

    def test_annotation_keeps_native_zip_name_and_wave_title_tag(self):
        import numpy as np
        import soundfile as sf
        import mutagen.wave
        Path('dataset_temp').mkdir()
        sf.write('dataset_temp/audio_24k_scratch.wav',
                 np.sin(np.arange(28800) * .05).astype('float32') * .1, 24000)
        Path('dataset_temp/asr_segments.json').write_text(json.dumps({
            'detected_lang': 'en', 'word_segments': [{'word': 'Hello.',
            'start': 0, 'end': 1.2, 'confidence': 1, 'speaker': 'ONLY'}]}))
        llm = SimpleNamespace(create_chat_completion=lambda **kwargs: {
            'choices': [{'message': {'content': 'Hello.'}}]})
        argv = ['preparer', '--audio', 'book.wav', '--source', 'book.epub',
                '--model', 'model.gguf', '--phase', 'annotate', '--chunk-size', '1',
                '--min-chunk-duration', '1']
        with patch.object(p, 'LLAMA_CPP_AVAILABLE', True), patch.object(p, 'acquire_run_lock', return_value=1), patch.object(p, 'ensure_run_manifest'), patch.object(p, 'validate_scratch_path'), patch.object(p, 'is_verified_artifact', side_effect=lambda root, identity, name, path: name == 'asr'), patch.object(p, 'get_source_alignment_prescan', return_value=(None, (1, 1, 0, None))), patch.object(p, '_load_llm', return_value=llm), patch.object(p, 'log_gpu_stats'), patch.object(p, '_calculate_chunk_snr', return_value=80), patch.object(sys, 'argv', argv):
            self.assertEqual(0, p.main())
        self.assertTrue(Path('A_Native_Title_The_Author.zip').is_file())
        sample = next(Path('dataset_temp').glob('sample_*.wav'))
        self.assertEqual(['A Native Title'], mutagen.wave.WAVE(sample).tags['TALB'].text)
        waveform, rate = sf.read(sample)
        self.assertEqual(24000, rate)
        self.assertAlmostEqual(1.2, len(waveform) / rate)
