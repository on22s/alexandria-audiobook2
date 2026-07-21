# Three-Pass Script Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a side-by-side `three_pass` script-generation mode that splits the single monolithic LLM call into segment (fidelity-critical) / attribute (naming) / instruct (creative) passes, quarantining all text-fidelity risk in pass 1.

**Architecture:** New `app/three_pass_generate.py` orchestrator with a CLI mirroring `generate_script.py`. Pass 1 chunks the source and reuses the existing trigram gate + near-miss + adaptive split. Passes 2 & 3 batch entries and enforce a hard per-entry text freeze so they can only add fields. Single-pass path is untouched.

**Tech Stack:** Python 3.10, stdlib `unittest`, OpenAI-compatible LM Studio client. All commands run from `app/` using `env/bin/python`.

**Spec:** `docs/superpowers/specs/2026-07-21-three-pass-script-generation-design.md`

---

## File Structure

- **Create `app/recall_core.py`** — the token/n-gram/recall math extracted from `chunk_quality.py` so segment + single-pass share identical fidelity scoring.
- **Modify `app/chunk_quality.py`** — import the recall math from `recall_core` instead of defining it privately.
- **Create `app/pass_quality.py`** — the three pass validators + the hard freeze check.
- **Create `app/default_prompts_segment.txt`, `_attribute.txt`, `_instruct.txt`** — the three system/user prompt pairs.
- **Modify `app/default_prompts.py`** — add loaders for the three new prompt files.
- **Create `app/three_pass_generate.py`** — orchestrator + CLI.
- **Create `app/test_pass_quality.py`, `app/test_three_pass_generate.py`** — tests.
- **Modify `app/unit_test_inventory.json`** — regenerated.

Reused as-is (imported, not modified): `generate_script.call_llm_for_entries`, `generate_script.split_into_chunks`, `generate_script.LLMGenParams`, `generate_script.fix_mojibake`, `source_normalization.*`, `speaker_identity.stabilize_speaker_identities`, `review_script.normalize_text`.

---

## Task 1: Extract the recall/trigram core into a shared module

**Files:**
- Create: `app/recall_core.py`
- Modify: `app/chunk_quality.py` (lines 3-6 imports, 27-29 regex consts, 144-193 functions)
- Test: `app/test_chunk_quality.py` (existing suite is the regression guard)

- [ ] **Step 1: Create `app/recall_core.py` with the exact functions currently private in `chunk_quality.py`**

```python
"""Token / n-gram / multiset-recall math shared by the single-pass quality gate
(chunk_quality) and the three-pass segment gate (pass_quality). Kept in one
module so both score source-text fidelity identically (no duplicated decision
logic)."""

import re
import unicodedata
from collections import Counter

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_CHARACTER_TOKEN_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "THAI")


def tokens(text):
    normalized = unicodedata.normalize("NFC", str(text or "")).casefold()
    out = []
    for word in _TOKEN_RE.findall(normalized):
        if any(unicodedata.name(char, "").startswith(_CHARACTER_TOKEN_SCRIPTS)
               for char in word):
            out.extend(char for char in word if char.isalnum())
        else:
            out.append(word)
    return out


def ngrams(token_list, size):
    return list(zip(*(token_list[offset:] for offset in range(size))))


def counter_recall(source_items, output_items):
    if not source_items:
        return 1.0
    source = Counter(source_items)
    output = Counter(output_items)
    return sum(min(count, output.get(item, 0))
               for item, count in source.items()) / sum(source.values())
```

- [ ] **Step 2: Point `chunk_quality.py` at the shared module**

In `app/chunk_quality.py`, delete the private `_TOKEN_RE` (line 27), `_CHARACTER_TOKEN_SCRIPTS` (line 29), and the `_tokens` (144), `_ngrams` (156), `_counter_recall` (184) function bodies. Add at the top imports:

```python
from recall_core import tokens as _tokens, ngrams as _ngrams, counter_recall as _counter_recall
```

Leave every call site (`_tokens(...)`, `_ngrams(...)`, `_counter_recall(...)`) unchanged — the import aliases preserve the names. Keep `_CYRILLIC_RE` (line 28) in `chunk_quality.py`; it is not recall math and is used only there.

- [ ] **Step 3: Run the existing suite to prove scoring is unchanged**

Run: `cd app && env/bin/python -m unittest test_chunk_quality -v 2>&1 | tail -3`
Expected: `OK` (all existing tests pass — the extraction changed no behavior).

- [ ] **Step 4: Commit**

```bash
cd app && git add recall_core.py chunk_quality.py
git commit -m "Extract recall/trigram core into shared recall_core module"
```

---

## Task 2: Segment validator (`validate_segment_quality`)

Pass 1 emits `[{type, text}]`. Its gate reuses the recall math but checks the
`{type, text}` shape instead of `{speaker, text, instruct}`.

**Files:**
- Create: `app/pass_quality.py`
- Test: `app/test_pass_quality.py`

- [ ] **Step 1: Write the failing test**

```python
import unittest
from pass_quality import validate_segment_quality


def _seg(text, type_="NARRATOR"):
    return {"type": type_, "text": text}


class SegmentQualityTests(unittest.TestCase):
    def test_complete_segment_passes(self):
        source = " ".join(f"word{i}" for i in range(50))
        report = validate_segment_quality(source, [_seg(source)])
        self.assertTrue(report["passed"], report["findings"])

    def test_missing_type_field_fails(self):
        report = validate_segment_quality("A line.", [{"text": "A line."}])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("missing_fields", codes)

    def test_invalid_type_value_fails(self):
        report = validate_segment_quality("A line.", [{"type": "MARCUS", "text": "A line."}])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("invalid_type", codes)

    def test_severe_truncation_fails_recall(self):
        source = " ".join(f"word{i}" for i in range(100))
        report = validate_segment_quality(source, [_seg("word0 word1")])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("low_source_token_recall", codes)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && env/bin/python -m unittest test_pass_quality -v 2>&1 | tail -5`
Expected: FAIL — `ModuleNotFoundError: No module named 'pass_quality'`.

- [ ] **Step 3: Write `validate_segment_quality` in `app/pass_quality.py`**

