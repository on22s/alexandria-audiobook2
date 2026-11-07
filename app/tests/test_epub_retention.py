"""EPUB retention for read-aloud (#1779): exact bytes, quota, duplicate, rollback."""
import os
import tempfile
import unittest

from source_upload_publication import (get_retained_epub_name, get_retained_epub_path,
                                       publish_retained_epub_upload, create_source_upload_stage)


class EpubRetentionTests(unittest.TestCase):
    def test_retained_name_and_path(self):
        self.assertEqual("book.source.epub", get_retained_epub_name("book.txt"))
        self.assertEqual(os.path.join("d", "book.source.epub"),
                         get_retained_epub_path(os.path.join("d", "book.txt")))

    def test_exact_bytes_quota_and_single_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = b"PK\x03\x04" + b"x" * 1024
            stage = create_source_upload_stage(tmp, limit=8192)
            stage.write(raw)
            self.assertEqual(len(raw), stage.size)
            path = publish_retained_epub_upload(stage, "book.source.epub", limit=8192)
            self.assertTrue(path.endswith("book.source.epub"))
            with open(path, "rb") as f:
                self.assertEqual(raw, f.read())
            # Quota accounting counts the named retained file.
            total = sum(entry.stat(follow_symlinks=False).st_size
                        for entry in __import__("os").scandir(tmp))
            self.assertEqual(len(raw), total)

    def test_collision_steps_to_numbered_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = create_source_upload_stage(tmp, limit=8192)
            first.write(b"first")
            first_path = publish_retained_epub_upload(first, "book.source.epub", limit=8192)
            self.assertTrue(first_path.endswith("book.source.epub"))
            second = create_source_upload_stage(tmp, limit=8192)
            second.write(b"second")
            second_path = publish_retained_epub_upload(second, "book.source.epub", limit=8192)
            self.assertTrue(second_path.endswith("book_2.source.epub"))
            with open(second_path, "rb") as f:
                self.assertEqual(b"second", f.read())

    def test_oversized_retention_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = create_source_upload_stage(tmp, limit=4)
            stage.write(b"1234")
            with self.assertRaises(ValueError):
                publish_retained_epub_upload(stage, "book.source.epub", limit=2)
            stage.close()

    def test_stage_rejects_unretained_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = create_source_upload_stage(tmp, limit=8192)
            stage.write(b"x")
            with self.assertRaises(ValueError):
                publish_retained_epub_upload(stage, "book.epub", limit=8192)
            stage.close()


if __name__ == "__main__":
    unittest.main()
