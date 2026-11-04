import unittest

from docx_publication import capture_docx_group, get_docx_group_names
from script_selection_binding import _read_source_snapshot
from source_encoding import read_source_text
from tests import test_docx_upload
from tests.test_docx_preview import archive
from routers import script


class DocxAdmissionTests(unittest.IsolatedAsyncioTestCase):
    environment = test_docx_upload.DocxUploadTests.environment
    accept = test_docx_upload.DocxUploadTests.accept
    upload = test_docx_upload.DocxUploadTests.upload
    async def test_group_changes_refused_by_selection_and_generation(self):
        for kind in ('original', 'receipt', 'text'):
            for mutation in ('missing', 'modified'):
                with self.subTest(kind=kind, mutation=mutation):
                    async with self.environment() as (_, uploads, client):
                        response = await self.accept(client, archive())
                        self.assertEqual(200, response.status_code, response.text)
                        name = response.json()['stored_filename']
                        path = uploads / name
                        group = capture_docx_group(path)
                        self.assertEqual(group['text_bytes'], _read_source_snapshot(path, include_bytes=True)[1])
                        self.assertEqual(group['text_bytes'].decode(), read_source_text(path)[0])
                        member = uploads / get_docx_group_names(path)[kind]
                        if mutation == 'missing':
                            member.unlink()
                        else:
                            member.write_bytes(b'changed')
                        selected = await client.post('/api/uploads/select', json={'filename': name})
                        self.assertIn(selected.status_code, (404, 409))
                        with self.assertRaises(ValueError):
                            _read_source_snapshot(path, include_bytes=True)
                        with self.assertRaises(ValueError):
                            read_source_text(path)
                        with self.assertRaises(ValueError):
                            script.get_validated_batch_script_source({'input_path': str(path),
                                'filename': name, 'prepared_source': {'identity': (), 'text': 'cached'}})

    def test_ordinary_text_is_not_docx(self):
        self.assertIsNone(get_docx_group_names('ordinary.txt'))
        self.assertIsNone(get_docx_group_names('book-docx.txt'))


if __name__ == '__main__':
    unittest.main()
