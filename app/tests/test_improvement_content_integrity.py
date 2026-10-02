"""Real content artifacts reject order/text conflicts before publication."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from tests.test_preparer_run_state import preparer
import alexandria_alignment as alignment
from ebooklib import epub
from tests.test_enricher_preflight_json import load_enricher
from tests import test_preparer_batch_outcomes as batch_support
from tests import test_voice_dataset_merge as merge_support
from voice_dataset_merge import get_source_records, merge_voice_datasets


class ImprovementContentIntegrityTests(unittest.TestCase):
    def test_real_epub_uses_spine_and_excludes_navigation_and_unlisted_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = epub.EpubBook()
            book.set_identifier('order'); book.set_title('Order'); book.set_language('en')
            chapters = []
            for title in ('SECOND', 'FIRST', 'UNLISTED'):
                chapter = epub.EpubHtml(title=title, file_name=title+'.xhtml', lang='en')
                chapter.content = f'<html><body><p>{title} chapter.</p></body></html>'
                book.add_item(chapter); chapters.append(chapter)
            nav = epub.EpubNav(); book.add_item(nav); book.add_item(epub.EpubNcx())
            book.toc = (chapters[1], chapters[0])
            book.spine = ['nav', chapters[1], chapters[0]]
            source = Path(tmp)/'order.epub'; epub.write_epub(str(source), book)
            text = alignment.load_source(str(source))
            self.assertLess(text.index('FIRST'), text.index('SECOND'))
            self.assertNotIn('UNLISTED', text)
            # The chapter contributes its HTML title and body; navigation would add a third.
            self.assertEqual(2, text.count('FIRST'))

    def test_missing_spine_document_is_an_error_not_an_empty_or_partial_source(self):
        book = SimpleNamespace(spine=[('missing', 'yes')], get_item_with_id=lambda _: None)
        with patch.object(alignment, 'validate_epub_archive'), patch.object(alignment.epub, 'read_epub', return_value=book):
            with self.assertRaisesRegex(ValueError, 'spine references missing'):
                alignment.load_epub('fixture.epub')

    def test_batch_rejects_swapped_rewritten_and_dropped_words_without_counting_success(self):
        batch = [{'segment_idx': 17, 'text': "Alice can't open 12 doors."},
                 {'segment_idx': 42, 'text': 'Bob closed the window.'}]
        for response in ([batch[1]['text'], batch[0]['text']],
                         ['Alice can open 12 doors.', batch[1]['text']],
                         ["Alice can't open 13 doors.", batch[1]['text']],
                         ['Unrelated text.', batch[1]['text']]):
            with self.subTest(response=response):
                llm = SimpleNamespace(create_chat_completion=lambda **_: {'choices':[{'message':{'content':json.dumps(response)}}]})
                stats = {'llm_success':0, 'llm_fail':0, 'sanitize_changed':0}
                self.assertIsNone(preparer._annotate_batch(llm, batch, alignment, 2,
                                  {'llm_infer':0, 'sanitize':0}, stats))
                self.assertEqual(0, stats['llm_success'])
                self.assertEqual(1, stats['llm_batch_fail'])

    def test_prosody_punctuation_case_and_unicode_composition_are_admitted(self):
        text = "Élodie can't open 12 doors."
        annotated = "*E\u0301LODIE*... can't open 12 doors!"
        result = preparer.get_validated_annotation(annotated, text, None, alignment)
        self.assertIn('12 doors!', result)
        merge = Mock(return_value='Verified source words.')
        self.assertEqual('Verified source words.', preparer.get_validated_annotation(
            annotated, text, ['Verified', 'source', 'words.'], SimpleNamespace(merge_annotations_with_source=merge)))
        merge.assert_called_once()

    def test_failed_singleton_annotation_keeps_original_text_and_audio(self):
        # The existing artifact helper asserts all three persisted source texts
        # and decodes each saved WAV. A bad singleton must take that same path.
        helper = batch_support.PreparerBatchOutcomeTests()
        text, calls = helper.run_annotation(rewritten_chunk='Second.')
        self.assertIn('1 failed', text)
        self.assertEqual(4, len(calls))

    def test_duplicate_transcript_conflict_preserves_previous_output_and_cleans_staging(self):
        wav = merge_support.VoiceDatasetMergeTests().make_wav(180)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root/'merged.zip'
            for name, text in [('a.zip','Alice opened the door.'), ('b.zip','Bob closed the window.')]:
                with zipfile.ZipFile(root/name, 'w') as archive:
                    archive.writestr('train/clip.wav', wav)
                    archive.writestr('metadata.jsonl', json.dumps({'audio_filepath':'train/clip.wav','text':text})+'\n')
            output.write_bytes(b'previous output')
            with self.assertRaisesRegex(ValueError, 'Conflicting transcripts.*b.zip'):
                merge_voice_datasets([root/'b.zip',root/'a.zip'], output)
            self.assertEqual(b'previous output', output.read_bytes())
            self.assertFalse(list(root.glob('.merged.zip.*.tmp')))

    def test_version_three_merge_cannot_hide_an_existing_transcript_conflict(self):
        wav = merge_support.VoiceDatasetMergeTests().make_wav(180)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = [root/'a.zip', root/'b.zip']; output=root/'merged.zip'
            for path, text in zip(paths, ['first', 'second']):
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('train/clip.wav', wav)
                    archive.writestr('metadata.jsonl', json.dumps({'audio_filepath':'train/clip.wav','text':text}))
            with zipfile.ZipFile(output,'w') as archive:
                archive.writestr('merge_manifest.json',json.dumps({'version':3,'sources':get_source_records(paths)}))
            prior=output.read_bytes()
            with self.assertRaisesRegex(ValueError,'Conflicting transcripts'):
                merge_voice_datasets(paths,output)
            self.assertEqual(prior,output.read_bytes())

    def test_empty_enrichment_selection_does_not_acquire_a_lease_or_load_a_model(self):
        module, provider = load_enricher()
        chunk={'text':'Original', 'custom':{'keep':True}}
        with patch.object(module,'acquire_gpu_lock') as acquire:
            enricher=module.LLMEnricher('unused.gguf', [])
            result=enricher.enrich_transcript_chunk(chunk)
            self.assertEqual(chunk,result); self.assertIsNot(chunk,result)
            acquire.assert_not_called(); provider.Llama.assert_not_called()
            enricher.close()

    def test_cli_no_selected_fields_publishes_unchanged_rows_without_gpu_work(self):
        import sys
        module, provider = load_enricher()
        with tempfile.TemporaryDirectory() as tmp:
            source,output=Path(tmp)/'input.json',Path(tmp)/'output.json'
            rows=[{'text':'Original','speaker':'ALICE','custom':{'keep':True}}]
            source.write_text(json.dumps(rows))
            with patch.object(module,'acquire_gpu_lock') as acquire, patch.object(sys,'argv',[
                    'llm_enricher.py','--model-path','unused.gguf','--input-file',str(source),'--output-file',str(output)]):
                module.main()
            self.assertEqual(rows,json.loads(output.read_text()))
            acquire.assert_not_called();provider.Llama.assert_not_called()
