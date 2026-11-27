import json
import unittest

from pass_quality import (NAME_ATTESTATION_ATTESTED,
                          NAME_ATTESTATION_UNATTESTED,
                          NAME_ATTESTATION_UNVERIFIABLE,
                          UNCASED_MIN_NAME_ATTESTATIONS,
                          get_name_attestation_verdict,
                          is_attested_name, validate_attribution)


class RejectUnattestedSpeakerTest(unittest.TestCase):
    """The roster gate filters what goes IN; nothing filtered what came OUT.
    The model invented FUTURE_ME - the protagonist's future self, which the
    book never names - and shipped it on 250 entries, 13% of one book. WEARING,
    MAILMAN, ARUMANFI and SWORD_GOD_GARU_FARION came through the same hole:
    279 entries, 14.6% of the book, attributed to names not in the text."""

    # Long enough to clear MIN_SOURCE_FOR_ATTESTATION: the gate deliberately
    # stands down on fragments, where a real name may appear only once.
    SOURCE = (("Roxy smiled at Rudi. Roxy and Eris spoke, and Eris laughed. "
               "Rudi looked at Roxy again. ") * 90
              + "I remembered what my future me had written. " * 4)

    def _report(self, speaker):
        frozen = [{"type": "SPOKEN", "text": "Hello there."}]
        return validate_attribution(
            frozen, [{"n": 0, "head": "Hello there.", "speaker": speaker}],
            source_text=self.SOURCE)

    def test_invented_speaker_is_rejected(self):
        report = self._report("FUTURE_ME")
        self.assertFalse(report["passed"])
        self.assertTrue(any(f.get("code") == "speaker_not_in_source"
                            for f in report["findings"]))

    def test_real_character_passes(self):
        self.assertTrue(self._report("ROXY")["passed"], self._report("ROXY"))

    def test_common_word_is_rejected(self):
        # "wearing"-class inventions: a word from the prose, not a name.
        self.assertFalse(self._report("REMEMBERED")["passed"])

    def test_unknown_placeholder_still_allowed(self):
        self.assertTrue(self._report("UNKNOWN")["passed"])

    def test_without_source_nothing_is_rejected(self):
        # Callers that pass no source keep today's behaviour exactly.
        frozen = [{"type": "SPOKEN", "text": "Hello there."}]
        report = validate_attribution(
            frozen, [{"n": 0, "head": "Hello there.", "speaker": "FUTURE_ME"}])
        self.assertTrue(report["passed"])




class HonorificSuffixTest(unittest.TestCase):
    """Translated works name characters "Bri-chan", "Zodiac-kun", "Ako-san".

    str.title() capitalises after every non-letter, so "BRI-CHAN".title() is
    "Bri-Chan" and never matched the book's "Bri-chan". That rejected three real
    grimgar03 characters as inventions - Bri-chan alone is named 55 times - and
    would reject every honorific-suffixed name in any translated book.
    """

    def _book(self, *sentences):
        # Long enough to clear MIN_SOURCE_FOR_ATTESTATION.
        return (" ".join(sentences) + " ") + ("filler word here. " * 400)

    def test_punctuation_ending_names_pass_both_shared_attestation_callers(self):
        from three_pass_generate import build_roster
        for name in ("J.R.R.", "Prince?", "Bri-chan"):
            with self.subTest(name=name):
                source = self._book(*(f"{name} spoke at the door." for _ in range(3)))
                label = name.upper()
                self.assertTrue(is_attested_name(label, source))
                frozen = [{"type": "SPOKEN", "text": "Hello."}]
                report = validate_attribution(
                    frozen, [{"n": 0, "speaker": label}], source_text=source)
                self.assertTrue(report["passed"], report["findings"])
                self.assertEqual([label], build_roster([{"speaker": label}], source))

    def test_punctuation_names_require_literal_whole_labels(self):
        for label, text in (("J.R.R.", "JxRxRz spoke."),
                            ("J.R.R.", "J.R.R.X spoke."),
                            ("J.R.R.", "AJ.R.R. spoke."),
                            ("Prince?", "Prince?ling spoke.")):
            with self.subTest(label=label, text=text):
                self.assertFalse(is_attested_name(label, self._book(text, text, text)))

    def test_a_hyphenated_honorific_name_is_attested(self):
        source = self._book("Bri-chan drew his sword.", "Bri-chan laughed again.")
        self.assertTrue(is_attested_name("BRI-CHAN", source))

    def test_a_misspelling_of_a_real_name_is_still_rejected(self):
        # BARABARA-SENSEI (doubled 'ba') is not BARBARA-SENSEI, and shipped 18
        # times on grimgar03.
        source = self._book("Barbara-sensei drew her sword.",
                            "Barbara-sensei laughed again.")
        self.assertTrue(is_attested_name("BARBARA-SENSEI", source))
        self.assertFalse(is_attested_name("BARABARA-SENSEI", source))

    def test_a_lowercase_phrase_is_not_a_name_however_it_is_hyphenated(self):
        source = self._book("He thought of his future-self often.",
                            "The future-self said nothing.",
                            "Again the future-self was silent.")
        self.assertFalse(is_attested_name("FUTURE-SELF", source))


