"""Native compressed fixtures prove admission limits before member expansion."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile
import archive_utils
from tests import test_voicelab_pipeline_scripts as pipeline
voice_profiler = pipeline.voice_profiler


class ProfilerArchiveBudgetTests(unittest.TestCase):
    def test_oversize_reference_is_rejected_before_decompression(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dataset.zip'
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('ref.wav', b'x' * 4096)
            original = path.read_bytes()
            with patch.object(voice_profiler,'MAX_REF_WAV_BYTES',128), \
                 patch.object(zipfile.ZipFile,'open', side_effect=AssertionError('must reject before decompression')), \
                 contextlib.redirect_stdout(io.StringIO()) as logs:
                self.assertIsNone(voice_profiler.get_ref_wav(str(path)))
            self.assertIn('exceeds 128-byte limit',logs.getvalue())
            self.assertEqual(original,path.read_bytes())

    def test_oversize_epub_chapter_is_rejected_before_decompression(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'book.epub'
            pipeline.ProfilerEpubNavigationTests().build_epub(path)
            original = path.read_bytes()
            actual_open = zipfile.ZipFile.open
            opened = []
            def open_member(zf,member,*args,**kwargs):
                name = member.filename if isinstance(member,zipfile.ZipInfo) else member
                opened.append(name)
                return actual_open(zf,member,*args,**kwargs)
            with patch.object(voice_profiler,'MAX_EPUB_CHAPTER_BYTES',32), \
                 patch.object(zipfile.ZipFile,'open',open_member), contextlib.redirect_stdout(io.StringIO()) as logs:
                self.assertEqual('',voice_profiler.extract_epub_passage(str(path)))
            self.assertNotIn('OEBPS/chapter.xhtml',opened)
            self.assertIn('exceeds 32-byte limit',logs.getvalue())
            self.assertEqual(original,path.read_bytes())

    def test_total_expanded_budget_limits_many_individually_small_members(self):
        container='<container><rootfile full-path="book.opf"/></container>'
        opf='<package><manifest><item id="c" href="c.xhtml"/></manifest><spine>'+('<itemref idref="c"/>'*8)+'</spine></package>'
        chapter='<p>Short paragraph.</p>'  # no usable prose; scanner would keep reading
        budget=len(container.encode())+len(opf.encode())+len(chapter.encode())
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.epub'
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('META-INF/container.xml',container)
                zf.writestr('book.opf',opf)
                zf.writestr('c.xhtml',chapter)
            with patch.object(voice_profiler,'MAX_EPUB_PASSAGE_BYTES',budget), contextlib.redirect_stdout(io.StringIO()) as logs:
                self.assertEqual('',voice_profiler.extract_epub_passage(str(path)))
            self.assertIn('exceeds 0-byte limit',logs.getvalue())

    def test_stream_overrun_is_capped_even_if_declared_size_is_small(self):
        source=io.BytesIO(b'x'*100)
        reader=Mock(wraps=source.read)
        source.read=reader
        fake=SimpleNamespace(getinfo=lambda _:SimpleNamespace(file_size=1),open=Mock(return_value=source))
        with self.assertRaisesRegex(ValueError,'exceeds 8-byte limit'):
            archive_utils.read_zip_member_bounded(fake,'member',8)
        reader.assert_called_once_with(9)

    def test_small_reference_roundtrips_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'dataset.zip'
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('ref.wav',b'known PCM fixture bytes')
                zf.writestr('ref_text.txt','日本語')
            self.assertEqual(b'known PCM fixture bytes',voice_profiler.get_ref_wav(str(path)))
            self.assertEqual('日本語',voice_profiler.get_ref_text(str(path)))


class ProfilerEpubCacheTests(unittest.TestCase):
    def test_actual_batch_reuses_same_book_and_resets_cache_next_invocation(self):
        features={'mean_f0':120.,'std_f0':10.,'mean_rms':.04,'speaking_rate':3.,'mean_centroid':2000.,'mean_rolloff':3000.,'smoothness':.4,'flatness':.03,'duration':5.}
        entries=[{'id':str(i),'dataset_id':f'narrator_new_voice_same_book_char{i}_vol01','zip_source':'fixture.zip'} for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest=root/'manifest.json';manifest.write_text(json.dumps(entries));before=manifest.read_bytes()
            pipeline.ProfilerEpubNavigationTests().build_epub(root/'same book.epub')
            find=Mock(wraps=voice_profiler.find_epub)
            extract=Mock(wraps=voice_profiler.extract_epub_passage)
            argv=['voice_profiler.py','--manifest',str(manifest),'--dry_run','--epub-dir',tmp]
            with patch.object(sys,'argv',argv),patch.object(voice_profiler,'get_ref_wav',return_value=b'fixture'), \
                 patch.object(voice_profiler,'analyze_ref_wav',return_value=features),patch.object(voice_profiler,'get_ref_text',return_value='reference'), \
                 patch.object(voice_profiler,'find_epub',find),patch.object(voice_profiler,'extract_epub_passage',extract),contextlib.redirect_stdout(io.StringIO()) as logs:
                self.assertEqual(0,voice_profiler.main())
                self.assertEqual(1,find.call_count)
                self.assertEqual(1,extract.call_count)
                self.assertEqual(0,voice_profiler.main())
                self.assertEqual(2,find.call_count)
                self.assertEqual(2,extract.call_count)
            self.assertIn('Done: 3 profiles processed, 0 errors',logs.getvalue())
            self.assertEqual(before,manifest.read_bytes())
