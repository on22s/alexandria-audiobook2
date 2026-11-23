"""Both EPUB experiment CLIs discover the active runtime's uploaded books."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ebooklib import epub

from experiments import epub_extraction_fidelity as fidelity, epub_structural as structural


def write_epub(path):
    book = epub.EpubBook()
    book.set_identifier('observatory')
    book.set_title('Observatory Log')
    book.set_language('en')
    chapter = epub.EpubHtml(title='A Chapter', file_name='chapter.xhtml', lang='en')
    chapter.content = '<html><body><h1>A Chapter</h1><p>The observatory opened at dusk.</p></body></html>'
    book.add_item(chapter)
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = (chapter,)
    book.spine = ['nav', chapter]
    epub.write_epub(str(path), book)


class EpubExperimentUploadsTests(unittest.TestCase):
    def test_discovery_and_explicit_inputs_under_either_upload_layout(self):
        for custom in (False, True):
            with self.subTest(custom=custom), TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / 'repo'
                app = repo / 'app'
                data = root / 'data'
                default_uploads, custom_uploads = app / 'uploads', data / 'uploads'
                default_uploads.mkdir(parents=True)
                custom_uploads.mkdir(parents=True)
                uploads, other = ((custom_uploads, default_uploads) if custom else
                                  (default_uploads, custom_uploads))
                names = ('alpha.epub', 'beta.epub', 'gamma.epub', 'zeta.epub')
                for name in reversed(names):
                    write_epub(uploads / name)
                write_epub(other / 'decoy.epub')
                (uploads / 'nested').mkdir()
                write_epub(uploads / 'nested' / 'ignored.epub')
                explicit = root / 'explicit.epub'
                write_epub(explicit)
                output = root / 'fidelity.json'
                env = {'ALEXANDRIA_DATA_DIR': str(data) if custom else ''}
                with patch.dict('os.environ', env), patch.object(fidelity, 'REPO', str(repo)), \
                        patch.object(fidelity, 'APP', str(app)), patch.object(structural, 'REPO', str(repo)):
                    for specified in (False, True):
                        with self.subTest(tool='fidelity', specified=specified):
                            argv = ['fidelity', '--out', str(output)]
                            if specified:
                                argv += ['--books', str(explicit)]
                            with patch('sys.argv', argv), redirect_stdout(StringIO()):
                                self.assertEqual(0, fidelity.main())
                            rows = json.loads(output.read_bytes())['results']
                            self.assertEqual([explicit.name] if specified else list(names),
                                             [row['book'] for row in rows])
                            self.assertTrue(all(row['characters'] > 0 for row in rows))
                        with self.subTest(tool='structural', specified=specified):
                            argv = ['structural', '--epubs', str(explicit)] if specified else ['structural']
                            with patch('sys.argv', argv), redirect_stdout(StringIO()) as log:
                                structural.main()
                            rows = [line.split()[0] for line in log.getvalue().splitlines() if ' flat ' in line]
                            self.assertEqual([explicit.name] if specified else list(names[:3]), rows, log.getvalue())
                            self.assertNotIn('FAILED', log.getvalue())
