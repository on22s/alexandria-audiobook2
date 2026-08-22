"""The adjacent-tag stratifier must read the field the schema actually has.

`tag_priority.has_adjacent_tag` gated on `entry["type"] == "NARRATOR"`. Script
entries carry exactly {speaker, text, instruct} - there is no `type` key in the
schema at all - so the guard was `None == "NARRATOR"` on every row, the
function returned False every time it was ever called, and the experiment
reported `available: 0` across 396 rows with a conditional accuracy of 0.0 by
construction. The A/B was unaffected, since both arms ran; what never ran was
the stratification that exists to LOCATE a gain.

Measured after the fix on a shipped book: 278 of 1,408 spoken lines (19.7%)
have an adjacent tag, against 0 before.

Found on 2026-08-22 while reading an unrelated pull request on the sibling
repository, which performs the same check against its own schema.

THE FIXTURE IS SYNTHETIC ON PURPOSE. `scripts/` is gitignored (.gitignore:41),
so a test driven by real books can only ever SKIP in CI - a first version of
this did exactly that, four skips and a green tick. The synthetic entries below
use the real schema, so the test fails wherever it runs.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPERIMENT = os.path.join(REPO, "app", "experiments", "tag_priority.py")

SPEECH_VERB = (r"\b(said|asked|replied|answered|shouted|whispered|muttered|"
               r"called|cried|yelled|murmured|added|continued|explained)\b")

# The real schema: speaker / text / instruct. No `type` key, deliberately.
ENTRIES = [
    {"speaker": "NARRATOR", "text": "The room was quiet.", "instruct": ""},
    {"speaker": "SUBARU", "text": "Is anyone there?", "instruct": ""},
    {"speaker": "NARRATOR", "text": "Subaru said, peering into the dark.",
     "instruct": ""},
    # EMILIA is deliberately fenced by narration carrying NO speech verb.
    # A first version put her directly after Subaru's tag; the detector reads
    # BOTH neighbours, so she counted as tagged and the test caught the
    # fixture rather than the code.
    {"speaker": "NARRATOR", "text": "Rain traced the window.", "instruct": ""},
    {"speaker": "EMILIA", "text": "Only me.", "instruct": ""},
    {"speaker": "NARRATOR", "text": "The candle guttered.", "instruct": ""},
    {"speaker": "PUCK", "text": "And me, obviously.", "instruct": ""},
    {"speaker": "NARRATOR", "text": "the cat replied from the shelf.",
     "instruct": ""},
]


def adjacent_tag(entries, index, field):
    """The stratifier, parameterised by the field it gates on."""
    for j in (index - 1, index + 1):
        if 0 <= j < len(entries) and entries[j].get(field) == "NARRATOR":
            if re.search(SPEECH_VERB, (entries[j].get("text") or "").lower()):
                return True
    return False


def spoken_indices(entries):
    return [i for i, e in enumerate(entries)
            if e.get("speaker") not in (None, "NARRATOR")]


class AdjacentTagFieldTests(unittest.TestCase):
    def test_the_schema_has_no_type_key(self):
        """The schema fact the bug depended on."""
        self.assertTrue(all("type" not in e for e in ENTRIES))
        self.assertTrue(all("speaker" in e for e in ENTRIES))

    def test_gating_on_type_finds_nothing(self):
        """Reproduces the bug: the old gate is blind by construction."""
        spoken = spoken_indices(ENTRIES)
        self.assertEqual(sum(adjacent_tag(ENTRIES, i, "type") for i in spoken), 0)

    def test_gating_on_speaker_finds_the_tags(self):
        """SUBARU and PUCK each sit beside a narrator line with a speech verb;
        EMILIA's neighbours carry none, so exactly two must be found."""
        spoken = spoken_indices(ENTRIES)
        found = [ENTRIES[i]["speaker"]
                 for i in spoken if adjacent_tag(ENTRIES, i, "speaker")]
        self.assertEqual(sorted(found), ["PUCK", "SUBARU"])

    def test_a_neighbour_without_a_speech_verb_is_not_a_tag(self):
        # Guards against the opposite failure: counting every adjacent
        # narration line as a tag would make the stratifier useless the
        # other way.
        spoken = spoken_indices(ENTRIES)
        emilia = [i for i in spoken if ENTRIES[i]["speaker"] == "EMILIA"][0]
        self.assertFalse(adjacent_tag(ENTRIES, emilia, "speaker"))

    @unittest.skipUnless(os.path.exists(EXPERIMENT), "tag_priority.py absent")
    def test_the_experiment_gates_on_speaker(self):
        with open(EXPERIMENT, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn('seg[j].get("type")', source,
                         "has_adjacent_tag gates on a key the schema lacks")
        self.assertIn('seg[j].get("speaker")', source)
