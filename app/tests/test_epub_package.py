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
            self.assertTrue(get_epub_package(archive, include_details=True)[2]['narrow_refines_repair_applied'])
        with self.archive('<package><meta broken">Fixture</meta></package>') as archive:
            with self.assertRaises(ET.ParseError):
                get_epub_package(archive)

    def test_text_extraction_first_root_is_preserved_but_review_refuses_ambiguity(self):
        with self.archive(roots=['book.opf', 'alternative.opf']) as archive:
            self.assertEqual('book.opf', get_epub_package(archive)[0])
            self.assertEqual({'rootfile_count': 2, 'selected_rootfile_index': 0,
                              'narrow_refines_repair_applied': False},
                             get_epub_package(archive, include_details=True)[2])
            with self.assertRaisesRegex(ValueError, 'ambiguous publication'):
                get_epub_package(archive, max_xml_bytes=4096, require_unique=True)

    def test_an_unknown_declared_encoding_is_a_value_error(self):
        # #2375: ET.fromstring raises LookupError (not ET.ParseError) for an
        # unknown declared encoding, escaping both parse sites.
        container = ('<?xml version="1.0" encoding="bogus"?>'
                     '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        with self.archive(container=container) as archive:
            with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                get_epub_package(archive)
        with self.archive('<?xml version="1.0" encoding="bogus"?><package/>') as archive:
            with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                get_epub_package(archive)

    def test_a_multibyte_declared_encoding_is_a_value_error(self):
        # E2: expat refuses a declaration it cannot stream with
        # ValueError('multi-byte encodings are not supported'), not
        # LookupError. Both parse sites must map it to the documented refusal.
        for codec in ('shift_jis', 'utf-16-le', 'utf-16-be', 'utf-32-le'):
            declaration = f'<?xml version="1.0" encoding="{codec}"?>'
            container = (declaration +
                         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
            with self.subTest(site='container', codec=codec), \
                    self.archive(container=container) as archive:
                with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                    get_epub_package(archive)
            with self.subTest(site='opf', codec=codec), \
                    self.archive(declaration + '<package/>') as archive:
                with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                    get_epub_package(archive)

    def test_unusable_python_codec_declarations_are_value_errors(self):
        # #2370/#2375: Expat hands ``punycode``/``idna``/``undefined`` to
        # Python's codec, which raises UnicodeError (not LookupError or the
        # multi-byte ValueError). The shared unusable-codec decision refuses
        # the declaration before that raw codec text can escape.
        for codec in ('punycode', 'idna', 'undefined'):
            declaration = f'<?xml version="1.0" encoding="{codec}"?>'
            container = (declaration +
                         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
            with self.subTest(site='container', codec=codec), \
                    self.archive(container=container) as archive:
                with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                    get_epub_package(archive)
            with self.subTest(site='opf', codec=codec), \
                    self.archive(declaration + '<package/>') as archive:
                with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                    get_epub_package(archive)

    def test_utf32_container_and_opf_are_refused_by_name(self):
        # L1 rev1: the package readers share the one UTF-32 decision, so a
        # UTF-32 container or OPF is named as UTF-32, not reported as a bare
        # Expat parse error (#2448).
        container = ('<container xmlns="urn:oasis:names:tc:opendocument:'
                     'xmlns:container"><rootfiles><rootfile full-path="book.opf"/>'
                     '</rootfiles></container>')
        with self.assertRaisesRegex(
                ValueError, r'UTF-32.*META-INF/container\.xml'), \
                self.archive(container=container.encode('utf-32')) as archive:
            get_epub_package(archive)
        with self.assertRaisesRegex(
                ValueError, r'UTF-32.*book\.opf'), \
                self.archive('<package/>'.encode('utf-32')) as archive:
            get_epub_package(archive)

    def test_a_stylesheet_pi_before_the_declaration_is_a_named_refusal(self):
        # #2452 N1: a real declaration after a PI is malformed XML. Expat's
        # XML_ERROR_XML_DECL is mapped to the named refusal, not a bare
        # ParseError; the PI's bogus encoding is not what decides it.
        pi = ('<?xml-stylesheet type="text/xsl" href="s.xsl" '
              'encoding="idna"?>')
        for declaration in ('<?xml version="1.0" encoding="utf-8"?>',
                            '<?xml version="1.0" encoding="idna"?>'):
            container = (pi + declaration +
                         '<container xmlns="urn:oasis:names:tc:opendocument:'
                         'xmlns:container"><rootfiles><rootfile '
                         'full-path="book.opf"/></rootfiles></container>')
            with self.subTest(site='container', declaration=declaration), \
                    self.archive(container=container) as archive:
                with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                    get_epub_package(archive)
            with self.subTest(site='opf', declaration=declaration), \
                    self.archive(pi + declaration + '<package/>') as archive:
                with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                    get_epub_package(archive)

    def test_a_stylesheet_pi_without_a_declaration_is_not_refused(self):
        # Guard the #2452 fix from overreach: a PI alone is not the XML
        # declaration, so its encoding pseudo-attribute cannot refuse a
        # well-formed package.
        pi = ('<?xml-stylesheet type="text/xsl" href="s.xsl" '
              'encoding="idna"?>')
        container = (pi + '<container xmlns="urn:oasis:names:tc:opendocument:'
                     'xmlns:container"><rootfiles><rootfile '
                     'full-path="book.opf"/></rootfiles></container>')
        with self.archive(container=container) as archive:
            self.assertEqual('book.opf', get_epub_package(archive)[0])

    def test_a_utf16_document_with_a_python_only_declaration_is_refused(self):
        # The declaration bytes are UTF-16, so only a prolog decoded with the
        # BOM/signature encoding can name the codec; the same shared decision
        # still refuses it before Expat hands it to Python (Rule 15).
        from epub_package import EpubXmlEncodingError, parse_epub_xml
        for codec in ('punycode', 'idna', 'undefined'):
            raw = (f'<?xml version="1.0" encoding="{codec}"?>'
                   '<package/>').encode('utf-16')
            with self.subTest(codec=codec), \
                    self.assertRaises(EpubXmlEncodingError):
                parse_epub_xml(raw)

    def test_padded_python_codec_declarations_are_value_errors(self):
        # Gate-2 B1: XML allows arbitrary whitespace before ``encoding=``, and
        # the old 256/512-byte prolog window could not see a declaration padded
        # past it, so punycode/idna/undefined escaped to Expat and Python. The
        # declaration is read to its closing ``?>`` now, with and without a
        # UTF-8 BOM, so every padded form is the documented refusal.
        for codec in ('punycode', 'idna', 'undefined'):
            for pad in (230, 1000, 10000):
                declaration = ('<?xml version="1.0"'.encode('ascii')
                               + b' ' * pad + f' encoding="{codec}"?>'.encode('ascii'))
                container = (declaration +
                             b'<container xmlns="urn:oasis:names:tc:opendocument:'
                             b'xmlns:container"><rootfiles><rootfile '
                             b'full-path="book.opf"/></rootfiles></container>')
                with self.subTest(site='container', codec=codec, pad=pad), \
                        self.archive(container=container) as archive:
                    with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                        get_epub_package(archive)
                with self.subTest(site='container_bom', codec=codec, pad=pad), \
                        self.archive(container=b'\xef\xbb\xbf' + container) as archive:
                    with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                        get_epub_package(archive)
                for bom, site in ((b'', 'opf'), (b'\xef\xbb\xbf', 'opf_bom')):
                    with self.subTest(site=site, codec=codec, pad=pad), \
                            self.archive(bom + declaration + b'<package/>') as archive:
                        with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                            get_epub_package(archive)

    def test_a_declaration_beyond_the_read_limit_is_malformed(self):
        # The declaration is read up to its closing ``?>`` with a 64 KiB cap.
        # Beyond the cap the declaration is malformed, not silently handed to
        # Expat with an unclassified codec: both sites take the documented
        # refusal.
        from epub_package import EpubXmlEncodingError, parse_epub_xml
        declaration = (b'<?xml version="1.0" encoding="utf-8"'
                       + b' ' * (64 * 1024) + b'?>')
        with self.assertRaises(EpubXmlEncodingError):
            parse_epub_xml(declaration + b'<package/>')
        container = (declaration +
                     b'<container xmlns="urn:oasis:names:tc:opendocument:'
                     b'xmlns:container"><rootfiles><rootfile '
                     b'full-path="book.opf"/></rootfiles></container>')
        with self.archive(container=container) as archive:
            with self.assertRaisesRegex(ValueError, 'unknown encoding'):
                get_epub_package(archive)

    def test_a_missing_container_is_not_reported_as_an_encoding_problem(self):
        # #2375: read_member's KeyError is a LookupError too, so the encoding
        # handler must not absorb a missing META-INF/container.xml.
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
        with zipfile.ZipFile(io.BytesIO(raw.getvalue())) as archive:
            with self.assertRaises(KeyError):
                get_epub_package(archive)

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

    def test_root_alias_member_is_refused_before_parsing_in_both_modes(self):
        container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfiles><rootfile full-path="."/></rootfiles></container>')
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('META-INF/container.xml', container)
            archive.writestr('.', '<package/>')
        with zipfile.ZipFile(io.BytesIO(raw.getvalue())) as archive:
            self.assertIn('.', archive.namelist())
            for options in ({}, {'max_xml_bytes': 4096, 'require_unique': True}):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'Unsafe'):
                    get_epub_package(archive, **options)

    def test_review_confines_package_member_names_without_extracting(self):
        for path in ('../book.opf', '/book.opf', 'C:/book.opf', 'dir\\book.opf', './book.opf'):
            with self.subTest(path=path), self.archive(path=path) as archive:
                if path in archive.namelist():
                    self.assertEqual(path, get_epub_package(archive)[0])
                else:
                    self.assertEqual('dir\\book.opf', path)
                    self.assertIn('dir/book.opf', archive.namelist())
                    with self.assertRaises(KeyError):
                        get_epub_package(archive)
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    get_epub_package(archive, max_xml_bytes=4096, require_unique=True)


if __name__ == '__main__':
    unittest.main()