class _StubAttributionClient:
    def __init__(self, speaker):
        self.speaker = speaker
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls += 1
        content = json.dumps([{"n": 0, "speaker": self.speaker}])
        msg = type("Msg", (), {"content": content})()
        choice = type("Choice", (), {"message": msg, "finish_reason": "stop"})()
        return type("Resp", (), {"choices": [choice], "usage": None})()


class UncasedScriptAttestationTest(unittest.TestCase):
    """Issue #2330: names in uncased scripts cannot be judged by capitalisation."""

    LONG_FILLER = "The road went on through the quiet valley. " * 150

    def test_issue_2330_uncased_and_cased_attestation_table(self):
        from three_pass_generate import attested_new_speakers, build_roster

        cases = (
            ("ja", "太郎", "太郎は静かに歩いた。「おはよう」と太郎が言った。太郎は笑った。"),
            ("zh", "王明", "王明慢慢地走进屋里。“你好，”王明说道。王明笑了笑。"),
            ("ko", "민수", "민수가 조용히 걸어왔다. “안녕,” 민수가 말했다. 민수는 웃었다."),
            ("ar", "أحمد", "مشى أحمد بهدوء. قال أحمد: «مرحباً». ابتسم أحمد."),
            ("he", "דוד", "דוד הלך בשקט. «שלום,» אמר דוד. דוד חייך."),
            ("hi", "राम", "राम धीरे चला। «नमस्ते,» राम ने कहा। राम मुस्कुराया।"),
            ("ru", "Анна", "Анна тихо шла. «Привет,» сказала Анна. Анна улыбнулась."),
            ("en", "ANNA", 'Anna walked quietly. "Hello," Anna said. Anna smiled.'),
        )
        frozen = [{"type": "SPOKEN", "text": "Hello"}]
        for lang, name, sentence in cases:
            for kind, source in (("short", sentence), ("x400", (sentence + " ") * 400)):
                with self.subTest(lang=lang, name=name, kind=kind):
                    self.assertEqual(NAME_ATTESTATION_ATTESTED,
                                     get_name_attestation_verdict(name, source))
                    self.assertTrue(is_attested_name(name, source))
                    report = validate_attribution(
                        frozen, [{"n": 0, "speaker": name}], source_text=source)
                    self.assertTrue(report["passed"], report)
                    expected = [name.upper()]
                    self.assertEqual(expected, build_roster([{"speaker": name}], source))
                    self.assertEqual(
                        expected, attested_new_speakers([{"speaker": name}], set(), source))

    def test_issue_2330_attribute_batch_stub_rows(self):
        from generate_script import LLMGenParams
        from three_pass_generate import attribute_batch, build_roster

        ja_source = "太郎は静かに歩いた。「おはよう」と太郎が言った。太郎は笑った。" * 200
        en_source = 'Anna walked quietly. "Hello," Anna said. Anna smiled. ' * 200
        ja_frozen = [{"type": "SPOKEN", "text": "おはよう"}]
        en_frozen = [{"type": "SPOKEN", "text": "Hello"}]
        params = LLMGenParams()

        rows = (
            ("ja_fail", "太郎", ja_source, ja_frozen, "fail", None),
            ("ja_keep", "太郎", ja_source, ja_frozen, "keep", None),
            ("ja_fallback", "太郎", ja_source, ja_frozen, "fallback", None),
            ("ja_cast", "太郎", ja_source, ja_frozen, "fail", {"known_names": {"太郎"}}),
            ("en_control", "ANNA", en_source, en_frozen, "fail", None),
        )
        for label, speaker, source, frozen, on_exhaustion, cast in rows:
            with self.subTest(label=label):
                client = _StubAttributionClient(speaker)
                out = attribute_batch(
                    client, "stub-model", frozen, params, roster=[],
                    source_text=source, on_exhaustion=on_exhaustion, cast=cast,
                    max_retries=1)
                self.assertEqual(1, client.calls)
                self.assertEqual(speaker, out[0]["speaker"])
                self.assertIsNone(out[0].get("attribution_unchecked"))
                self.assertEqual([speaker], build_roster(out, source))

    def test_absent_uncased_name_rejected_at_both_gates(self):
        from three_pass_generate import attested_new_speakers, build_roster

        cases = (
            ("ja", "花子", "太郎は静かに歩いた。「おはよう」と太郎が言った。太郎は笑った。" * 50),
            ("zh", "李华", "王明慢慢地走进屋里。“你好，”王明说道。王明笑了笑。" * 50),
            ("ko", "영희", "민수가 조용히 걸어왔다. 민수는 웃었다. 민수가 말했다." * 50),
            ("ar", "سارة", "مشى أحمد بهدوء. قال أحمد مرحباً. ابتسم أحمد." * 50),
            ("he", "שרה", "דוד הלך בשקט. אמר דוד שלום. דוד חייך." * 50),
            ("hi", "सीता", "राम धीरे चला। राम ने कहा। राम मुस्कुराया।" * 50),
        )
        frozen = [{"type": "SPOKEN", "text": "Hello"}]
        for lang, absent, source in cases:
            with self.subTest(lang=lang, absent=absent):
                self.assertEqual(NAME_ATTESTATION_UNATTESTED,
                                 get_name_attestation_verdict(absent, source))
                self.assertFalse(is_attested_name(absent, source))
                report = validate_attribution(
                    frozen, [{"n": 0, "speaker": absent}], source_text=source)
                self.assertFalse(report["passed"])
                self.assertEqual("speaker_not_in_source", report["findings"][0]["code"])
                self.assertNotIn("capitalised", report["findings"][0]["message"])
                self.assertEqual([], build_roster([{"speaker": absent}], source))
                self.assertEqual(
                    [], attested_new_speakers([{"speaker": absent}], set(), source))

    def test_unverifiable_band_at_both_gates(self):
        from generate_script import LLMGenParams
        from three_pass_generate import (attested_new_speakers, attribute_batch,
                                         build_roster)

        frozen = [{"type": "SPOKEN", "text": "おはよう"}]
        for count in range(1, UNCASED_MIN_NAME_ATTESTATIONS):
            for pad in ("", self.LONG_FILLER):
                source = ("太郎は静かに歩いた。" * count) + pad
                with self.subTest(count=count, long=bool(pad)):
                    self.assertEqual(NAME_ATTESTATION_UNVERIFIABLE,
                                     get_name_attestation_verdict("太郎", source))
                    self.assertFalse(is_attested_name("太郎", source))
                    # Output gate does NOT reject unverifiable names or burn retries.
                    report = validate_attribution(
                        frozen, [{"n": 0, "speaker": "太郎"}], source_text=source)
                    self.assertTrue(report["passed"], report)
                    client = _StubAttributionClient("太郎")
                    out = attribute_batch(
                        client, "stub-model", frozen, LLMGenParams(), roster=[],
                        source_text=source, on_exhaustion="keep", max_retries=1)
                    self.assertEqual(1, client.calls)
                    self.assertEqual("太郎", out[0]["speaker"])
                    self.assertIsNone(out[0].get("attribution_unchecked"))
                    # Roster gate refuses unverifiable names.
                    self.assertEqual([], build_roster(out, source))
                    self.assertEqual([], attested_new_speakers(out, set(), source))

        at_threshold = "太郎は静かに歩いた。" * UNCASED_MIN_NAME_ATTESTATIONS + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("太郎", at_threshold))
        self.assertTrue(is_attested_name("太郎", at_threshold))
        self.assertEqual(["太郎"], build_roster([{"speaker": "太郎"}], at_threshold))
        # Explicit stricter caller threshold is honoured.
        self.assertEqual(NAME_ATTESTATION_UNVERIFIABLE,
                         get_name_attestation_verdict("太郎", at_threshold, min_attestations=5))

    def test_cased_mixed_script_and_letterless_controls_unchanged(self):
        source = (
            "Roxy smiled at Rudi. Roxy laughed. "
            "I remembered what my future me wrote. " * 4
            + "Dr. 太郎 arrived. 太郎は歩いた。太郎が言った。太郎は笑った。太郎は見た。"
            + "Mr. 王明 arrived. 王明走了。王明说了。王明笑了。王明看了。"
            + "太郎a walked. 太郎a spoke. 太郎a smiled. "
            + "123 arrived. 123 spoke. 123 laughed. ??? spoke. ??? laughed. ??? cried. "
            + "анна тихо шла. анна сказала. анна ушла. "
            + self.LONG_FILLER
        )
        for label in ("FUTURE_ME", "REMEMBERED", "DR. 太郎", "MR. 王明",
                      "太郎A", "123", "???", "АННА"):
            with self.subTest(label=label):
                self.assertEqual(NAME_ATTESTATION_UNATTESTED,
                                 get_name_attestation_verdict(label, source))
                self.assertFalse(is_attested_name(label, source))
                report = validate_attribution(
                    [{"type": "SPOKEN", "text": "Hello."}],
                    [{"n": 0, "speaker": label}], source_text=source)
                self.assertFalse(report["passed"])
                self.assertEqual("speaker_not_in_source", report["findings"][0]["code"])

    def test_spaced_uncased_scripts_boundaries_and_clitics(self):
        from three_pass_generate import build_roster

        # Arabic: bare + proclitic prefixes attested; embedded substrings rejected.
        ar_clitic = "قال وأحمد ولأحمد وبأحمد فأحمد لأحمد." + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("أحمد", ar_clitic))
        self.assertEqual(["أحمد"], build_roster([{"speaker": "أحمد"}], ar_clitic))
        ar_inside = "هذا أحمدي وهذا محمدأحمد وهذا بفأحمد وهذا موأحمد." * 5 + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_UNATTESTED,
                         get_name_attestation_verdict("أحمد", ar_inside))

        # Hebrew: bare + proclitic prefixes attested; embedded substrings rejected.
        he_clitic = "אמר ודוד ולדוד בדוד מדוד שדוד." + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("דוד", he_clitic))
        he_inside = "הוא דודי וזה מידוד." * 5 + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_UNATTESTED,
                         get_name_attestation_verdict("דוד", he_inside))

        # Devanagari: bare with spaces/danda attested; matra/compound continuations rejected.
        hi_bare = "राम ने कहा। राम हँसा। राम बोला।" + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("राम", hi_bare))
        hi_inside = "रामायण की कथा। रामेश्वर गए। अभिरराम बोला।" * 5 + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_UNATTESTED,
                         get_name_attestation_verdict("राम", hi_inside))

        # Korean: bare with spaces + attached particles attested; embedded/compound words rejected.
        ko_bare = "민수 말했다. 민수 웃었다. 민수 걸었다." + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("민수", ko_bare))
        ko_particles = "민수가 왔다. 민수에게도 말했다. 민수씨는 웃었다." + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("민수", ko_particles))
        ko_inside = "국민수입이 늘었다. 민수용품도 늘었다. 민수가방이 있다." * 5 + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_UNATTESTED,
                         get_name_attestation_verdict("민수", ko_inside))

    def test_one_letter_uncased_names_are_never_attested(self):
        from generate_script import LLMGenParams
        from three_pass_generate import (attested_new_speakers, attribute_batch,
                                         build_roster)

        # Synthetic Chinese prose: 的 occurs 3x and 他 4x, so occurrence count
        # alone would attest both. 79.9% of a full novel's distinct Han
        # characters reach T, so a one-letter label cannot be judged by count.
        source = ("他的书在那里。你的书也在这里。谁的书在桌子上。"
                  "他说了什么。他人很好。他走了。"
                  "王明来了。王明笑了。王明走了。") + self.LONG_FILLER
        frozen = [{"type": "SPOKEN", "text": "你好"}]
        for name in ("的", "他"):
            with self.subTest(name=name):
                self.assertEqual(NAME_ATTESTATION_UNVERIFIABLE,
                                 get_name_attestation_verdict(name, source))
                self.assertFalse(is_attested_name(name, source))
                # Output gate accepts (no retry, no unchecked flag) ...
                self.assertTrue(validate_attribution(
                    frozen, [{"n": 0, "speaker": name}],
                    source_text=source)["passed"])
                # ... the roster refuses it.
                self.assertEqual([], build_roster([{"speaker": name}], source))
                self.assertEqual(
                    [], attested_new_speakers([{"speaker": name}], set(), source))

        # A supplied cast still ships a one-letter name.
        client = _StubAttributionClient("的")
        out = attribute_batch(
            client, "stub-model", frozen, LLMGenParams(), roster=[],
            source_text=source, cast={"known_names": {"的"}}, max_retries=1)
        self.assertEqual(1, client.calls)
        self.assertEqual("的", out[0]["speaker"])
        self.assertTrue(validate_attribution(
            frozen, [{"n": 0, "speaker": "的"}], source_text=source,
            known_names=frozenset({"的"}))["passed"])

        # Two-letter names are unchanged: >= T is attested and roster-eligible.
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("王明", source))
        self.assertEqual(["王明"], build_roster([{"speaker": "王明"}], source))

    def test_korean_honorific_suffixes_are_attested(self):
        from three_pass_generate import build_roster

        # 씨 was the only honorific the earlier test exercised; a mutant that
        # cut the alternation to 씨 survived. Each alternative is pinned.
        for honorific, particle in (("씨", "가"), ("님", "이"),
                                    ("군", "은"), ("양", "도")):
            source = (f"민수{honorific}{particle} 왔다. "
                      f"민수{honorific}{particle} 웃었다. "
                      f"민수{honorific}{particle} 말했다.") + self.LONG_FILLER
            with self.subTest(honorific=honorific):
                self.assertEqual(NAME_ATTESTATION_ATTESTED,
                                 get_name_attestation_verdict("민수", source))
                self.assertEqual(["민수"],
                                 build_roster([{"speaker": "민수"}], source))

    def test_spaced_script_known_residuals_are_pinned(self):
        from pass_quality import _count_uncased_occurrences

        # Documented false positives (Limits in the PR): a spaced-script word
        # beginning with a proclitic that contains the candidate name. Counted
        # by design today; pinned so a future change is deliberate.
        self.assertEqual(1, _count_uncased_occurrences("مال", "كمال"))
        self.assertEqual(1, _count_uncased_occurrences("تاب", "كتاب"))
        self.assertEqual(1, _count_uncased_occurrences("בן", "לבן"))
        self.assertEqual(1, _count_uncased_occurrences("בד", "כבד"))
        # Documented false negatives: the proclitic lookback is two characters,
        # and the Korean suffix alternation does not cover 만의 / 라는.
        self.assertEqual(0, _count_uncased_occurrences("كتاب", "بالكتاب"))
        self.assertEqual(0, _count_uncased_occurrences("أحمد", "بفأحمد"))
        self.assertEqual(0, _count_uncased_occurrences("민수", "민수만의"))
        self.assertEqual(0, _count_uncased_occurrences("민수", "민수라는"))

    def test_cjk_name_in_unspaced_prose_and_inside_longer_word(self):
        from three_pass_generate import build_roster

        # Unspaced prose with word chars on both sides passes (no \w boundary).
        unspaced = "向王明说道。看王明笑了。问王明去哪。" + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("王明", unspaced))
        self.assertEqual(["王明"], build_roster([{"speaker": "王明"}], unspaced))

        # Soft hyphen inside a CJK name or source folds cleanly (#2316).
        self.assertEqual(NAME_ATTESTATION_ATTESTED,
                         get_name_attestation_verdict("王\u00ad明", unspaced))

        # Fewer than T matches inside longer CJK compounds stay unverifiable:
        # accepted at output gate, refused from roster.
        compound_only = "桃太郎が来た。金太郎も来た。" + self.LONG_FILLER
        self.assertEqual(NAME_ATTESTATION_UNVERIFIABLE,
                         get_name_attestation_verdict("太郎", compound_only))
        self.assertTrue(validate_attribution(
            [{"type": "SPOKEN", "text": "こんにちは"}],
            [{"n": 0, "speaker": "太郎"}], source_text=compound_only)["passed"])
        self.assertEqual([], build_roster([{"speaker": "太郎"}], compound_only))


if __name__ == "__main__":
    unittest.main()
