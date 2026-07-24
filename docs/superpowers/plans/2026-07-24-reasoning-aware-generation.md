# Reasoning-Aware Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the three-pass generator from wasting 42.5% of a reasoning
model's wall time on truncated-then-discarded generations, and stop it from
spending hours on sources it can never process.

**Architecture:** Four independent phases. Phase 1 gates and repairs damaged
source text before any LLM call. Phase 2 records why a batch failed so the
later phases are measurable. Phase 3 makes the token ceiling aware of invisible
reasoning tokens and replaces unbounded escalation with a single-retry circuit
break. Phase 4 plumbs `reasoning_effort` through and runs a two-arm probe.

**Tech Stack:** Python 3.10, `unittest` (flat `app/test_*.py` modules, imported
without a package prefix), OpenAI-compatible client against LM Studio.

**Spec:** `docs/superpowers/specs/2026-07-24-reasoning-aware-generation-design.md`

**Run tests with:** `cd app && ./env/bin/python -m unittest <module> -v`

---

## Phase 1 — Unicode gate and repair

### Task 1: `repair_lossy_replacements` core rules

**Files:**
- Modify: `app/source_normalization.py` (append; the module already holds
  `normalize_known_source_corruptions` and `strip_known_front_matter`)
- Test: `app/test_source_normalization_lossy.py` (create)

Background the implementer needs: U+FFFD (`�`) is the Unicode replacement
character. When cp1252 smart punctuation is decoded as UTF-8 with
`errors="replace"`, each destroyed character becomes one U+FFFD. The original
bytes are gone, so recovery is inference from surrounding text. This is
different from `generate_script.py`'s `fix_mojibake`, which handles the case
where the bytes *survived* as `â€™` — do not modify that function.

Critical detail: a run of consecutive U+FFFD is **several** destroyed
characters, not one. `�Hee-hee��` was `“Hee-hee…”`. So inference runs per
character position, and neighbour lookup skips over adjacent U+FFFD to find
the nearest surviving character.

- [ ] **Step 1: Write the failing test**

Create `app/test_source_normalization_lossy.py`:

```python
import unittest

from source_normalization import repair_lossy_replacements


class RepairLossyReplacementsTest(unittest.TestCase):

    def test_apostrophe_between_letters(self):
        text, repairs = repair_lossy_replacements("don�t")
        self.assertEqual(text, "don’t")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["after"], "’")
        self.assertEqual(repairs[0]["offset"], 3)

    def test_open_quote_after_newline(self):
        text, _ = repair_lossy_replacements("\n�I was there")
        self.assertEqual(text, "\n“I was there")

    def test_close_quote_after_sentence_punctuation(self):
        text, _ = repair_lossy_replacements("he said.�\n")
        self.assertEqual(text, "he said.”\n")

    def test_em_dash_before_capital(self):
        text, _ = repair_lossy_replacements("Magic�Fiction")
        self.assertEqual(text, "Magic—Fiction")

    def test_en_dash_after_digit(self):
        text, _ = repair_lossy_replacements("Kiyotaka, 1973� illustrator")
        self.assertEqual(text, "Kiyotaka, 1973– illustrator")

    def test_copyright_before_year(self):
        text, _ = repair_lossy_replacements("translation � 2019 by Yen Press")
        self.assertEqual(text, "translation © 2019 by Yen Press")

    def test_multi_run_is_several_characters(self):
        text, _ = repair_lossy_replacements("\n�Hee-hee��\n")
        self.assertEqual(text, "\n“Hee-hee…”\n")

    def test_triple_run_is_quoted_ellipsis(self):
        text, _ = repair_lossy_replacements("\n���\n")
        self.assertEqual(text, "\n“…”\n")

    def test_clean_text_is_untouched(self):
        source = "Nothing wrong here — nothing at all."
        text, repairs = repair_lossy_replacements(source)
        self.assertEqual(text, source)
        self.assertEqual(repairs, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_source_normalization_lossy -v`
Expected: FAIL with `ImportError: cannot import name 'repair_lossy_replacements'`

- [ ] **Step 3: Write the implementation**

Append to `app/source_normalization.py`:

```python
_YEAR_RE = re.compile(r"\s*(?:1[89]|20)\d{2}\b")
_REPLACEMENT = "�"


def _nearest_surviving(chars, index, step):
    """Return the closest non-U+FFFD neighbour in one direction.

    Consecutive U+FFFD are separate destroyed characters, so a neighbour
    lookup has to skip past them to find real context. Returns "\n" when it
    runs off either end, which makes start/end of file behave like a line
    boundary.
    """
    position = index + step
    while 0 <= position < len(chars) and chars[position] == _REPLACEMENT:
        position += step
    return chars[position] if 0 <= position < len(chars) else "\n"


def _infer_replacement(chars, index):
    """Infer one destroyed character from its surroundings, or None."""
    left = chars[index - 1] if index else "\n"
    right = chars[index + 1] if index + 1 < len(chars) else "\n"
    right_surviving = _nearest_surviving(chars, index, 1)
    if _YEAR_RE.match("".join(chars[index + 1:index + 7])):
        return "©"
    if right == _REPLACEMENT and (left.isalnum() or left in ".,!?"):
        return "…"
    if left == _REPLACEMENT and (right in "\n \t" or right == _REPLACEMENT):
        return "”"
    if left in "\n \t" and (right_surviving.isalnum() or right == _REPLACEMENT):
        return "“"
    if left in ".!?,;:…" and right in "\n \t":
        return "”"
    if left.isalpha() and right.islower():
        return "’"
    if left.isalpha() and right.isupper():
        return "—"
    if left.isdigit():
        return "–"
    return None


def repair_lossy_replacements(text):
    """Infer characters destroyed into U+FFFD, returning text and evidence.

    Distinct from generate_script.fix_mojibake, which repairs the recoverable
    byte form (``â€™``). Here the original bytes are gone, so each U+FFFD is
    inferred from its neighbours. Inference is per character position because
    a run of U+FFFD is several destroyed characters, not one. Returns the text
    unchanged when there is nothing to repair, and never mutates the source
    file. Positions that cannot be inferred are left as U+FFFD for the caller's
    residual policy to handle.
    """
    if _REPLACEMENT not in text:
        return text, []
    chars = list(text)
    repairs = []
    for index, char in enumerate(chars):
        if char != _REPLACEMENT:
            continue
        inferred = _infer_replacement(chars, index)
        if inferred is not None:
            repairs.append({"offset": index, "before": _REPLACEMENT,
                            "after": inferred})
    for repair in repairs:
        chars[repair["offset"]] = repair["after"]
    return "".join(chars), repairs
```

