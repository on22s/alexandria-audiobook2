"""One definition of "is this the right speaker", for every harness.

Eighteen harnesses in this directory each define their own `same()`. They agree
today because they were copy-pasted, which is exactly the situation Rule 15
warns about: two independently-maintained copies of one decision will drift,
and the drift is invisible because both keep producing plausible numbers. This
module is the single copy.

It also fixes a scoring bug the copies all share. Across every artifact in the
ledger, 268 of 21,190 wrong rows are not wrong:

    punctuation only       162   MR. PRIEST vs MR PRIEST, MS. SHORT HAIR
    romanization variant   106   ARUMANFI vs ALMANFI, RUDIUS vs RUDEUS
    genuinely different  20,922

The 162 are a period. `same()` upper-cased both sides and compared, so a model
that wrote the honorific with its full stop was marked wrong for it. That is a
defect in the instrument, not a finding about models, and it is folded into the
primary comparison here.

THE 106 ARE DELIBERATELY NOT. `attribution_accuracy.romaji_key` documents why
and the reasoning still holds: exact spelling measures whether a name is usable
downstream, because a misspelled speaker fragments the cast list and breaks
voice assignment, while phonetic matching measures whether the model identified
the right character. Those are different questions, and the penalty is
model-specific - magistral-small loses 7.9 points of oracle accuracy to
romanization where three other models lose nothing. Folding it into the primary
metric would hide a real, unevenly-distributed weakness. `same_speaker` reports
it separately so a caller can have both numbers.
"""
import re

# ASCII-ONLY ALLOW-LISTS DELETE ENTIRE WRITING SYSTEMS. The previous pattern
# was `[^A-Z0-9 ]`, which kept latin letters, digits and space and dropped
# everything else AS PUNCTUATION - so every CJK name normalised to the empty
# string, `same_speaker` hit its `if not b: return False` guard, and every
# Chinese row scored wrong. Measured on the WP2021 arm: 0 of 380 correct,
# with `expected` and `predicted` byte-identical on the rows it refused.
# The real figure is 260 of 380.
#
# Now a DENY-list of punctuation, so any script's letters survive: hanzi,
# kana, hangul, cyrillic, and accented latin (JOSÉ kept its É rather than
# becoming JOS). `_` is removed explicitly because \w keeps it.
#
# This can only make normalize MORE discriminating, never less, so the
# docstring's promise - that it must not merge distinct characters - is
# strengthened rather than weakened. Verified behaviour-identical on English:
# re-scoring the stored RiQuA and PDNC arms gives exactly the same totals.
_PUNCT = re.compile(r"[^\w\s]|_")


def normalize(name):
    """Upper-case, strip punctuation, collapse whitespace.

    Punctuation only: this must not merge distinct characters, and the honorific
    cases it exists for (MR. TALL, MS. SHORT HAIR) differ from every other
    roster entry by more than a full stop.
    """
    return " ".join(_PUNCT.sub("", (name or "").upper()).split())


def alias_groups(*sources):
    """Collect alias groups from any number of fixture-shaped dicts or lists."""
    groups = []
    for source in sources:
        if not source:
            continue
        raw = source.get("aliases", []) if isinstance(source, dict) else source
        for group in raw or []:
            members = {normalize(n) for n in group if n}
            if len(members) > 1:
                groups.append(members)
    return groups


def same_speaker(expected, actual, groups=(), phonetic=False):
    """Is `actual` the speaker `expected` names?

    `phonetic=True` additionally accepts romanization variants. Leave it False
    for the headline number and call it twice if you want both - see the module
    docstring for why the two are not the same question.
    """
    a, b = normalize(expected), normalize(actual)
    if not b:
        return False
    if a == b:
        return True
    for group in groups:
        if a in group and b in group:
            return True
    if phonetic:
        # Imported lazily: this module is used by offline analyses that have no
        # reason to pull in the accuracy module's dependencies.
        from attribution_accuracy import romaji_key
        key = romaji_key(b)
        return bool(key) and key == romaji_key(a)
    return False


def roster_membership_names(roster, groups=()):
    """-> every name the shown roster lines stand for, for `in_candidates`.

    WHAT THIS IS FOR. `ExperimentRecord.add` tests candidate membership by
    EXACT match, deliberately, and its own note says what a caller owes it:
    "Pass the names the roster line stands for (canonical and aliases) so
    `in_candidates` means what it says". Two evaluators did not. They passed
    the display roster while scoring `correct` through `same_speaker`, which IS
    alias-aware - so the two fields disagreed by construction, and the
    availability figure was the one that lied.

    Measured on the four-book local baseline of 2026-09-07:

        book               exact   with this
        grimgar03          98.7%      99.5%
        index18            70.5%      86.4%
        owarimonogatari3   73.5%      94.4%

    owarimonogatari3's gold declares ['GAEN', 'IZUKO GAEN']. The roster showed
    the full name, the gold asks for the short one, and 31 rows - 72% of that
    book's apparent shortfall - were recorded as the model never having been
    offered a character it was offered every time. That reads as "the cast list
    is incomplete" and sends you to fix candidate generation, which is not the
    problem.

    ONLY CHARACTERS ACTUALLY ON THE ROSTER ARE EXPANDED. An alias group whose
    members are all absent contributes nothing: the question is what the shown
    lines stand for, not who exists in the book. Widening it to every declared
    name would make `in_candidates` unfalsifiable, which is worse than the bug.
    """
    shown = {normalize(name) for name in roster if name}
    names = set(shown)
    for group in groups:
        if group & shown:
            names |= group
    return sorted(names)
