import unittest

from project import group_into_chunks, get_chunk_char_limit, MAX_CHUNK_CHARS


CHINESE_LIMIT = int(MAX_CHUNK_CHARS / 2.72)  # 183
JAPANESE_LIMIT = int(MAX_CHUNK_CHARS / 2.09)  # 239


def make_entry(text, speaker="NARRATOR"):
    return {"speaker": speaker, "text": text, "instruct": ""}


def strip_whitespace(text):
    return "".join(text.split())


def join_chunk_texts(chunks):
    return " ".join(chunk["text"] for chunk in chunks)


class GetChunkCharLimitTests(unittest.TestCase):
    def test_latin_text_keeps_base(self):
        self.assertEqual(get_chunk_char_limit("Hello world, an English sentence."),
                         MAX_CHUNK_CHARS)
        self.assertEqual(get_chunk_char_limit("Hello world", 120), 120)

    def test_chinese_text_reduces_limit(self):
        self.assertEqual(get_chunk_char_limit("汉字" * 20), CHINESE_LIMIT)
        self.assertEqual(CHINESE_LIMIT, 183)

    def test_japanese_text_reduces_limit(self):
        self.assertEqual(get_chunk_char_limit("日本語のテキスト"), JAPANESE_LIMIT)
        self.assertEqual(get_chunk_char_limit("あいうえお" * 10), JAPANESE_LIMIT)
        self.assertEqual(JAPANESE_LIMIT, 239)

    def test_hangul_keeps_base(self):
        self.assertEqual(get_chunk_char_limit("안녕하세요" * 10), MAX_CHUNK_CHARS)

    def test_mixed_text_uses_letter_majority(self):
        # one Han letter among far more Latin letters stays at the base
        self.assertEqual(get_chunk_char_limit("汉" + "abcdefghij"), MAX_CHUNK_CHARS)
        # six Han letters among fewer Latin letters uses the CJK limit
        self.assertEqual(get_chunk_char_limit("汉字汉字汉字" + "abc"), CHINESE_LIMIT)

    def test_letterless_text_keeps_base(self):
        self.assertEqual(get_chunk_char_limit(""), MAX_CHUNK_CHARS)
        self.assertEqual(get_chunk_char_limit("0123456789"), MAX_CHUNK_CHARS)
        self.assertEqual(get_chunk_char_limit("!!!???...,,,"), MAX_CHUNK_CHARS)


class GroupIntoChunksTests(unittest.TestCase):
    # Sentences end in punctuation: unpunctuated text counts as structural
    # (heading-like) and is never merged, which would hide the limit entirely.
    def test_english_script_keeps_base_chunks(self):
        entries = [make_entry("word " * 11 + "end.") for _ in range(12)]
        chunks = group_into_chunks(entries)
        self.assertLess(len(chunks), len(entries), "entries must merge for this to test the limit")
        self.assertGreater(max(len(chunk["text"]) for chunk in chunks), CHINESE_LIMIT)
        for chunk in chunks:
            self.assertLessEqual(len(chunk["text"]), MAX_CHUNK_CHARS)
        self.assertEqual(join_chunk_texts(chunks),
                         " ".join(entry["text"] for entry in entries))

    def test_chinese_script_uses_smaller_chunks(self):
        chinese_entries = [make_entry("汉" * 59 + "。") for _ in range(12)]
        english_entries = [make_entry("word " * 11 + "end.") for _ in range(12)]
        chinese_chunks = group_into_chunks(chinese_entries)
        english_chunks = group_into_chunks(english_entries)
        self.assertLess(len(chinese_chunks), len(chinese_entries), "Chinese entries must still merge")
        for chunk in chinese_chunks:
            self.assertLessEqual(len(chunk["text"]), CHINESE_LIMIT)
        self.assertGreater(len(chinese_chunks), len(english_chunks))
        self.assertEqual(strip_whitespace(join_chunk_texts(chinese_chunks)),
                         strip_whitespace(" ".join(entry["text"] for entry in chinese_entries)))

    def test_single_long_chinese_entry_splits(self):
        entries = [make_entry("汉" * 600)]
        chunks = group_into_chunks(entries)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk["text"]), CHINESE_LIMIT)
        self.assertEqual(strip_whitespace(join_chunk_texts(chunks)), "汉" * 600)
        # A book chunked before these limits (#1353) stored one 500-character
        # chunk. Delivery retry must still recognise it as unedited, keep its
        # size, and refuse a real edit.
        import copy
        from delivery_review import get_delivery_retry_chunks
        entries = [dict(make_entry("汉" * 600), instruct_unchecked=True)]
        legacy = group_into_chunks(entries, include_source_indices=True, cjk_limits=False)
        self.assertEqual([500, 100], [len(chunk["text"]) for chunk in legacy])
        retried = get_delivery_retry_chunks(entries, entries, copy.deepcopy(legacy))
        self.assertEqual([500, 100], [len(row["text"]) for row in retried])
        edited = copy.deepcopy(legacy)
        edited[0]["text"] = "字" + edited[0]["text"][1:]
        with self.assertRaisesRegex(ValueError, "Editor text or delivery changed"):
            get_delivery_retry_chunks(entries, entries, edited)


if __name__ == "__main__":
    unittest.main()