Note for the implementer: inference reads from `chars` before any mutation is
applied (mutations are collected then applied in a second loop), so an inferred
character never becomes context for its neighbour. This keeps the result
independent of iteration order — `\n���\n` must resolve using the original
U+FFFD positions, not partially repaired ones.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && ./env/bin/python -m unittest test_source_normalization_lossy -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add app/source_normalization.py app/test_source_normalization_lossy.py
git commit -m "Add lossy U+FFFD repair for cp1252-destroyed punctuation"
```

---

### Task 2: Residual policy and corpus verification

**Files:**
- Modify: `app/source_normalization.py`
- Test: `app/test_source_normalization_lossy.py`

- [ ] **Step 1: Write the failing test**

Append to `app/test_source_normalization_lossy.py`, inside the existing class:

```python
    def test_residual_is_neutralized_to_apostrophe(self):
        # "coup d<FFFD><FFFD>tat": the second FFFD was an "e-acute", a letter,
        # which no context rule can restore. It must survive rule inference and
        # then be neutralized.
        text, repairs = repair_lossy_replacements("coup d��tat")
        self.assertIn("�", text)
        cleaned, residual = neutralize_lossy_residue(text)
        self.assertNotIn("�", cleaned)
        self.assertEqual(residual, text.count("�"))

    def test_neutralize_is_noop_on_clean_text(self):
        cleaned, residual = neutralize_lossy_residue("all clean")
        self.assertEqual(cleaned, "all clean")
        self.assertEqual(residual, 0)
```

Update the import line at the top of the file to:

```python
from source_normalization import (neutralize_lossy_residue,
                                  repair_lossy_replacements)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_source_normalization_lossy -v`
Expected: FAIL with `ImportError: cannot import name 'neutralize_lossy_residue'`

- [ ] **Step 3: Write the implementation**

Append to `app/source_normalization.py`:

```python
def neutralize_lossy_residue(text, substitute="'"):
    """Replace U+FFFD that no rule could infer, returning text and a count.

    Applied only after repair_lossy_replacements. The residue is genuinely
    unrecoverable: destroyed letters (``coup d’état``), and cases ambiguous
    between a plural possessive and a closing quote (``knights’`` vs
    ``knights”``) that context cannot separate. A plain apostrophe is the most
    likely value across that residue. Callers record the count so the
    approximation is visible rather than silent.
    """
    if _REPLACEMENT not in text:
        return text, 0
    return text.replace(_REPLACEMENT, substitute), text.count(_REPLACEMENT)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && ./env/bin/python -m unittest test_source_normalization_lossy -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Verify against the real corpus**

This step proves the rules hit the coverage the spec claims. Run:

```bash
cd app && ./env/bin/python - <<'PY'
from source_normalization import neutralize_lossy_residue, repair_lossy_replacements
import os
inputs = "../ab_test_runtime/results/collect_all_20260723-040555/inputs"
for name in sorted(os.listdir(inputs)):
    original = open(os.path.join(inputs, name), encoding="utf-8").read()
    repaired, repairs = repair_lossy_replacements(original)
    cleaned, residual = neutralize_lossy_residue(repaired)
    print(f"{name:24} damaged={original.count(chr(0xFFFD)):5} "
          f"repaired={len(repairs):5} residual={residual:4} "
          f"clean_unchanged={cleaned == original if chr(0xFFFD) not in original else 'n/a'}")
PY
```

Expected output: `index18.txt` shows `damaged=6662 repaired=6036 residual=626`,
and every other book shows `damaged=0 repaired=0 residual=0
clean_unchanged=True`.

If `repaired` is below 6036, a rule regressed — do not proceed, fix it first.

- [ ] **Step 6: Commit**

```bash
git add app/source_normalization.py app/test_source_normalization_lossy.py
git commit -m "Neutralize unrecoverable U+FFFD residue with a recorded count"
```

---

### Task 3: Wire the gate into the three-pass CLI

**Files:**
- Modify: `app/three_pass_generate.py:1013-1018`
- Test: `app/test_three_pass_unicode_gate.py` (create)

Context: `generate_script.py:1161-1167` is production's equivalent gate. It
calls `audit_unicode_text` and `sys.exit(1)` on replacement or unsafe control
characters. `three_pass_generate.py` has no such gate, which is why `index18`
burned 2.1 hours per model. The current block reads:

```python
    with open(args.input_file, encoding="utf-8", errors="replace") as fh:
        book = fh.read()
    book = fix_mojibake(book)
    book, _ = normalize_known_source_corruptions(book)
    if args.strip_front_matter:
        book, _ = strip_known_front_matter(book)
```

`errors="replace"` is itself a hazard: it can manufacture the exact U+FFFD the
gate exists to catch. It becomes strict decoding with an explicit message.

- [ ] **Step 1: Write the failing test**

Create `app/test_three_pass_unicode_gate.py`:

```python
import unittest

from three_pass_generate import prepare_source_text


class PrepareSourceTextTest(unittest.TestCase):

    def test_clean_source_passes_through(self):
        text, report = prepare_source_text("A clean line.\n")
        self.assertEqual(text, "A clean line.\n")
        self.assertEqual(report["repaired"], 0)
        self.assertEqual(report["residual"], 0)

    def test_damaged_source_is_repaired_and_reported(self):
        text, report = prepare_source_text("don�t stop\n")
        self.assertEqual(text, "don’t stop\n")
        self.assertEqual(report["repaired"], 1)
        self.assertEqual(report["residual"], 0)
        self.assertNotIn("�", text)

    def test_unsafe_control_characters_are_rejected(self):
        with self.assertRaises(ValueError) as caught:
            prepare_source_text("bad\x00byte\n")
        self.assertIn("control", str(caught.exception).lower())

    def test_density_above_threshold_is_rejected(self):
        # 3 of 10 characters destroyed = 30%, far above the 2% ceiling.
        with self.assertRaises(ValueError) as caught:
            prepare_source_text("ab�cd�ef�g")
        self.assertIn("density", str(caught.exception).lower())

    def test_index18_density_is_admitted(self):
        # index18 sits at 1.4%, below the 2% ceiling: 14 damaged in 1000.
        source = ("x" * 986) + ("�" * 14)
        text, report = prepare_source_text(source)
        self.assertNotIn("�", text)
        self.assertEqual(report["repaired"] + report["residual"], 14)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_three_pass_unicode_gate -v`
