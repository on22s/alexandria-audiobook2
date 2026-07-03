#!/usr/bin/env python3
"""
Alexandria Compare — diff a metadata.jsonl against an original EPUB or text file.
Shows each mismatch for your review. You approve every change; nothing is
written automatically.

The alignment primitives (fuzzy matching, source loading, proper-noun lexicon,
trim/extend boundary logic) live in alexandria_alignment.py so the preparer
script can share them. This file owns the compare-specific layer: the merge
preview that re-applies LLM prosody markers onto source spelling, the
interactive review loop, the checkpoint/log handling, and the targeted-reset
flags that let you undo specific decisions without losing a long session.
"""

import os
import sys
import re
import json
import difflib
import argparse
from pathlib import Path

# Shared alignment primitives (source loading + cleanups, proper-noun lexicon,
# fuzzy alignment, trim/extend, all threshold tiers, char_sim cache).
# Anything compare needs from this module is imported here; `_alignment` is
# kept as a module alias so main() can mutate the per-book lexicon attribute
# (`_alignment._PROPER_NOUNS = ...`) in one place that all the helpers see.
import alexandria_alignment as _alignment
from alexandria_alignment import (
    _SMART_QUOTES,
    _FUZZY_KEEP_THRESHOLD,
    _OCR_DIGIT_GLITCH,
    _DIACRITIC_REJOIN,
    _DIACRITIC_REJOIN_TAIL,
    load_source,
    normalize,
    to_words,
    _build_proper_nouns,
    find_best_match,
    find_anchor_position,
    auto_anchor,
    realign,
    trim_span_to_alignment,
    estimate_alignment_quality,
    find_text_in_source,
    _ratio,
)

# ── ANSI colours ──────────────────────────────────────────────────────────────
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"
SEP    = "─" * 72


# ── JSONL loader ──────────────────────────────────────────────────────────────
def load_jsonl(path: str) -> list:
    entries = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if s:
                entries.append(json.loads(s))
    return entries


# ── Parse + merge (compare-specific — operates on LLM annotation markers) ────
def parse_annotated_tokens(text: str) -> list:
    """
    Parse a TTS-annotated string into tokens carrying their prosody markers.
    Each token = {'word', 'leading_pause', 'trailing_pause', 'emphasized'}.
    Used by merge_annotations_with_source to preserve markers while replacing
    the actual words with the cleaner source text.
    """
    tokens = []
    pending_leading = ''
    # Expand multi-word emphasis spans ("*Trull Sengar*") into per-word
    # emphasis ("*Trull* *Sengar*") so the whitespace tokenizer below sees
    # each emphasized word as a self-contained *word* token. Without this,
    # *Trull splits off (starts with * but doesn't end with one) and so
    # does Sengar*, and the emphasis is lost on both.
    text = re.sub(
        r'\*([^*]+)\*',
        lambda m: ' '.join(f'*{w}*' for w in m.group(1).split()) or m.group(0),
        text,
    )
    # LLM/ASR output sometimes joins words with `...` instead of spaces
    # ("YOU...*DERONDL*...THE..."). Split those dot-runs out so the
    # whitespace tokenizer below sees each word and each pause separately —
    # otherwise the whole chain collapses into one garbled token and every
    # *emphasis* marker in it is silently dropped from the merge.
    text = re.sub(r'\.{3,}', r' \g<0> ', text)
    for raw in text.split():
        # Pure pause token (e.g. "..." or "....") — attach to NEXT word
        if re.fullmatch(r'\.{3,}', raw):
            if len(raw) > len(pending_leading):
                pending_leading = raw
            continue

        leading = pending_leading
        pending_leading = ''

        # Leading dots fused to the start of this token
        m = re.match(r'^(\.{3,})', raw)
        if m:
            d = m.group(1)
            leading = d if len(d) > len(leading) else leading
            raw = raw[len(d):]

        # Trailing dots fused to the end
        trailing = ''
        m = re.search(r'(\.{3,})$', raw)
        if m:
            trailing = m.group(1)
            raw = raw[:-len(m.group(1))]

        # LLM sometimes attaches sentence punctuation directly to the closing
        # emphasis marker ("*HULLO*!", "*PLEASANT*,", "*DURONDYL*?"). That
        # hides the closing * inside the token, so the endswith('*') check
        # below misses the emphasis. Peel any trailing non-word junk off a
        # clean *…* group; source punctuation comes back naturally through
        # the source spelling during merge.
        m = re.match(r'^(\*[^*\s]+\*)([\W_]+)$', raw)
        if m:
            raw = m.group(1)

        # Emphasis (*word*)
        emphasized = False
        if raw.startswith('*') and raw.endswith('*') and len(raw) >= 3:
            emphasized = True
            raw = raw[1:-1]

        # Strip surrounding non-word punctuation for matching
        word_for_match = re.sub(r"[^\w']", '', raw.translate(_SMART_QUOTES).lower())
        if not word_for_match:
            if trailing:
                pending_leading = trailing
            continue

        tokens.append({
            'word':           word_for_match,
            'leading_pause':  leading,
            'trailing_pause': trailing,
            'emphasized':     emphasized,
        })
    # Trailing `...` after the final word has nowhere to attach as
    # leading_pause — flush it onto the last token's trailing_pause so
    # terminal pauses aren't lost.
    if pending_leading and tokens:
        if len(pending_leading) > len(tokens[-1]['trailing_pause']):
            tokens[-1]['trailing_pause'] = pending_leading
    return tokens


