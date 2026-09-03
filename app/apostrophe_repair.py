"""Restore apostrophes that a source lost to spaces.

WHY THIS EXISTS. 9 of PDNC's 28 novels ship a novel_text.txt with ZERO
apostrophes of either kind - the character was replaced by a SPACE, so the text
reads "don t", "Miller s", "I m". Measured 2026-09-03; the same novels'
quotation_info.csv still has its apostrophes, so the loss is upstream in PDNC's
plain-text extraction, not in anything this repo does.

WHY IT MATTERS MORE THAN IT LOOKS. generate_script cannot pass its quality gate
on such a book EVER: the model is told to preserve the author's text, writes
"don't" where the source says "don t", and validate_chunk_quality scores the
correction as missing content. DaisyMiller burned 129 rejections, three
adaptive splits and a permanent chunk failure before the run gave up. Every
retry reproduces the same "error", so the loop is unwinnable by construction.

CONSERVATIVE BY DESIGN. The transform runs only on a document that shows the
exact damage signature - no apostrophe anywhere AND at least one broken
contraction. A healthy book is returned untouched, so this cannot corrupt the
19 novels that are fine or any normal user input.
"""
import re

# Stems that take "'t". `X t` -> `X't` gives don't, isn't, can't, won't.
_STEMS = ("don", "isn", "didn", "doesn", "wasn", "weren", "hasn", "haven",
          "hadn", "couldn", "wouldn", "shouldn", "mustn", "needn", "aren",
          "can", "won", "ain", "shan", "mightn", "daren", "oughtn", "mayn")
_CONTRACTION_RE = re.compile(r"\b(%s) t\b" % "|".join(_STEMS), re.IGNORECASE)

# Clitics that follow a word: he s, I m, we re, they ve, you ll, I d.
#
# THE HYPHEN LOOKAHEAD IS LOAD-BEARING. Without it "once re-entered" matches as
# "once" + "re", because \b is satisfied by the hyphen, and the repair emits
# "once're-entered". Measured on 18 novels with ground truth, that single case
# was two thirds of all false insertions - more than every other cause put
# together. The trailing (?![-\w]) refuses any clitic that is really the head of
# a hyphenated word.
_CLITIC_RE = re.compile(r"\b([A-Za-z]+) (s|re|ve|ll|m|d)(?![-\w])")

_BROKEN_RE = re.compile(r"\b(?:%s) t\b" % "|".join(_STEMS), re.IGNORECASE)


def looks_damaged(text):
    """-> True only for the upstream signature, never for healthy text.

    PROPORTION, NOT PRESENCE. An earlier version returned False the moment it
    saw a single apostrophe, and that excluded the WORST-damaged novel in the
    corpus: HowardsEnd carries exactly one apostrophe - inside a quoted verse -
    against 763 broken contractions. A healthy book has thousands of
    apostrophes and no broken contractions, so the two populations are orders
    of magnitude apart and a ratio separates them with room to spare.
    """
    if not text:
        return False
    broken = len(_BROKEN_RE.findall(text))
    if not broken:
        return False
    # NO MINIMUM COUNT. An earlier floor of five excluded short inputs that are
    # genuinely damaged - a pasted excerpt with two "don t" needs the repair as
    # much as a whole novel does. The RATIO below is what keeps healthy text
    # safe: a book with 2,374 apostrophes and one stray "don t" fails it, while
    # a damaged one has hundreds of breaks against at most a handful.
    apostrophes = text.count("'") + text.count("\u2019")
    return apostrophes <= max(2, broken * 0.05)


def restore_stripped_apostrophes(text):
    """Return (repaired, changes). Healthy text is returned unchanged."""
    if not looks_damaged(text):
        return text, []
    changes = []

    def note(match, after):
        offset = match.start()
        line = text.count("\n", 0, offset) + 1
        changes.append({"offset": offset, "line": line,
                        "before": match.group(0), "after": after,
                        "rule": "stripped_apostrophe"})
        return after

    repaired = _CONTRACTION_RE.sub(
        lambda m: note(m, "%s't" % m.group(1)), text)
    repaired = _CLITIC_RE.sub(
        lambda m: "%s'%s" % (m.group(1), m.group(2)), repaired)
    return repaired, changes