Expected: FAIL with `ImportError: cannot import name 'prepare_source_text'`

- [ ] **Step 3: Write the implementation**

Add to `app/three_pass_generate.py`, above the `main()` function:

```python
MAX_REPLACEMENT_DENSITY = 0.02


def prepare_source_text(book):
    """Repair, neutralize and audit source text before any LLM call.

    Mirrors production's gate in generate_script.main (audit_unicode_text then
    hard failure) so the diagnostic CLI cannot spend hours on a source that
    production would reject outright. Raises ValueError rather than exiting so
    the behaviour is testable; main() turns it into a non-zero exit.
    """
    damaged = book.count("�")
    if damaged and damaged / max(len(book), 1) > MAX_REPLACEMENT_DENSITY:
        raise ValueError(
            f"source replacement-character density "
            f"{damaged / len(book):.1%} exceeds the "
            f"{MAX_REPLACEMENT_DENSITY:.0%} ceiling; refusing to process it")
    book, repairs = repair_lossy_replacements(book)
    book, residual = neutralize_lossy_residue(book)
    report = audit_unicode_text(book)
    if report["unsafe_controls"]:
        raise ValueError("source contains unsafe control characters: "
                         f"{report['unsafe_controls']}")
    if report["replacement_character_count"]:
        raise ValueError("source still contains replacement characters after "
                         "repair; refusing to process it")
    return book, {"repaired": len(repairs), "residual": residual,
                  "scripts": report["scripts"], "is_nfc": report["is_nfc"]}
```

Add these imports at the top of `app/three_pass_generate.py`, extending the
existing `source_normalization` import (which currently brings in
`strip_known_front_matter` at line 22) and adding the preflight import:

```python
from script_preflight import audit_unicode_text
from source_normalization import (neutralize_lossy_residue,
                                  repair_lossy_replacements)
```

Replace lines 1013-1018 with:

```python
    try:
        with open(args.input_file, encoding="utf-8") as fh:
            book = fh.read()
    except UnicodeDecodeError as exc:
        print(f"Error: {args.input_file} is not valid UTF-8: {exc}")
        sys.exit(1)
    book = fix_mojibake(book)
    book, _ = normalize_known_source_corruptions(book)
    if args.strip_front_matter:
        book, _ = strip_known_front_matter(book)
    try:
        book, unicode_report = prepare_source_text(book)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    if unicode_report["repaired"] or unicode_report["residual"]:
        print(f"Repaired {unicode_report['repaired']} destroyed character(s); "
              f"neutralized {unicode_report['residual']} unrecoverable one(s). "
              "The source file was not modified.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd app && ./env/bin/python -m unittest test_three_pass_unicode_gate test_source_normalization_lossy -v`
Expected: PASS, 16 tests total

- [ ] **Step 5: Verify the gate on the real damaged book**

```bash
cd app && ./env/bin/python -c "
from three_pass_generate import prepare_source_text
book = open('../ab_test_runtime/results/collect_all_20260723-040555/inputs/index18.txt', encoding='utf-8').read()
text, report = prepare_source_text(book)
print(report)
assert chr(0xFFFD) not in text
print('index18 now passes the gate')
"
```

Expected: `{'repaired': 6036, 'residual': 626, ...}` then
`index18 now passes the gate`.

- [ ] **Step 6: Run the existing three-pass suite for regressions**

Run: `cd app && ./env/bin/python -m unittest test_three_pass_generate -v`
Expected: PASS, no new failures. If a test fails, the import or the
lines 1013-1018 replacement broke something — fix before committing.

- [ ] **Step 7: Commit**

```bash
git add app/three_pass_generate.py app/test_three_pass_unicode_gate.py
git commit -m "Gate three-pass CLI on damaged source before any LLM call"
```

---

## Phase 2 — Failure telemetry

### Task 4: Enrich diagnostic failure records

**Files:**
- Modify: `app/generate_script.py:672-685` (the `attempt_observer` record)
- Modify: `app/three_pass_generate.py:891-896` and `:975-979` (the two
  `diagnostic_failures.append` sites)
- Test: `app/test_three_pass_telemetry.py` (create)

Context: failure records currently carry only `pass`, `entry`, `text_sha256`,
`text_preview`. Every causal finding in the analysis behind this plan had to be
recovered by grepping run logs. `attempt_observer` already receives
`finish_reason`, `prompt_tokens`, `completion_tokens`, `requested_max_tokens`
and `effective_max_tokens` and drops them.

`reasoning_tokens` is new: LM Studio returns it at
`response.usage.completion_tokens_details.reasoning_tokens`. It is the count of
thinking tokens, which are billed to `completion_tokens` but returned in
`message.reasoning_content`, not `message.content`.

- [ ] **Step 1: Write the failing test**

Create `app/test_three_pass_telemetry.py`:

