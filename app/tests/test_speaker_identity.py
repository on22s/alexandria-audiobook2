import contextlib
import copy
import io
import json
import unicodedata
import unittest

from speaker_identity import (build_speaker_consistency_report,
                              get_resolved_speaker_merge_map,
                              get_safe_alias_proposals,
                              get_saveable_alias_graph,
                              get_speaker_label_index,
                              get_validated_alias_graph,
                              get_validated_alias_map,
                              is_group_speaker_label,
                              is_speaker_merge_allowed, resolve_speaker_label,
                              stabilize_speaker_identities,
                              _CANDRABINDU_ANUSVARA, _IDENTITY_MARK_SCRIPTS,
                              _get_alias_roots, _identity_key,
                              _is_extended_person_name, _is_identity_mark,
                              _OPTIONAL_IDENTITY_MARKS, _uncertain_candidates)


def _entry(speaker):
    return {"speaker": speaker, "text": "Spoken text.", "instruct": "Natural."}


class SpeakerIdentityTests(unittest.TestCase):
    def test_prose_split_repair_respects_established_roster_spelling(self):
        entries = [{"speaker": "R UDEUS", "text": "Rudeus returned."}]
        original = copy.deepcopy(entries)
        result = stabilize_speaker_identities(entries, ["R UDEUS", "UNUSED ROSTER"])
        self.assertEqual("R UDEUS", result["entries"][0]["speaker"])
        self.assertEqual(["R UDEUS", "UNUSED ROSTER"], result["speakers"])
        self.assertEqual(original, entries)

    def test_split_repair_requires_a_complete_name_token_in_prose(self):
        for text in ("Rudeusson returned.", "Arudeus returned.", "Rudeus_1 returned."):
            with self.subTest(text=text):
                result = stabilize_speaker_identities([{"speaker": "R UDEUS", "text": text}])
                self.assertEqual("R UDEUS", result["entries"][0]["speaker"])

    def test_split_repair_returns_the_actual_repaired_speaker_and_keeps_roster(self):
        result = stabilize_speaker_identities(
            [{"speaker": "R UDEUS", "text": "(Rudeus) returned."},
             {"speaker": "ALICE", "text": "Hello."}], ["UNUSED ROSTER"])
        self.assertEqual(["RUDEUS", "ALICE"], [e["speaker"] for e in result["entries"]])
        self.assertEqual({"RUDEUS", "ALICE", "UNUSED ROSTER"}, set(result["speakers"]))
        self.assertNotIn("R UDEUS", result["speakers"])


    def test_canonically_equivalent_unicode_names_share_existing_identity(self):
        composed = "JOSÉ"
        decomposed = "JOSE\u0301"
        self.assertEqual(composed, resolve_speaker_label(decomposed, [composed]))
        entries = [_entry(decomposed)]
        original = copy.deepcopy(entries)
        result = stabilize_speaker_identities(entries, [composed])
        self.assertEqual(composed, result["entries"][0]["speaker"])
        self.assertEqual(original, entries)
        self.assertIsNone(resolve_speaker_label("JOSÈ", [composed]))
        self.assertIsNone(resolve_speaker_label("ＪＯＳＥ", ["JOSE"]))


    def test_consistency_report_counts_usage_and_deduplicates_suggestions(self):
        entries = [{"speaker": "ROSWAAL"}, {"speaker": "ROSWAL"},
                   {"speaker": "ROSWAAL"}]
        review = [{"entry_number": 2, "speaker": "ROSWAL",
                   "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]},
                  {"entry_number": 3, "speaker": "ROSWAL",
                   "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]}]

        report = build_speaker_consistency_report(entries, review)

        self.assertEqual(2, report["speaker_count"])
        self.assertEqual(2, report["speakers"][0]["entry_count"])
        self.assertEqual(1, len(report["review_suggestions"]))
    def test_safe_variants_reuse_first_established_spelling_without_mutation(self):
        entries = [_entry(" roswaal "), _entry("ROSWAAL"), _entry("Voice O.S.")]
        original = copy.deepcopy(entries)

        result = stabilize_speaker_identities(entries, ["ROSWAAL", "VOICE (O.S.)"])

        self.assertEqual(["ROSWAAL", "ROSWAAL", "VOICE (O.S.)"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual(original, entries)

    def test_uncertain_typo_and_extended_name_are_reported_not_merged(self):
        entries = [_entry("ROSWAL"), _entry("OTTO SUWEN")]

        result = stabilize_speaker_identities(entries, ["ROSWAAL", "OTTO"])

        self.assertEqual(["ROSWAL", "OTTO SUWEN"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual(["ROSWAAL"],
                         [item["speaker"] for item in result["review"][0]["candidates"]])
        self.assertEqual(["OTTO"],
                         [item["speaker"] for item in result["review"][1]["candidates"]])

    def test_distinct_names_are_not_reported(self):
        result = stabilize_speaker_identities(
            [_entry("EMILIA"), _entry("VILLAGER 2"), _entry("SUBARU'S MOTHER")],
            ["SUBARU", "VILLAGER 1"])
        self.assertEqual([], result["review"])

    def test_resolve_speaker_label_matches_punctuation_and_spacing_variants(self):
        labels = ["MR. SMITH", "NARRATOR"]
        self.assertEqual("MR. SMITH", resolve_speaker_label("MR SMITH", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("Mr.Smith", labels))

    def test_resolve_speaker_label_returns_none_when_no_match(self):
        self.assertIsNone(resolve_speaker_label("NOBODY", ["MR. SMITH", "NARRATOR"]))
        self.assertIsNone(resolve_speaker_label("", ["MR. SMITH"]))

    def test_resolve_speaker_label_is_deterministic_on_duplicate_keys(self):
        # Both labels normalize to the same identity key; sorted order picks
        # the same winner every time regardless of input iteration order.
        labels = ["Mr Smith", "MR. SMITH"]
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", list(reversed(labels))))




class CombiningMarkIdentityTests(unittest.TestCase):
    """A combining mark is kept only where it is part of the letter.

    THE BUG. `_identity_key` removed everything that is not `\\w`, and `\\w`
    does not match combining marks (Mn/Mc). Devanagari vowel signs are Mc, so
    `राम` and `रमा` both folded to `रम`, and `सीता`/`सता` to `सत`; the
    stabilizer then rewrote the second character's label to the first and two
    characters shared one voice.

    THE OTHER HALF. Keeping every Mn/Mc split names that are one character:
    casefold turns `İ` into `i` plus a dot, and Arabic harakat, Hebrew niqqud
    and Latin/Greek accents are optional diacritics, not different names. Only
    the marks of the scripts whose marks are required are identity-bearing
    (the Brahmic and Southeast-Asian abugidas plus Thaana, N'Ko, Bassa Vah and
    the later tone-mark scripts), and within those anusvara is too
    (`रंजीत`/`रजीत` are different names) while candrabindu is its other
    spelling (`चाँद`/`चांद` are one).
    """

    def test_devanagari_vowel_signs_are_kept_in_the_key(self):
        self.assertEqual("राम", _identity_key("राम"))
        self.assertNotEqual(_identity_key("राम"), _identity_key("रमा"))
        self.assertNotEqual(_identity_key("सीता"), _identity_key("सता"))

    def test_other_abugida_marks_keep_names_distinct(self):
        pairs = [
            ("Bengali", "রাম", "রমা"),           # Mc vowel sign
            ("Tamil", "ராமா", "ரமா"),             # Mc vowel sign
            ("Tamil", "ராம்", "ராம"),             # Mn vowel sign/virama
            ("Thai", "กิ", "ก"),                  # Mn vowel sign
            ("Khmer", "កិ", "ក"),                  # Mn vowel sign
            ("Tibetan", "ཀི", "ཀ"),                # Mn vowel sign
        ]
        for script, first, second in pairs:
            with self.subTest(script=script):
                self.assertNotEqual(_identity_key(first), _identity_key(second))

    def test_nukta_folds_for_the_same_name(self):
        """A nukta is added or omitted by writers, so it does not split a name.

        It changes a consonant's value, but `ज़ोया`/`जोया` and
        `फ़ातिमा`/`फातिमा` are the same character, and main folded the nukta
        with every other mark. Keeping it split one person into two voices.
        """
        self.assertEqual(_identity_key("जोया"), _identity_key("ज़ोया"))
        self.assertEqual(_identity_key("फातिमा"), _identity_key("फ़ातिमा"))
        self.assertEqual("ज़ोया", resolve_speaker_label("जोया", ["ज़ोया"]))
        self.assertEqual("जोया", resolve_speaker_label("ज़ोया", ["जोया"]))
        result = stabilize_speaker_identities(
            [_entry("जोया"), _entry("ज़ोया")], ["जोया"])
        self.assertEqual(["जोया", "जोया"],
                         [entry["speaker"] for entry in result["entries"]])

    def test_precomposed_nukta_letter_matches_its_base(self):
        # NFC leaves the nukta letters decomposed (U+0958 -> क + nukta), so
        # the precomposed letter, the decomposed spelling and plain क are one
        # name once the nukta folds.
        self.assertEqual(_identity_key("\u0958"), _identity_key("क\u093c"))
        self.assertEqual(_identity_key("\u0958"), _identity_key("क"))

    def test_candrabindu_folds_with_anusvara(self):
        """Both are the same nasal spelling, so they share a key.

        Candrabindu is mapped onto the same script's anusvara, which keeps
        `चाँद`/`चांद` one name. A name with neither nasal mark is still
        different (`चाँद`/`चाद`), because anusvara itself is an identity mark.
        """
        self.assertEqual(_identity_key("चाँद"), _identity_key("चांद"))
        self.assertNotEqual(_identity_key("चाँद"), _identity_key("चाद"))
        self.assertEqual("चाँद", resolve_speaker_label("चांद", ["चाँद"]))
        result = stabilize_speaker_identities(
            [_entry("चाँद"), _entry("चांद")], ["चाँद"])
        self.assertEqual(["चाँद", "चाँद"],
                         [entry["speaker"] for entry in result["entries"]])

    def test_script_nasal_variants_fold_to_their_plain_anusvara(self):
        """Gurmukhi tippi/adak bindi, combining anusvara above and inverted
        candrabindu share a key with their script's plain anusvara, while a
        name with no nasal mark still splits.
        """
        pairs = [
            ("Gurmukhi tippi vs bindi", "ਸਿੰਘ", "ਸਿਂਘ", "ਸਿਘ"),
            ("Gurmukhi adak bindi vs bindi", "ਸਿ\u0a01ਘ", "ਸਿਂਘ", "ਸਿਘ"),
            ("Telugu combining anusvara above", "స\u0c04గీత", "సంగీత", "సగీత"),
            ("Telugu combining candrabindu above", "స\u0c00గీత", "సంగీత", "సగీత"),
            ("Malayalam combining anusvara above", "ഗ\u0d00", "ഗം", "ഗ"),
            ("Grantha combining anusvara above", "\U00011315\U00011300",
             "\U00011315\U00011302", "\U00011315"),
            ("Devanagari inverted candrabindu", "चा\u0900द", "चांद", "चाद"),
            ("Sharada inverted candrabindu", "\U00011191\U000111cf",
             "\U00011191\U00011181", "\U00011191"),
        ]
        for label, variant, anusvara, bare in pairs:
            with self.subTest(label=label):
                self.assertEqual(_identity_key(variant), _identity_key(anusvara))
                self.assertNotEqual(_identity_key(variant), _identity_key(bare))
        self.assertEqual("ਸਿੰਘ", resolve_speaker_label("ਸਿਂਘ", ["ਸਿੰਘ"]))
        result = stabilize_speaker_identities(
            [_entry("ਸਿੰਘ"), _entry("ਸਿਂਘ")], ["ਸਿੰਘ"])
        self.assertEqual(["ਸਿੰਘ", "ਸਿੰਘ"],
                         [entry["speaker"] for entry in result["entries"]])

    def test_zanabazar_candrabindu_with_ornament_matches_candrabindu_and_anusvara(self):
        """U+11A36 is a candrabindu, so it maps to anusvara like U+11A35.

        U+11A37 (CANDRA WITH ORNAMENT, no bindu) remains an optional ornament
        and folds away.
        """
        base = "\U00011a00"
        self.assertEqual(_identity_key(base + "\U00011a36"),
                         _identity_key(base + "\U00011a38"))
        self.assertEqual(_identity_key(base + "\U00011a36"),
                         _identity_key(base + "\U00011a35"))
        self.assertNotEqual(_identity_key(base + "\U00011a36"),
                            _identity_key(base))
        self.assertEqual(_identity_key(base + "\U00011a37"),
                         _identity_key(base))

    def test_every_unicode_nasal_sign_maps_to_its_own_scripts_plain_anusvara(self):
        """Enumerate Unicode Mn/Mc nasal signs rather than hand-picking.

        Every table entry must map a kept mark directly to its own script's
        plain anusvara (never chained through another mapped mark), and every
        Unicode mark named CANDRABINDU / ANUSVARA / BINDI / TIPPI in a script
        that has a plain anusvara must be an identity mark and fold to that
        script's plain anusvara. Checking the script first is the point: a
        nasal mark in a kept script that an optional-mark keyword masks out is
        exactly the #2347 split this table exists to fix.
        """
        keywords = ("CANDRABINDU", "ANUSVARA", "BINDI", "TIPPI")
        plain_suffixes = ("SIGN ANUSVARA", "SIGN ANUSVARAYA", "SIGN BINDI")

        def script_of(char):
            name = unicodedata.name(char, "")
            return next((s for s in _IDENTITY_MARK_SCRIPTS
                         if name.startswith(s + " ")), None)

        for src, dst in _CANDRABINDU_ANUSVARA.items():
            if "Cn" in (unicodedata.category(src), unicodedata.category(dst)):
                # Unicode 15 added U+0CF3 and the Kawi block. Older
                # interpreters (CI runs 3.10 / Unicode 13) cannot classify a
                # code point they do not know; the enumeration below still
                # checks every mark the running tables do define.
                continue
            with self.subTest(entry=(hex(ord(src)), hex(ord(dst)))):
                self.assertTrue(_is_identity_mark(src))
                self.assertTrue(_is_identity_mark(dst))
                self.assertNotEqual(src, dst)
                self.assertNotIn(dst, _CANDRABINDU_ANUSVARA)
                self.assertIsNotNone(script_of(src))
                self.assertEqual(script_of(src), script_of(dst))
                self.assertTrue(
                    unicodedata.name(dst, "").endswith(plain_suffixes),
                    f"{hex(ord(dst))} ({unicodedata.name(dst, '')}) is not a plain anusvara",
                )

        by_script = {}
        for cp in range(0x110000):
            char = chr(cp)
            if unicodedata.category(char) not in ("Mn", "Mc"):
                continue
            name = unicodedata.name(char, "")
            if not any(kw in name for kw in keywords):
                continue
            script = script_of(char)
            if script is None:
                # Scriptless marks (U+0310 COMBINING CANDRABINDU) fold
                # everywhere; they are not a script's nasal sign.
                continue
            self.assertTrue(
                _is_identity_mark(char),
                f"{hex(ord(char))} ({name}) is a nasal sign in {script} but folds away",
            )
            by_script.setdefault(script, []).append((char, name))

        for script, marks in by_script.items():
            plains = [c for c, n in marks if n.endswith(plain_suffixes)]
            if not plains:
                # Single standalone mark such as LIMBU SMALL LETTER ANUSVARA.
                self.assertEqual(1, len(marks), f"{script}: {marks}")
                continue
            self.assertEqual(1, len(plains), f"{script}: multiple plain anusvaras {plains}")
            plain = plains[0]
            for char, name in marks:
                if char == plain:
                    continue
                with self.subTest(script=script, mark=hex(ord(char)), name=name):
                    self.assertEqual(plain, _CANDRABINDU_ANUSVARA.get(char))
                    self.assertEqual(_identity_key("a" + char),
                                     _identity_key("a" + plain))

    def test_script_name_must_start_the_character_name(self):
        """A script name appearing mid-name must not turn a mark into an identity mark.

        U+A8E0..U+A8F1 are COMBINING DEVANAGARI Vedic cantillation marks,
        U+11366..U+11374 are COMBINING GRANTHA marks, and Adlam/SignWriting
        MODIFIER marks contain 'MODI' as a substring; all must fold away.
        """
        for cp in range(0xA8E0, 0xA8F2):
            with self.subTest(cp=hex(cp)):
                self.assertEqual(_identity_key("क"),
                                 _identity_key("क" + chr(cp)))
        for char in ("\U00011366", "\U0001da9b", "\U0001e948"):
            with self.subTest(cp=hex(ord(char))):
                self.assertEqual(_identity_key("a"),
                                 _identity_key("a" + char))

    def test_anusvara_is_an_identity_mark(self):
        """Keeping anusvara splits real distinct names instead of merging them.

        Folding every nasal sign merged these pairs, which are different
        people. Candrabindu folds *into* anusvara; anusvara itself does not
        fold.
        """
        pairs = [
            ("Hindi", "रंजीत", "रजीत"),
            ("Sinhala", "රංජිත්", "රජිත්"),
            ("Sinhala", "සංජීව", "සජීව"),
            ("Kannada", "ರಂಜಿತ್", "ರಜಿತ್"),
            ("Telugu", "సంగీత", "సగీత"),
            ("Myanmar", "\u1000\u1036", "\u1000"),
        ]
        for script, first, second in pairs:
            with self.subTest(script=script):
                self.assertNotEqual(_identity_key(first), _identity_key(second))

    def test_script_specific_nasal_marks_stay_kept(self):
        """Thai/Lao/Khmer/Limbu nasal marks were already kept; keep them."""
        pairs = [
            ("Thai NIKHAHIT", "\u0e01\u0e4d"),
            ("Lao NIGGAHITA", "\u0e81\u0ecd"),
            ("Khmer NIKAHIT", "\u1780\u17c6"),
            ("Limbu U+1932", "\u1900\u1932"),
        ]
        for label, marked in pairs:
            with self.subTest(label=label):
                self.assertNotEqual(_identity_key(marked),
                                    _identity_key(marked[0]))

    def test_mark_order_is_fixed_without_merging_different_marks(self):
        """NFC does not order a class-0 sign against a tone mark.

        Thai `กิ่`/`ก่ิ`, Lao `ກິ່`/`ກ່ິ` and Khmer `កំុ`/`កុំ` are the same
        spelling typed in either order, so the key sorts each run of kept
        marks. Names carrying different marks must still split.
        """
        same = [
            ("Thai", "\u0e01\u0e34\u0e48", "\u0e01\u0e48\u0e34"),
            ("Lao", "\u0e81\u0eb4\u0ec8", "\u0e81\u0ec8\u0eb4"),
            ("Khmer", "\u1780\u17c6\u17bb", "\u1780\u17bb\u17c6"),
        ]
        for script, first, second in same:
            with self.subTest(script=script):
                self.assertEqual(_identity_key(first), _identity_key(second))
        different = [
            ("Thai tone", "\u0e01\u0e34\u0e48", "\u0e01\u0e34\u0e49"),
            ("Lao tone", "\u0e81\u0eb4\u0ec8", "\u0e81\u0eb4\u0ec9"),
            ("Khmer mark", "\u1780\u17c6\u17bb", "\u1780\u17c6\u17b7"),
        ]
        for script, first, second in different:
            with self.subTest(script=script):
                self.assertNotEqual(_identity_key(first), _identity_key(second))

    def test_lao_cancellation_and_thai_thanthakhat_are_kept(self):
        # U+0ECC is Lao's counterpart of Thai THANTHAKHAT U+0E4C, which was
        # already kept: both are identity marks, not optional decoration.
        self.assertNotEqual(_identity_key("\u0e81\u0ecc"),
                            _identity_key("\u0e81"))
        self.assertNotEqual(_identity_key("\u0e01\u0e4c"),
                            _identity_key("\u0e01"))

    def test_nko_and_bassa_vah_tone_marks_keep_names_distinct(self):
        # Their tone marks are required; before this they were outside the
        # script gate and folded away.
        self.assertNotEqual(_identity_key("\u07ca\u07eb"),
                            _identity_key("\u07ca\u07ec"))
        self.assertNotEqual(_identity_key("\u07ca\u07eb"),
                            _identity_key("\u07ca"))
        self.assertNotEqual(_identity_key("\U00016ad0\U00016af0"),
                            _identity_key("\U00016ad0\U00016af1"))

    def test_protected_labels_keep_their_own_key(self):
        """A required mark appended to a Latin word makes a different label.

        `UNKNOWNि` is not the protected `UNKNOWN`: the key keeps the
        Devanagari vowel sign, so the label is distinct. main's fold dropped
        the sign and therefore refused this merge; head allows it, which the
        gate found consistent and not exploitable, because plain
        `UNKNOWN`/`NARRATOR` stay protected and the suffixed label still needs
        an explicit alias. Pinned rather than changed: refusing it would need
        a second key for the protected check.
        """
        self.assertFalse(is_speaker_merge_allowed("UNKNOWN", "Bob"))
        self.assertTrue(is_speaker_merge_allowed("UNKNOWN\u093f", "Bob"))

    def test_stress_cantillation_and_decoration_marks_fold(self):
        """Optional marks fold even inside a kept abugida script."""
        pairs = [
            ("राम", "रा\u0951म"),           # U+0951 Devanagari stress udatta
            ("राम", "रा\u0952म"),           # U+0952 Devanagari stress anudatta
            ("राम", "रा\u0953म"),           # U+0953 Devanagari grave accent
            ("राम", "रा\u0954म"),           # U+0954 Devanagari acute accent
            ("ཀ", "ཀ\u0f35"),               # TIBETAN MARK NGAS BZUNG NYI ZLA
            ("ཀ", "ཀ\u0f37"),               # TIBETAN MARK NGAS BZUNG SGOR RTAGS
            ("ཀ", "ཀ\u0f86"),               # TIBETAN SIGN LCI RTAGS
            ("ཀ", "ཀ\u0f87"),               # TIBETAN SIGN YANG RTAGS
            ("ক", "ক\u09fe"),                # BENGALI SANDHI MARK
            ("क", "क\U0001145e"),              # NEWA SANDHI MARK
            ("क", "क\U000111c9"),              # SHARADA SANDHI MARK
            ("\u1b13", "\u1b13\u1b6b"),      # BALINESE MUSICAL SYMBOL COMBINING
            ("\u1b13", "\u1b13\u1b73"),      # ... and the last of the run
            ("ਕ", "ਕ\u0a51"),                # GURMUKHI SIGN UDAAT (stress)
            ("ਕ", "ਕ\u0a75"),                # GURMUKHI SIGN YAKASH (stress)
            ("\U00011600", "\U00011600\U00011640"),   # MODI ARDHACANDRA
            ("\U00011a00", "\U00011a00\U00011a37"),   # ZANABAZAR CANDRA WITH ORNAMENT
            ("\u0f40", "\u0f40\u0f18"),      # TIBETAN ASTROLOGICAL
            ("\u0f40", "\u0f40\u0f19"),      # TIBETAN ASTROLOGICAL
            ("\u0f40", "\u0f40\u0f39"),      # TIBETAN TSA -PHRU
            ("\u0f40", "\u0f40\u0fc6"),      # TIBETAN SYMBOL PADMA GDAN
            ("\u1a20", "\u1a20\u1a7f"),      # TAI THAM CRYPTOGRAMMIC DOT
            ("\u0e01", "\u0e01\u0e4e"),      # THAI YAMAKKAN
            ("\u0b15", "\u0b15\u0b55"),      # ORIYA OVERLINE
        ]
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                self.assertEqual(_identity_key(first), _identity_key(second))

    def test_required_marks_in_late_scripts_keep_names_distinct(self):
        """Scripts whose required marks were still folded must split."""
        pairs = [
            ("Thaana vowels", "ހަސަން", "ހުސެން"),
            ("Kayah Li tone", "\ua90a", "\ua90a\ua92b"),
            ("Miao vowel", "\U00016f00", "\U00016f00\U00016f54"),
            ("Miao tone", "\U00016f00", "\U00016f00\U00016f8f"),
            ("Wancho tone", "\U0001e2c0", "\U0001e2c0\U0001e2ec"),
            ("Nyiakeng tone", "\U0001e100", "\U0001e100\U0001e130"),
        ]
        for script, first, second in pairs:
            with self.subTest(script=script):
                self.assertNotEqual(_identity_key(first), _identity_key(second))

    def test_mahajani_nukta_folds_like_every_nukta(self):
        # Mahajani's only mark is the nukta, so it makes no new name.
        self.assertEqual(_identity_key("\U00011150"),
                         _identity_key("\U00011150\U00011173"))

    def test_optional_diacritics_fold_exactly_as_before(self):
        """The pairs main already merged must stay merged.

        Latin/Greek/Cyrillic accents (including the ones casefold introduces),
        Arabic harakat and Hebrew niqqud are optional: they do not make a new
        name, so they fold away.
        """
        pairs = [
            ("İbrahim", "Ibrahim"),              # casefold makes U+0307
            ("İSMAİL", "ISMAIL"),
            ("مُحَمَّد", "محمد"),                   # Arabic harakat
            ("رَام", "رام"),
            ("שָׁלוֹם", "שלום"),                    # Hebrew niqqud
            ("ǰ", "j"), ("ẖ", "h"), ("ẗ", "t"), ("ẘ", "w"), ("ẙ", "y"),
            ("ΐ", "ι"), ("ΰ", "υ"), ("ῦ", "υ"), ("ᾶ", "α"),
        ]
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                self.assertEqual(_identity_key(first), _identity_key(second))

    def test_precomposed_greek_varia_is_left_as_main_had_it(self):
        # `ὺ` (U+1F7A) is one letter, not a bare mark: NFC keeps it composed
        # and the old key kept it too. Only the forms casefold actually
        # decomposes (ΐ, ΰ, ᾶ, ῦ) fold to their base letter.
        self.assertEqual("ὺ", _identity_key("ὺ"))
        self.assertEqual("ὺ", _identity_key("υ\u0300"))
        self.assertNotEqual(_identity_key("ὺ"), _identity_key("υ"))

    def test_controls_and_latin_keys_are_unchanged(self):
        """Representative Latin keys stay as they were."""
        expected = {
            "Ram": "ram", "Rama": "rama",
            "MR SMITH": "mrsmith", "MRS SMITH": "mrssmith",
            "김민수": "김민수", "김민소": "김민소",
            "สมชาย": "สมชาย", "สมชย": "สมชย",
            "R UDEUS": "rudeus", "RuDeUs": "rudeus",
            "SUBARU'S": "subarus", "MR. SMITH": "mrsmith",
            "Má": "má", "Ma": "ma",
        }
        for name, key in expected.items():
            with self.subTest(name=name):
                self.assertEqual(key, _identity_key(name))

    def test_latin_accents_match_across_composed_and_decomposed_forms(self):
        decomposed = "JOSE\u0301"
        self.assertEqual(_identity_key("JOSÉ"), _identity_key(decomposed))
        self.assertEqual(_identity_key("Nguyễn"), _identity_key("Nguye\u0302\u0303n"))
        self.assertNotEqual(_identity_key("JOSÉ"), _identity_key("JOSE"))
        # Casefold decomposes ǰ into j plus a combining caron; that mark is
        # not part of an abugida, so it folds away and matches plain j. NFC
        # runs once, before casefold - it is not run again to recompose.
        self.assertEqual(_identity_key("ǰ"), _identity_key("J\u030C"))
        self.assertEqual(_identity_key("ǰ"), _identity_key("j"))

    def test_casefold_and_optional_diacritics_merge_end_to_end(self):
        result = stabilize_speaker_identities(
            [_entry("İBRAHİM"), _entry("Ibrahim")], ["İBRAHİM"])
        self.assertEqual(["İBRAHİM", "İBRAHİM"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual("İBRAHİM",
                         resolve_speaker_label("Ibrahim", ["İBRAHİM"]))
        result = stabilize_speaker_identities(
            [_entry("مُحَمَّد"), _entry("محمد")], ["مُحَمَّد"])
        self.assertEqual(["مُحَمَّد", "مُحَمَّد"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual("مُحَمَّد",
                         resolve_speaker_label("محمد", ["مُحَمَّد"]))

    def test_two_devanagari_names_are_not_relabelled_by_stabilization(self):
        entries = [_entry("राम"), _entry("रमा")]
        result = stabilize_speaker_identities(entries, ["राम", "रमा"])
        self.assertEqual(["राम", "रमा"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual([], result["changes"])
        self.assertEqual({"राम", "रमा"}, set(result["speakers"]))

    def test_the_order_of_arrival_does_not_change_the_outcome(self):
        result = stabilize_speaker_identities(
            [_entry("सता"), _entry("सीता")], ["सीता", "सता"])
        self.assertEqual(["सता", "सीता"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual([], result["changes"])

    def test_a_saved_script_reloaded_from_disk_keeps_both_speakers(self):
        """Saved books store the labels, not the key, so reload re-derives it."""
        entries = json.loads(json.dumps([_entry("राम"), _entry("रमा")]))
        result = stabilize_speaker_identities(entries, ["राम", "रमा"])
        self.assertEqual(["राम", "रमा"],
                         [entry["speaker"] for entry in result["entries"]])

    def test_punctuation_and_spacing_still_fold_one_devanagari_name(self):
        result = stabilize_speaker_identities([_entry(" राम "), _entry("राम।")],
                                              ["राम"])
        self.assertEqual(["राम", "राम"],
                         [entry["speaker"] for entry in result["entries"]])

    def test_label_lookup_keeps_combining_mark_names_apart(self):
        labels = ["सीता", "सता"]
        self.assertEqual("सीता", resolve_speaker_label("सीता ", labels))
        self.assertIsNone(resolve_speaker_label("सता", ["सीता"]))
        self.assertEqual({"सीता", "सता"}, set(get_speaker_label_index(labels)))

    def test_alias_registry_does_not_report_a_false_target_conflict(self):
        """Two distinct names may each have their own canonical.

        Under the old key both variants folded to `रम`, so the second target
        looked like a conflicting normalization and the whole registry was
        refused. They are different characters, so both edges are valid.
        """
        registry = get_validated_alias_map({"राम": "रामजी", "रमा": "रमेश"})
        self.assertEqual({"राम": "रामजी", "रमा": "रमेश"}, registry)


class SplitNameSpellingTest(unittest.TestCase):
    """The best-formed spelling wins, not the first-arrived one.

    THE BUG. Generating mushoku18, the model emitted `R UDEUS` 86 times and
    `RUDEUS` 16 - the name split after its first letter, which happened to
    exactly one character in three books (every other capitalised name came
    through whole). Both spellings normalise to the same identity, so they were
    already being merged; the canonical was simply whichever arrived first, and
    the split one arrived first. All 102 lines in that book and 137 in
    mushoku23 ended up attributed to a person no cast list contains, so they
    match no voice.
    """

    def test_the_intact_spelling_wins_even_when_the_split_one_comes_first(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "first"},
            {"speaker": "RUDEUS", "text": "second"},
        ])
        self.assertEqual(["RUDEUS", "RUDEUS"],
                         [e["speaker"] for e in out["entries"]])
        self.assertIn("RUDEUS", out["speakers"])
        self.assertNotIn("R UDEUS", out["speakers"])

    def test_a_genuinely_multi_word_name_is_left_alone(self):
        """The repair must not glue real names together: these have no
        alternative spelling, so there is nothing to prefer."""
        names = ["NORTH KING WII TAA", "OLD MAN", "KNUCKLE GUARD"]
        out = stabilize_speaker_identities([{"speaker": n, "text": "x"} for n in names])
        self.assertEqual(names, [e["speaker"] for e in out["entries"]])

    def test_the_roster_still_wins_over_both_spellings(self):
        # An explicit cast list is the caller's own answer and outranks
        # anything inferred from how the model happened to write it.
        out = stabilize_speaker_identities(
            [{"speaker": "R UDEUS", "text": "a"}, {"speaker": "RUDEUS", "text": "b"}],
            established_speakers=["Rudeus"])
        self.assertEqual(["Rudeus", "Rudeus"], [e["speaker"] for e in out["entries"]])

    def test_the_change_is_recorded_so_it_can_be_audited(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "first"},
            {"speaker": "RUDEUS", "text": "second"},
        ])
        kinds = {c["type"] for c in out["changes"]}
        self.assertIn("speaker_spelling", kinds)


class FinishedScriptRepairTest(unittest.TestCase):
    """A script already saved with the broken name must still be repairable.

    Preferring the better spelling only helps while both exist. In mushoku18's
    saved script they do not: generation had already collapsed `RUDEUS` onto
    `R UDEUS` line by line, leaving 67 of the broken form and none of the good
    one. The book's own prose is the evidence - it says "Rudeus" 123 times and
    "R udeus" never.
    """

    def test_a_split_name_is_rejoined_when_the_prose_confirms_it(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "『Ariel-sama.』"},
            {"speaker": "NARRATOR", "text": "Rudeus turned to look at her."},
        ])
        self.assertEqual("RUDEUS", out["entries"][0]["speaker"])

    def test_nothing_is_joined_without_that_evidence(self):
        """`J SMITH` is a person, not a typo. Guessing invents a character."""
        out = stabilize_speaker_identities([
            {"speaker": "J SMITH", "text": "He said nothing at all."},
        ])
        self.assertEqual("J SMITH", out["entries"][0]["speaker"])

    def test_a_two_word_name_is_not_glued_together(self):
        out = stabilize_speaker_identities([
            {"speaker": "OLD MAN", "text": "The old man walked away."},
        ])
        self.assertEqual("OLD MAN", out["entries"][0]["speaker"])

    def test_the_repair_is_recorded(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "Rudeus said so himself."},
        ])
        self.assertIn("speaker_split_repair", {c["type"] for c in out["changes"]})


class AliasRegistryBoundaryAndConflictTests(unittest.TestCase):
    def test_alias_registry_count_and_name_length_boundaries(self):
        at_limit = {f"v{i}": f"c{i}" for i in range(10000)}
        validated = get_validated_alias_map(at_limit)
        self.assertEqual(10000, len(validated))

        over_limit = dict(at_limit)
        over_limit["v10000"] = "c10000"
        with self.assertRaises(ValueError):
            get_validated_alias_map(over_limit)

        max_name = "a" * 1024
        max_target = "b" * 1024
        self.assertEqual({max_name: max_target},
                         get_validated_alias_map({max_name: max_target}))
        with self.assertRaises(ValueError):
            get_validated_alias_map({"a" * 1025: "b"})
        with self.assertRaises(ValueError):
            get_validated_alias_map({"a": "b" * 1025})

    def test_alias_registry_stripped_and_normalized_target_conflicts(self):
        # Two raw keys stripping to the same variant with different exact targets.
        with self.assertRaises(ValueError):
            get_validated_alias_map({" A": "B", "A": "b"})

        # Two raw keys stripping to the same variant with the same target.
        self.assertEqual({"A": "B"},
                         get_validated_alias_map({"A": "B", "A ": "B"}))

        # Two spellings of one identity pointing to the same canonical target.
        self.assertEqual({"A": "B", "a": "B"},
                         get_validated_alias_map({"A": "B", "a": "B"}))

        # Two spellings of one identity pointing to conflicting canonical targets.
        with self.assertRaises(ValueError):
            get_validated_alias_map({"A": "B", "a": "C"})

    def test_alias_registry_rejects_empty_variant_or_target(self):
        # Stripping happens before this check, so whitespace-only is empty too.
        for value in ({"": "A"}, {" ": "A"}, {"A": ""}, {"A": " "}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_validated_alias_map(value)


class SpeakerIdentityEdgeAndAuditTests(unittest.TestCase):
    def test_empty_and_missing_speakers_pass_through_unchanged(self):
        entries = [
            {"speaker": None, "text": "a"},
            {"speaker": "", "text": "b"},
            {"text": "d"},
        ]
        result = stabilize_speaker_identities(
            entries, established_speakers=[None, "", "ALICE", "ALICE"])
        self.assertEqual(entries, result["entries"])
        self.assertEqual([], result["changes"])
        self.assertEqual([], result["review"])
        self.assertEqual(["ALICE"], result["speakers"])

        # Whitespace-only speaker passes through unchanged with no change or sentinel.
        ws_entries = [{"speaker": "   ", "text": "c"}]
        ws_result = stabilize_speaker_identities(ws_entries, established_speakers=["ALICE"])
        self.assertEqual(ws_entries, ws_result["entries"])
        self.assertEqual([], ws_result["changes"])
        self.assertNotIn("XXXX", ws_result["speakers"])

        self.assertEqual("", _identity_key(None))
        self.assertEqual("", _identity_key(""))
        self.assertFalse(is_group_speaker_label(None))

        report = build_speaker_consistency_report(
            ["not-a-dict", {"speaker": None}, {"speaker": ""}, {"speaker": "ALICE"}])
        self.assertEqual(1, report["speaker_count"])
        self.assertEqual(
            [{"speaker": "ALICE", "entry_count": 1, "first_entry_numbers": [4]}],
            report["speakers"],
        )

    def test_stabilization_continues_past_non_dict_and_non_matching_entries(self):
        # First loop: non-dict entry before a variant that must be canonicalized.
        out_id = stabilize_speaker_identities(
            ["not-a-dict", {"speaker": " roswaal ", "text": "hello"}],
            established_speakers=["ROSWAAL"],
        )
        self.assertEqual("ROSWAAL", out_id["entries"][1]["speaker"])
        self.assertEqual(
            [{"type": "speaker_identity", "entry_number": 2,
              "before": " roswaal ", "after": "ROSWAAL"}],
            out_id["changes"],
        )

        # Spelling pass: non-dict and roster speaker before split/intact pair.
        out_spell = stabilize_speaker_identities(
            ["not-a-dict",
             {"speaker": "EMILIA", "text": "x"},
             {"speaker": "R UDEUS", "text": "a"},
             {"speaker": "RUDEUS", "text": "b"}],
            established_speakers=["EMILIA"],
        )
        self.assertEqual("RUDEUS", out_spell["entries"][2]["speaker"])
        self.assertEqual("RUDEUS", out_spell["entries"][3]["speaker"])
        self.assertIn(
            {"type": "speaker_spelling", "entry_number": 3,
             "before": "R UDEUS", "after": "RUDEUS"},
            out_spell["changes"],
        )

        # Split-repair pass: non-dict and non-split speaker before split name.
        out_split = stabilize_speaker_identities([
            "not-a-dict",
            {"speaker": "NARRATOR", "text": "Rudeus arrived."},
            {"speaker": "R UDEUS", "text": "Hello."},
        ])
        self.assertEqual("RUDEUS", out_split["entries"][2]["speaker"])
        self.assertEqual(
            [{"type": "speaker_split_repair",
              "before": "R UDEUS", "after": "RUDEUS"}],
            out_split["changes"],
        )

    def test_audit_records_entry_numbers_and_speaker_list_order(self):
        out = stabilize_speaker_identities([
            {"speaker": "BOB", "text": "hi"},
            {"speaker": "R UDEUS", "text": "a"},
            {"speaker": "ALICE", "text": "b"},
            {"speaker": "RUDEUS", "text": "c"},
            {"speaker": "ROSWAL", "text": "d"},
        ], established_speakers=["ROSWAAL"])

        self.assertEqual(["ROSWAAL", "BOB", "ALICE", "ROSWAL", "RUDEUS"],
                         out["speakers"])
        # 0.9231 (and 0.9524/0.90 below) are the review payload's SequenceMatcher
        # similarity values shown by the UI, pinned as a contract, not an internal detail.
        self.assertEqual(
            [{"entry_number": 5, "speaker": "ROSWAL",
              "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]}],
            out["review"],
        )
        self.assertIn(
            {"type": "speaker_identity", "entry_number": 4,
             "before": "RUDEUS", "after": "R UDEUS"},
            out["changes"],
        )
        self.assertIn(
            {"type": "speaker_spelling", "entry_number": 2,
             "before": "R UDEUS", "after": "RUDEUS"},
            out["changes"],
        )
        self.assertIn(
            {"type": "speaker_spelling", "entry_number": 4,
             "before": "R UDEUS", "after": "RUDEUS"},
            out["changes"],
        )

    def test_consistency_report_entry_index_cap_and_suggestion_deduplication(self):
        entries = [{"speaker": "ALICE"} for _ in range(7)]
        review = [
            {"entry_number": 2, "speaker": "ROSWAL",
             "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]},
            {"entry_number": 4, "speaker": "ROSWAL",
             "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]},
            {"entry_number": 5, "speaker": "ROSWAA",
             "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]},
            {"entry_number": 6, "speaker": "ROSWAL",
             "candidates": [{"speaker": "ROSWEL", "similarity": 0.90}]},
        ]
        report = build_speaker_consistency_report(entries, review)
        self.assertEqual(
            [{"speaker": "ALICE", "entry_count": 7,
              "first_entry_numbers": [1, 2, 3, 4, 5]}],
            report["speakers"],
        )
        self.assertEqual(
            [
                {"speaker": "ROSWAL",
                 "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}],
                 "example_entry_number": 2},
                {"speaker": "ROSWAA",
                 "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}],
                 "example_entry_number": 5},
                {"speaker": "ROSWAL",
                 "candidates": [{"speaker": "ROSWEL", "similarity": 0.90}],
                 "example_entry_number": 6},
            ],
            report["review_suggestions"],
        )

    def test_best_spelling_wins_when_the_split_spelling_was_seen_first(self):
        # The canonicals best-spelling filter and the later final-speaker pass
        # both keep the intact spelling: the split form is never a second speaker.
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "a"},
            {"speaker": "RUDEUS", "text": "b"},
        ])
        self.assertEqual(["RUDEUS"], out["speakers"])
        self.assertEqual(["RUDEUS", "RUDEUS"],
                         [entry["speaker"] for entry in out["entries"]])


class UnicodeScriptTableAndProtectionTests(unittest.TestCase):
    # Every row of _IDENTITY_MARK_SCRIPTS that owns at least one Mn/Mc code
    # point under the interpreter's Unicode tables. This drives the generated
    # assertions and also keeps the historical row mutants killed: a mutant row
    # name ("XXKHOJKIXX") stops matching its marks, so the exact name must still
    # be present and own marks. MULTANI, KAWI, TULU-TIGALARI, GURUNG KHEMA and
    # NEW TAI LUE own none (see NO_MARK_SCRIPTS).
    MARKED_SCRIPTS = (
        "DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA", "TAMIL",
        "TELUGU", "KANNADA", "MALAYALAM", "SINHALA", "BRAHMI", "KAITHI",
        "KHOJKI", "KHUDAWADI", "SHARADA", "SIDDHAM", "TIRHUTA", "GRANTHA",
        "NEWA", "BHAIKSUKI", "MARCHEN", "MASARAM GONDI", "GUNJALA GONDI",
        "SOYOMBO", "ZANABAZAR SQUARE", "NANDINAGARI", "DIVES AKURU", "CHAKMA",
        "MODI", "TAKRI", "AHOM", "DOGRA", "SAURASHTRA", "SYLOTI NAGRI",
        "MEETEI MAYEK", "KHAROSHTHI", "LEPCHA", "LIMBU", "BUGINESE", "REJANG",
        "MAKASAR", "THAANA", "KAYAH LI", "MIAO", "WANCHO",
        "NYIAKENG PUACHUE HMONG", "NKO", "BASSA VAH", "THAI", "LAO", "TIBETAN",
        "MYANMAR", "KHMER", "BALINESE", "JAVANESE", "SUNDANESE", "BATAK",
        "CHAM", "TAGALOG", "HANUNOO", "BUHID", "TAGBANWA", "TAI THAM",
        "TAI VIET",
    )
    # The remaining rows (MULTANI, KAWI, TULU-TIGALARI, GURUNG KHEMA and
    # NEW TAI LUE) own no Mn/Mc code points in Python 3.10's unicodedata
    # (Unicode 13.0.0). The assertion below is deliberately version-sensitive:
    # KAWI arrives in Unicode 15.0 and TULU-TIGALARI/GURUNG KHEMA in 16.0, so a
    # newer interpreter fails here instead of silently invalidating the
    # "equivalent (Unicode 13.0)" classification in the PR table.

    def test_every_identity_mark_script_row_preserves_its_marks(self):
        # Generated from _IDENTITY_MARK_SCRIPTS against the running
        # interpreter's unicodedata: no hand-picked list of 28 scripts can drift
        # away from the 69 rows again. Optional diacritics (nukta, stress
        # accents, Tibetan decoration) still fold; every required mark of a row
        # survives in the identity key.
        named_marks = []
        for code_point in range(0x110000):
            char = chr(code_point)
            if unicodedata.category(char) in ("Mn", "Mc"):
                name = unicodedata.name(char, "")
                if name:
                    named_marks.append((char, name))

        rows = set(_IDENTITY_MARK_SCRIPTS)
        for script in self.MARKED_SCRIPTS:
            self.assertIn(script, rows)
            marks = [(char, name) for char, name in named_marks
                     if name.startswith(script + " ")]
            with self.subTest(script=script):
                self.assertTrue(marks, f"{script} owns no Mn/Mc code point in this unicodedata")
            for char, name in marks:
                optional = any(word in name for word in _OPTIONAL_IDENTITY_MARKS)
                with self.subTest(script=script, mark=name):
                    if optional:
                        self.assertFalse(_is_identity_mark(char))
                    else:
                        self.assertTrue(_is_identity_mark(char))
                        self.assertNotEqual(_identity_key("a" + char),
                                            _identity_key("a"))
        for script in rows - set(self.MARKED_SCRIPTS):
            marks = [name for _, name in named_marks
                     if name.startswith(script + " ")]
            with self.subTest(script=script):
                self.assertEqual([], marks)

    def test_extended_person_name_rejects_all_relation_words_and_digits(self):
        for relation in ("mother", "father", "sister", "brother", "daughter", "son"):
            with self.subTest(relation=relation):
                self.assertFalse(_is_extended_person_name(f"SUBARU {relation.upper()}", "SUBARU"))
        self.assertFalse(_is_extended_person_name("MAN 2", "MAN"))
        self.assertTrue(_is_extended_person_name("SUBARU NATSUKI", "SUBARU"))

        out = stabilize_speaker_identities(
            [_entry("SUBARU MOTHER"), _entry("SUBARU SON"), _entry("MAN 2")],
            ["SUBARU", "MAN"],
        )
        self.assertEqual([], out["review"])

    def test_uncertain_candidates_threshold_rounding_and_tie_break_order(self):
        # Default identity_keys=None path and 4-decimal rounding (12/13 -> 0.9231).
        self.assertEqual(
            [{"speaker": "ROSWAAL", "similarity": 0.9231}],
            _uncertain_candidates("ROSWAL", ["ROSWAAL"]),
        )
        # Exact 0.90 boundary (18/20) is included; lower similarity is excluded;
        # candidates are ordered by descending similarity then speaker name.
        candidates = _uncertain_candidates(
            "ABCDEFGHIJ",
            ["ZBCDEFGHIJ", "XBCDEFGHIJ", "ABCDEFGHIJK", "ZZZZZZZZZZ"],
        )
        self.assertEqual(
            [
                {"speaker": "ABCDEFGHIJK", "similarity": 0.9524},
                {"speaker": "XBCDEFGHIJ", "similarity": 0.9},
                {"speaker": "ZBCDEFGHIJ", "similarity": 0.9},
            ],
            candidates,
        )

    def test_group_labels_and_protected_narration_merge_rules(self):
        for label in ("ALICE/BOB", "ALICE & BOB", "ALICE + BOB",
                      "RAM AND REM", "TWINS", "EMILIA (DUAL)", "CROWD"):
            with self.subTest(group_label=label):
                self.assertTrue(is_group_speaker_label(label))
        for label in ("ALICE", "ANDREW", "", None):
            with self.subTest(single_label=label):
                self.assertFalse(is_group_speaker_label(label))

        for protected in ("NARRATOR", "NARRATION", "NARRATIVE", "UNKNOWN"):
            with self.subTest(protected=protected):
                self.assertFalse(is_speaker_merge_allowed(protected, "ALICE"))
                self.assertFalse(is_speaker_merge_allowed("ALICE", protected))

        # Group-to-single is refused on both sides; same-group spelling/NFC/spacing
        # variants are allowed; distinct group labels are refused.
        self.assertFalse(is_speaker_merge_allowed("RAM AND REM", "RAM"))
        self.assertFalse(is_speaker_merge_allowed("RAM", "RAM AND REM"))
        self.assertTrue(is_speaker_merge_allowed("RAM  AND  REM", "ram and rem"))
        self.assertTrue(is_speaker_merge_allowed("JOSÉ & ANA", "JOSE\u0301  &  ANA"))
        self.assertFalse(is_speaker_merge_allowed("RAM AND REM", "EMILIA AND PUCK"))


class AliasGraphAndProposalTests(unittest.TestCase):
    def test_alias_roots_handles_chains_diamonds_cycles_self_aliases_and_refused_edges(self):
        # Linear chain and reversed-order diamond hitting cached roots.
        roots, cyclic = _get_alias_roots({"B": "C", "A": "B", "D": "B"})
        self.assertEqual({"A": "C", "B": "C", "D": "C"}, roots)
        self.assertEqual(set(), cyclic)

        # Self-alias terminates without marking a cycle.
        roots, cyclic = _get_alias_roots({"A": "a"})
        self.assertEqual({"A": "a"}, roots)
        self.assertEqual(set(), cyclic)

        # Two-node cycle and tail leading into it (both orders).
        roots, cyclic = _get_alias_roots({"A": "B", "B": "A", "T": "A"})
        self.assertEqual({}, roots)
        self.assertEqual({"A", "B", "T"}, cyclic)

        # allowed_edge: allowed chain follows through; refused edge stops at terminal
        # (including when `following` exists with different casing than `canonical`).
        roots, cyclic = _get_alias_roots(
            {"A": "B", "B": "C"}, allowed_edge=is_speaker_merge_allowed)
        self.assertEqual({"A": "C", "B": "C"}, roots)
        self.assertEqual(set(), cyclic)

        roots, cyclic = _get_alias_roots(
            {"A": "NARRATOR"}, allowed_edge=is_speaker_merge_allowed)
        self.assertEqual({"A": "NARRATOR"}, roots)
        self.assertEqual(set(), cyclic)

        roots, cyclic = _get_alias_roots(
            {"A": "b", "B": "C"},
            allowed_edge=lambda u, v: (u, v) != ("A", "B"),
        )
        self.assertEqual({"A": "B", "B": "C"}, roots)
        self.assertEqual(set(), cyclic)

    def test_validated_and_saveable_alias_graph_rules(self):
        self.assertEqual({"A": "B", "B": "C"},
                         get_validated_alias_graph({" A ": " B ", "B": "C"}))
        with self.assertRaisesRegex(ValueError, r"A, B"):
            get_validated_alias_graph({"B": "A", "A": "B"})

        self.assertEqual({"A": "B"}, get_saveable_alias_graph({"A": "B"}))
        with self.assertRaises(ValueError):
            get_saveable_alias_graph({"A": "NARRATOR"})
        with self.assertRaises(ValueError):
            get_saveable_alias_graph({"RAM AND REM": "RAM"})

    def test_resolved_speaker_merge_map_flattens_chains_and_skips_cycles_and_protected_edges(self):
        with contextlib.redirect_stdout(io.StringIO()):
            # Cycle before valid chain still resolves the valid chain.
            resolved = get_resolved_speaker_merge_map(
                {"X": "Y", "Y": "X", "A": "B", "B": "C"})
            self.assertEqual({"A": "C", "B": "C"}, resolved)

            # Protected edge halts flattening while preserving the unprotected prefix.
            self.assertEqual(
                {"A": "B"},
                get_resolved_speaker_merge_map({"A": "B", "B": "NARRATOR"}),
            )

            # Self-alias produces no merge entry.
            self.assertEqual({}, get_resolved_speaker_merge_map({"ALICE": "ALICE"}))

    def test_safe_alias_proposals_flatten_conflicts_protection_cycles_and_roster_bounds(self):
        with contextlib.redirect_stdout(io.StringIO()):
            # Default flatten=True vs flatten=False over a human alias chain.
            self.assertEqual(
                {"A": "C"},
                get_safe_alias_proposals(
                    [{"A": "B"}], roster=["C"], existing_aliases={"B": "C"}),
            )
            self.assertEqual(
                {"A": "B"},
                get_safe_alias_proposals(
                    [{"A": "B"}], roster=["C"], existing_aliases={"B": "C"},
                    flatten=False),
            )

            # existing_aliases=None works; human-owned key/root and protected edge
            # are skipped without halting later valid proposals in the same map.
            self.assertEqual(
                {"NEW": "ROSTER"},
                get_safe_alias_proposals(
                    [{"H": "ROSTER", "NARRATOR": "ROSTER", "NEW": "ROSTER"}],
                    roster=["ROSTER"],
                    existing_aliases={"H": "OLD"},
                ),
            )
            self.assertEqual(
                {"NEW": "ROSTER"},
                get_safe_alias_proposals(
                    [{"NARRATOR": "ROSTER", "NEW": "ROSTER"}],
                    roster=["ROSTER"],
                ),
            )

            # Repeated same-target proposal is kept; conflicting target is dropped
            # (and stays conflicted in a 3rd map) without stopping later keys.
            self.assertEqual(
                {"AGREE": "R1", "AFTER": "R2"},
                get_safe_alias_proposals(
                    [{"AGREE": "R1", "CLASH": "R1"},
                     {"agree": "R1", "CLASH": "R2", "AFTER": "R2"},
                     {"CLASH": "R1"}],
                    roster=["R1", "R2"],
                ),
            )

            # Cycles and off-roster targets are skipped without stopping later keys.
            self.assertEqual(
                {"VALID": "R1"},
                get_safe_alias_proposals(
                    [{"C1": "C2", "C2": "C1", "OFF": "NOT_IN_ROSTER", "VALID": "R1"}],
                    roster=["R1"],
                ),
            )

            # Self-alias onto roster spelling is omitted, and a legacy protected
            # human edge cannot be laundered through an automatic proposal.
            self.assertEqual(
                {},
                get_safe_alias_proposals([{"ALICE": "alice"}], roster=["ALICE"]),
            )
            self.assertEqual(
                {},
                get_safe_alias_proposals(
                    [{"A": "B"}],
                    roster=["NARRATOR"],
                    existing_aliases={"B": "NARRATOR"},
                ),
            )

    def test_safe_alias_proposals_never_replace_a_human_root(self):
        # The terminal root of a human alias is human-owned even when it is not
        # itself a key in the registry, so no automatic edge may shadow it.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                {},
                get_safe_alias_proposals(
                    [{"B": "C"}],
                    roster=["B", "C"],
                    existing_aliases={"A": "B"},
                ),
            )
            # A different variant may still target the same root.
            self.assertEqual(
                {"D": "C"},
                get_safe_alias_proposals(
                    [{"B": "C", "D": "C"}],
                    roster=["B", "C", "D"],
                    existing_aliases={"A": "B"},
                ),
            )


if __name__ == "__main__":
    unittest.main()