```python
"""Validators for the three-pass generation flow (segment / attribute /
instruct). Segment reuses recall_core for source-fidelity scoring; attribute and
instruct enforce a hard per-entry text freeze (they may only add fields)."""

from recall_core import tokens, ngrams, counter_recall

MIN_SOURCE_TOKEN_RECALL = 0.90
MIN_ORDERED_TRIGRAM_RECALL = 0.90
MIN_OUTPUT_SOURCE_RATIO = 0.90
MAX_OUTPUT_SOURCE_RATIO = 1.10
_VALID_SEGMENT_TYPES = {"NARRATOR", "SPOKEN"}


def validate_segment_quality(source_text, entries):
    """Fidelity gate for pass 1 output [{type, text}]. Same recall/trigram math
    as the single-pass gate, but validates the segment shape (type in
    {NARRATOR, SPOKEN}) rather than speaker/instruct."""
    findings = []
    if not isinstance(entries, list) or not entries:
        return _report(0, 0, 0.0, 0.0, 0.0,
                       [{"code": "missing_entries", "message": "No entries."}])
    output_parts = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            findings.append({"code": "invalid_entry", "entry_number": number})
            continue
        missing = [k for k in ("type", "text") if k not in entry]
        if missing:
            findings.append({"code": "missing_fields", "entry_number": number,
                             "fields": missing})
        if entry.get("type") not in _VALID_SEGMENT_TYPES:
            findings.append({"code": "invalid_type", "entry_number": number,
                             "value": entry.get("type")})
        text = str(entry.get("text") or "")
        if not text.strip():
            findings.append({"code": "empty_text", "entry_number": number})
        output_parts.append(text)

    source_tokens = tokens(source_text)
    output_tokens = tokens(" ".join(output_parts))
    sc, oc = len(source_tokens), len(output_tokens)
    recall = counter_recall(source_tokens, output_tokens)
    trigram = counter_recall(ngrams(source_tokens, 3), ngrams(output_tokens, 3))
    ratio = oc / sc if sc else (1.0 if not oc else 0.0)
    if sc and recall < MIN_SOURCE_TOKEN_RECALL:
        findings.append({"code": "low_source_token_recall", "value": round(recall, 4)})
    if sc >= 3 and trigram < MIN_ORDERED_TRIGRAM_RECALL:
        findings.append({"code": "low_ordered_trigram_recall", "value": round(trigram, 4)})
    if sc and not MIN_OUTPUT_SOURCE_RATIO <= ratio <= MAX_OUTPUT_SOURCE_RATIO:
        findings.append({"code": "output_source_ratio", "value": round(ratio, 4)})
    return _report(sc, oc, recall, trigram, ratio, findings)


def _report(source_count, output_count, recall, trigram, ratio, findings):
    return {
        "passed": not findings,
        "metrics": {
            "source_tokens": source_count, "output_tokens": output_count,
            "source_token_recall": round(recall, 4),
            "ordered_trigram_recall": round(trigram, 4),
            "output_source_ratio": round(ratio, 4),
        },
        "findings": findings,
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd app && env/bin/python -m unittest test_pass_quality -v 2>&1 | tail -3`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd app && git add pass_quality.py test_pass_quality.py
git commit -m "Add segment quality validator for three-pass pass 1"
```

---

## Task 3: Hard text-freeze + attribution validator

**Files:**
- Modify: `app/pass_quality.py`
- Test: `app/test_pass_quality.py`

- [ ] **Step 1: Write the failing test (append to `test_pass_quality.py`)**

```python
from pass_quality import freeze_check, validate_attribution