def merge_annotations_with_source(annotated_text: str, source_words: list) -> str:
    """
    Return a new string using `source_words` (correct content/spelling/punct)
    while preserving the LLM's prosody markers (...., *word*) from
    `annotated_text` wherever words align.

    This is the heart of the [m]erge option — it lets users fix ASR errors
    without throwing away the prosody hints that keep TTS output expressive.
    """
    if not source_words:
        return ''

    annotated = parse_annotated_tokens(annotated_text)
    annot_words = [t['word'] for t in annotated]

    src_match = [
        re.sub(r"[^\w']", '', w.translate(_SMART_QUOTES).lower())
        for w in source_words
    ]

    # Word-level alignment between annotated and source.
    #
    # 'equal' opcodes are the easy case — chunk and source agree exactly.
    # 'replace' opcodes are where ASR mistranscribes a word (chunk "amander"
    # vs source "Amanda") or compounds get split differently (chunk "first"
    # "hand" vs source "firsthand"). Without same-position fuzzy mapping
    # inside 'replace' blocks, those chunk words' prosody markers (*emphasis*
    # and pauses) get dropped on the floor and the merge silently produces
    # plain source text, indistinguishable from [a]ccept original.
    sm = difflib.SequenceMatcher(None, annot_words, src_match, autojunk=False)
    src_to_tok = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                src_to_tok[j1 + k] = annotated[i1 + k]
        elif tag == 'replace':
            for k in range(min(i2 - i1, j2 - j1)):
                sim = difflib.SequenceMatcher(
                    None, annot_words[i1 + k], src_match[j1 + k], autojunk=False
                ).ratio()
                if sim >= _FUZZY_KEEP_THRESHOLD:
                    src_to_tok[j1 + k] = annotated[i1 + k]
            # Imbalanced replace: ASR may mash a multi-word name into one
            # token ("Tralesengar" vs "Trull Sengar") or split one into many
            # ("nine hundred forty third" vs "943rd"). The 1↔1 pass above
            # only checks paired positions, so the unpaired side gets no
            # emphasis. Try a concatenation match on whichever side is
            # longer and, if it clears the fuzzy bar, broadcast the chunk
            # emphasis across the matched source positions.
            chunk_n, src_n = i2 - i1, j2 - j1
            if chunk_n == 1 and src_n > 1 and j1 not in src_to_tok:
                cat = ''.join(src_match[j1:j2])
                sim = difflib.SequenceMatcher(
                    None, annot_words[i1], cat, autojunk=False
                ).ratio()
                if sim >= _FUZZY_KEEP_THRESHOLD:
                    for k in range(src_n):
                        src_to_tok[j1 + k] = annotated[i1]
            elif src_n == 1 and chunk_n > 1 and j1 not in src_to_tok:
                cat = ''.join(annot_words[i1:i2])
                sim = difflib.SequenceMatcher(
                    None, cat, src_match[j1], autojunk=False
                ).ratio()
                if sim >= _FUZZY_KEEP_THRESHOLD:
                    # Prefer the first emphasized chunk token so the
                    # emphasis carries onto the single source word.
                    chosen = next(
                        (annotated[i1 + k] for k in range(chunk_n)
                         if annotated[i1 + k]['emphasized']),
                        annotated[i1],
                    )
                    src_to_tok[j1] = chosen

    parts = []
    for i, src_word in enumerate(source_words):
        tok = src_to_tok.get(i)
        if tok:
            if tok['leading_pause']:
                parts.append(tok['leading_pause'])
            if tok['emphasized']:
                # Wrap the word's *letters* in asterisks, leaving any
                # surrounding punctuation outside: "library." -> "*library*."
                m = re.match(r"^(\W*)(.*?)(\W*)$", src_word)
                if m and m.group(2):
                    parts.append(f"{m.group(1)}*{m.group(2)}*{m.group(3)}")
                else:
                    parts.append(f"*{src_word}*")
            else:
                parts.append(src_word)
            if tok['trailing_pause']:
                parts.append(tok['trailing_pause'])
        else:
            parts.append(src_word)

    return ' '.join(parts)


# ── Diff display ──────────────────────────────────────────────────────────────
def color_diff(a: list, b: list) -> tuple:
    """Return (a_colored_str, b_colored_str) with ANSI markup."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    a_out, b_out = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a_seg = ' '.join(a[i1:i2])
        b_seg = ' '.join(b[j1:j2])
        if tag == 'equal':
            a_out.append(a_seg)
            b_out.append(b_seg)
        elif tag == 'replace':
            a_out.append(RED + a_seg + RESET)
            b_out.append(GREEN + b_seg + RESET)
        elif tag == 'delete':
            a_out.append(RED + a_seg + RESET)
        elif tag == 'insert':
            b_out.append(GREEN + b_seg + RESET)
    return ' '.join(a_out), ' '.join(b_out)

def fmt_time(s: float) -> str:
    m, sec = divmod(int(s), 60)
    h, m   = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"

# ── Checkpoint ────────────────────────────────────────────────────────────────
def checkpoint_path(jsonl_path: str) -> Path:
    p = Path(jsonl_path)
    return p.with_name(f".{p.stem}_compare_progress.json")

def load_checkpoint(jsonl_path: str) -> dict:
    cp = checkpoint_path(jsonl_path)
    if cp.exists():
        return json.loads(cp.read_text())
    return {"decisions": {}, "cursor": 0}

def save_checkpoint(jsonl_path: str, decisions: dict, cursor: int):
    cp = checkpoint_path(jsonl_path)
    cp.write_text(json.dumps({"decisions": decisions, "cursor": cursor}, indent=2))

# ── Review log ────────────────────────────────────────────────────────────────
# Records every entry the user manually reviewed (auto-approved entries are
# skipped to keep the log signal-rich). The intent is to feed this back into
# the script so common edit patterns can be fixed at the source — for example,
# if every [e]dit reshapes the merge preview the same way, the merge function
# can be improved instead.
def review_log_path(output_path: str) -> Path:
    p = Path(output_path)
    return p.with_name(p.stem + '_review_log.jsonl')

def log_decision(log_path: Path, record: dict):
    """Append one decision as a JSON line. Best-effort: a logging failure
    must never abort the user's review session."""
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception:
        pass

