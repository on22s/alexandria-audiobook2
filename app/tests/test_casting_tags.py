"""Casting library tags (#1532): keyword rules over persona descriptions, checked by hand.

Each expected value was read off the description by hand. The rejection cases are
the substring traps a careless matcher falls into ("female" contains "male",
"manly" contains "man", "her" inside "other"), so the rules must not regress to
substring matching.
"""
import unittest

from casting_tags import TEXTURE_VOCAB, TONE_VOCAB, get_age_tags, get_gender_tag, get_voice_tags

ABEL = ("ABEL CARNEDAS should sound like: Young male; Bright, animated, cheerful delivery shifting quickly to "
        "flustered realization; Young male voice; Calm and slightly dry tone; Perplexed when dealing with reckless "
        "teammates; Enthusiastic; Academic; Encouraging; Pragmatic.")


class CastingTagTests(unittest.TestCase):
    def test_real_shaped_persona_description(self):
        tags = get_voice_tags(ABEL)
        self.assertEqual('Male', tags['gender'])
        self.assertEqual(['Young Adult'], tags['ages'])
        self.assertEqual(['cheerful', 'calm', 'academic'], tags['tones'])  # in the order they appear
        self.assertEqual([], tags['textures'])

    def test_gender_rules_and_their_traps(self):
        cases = {
            'An elderly woman with a warm, measured voice.': 'Female',
            'A gruff old man.': 'Male',
            'Androgynous, light and airy.': 'Ambiguous / mixed',
            'He speaks gently to his sister.': 'Ambiguous / mixed',  # both pronoun sets present
            'Neither male nor female; a hollow echo.': 'Ambiguous / mixed',
            'A female knight.': 'Female',             # "female" must not also count as "male"
            'A manly, booming bellow.': 'Not specified',  # "manly" is not "man"
            'Some other voice, crisp and clear.': 'Not specified',  # "other" holds "her"
            '': 'Not specified',
        }
        for description, expected in cases.items():
            with self.subTest(description=description):
                self.assertEqual(expected, get_gender_tag(description))

    def test_age_rules_keep_order_and_allow_several(self):
        cases = {
            'A young adult woman in her twenties.': ['Young Adult', 'Adult'],  # as the concept shows (A'DAL)
            'A middle-aged father, mature and steady.': ['Adult'],  # the concept also said Older / Elderly
            'An aged, frail scholar.': ['Older / Elderly'],  # "aged" on its own still counts
            'An ancient, ageless deity.': ['Ancient / Ageless'],
            'A little girl, childlike and bright.': ['Child'],
            'Teenaged boy, mature beyond his years.': ['Teen', 'Adult'],
            'An old woman, grey-haired and frail.': ['Older / Elderly'],
            'A crisp, clear narrator.': ['Not specified'],
            'Kidnapper with a cold voice.': ['Not specified'],  # "kid" only as a whole word
        }
        for description, expected in cases.items():
            with self.subTest(description=description):
                self.assertEqual(expected, get_age_tags(description))

    def test_tone_and_texture_match_whole_terms_including_hyphenated_ones(self):
        tags = get_voice_tags('Soft-spoken and matter-of-fact; a low-pitched, gravelly voice. Unsettling calm.')
        self.assertEqual(['soft-spoken', 'matter-of-fact', 'unsettling', 'calm'], tags['tones'])
        self.assertEqual(['low-pitched', 'gravelly'], tags['textures'])
        # Known-bad controls: terms inside longer words do not count.
        tags = get_voice_tags('Uncalmed, sharpened, warmly, coldness.')
        self.assertEqual([], tags['tones'])
        self.assertEqual([], tags['textures'])

    def test_vocabularies_match_the_concept_sizes(self):
        self.assertEqual(188, len(TONE_VOCAB))
        self.assertEqual(61, len(TEXTURE_VOCAB))
        self.assertEqual(len(TONE_VOCAB), len(set(TONE_VOCAB)))
        self.assertEqual(len(TEXTURE_VOCAB), len(set(TEXTURE_VOCAB)))


if __name__ == '__main__':
    unittest.main()