```python
import unittest

from three_pass_generate import build_failure_record


class BuildFailureRecordTest(unittest.TestCase):

    def test_record_carries_causal_fields(self):
        record = build_failure_record(
            pass_name="attribute", index=7, text="He said hello.",
            last_attempt={"finish_reason": "length", "prompt_tokens": 2328,
                          "completion_tokens": 10000, "reasoning_tokens": 9987,
                          "effective_max_tokens": 10000, "attempt": 3,
                          "failure_codes": ["missing_json_array"]})
        self.assertEqual(record["pass"], "attribute")
        self.assertEqual(record["entry"], 7)
        self.assertEqual(record["finish_reason"], "length")
        self.assertEqual(record["reasoning_tokens"], 9987)
        self.assertEqual(record["effective_max_tokens"], 10000)
        self.assertEqual(record["attempt"], 3)
        self.assertEqual(record["reason"], "missing_json_array")
        self.assertEqual(record["text_preview"], "He said hello.")
        self.assertEqual(len(record["text_sha256"]), 64)

    def test_record_tolerates_missing_attempt_data(self):
        record = build_failure_record(
            pass_name="instruct", index=0, text="x", last_attempt=None)
        self.assertEqual(record["pass"], "instruct")
        self.assertIsNone(record["finish_reason"])
        self.assertIsNone(record["reasoning_tokens"])
        self.assertEqual(record["reason"], "unknown")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_three_pass_telemetry -v`
Expected: FAIL with `ImportError: cannot import name 'build_failure_record'`

- [ ] **Step 3: Write the implementation**

Add to `app/three_pass_generate.py`:

```python
def build_failure_record(pass_name, index, text, last_attempt=None):
    """Build a diagnostic failure record carrying why the batch failed.

    The earlier record shape (pass/entry/text_sha256/text_preview) said which
    entry failed but never why, so causes had to be recovered by grepping run
    logs. last_attempt is the final observed attempt dict from
    generate_script's attempt_observer, or None when no attempt was recorded.
    """
    attempt = last_attempt or {}
    codes = attempt.get("failure_codes") or []
    return {
        "pass": pass_name,
        "entry": index,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text_preview": text[:500],
        "reason": codes[0] if codes else (attempt.get("outcome") or "unknown"),
        "finish_reason": attempt.get("finish_reason"),
        "prompt_tokens": attempt.get("prompt_tokens"),
        "completion_tokens": attempt.get("completion_tokens"),
        "reasoning_tokens": attempt.get("reasoning_tokens"),
        "effective_max_tokens": attempt.get("effective_max_tokens"),
        "attempt": attempt.get("attempt"),
    }
```

In `app/generate_script.py`, add `reasoning_tokens` to the attempt record built
at lines 672-685. Immediately after the `usage = getattr(response, 'usage', None)`
line at 652, add:

```python
            usage_details = getattr(usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(usage_details, "reasoning_tokens", None)
```

Then inside the `attempt_record = {...}` dict at line 673, add this entry after
the `completion_tokens` line:

```python
                    "reasoning_tokens": reasoning_tokens,
```

Then replace the two `diagnostic_failures.append({...})` blocks in
`three_pass_generate.py`. The attribute site at lines 891-896 becomes:

```python
                            diagnostic_failures.append(build_failure_record(
                                "attribute", index, entry["text"],
                                last_attempt_for(index)))
```

and the instruct site at lines 975-979 becomes:

```python
                diagnostic_failures.append(build_failure_record(
                    "instruct", index, entry["text"], last_attempt_for(index)))
```

`last_attempt_for` is a closure over the most recent attempt record seen by the
`attempt_observer` for the batch being processed. Add it next to the existing
observer wiring:

```python
    last_attempts = {}

    def record_attempt(attempt):
        last_attempts["latest"] = attempt

    def last_attempt_for(_index):
        return last_attempts.get("latest")
```

and pass `record_attempt` as the `attempt_observer` argument wherever
`attribute_batch` and `annotate_batch` are invoked.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && ./env/bin/python -m unittest test_three_pass_telemetry -v`
Expected: PASS, 2 tests

- [ ] **Step 5: Run the existing suites for regressions**

Run: `cd app && ./env/bin/python -m unittest test_three_pass_generate test_llm_review_regressions -v`
Expected: PASS, no new failures

- [ ] **Step 6: Commit**

```bash
git add app/generate_script.py app/three_pass_generate.py app/test_three_pass_telemetry.py
git commit -m "Record why a three-pass batch failed, not just which entry"
```

---

### Task 5: Per-run manifest

**Files:**
- Modify: `app/three_pass_generate.py` (the checkpoint `save()` path)
- Test: `app/test_three_pass_telemetry.py`

- [ ] **Step 1: Write the failing test**

Append to the existing class in `app/test_three_pass_telemetry.py`:

```python
    def test_manifest_summarizes_a_run(self):
        from three_pass_generate import build_run_manifest
        manifest = build_run_manifest(
            model_name="qwen3.5-9b", thinking_mode="none",
            elapsed_s={"segment": 95.0, "attribute": 1059.0},
            counters={"truncations": 44, "subdivisions": 3,
                      "near_misses": 1, "context_rescues": 2},
            unicode_report={"repaired": 6036, "residual": 626},
            failures=[{"reason": "reasoning_overflow"},
                      {"reason": "missing_json_array"},
                      {"reason": "reasoning_overflow"}])
        self.assertEqual(manifest["model_name"], "qwen3.5-9b")
        self.assertEqual(manifest["thinking_mode"], "none")
        self.assertEqual(manifest["elapsed_s"]["attribute"], 1059.0)
        self.assertEqual(manifest["counters"]["truncations"], 44)
        self.assertEqual(manifest["failure_reasons"]["reasoning_overflow"], 2)
        self.assertEqual(manifest["failure_reasons"]["missing_json_array"], 1)
        self.assertEqual(manifest["unicode"]["residual"], 626)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_three_pass_telemetry -v`
Expected: FAIL with `ImportError: cannot import name 'build_run_manifest'`

- [ ] **Step 3: Write the implementation**

Add to `app/three_pass_generate.py`:

```python
def build_run_manifest(model_name, thinking_mode, elapsed_s, counters,
                       unicode_report, failures):
    """Summarize one three-pass run so results need no log grepping."""
    reasons = collections.Counter(
        failure.get("reason") or "unknown" for failure in failures)
    return {"model_name": model_name, "thinking_mode": thinking_mode,
            "elapsed_s": dict(elapsed_s), "counters": dict(counters),
            "unicode": dict(unicode_report),
            "failure_reasons": dict(reasons),
            "failure_count": len(failures)}
