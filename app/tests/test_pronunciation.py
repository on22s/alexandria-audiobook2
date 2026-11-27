"""The pronunciation lexicon must change audio, never the record of it.

46% of the active book's lines carry a Latinized Japanese name, so a lexicon
bug reaches nearly half the audiobook. The dangerous failures are not crashes:
they are a respelling that fires where it should not, or one that fires and
leaves no trace, so the listener hears one thing and the script says another.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pronunciation
from pronunciation import apply_pronunciation, load_lexicon


class LexiconTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pronunciation._cache.update({"path": None, "fingerprint": None,
                                     "entries": {}, "pattern": None})

    def tearDown(self):
        self.tmp.cleanup()
        pronunciation._cache.update({"path": None, "fingerprint": None,
                                     "entries": {}, "pattern": None})

    def write(self, names):
        p = os.path.join(self.tmp.name, "pron.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"names": names}, fh)
        return p

    # ── the substitution itself ──────────────────────────────────────────

    def test_a_name_is_respelled(self):
        p = self.write({"Subaru": "Soo-bah-roo"})
        out, applied = apply_pronunciation("or so Subaru thought", p)
        self.assertEqual(out, "or so Soo-bah-roo thought")
        self.assertEqual(applied, [{"name": "Subaru", "spoken": "Soo-bah-roo"}])

    def test_every_substitution_is_reported(self):
        """A silent respelling is untraceable: the audio and the script would
        disagree with nothing recording why."""
        p = self.write({"Rom": "Rohm", "Felt": "Felt"})
        _, applied = apply_pronunciation("Rom and Felt and Rom", p)
        self.assertEqual([a["name"] for a in applied], ["Rom", "Felt", "Rom"])

    def test_longest_key_wins(self):
        """With both 'Natsuki' and 'Natsuki Subaru' present, matching the
        shorter key inside the longer name leaves a half-substituted phrase."""
        p = self.write({"Natsuki": "Nat-ski",
                        "Natsuki Subaru": "Nat-ski Soo-bah-roo"})
        out, _ = apply_pronunciation("Natsuki Subaru arrived", p)
        self.assertEqual(out, "Nat-ski Soo-bah-roo arrived")

    # ── where it must NOT fire ───────────────────────────────────────────

    def test_it_does_not_fire_inside_a_longer_word(self):
        """'Rom' must not rewrite 'Romance' or 'from'."""
        p = self.write({"Rom": "Rohm"})
        out, applied = apply_pronunciation("a Romance from Rome", p)
        self.assertEqual(out, "a Romance from Rome")
        self.assertEqual(applied, [])
        p = self.write({"Dr": "Doctor"})
        self.assertEqual("Drake saw Doctor Who", apply_pronunciation("Drake saw Dr Who", p)[0])

    def test_a_unspaced_script_key_matches_inside_a_run(self):
        """In Han/kana/Thai text the next letter cannot be a boundary: it is
        almost always the next word. Longest-first keeps a shorter key from
        splitting a longer one."""
        p = self.write({"太郎": "Taro"})
        for text in ("太郎は来た", "太郎が来た", "太郎。来た"):
            with self.subTest(text=text):
                self.assertEqual(text.replace("太郎", "Taro"),
                                 apply_pronunciation(text, p)[0])
        p = self.write({"王明": "Wang Ming"})
        self.assertEqual("Wang Ming走了", apply_pronunciation("王明走了", p)[0])
        p = self.write({"王": "W", "王明": "Wang Ming"})
        self.assertEqual("Wang Ming走了", apply_pronunciation("王明走了", p)[0])
        p = self.write({"太郎": "Taro", "郎": "Rou"})
        self.assertEqual("Taroは", apply_pronunciation("太郎は", p)[0])
        p = self.write({"ไทย": "Thai"})
        self.assertEqual("ประเทศThai", apply_pronunciation("ประเทศไทย", p)[0])

    def test_a_combining_mark_after_a_key_blocks_it(self):
        """A vowel sign belongs to the word: राम must not fire in रामा."""
        for key, blocked, allowed in (("राम", "रामा ने", "राम ने"),
                                      ("রাম", "রামা", "রাম "),
                                      ("ராம", "ராமா", "ராம ")):
            with self.subTest(key=key):
                p = self.write({key: "Raam"})
                self.assertEqual(blocked, apply_pronunciation(blocked, p)[0])
                self.assertEqual(allowed.replace(key, "Raam"),
                                 apply_pronunciation(allowed, p)[0])
        p = self.write({"राम": "Raam"})
        self.assertEqual("Raam। ने", apply_pronunciation("राम। ने", p)[0])

    def test_a_single_character_unspaced_key_needs_same_script_neighbours(self):
        """One character is not evidence of a whole word: a single-character
        key must not fire inside a run of its own script. Longer keys still
        match inside a run (the next test); the issue's 太郎/王明 rows survive."""
        p = self.write({"中": "Naka", "王": "Oh"})
        self.assertEqual(("中国の王国", []), apply_pronunciation("中国の王国", p))
        self.assertEqual("Ohは来た", apply_pronunciation("王は来た", p)[0])
        self.assertEqual("Naka。", apply_pronunciation("中。", p)[0])
        self.assertEqual("Naka 国", apply_pronunciation("中 国", p)[0])
        p = self.write({"ロ": "Ro", "ヒ": "Hi"})
        self.assertEqual(("ローマとヒーロー", []),
                         apply_pronunciation("ローマとヒーロー", p))
        p = self.write({"ท": "T"})
        self.assertEqual(("ประเทศไทย", []), apply_pronunciation("ประเทศไทย", p))
        self.assertEqual("T", apply_pronunciation("ท", p)[0])

    def test_a_multi_character_unspaced_key_matches_inside_a_run(self):
        """The documented remaining false-match class: a longer, unrelated
        word is not protected unless it is itself an entry. An identity entry
        wins longest-first and absorbs the match."""
        p = self.write({"タイ": "Tai"})
        self.assertEqual("Taiトル", apply_pronunciation("タイトル", p)[0])
        p = self.write({"タイ": "Tai", "タイトル": "タイトル"})
        self.assertEqual(("タイトル", [{"name": "タイトル", "spoken": "タイトル"}]),
                         apply_pronunciation("タイトル", p))

    def test_iteration_and_ideographic_marks_count_as_unspaced(self):
        """々/〆/〻/〼/𖿣 are Script=Han and 〇 is Han although its category is
        Nl, so a key containing them must not keep word bounds. The vertical
        kana repeat marks join both kana classes."""
        for key in ("〆", "〻", "〼", "\U00016fe3", "〇", "〱"):
            with self.subTest(key=key):
                self.assertTrue(pronunciation.is_unspaced_key(key))
        for key, text in (("時々", "時々は"), ("〆香", "〆香は"),
                          ("〼香", "〼香は"), ("\U00016fe3香", "\U00016fe3香は"),
                          ("〇", "〇は"), ("〇〇", "〇〇は")):
            with self.subTest(key=key):
                p = self.write({key: "Spoken"})
                self.assertEqual("Spokenは", apply_pronunciation(text, p)[0])
        p = self.write({"中": "Naka"})
        self.assertEqual("中々", apply_pronunciation("中々", p)[0])

    def test_han_marks_block_as_neighbours_of_a_single_character_key(self):
        """The Han marks classify the same way as a neighbour as they do in a
        key: 〇 (Nl), 〼 and 𖿣 each block a one-character Han key."""
        p = self.write({"中": "Naka"})
        for mark in ("〇", "〼", "\U00016fe3"):
            with self.subTest(mark=mark):
                self.assertEqual(("中" + mark, []),
                                 apply_pronunciation("中" + mark, p))

    def test_kana_run_marks_block_but_radicals_do_not(self):
        """Vertical repeat marks and spacing voiced marks continue a kana run,
        so a one-character kana key must not fire beside them. Kangxi/CJK
        radicals are symbols, not Han, so they do not block (documented in
        Limits)."""
        p = self.write({"カ": "ka", "き": "ki"})
        for mark in tuple(chr(codepoint) for codepoint in range(0x3031, 0x3036)) + (
                "\u309b", "\u309c"):
            with self.subTest(mark=mark):
                self.assertEqual(("カ" + mark, []),
                                 apply_pronunciation("カ" + mark, p))
        self.assertEqual(("き〱", []), apply_pronunciation("き〱", p))
        p = self.write({"中": "Naka"})
        self.assertEqual("Naka⼀", apply_pronunciation("中⼀", p)[0])
        self.assertEqual("Naka⺀", apply_pronunciation("中⺀", p)[0])

    def test_script_native_digits_block_while_ascii_and_fullwidth_are_neutral(self):
        """Digits are neutral when classifying a KEY (so 3月 and ３月 are
        unspaced), but a script-native digit continues its own run and blocks a
        one-character key beside it; ASCII and fullwidth digits do not."""
        self.assertTrue(pronunciation.is_unspaced_key("3月"))
        self.assertTrue(pronunciation.is_unspaced_key("３月"))
        for key, digit in (("ก", "\u0e51"), ("ກ", "\u0ed1"), ("က", "\u1041")):
            with self.subTest(key=key):
                p = self.write({key: "X"})
                self.assertEqual((key + digit, []),
                                 apply_pronunciation(key + digit, p))
                self.assertEqual("X1", apply_pronunciation(key + "1", p)[0])
                self.assertEqual("X１", apply_pronunciation(key + "１", p)[0])

    def test_digits_are_neutral_when_classifying_an_unspaced_key(self):
        """Letters decide: 3月 is unspaced because 月 is Han, while 3rd is not
        and a digit-only key is not. The neutral digit is deliberate, so 3月
        also fires between Latin characters (the documented cost)."""
        self.assertTrue(pronunciation.is_unspaced_key("3月"))
        self.assertFalse(pronunciation.is_unspaced_key("3rd"))
        self.assertFalse(pronunciation.is_unspaced_key("3"))
        p = self.write({"3月": "Sangatsu"})
        self.assertEqual("Sangatsuは", apply_pronunciation("3月は", p)[0])
        self.assertEqual("Sangatsu", apply_pronunciation("3月", p)[0])

    def test_a_variation_selector_after_a_key_does_not_block(self):
        """An ideographic/emoji variation selector modifies the preceding
        character, so 葛+IVS is still 葛 and must not be refused as a mark."""
        p = self.write({"葛": "Kuzu"})
        self.assertEqual("Kuzu\U000E0100は",
                         apply_pronunciation("葛\U000E0100は", p)[0])
        p = self.write({"絵": "E"})
        self.assertEqual("E\uFE0F", apply_pronunciation("絵\uFE0F", p)[0])

    def test_a_leading_left_quote_blocks_like_an_apostrophe(self):
        """Some texts use U+2018 as an apostrophe: ‘tis must stay unchanged,
        exactly as ’tis and 'tis do for a key for tis."""
        p = self.write({"tis": "tiz"})
        self.assertEqual(("‘tis", []), apply_pronunciation("‘tis", p))
        self.assertEqual("tiz", apply_pronunciation("tis", p)[0])

    def test_halfwidth_katakana_and_the_prolonged_sound_mark_inside_a_run(self):
        p = self.write({"ﾀﾝ": "Tan"})
        self.assertEqual("Tanが来た", apply_pronunciation("ﾀﾝが来た", p)[0])
        p = self.write({"コー": "Koh"})
        self.assertEqual("Kohヒー", apply_pronunciation("コーヒー", p)[0])

    def test_latin_combining_accents_block_in_nfc_and_nfd(self):
        """A precomposed é is a word letter and é as e+U+0301 is a combining
        mark: neither may let a key for Cafe fire inside Café."""
        p = self.write({"Cafe": "Kafay"})
        self.assertEqual("Café", apply_pronunciation("Café", p)[0])
        self.assertEqual("Cafe\u0301", apply_pronunciation("Cafe\u0301", p)[0])
        p = self.write({"Café": "Kafay"})
        self.assertEqual("Kafay", apply_pronunciation("Café", p)[0])

    def test_korean_keeps_word_bounds(self):
        """Hangul is written with spaces between words, so a single-character
        key must not fire inside a Hangul word."""
        p = self.write({"김": "Kim"})
        self.assertEqual("김밥", apply_pronunciation("김밥", p)[0])
        self.assertEqual("Kim 밥", apply_pronunciation("김 밥", p)[0])

    def test_a_mixed_latin_and_han_key_keeps_word_bounds(self):
        """One spaced-script letter is enough: A太郎 is not unspaced."""
        p = self.write({"A太郎": "Ataro"})
        self.assertEqual("A太郎は", apply_pronunciation("A太郎は", p)[0])
        self.assertEqual("Ataro", apply_pronunciation("A太郎", p)[0])

    def test_a_changed_match_changes_render_identity_only_on_affected_lines(self):
        """This pins the PRONUNCIATION COMPONENT of render identity, not a take.

        A take publication stores get_recovery_digest(preparation) as
        pronunciation_sha256, and this test proves that digest changes for the
        straight-apostrophe possessive line and not for the already-matching
        curly control. It stores no take. The serving fingerprint also hashes
        the pronunciation module code, so every older take becomes audition-only
        after this change regardless of whether its line's preparation changed;
        that is the same behaviour as any pronunciation.py update."""
        from speech_text import get_speech_normalization
        from synthesis_recovery_identity import get_recovery_digest
        p = self.write({"Mara": "Mahra"})
        legacy = re.compile(r"(?<![\w'])(Mara)(?![\w'])")
        original_path = pronunciation.DEFAULT_PATH
        original_resolver = pronunciation.get_pronunciation_path
        pronunciation.DEFAULT_PATH = p
        pronunciation.get_pronunciation_path = lambda path=None: p
        reset = dict(path=None, fingerprint=None, entries={}, pattern=None)
        try:
            for text, affected in (("Mara's hat", True), ("Mara\u2019s hat", False)):
                with self.subTest(text=text):
                    pronunciation._cache.update(reset)
                    after = get_recovery_digest(get_speech_normalization(text))
                    pronunciation._cache.update(reset)
                    with patch.object(pronunciation, "_compile", return_value=legacy):
                        before = get_recovery_digest(get_speech_normalization(text))
                    if affected:
                        self.assertNotEqual(before, after)
                    else:
                        self.assertEqual(before, after)
        finally:
            pronunciation.DEFAULT_PATH = original_path
            pronunciation.get_pronunciation_path = original_resolver
            pronunciation._cache.update(reset)

    def test_it_fires_on_a_possessive_but_not_across_a_leading_apostrophe(self):
        """`Subaru's` is the same name: the trailing possessive is kept rather
        than blocking the substitution, and both apostrophes behave the same.
        A LEADING apostrophe still blocks, so a name inside O'Brien is never
        split by an entry for Brien."""
        p = self.write({"Subaru": "Soo-bah-roo", "Brien": "Bry-en"})
        for apostrophe in ("'", "\u2019", "\u02bc"):
            with self.subTest(apostrophe=apostrophe):
                out, applied = apply_pronunciation("Subaru" + apostrophe + "s hand", p)
                self.assertEqual("Soo-bah-roo" + apostrophe + "s hand", out)
                self.assertEqual([{"name": "Subaru", "spoken": "Soo-bah-roo"}], applied)
                leading = "O" + apostrophe + "Brien"
                self.assertEqual((leading, []), apply_pronunciation(leading, p))

    def test_a_plural_s_still_blocks_a_shorter_key_but_not_its_own_key(self):
        p = self.write({"Mara": "Mahra"})
        self.assertEqual(("Maras' hat", []), apply_pronunciation("Maras' hat", p))
        p = self.write({"Maras": "Mahras"})
        self.assertEqual("Mahras' hat", apply_pronunciation("Maras' hat", p)[0])

    def test_an_empty_respelling_is_ignored(self):
        """The shipped file lists every name with an empty value, so the
        mechanism is present and inert until an entry is MEASURED to help.
        An empty value must not delete the name from the text."""
        p = self.write({"Subaru": "", "Felt": "   "})
        out, applied = apply_pronunciation("Subaru met Felt", p)
        self.assertEqual(out, "Subaru met Felt")
        self.assertEqual(applied, [])

    # ── it must never stop a book generating ─────────────────────────────

    def test_a_missing_lexicon_is_not_an_error(self):
        out, applied = apply_pronunciation(
            "text", os.path.join(self.tmp.name, "absent.json"))
        self.assertEqual(out, "text")
        self.assertEqual(applied, [])

    def test_a_malformed_lexicon_degrades_to_no_substitution(self):
        p = os.path.join(self.tmp.name, "bad.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        out, applied = apply_pronunciation("Subaru", p)
        self.assertEqual(out, "Subaru")
        self.assertEqual(applied, [])

    def test_non_string_entries_are_skipped(self):
        p = os.path.join(self.tmp.name, "odd.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"names": {"Subaru": 5, "Felt": None, "Rom": "Rohm"}}, fh)
        out, _ = apply_pronunciation("Subaru Felt Rom", p)
        self.assertEqual(out, "Subaru Felt Rohm")

    def test_an_edit_takes_effect(self):
        """Cached on mtime, so correcting a name does not need a restart."""
        p = self.write({"Subaru": "First"})
        self.assertEqual(apply_pronunciation("Subaru", p)[0], "First")
        import time
        time.sleep(0.01)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"names": {"Subaru": "Second"}}, fh)
        os.utime(p, (os.path.getatime(p), os.path.getmtime(p) + 10))
        self.assertEqual(apply_pronunciation("Subaru", p)[0], "Second")