class FreezeAndAttributionTests(unittest.TestCase):
    def test_freeze_passes_when_text_identical(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me the truth."}]
        new = [{"speaker": "ELENA", "text": "Tell me the truth."}]
        ok, reason = freeze_check(frozen, new)
        self.assertTrue(ok, reason)

    def test_freeze_fails_when_text_altered(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me the truth."}]
        new = [{"speaker": "ELENA", "text": "Tell me the whole truth."}]
        ok, reason = freeze_check(frozen, new)
        self.assertFalse(ok)

    def test_freeze_fails_on_count_mismatch(self):
        frozen = [{"type": "NARRATOR", "text": "A."}, {"type": "SPOKEN", "text": "B."}]
        new = [{"speaker": "NARRATOR", "text": "A."}]
        ok, reason = freeze_check(frozen, new)
        self.assertFalse(ok)

    def test_freeze_ignores_punctuation_and_case(self):
        # normalize_text collapses case/punctuation, matching review's comparison.
        frozen = [{"type": "SPOKEN", "text": "We should leave."}]
        new = [{"speaker": "MARCUS", "text": "we should leave"}]
        ok, reason = freeze_check(frozen, new)
        self.assertTrue(ok, reason)

    def test_attribution_passes_when_all_spoken_named(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold."},
                  {"type": "SPOKEN", "text": "Tell me."}]
        named = [{"speaker": "NARRATOR", "text": "The room was cold."},
                 {"speaker": "ELENA", "text": "Tell me."}]
        report = validate_attribution(frozen, named)
        self.assertTrue(report["passed"], report["findings"])

    def test_attribution_fails_when_spoken_left_as_narrator(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        named = [{"speaker": "NARRATOR", "text": "Tell me."}]
        report = validate_attribution(frozen, named)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("spoken_not_named", codes)

    def test_attribution_fails_on_text_drift(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        named = [{"speaker": "ELENA", "text": "Tell me now."}]
        report = validate_attribution(frozen, named)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("text_freeze_violated", codes)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && env/bin/python -m unittest test_pass_quality.FreezeAndAttributionTests -v 2>&1 | tail -5`
Expected: FAIL — `ImportError: cannot import name 'freeze_check'`.

- [ ] **Step 3: Add `freeze_check` and `validate_attribution` to `app/pass_quality.py`**

```python
from review_script import normalize_text


def freeze_check(frozen_entries, new_entries):
    """Return (ok, reason). ok iff new_entries has the same count as
    frozen_entries and each new entry's text matches the frozen text under
    normalize_text (case/punctuation/whitespace-insensitive, same comparison
    review uses). new_entries may add fields; it may not change or reorder text."""
    if len(new_entries) != len(frozen_entries):
        return False, f"count {len(new_entries)} != frozen {len(frozen_entries)}"
    for i, (frozen, new) in enumerate(zip(frozen_entries, new_entries), 1):
        if normalize_text(new.get("text", "")) != normalize_text(frozen.get("text", "")):
            return False, f"entry {i} text changed"
    return True, ""


def validate_attribution(frozen_entries, named_entries):
    """Pass 2 gate. Enforces the freeze, then requires every SPOKEN span to have
    a non-empty speaker other than NARRATOR, and every NARRATOR span to stay
    NARRATOR."""
    findings = []
    ok, reason = freeze_check(frozen_entries, named_entries)
    if not ok:
        findings.append({"code": "text_freeze_violated", "message": reason})
        return {"passed": False, "findings": findings}
    for i, (frozen, named) in enumerate(zip(frozen_entries, named_entries), 1):
        speaker = (named.get("speaker") or "").strip()
        if frozen["type"] == "SPOKEN":
            if not speaker or speaker.upper() == "NARRATOR":
                findings.append({"code": "spoken_not_named", "entry_number": i})
        else:  # NARRATOR
            if speaker.upper() != "NARRATOR":
                findings.append({"code": "narrator_renamed", "entry_number": i,
                                 "value": speaker})
    return {"passed": not findings, "findings": findings}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd app && env/bin/python -m unittest test_pass_quality -v 2>&1 | tail -3`
Expected: PASS (all Task 2 + Task 3 tests).

- [ ] **Step 5: Commit**

```bash
cd app && git add pass_quality.py test_pass_quality.py
git commit -m "Add hard text-freeze and attribution validator for three-pass pass 2"
```

---

## Task 4: Instruct validator

**Files:**
- Modify: `app/pass_quality.py`
- Test: `app/test_pass_quality.py`

- [ ] **Step 1: Write the failing test (append to `test_pass_quality.py`)**

```python
from pass_quality import validate_instruct


class InstructValidatorTests(unittest.TestCase):
    def test_passes_when_all_have_instruct(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        annotated = [{"speaker": "ELENA", "text": "Tell me.", "instruct": "Firm, quiet."}]
        report = validate_instruct(prior, annotated)
        self.assertTrue(report["passed"], report["findings"])

    def test_fails_when_instruct_missing_or_empty(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        annotated = [{"speaker": "ELENA", "text": "Tell me.", "instruct": "  "}]
        report = validate_instruct(prior, annotated)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("missing_instruct", codes)

    def test_fails_when_speaker_changed(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        annotated = [{"speaker": "MARCUS", "text": "Tell me.", "instruct": "Firm."}]
        report = validate_instruct(prior, annotated)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("speaker_changed", codes)

    def test_fails_on_text_drift(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        annotated = [{"speaker": "ELENA", "text": "Tell me now.", "instruct": "Firm."}]
        report = validate_instruct(prior, annotated)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("text_freeze_violated", codes)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && env/bin/python -m unittest test_pass_quality.InstructValidatorTests -v 2>&1 | tail -5`
Expected: FAIL — `ImportError: cannot import name 'validate_instruct'`.

- [ ] **Step 3: Add `validate_instruct` to `app/pass_quality.py`**

```python
def validate_instruct(prior_entries, annotated_entries):
    """Pass 3 gate. Enforces the freeze on text AND speaker (pass 3 may only add
    instruct), and requires a non-empty instruct on every entry."""
    findings = []
    ok, reason = freeze_check(prior_entries, annotated_entries)
    if not ok:
        findings.append({"code": "text_freeze_violated", "message": reason})
        return {"passed": False, "findings": findings}
    for i, (prior, ann) in enumerate(zip(prior_entries, annotated_entries), 1):
        if (ann.get("speaker") or "") != (prior.get("speaker") or ""):
            findings.append({"code": "speaker_changed", "entry_number": i})
        if not (ann.get("instruct") or "").strip():
            findings.append({"code": "missing_instruct", "entry_number": i})
    return {"passed": not findings, "findings": findings}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd app && env/bin/python -m unittest test_pass_quality -v 2>&1 | tail -3`
Expected: PASS (all pass_quality tests).

- [ ] **Step 5: Commit**

```bash
cd app && git add pass_quality.py test_pass_quality.py
git commit -m "Add instruct validator for three-pass pass 3"
```

---

## Task 5: The three prompt files + loaders

**Files:**
- Create: `app/default_prompts_segment.txt`, `app/default_prompts_attribute.txt`, `app/default_prompts_instruct.txt`
- Modify: `app/default_prompts.py`
- Test: `app/test_pass_quality.py` (loader smoke test)

- [ ] **Step 1: Create `app/default_prompts_segment.txt`**

Format is `SYSTEM ---SEPARATOR--- USER`, matching `default_prompts.txt`. The `{chunk}` placeholder is filled at call time.

```
You are converting a book into an ordered list of speech units for a TTS system. Output ONLY a valid JSON array — no markdown, no explanations.

Each element is {"type": "...", "text": "..."} where:
- "type": "SPOKEN" if the text is spoken aloud by a character (dialogue). "NARRATOR" for everything else (description, action, thought, scene-setting).
- "text": the exact words, verbatim from the source.

RULES:
1. Do NOT assign character names. Only label SPOKEN vs NARRATOR. Naming happens in a later step.
2. Split mixed paragraphs by tracking quotation marks: inside outer quotes = SPOKEN, outside = NARRATOR. Drop attribution tags ("he said") but keep descriptive actions as NARRATOR.
3. PRESERVE THE AUTHOR'S TEXT exactly — same words, tense, person. Reproduce every word of narration; never summarize or drop content.
4. Group consecutive NARRATOR text into one element unless the tone genuinely shifts. Keep each SPOKEN turn its own element.
5. TTS-normalize: "Chapter I" -> "Chapter One", "Dr." -> "Doctor", "&" -> "and", "3rd" -> "third". No bracket sounds; use real words ("Ah!", "Mmm...").
---SEPARATOR---
Convert this source text to the JSON array of {"type","text"} units. Preserve the author's wording exactly.

SOURCE TEXT:
{chunk}
```

- [ ] **Step 2: Create `app/default_prompts_attribute.txt`**

`{roster}` = established speaker names seen so far; `{batch}` = the JSON array of `{type, text}` entries to name.

```
You assign speaker names to already-segmented script entries for a TTS system. Output ONLY a valid JSON array — no markdown, no explanations.

You receive entries as {"type": "NARRATOR"|"SPOKEN", "text": "..."}. Return the SAME entries in the SAME order, each as {"speaker": "...", "text": "..."} where:
- NARRATOR entries: "speaker" is exactly "NARRATOR".
- SPOKEN entries: "speaker" is the UPPERCASE name of whoever says the line, inferred from context and the established roster.

RULES:
1. Do NOT change, add, remove, reorder, or rephrase any "text". Copy each "text" exactly. Only set "speaker".
2. Prefer names already in the roster over inventing new spellings of the same character.
3. If a SPOKEN line's speaker is genuinely unknowable from context, use "UNKNOWN".
---SEPARATOR---
ESTABLISHED ROSTER: {roster}

Assign speakers to these entries, copying every "text" exactly:

{batch}
```

(Note: `{roster}` and `{batch}` both live in the USER half — pass 2's runtime
formats only the user template via `.format(roster=..., batch=...)`, matching how
segment/instruct format only their user templates.)

- [ ] **Step 3: Create `app/default_prompts_instruct.txt`**

```
You add TTS voice-direction to already-attributed script entries. Output ONLY a valid JSON array — no markdown, no explanations.

You receive entries as {"speaker": "...", "text": "..."}. Return the SAME entries in the SAME order, each as {"speaker": "...", "text": "...", "instruct": "..."} where "instruct" is a 1-2 sentence (~8-15 word) voice direction for the TTS engine.

RULES:
1. Do NOT change "speaker" or "text". Copy them exactly. Only add "instruct".
2. NARRATOR default: "Neutral, even narration." Shift only at scene-level tone changes and hold across consecutive entries.
3. CHARACTER: describe the VOICE (emotion, delivery, vocal quality), not the body. Be direct; no weak qualifiers. Give each line enough context to stand alone.
---SEPARATOR---
Add an "instruct" to each entry, copying "speaker" and "text" exactly:

{batch}
```

- [ ] **Step 4: Add loaders to `app/default_prompts.py`**

Append after the existing `load_default_prompts` (mirror its structure — one cache dict per file):

```python
_SEGMENT_FILE = os.path.join(os.path.dirname(__file__), "default_prompts_segment.txt")
_ATTRIBUTE_FILE = os.path.join(os.path.dirname(__file__), "default_prompts_attribute.txt")
_INSTRUCT_FILE = os.path.join(os.path.dirname(__file__), "default_prompts_instruct.txt")
_segment_cache = {"mtime": None, "prompts": None}
_attribute_cache = {"mtime": None, "prompts": None}
_instruct_cache = {"mtime": None, "prompts": None}


def _load_pair(path, cache, name):
    return load_prompts_file(
        path, 2,
        missing_msg=f"{name} not found at {os.path.abspath(path)}.",
        malformed_msg=f"{name} is malformed: expected one '---SEPARATOR---'.",
        cache=cache)


def load_segment_prompts():
    return _load_pair(_SEGMENT_FILE, _segment_cache, "default_prompts_segment.txt")


def load_attribute_prompts():
    return _load_pair(_ATTRIBUTE_FILE, _attribute_cache, "default_prompts_attribute.txt")


def load_instruct_prompts():
    return _load_pair(_INSTRUCT_FILE, _instruct_cache, "default_prompts_instruct.txt")
```

- [ ] **Step 5: Write the loader smoke test (append to `test_pass_quality.py`)**

```python
import default_prompts


class PromptLoaderTests(unittest.TestCase):
    def test_three_pass_prompts_load_and_have_placeholders(self):
        seg_sys, seg_usr = default_prompts.load_segment_prompts()
        self.assertIn("{chunk}", seg_usr)
        self.assertTrue(seg_sys.strip())
        att_sys, att_usr = default_prompts.load_attribute_prompts()
        self.assertIn("{batch}", att_usr)
        self.assertIn("{roster}", att_usr)
        ins_sys, ins_usr = default_prompts.load_instruct_prompts()
        self.assertIn("{batch}", ins_usr)
```

- [ ] **Step 6: Run to verify it passes**

Run: `cd app && env/bin/python -m unittest test_pass_quality.PromptLoaderTests -v 2>&1 | tail -3`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd app && git add default_prompts_segment.txt default_prompts_attribute.txt default_prompts_instruct.txt default_prompts.py test_pass_quality.py
git commit -m "Add three-pass prompt files and loaders"
```

---

## Task 6: Pass helpers in `three_pass_generate.py` (pure, no LLM)

Build the deterministic helpers first so they can be unit-tested without a model:
roster extraction, batch iteration, and the pass-2/3 default fallbacks.

**Files:**
- Create: `app/three_pass_generate.py`
- Test: `app/test_three_pass_generate.py`

- [ ] **Step 1: Write the failing test**

```python
import unittest
import three_pass_generate as tp


class PassHelperTests(unittest.TestCase):
    def test_batches_split_entries_by_size(self):
        entries = [{"text": str(i)} for i in range(55)]
        batches = list(tp.iter_entry_batches(entries, batch_size=25))
        self.assertEqual([25, 25, 5], [len(b) for b in batches])

    def test_roster_collects_uppercase_non_narrator_speakers(self):
        entries = [{"speaker": "NARRATOR"}, {"speaker": "ELENA"},
                   {"speaker": "MARCUS"}, {"speaker": "ELENA"}, {"speaker": "UNKNOWN"}]
        self.assertEqual(["ELENA", "MARCUS"], tp.build_roster(entries))

    def test_default_instruct_by_type(self):
        self.assertEqual("Neutral, even narration.",
                         tp.default_instruct({"speaker": "NARRATOR", "text": "x"}))
        self.assertEqual("Natural, in-character delivery.",
                         tp.default_instruct({"speaker": "ELENA", "text": "x"}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate -v 2>&1 | tail -5`
Expected: FAIL — `ModuleNotFoundError: No module named 'three_pass_generate'`.

- [ ] **Step 3: Create `app/three_pass_generate.py` with the helpers**

```python
"""Three-pass script generation orchestrator (segment -> attribute -> instruct).
A side-by-side alternative to generate_script.py's single pass; the single-pass
path is untouched. See docs/superpowers/specs/2026-07-21-three-pass-...md."""

import argparse
import os
import sys

from openai import OpenAI

from generate_script import (call_llm_for_entries, split_into_chunks,
                             fix_mojibake, LLMGenParams)
from source_normalization import (normalize_known_source_corruptions,
                                  strip_known_front_matter)
from speaker_identity import stabilize_speaker_identities
from default_prompts import (load_segment_prompts, load_attribute_prompts,
                             load_instruct_prompts)
from pass_quality import (validate_segment_quality, validate_attribution,
                          validate_instruct)
from config_settings import load_app_config
from utils import get_runtime_data_dir, get_app_config_path

BATCH_SIZE = 25
NARRATOR_DEFAULT_INSTRUCT = "Neutral, even narration."
CHARACTER_DEFAULT_INSTRUCT = "Natural, in-character delivery."


def iter_entry_batches(entries, batch_size=BATCH_SIZE):
    for start in range(0, len(entries), batch_size):
        yield entries[start:start + batch_size]


def build_roster(entries):
    """Ordered unique UPPERCASE speaker names seen so far, excluding NARRATOR and
    the UNKNOWN placeholder — fed to pass 2 for naming consistency."""
    roster = []
    for entry in entries:
        speaker = (entry.get("speaker") or "").strip().upper()
        if speaker and speaker not in ("NARRATOR", "UNKNOWN") and speaker not in roster:
            roster.append(speaker)
    return roster


def default_instruct(entry):
    speaker = (entry.get("speaker") or "").strip().upper()
    return NARRATOR_DEFAULT_INSTRUCT if speaker == "NARRATOR" else CHARACTER_DEFAULT_INSTRUCT
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate -v 2>&1 | tail -3`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd app && git add three_pass_generate.py test_three_pass_generate.py
git commit -m "Add three-pass pure helpers (batching, roster, default instruct)"
```

---

## Task 7: Pass 2 runner (attribute a batch, with freeze retry + testing-mode fail)

**Files:**
- Modify: `app/three_pass_generate.py`
- Test: `app/test_three_pass_generate.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from types import SimpleNamespace


def _client_returning(payloads):
    """LM Studio stub: each call returns the next payload as JSON content."""
    responses = iter(payloads)

    def create(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(next(responses))),
            finish_reason="stop")], usage=None)

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


class Pass2Tests(unittest.TestCase):
    def _params(self):
        return LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                            max_tokens=500, temperature=0.1)

    def test_attributes_a_batch_and_freezes_text(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold."},
                  {"type": "SPOKEN", "text": "Tell me."}]
        good = [{"speaker": "NARRATOR", "text": "The room was cold."},
                {"speaker": "ELENA", "text": "Tell me."}]
        client = _client_returning([good])
        out = tp.attribute_batch(client, "m", frozen, self._params(), roster=[])
        self.assertEqual(["NARRATOR", "ELENA"], [e["speaker"] for e in out])
        self.assertEqual("Tell me.", out[1]["text"])

    def test_pass2_fail_mode_raises_when_exhausted(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        # model keeps leaving the SPOKEN line as NARRATOR -> never valid
        bad = [{"speaker": "NARRATOR", "text": "Tell me."}]
        client = _client_returning([bad, bad, bad, bad])
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, "m", frozen, self._params(), roster=[],
                               max_retries=1, on_exhaustion="fail")
```

Need `LLMGenParams` and `tp` imported at the top of the test file — add `from generate_script import LLMGenParams` to the imports.

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate.Pass2Tests -v 2>&1 | tail -5`
Expected: FAIL — `AttributeError: module 'three_pass_generate' has no attribute 'attribute_batch'`.

- [ ] **Step 3: Add `PassExhausted` and `attribute_batch` to `three_pass_generate.py`**

```python
import json


class PassExhausted(Exception):
    """A pass-2/3 batch could not produce valid output within its retry budget.
    In testing mode (on_exhaustion='fail') this aborts the book so the real
    failure rate is visible."""


def attribute_batch(client, model_name, frozen_batch, params, roster,
                    max_retries=3, on_exhaustion="fail"):
    """Assign speakers to one batch of frozen {type,text} entries. Enforces the
    text freeze; retries on invalid output. On exhaustion: 'fail' raises
    PassExhausted (testing default); 'fallback' keeps frozen text and labels
    unresolved SPOKEN spans UNKNOWN via stabilize_speaker_identities."""
    sys_prompt, usr_template = load_attribute_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    batch_json = json.dumps([{"type": e["type"], "text": e["text"]}
                             for e in frozen_batch], ensure_ascii=False)
    user_prompt = usr_template.format(roster=", ".join(roster) or "(none yet)",
                                      batch=batch_json)
    named = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, params,
        log_name="llm_responses.log", label="ATTRIBUTE", max_retries=max_retries,
        validate_entries=lambda entries: validate_attribution(frozen_batch, entries))
    if named:
        return named
    if on_exhaustion == "fail":
        raise PassExhausted(f"attribution failed for a {len(frozen_batch)}-entry batch")
    # fallback: frozen text + deterministic naming, UNKNOWN where unresolved
    seeded = [{"speaker": "NARRATOR" if e["type"] == "NARRATOR" else "UNKNOWN",
               "text": e["text"]} for e in frozen_batch]
    return stabilize_speaker_identities(seeded, established_speakers=roster)["entries"]
```

Note: `call_llm_for_entries` returns `[]` when every attempt fails validation, which is why `attribute_batch` treats a falsy return as exhaustion.

- [ ] **Step 4: Run to verify it passes**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate.Pass2Tests -v 2>&1 | tail -3`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd app && git add three_pass_generate.py test_three_pass_generate.py
git commit -m "Add three-pass pass 2 (attribute) batch runner with freeze + fail mode"
```

---

## Task 8: Pass 3 runner (instruct a batch, with default fallback)

**Files:**
- Modify: `app/three_pass_generate.py`
- Test: `app/test_three_pass_generate.py`

- [ ] **Step 1: Write the failing test**

```python
class Pass3Tests(unittest.TestCase):
    def _params(self):
        return LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                            max_tokens=500, temperature=0.1)

    def test_adds_instruct_and_freezes(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        good = [{"speaker": "ELENA", "text": "Tell me.", "instruct": "Firm, quiet."}]
        client = _client_returning([good])
        out = tp.instruct_batch(client, "m", prior, self._params())
        self.assertEqual("Firm, quiet.", out[0]["instruct"])

    def test_falls_back_to_default_instruct_on_exhaustion(self):
        prior = [{"speaker": "NARRATOR", "text": "The room was cold."}]
        bad = [{"speaker": "NARRATOR", "text": "The room was cold.", "instruct": ""}]
        client = _client_returning([bad, bad])
        out = tp.instruct_batch(client, "m", prior, self._params(), max_retries=1)
        self.assertEqual("Neutral, even narration.", out[0]["instruct"])
        self.assertEqual("The room was cold.", out[0]["text"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate.Pass3Tests -v 2>&1 | tail -5`
Expected: FAIL — no attribute `instruct_batch`.

- [ ] **Step 3: Add `instruct_batch` to `three_pass_generate.py`**

```python
def instruct_batch(client, model_name, prior_batch, params, max_retries=3):
    """Add instruct to one batch of {speaker,text} entries. Enforces the freeze
    on text+speaker. On exhaustion, attaches a default instruct per entry so
    pass 3 never fails the book."""
    sys_prompt, usr_template = load_instruct_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    batch_json = json.dumps([{"speaker": e["speaker"], "text": e["text"]}
                             for e in prior_batch], ensure_ascii=False)
    user_prompt = usr_template.format(batch=batch_json)
    annotated = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, params,
        log_name="llm_responses.log", label="INSTRUCT", max_retries=max_retries,
        validate_entries=lambda entries: validate_instruct(prior_batch, entries))
    if annotated:
        return annotated
    return [{"speaker": e["speaker"], "text": e["text"],
             "instruct": default_instruct(e)} for e in prior_batch]
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate.Pass3Tests -v 2>&1 | tail -3`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd app && git add three_pass_generate.py test_three_pass_generate.py
git commit -m "Add three-pass pass 3 (instruct) batch runner with default fallback"
```

---

## Task 9: Pass 1 runner + full three-pass orchestration over an in-memory book

**Files:**
- Modify: `app/three_pass_generate.py`
- Test: `app/test_three_pass_generate.py`

- [ ] **Step 1: Write the failing test (end-to-end through all 3 passes, mocked LLM)**

```python
class EndToEndTests(unittest.TestCase):
    def test_three_passes_assemble_final_entries(self):
        source = "The room was cold. \"Tell me the truth.\""
        # Pass 1 segments; pass 2 names; pass 3 instructs. One chunk, one batch.
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        named = [{"speaker": "NARRATOR", "text": "The room was cold."},
                 {"speaker": "ELENA", "text": "Tell me the truth."}]
        instructed = [{"speaker": "NARRATOR", "text": "The room was cold.",
                       "instruct": "Cold, still narration."},
                      {"speaker": "ELENA", "text": "Tell me the truth.",
                       "instruct": "Firm, quiet demand."}]
        client = _client_returning([seg, named, instructed])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        entries = tp.run_three_pass(client, "m", source, params, chunk_size=6000)
        self.assertEqual(2, len(entries))
        self.assertEqual({"speaker", "text", "instruct"}, set(entries[0].keys()))
        self.assertEqual("ELENA", entries[1]["speaker"])
        self.assertEqual("Firm, quiet demand.", entries[1]["instruct"])

    def test_segment_accepts_trigram_only_near_miss_on_exhaustion(self):
        # A chunk too short to split whose every attempt is a complete but
        # lightly-reordered conversion (>=0.90 recall, trigram in [0.82,0.90))
        # must be accepted rather than failing pass 1. Build a source and a
        # reordered echo that lands in the near-miss band.
        words = [f"word{i}" for i in range(100)]
        source = " ".join(words)
        self.assertEqual([], tp.split_failed_chunk(source))  # unsplittable
        swapped = list(words)
        i = 0
        while i + 1 < len(swapped):
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            i += 25
        near = [{"type": "NARRATOR", "text": " ".join(swapped)}]
        # every attempt returns the same near-miss
        client = _client_returning([near, near, near, near, near, near, near])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        out = tp.segment_chunk_adaptively(client, "m", source, params)
        self.assertTrue(out)
        self.assertFalse(tp.validate_segment_quality(source, out)["passed"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate.EndToEndTests -v 2>&1 | tail -5`
Expected: FAIL — no attribute `run_three_pass`.

- [ ] **Step 3: Add `segment_chunk`, `segment_chunk_adaptively`, and `run_three_pass`**

`segment_chunk_adaptively` gives pass 1 the SAME near-miss acceptance +
adaptive-split recursion the single-pass path has, reusing the shared
validator-agnostic primitives from `generate_script` (`split_failed_chunk`,
`is_trigram_only_near_miss`) rather than touching `process_chunk`. Add these
imports at the top of `three_pass_generate.py`:

```python
from generate_script import split_failed_chunk, is_trigram_only_near_miss
from pass_quality import validate_segment_quality  # already imported; keep one copy
```

```python
def segment_chunk(client, model_name, chunk, params, max_retries=4, near_miss_sink=None):
    """Pass 1 single attempt-budget over one chunk -> [{type,text}], via the
    segment fidelity gate. Captures a trigram-only near-miss into near_miss_sink
    (same mechanism call_llm_for_entries uses for single-pass). Returns [] on
    exhaustion."""
    sys_prompt, usr_template = load_segment_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    user_prompt = usr_template.format(chunk=chunk)
    return call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, params,
        log_name="llm_responses.log", label="SEGMENT", max_retries=max_retries,
        validate_entries=lambda entries: validate_segment_quality(chunk, entries),
        near_miss_sink=near_miss_sink)


def _accept_segment_near_miss(near_miss):
    if not near_miss:
        return []
    entries, quality = near_miss[0]
    print("  SEGMENT accepted as trigram-only near-miss "
          f"(ordered_trigram_recall={quality['metrics']['ordered_trigram_recall']})")
    return entries


def segment_chunk_adaptively(client, model_name, chunk, params):
    """Pass 1 with the full safety net: full-chunk attempt, then a
    natural-boundary split whose halves each recurse, and exhaustion-only
    trigram-only near-miss acceptance. Mirrors process_chunk_adaptively but for
    the segment gate. Returns [{type,text}] or [] (book failure)."""
    near_miss = []
    entries = segment_chunk(client, model_name, chunk, params, near_miss_sink=near_miss)
    if entries:
        return entries
    parts = split_failed_chunk(chunk)
    if not parts:
        return _accept_segment_near_miss(near_miss)
    print(f"  Adaptive split (segment): -> {len(parts[0])} + {len(parts[1])} chars")
    combined, any_failed = [], False
    for part in parts:
        part_entries = segment_chunk_adaptively(client, model_name, part, params)
        if not part_entries:
            any_failed = True
            continue
        combined.extend(part_entries)
    if any_failed:
        return _accept_segment_near_miss(near_miss)
    combined_quality = validate_segment_quality(chunk, combined)
    if not combined_quality["passed"]:
        if is_trigram_only_near_miss(combined_quality):
            return combined
        return _accept_segment_near_miss(near_miss)
    return combined


def run_three_pass(client, model_name, source_text, params, chunk_size,
                   on_exhaustion="fail"):
    """Full flow. Returns the assembled [{speaker,text,instruct}] list, or raises
    RuntimeError if pass 1 exhausts a chunk (book failure)."""
    # Pass 1: segment every chunk (with near-miss + adaptive split).
    chunks = split_into_chunks(source_text, max_size=chunk_size)
    segmented = []
    for i, chunk in enumerate(chunks, 1):
        seg = segment_chunk_adaptively(client, model_name, chunk, params)
        if not seg:
            raise RuntimeError(f"pass 1 (segment) failed on chunk {i}/{len(chunks)}")
        segmented.extend(seg)
    # Pass 2: name in wide entry-batches, carrying the growing roster.
    named = []
    for batch in iter_entry_batches(segmented):
        named.extend(attribute_batch(client, model_name, batch, params,
                                     roster=build_roster(named),
                                     on_exhaustion=on_exhaustion))
    # Pass 3: instruct in wide entry-batches.
    annotated = []
    for batch in iter_entry_batches(named):
        annotated.extend(instruct_batch(client, model_name, batch, params))
    return annotated
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate.EndToEndTests -v 2>&1 | tail -3`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd app && git add three_pass_generate.py test_three_pass_generate.py
git commit -m "Add three-pass pass 1 (segment) and full run_three_pass orchestration"
```

---

## Task 9B: Per-pass checkpoint & resume

Save progress after each pass-1 chunk and each pass-2 / pass-3 batch, and resume
mid-flow after a crash. Checkpoint is keyed by a fingerprint distinct from
single-pass, so it never collides with or resumes from a single-pass checkpoint.

**Files:**
- Modify: `app/three_pass_generate.py`
- Test: `app/test_three_pass_generate.py`

- [ ] **Step 1: Write the failing test**

```python
import os, tempfile


class CheckpointTests(unittest.TestCase):
    def _payloads(self):
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        named = [{"speaker": "NARRATOR", "text": "The room was cold."},
                 {"speaker": "ELENA", "text": "Tell me the truth."}]
        instructed = [{"speaker": "NARRATOR", "text": "The room was cold.",
                       "instruct": "Cold."},
                      {"speaker": "ELENA", "text": "Tell me the truth.",
                       "instruct": "Firm."}]
        return seg, named, instructed

    def test_completed_stage_is_not_recomputed_on_resume(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg, named, instructed = self._payloads()
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            # First run: crash after pass 1 by making pass 2 raise.
            crashing = _client_returning([seg])  # only pass-1 payload; pass 2 will StopIteration
            with self.assertRaises(StopIteration):
                tp.run_three_pass(crashing, "m", source, params, chunk_size=6000,
                                  output_path=out)
            cp = tp.three_pass_checkpoint_path(out)
            self.assertTrue(os.path.exists(cp))
            # Resume: a client that ONLY provides pass-2 and pass-3 payloads.
            # If pass 1 were recomputed it would StopIteration; it must not be.
            resume_client = _client_returning([named, instructed])
            entries = tp.run_three_pass(resume_client, "m", source, params,
                                        chunk_size=6000, output_path=out)
            self.assertEqual(2, len(entries))
            self.assertEqual("ELENA", entries[1]["speaker"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate.CheckpointTests -v 2>&1 | tail -5`
Expected: FAIL — `run_three_pass()` has no `output_path` param / `three_pass_checkpoint_path` missing.

- [ ] **Step 3: Add checkpoint helpers and thread them through `run_three_pass`**

```python
import hashlib
from utils import atomic_json_write, safe_load_json


def three_pass_checkpoint_path(output_path):
    return output_path + ".threepass_checkpoint.json"


def three_pass_fingerprint(source_text, model_name, chunk_size):
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    return {"source_sha256": digest, "model_name": model_name,
            "chunk_size": chunk_size, "pipeline": "three_pass"}


def _load_three_pass_checkpoint(output_path, fingerprint):
    data = safe_load_json(three_pass_checkpoint_path(output_path), None)
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return None
    return data


def _save_three_pass_checkpoint(output_path, fingerprint, stage, segmented,
                                chunks_done, named, annotated):
    atomic_json_write({"fingerprint": fingerprint, "stage": stage,
                       "chunks_done": chunks_done, "segmented": segmented,
                       "named": named, "annotated": annotated},
                      three_pass_checkpoint_path(output_path))
```

Then replace the `run_three_pass` body (from Task 9) with a checkpoint-aware
version. The signature gains `output_path=None`; when `None`, it behaves exactly
as before (no checkpointing — the pure in-memory path the earlier tests use):

```python
def run_three_pass(client, model_name, source_text, params, chunk_size,
                   on_exhaustion="fail", output_path=None):
    chunks = split_into_chunks(source_text, max_size=chunk_size)
    fingerprint = three_pass_fingerprint(source_text, model_name, chunk_size)
    state = _load_three_pass_checkpoint(output_path, fingerprint) if output_path else None
    segmented = state["segmented"] if state else []
    chunks_done = state["chunks_done"] if state else 0
    named = state["named"] if state else []
    annotated = state["annotated"] if state else []

    def save(stage):
        if output_path:
            _save_three_pass_checkpoint(output_path, fingerprint, stage,
                                        segmented, chunks_done, named, annotated)

    # Pass 1 — resume from chunks_done.
    for i in range(chunks_done, len(chunks)):
        seg = segment_chunk_adaptively(client, model_name, chunks[i], params)
        if not seg:
            raise RuntimeError(f"pass 1 (segment) failed on chunk {i + 1}/{len(chunks)}")
        segmented.extend(seg)
        chunks_done = i + 1
        save("segment")
    # Pass 2 — resume from len(named) entries (batch-aligned slices of segmented).
    while len(named) < len(segmented):
        batch = segmented[len(named):len(named) + BATCH_SIZE]
        named.extend(attribute_batch(client, model_name, batch, params,
                                     roster=build_roster(named),
                                     on_exhaustion=on_exhaustion))
        save("attribute")
    # Pass 3 — resume from len(annotated).
    while len(annotated) < len(named):
        batch = named[len(annotated):len(annotated) + BATCH_SIZE]
        annotated.extend(instruct_batch(client, model_name, batch, params))
        save("instruct")
    save("done")
    return annotated
```

- [ ] **Step 4: Run to verify it passes (and the earlier end-to-end still passes)**

Run: `cd app && env/bin/python -m unittest test_three_pass_generate -v 2>&1 | tail -3`
Expected: PASS (helpers + Pass2 + Pass3 + EndToEnd + Checkpoint tests).

- [ ] **Step 5: Commit**

```bash
cd app && git add three_pass_generate.py test_three_pass_generate.py
git commit -m "Add per-pass checkpoint and resume to three-pass generation"
```

---

## Task 10: CLI + config + file output (mirror generate_script)

**Files:**
- Modify: `app/three_pass_generate.py`
- Test: manual smoke (no unit test — this is I/O wiring; the A/B run is the real exercise)

- [ ] **Step 1: Add `main()` to `three_pass_generate.py`**

```python
def main():
    parser = argparse.ArgumentParser(description="Three-pass annotated script generation.")
    parser.add_argument("input_file")
    parser.add_argument("--output", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--strip-front-matter", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--pass2-on-exhaustion", choices=["fail", "fallback"],
                        default="fail",
                        help="testing default 'fail' surfaces pass-2 failures; "
                             "'fallback' degrades gracefully (production).")
    args = parser.parse_args()

    with open(args.input_file, encoding="utf-8", errors="replace") as fh:
        book = fh.read()
    book = fix_mojibake(book)
    book, _ = normalize_known_source_corruptions(book)
    if args.strip_front_matter:
        book, _ = strip_known_front_matter(book)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_dir = os.path.dirname(__file__)
    data_dir = get_runtime_data_dir(root)
    config = load_app_config(get_app_config_path(data_dir, root, app_dir))
    llm = config.get("llm", {})
    gen = config.get("generation") or {}
    chunk_size = args.chunk_size or gen.get("chunk_size", 6000)
    params = LLMGenParams(
        max_tokens=gen.get("max_tokens", 10000),
        temperature=gen.get("temperature", 0.6),
        top_p=gen.get("top_p", 0.8),
        top_k=gen.get("top_k"), min_p=gen.get("min_p"),
        context_length=None)
    client = OpenAI(base_url=llm.get("base_url", "http://localhost:1234/v1"),
                    api_key=llm.get("api_key", "local"))
    model_name = llm.get("model_name")

    output_path = args.output or os.path.join(root, "annotated_script.json")
    print(f"Three-pass generation: {len(book)} chars, chunk_size={chunk_size}, "
          f"model={model_name}, pass2_on_exhaustion={args.pass2_on_exhaustion}")
    try:
        entries = run_three_pass(client, model_name, book, params, chunk_size,
                                 on_exhaustion=args.pass2_on_exhaustion,
                                 output_path=output_path)
    except (RuntimeError, PassExhausted) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)
    print(f"Wrote {len(entries)} entries to {output_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the CLI parses and imports cleanly**

Run: `cd app && env/bin/python three_pass_generate.py --help 2>&1 | grep -E "pass2-on-exhaustion|chunk-size"`
Expected: both options listed.

- [ ] **Step 3: Verify the module imports with no error**

Run: `cd app && env/bin/python -c "import three_pass_generate; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 4: Commit**

```bash
cd app && git add three_pass_generate.py
git commit -m "Add three-pass CLI, config loading, and file output"
```

---

## Task 11: Full suite + inventory regen

**Files:**
- Modify: `app/unit_test_inventory.json`

- [ ] **Step 1: Run the whole suite**

Run: `cd app && env/bin/python -m unittest discover -b -p "test_*.py" 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: `OK` (existing tests + new `test_pass_quality` + `test_three_pass_generate`).

- [ ] **Step 2: Regenerate the test inventory**

Run: `cd app && env/bin/python update_test_inventory.py 2>&1 | tail -1`
Expected: `Updated .../unit_test_inventory.json`.

- [ ] **Step 3: Confirm the inventory test passes**

Run: `cd app && env/bin/python -m unittest test_inventory 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
cd app && git add unit_test_inventory.json
git commit -m "Regenerate test inventory for three-pass modules"
```

---

## Task 12: Acceptance A/B harness (three_pass vs single-pass on 10wn)

**Files:**
- Create: `ab_test/run_three_pass_ab.sh`

This mirrors `ab_test/run_chunksize.sh` but calls `three_pass_generate.py` for the
three-pass arm and `generate_script.py` for the single-pass baseline, on the same
hard book at the same model/context. Not run here (needs the GPU, which is busy);
it is the acceptance artifact.

- [ ] **Step 1: Create `ab_test/run_three_pass_ab.sh`**

```bash
#!/usr/bin/env bash
# Acceptance A/B: three-pass vs single-pass on the most problematic book (10wn),
# same model + context. Compare completion / storms / wall-clock.
set -u
APP="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app"
OUT="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/ab_test_threepass"
PY="$APP/env/bin/python"; LMS="/home/fakemitch/.lmstudio/bin/lms"
cd "$APP" || exit 1
BOOK="Arc 4 - Volume 10wn"
MODEL="gemma-4-e4b-uncensored-hauhaucs-aggressive"
mkdir -p "$OUT/three_pass" "$OUT/single_pass"
"$LMS" unload --all >/dev/null 2>&1
echo "=== three_pass start $(date -Is) ==="
"$PY" three_pass_generate.py "uploads/$BOOK.txt" --pass2-on-exhaustion fail \
    --output "$OUT/three_pass/$BOOK.json" > "$OUT/three_pass/$BOOK.log" 2>&1
echo "=== three_pass exit=$? $(date -Is) ==="
echo "=== single_pass start $(date -Is) ==="
"$PY" generate_script.py "uploads/$BOOK.txt" \
    --output "$OUT/single_pass/$BOOK.json" > "$OUT/single_pass/$BOOK.log" 2>&1
echo "=== single_pass exit=$? $(date -Is) ==="
```

- [ ] **Step 2: Syntax-check and commit (do not run — GPU busy)**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git
chmod +x ab_test/run_three_pass_ab.sh && bash -n ab_test/run_three_pass_ab.sh && echo "OK"
git add -f ab_test/run_three_pass_ab.sh
git commit -m "Add three-pass vs single-pass acceptance A/B runner"
```

Note: `ab_test/` is gitignored, so `-f` is required (mirrors how the other ab_test runners are tracked, or leave untracked if preferred — confirm with the repo owner).

---

## Deferred follow-ups (from the spec — do NOT do in this plan)

- After the acceptance A/B validates three-pass, flip the `--pass2-on-exhaustion`
  default and the config default from `"fail"` to `"fallback"`.
- If three-pass proves strictly better, promote it to the default generator and
  retire the single-pass prompt path (spec scope option B).
