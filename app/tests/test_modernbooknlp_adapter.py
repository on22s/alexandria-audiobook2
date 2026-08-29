import csv

from experiments.modernbooknlp_adapter import adapt


def write_tsv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def test_adapt_aligns_names_and_preserves_fixture_id(tmp_path):
    quotes = tmp_path / "book.quotes"
    entities = tmp_path / "book.entities"
    write_tsv(quotes, [{"quote": "Hello there!", "char_id": "7"}])
    write_tsv(entities, [{"COREF": "7", "text": "Ms. Alice", "prop": "PROP"}])
    fixture = {"entries": [{"id": "Book-00001", "line": "Hello there!",
                             "expected_speaker": "MS ALICE",
                             "quote_type": "Explicit"}]}
    rows, coverage = adapt(str(quotes), str(entities), fixture, "fixture_w3200")
    assert rows == [{"id": "fixture_w3200:Book-00001", "line": "Hello there!",
                     "expected": "MS ALICE", "predicted": "MS ALICE",
                     "raw_predicted": "MS. ALICE", "correct": True,
                     "quote_type": "Explicit",
                     "confidence": None, "narrator": None,
                     "split_alignment": False}]
    assert coverage["matched"] == 1
    assert coverage["unmatched"] == 0


def test_adapt_counts_unmatched_fixture_rows(tmp_path):
    quotes = tmp_path / "book.quotes"
    entities = tmp_path / "book.entities"
    write_tsv(quotes, [{"quote": "Different words", "char_id": "-1"}])
    write_tsv(entities, [{"COREF": "1", "text": "Alice", "prop": "PROP"}])
    fixture = {"entries": [{"id": "Book-1", "line": "Expected quotation",
                             "expected_speaker": "ALICE"}]}
    rows, coverage = adapt(str(quotes), str(entities), fixture, "fixture")
    assert rows == []
    assert coverage["unmatched"] == 1


def test_adapt_accepts_pdnc_character_aliases(tmp_path):
    quotes = tmp_path / "book.quotes"
    entities = tmp_path / "book.entities"
    write_tsv(quotes, [{"quote": "Hello!", "char_id": "7"}])
    write_tsv(entities, [{"COREF": "7", "text": "Marilla", "prop": "PROP"}])
    fixture = {"entries": [{"id": "Book-1", "line": "Hello!",
                             "expected_speaker": "MARILLA CUTHBERT"}]}
    rows, _ = adapt(str(quotes), str(entities), fixture, "fixture",
                    [["Marilla", "Marilla Cuthbert"]])
    assert rows[0]["correct"] is True
    assert rows[0]["predicted"] == "MARILLA CUTHBERT"
    assert rows[0]["raw_predicted"] == "MARILLA"
