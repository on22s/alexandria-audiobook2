"""Known cases for the DraCor play parser, including the ones it must reject.

Every fixture below is a real TEI shape seen in lacydracor (speaker labels
like "Mar.", inline stage directions inside a speech, group speeches), so the
parser is checked against what it will actually meet, not a tidy ideal.
"""
import random

from experiments.dracor_trainset import (language_round_robin, parse_play,
                                         play_rows, speaker_categories)

FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
 <teiHeader>
  <fileDesc>
   <titleStmt><title type="main">Marguerite's Colours</title></titleStmt>
   <publicationStmt><availability><licence target="https://creativecommons.org/publicdomain/zero/1.0/">The Lacy Project waives all rights to the TEI encoding applied to this material, which it believes to be in the public domain.</licence></availability></publicationStmt>
  </fileDesc>
  <profileDesc>
   <langUsage><language ident="en"/></langUsage>
   <particDesc><listPerson>
    <person xml:id="F7"><persName>Madame Thibaut</persName></person>
    <person xml:id="M1"><persName>Duke of Croissy</persName></person>
    <person xml:id="F5"><persName>Marguerite .</persName></person>
    <person xml:id="mult"><persName type="role">[Multiple speakers]</persName><persName type="spkr">All.</persName></person>
    <personGrp xml:id="all"><persName>All</persName></personGrp>
   </listPerson></particDesc>
  </profileDesc>
 </teiHeader>
 <text><body><div type="act">
  <stage>Scene - The Garden of an Inn at Verdun.</stage>
  <sp who="#F7"><speaker>Mad.</speaker><p>Coming, coming!</p></sp>
  <sp who="#M1"><speaker>Duke.</speaker><p><stage>(L. C.)</stage> My dear, make yourself easy.</p></sp>
  <sp who="#F5 #M1"><speaker>Both.</speaker><p>Together we go.</p></sp>
  <sp who="#all"><speaker>All.</speaker><p>Hurrah!</p></sp>
  <sp who="#mult"><speaker>Both.</speaker><p>Huzza!</p></sp>
  <sp who="#nobody"><speaker>Ghost.</speaker><p>Boo.</p></sp>
  <sp who="#F5"><speaker>Mar.</speaker></sp>
  <stage>Enter Marguerite, hanging on his arm.</stage>
  <sp who="#F5"><speaker>Mar.</speaker><l>Mar. And where</l><l>are we now?</l></sp>
 </div></body></text>