```

Add `import collections` to the imports at the top of the file if it is not
already present.

Write the manifest next to the existing checkpoint. In the `save()` helper,
after the checkpoint is written, add:

```python
        atomic_json_write(
            build_run_manifest(model_name, thinking_mode, elapsed_s, counters,
                               unicode_report, diagnostic_failures),
            args.output + ".threepass_manifest.json")
```

`counters` is a `collections.Counter()` created next to `elapsed_s` at the top
of the run and incremented at the existing subdivision, near-miss and
context-rescue sites. `thinking_mode` comes from Task 6.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && ./env/bin/python -m unittest test_three_pass_telemetry -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add app/three_pass_generate.py app/test_three_pass_telemetry.py
git commit -m "Write a per-run three-pass manifest alongside the checkpoint"
```

---

## Phase 3 — Reasoning-aware token budget

### Task 6: Reasoning allowance in the segment ceiling

**Files:**
- Modify: `app/generate_script.py:459-464` (`LLMGenParams`)
- Modify: `app/three_pass_generate.py:246-250` (`_call_segment`)
- Test: `app/test_reasoning_budget.py` (create)

Context: `three_pass_generate.py:246` currently reads:

```python
    source_words = max(1, len(chunk.split()))
    completion_ceiling = max(512, math.ceil(source_words * params.segment_output_ratio))
```

The comment above it says the bound exists "so a weak model cannot spend
10k-16k tokens expanding a ~1k-token source chunk." That intent is correct and
must be preserved for non-reasoning models. The defect is that the ceiling is
sized against *visible* output, while a reasoning model spends most of its
budget on invisible thinking, so the ceiling truncates mid-thought and returns
empty content.

- [ ] **Step 1: Write the failing test**

Create `app/test_reasoning_budget.py`:

```python
import unittest

from generate_script import LLMGenParams
from three_pass_generate import resolve_completion_ceiling


class ResolveCompletionCeilingTest(unittest.TestCase):

    def test_non_reasoning_model_keeps_todays_ceiling(self):
        params = LLMGenParams(segment_output_ratio=3.0)
        ceiling = resolve_completion_ceiling(
            source_words=400, params=params, reasoning_allowance=0)
        self.assertEqual(ceiling, 1200)

    def test_floor_still_applies(self):
        params = LLMGenParams(segment_output_ratio=3.0)
        ceiling = resolve_completion_ceiling(
            source_words=10, params=params, reasoning_allowance=0)
        self.assertEqual(ceiling, 512)

    def test_reasoning_allowance_is_added_on_top(self):
        params = LLMGenParams(segment_output_ratio=3.0)
        ceiling = resolve_completion_ceiling(
            source_words=400, params=params, reasoning_allowance=2048)
        self.assertEqual(ceiling, 1200 + 2048)

    def test_allowance_does_not_shrink_the_visible_budget(self):
        params = LLMGenParams(segment_output_ratio=3.0)
        without = resolve_completion_ceiling(
            source_words=400, params=params, reasoning_allowance=0)
        with_allowance = resolve_completion_ceiling(
            source_words=400, params=params, reasoning_allowance=5000)
        self.assertGreater(with_allowance, without)


class ReasoningAllowanceTest(unittest.TestCase):

    def test_cold_start_uses_the_floor(self):
        from three_pass_generate import ReasoningAllowance
        allowance = ReasoningAllowance()
        self.assertEqual(allowance.current(), 0)
        allowance.observe(1500)
        self.assertGreaterEqual(allowance.current(), 1024)

    def test_non_reasoning_model_stays_at_zero(self):
        from three_pass_generate import ReasoningAllowance
        allowance = ReasoningAllowance()
        for _ in range(10):
            allowance.observe(0)
        self.assertEqual(allowance.current(), 0)

    def test_allowance_tracks_p95_of_observations(self):
        from three_pass_generate import ReasoningAllowance
        allowance = ReasoningAllowance()
        for value in list(range(1000, 3000, 100)) + [9000]:
            allowance.observe(value)
        self.assertGreaterEqual(allowance.current(), 2800)
        self.assertLessEqual(allowance.current(), 9000)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_reasoning_budget -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_completion_ceiling'`

- [ ] **Step 3: Write the implementation**

Add to `app/three_pass_generate.py`:

```python
REASONING_ALLOWANCE_FLOOR = 1024


class ReasoningAllowance:
    """Track a model's observed thinking-token cost.

    Reasoning tokens bill to completion_tokens but are returned in
    message.reasoning_content, so a ceiling sized on visible output truncates a
    reasoning model mid-thought. A model that never reports reasoning_tokens
    keeps an allowance of zero, so non-reasoning behaviour is unchanged.
    """

    def __init__(self):
        self._observations = []

    def observe(self, reasoning_tokens):
        if reasoning_tokens:
            self._observations.append(int(reasoning_tokens))

    def current(self):
        if not self._observations:
            return 0
        ordered = sorted(self._observations)
        index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return max(REASONING_ALLOWANCE_FLOOR, ordered[index])


def resolve_completion_ceiling(source_words, params, reasoning_allowance=0):
    """Bound segmentation output, leaving room for invisible reasoning.

    The visible-output bound is unchanged from the original: it stops a weak
    model spending 10k-16k tokens expanding a ~1k-token chunk. The reasoning
    allowance is added on top rather than carved out of it, so a reasoning
    model gets the same visible budget as everyone else.
    """
    visible = max(512, math.ceil(max(1, source_words) * params.segment_output_ratio))
    return visible + max(0, int(reasoning_allowance))
```

Then replace lines 246-250 of `_call_segment` with:

```python
    source_words = max(1, len(chunk.split()))
    completion_ceiling = resolve_completion_ceiling(
        source_words, params, reasoning_allowance=params.reasoning_allowance)
```

Add `reasoning_allowance: int = 0` to `LLMGenParams` in
`app/generate_script.py`, after `presegment_quotes` at line 464.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && ./env/bin/python -m unittest test_reasoning_budget -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Wire the allowance into the run**

Defining `ReasoningAllowance` is not enough — nothing yet feeds it. In
`three_pass_generate.py`, create one allowance per run next to `elapsed_s`:

```python
    reasoning_allowance = ReasoningAllowance()
```

