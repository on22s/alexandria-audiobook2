import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import alexandria_alignment as alignment
from routers.script import extract_epub_text
from integration_corpus import build_manifest


class EpubCapturedBytesTests(unittest.TestCase):
    def write_epub(self, path):
        with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>'.replace('<container>','<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'))
            z.writestr('book.opf','<package><manifest><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="body"/></spine></package>')
            z.writestr('body.xhtml','<html><body><p>'+('Original café chapter. '*100)+'</p></body></html>')

    def test_bytes_parser_preserves_path_output_without_reopening_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp,'book.epub');self.write_epub(source);raw=source.read_bytes()
            expected=extract_epub_text(str(source))
            source.unlink()
            self.assertEqual(expected,extract_epub_text(str(source),archive_bytes=raw))

    def test_all_archive_guards_reject_same_fixture_in_path_and_bytes_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp,'book.epub');self.write_epub(source);raw=source.read_bytes()
            for setting,limit,message in [('EPUB_MAX_ARCHIVE_BYTES',1,'input limit'),
                                          ('EPUB_MAX_MEMBERS',1,'too many entries'),
                                          ('EPUB_MAX_EXPANDED_BYTES',100,'expands beyond'),
                                          ('EPUB_MAX_EXPANSION_RATIO',1,'expansion ratio')]:
                with self.subTest(setting=setting), patch.object(alignment,setting,limit):
                    for options in ({},{'archive_bytes':raw}):
                        with self.assertRaisesRegex(ValueError,message):alignment.validate_epub_archive(str(source),**options)

    def test_corpus_reads_epub_source_once_and_creates_no_scratch_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp,'book.epub');self.write_epub(source);raw=source.read_bytes()
            original=Path.open;reads=[]
            def observed(path,*args,**kwargs):
                mode=args[0] if args else kwargs.get('mode','r')
                if 'r' in mode:reads.append(str(path))
                return original(path,*args,**kwargs)
            with patch.object(Path,'open',observed), patch('tempfile.TemporaryDirectory',side_effect=AssertionError('scratch snapshot must not be needed')):
                manifest=build_manifest(tmp)
            self.assertEqual([],manifest['errors'])
            self.assertEqual([str(source)],reads)
            self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['books'][0]['source_sha256'])
