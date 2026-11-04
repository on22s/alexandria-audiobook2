import hashlib
import io
import stat
import struct
import unittest
import zipfile
from unittest.mock import patch

import docx_preview as docx


def archive(body='<w:p><w:r><w:t>Synthetic “book” 123 café é.</w:t></w:r></w:p>', *, extra=None, document=None, comment=b''):
    parts = {
        '[Content_Types].xml': f'<Types xmlns="{docx.CT}"><Override PartName="/word/document.xml" ContentType="{docx.MAIN_TYPE}"/></Types>',
        '_rels/.rels': f'<Relationships xmlns="{docx.R}"><Relationship Id="r1" Type="{docx.OFFICE}officeDocument" Target="word/document.xml"/></Relationships>',
        'word/document.xml': document or f'<w:document xmlns:w="{docx.W}"><w:body>{body}</w:body></w:document>',
    }
    parts.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        output.comment = comment
        for name, value in parts.items():
            output.writestr(name, value)
    return buffer.getvalue()


class DocxPreviewTests(unittest.TestCase):
    def test_synthetic_producer_shaped_package(self):
        # Synthetic shape only: not output qualified against Word/LibreOffice.
        extra = {}
        overrides = f'<Override PartName="/word/document.xml" ContentType="{docx.MAIN_TYPE}"/>'
        package = f'<Relationship Id="main" Type="{docx.OFFICE}officeDocument" Target="word/document.xml"/>'
        relationships = ''
        for index, (name, (ns, root, relation, content_type)) in enumerate(docx.METADATA.items()):
            extra[name] = f'<{root} xmlns="{ns}"/>'
            overrides += f'<Override PartName="/{name}" ContentType="{content_type}"/>'
            target = name if name.startswith('docProps/') else name.removeprefix('word/')
            entry = f'<Relationship Id="meta{index}" Type="{relation}" Target="{target}"/>'
            if name.startswith('docProps/'):
                package += entry
            else:
                relationships += entry
        extra['word/styles.xml'] = f'''<w:styles xmlns:w="{docx.W}">
          <w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>
          <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
          <w:style w:type="paragraph" w:styleId="Heading1"><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/></w:pPr></w:style>
          <w:style w:type="paragraph" w:styleId="UnusedList"><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>
        </w:styles>'''
        extra['word/settings.xml'] = f'<w:settings xmlns:w="{docx.W}"><w:zoom w:percent="100"/><w:compat/></w:settings>'
        extra['word/fontTable.xml'] = f'<w:fonts xmlns:w="{docx.W}"><w:font w:name="Calibri"><w:family w:val="swiss"/></w:font></w:fonts>'
        extra['word/numbering.xml'] = f'<w:numbering xmlns:w="{docx.W}"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum></w:numbering>'
        extra['docProps/core.xml'] = '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Synthetic</dc:title></cp:coreProperties>'
        extra['docProps/app.xml'] = '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Synthetic fixture</Application></Properties>'
        extra['word/theme/theme1.xml'] = '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Synthetic"><a:themeElements><a:clrScheme name="Synthetic"/></a:themeElements></a:theme>'
        extra['[Content_Types].xml'] = f'<Types xmlns="{docx.CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>{overrides}</Types>'
        extra['_rels/.rels'] = f'<Relationships xmlns="{docx.R}">{package}</Relationships>'
        extra['word/_rels/document.xml.rels'] = f'<Relationships xmlns="{docx.R}">{relationships}</Relationships>'
        body = '<w:p w:rsidR="12345678"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:bookmarkStart w:id="0" w:name="title"/><w:r><w:rPr><w:b/><w:color w:val="123456"/></w:rPr><w:t>Chapter 42</w:t></w:r><w:bookmarkEnd w:id="0"/></w:p><w:p><w:proofErr w:type="spellStart"/><w:r><w:t>“Hello,” café é 001 — 世界.</w:t><w:br w:type="textWrapping"/><w:lastRenderedPageBreak/><w:t>Next.</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:bottom="1440"/></w:sectPr>'
        self.assertEqual(docx.extract_docx_preview(archive(body, extra=extra))['text'], 'Chapter 42\n\n“Hello,” café é 001 — 世界.\nNext.')
        for dangerous in ('<w:vanish/>', '<w:webHidden/>', '<w:specVanish/>', '<w:numPr/>', '<w:rPrChange/>'):
            bad = dict(extra)
            bad['word/styles.xml'] = extra['word/styles.xml'].replace('<w:keepNext/>', dangerous)
            with self.subTest(dangerous=dangerous), self.assertRaises(ValueError):
                docx.extract_docx_preview(archive(body, extra=bad))
            # Inheritance into the used heading must also refuse.
            bad['word/styles.xml'] = extra['word/styles.xml'].replace('<w:name w:val="Normal"/>', '<w:rPr>' + dangerous + '</w:rPr>')
            with self.assertRaises(ValueError):
                docx.extract_docx_preview(archive(body, extra=bad))
        bad = dict(extra)
        bad['word/styles.xml'] = extra['word/styles.xml'].replace('<w:name w:val="Normal"/>', '<w:basedOn w:val="Heading1"/>')
        with self.assertRaisesRegex(ValueError, 'cycle'):
            docx.extract_docx_preview(archive(body, extra=bad))
        bad = dict(extra)
        bad['word/settings.xml'] = extra['word/settings.xml'].replace('<w:compat/>', '<w:trackRevisions/>')
        with self.assertRaisesRegex(ValueError, 'trackRevisions'):
            docx.extract_docx_preview(archive(body, extra=bad))

    def test_known_metadata_requires_correct_root_and_safe_xml(self):
        for name in docx.METADATA:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'metadata root'):
                docx.extract_docx_preview(archive(extra={name: '<x/>'}))

    def test_truncated_member_error_is_a_malformed_input_refusal(self):
        raw = archive()
        with patch.object(docx.zipfile.ZipFile, 'open', side_effect=EOFError('truncated member')):
            with self.assertRaisesRegex(ValueError, 'Malformed DOCX ZIP/XML'):
                docx.extract_docx_preview(raw)

    def test_zip_directory_bounds_precede_library_index_allocation(self):
        raw = bytearray(archive())
        end = raw.rfind(b'PK\x05\x06')
        struct.pack_into('<HH', raw, end + 8, 257, 257)
        with patch.object(docx.zipfile, 'ZipFile', side_effect=AssertionError('unbounded index')):
            with self.assertRaisesRegex(ValueError, 'ZIP directory'):
                docx.extract_docx_preview(bytes(raw))

    def test_duplicate_encrypted_symlink_and_unsupported_compression(self):
        for kind in ('duplicate', 'encrypted', 'symlink', 'compression', 'count'):
            raw = bytearray(archive())
            central = raw.index(b'PK\x01\x02')
            if kind == 'encrypted':
                struct.pack_into('<H', raw, central + 8, 1)
            elif kind == 'symlink':
                struct.pack_into('<I', raw, central + 38, (stat.S_IFLNK | 0o777) << 16)
            elif kind == 'compression':
                struct.pack_into('<H', raw, central + 10, 99)
            elif kind == 'count':
                end = raw.rfind(b'PK\x05\x06')
                struct.pack_into('<HH', raw, end + 8, 1, 1)
            else:
                buffer = io.BytesIO(raw)
                with zipfile.ZipFile(buffer, 'a') as output:
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore', UserWarning)
                        output.writestr('word/document.xml', '<x/>')
                raw = buffer.getvalue()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                docx.extract_docx_preview(bytes(raw))

    def test_exact_text_and_source_binding(self):
        raw = archive('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chapter 42</w:t></w:r></w:p>'
                      '<w:p/><w:p><w:r><w:t xml:space="preserve"> “Hello,” café é 001 </w:t><w:tab/><w:t>&lt;tag&gt;</w:t><w:br/><w:t>Next.</w:t></w:r></w:p>')
        result = docx.extract_docx_preview(raw)
        expected = 'Chapter 42\n\n\n\n “Hello,” café é 001 \t<tag>\nNext.'
        self.assertEqual(expected, result['text'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['original_sha256'])
        self.assertEqual(hashlib.sha256(expected.encode()).hexdigest(), result['text_sha256'])
        self.assertEqual(result, docx.extract_docx_preview(raw))
        self.assertTrue(result['warnings'])

    def test_ambiguous_body_structures_are_specific_refusals(self):
        for name in ('tbl', 'footnoteReference', 'endnoteReference', 'ins', 'del', 'moveFrom', 'moveTo',
                     'commentRangeStart', 'headerReference', 'footerReference', 'fldSimple', 'fldChar',
                     'instrText', 'txbxContent', 'drawing', 'pict', 'hyperlink', 'unknown'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                docx.extract_docx_preview(archive(f'<w:p><w:r><w:t>Safe</w:t><w:{name}/></w:r></w:p>'))

    def test_unsupported_package_parts_are_never_silently_omitted(self):
        for name in ('word/footnotes.xml', 'word/endnotes.xml', 'word/comments.xml', 'word/header1.xml',
                     'word/footer1.xml', 'word/vbaProject.bin',
                     'word/media/image.png', 'unknown.xml'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsupported DOCX package part'):
                docx.extract_docx_preview(archive(extra={name: '<x/>'}))

    def test_unsafe_paths(self):
        for name in ('../evil.xml', '/evil.xml', 'C:/evil.xml', 'word\\evil.xml', 'word/./evil.xml',
                     'word//evil.xml', 'WORD/DOCUMENT.XML'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsafe or duplicate'):
                raw = archive(extra={name.replace('\\', '/'): '<x/>'})
                if '\\' in name:
                    raw = raw.replace(name.replace('\\', '/').encode(), name.encode())
                docx.extract_docx_preview(raw)

    def test_entities_encoding_and_malformed_xml(self):
        for document in ('<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>',
                         '<!DOCTYPE x SYSTEM "file:///private"><x/>',
                         '<?xml version="1.0" encoding="UTF-16"?><x/>',
                         '<x>', '<!--ignored--><x/>', '<?instruction unsafe?><x/>'):
            with self.subTest(document=document), self.assertRaises(ValueError):
                docx.extract_docx_preview(archive(document=document))
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            docx.extract_docx_preview(archive(document='<x/>'.encode('utf-16')))

    def test_relationships_do_not_fetch_or_traverse(self):
        for target in ('https://example.invalid/source', '../document.xml', '/word/document.xml'):
            xml = f'<Relationships xmlns="{docx.R}"><Relationship Id="r1" Type="{docx.OFFICE}officeDocument" Target="{target}"/></Relationships>'
            with self.subTest(target=target), self.assertRaises(ValueError):
                docx.extract_docx_preview(archive(extra={'_rels/.rels': xml}))
        xml = f'<Relationships xmlns="{docx.R}"><Relationship Id="r2" Type="external" Target="https://example.invalid" TargetMode="External"/></Relationships>'
        with self.assertRaises(ValueError):
            docx.extract_docx_preview(archive(extra={'word/_rels/document.xml.rels': xml}))

    def test_bounds_and_malformed_zip(self):
        raw = archive()
        for setting in ('MAX_ARCHIVE', 'MAX_ENTRIES', 'MAX_EXPANDED', 'MAX_XML', 'MAX_TEXT', 'MAX_DEPTH', 'MAX_NODES', 'MAX_RATIO'):
            with self.subTest(setting=setting), patch.object(docx, setting, 1), self.assertRaises(ValueError):
                docx.extract_docx_preview(raw)
        with self.assertRaises(ValueError):
            docx.extract_docx_preview(b'not a zip')

    def test_hidden_numbering_unknown_attributes_and_empty_text_refuse(self):
        for body in ('<w:p/>', '<w:p w:unknown="1"><w:r><w:t>x</w:t></w:r></w:p>',
                     '<w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>x</w:t></w:r></w:p>',
                     '<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>x</w:t></w:r></w:p>',
                     '<w:p>outside<w:r><w:t>x</w:t></w:r></w:p>'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                docx.extract_docx_preview(archive(body))
