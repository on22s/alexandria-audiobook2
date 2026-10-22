import io
import unittest
import warnings
import xml.etree.ElementTree as ET
import zipfile

from epub_package import get_epub_package


class EpubPackageTests(unittest.TestCase):
    def archive(self, package='<package/>', *, path='book.opf', roots=None,
                container=None, duplicate=None):
        if container is None:
            roots = roots if roots is not None else [path]
            container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles>' + ''.join(f'<rootfile full-path="{name}"/>' for name in roots)
                         + '</rootfiles></container>')
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('META-INF/container.xml', container)
            archive.writestr(path, package)
            if duplicate is not None:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(path, duplicate)
        return zipfile.ZipFile(io.BytesIO(raw.getvalue()))

    def test_package_preserves_epub2_and_epub3_namespaces(self):
        for namespace in ('', ' xmlns="http://www.idpf.org/2007/opf"'):
            with self.subTest(namespace=namespace), self.archive(
                    f'<package{namespace}><metadata><title>Fixture title</title></metadata></package>') as archive:
                path, package = get_epub_package(archive, max_xml_bytes=4096, require_unique=True)
                self.assertEqual('book.opf', path)
                self.assertEqual('Fixture title', next(iter(next(iter(package)))).text)

    def test_only_known_sigil_fragment_is_repaired(self):
        with self.archive('<package><meta property="identifier" refines">"#pub-i">Fixture</meta></package>') as archive:
            self.assertEqual('Fixture', get_epub_package(archive)[1][0].text)
        with self.archive('<package><meta broken">Fixture</meta></package>') as archive:
            with self.assertRaises(ET.ParseError):
                get_epub_package(archive)

    def test_text_extraction_first_root_is_preserved_but_review_refuses_ambiguity(self):
        with self.archive(roots=['book.opf', 'alternative.opf']) as archive:
            self.assertEqual('book.opf', get_epub_package(archive)[0])
            with self.assertRaisesRegex(ValueError, 'ambiguous publication'):
                get_epub_package(archive, max_xml_bytes=4096, require_unique=True)

    def test_missing_rootfile_fails_in_both_modes(self):
        with self.archive(roots=[]) as archive:
            for options in ({}, {'max_xml_bytes': 4096, 'require_unique': True}):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'no rootfile'):
                    get_epub_package(archive, **options)

    def test_review_bounds_container_and_opf_without_changing_default_parser(self):
        cases = [{'package': '<package><metadata>' + 'x' * 5000 + '</metadata></package>'},
                 {'container': '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                               '<rootfile full-path="book.opf"/>' + ' ' * 5000 + '</container>'}]
        for options in cases:
            with self.subTest(options=list(options)), self.archive(**options) as archive:
                self.assertEqual('book.opf', get_epub_package(archive)[0])
                with self.assertRaisesRegex(ValueError, 'review limit'):
                    get_epub_package(archive, max_xml_bytes=4096, require_unique=True)

    def test_review_refuses_duplicate_selected_member_instead_of_guessing(self):
        with self.archive(duplicate='<package><title>Last entry</title></package>') as archive:
            self.assertEqual('Last entry', get_epub_package(archive)[1][0].text)
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                get_epub_package(archive, max_xml_bytes=4096, require_unique=True)

    def test_review_confines_package_member_names_without_extracting(self):
        for path in ('../book.opf', '/book.opf', 'C:/book.opf', 'dir\\book.opf', './book.opf'):
            with self.subTest(path=path), self.archive(path=path) as archive:
                self.assertEqual(path, get_epub_package(archive)[0])
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    get_epub_package(archive, max_xml_bytes=4096, require_unique=True)


if __name__ == '__main__':
    unittest.main()
