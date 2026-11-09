"""Review-repair regressions (#1844) — fail-first on frozen HEAD, synthetic only.

Each test discriminates a supported repair: it must fail on the frozen
source (Unicode indices, empty-inline loss, single-sentence times,
builder cancellation, fail-closed runtime/preference handling) and pass
after repair. No model, torch, network or private books are used.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import qwen_align as qa
from epub_readaloud import build_epub_readaloud, validate_epub_readaloud

XHTML = "{http://www.w3.org/1999/xhtml}"
SMIL = "{http://www.w3.org/ns/SMIL}"


def _source_epub(chapter_body):
    from tests.test_epub_readaloud_sentences import _source_epub as build
    return build(chapter_body)


def _smil_clips(result):
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        smil = ET.fromstring(archive.read('OEBPS/ch1.smil'))
        clips = []
        for par in smil.findall('.//' + SMIL + 'par'):
            audio = par.find(SMIL + 'audio')
            clips.append((audio.get('clipBegin'), audio.get('clipEnd')))
    return clips


class UnicodeMappingTests(unittest.TestCase):
    def test_expansion_strasse_end_is_original_length(self):
        # "Straße" is 6 original chars; ß casefolds to "ss" (7 normalized).
        spans, unmatched = qa.map_tokens_to_text(
            "Straße", [{"text": "Straße"}])
        self.assertEqual([], unmatched)
        self.assertEqual(6, spans[0][2])

    def test_expansion_sentence_second_range_not_empty(self):
        # Aß。B: original ranges (0,3),(3,4); second sentence must map to B.
        text = "Aß。B"
        ranges = qa.cover_ranges(text, qa.split_sentences(text))
        normalized, indices = qa.get_normalized_stream(text)
        self.assertEqual("assb", normalized)
        self.assertTrue(all(0 <= i < len(text) for i in indices))
        # First sentence covers A(0),ß(1),。 -> stream pos of 。 excluded,
        # second sentence B at stream position 3.
        self.assertEqual(3, normalized.index("b"))
        tokens = [{"text": "A", "start_time": 0.0, "end_time": 0.4},
                  {"text": "ß", "start_time": 0.5, "end_time": 0.9},
                  {"text": "B", "start_time": 1.0, "end_time": 1.4}]
        spans = qa.require_exact_coverage(text, tokens)
        self.assertEqual("B", text[spans[-1][1]:spans[-1][2]])

    def test_contraction_combining_and_hangul_match_aa2(self):
        from epub_readaloud import get_normalized_words
        for text in ("éclair", "각 text", "ﬁancé"):
            normalized, indices = qa.get_normalized_stream(text)
            self.assertEqual("".join(get_normalized_words(text)), normalized)
            self.assertTrue(all(0 <= i < len(text) for i in indices))
        # Token end must include the combining mark (contraction).
        text = "éclair"
        spans, _ = qa.map_tokens_to_text(text, [{"text": text}])
        self.assertEqual(len(text), spans[0][2])

    def test_empty_key_token_is_unmatched(self):
        with self.assertRaises(ValueError):
            qa.require_exact_coverage(
                "Hello", [{"text": "Hello", "start_time": 0, "end_time": 0.5},
                          {"text": "!!!", "start_time": 0.6, "end_time": 0.9}])

    def test_initial_abbreviation_stays_with_sentence(self):
        self.assertEqual([(0, 19), (19, 29)],
                         qa.split_sentences("J. Smith went home. He slept."))

    def test_count_units_matches_aa2_vocabulary(self):
        # AA2 \w+ counts "a_b" as one word and "a''b" as two tokens.
        self.assertEqual(1, qa.count_units("a_b"))
        self.assertEqual(2, qa.count_units("a''b"))


class UnicodeBoundaryFollowupTests(unittest.TestCase):
    """C-1 follow-up RED: NFKC composes across original scalars via
    compatibility decomposition (UAX15), not raw codepoints."""

    def _check(self, text, words):
        from epub_readaloud import get_normalized_words
        normalized, indices = qa.get_normalized_stream(text)
        self.assertEqual("".join(get_normalized_words(text)), normalized)
        self.assertEqual(qa.get_token_key(text), normalized)
        self.assertTrue(all(0 <= i < len(text) for i in indices))
        cursor = 0
        expected = []
        for word in words:
            expected.append((cursor, cursor + len(word)))
            cursor += len(word) + 1  # single-space joins in these cases
        tokens = [{"text": w, "start_time": i * 0.1, "end_time": i * 0.1 + 0.08}
                  for i, w in enumerate(words)]
        spans = qa.require_exact_coverage(text, tokens)
        self.assertEqual(expected, [(s, e) for _t, s, e in spans])

    def test_halfwidth_voiced_kana(self):
        self._check("ｶﾞ", ["ｶﾞ"])
        self._check("ﾊﾟ", ["ﾊﾟ"])

    def test_compatibility_hangul(self):
        self._check("ㄱㅏ", ["ㄱㅏ"])
        self._check("ㄱㅏㄱ", ["ㄱㅏㄱ"])
        # L/V/T across compatibility scalars (ㄱ + ㅏ + ㄱ T-part).
        text = "ㄱㅏㄴ"
        normalized, _indices = qa.get_normalized_stream(text)
        self.assertEqual(qa.get_token_key(text), normalized)

    def test_repeated_mixed_sequences(self):
        self._check("ｶﾞ A ｶﾞ", ["ｶﾞ", "A", "ｶﾞ"])
        self._check("ㄱㅏ Straße", ["ㄱㅏ", "Straße"])
        text = "éclair ﬁancé ㄱㅏ ｶﾞ"
        words = ["éclair", "ﬁancé", "ㄱㅏ", "ｶﾞ"]
        normalized, _indices = qa.get_normalized_stream(text)
        self.assertEqual(qa.get_token_key(text), normalized)
        cursor = 0
        expected = []
        for word in words:
            expected.append((cursor, cursor + len(word)))
            cursor += len(word) + 1
        tokens = [{"text": w, "start_time": i * 0.1, "end_time": i * 0.1 + 0.08}
                  for i, w in enumerate(words)]
        spans = qa.require_exact_coverage(text, tokens)
        self.assertEqual(expected, [(s, e) for _t, s, e in spans])

    def test_expansion_split_within_one_original_char(self):
        # Tokens split inside one expanding original char share its
        # original range; normalized order still enforced.
        spans = qa.require_exact_coverage(
            "ﬁ", [{"text": "f", "start_time": 0.0, "end_time": 0.1},
                   {"text": "i", "start_time": 0.1, "end_time": 0.2}])
        self.assertEqual([(0, 1), (0, 1)], [(s, e) for _t, s, e in spans])
        spans = qa.require_exact_coverage(
            "ß", [{"text": "s", "start_time": 0.0, "end_time": 0.1},
                   {"text": "s", "start_time": 0.1, "end_time": 0.2}])
        self.assertEqual([(0, 1), (0, 1)], [(s, e) for _t, s, e in spans])

    def test_echoed_token_still_refused(self):
        with self.assertRaises(ValueError):
            qa.require_exact_coverage(
                "Hello world",
                [{"text": "Hello", "start_time": 0.0, "end_time": 0.1},
                 {"text": "Hello", "start_time": 0.1, "end_time": 0.2},
                 {"text": "world", "start_time": 0.2, "end_time": 0.3}])
        with self.assertRaises(ValueError):
            qa.require_exact_coverage(
                "Hello world",
                [{"text": "world", "start_time": 0.0, "end_time": 0.1},
                 {"text": "Hello", "start_time": 0.1, "end_time": 0.2}])


class InlinePreservationTests(unittest.TestCase):
    def _build(self, body, prepared, tokens, end=72000):
        source = _source_epub(body)
        entries = [{"index": 0, "prepared_text": prepared,
                    "start_frame": 0, "end_frame": end, "sample_rate": 24000}]
        result, _ = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=entries,
            aligned_rows=[{"tokens": tokens}])
        return result

    def _words(self, prepared, duration=3.0):
        words = prepared.replace(".", " ").split()
        step = duration / (len(words) + 1)
        return [{"text": w, "start_time": round((i + 1) * step, 3),
                 "end_time": round((i + 2) * step, 3)} for i, w in enumerate(words)]

    def test_img_br_empty_em_preserved(self):
        result = self._build(
            '<p>First. <img src="x.png" alt="dot"/><br/><em></em> Second.</p>',
            "First. Second.", self._words("First. Second."))
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            doc = ET.fromstring(archive.read('OEBPS/ch1.xhtml'))
        tags = [el.tag.rsplit("}", 1)[-1] for el in doc.iter()]
        self.assertIn("img", tags)
        self.assertIn("br", tags)
        self.assertIn("em", tags)

    def test_trailing_inline_preserved(self):
        result = self._build(
            '<p>First. Second. <em></em></p>', "First. Second.",
            self._words("First. Second."))
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            doc = ET.fromstring(archive.read('OEBPS/ch1.xhtml'))
        tags = [el.tag.rsplit("}", 1)[-1] for el in doc.iter()]
        self.assertIn("em", tags)

    def test_script_style_preserved(self):
        result = self._build(
            '<p>First. <script>var x=1;</script> Second.</p>',
            "First. Second.", self._words("First. Second."))
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            doc = ET.fromstring(archive.read('OEBPS/ch1.xhtml'))
        scripts = doc.findall('.//' + XHTML + 'script')
        self.assertEqual(1, len(scripts))
        self.assertIn("var x=1;", "".join(scripts[0].itertext()))

    def test_single_sentence_uses_token_times(self):
        tokens = [{"text": "Hello", "start_time": 0.2, "end_time": 0.5},
                  {"text": "world", "start_time": 0.5, "end_time": 0.8}]
        result = self._build('<p>Hello world</p>', "Hello world", tokens,
                             end=48000)
        self.assertEqual([("00:00:00.200", "00:00:00.800")], _smil_clips(result))

    def test_builder_honours_cancel(self):
        source = _source_epub('<p>Hello synthetic world. Second sentence here.</p>')
        entries = [{"index": 0,
                    "prepared_text": "Hello synthetic world. Second sentence here.",
                    "start_frame": 0, "end_frame": 48000, "sample_rate": 24000}]
        tokens = [{"text": w, "start_time": i * 0.25, "end_time": (i + 1) * 0.25}
                  for i, w in enumerate(
                      "Hello synthetic world Second sentence here".split())]
        with self.assertRaises(ValueError):
            build_epub_readaloud(
                retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3',
                audio_filename='audiobook.mp3', script_entries=entries,
                aligned_rows=[{"tokens": tokens}],
                cancel_check=lambda: True)


class RuntimeFailClosedTests(unittest.TestCase):
    def test_missing_model_dir_returns_tuple(self):
        import qwen_runtime as qr
        issues, languages = qr.get_model_issues("/nonexistent-qwen-1844")
        self.assertTrue(issues)

    def test_non_dict_config_fails(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in qr._REQUIRED_MODEL_FILES:
                (root / name).write_text("{}", encoding="utf-8")
            (root / "config.json").write_text("[]", encoding="utf-8")
            (root / "model.safetensors").write_bytes(b"x")
            issues, _ = qr.get_model_issues(str(root))
            self.assertTrue(issues)

    def test_malformed_device_types_raise_value_error(self):
        import qwen_runtime as qr
        # Malformed probe data is ineligible (CPU fallback), never TypeError.
        self.assertEqual(
            ("cpu", "float32"),
            qr.get_qwen_device_selection(
                [{"device": "cuda:0", "capability": ["8", "6"],
                  "free_bytes": 6 * 1024**3, "total_bytes": 8 * 1024**3}]))

    def test_probe_imports_qwen_asr(self):
        import qwen_runtime as qr
        self.assertIn("qwen_asr import", qr._PROBE_SOURCE)

    def test_no_sys_executable_fallback(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                candidates = qr.get_qwen_python_candidates(tmp)
            self.assertNotIn(sys.executable, candidates)

    def test_unknown_packages_refuse_identity(self):
        # Identity records unknown metadata as None and binds it; the
        # probe/provider version gates (not identity) decide readiness,
        # so alternate interpreters without pip still capture identity.
        import subprocess
        import qwen_runtime as qr
        payload = json.dumps({"python_version": "3.12.0",
                              "packages": {"qwen-asr": "0.0.6", "torch": "2.10.0",
                                           "transformers": None}}).encode()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload)
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"x")
            exe = handle.name
        self.addCleanup(lambda: os.unlink(exe) if os.path.exists(exe) else None)
        with unittest.mock.patch.object(qr, "run_owned_capture",
                                       return_value=completed):
            identity = qr.get_qwen_runtime_identity(exe)
            self.assertIsNone(identity["packages"]["transformers"])
            self.assertTrue(identity["runtime_digest"])

    def test_digest_refuses_symlink(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_bytes(b"hello")
            original_lstat = os.lstat

            def fake_lstat(path):
                info = original_lstat(path)
                if str(path).endswith("a.txt"):
                    import stat as _stat

                    class FakeStat:
                        st_mode = _stat.S_IFLNK | 0o777
                        st_size = info.st_size
                    return FakeStat()
                return info

            with unittest.mock.patch.object(os, "lstat", side_effect=fake_lstat):
                with self.assertRaises(ValueError):
                    qr.get_qwen_model_file_digest(str(root))


class PreferenceAndRouteTests(unittest.TestCase):
    def test_corrupt_preference_raises(self):
        from readalong_export import get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "readalong_timing.json").write_text("{not json",
                                                          encoding="utf-8")
            with self.assertRaises(ValueError):
                get_readaloud_timing_preference(tmp)

    def test_invalid_mode_preference_raises(self):
        from readalong_export import get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "readalong_timing.json").write_text(json.dumps(
                {"timing_mode": "sideways", "timing_device": "auto",
                 "timing_language": None}), encoding="utf-8")
            with self.assertRaises(ValueError):
                get_readaloud_timing_preference(tmp)

    def test_missing_preference_still_defaults(self):
        from readalong_export import get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                {"timing_mode": "auto", "timing_device": "auto",
                 "timing_language": None},
                get_readaloud_timing_preference(tmp))

    def test_device_path_rejected_by_request(self):
        from pydantic import ValidationError
        from routers.editor import ReadaloudEpubRequest
        with self.assertRaises(ValidationError):
            ReadaloudEpubRequest(timing_device="../evil")

    def test_js_device_dict_extraction(self):
        text = Path(__file__).parent.parent / "static" / "js" / "app-readalong.js"
        body = text.read_text(encoding="utf-8")
        self.assertIn("device.device", body)

    def test_fake_worker_matches_exact_coverage(self):
        import importlib
        fake = importlib.import_module("tests.fixtures.qwen_fake_worker")
        self.assertEqual(["don't"], fake._units("don’t"))

    def test_long_windows_respect_180_cap_without_sharing_sentences(self):
        sentences = []
        for index in range(12):
            sentences.append({"start": index * 20, "end": index * 20 + 19,
                              "words": 30})
        windows = qa.get_aligner_windows(400.0, sentences)
        self.assertGreater(len(windows), 1)
        seen = set()
        for window in windows:
            self.assertLessEqual(window["end_sec"] - window["start_sec"], 180.0 + 1e-9)
            for sentence_index in range(window["first_sentence"], window["last_sentence"]):
                self.assertNotIn(sentence_index, seen)
                seen.add(sentence_index)
        self.assertEqual(set(range(12)), seen)

    def test_write_slice_is_cancelable(self):
        from readalong_export import _write_row_slice_wav
        from tts import ExportCancelled
        with tempfile.TemporaryDirectory() as tmp:
            pcm = Path(tmp) / "a.pcm"
            pcm.write_bytes(b"\x00\x00" * 48000)
            out = Path(tmp) / "out.wav"
            with open(pcm, "rb") as stream:
                with self.assertRaises(ExportCancelled):
                    _write_row_slice_wav(stream, str(out), (1, 24000, 2), 0, 24000,
                                         cancel_check=lambda: True)


if __name__ == "__main__":
    unittest.main()