Extend the `record_attempt` observer added in Task 4 so every attempt updates
it, and so the next call is sized from what the model has actually shown:

```python
    def record_attempt(attempt):
        last_attempts["latest"] = attempt
        reasoning_allowance.observe(attempt.get("reasoning_tokens"))
        params.reasoning_allowance = reasoning_allowance.current()
```

`params` is the `LLMGenParams` instance for the run. Because `current()`
returns 0 until a non-zero `reasoning_tokens` is observed, a non-reasoning
model never changes behaviour.

- [ ] **Step 6: Verify the allowance climbs only for reasoning models**

```bash
cd app && ./env/bin/python -c "
from three_pass_generate import ReasoningAllowance
gemma, qwen = ReasoningAllowance(), ReasoningAllowance()
for _ in range(50): gemma.observe(None)
for value in (1500, 2200, 3100, 2800, 2000): qwen.observe(value)
print('gemma allowance:', gemma.current())
print('qwen allowance:', qwen.current())
assert gemma.current() == 0
assert qwen.current() >= 1024
print('OK')
"
```

Expected: `gemma allowance: 0`, `qwen allowance: 3100`, then `OK`.

- [ ] **Step 7: Commit**

```bash
git add app/generate_script.py app/three_pass_generate.py app/test_reasoning_budget.py
git commit -m "Size the segment ceiling for invisible reasoning tokens"
```

---

### Task 7: Reasoning-overflow circuit break

**Files:**
- Modify: `app/generate_script.py:687-703` (the `finish_reason == "length"` branch)
- Test: `app/test_reasoning_budget.py`

Context: today a truncation escalates the budget repeatedly and then the caller
subdivides the batch. For a reasoning model this is useless — halving the input
does not shrink the reasoning preamble — and it produced 735 truncations and
10.5 wasted hours. Per CLAUDE.md Rule 10, the replacement policy is decided
once and applied identically on every attempt: escalate **once**, then fail.

- [ ] **Step 1: Write the failing test**

Append to `app/test_reasoning_budget.py`:

```python
class ReasoningOverflowTest(unittest.TestCase):

    def test_empty_content_with_reasoning_is_overflow(self):
        from generate_script import classify_length_finish
        verdict = classify_length_finish(
            content="", reasoning_tokens=9987, already_escalated=False)
        self.assertEqual(verdict, "escalate_once")

    def test_second_overflow_fails_fast(self):
        from generate_script import classify_length_finish
        verdict = classify_length_finish(
            content="", reasoning_tokens=9987, already_escalated=True)
        self.assertEqual(verdict, "reasoning_overflow")

    def test_truncated_visible_output_is_not_overflow(self):
        from generate_script import classify_length_finish
        verdict = classify_length_finish(
            content='[{"n": 0, "speaker": "ARARAGI"}', reasoning_tokens=0,
            already_escalated=False)
        self.assertEqual(verdict, "truncated_output")

    def test_non_reasoning_model_never_reports_overflow(self):
        from generate_script import classify_length_finish
        verdict = classify_length_finish(
            content="", reasoning_tokens=None, already_escalated=True)
        self.assertEqual(verdict, "truncated_output")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_reasoning_budget -v`
Expected: FAIL with `ImportError: cannot import name 'classify_length_finish'`

- [ ] **Step 3: Write the implementation**

Add to `app/generate_script.py`, next to `get_quality_retry_policy` at line 479:

```python
def classify_length_finish(content, reasoning_tokens, already_escalated):
    """Decide what a finish_reason=length response means.

    Returns "reasoning_overflow" when a reasoning model spent its whole budget
    thinking and emitted no visible content, and has already been given one
    larger budget. Returns "escalate_once" for the first such response, and
    "truncated_output" for ordinary visible truncation, which keeps its existing
    retry and subdivision handling. Rule 10: one policy, applied the same way on
    every attempt.
    """
    if reasoning_tokens and not (content or "").strip():
        return "reasoning_overflow" if already_escalated else "escalate_once"
    return "truncated_output"
```

Then rewrite the `if finish_reason == "length":` branch at lines 687-703 to use
it:

```python
            if finish_reason == "length":
                verdict = classify_length_finish(
                    text, reasoning_tokens, reasoning_escalated)
                if verdict == "reasoning_overflow":
                    print("  Reasoning overflow: the model spent its whole "
                          "budget thinking and returned no content. Failing "
                          "this batch instead of subdividing.")
                    if attempt_record is not None:
                        attempt_record["outcome"] = "response_rejected"
                        attempt_record["failure_codes"] = ["reasoning_overflow"]
                    return []
                print(f"  WARNING: Response was truncated (hit effective max_tokens={effective_max}).")
                if attempt < max_retries:
                    next_max = get_next_retry_max_tokens(
                        requested_max, "token_truncated", params.hard_max_tokens)
                    next_effective = get_effective_max_tokens(
                        next_max, params.context_length, base_messages,
                        params.hard_max_tokens, scale_to_context=False)
                    if next_effective > effective_max:
                        print(f"  Token budget: requested={requested_max}, effective={effective_max}, "
                              f"next_requested={next_max}, next_effective={next_effective}")
                        requested_max = next_max
                        retry_feedback = None
                        truncation_retry_available = True
                        if verdict == "escalate_once":
                            reasoning_escalated = True
                    else:
                        print(f"  Token escalation exhausted: effective budget cannot grow "
                              f"beyond {effective_max} in the loaded context.")
```

Initialize `reasoning_escalated = False` alongside the other per-call state
(next to `truncation_retry_available`) before the retry loop begins.

Per CLAUDE.md Rule 9, this adds a circuit break; it does not weaken the
existing retry or subdivision paths, which still handle ordinary visible
truncation exactly as before.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd app && ./env/bin/python -m unittest test_reasoning_budget -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Verify gemma's behaviour is unchanged**

Run: `cd app && ./env/bin/python -m unittest test_llm_review_regressions test_three_pass_generate -v`
Expected: PASS, no new failures. These suites cover the non-reasoning retry
paths; any failure means the circuit break leaked into ordinary truncation
handling.

- [ ] **Step 6: Commit**