def remove_log_entries(log_path: Path, indices: set) -> int:
    """Rewrite the review log without records whose entry_idx is in `indices`.
    Returns the count removed. Used by in-session [u]ndo and by --reset-* flags."""
    if not log_path.exists():
        return 0
    kept, removed = [], 0
    with open(log_path, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get('entry_idx') in indices:
                    removed += 1
                    continue
            except json.JSONDecodeError:
                pass
            kept.append(line)
    with open(log_path, 'w', encoding='utf-8') as f:
        for line in kept:
            f.write(line + '\n')
    return removed

def find_last_manual_idx(decisions: dict):
    """Highest entry index that was decided manually (not auto-approved and
    not a pre-anchor auto-keep). Returns None if there's nothing to undo."""
    for k in sorted((int(k) for k in decisions.keys()), reverse=True):
        d = decisions[str(k)]
        if not d.get('auto') and not d.get('pre_anchor'):
            return k
    return None

# ── Targeted reset ────────────────────────────────────────────────────────────
# `--reset` wipes everything; the flags below let the user undo specific
# entries without losing the rest of a long review session. Triggered from
# main() before any review work happens — the script does the reset, prints
# what changed, and exits.
def parse_reset_spec(reset_entry: str, reset_from: int, reset_range: str,
                     total_entries: int) -> set:
    """Collect all entry indices targeted by --reset-entry / --reset-from /
    --reset-range into a single sorted set. Returns empty set if none given."""
    indices = set()
    if reset_entry:
        for tok in reset_entry.split(','):
            tok = tok.strip()
            if not tok:
                continue
            try:
                indices.add(int(tok))
            except ValueError:
                sys.exit(f"--reset-entry: bad index {tok!r}")
    if reset_from is not None:
        if reset_from < 0:
            sys.exit(f"--reset-from: index must be ≥ 0, got {reset_from}")
        for i in range(reset_from, total_entries):
            indices.add(i)
    if reset_range:
        try:
            lo_s, hi_s = reset_range.split(':', 1)
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            sys.exit(f"--reset-range: expected N:M, got {reset_range!r}")
        if lo > hi:
            sys.exit(f"--reset-range: lo > hi ({lo} > {hi})")
        for i in range(lo, hi + 1):
            indices.add(i)
    return indices

def apply_targeted_reset(
    jsonl_path: str,
    output_path: str,
    log_path: Path,
    indices: set,
    also_clear_log: bool,
):
    """Restore each indexed entry's text from metadata.jsonl, drop its
    checkpoint decision, and optionally remove matching review-log records.
    The cursor in the checkpoint is rewound to the smallest reset index so the
    next normal run re-aligns from there (otherwise the cursor could be ahead
    of source content that the reset entries should have consumed)."""
    with open(jsonl_path, encoding='utf-8') as f:
        orig_lines = f.readlines()

    out_path = Path(output_path)
    if out_path.exists():
        with open(out_path, encoding='utf-8') as f:
            cur_lines = f.readlines()
    else:
        cur_lines = orig_lines.copy()

    restored, out_of_range = 0, []
    for idx in sorted(indices):
        if 0 <= idx < len(orig_lines) and idx < len(cur_lines):
            cur_lines[idx] = orig_lines[idx]
            restored += 1
        else:
            out_of_range.append(idx)

    with open(out_path, 'w', encoding='utf-8') as f:
        f.writelines(cur_lines)

    # Drop decisions; rewind cursor to the lowest reset index so the next
    # run re-anchors before that point instead of skipping ahead.
    cp_path = checkpoint_path(jsonl_path)
    popped = 0
    if cp_path.exists():
        cp = json.loads(cp_path.read_text())
        decisions = cp.get('decisions', {})
        for idx in indices:
            if decisions.pop(str(idx), None) is not None:
                popped += 1
        if indices:
            min_idx = min(indices)
            # cursor_after of the entry just before min_idx, if present
            prev = decisions.get(str(min_idx - 1)) if min_idx > 0 else None
            cp['cursor'] = prev['cursor_after'] if (prev and 'cursor_after' in prev) else 0
        cp_path.write_text(json.dumps(cp, indent=2, ensure_ascii=False))

    log_removed = 0
    if also_clear_log and log_path.exists():
        kept = []
        with open(log_path, encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get('entry_idx') in indices:
                        log_removed += 1
                        continue
                except json.JSONDecodeError:
                    pass
                kept.append(line)
        with open(log_path, 'w', encoding='utf-8') as f:
            for line in kept:
                f.write(line + '\n')

    print(f"Reset {len(indices)} target(s):")
    print(f"  {restored} JSONL line(s) restored from {jsonl_path}")
    print(f"  {popped} checkpoint decision(s) removed")
    if also_clear_log:
        print(f"  {log_removed} review log record(s) removed")
    else:
        print(f"  Review log untouched (pass --also-clear-log to remove records)")
    if out_of_range:
        print(f"  ⚠ Out of range (ignored): {sorted(out_of_range)}")

# ── Write output ──────────────────────────────────────────────────────────────
def write_output(entries: list, decisions: dict, output_path: str):
    """Write corrected JSONL. Entries with action 'accept', 'merge', or 'edit'
    get their text replaced; everything else is written through unchanged."""
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, entry in enumerate(entries):
            key = str(i)
            d   = decisions.get(key)
            if d and d['action'] in ('accept', 'merge', 'edit'):
                entry = dict(entry)
                entry['text'] = d['text']
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    print(f"\n{GREEN}✓ Corrected JSONL written → {output_path}{RESET}")

# ── Interactive review loop ───────────────────────────────────────────────────
def run(
    entries: list,
    orig_display: list,   # source words, original capitalisation + punctuation
    orig_match: list,     # source words, normalised (parallel to orig_display)
    decisions: dict,
    cursor: int,
    threshold: float,
    review_all: bool,
    jsonl_path: str,
    output_path: str,
    log_path: Path,
):
    total    = len(entries)
    auto_ct  = 0

    print(f"\n{BOLD}Alexandria Compare{RESET}")
    print(f"  Entries      : {total}")
    print(f"  Auto-approve : similarity ≥ {threshold:.0%}  (override with --review-all)")
    print(f"  Checkpoint   : {checkpoint_path(jsonl_path)}")
    print(f"  Output       : {output_path}")
    print(f"  Review log   : {log_path}")
    if decisions:
        print(f"  Resuming     : {len(decisions)} entries already decided")
    print()
    print(f"  {DIM}Tip: the LLM's *emphasis* and ... pause markers carry the prosody "
          f"that keeps the trained TTS voice from sounding flat. When fixing ASR "
          f"errors, prefer {RESET}{BOLD}[m]erge{RESET}{DIM} over {RESET}{BOLD}[a]ccept original{RESET}{DIM} "
          f"— it uses the correct source words while keeping those markers.{RESET}")
    print()

    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        key = str(idx)

        # Already decided in a prior session — restore cursor and skip display
        if key in decisions:
            if 'cursor_after' in decisions[key]:
                cursor = decisions[key]['cursor_after']
            idx += 1
            continue

        chunk_text  = entry.get('text', '')
        chunk_words = to_words(chunk_text)

        start, end, ratio = find_best_match(chunk_words, orig_match, cursor)

        # ── Re-anchor when alignment looks lost ────────────────────────────────
        # Weak match within the narrow window? The cursor may be drifting past
        # a section the audio skipped (or vice versa). Try a wider search ahead.
        # If realign finds a confident jump, use it. If not, the chunk likely
        # has no source equivalent — keep cursor where it is.
        no_source_match = False
        if ratio < 0.45 and len(chunk_words) >= 5:
            r_start, r_end, r_ratio = realign(chunk_words, orig_match, cursor)
            if r_ratio >= 0.55 and r_ratio > ratio + 0.15:
                start, end, ratio = r_start, r_end, r_ratio
            elif r_ratio < 0.30:
                # Last-resort full-source re-anchor. Catches catastrophic
                # alignment loss caused by unusual EPUB ordering — e.g. when
                # the front matter (J Novel Club credits, copyright, etc.)
                # sits at the END of the source. auto_anchor sees credits
                # match credits and parks the cursor at ~99% of the file;
                # realign only searches forward, so it can never recover
                # the prologue prose that's actually at char 0. find_anchor_position
                # scans the WHOLE source and can jump backward.
                a_start, a_end, a_ratio = find_anchor_position(
                    chunk_words, orig_match, min_ratio=0.6
                )
                # Require both an absolute high bar and a clear improvement
                # over the local ratio, so we don't false-positive on
                # epigraphs / audio-only material that legitimately has no
                # source equivalent.
                if a_ratio >= 0.6 and a_ratio > ratio + 0.4:
                    t_start, t_end = trim_span_to_alignment(
                        chunk_words, orig_match, a_start, a_end
                    )
                    if t_end > t_start:
                        start, end = t_start, t_end
                        ratio = _ratio(chunk_words, orig_match[start:end])
                    else:
                        start, end, ratio = a_start, a_end, a_ratio
                    print(f"{DIM}  [entry {idx+1}] full-source re-anchor "
                          f"jumped cursor to source word {start} "
                          f"(ratio {ratio:.1%}){RESET}")
                else:
                    # Truly no good match anywhere — likely audio-only material
                    # (chapter epigraph, intro/outro). Don't advance cursor.
                    no_source_match = True

        if no_source_match:
            orig_span_display = "(no matching passage found in source within search range)"
            orig_span_words   = []
            new_cursor        = cursor   # don't advance
        else:
            orig_span_display = ' '.join(orig_display[start:end])
            orig_span_words   = orig_match[start:end]
            new_cursor        = end

        # ── Auto-approve high-similarity entries ──────────────────────────────
        if not review_all and ratio >= threshold:
            decisions[key] = {
                'action': 'keep',
                'text': chunk_text,
                'ratio': ratio,
                'cursor_after': new_cursor,
                'auto': True,
            }
            cursor = new_cursor
            auto_ct += 1
            if auto_ct % 200 == 0:
                save_checkpoint(jsonl_path, decisions, cursor)
                print(f"{DIM}  [{idx+1}/{total}] {auto_ct} auto-approved, checkpoint saved{RESET}")
            idx += 1
            continue

        # ── Show for review ───────────────────────────────────────────────────
        a_col, b_col = color_diff(chunk_words, orig_span_words)

        # Build the merge preview: source words with LLM markers re-applied.
        # This is the option that preserves prosody (emphasis + pauses) while
        # fixing ASR errors — it's what keeps the trained TTS voice from
        # going flat.
        merge_preview = None
        if not no_source_match and ('*' in chunk_text or '..' in chunk_text):
            merge_preview = merge_annotations_with_source(
                chunk_text, orig_display[start:end]
            )
            # Don't show merge if it's identical to the plain original
            if merge_preview == orig_span_display:
                merge_preview = None

        print(SEP)
        print(
            f"{BOLD}Entry {idx+1}/{total}{RESET}  "
            f"{DIM}{entry.get('audio_filepath','?')}{RESET}  "
            f"{fmt_time(entry.get('start', 0))} → {fmt_time(entry.get('end', 0))}  "
            f"Match: {YELLOW}{ratio:.1%}{RESET}"
        )
        print(SEP)
        print(f"{CYAN}ANNOTATED :{RESET}  {chunk_text}")
        print(f"{CYAN}ORIGINAL  :{RESET}  {orig_span_display}")
        if merge_preview:
            print(f"{CYAN}MERGED    :{RESET}  {merge_preview}  "
                  f"{DIM}(original words + LLM prosody markers){RESET}")
        print()
        print(f"  {DIM}TRANS diff:{RESET}  {a_col}")
        print(f"  {DIM}ORIG  diff:{RESET}  {b_col}")
        print()

        menu = [f"{BOLD}[a]{RESET} accept original"]
        if merge_preview:
            menu.append(f"{BOLD}[m]{RESET} merge (keeps prosody)")
        menu.extend([
            f"{BOLD}[k]{RESET} keep annotation",
            f"{BOLD}[e]{RESET} edit manually",
            f"{BOLD}[s]{RESET} skip for now",
            f"{BOLD}[u]{RESET} undo last",
            f"{BOLD}[q]{RESET} quit & save",
        ])
        print("  " + "   ".join(menu))

        undone = False
        while True:
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = 'q'

            if choice == 'a':
                if no_source_match:
                    print(f"  {YELLOW}No source text to accept here. Choose [k], [e], or [s].{RESET}")
                    continue
                decisions[key] = {
                    'action': 'accept',
                    'text': orig_span_display,
                    'ratio': ratio,
                    'cursor_after': new_cursor,
                }
                print(f"  {GREEN}✓ Accepted original {DIM}(prosody markers stripped){RESET}")
                break

            elif choice == 'm':
                if not merge_preview:
                    print(f"  {YELLOW}Merge not available for this entry "
                          f"(no source match or no markers to preserve).{RESET}")
                    continue
                decisions[key] = {
                    'action': 'merge',
                    'text': merge_preview,
                    'ratio': ratio,
                    'cursor_after': new_cursor,
                }
                print(f"  {GREEN}✓ Merged: original words with preserved prosody markers{RESET}")
                break

            elif choice == 'k':
                decisions[key] = {
                    'action': 'keep',
                    'text': chunk_text,
                    'ratio': ratio,
                    'cursor_after': new_cursor,
                }
                print(f"  {DIM}Kept annotation{RESET}")
                break

            elif choice == 'e':
                print(f"  Type replacement text (blank = keep annotation):")
                try:
                    replacement = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    replacement = ''
                if replacement:
                    decisions[key] = {
                        'action': 'edit',
                        'text': replacement,
                        'ratio': ratio,
                        'cursor_after': new_cursor,
                    }
                    print(f"  {GREEN}✓ Saved edit{RESET}")
                else:
                    decisions[key] = {
                        'action': 'keep',
                        'text': chunk_text,
                        'ratio': ratio,
                        'cursor_after': new_cursor,
                    }
                    print(f"  {DIM}Kept annotation (blank input){RESET}")
                break

            elif choice == 's':
                decisions[key] = {
                    'action': 'skip',
                    'text': chunk_text,
                    'ratio': ratio,
                    'cursor_after': new_cursor,
                }
                print(f"  {YELLOW}Skipped — will appear again on next run{RESET}")
                break

            elif choice == 'u':
                # Undo the most recent MANUAL decision. Auto-approves between
                # that decision and "now" are also popped, because their cursor
                # math depended on the (now-undone) decision's cursor_after —
                # they'll re-auto-approve on the next pass with the corrected
                # cursor. Pre-anchor auto-keeps are left alone since the
                # anchor logic runs before this loop.
                target = find_last_manual_idx(decisions)
                if target is None:
                    print(f"  {YELLOW}Nothing to undo — no prior manual decisions in this session.{RESET}")
                    continue

                to_remove = sorted(int(k) for k in decisions if int(k) >= target)
                for k in to_remove:
                    decisions.pop(str(k), None)

                # Rewind cursor to the entry just before `target`
                if target > 0 and str(target - 1) in decisions:
                    cursor = decisions[str(target - 1)].get('cursor_after', 0)
                else:
                    cursor = 0

                # Drop matching review-log records so the log reflects current
                # state rather than the undone attempt.
                n_log = remove_log_entries(log_path, set(to_remove))

                save_checkpoint(jsonl_path, decisions, cursor)
                tail = f" + {len(to_remove)-1} subsequent auto-approve(s)" if len(to_remove) > 1 else ""
                log_note = f", {n_log} log record(s) removed" if n_log else ""
                print(f"  {YELLOW}↶ Undone entry {target+1}{tail}{log_note}. Rewinding…{RESET}")
                idx = target
                undone = True
                break

            elif choice == 'q':
                cursor = new_cursor
                save_checkpoint(jsonl_path, decisions, cursor)
                write_output(entries, decisions, output_path)
                n_done = sum(1 for d in decisions.values() if d['action'] != 'skip')
                print(f"\n{YELLOW}Paused.{RESET}  {len(decisions)}/{total} entries seen, {n_done} decided.")
                print(f"Rerun the same command to resume from here.")
                sys.exit(0)

            else:
                valid = "a, m, k, e, s, u, or q" if merge_preview else "a, k, e, s, u, or q"
                print(f"  Enter {valid}")

        # [u]ndo doesn't produce a decision and already rewound idx/cursor —
        # skip the log+advance tail.
        if undone:
            continue

        # Record this manual decision so the session can be reviewed afterward
        # for script-improvement patterns. ('q' sys.exit()s above, so we only
        # log entries that actually produced a decision.)
        decided = decisions[key]
        log_decision(log_path, {
            'entry_idx':       idx,
            'audio':           entry.get('audio_filepath'),
            'start':           entry.get('start'),
            'end':             entry.get('end'),
            'ratio':           round(ratio, 4),
            'action':          decided['action'],
            'no_source_match': no_source_match,
            'annotated':       chunk_text,
            'original':        None if no_source_match else orig_span_display,
            'merge_preview':   merge_preview,
            'final_text':      decided['text'],
        })

        cursor = new_cursor
        save_checkpoint(jsonl_path, decisions, cursor)
        idx += 1

    # ── All entries processed ─────────────────────────────────────────────────
    write_output(entries, decisions, output_path)

    kept     = sum(1 for d in decisions.values() if d['action'] == 'keep' and not d.get('auto'))
    auto     = sum(1 for d in decisions.values() if d.get('auto'))
    accepted = sum(1 for d in decisions.values() if d['action'] == 'accept')
    merged   = sum(1 for d in decisions.values() if d['action'] == 'merge')
    edited   = sum(1 for d in decisions.values() if d['action'] == 'edit')
    skipped  = sum(1 for d in decisions.values() if d['action'] == 'skip')

    print(f"\n{BOLD}Complete!{RESET}")
    print(f"  Auto-approved (≥{threshold:.0%}) : {auto}")
    print(f"  Kept annotation             : {kept}")
    print(f"  Accepted original (stripped): {accepted}")
    print(f"  Merged (words + prosody)    : {merged}")
    print(f"  Edited manually             : {edited}")
    print(f"  Skipped                     : {skipped}")
    if accepted > merged * 3 and accepted > 50:
        print(f"\n  {YELLOW}⚠ You accepted {accepted} originals plain (no prosody markers).{RESET}")
        print(f"  {YELLOW}  Consider using [m]erge instead — it keeps the LLM's pause "
              f"and emphasis markers,{RESET}")
        print(f"  {YELLOW}  which is what stops the trained TTS voice from sounding "
              f"flat/monotone.{RESET}")
    if skipped:
        print(f"  {YELLOW}Re-run to review {skipped} skipped entries.{RESET}")

    # Clean up checkpoint on full completion (no skips remaining)
    cp = checkpoint_path(jsonl_path)
    if skipped == 0 and cp.exists():
        cp.unlink()
        print(f"  Checkpoint removed (all entries decided).")

    if log_path.exists():
        n_logged = sum(1 for _ in open(log_path, encoding='utf-8'))
        print(f"  Review log : {log_path} ({n_logged} manual decisions)")

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Compare metadata.jsonl transcriptions against an original EPUB or text file"
    )
    parser.add_argument("--jsonl",   required=True,
                        help="Path to metadata.jsonl (or extracted from the dataset zip)")
    parser.add_argument("--source",  required=True,
                        help="Path to the original .epub or .txt file")
    parser.add_argument("--output",
                        help="Output path for corrected JSONL "
                             "(default: <jsonl_name>_corrected.jsonl)")
    parser.add_argument("--threshold", type=float, default=0.90,
                        help="Similarity ratio above which entries are auto-approved "
                             "without showing them to you (default: 0.90)")
    parser.add_argument("--review-all", action="store_true",
                        help="Show every entry for review, ignoring --threshold")
    parser.add_argument("--reset", action="store_true",
                        help="Discard saved checkpoint and start from the beginning")

    # ── Targeted reset (undo specific decisions without losing the rest) ──
    parser.add_argument("--reset-entry", metavar="N[,N,...]",
                        help="Restore specific entries by index (comma-separated). "
                             "Replaces their lines in the corrected JSONL with the "
                             "originals from --jsonl and drops their checkpoint "
                             "decisions. Exits after reset.")
    parser.add_argument("--reset-from", type=int, metavar="N",
                        help="Reset every entry from index N to the end. Exits after reset.")
    parser.add_argument("--reset-range", metavar="N:M",
                        help="Reset entries in the inclusive range N..M. Exits after reset.")
    parser.add_argument("--also-clear-log", action="store_true",
                        help="When combined with --reset-entry/--reset-from/--reset-range, "
                             "also remove matching records from the review log "
                             "(default: leave the log intact).")

    # ── Alignment offset controls ─────────────────────────────────────────────
    # Audiobooks and source texts rarely start at the same point — the audio
    # may open with credits/narrator intro, the text may open with copyright
    # and TOC. These flags control where the alignment cursor starts.
    parser.add_argument("--source-start", type=int, metavar="N",
                        help="Manually start at source word N (skip auto-anchor)")
    parser.add_argument("--source-start-text", metavar="TEXT",
                        help="Fuzzy-search for TEXT in the source and start there "
                             "(e.g. --source-start-text \"It was a warm Saturday\")")
    parser.add_argument("--no-auto-anchor", action="store_true",
                        help="Disable automatic anchor detection (start at source word 0)")
    parser.add_argument("--review-preanchor", action="store_true",
                        help="Review JSONL entries before the anchor individually "
                             "(default: auto-keep them as-is since they're usually intro)")
    args = parser.parse_args()

    jsonl_path  = args.jsonl
    output_path = args.output or str(
        Path(jsonl_path).with_name(Path(jsonl_path).stem + '_corrected.jsonl')
    )
    log_path = review_log_path(output_path)

    print(f"Loading JSONL   : {jsonl_path}")
    entries = load_jsonl(jsonl_path)
    print(f"  {len(entries)} entries")

    # Targeted reset: undo specific entries and exit before any expensive
    # source loading / alignment work runs.
    reset_indices = parse_reset_spec(
        args.reset_entry, args.reset_from, args.reset_range, len(entries)
    )
    if reset_indices:
        apply_targeted_reset(
            jsonl_path, output_path, log_path, reset_indices, args.also_clear_log
        )
        sys.exit(0)

    print(f"Loading source  : {args.source}")
    source_text = load_source(args.source)
    # Strip EPUB OCR digit-in-word glitches ('thos1e' → 'those', 'Kars1a' →
    # 'Karsa') before tokenisation. The ASR-derived chunks never have these
    # digits, so leaving them in source produces visibly-wrong merged output.
    source_text = _OCR_DIGIT_GLITCH.sub('', source_text)
    # Rejoin precomposed Latin diacritics that got split from their stem
    # during EPUB extraction ('fianc é' → 'fiancé', 'fiancé e' → 'fiancée').
    source_text = _DIACRITIC_REJOIN.sub(r'\1\2', source_text)
    source_text = _DIACRITIC_REJOIN_TAIL.sub(r'\1\2', source_text)
    print(f"  {len(source_text):,} characters")

    # Build the per-book proper-noun lexicon. Used by _step_threshold to relax
    # the boundary acceptance bar when the source-side token is a known name —
    # critical for Japanese romanization ASR mistranscriptions like
    # 'coodo'↔'kudou' or 'youth'↔'yurie' that sit far below the default 0.55.
    # Stored on the alignment module so every helper there sees the same value.
    _alignment._PROPER_NOUNS = _build_proper_nouns(source_text)
    if _alignment._PROPER_NOUNS:
        sample = ', '.join(sorted(_alignment._PROPER_NOUNS)[:8])
        more = f' +{len(_alignment._PROPER_NOUNS) - 8} more' if len(_alignment._PROPER_NOUNS) > 8 else ''
        print(f"  {len(_alignment._PROPER_NOUNS)} recurring proper nouns ({sample}{more})")

    # Build parallel word lists: display (original form) and match (normalised).
    #
    # Hyphens and dashes are split BEFORE whitespace tokenisation so that a
    # source compound like "twenty-minute" becomes two entries ["twenty",
    # "minute"] instead of one. Without this split, orig_match[i] would be
    # the string "twenty minute" (one element with an embedded space), and
    # the audio chunk's separately-spoken "twenty" / "minute" tokens fail to
    # align with it — causing those words to disappear from ORIGINAL when
    # trim_span_to_alignment runs. Loss of the hyphen in display is fine
    # for TTS training (the audio speaks the parts as separate words with
    # a slight pause anyway).
    # U+2500 (BOX DRAWINGS LIGHT HORIZONTAL) shows up in EPUB→text conversions
    # in place of a real em-dash (U+2014). Treat it as a dash for split purposes.
    _COMPOUND_SPLIT = re.compile(r'[-‐‑‒–—―─━]')
    source_tokens = _COMPOUND_SPLIT.sub(' ', source_text).split()
    orig_display, orig_match = [], []
    for w in source_tokens:
        m = normalize(w)
        if not m:
            continue   # pure punctuation token
        orig_display.append(w)
        orig_match.append(m)
    print(f"  {len(orig_display):,} words")

    # Checkpoint
    if args.reset:
        cp = checkpoint_path(jsonl_path)
        if cp.exists():
            cp.unlink()
            print("Checkpoint cleared — starting fresh.")
        if log_path.exists():
            log_path.unlink()
            print("Review log cleared — starting fresh.")
        decisions, cursor = {}, 0
    else:
        saved     = load_checkpoint(jsonl_path)
        decisions = saved.get("decisions", {})
        cursor    = saved.get("cursor", 0)
        if decisions:
            print(f"Resuming checkpoint: {len(decisions)} entries already decided, "
                  f"cursor at source word {cursor}")

    # ── Initial alignment: figure out where in the source to start ────────────
    # Skip this whole block if we're resuming a session.
    if not decisions:
        if args.source_start is not None:
            cursor = max(0, min(args.source_start, len(orig_match)))
            preview = ' '.join(orig_display[cursor:cursor+12])
            print(f"\nStarting at source word {cursor} (--source-start)")
            print(f"  Source: \"{preview}...\"")

        elif args.source_start_text:
            print(f"\nSearching source for: \"{args.source_start_text}\" ...")
            pos = find_text_in_source(args.source_start_text, orig_match)
            if pos < 0:
                sys.exit(f"{RED}Could not confidently locate that text in the source.{RESET}\n"
                         f"Try a longer / more distinctive phrase, or use --source-start N.")
            cursor = pos
            preview = ' '.join(orig_display[cursor:cursor+12])
            print(f"  ✓ Found at source word {cursor}")
            print(f"  Source: \"{preview}...\"")

        elif args.no_auto_anchor:
            cursor = 0
            print(f"\nAuto-anchor disabled — starting at source word 0")

        else:
            # Default: auto-detect where the audio first connects to the source
            print(f"\n🔍 Searching for initial alignment anchor "
                  f"(audio intro and text front-matter often don't line up)...")
            anchor_idx, anchor_pos, anchor_ratio = auto_anchor(entries, orig_match)

            if anchor_ratio > 0:
                preview = ' '.join(orig_display[anchor_pos:anchor_pos+12])
                print(f"  ✓ JSONL entry {anchor_idx} anchors at source word {anchor_pos} "
                      f"({YELLOW}{anchor_ratio:.1%}{RESET} match)")
                print(f"  Source preview: \"{preview}...\"")
                cursor = anchor_pos

                # Handle entries before the anchor: audio-only intro material
                # (credits, narrator intro, "this story is fiction" disclaimer, etc.)
                if anchor_idx > 0:
                    print()
                    print(f"  {YELLOW}⚠ {anchor_idx} JSONL entr{'y' if anchor_idx == 1 else 'ies'} "
                          f"before the anchor have no matching source text{RESET}")
                    print(f"     (likely audio intro/credits not present in the text)")
                    if args.review_preanchor:
                        print(f"     {DIM}--review-preanchor set: will show each individually{RESET}")
                    else:
                        for i in range(anchor_idx):
                            decisions[str(i)] = {
                                'action': 'keep',
                                'text':   entries[i].get('text', ''),
                                'ratio':  0.0,
                                'cursor_after': cursor,
                                'pre_anchor':   True,
                            }
                        save_checkpoint(jsonl_path, decisions, cursor)
                        print(f"     ✓ Auto-kept as-is "
                              f"({DIM}use --review-preanchor to review them individually{RESET})")
            else:
                print(f"  {YELLOW}⚠ No confident anchor found in the first "
                      f"{min(20, len(entries))} entries.{RESET}")
                print(f"  Starting at source word 0. If alignment is poor, retry with:")
                print(f"    --source-start N            (manual word offset)")
                print(f"    --source-start-text \"...\"   (search the source for a phrase)")
                cursor = 0

    # ── Divergence warning (fresh sessions only) ──────────────────────────────
    # If a meaningful chunk of the audio doesn't align with the source, it's
    # usually because the audiobook was narrated from a different translation
    # or edition than the EPUB. Flag it now so the user can swap sources
    # instead of grinding through 100 manual edits to find out.
    if not decisions:
        print(f"\n🔍 Estimating source/audio alignment quality...")
        avg, n_sampled, low_ct, review_ct = estimate_alignment_quality(
            entries, orig_match, cursor
        )
        if n_sampled >= 10:
            pct_low = low_ct / n_sampled
            pct_review = review_ct / n_sampled
            # Catastrophic: bad average OR many outright failures OR a meaningfully
            # large fraction of entries would need manual review (= consistent
            # divergence even when individual alignments mostly succeed).
            # Empirical: clean books sit around 10-15% review rate; the Archer
            # book (different-translation case) sits at ~30%.
            if avg < 0.70 or pct_low > 0.20 or pct_review >= 0.30:
                print()
                print(f"  {BOLD}{YELLOW}⚠ Possible source/audio divergence{RESET}")
                print(f"  {YELLOW}Sampled {n_sampled} entries — average alignment ratio "
                      f"{avg:.0%}, {review_ct} ({pct_review:.0%}) would need manual "
                      f"review{RESET}")
                if low_ct:
                    print(f"  {YELLOW}{low_ct} ({pct_low:.0%}) aligned at < 60% "
                          f"(matching is brittle){RESET}")
                print(f"  {YELLOW}Usually means the audiobook was narrated from a "
                      f"different translation/edition{RESET}")
                print(f"  {YELLOW}than the source you provided. Many entries will need "
                      f"manual edits.{RESET}")
                print(f"  {DIM}Continue, or Ctrl-C and try a different --source.{RESET}")
                print()
            else:
                print(f"  {DIM}sampled {n_sampled} entries — avg ratio {avg:.0%}, "
                      f"{review_ct} would need review. Looks good.{RESET}")

    run(
        entries      = entries,
        orig_display = orig_display,
        orig_match   = orig_match,
        decisions    = decisions,
        cursor       = cursor,
        threshold    = args.threshold,
        review_all   = args.review_all,
        jsonl_path   = jsonl_path,
        output_path  = output_path,
        log_path     = log_path,
    )

if __name__ == "__main__":
    main()
