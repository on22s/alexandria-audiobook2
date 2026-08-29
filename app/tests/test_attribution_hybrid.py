import json

import pytest

from experiments.attribution_hybrid import (
    choose_prediction, evaluate, load_rows, select_policy, summarise,
)


def row(row_id, expected, predicted, **extra):
    return {"id": row_id, "expected": expected, "predicted": predicted, **extra}


def test_evaluate_reports_missing_rows_instead_of_dropping_silently():
    rows, coverage = evaluate(
        [row("a", "ALICE", "ALICE"), row("b", "BOB", "BOB")],
        [row("a", "ALICE", "BOB", confidence=.9)], {"kind": "qwen"})
    assert len(rows) == 2
    assert rows[1]["predicted"] == "BOB"
    assert coverage == {"qwen_rows": 2, "modern_rows": 1, "shared_rows": 1,
                        "missing_from_qwen": 0, "missing_from_modern": 1}


def test_evaluate_rejects_duplicate_ids_and_gold_disagreement():
    with pytest.raises(ValueError, match="duplicate"):
        evaluate([row("a", "A", "A"), row("a", "A", "A")],
                 [row("a", "A", "A")], {"kind": "qwen"})
    with pytest.raises(ValueError, match="gold disagreement"):
        evaluate([row("a", "A", "A")], [row("a", "B", "B")],
                 {"kind": "qwen"})


def test_confidence_policy_falls_back_on_abstention_or_low_confidence():
    policy = {"kind": "modern_confidence", "threshold": .7}
    assert choose_prediction(row("a", "A", "Q"), row("a", "A", None,
        confidence=.99), policy) == ("Q", "qwen")
    assert choose_prediction(row("a", "A", "Q"), row("a", "A", "M",
        confidence=.69), policy) == ("Q", "qwen")
    assert choose_prediction(row("a", "A", "Q"), row("a", "A", "M",
        confidence=.7), policy) == ("M", "modern")


def test_select_policy_uses_pilot_labels_and_returns_reusable_policy():
    qwen = [row("a", "A", "A"), row("b", "B", "A"), row("c", "C", "A")]
    modern = [row("a", "A", "B", confidence=.1, quote_type="Explicit"),
              row("b", "B", "B", confidence=.9, quote_type="Implicit"),
              row("c", "C", "C", confidence=.9, quote_type="Anaphoric")]
    policy, rows, _ = select_policy(qwen, modern)
    assert summarise(rows)["hybrid_accuracy"] == 1.0
    applied, _ = evaluate(qwen, modern, policy)
    assert summarise(applied)["hybrid_accuracy"] == 1.0


def test_cli_policy_file_shape_is_json_serializable():
    policy, _, _ = select_policy([row("a", "A", "A")],
                                 [row("a", "A", "A", confidence=.5)])
    assert json.loads(json.dumps({"selected_policy": policy}))["selected_policy"]


def test_preserves_source_alias_aware_correctness():
    qwen = [row("a", "RACHEL LYNDE", "MRS. LYNDE", correct=True)]
    modern = [row("a", "RACHEL LYNDE", "RACHEL LYNDE", correct=True)]
    rows, _ = evaluate(qwen, modern, {"kind": "qwen"})
    assert rows[0]["qwen_correct"] is True
    assert rows[0]["correct"] is True


def test_narrator_aware_policy_protects_known_first_person_books():
    policy = {"kind": "narrator_aware"}
    qwen = row("a", "JAKE", "JAKE")
    modern = row("a", "JAKE", "BRETT", narrator="JAKE")
    assert choose_prediction(qwen, modern, policy) == ("JAKE", "qwen")
    modern["narrator"] = None
    assert choose_prediction(qwen, modern, policy) == ("BRETT", "modern")


def test_load_rows_supports_pdnc_eval_book_mapping(tmp_path):
    path = tmp_path / "pdnc.json"
    path.write_text(json.dumps({"Book": {"base": {"rows": [
        {"id": "Book-00001", "expected": "A", "predicted": "A",
         "correct": True}]}}}), encoding="utf-8")
    assert load_rows(path) == [{"id": "Book:Book-00001", "expected": "A",
                                "predicted": "A", "correct": True}]