</TEI>
"""


def test_parse_play_reads_header_and_roster():
    play = parse_play(FIXTURE)
    assert play["title"] == "Marguerite's Colours"
    assert play["language"] == "en"
    assert play["licence_ok"] is True
    # Trailing punctuation is dropped from names; a bracketed "[Multiple
    # speakers]" pseudo-person is a group, not a character.
    assert play["roster"] == {"F7": "MADAME THIBAUT", "M1": "DUKE OF CROISSY",
                              "F5": "MARGUERITE"}
    assert play["groups"] == {"all", "mult"}


def test_parse_play_keeps_text_units_in_order_and_rejects_the_bad_ones():
    play = parse_play(FIXTURE)
    kinds = [(u["type"], u.get("who")) for u in play["units"]]
    # Rejected speeches with text stay as context (they are real lines of the
    # play); only the empty one disappears entirely.
    assert kinds == [("NARRATOR", None), ("SPOKEN", "F7"), ("SPOKEN", "M1"),
                     ("SPOKEN", None), ("SPOKEN", None), ("SPOKEN", None),
                     ("SPOKEN", None), ("NARRATOR", None), ("SPOKEN", "F5")]
    assert play["rejected"] == {"multi_speaker": 1, "group": 2,
                                "unknown_speaker": 1, "empty": 1}
    texts = [u["text"] for u in play["units"]]
    assert "Duke." not in texts[2] and "(L. C.)" not in texts[2]
    assert texts[2] == "My dear, make yourself easy."
    # coyne-whatwilltheysay repeats the speaker label inside <p>; it is
    # stripped when it matches the speech's own <speaker>.
    assert texts[-1] == "And where are we now?"


def test_play_rows_carry_canonical_names_and_label_free_context():
    play = parse_play(FIXTURE)
    rows = play_rows(play, "lacy", "archer-marguerites", context_chars=400)
    assert [r["teacher"] for r in rows] == ["MADAME THIBAUT", "DUKE OF CROISSY",
                                            "MARGUERITE"]
    first = rows[0]
    assert first["line"] == "Coming, coming!"
    assert first["roster"] == ["DUKE OF CROISSY", "MADAME THIBAUT", "MARGUERITE"]
    assert first["context"][0] == {"type": "NARRATOR",
                                   "text": "Scene - The Garden of an Inn at Verdun."}
    assert first["context"][1] == {"type": "SPOKEN", "text": "Coming, coming!",
                                   "target": True}
    assert first["context"][2] == {"type": "SPOKEN",
                                   "text": "My dear, make yourself easy."}
    assert first["book"] == "dracor_lacy_archer-marguerites"
    assert first["language"] == "en"
    assert first["quote_structure"] == "continuous"
    last = rows[-1]
    assert last["context"][0] == {"type": "NARRATOR",
                                  "text": "Enter Marguerite, hanging on his arm."}
    assert last["context"][2] == {"type": "NARRATOR", "text": ""}
    # No speaker label of any form leaks into the context the model sees.
    for row in rows:
        for part in row["context"]:
            for label in ("Mad.", "Duke.", "Mar.", "Both.", "All."):
                assert label not in part["text"]


def test_play_rows_truncate_context_to_the_window():
    play = parse_play(FIXTURE)
    rows = play_rows(play, "lacy", "x", context_chars=8)
    assert rows[0]["context"][0]["text"] == "Verdun."
    assert rows[0]["context"][2]["text"] == "My dear,"


def test_speaker_categories_split_by_share_of_speeches():
    units = ([{"type": "SPOKEN", "who": "A"}] * 10 +
             [{"type": "SPOKEN", "who": "B"}] * 5 +
             [{"type": "SPOKEN", "who": "C"}] * 1 +
             [{"type": "NARRATOR"}] * 3)
    assert speaker_categories(units) == {"A": "major", "B": "intermediate",
                                         "C": "minor"}


def test_parse_play_flags_a_non_free_licence():
    text = FIXTURE.replace(
        'target="https://creativecommons.org/publicdomain/zero/1.0/"',
        'target="https://creativecommons.org/licenses/by-nc/3.0/"')
    assert parse_play(text)["licence_ok"] is False
    unstated = FIXTURE.replace("<availability>", "<availability><!--")\
                      .replace("</availability>", "--></availability>")
    assert parse_play(unstated)["licence_ok"] is None
    # GerDraCor: CC0 for the encoding beside CC BY 3.0 for the source text is
    # fine; the same pair with a BY-NC layer is not.
    layered = FIXTURE.replace("</availability>",
        '<licence target="http://creativecommons.org/licenses/by/3.0/de/legalcode"/></availability>')
    assert parse_play(layered)["licence_ok"] is True
    assert len(parse_play(layered)["licences"]) == 2
    tainted = FIXTURE.replace("</availability>",
        '<licence target="http://creativecommons.org/licenses/by-nc/3.0/de/"/></availability>')
    assert parse_play(tainted)["licence_ok"] is False


def test_language_round_robin_gives_equal_shares_until_a_language_runs_out():
    rows = {"en": [{"n": i} for i in range(10)],
            "de": [{"n": i} for i in range(10)],
            "ru": [{"n": i} for i in range(2)]}
    chosen = language_round_robin(rows, 12, random.Random(3))
    counts = {}
    for row in chosen:
        counts[row["language"]] = counts.get(row["language"], 0) + 1
    assert counts == {"en": 5, "de": 5, "ru": 2}
    assert len(chosen) == 12