```bash
git add app/generate_script.py app/test_reasoning_budget.py
git commit -m "Fail fast on reasoning overflow instead of subdividing"
```

---

## Phase 4 — Thinking-on vs thinking-off probe

### Task 8: Plumb `reasoning_effort`

**Files:**
- Modify: `app/generate_script.py:459-464` and `:635-641` (`extra_body`)
- Test: `app/test_reasoning_budget.py`

Context: probes against this LM Studio build established that
`reasoning_effort: "none"` is the only method that disables thinking.
`chat_template_kwargs.enable_thinking=false` and a `/no_think` prompt suffix
are both silently ignored. Measured: 108 completion tokens (87 reasoning) at
baseline versus 19 completion tokens (0 reasoning) with `reasoning_effort`.

- [ ] **Step 1: Write the failing test**

Append to `app/test_reasoning_budget.py`:

```python
class ReasoningEffortPlumbingTest(unittest.TestCase):

    def test_reasoning_effort_reaches_extra_body(self):
        from generate_script import build_extra_body, LLMGenParams
        params = LLMGenParams(top_k=40, reasoning_effort="none")
        body = build_extra_body(params)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["top_k"], 40)

    def test_unset_reasoning_effort_is_omitted(self):
        from generate_script import build_extra_body, LLMGenParams
        body = build_extra_body(LLMGenParams(top_k=40))
        self.assertNotIn("reasoning_effort", body)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_reasoning_budget -v`
Expected: FAIL with `ImportError: cannot import name 'build_extra_body'`

- [ ] **Step 3: Write the implementation**

Add `reasoning_effort: str = None` to `LLMGenParams` after the
`reasoning_allowance` field added in Task 6.

Add to `app/generate_script.py`:

```python
def build_extra_body(params):
    """Collect non-standard sampling options for the OpenAI-compatible call."""
    return {k: v for k, v in {
        "top_k": params.top_k,
        "min_p": params.min_p,
        "banned_tokens": params.banned_tokens if params.banned_tokens else None,
        "reasoning_effort": params.reasoning_effort,
    }.items() if v is not None}
```

Replace the inline `extra_body={...}` dict at lines 635-641 with:

```python
                extra_body=build_extra_body(params),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && ./env/bin/python -m unittest test_reasoning_budget -v`
Expected: PASS, 13 tests

- [ ] **Step 5: Commit**

```bash
git add app/generate_script.py app/test_reasoning_budget.py
git commit -m "Plumb reasoning_effort through to the LLM call"
```

---

### Task 9: Disagreement sampler

**Files:**
- Create: `app/compare_attribution_arms.py`
- Test: `app/test_compare_attribution_arms.py` (create)

- [ ] **Step 1: Write the failing test**

Create `app/test_compare_attribution_arms.py`:

```python
import unittest

from compare_attribution_arms import find_disagreements, sample_disagreements


class FindDisagreementsTest(unittest.TestCase):

    def test_matching_arms_have_no_disagreements(self):
        arm_a = [{"speaker": "ARARAGI", "text": "Hi"},
                 {"speaker": "HACHIKUJI", "text": "Bye"}]
        self.assertEqual(find_disagreements(arm_a, list(arm_a)), [])

    def test_differing_speaker_is_reported(self):
        arm_a = [{"speaker": "ARARAGI", "text": "Hi"}]
        arm_b = [{"speaker": "HANEKAWA", "text": "Hi"}]
        found = find_disagreements(arm_a, arm_b)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["index"], 0)
        self.assertEqual(found[0]["arm_a"], "ARARAGI")
        self.assertEqual(found[0]["arm_b"], "HANEKAWA")
        self.assertEqual(found[0]["text"], "Hi")

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            find_disagreements([{"speaker": "A", "text": "x"}], [])

    def test_null_entries_are_skipped(self):
        arm_a = [None, {"speaker": "ARARAGI", "text": "Hi"}]
        arm_b = [None, {"speaker": "HANEKAWA", "text": "Hi"}]
        found = find_disagreements(arm_a, arm_b)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["index"], 1)


class SampleDisagreementsTest(unittest.TestCase):

    def test_sample_is_deterministic_for_a_seed(self):
        rows = [{"index": i, "arm_a": "A", "arm_b": "B", "text": str(i)}
                for i in range(200)]
        first = sample_disagreements(rows, size=50, seed=7)
        second = sample_disagreements(rows, size=50, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 50)

    def test_sample_smaller_than_size_returns_all(self):
        rows = [{"index": 0, "arm_a": "A", "arm_b": "B", "text": "x"}]
        self.assertEqual(len(sample_disagreements(rows, size=50, seed=7)), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && ./env/bin/python -m unittest test_compare_attribution_arms -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'compare_attribution_arms'`

- [ ] **Step 3: Write the implementation**

Create `app/compare_attribution_arms.py`:

```python
"""Compare two three-pass attribution arms and sample their disagreements.

Structural metrics cannot say which arm is correct, so the manual scoring pass
should look only at entries where the arms actually disagree.
"""

import argparse
import json
import random


def find_disagreements(arm_a, arm_b):
    """Return entries where two arms assigned different speakers."""
    if len(arm_a) != len(arm_b):
        raise ValueError(
            f"arms have different entry counts: {len(arm_a)} vs {len(arm_b)}")
    rows = []
    for index, (left, right) in enumerate(zip(arm_a, arm_b)):
        if not left or not right:
            continue
        if left.get("speaker") != right.get("speaker"):
            rows.append({"index": index, "arm_a": left.get("speaker"),
                         "arm_b": right.get("speaker"),
                         "text": left.get("text", "")[:300]})
    return rows


def sample_disagreements(rows, size=50, seed=7):
    """Draw a reproducible random sample for hand-scoring."""
    if len(rows) <= size:
        return list(rows)
    return random.Random(seed).sample(rows, size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm_a", help="checkpoint or result JSON for arm A")
    parser.add_argument("arm_b", help="checkpoint or result JSON for arm B")
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", default="disagreements.json")
    args = parser.parse_args()

    def load(path):
        data = json.load(open(path, encoding="utf-8"))
        return data.get("named") or data.get("entries") or data

    rows = find_disagreements(load(args.arm_a), load(args.arm_b))
    sample = sample_disagreements(rows, args.size, args.seed)
    json.dump({"total_entries_compared": len(load(args.arm_a)),
               "disagreement_count": len(rows), "sample": sample},
              open(args.output, "w", encoding="utf-8"), indent=2)
    print(f"{len(rows)} disagreements; wrote {len(sample)} sampled to {args.output}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && ./env/bin/python -m unittest test_compare_attribution_arms -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add app/compare_attribution_arms.py app/test_compare_attribution_arms.py
git commit -m "Add attribution-arm disagreement sampler for manual scoring"
```