class ShippedLexiconTest(unittest.TestCase):
    """The file that ships with the repo."""

    def test_it_ships_inert(self):
        """Entries must be measured, not guessed. If this ever fails it means
        somebody added a respelling - which is fine, but it should be because
        proper_noun_pronunciation.py showed it helped."""
        pronunciation._cache.update({"path": None, "fingerprint": None,
                                     "entries": {}, "pattern": None})
        entries = load_lexicon()
        for name, spoken in entries.items():
            self.assertTrue(spoken.strip(),
                            f"{name} has a respelling; was it measured?")

    def test_the_shipped_file_parses(self):
        path = pronunciation.DEFAULT_PATH
        if not os.path.exists(path):
            self.skipTest("no pronunciation.json in this checkout")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertIn("names", doc)
        self.assertIsInstance(doc["names"], dict)


class RosterDerivationTest(unittest.TestCase):
    """The name list comes from the roster, and aliases inform it WITHOUT
    leaking one character's pronunciation onto another's spelling."""

    def test_a_nickname_does_not_inherit_the_canonical_pronunciation(self):
        """THE TRAP. character_aliases maps 'BETTY' -> 'BEATRICE' because they
        are one character. They are NOT one sound. Substituting across the
        alias would put a word in the audio that is not in the book, which is
        worse than mispronouncing it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = os.path.join(tmp.name, "pron.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"names": {"Beatrice": "Bee-ah-tree-chay"}}, fh)
        out, applied = apply_pronunciation("Betty smiled at Beatrice", p)
        self.assertEqual(out, "Betty smiled at Bee-ah-tree-chay")
        self.assertEqual([a["name"] for a in applied], ["Beatrice"])

    def test_a_respelled_name_does_not_touch_its_lowercase_word(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = os.path.join(tmp.name, "pron.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"names": {"Felt": "Fehlt"}}, fh)
        out, _ = apply_pronunciation("Felt felt the cold", p)
        self.assertEqual(out, "Fehlt felt the cold")


class NormalizationIntegrationTest(unittest.TestCase):
    """The lexicon has to reach the text the model actually receives."""

    def test_normalization_reports_the_substitution(self):
        from speech_text import get_speech_normalization
        import pronunciation as pr
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = os.path.join(tmp.name, "pron.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"names": {"Subaru": "Soo-bah-roo"}}, fh)
        original, pr.DEFAULT_PATH = pr.DEFAULT_PATH, p
        original_resolver, pr.get_pronunciation_path = pr.get_pronunciation_path, lambda path=None: p
        pr._cache.update({"path": None, "fingerprint": None, "entries": {},
                          "pattern": None})
        try:
            out = get_speech_normalization("Subaru walked on.")
            self.assertIn("Soo-bah-roo", out["text"])
            kinds = [t["type"] for t in out["transformations"]]
            self.assertIn("pronunciation_lexicon", kinds)
        finally:
            pr.DEFAULT_PATH = original
            pr.get_pronunciation_path = original_resolver
            pr._cache.update({"path": None, "fingerprint": None, "entries": {},
                              "pattern": None})




class LexiconFileFingerprintTests(unittest.TestCase):
    def test_content_change_invalidates_cache_when_all_stat_fields_are_unchanged(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "pronunciation.json")
            path.write_text(json.dumps({"names": {"Aeliana": "First"}}), encoding="utf-8")
            frozen = path.stat()
            with patch.object(pronunciation.os, "stat", return_value=frozen), \
                    patch.object(pronunciation.os, "fstat", return_value=frozen):
                self.assertEqual("First", apply_pronunciation("Aeliana", str(path))[0])
                path.write_text(json.dumps({"names": {"Aeliana": "Other"}}), encoding="utf-8")
                self.assertEqual("Other", apply_pronunciation("Aeliana", str(path))[0])
                with patch.object(pronunciation.json, "load", side_effect=AssertionError("unchanged content reparsed")):
                    self.assertEqual("Other", apply_pronunciation("Aeliana", str(path))[0])

    def test_equal_mtime_and_size_edit_refreshes_actual_respelling_and_evidence(self):
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "pronunciation.json")
            path.write_text(json.dumps({"names": {"Subaru": "First"}}))
            before_stat = path.stat()
            self.assertEqual(("First arrived", [{"name": "Subaru", "spoken": "First"}]),
                             apply_pronunciation("Subaru arrived", str(path)))
            path.write_text(json.dumps({"names": {"Subaru": "Other"}}))
            os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            self.assertEqual(before_stat.st_size, path.stat().st_size)
            self.assertEqual(before_stat.st_mtime_ns, path.stat().st_mtime_ns)
            self.assertEqual(("Other arrived", [{"name": "Subaru", "spoken": "Other"}]),
                             apply_pronunciation("Subaru arrived", str(path)))
            data = path.read_bytes()
            with patch.object(pronunciation.json, "load", side_effect=AssertionError("cache miss")):
                loaded = load_lexicon(str(path))
                loaded["Subaru"] = "external mutation"
                self.assertEqual("Other", load_lexicon(str(path))["Subaru"])
            self.assertEqual(data, path.read_bytes())

    def test_same_timestamp_atomic_replacement_and_malformed_recovery_are_observed(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "pronunciation.json")
            path.write_text(json.dumps({"names": {"Subaru": "First"}}))
            before_stat = path.stat()
            self.assertEqual("First", apply_pronunciation("Subaru", str(path))[0])
            replacement = Path(tmp, "replacement.json")
            replacement.write_text(json.dumps({"names": {"Subaru": "Third"}}))
            os.utime(replacement, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            os.replace(replacement, path)
            self.assertEqual(before_stat.st_size, path.stat().st_size)
            self.assertNotEqual(before_stat.st_ino, path.stat().st_ino)
            self.assertEqual("Third", apply_pronunciation("Subaru", str(path))[0])
            path.write_bytes(b"{bad")
            malformed = path.stat()
            self.assertEqual(("Subaru", []), apply_pronunciation("Subaru", str(path)))
            path.write_text(json.dumps({"names": {"Subaru": "Valid"}}))
            os.utime(path, ns=(malformed.st_atime_ns, malformed.st_mtime_ns))
            self.assertEqual("Valid", apply_pronunciation("Subaru", str(path))[0])
            path.unlink()
            self.assertEqual(("Subaru", []), apply_pronunciation("Subaru", str(path)))


class LexiconFailureDiagnosticsTests(unittest.TestCase):
    def test_actual_invalid_files_warn_and_cached_fallback_stays_nonblocking(self):
        import pronunciation
        from speech_text import get_speech_normalization
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'lexicon.json')
            for raw in (b'{broken json', b'\xff\xfe', b'[]'):
                with self.subTest(raw=raw):
                    with open(path, 'wb') as stream:
                        stream.write(raw)
                    with patch.object(pronunciation, 'DEFAULT_PATH', path), \
                            patch.object(pronunciation, 'get_pronunciation_path', return_value=path):
                        with self.assertLogs('pronunciation', level='WARNING') as captured:
                            result = get_speech_normalization('Aeliana walked.')
                        self.assertTrue(any('lexicon' in line.lower() for line in captured.output))
                        self.assertEqual('Aeliana walked.', result['text'])
                        self.assertFalse(result['changed'])
                        with self.assertNoLogs('pronunciation', level='WARNING'):
                            self.assertEqual(result, get_speech_normalization('Aeliana walked.'))
            with patch.object(pronunciation, 'DEFAULT_PATH', path), \
                    patch.object(pronunciation, 'get_pronunciation_path', return_value=path):
                with open(path, 'w', encoding='utf-8') as stream:
                    json.dump({'Aeliana': 'Ay lee ah nah'}, stream)
                with self.assertNoLogs('pronunciation', level='WARNING'):
                    self.assertEqual('Ay lee ah nah walked.', get_speech_normalization('Aeliana walked.')['text'])
                with open(path, 'w', encoding='utf-8') as stream:
                    stream.write('{}')
                with self.assertNoLogs('pronunciation', level='WARNING'):
                    self.assertEqual('Aeliana walked.', get_speech_normalization('Aeliana walked.')['text'])
                os.remove(path)
                with self.assertNoLogs('pronunciation', level='WARNING'):
                    self.assertEqual('Aeliana walked.', get_speech_normalization('Aeliana walked.')['text'])

    def test_unexpected_pronunciation_processing_failure_warns_without_stopping_generation(self):
        import pronunciation
        from speech_text import get_speech_normalization
        with patch.object(pronunciation, 'apply_pronunciation', side_effect=RuntimeError('synthetic processing failure')):
            with self.assertLogs('speech_text', level='WARNING') as captured:
                result = get_speech_normalization('Synthetic sentence.')
        self.assertEqual('Synthetic sentence.', result['text'])
        self.assertTrue(any('synthetic processing failure' in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