---

### Task 10: Run the probe

**Files:**
- Create: `ab_test_runtime/probe_thinking/run_probe.sh` (this tree is gitignored;
  do not commit it)

Prerequisite: LM Studio running with
`qwen3.5-9b-uncensored-hauhaucs-aggressive` loaded at 32768 context, parallel 1.
Verify with `/home/fakemitch/.lmstudio/bin/lms ps` before starting. Confirm no
other three-pass process is running with
`pgrep -af three_pass_generate.py` — it must print nothing.

- [ ] **Step 1: Run the thinking-off arm**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app
mkdir -p ../ab_test_runtime/probe_thinking/off
./env/bin/python three_pass_generate.py \
  ../ab_test_runtime/results/collect_all_20260723-040555/inputs/mushoku16.txt \
  --reasoning-effort none --collect-all-failures \
  --output ../ab_test_runtime/probe_thinking/off/result.json \
  2>&1 | tee ../ab_test_runtime/probe_thinking/off/run.log
```

This requires a `--reasoning-effort` argument. Add it to the parser alongside
the existing `--collect-all-failures` flag at line 1006:

```python
    parser.add_argument("--reasoning-effort", default=None,
                        help="Pass through to the model (e.g. 'none' to "
                             "disable thinking on a reasoning model).")
```

Then set it on the `LLMGenParams` instance immediately after that instance is
constructed in `main()` (search for `LLMGenParams(` in
`three_pass_generate.py` — there is one construction site in `main`):

```python
    params.reasoning_effort = args.reasoning_effort
    thinking_mode = args.reasoning_effort or "default"
```

`thinking_mode` is the value Task 5's manifest records, so run it before the
first `save()` call.

Expected: completes with far fewer truncations than the historical run. The
manifest at `off/result.json.threepass_manifest.json` records
`thinking_mode: "none"`.

- [ ] **Step 2: Run the thinking-on arm**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app
mkdir -p ../ab_test_runtime/probe_thinking/on
./env/bin/python three_pass_generate.py \
  ../ab_test_runtime/results/collect_all_20260723-040555/inputs/mushoku16.txt \
  --collect-all-failures \
  --output ../ab_test_runtime/probe_thinking/on/result.json \
  2>&1 | tee ../ab_test_runtime/probe_thinking/on/run.log
```

Expected: completes without the escalation storm, because Task 6's allowance
sizes the ceiling for reasoning and Task 7 fails fast rather than subdividing.

- [ ] **Step 3: Compare the arms**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app
./env/bin/python -c "
import json
for arm in ('off', 'on'):
    m = json.load(open(f'../ab_test_runtime/probe_thinking/{arm}/result.json.threepass_manifest.json'))
    print(arm, m['elapsed_s'], m['counters'], m['failure_reasons'])
"
```

Expected: a per-arm table of wall time, truncation and subdivision counts, and
failure reasons — no log grepping required. This is what Phase 2 was for.

- [ ] **Step 4: Collect the structural quality metrics**

The spec calls for `script_preflight`'s existing findings per arm, not just
speed. Run:

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app
./env/bin/python -c "
import collections, json
from script_preflight import is_possible_misattributed_narration
for arm in ('off', 'on'):
    path = f'../ab_test_runtime/probe_thinking/{arm}/result.json.threepass_checkpoint.json'
    entries = [e for e in json.load(open(path))['named'] if e]
    speakers = collections.Counter(e.get('speaker') for e in entries)
    misattributed = sum(1 for e in entries
                        if is_possible_misattributed_narration(
                            e.get('text', ''), e.get('speaker')))
    print(f'{arm:4} entries={len(entries):5} distinct_speakers={len(speakers):3} '
          f'misattributed_narration={misattributed:4} singletons='
          f'{sum(1 for _, n in speakers.items() if n == 1)}')
"
```

Expected: a two-row table. Interpretation guide — a much larger
`distinct_speakers` or `singletons` count in one arm means that arm is
inventing speaker names, which is an attribution-quality failure that raw speed
numbers would hide.

- [ ] **Step 5: Sample the disagreements**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app
./env/bin/python compare_attribution_arms.py \
  ../ab_test_runtime/probe_thinking/off/result.json.threepass_checkpoint.json \
  ../ab_test_runtime/probe_thinking/on/result.json.threepass_checkpoint.json \
  --output ../ab_test_runtime/probe_thinking/disagreements.json
```

Expected: prints the disagreement count and writes 50 sampled entries.

- [ ] **Step 6: Report, do not decide**

Present to the user: the per-arm speed and structural table, the disagreement
rate, and the path to the 50-entry sample. The decision the probe informs —
whether the remaining matrix runs with `reasoning_effort: "none"` or with a
reasoning allowance — is the user's, and depends on their hand-scoring of the
sample. Do not start the full matrix without explicit approval.

---

## Notes for the implementer

- `docs/superpowers/` is listed in `.gitignore`, but the files already there
  are tracked. Use `git add -f` for anything new under it.
- `ab_test_runtime/results/` and `ab_test_runtime/probe_thinking/` are ignored
  and must stay uncommitted — they hold multi-megabyte checkpoints.
- The paused matrix's `owarimonogatari3` checkpoint (stage `attribute`,
  1,632/3,901 entries) is preserved. Do not delete or overwrite anything under
  `ab_test_runtime/results/collect_all_20260723-040555/`.
- Per CLAUDE.md Rule 8, report skipped tests as skipped. Per Rule 7, checkpoint
  after each task.
