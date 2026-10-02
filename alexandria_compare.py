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

import sys
import json
import difflib
import argparse
import hashlib
import tempfile
import os
import uuid
from pathlib import Path

# Shared alignment primitives (source loading + cleanups, proper-noun lexicon,
# fuzzy alignment, trim/extend, all threshold tiers, char_sim cache).
# Anything compare needs from this module is imported here.
from alexandria_alignment import (
    _OCR_DIGIT_GLITCH,
    _DIACRITIC_REJOIN,
    _DIACRITIC_REJOIN_TAIL,
    load_source,
    normalize,
    to_words,
    split_compounds,
    get_source_word_lists,
    _build_proper_nouns,
    find_best_match,
    find_anchor_position,
    auto_anchor,
    realign,
    trim_span_to_alignment,
    estimate_alignment_quality,
    get_alignment_quality_prescan,
    get_alignment_match,
    find_text_in_source,
    merge_annotations_with_source,
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


# ── Diff display ──────────────────────────────────────────────────────────────
def get_terminal_text(value):
    """Show untrusted controls literally, retaining normal Unicode text."""
    text = str(value)
    return ''.join(
        (f'\\x{ord(char):02x}' if ord(char) < 256 else f'\\u{ord(char):04x}')
        if (ord(char) < 32 or 127 <= ord(char) <= 159
            or char in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
        else char for char in text)


def color_diff(a: list, b: list) -> tuple:
    """Return (a_colored_str, b_colored_str) with ANSI markup."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    a_out, b_out = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a_seg = get_terminal_text(' '.join(a[i1:i2]))
        b_seg = get_terminal_text(' '.join(b[j1:j2]))
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

def get_checkpoint_identity(jsonl_path: str, source_path: str, output_path: str) -> dict:
    """Identify the input bytes and review destinations for one session."""
    identity = {'output': str(Path(output_path).resolve())}
    for name, path in (('jsonl', jsonl_path), ('source', source_path)):
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(block)
        identity[name] = digest.hexdigest()
    return identity


def load_checkpoint(jsonl_path: str, identity: dict = None) -> dict:
    cp = checkpoint_path(jsonl_path)
    if cp.exists():
        try:
            saved = json.loads(cp.read_text())
            if (not isinstance(saved, dict)
                    or not isinstance(saved.get('decisions', {}), dict)
                    or not isinstance(saved.get('cursor', 0), int)):
                raise ValueError('invalid checkpoint structure')
            if identity is not None and saved.get('identity') != identity:
                sys.exit(f"Checkpoint {get_terminal_text(cp)} belongs to different or older inputs. "
                         "Use --reset to start a new review, or restore the original inputs.")
            return load_decision_journal(jsonl_path, saved)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            if checkpoint_journal_path(jsonl_path).exists():
                sys.exit(f"Cannot recover checkpoint {get_terminal_text(cp)} with its journal: {get_terminal_text(e)}. Files preserved.")
            # Don't let a truncated/corrupt checkpoint crash the whole session
            # (it's saved after every decision, so a crash mid-write is likely).
            print(f"WARNING: checkpoint {get_terminal_text(cp.name)} is unreadable ({get_terminal_text(e)}); starting fresh.")
    if checkpoint_journal_path(jsonl_path).exists():
        sys.exit(f"Checkpoint {get_terminal_text(cp)} is missing but its decision journal exists. Files preserved; use --reset explicitly.")
    return {"decisions": {}, "cursor": 0}


def checkpoint_journal_path(jsonl_path):
    return checkpoint_path(jsonl_path).with_suffix('.json.journal')


def load_decision_journal(jsonl_path, saved):
    journal = checkpoint_journal_path(jsonl_path)
    if not journal.exists():
        return saved
    generation = saved.get('generation')
    if not isinstance(generation, str) or not generation:
        sys.exit(f"Checkpoint {get_terminal_text(journal)} has no journal generation. Files preserved.")
    recovered = dict(saved, decisions=dict(saved['decisions']))
    with journal.open('rb') as stream:
        for number, line in enumerate(stream, 1):
            if not line.endswith(b'\n'):
                print(f"WARNING: ignoring incomplete trailing journal record in {get_terminal_text(journal)}; prior decisions recovered.")
                break
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or not isinstance(record.get('generation'), str):
                    raise ValueError('invalid record generation')
                if record['generation'] != generation:
                    continue
                key, decision, cursor = record.get('key'), record.get('decision'), record.get('cursor')
                if (not isinstance(key, str) or not key.isdecimal()
                        or not isinstance(decision, dict)
                        or decision.get('action') not in ('keep', 'accept', 'merge', 'edit', 'skip')
                        or type(cursor) is not int or cursor < 0):
                    raise ValueError('invalid decision record')
            except (ValueError, UnicodeDecodeError) as exc:
                sys.exit(f"Invalid decision journal {get_terminal_text(journal)} record {number}: {get_terminal_text(exc)}. Files preserved.")
            recovered['decisions'][key] = decision
            recovered['cursor'] = cursor
    return recovered


def save_decision_checkpoint(jsonl_path, key, decision, cursor, generation):
    record = {'generation': generation, 'key': key, 'decision': decision, 'cursor': cursor}
    data = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    with checkpoint_journal_path(jsonl_path).open('ab') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def clear_checkpoint(jsonl_path):
    # A full snapshot remains recoverable if journal cleanup is interrupted.
    checkpoint_journal_path(jsonl_path).unlink(missing_ok=True)
    checkpoint_path(jsonl_path).unlink(missing_ok=True)


def save_checkpoint(jsonl_path: str, decisions: dict, cursor: int,
                    identity: dict = None):
    cp = checkpoint_path(jsonl_path)
    generation = uuid.uuid4().hex
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=cp.parent,
                                         prefix=f'.{cp.name}.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'decisions': decisions, 'cursor': cursor, 'identity': identity,
                       'generation': generation}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(cp)
        checkpoint_journal_path(jsonl_path).unlink(missing_ok=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return generation

# ── Review log ────────────────────────────────────────────────────────────────
# Records every entry the user manually reviewed (auto-approved entries are
# skipped to keep the log signal-rich). The intent is to feed this back into
# the script so common edit patterns can be fixed at the source — for example,
# if every [e]dit reshapes the merge preview the same way, the merge function
# can be improved instead.
def review_log_path(output_path: str) -> Path:
    p = Path(output_path)
    return p.with_name(p.stem + '_review_log.jsonl')

_log_decision_warned = False

def log_decision(log_path: Path, record: dict):
    """Append one decision as a JSON line. Best-effort: a logging failure
    must never abort the user's review session."""
    global _log_decision_warned
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        if not _log_decision_warned:
            _log_decision_warned = True
            print(f"{YELLOW}Warning: review log stopped recording decisions ({get_terminal_text(e)}). "
                  f"Your choices are still being applied, just not logged.{RESET}")

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
        if reset_from >= total_entries:
            sys.exit(f"--reset-from: index {reset_from} is past the last entry "
                     f"({total_entries - 1}); nothing to reset")
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
    identity: dict = None,
):
    """Restore each indexed entry's text from metadata.jsonl, drop its
    checkpoint decision, and optionally remove matching review-log records.
    The cursor in the checkpoint is rewound to the smallest reset index so the
    next normal run re-aligns from there (otherwise the cursor could be ahead
    of source content that the reset entries should have consumed)."""
    orig_entries = load_jsonl(jsonl_path)

    out_path = Path(output_path)
    if out_path.exists():
        cur_entries = load_jsonl(output_path)
        if len(cur_entries) != len(orig_entries):
            sys.exit(f"Cannot reset: {get_terminal_text(output_path)} has {len(cur_entries)} entries, "
                     f"but {jsonl_path} has {len(orig_entries)}")
    else:
        cur_entries = orig_entries.copy()

    cp_path = checkpoint_path(jsonl_path)
    cp = load_checkpoint(jsonl_path, identity) if (cp_path.exists() or checkpoint_journal_path(jsonl_path).exists()) else None

    restored, out_of_range = 0, []
    for idx in sorted(indices):
        if 0 <= idx < len(orig_entries):
            cur_entries[idx] = orig_entries[idx]
            restored += 1
        else:
            out_of_range.append(idx)

    write_jsonl_atomic(cur_entries, out_path)

    # Drop decisions; rewind cursor to the lowest reset index so the next
    # run re-anchors before that point instead of skipping ahead.
    popped = 0
    if cp is not None:
        decisions = cp.get('decisions', {})
        for idx in indices:
            if decisions.pop(str(idx), None) is not None:
                popped += 1
        if indices:
            min_idx = min(indices)
            # cursor_after of the entry just before min_idx, if present
            prev = decisions.get(str(min_idx - 1)) if min_idx > 0 else None
            cp['cursor'] = prev['cursor_after'] if (prev and 'cursor_after' in prev) else 0
        save_checkpoint(jsonl_path, decisions, cp.get('cursor', 0), identity)

    log_removed = remove_log_entries(log_path, indices) if also_clear_log and log_path.exists() else 0

    print(f"Reset {len(indices)} target(s):")
    print(f"  {restored} JSONL entry/entries restored from {get_terminal_text(jsonl_path)}")
    print(f"  {popped} checkpoint decision(s) removed")
    if also_clear_log:
        print(f"  {log_removed} review log record(s) removed")
    else:
        print(f"  Review log untouched (pass --also-clear-log to remove records)")
    if out_of_range:
        print(f"  ⚠ Out of range (ignored): {sorted(out_of_range)}")

# ── Write output ──────────────────────────────────────────────────────────────
def write_jsonl_atomic(entries: list, output_path: Path):
    """Replace a JSONL file only after every entry has been serialized."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8',
                                         dir=output_path.parent, prefix=f'.{output_path.name}.',
                                         suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        temporary.replace(output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_output(entries: list, decisions: dict, output_path: str):
    """Write corrected JSONL. Entries with action 'accept', 'merge', or 'edit'
    get their text replaced; everything else is written through unchanged."""
    corrected = []
    for i, entry in enumerate(entries):
        key = str(i)
        d   = decisions.get(key)
        if d and d['action'] in ('accept', 'merge', 'edit'):
            entry = dict(entry)
            entry['text'] = d['text']
        corrected.append(entry)
    write_jsonl_atomic(corrected, Path(output_path))
    print(f"\n{GREEN}✓ Corrected JSONL written → {get_terminal_text(output_path)}{RESET}")

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
    checkpoint_identity: dict = None,
    proper_nouns: frozenset = frozenset(),
    alignment_prescan = None,
):
    prescan_matches = {}
    if (alignment_prescan is not None
            and alignment_prescan.source_words == tuple(orig_match)
            and alignment_prescan.threshold == threshold
            and alignment_prescan.proper_nouns == frozenset(proper_nouns)):
        prescan_matches = {sample.entry_idx: sample for sample in alignment_prescan.samples}
    total    = len(entries)
    auto_ct  = 0
    review_ct = 0

    print(f"\n{BOLD}Alexandria Compare{RESET}")
    print(f"  Entries      : {total}")
    print(f"  Auto-approve : similarity ≥ {threshold:.0%}  (override with --review-all)")
    print(f"  Checkpoint   : {get_terminal_text(checkpoint_path(jsonl_path))}")
    print(f"  Output       : {get_terminal_text(output_path)}")
    print(f"  Review log   : {get_terminal_text(log_path)}")
    if decisions:
        print(f"  Resuming     : {len(decisions)} entries already decided")
    print()
    print(f"  {DIM}Tip: the LLM's *emphasis* and ... pause markers carry the prosody "
          f"that keeps the trained TTS voice from sounding flat. When fixing ASR "
          f"errors, prefer {RESET}{BOLD}[m]erge{RESET}{DIM} over {RESET}{BOLD}[a]ccept original{RESET}{DIM} "
          f"— it uses the correct source words while keeping those markers.{RESET}")
    print()

    generation = save_checkpoint(jsonl_path, decisions, cursor, checkpoint_identity)

    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        key = str(idx)

        # Already decided in a prior session — restore cursor and skip display
        if key in decisions and decisions[key]['action'] != 'skip':
            if 'cursor_after' in decisions[key]:
                cursor = decisions[key]['cursor_after']
            idx += 1
            continue

        chunk_text  = entry.get('text', '')
        chunk_words = to_words(chunk_text)

        sample = prescan_matches.get(idx)
        if sample is not None and sample.cursor == cursor and sample.chunk_words == tuple(chunk_words):
            match = sample.match
        else:
            match = get_alignment_match(
                chunk_words, orig_match, cursor, threshold, proper_nouns,
                find_match=find_best_match, realign_match=realign,
                find_anchor=find_anchor_position, trim_match=trim_span_to_alignment,
            )
        start, end, ratio, no_source_match, reanchored = match
        if reanchored:
            print(f"{DIM}  [entry {idx+1}] full-source re-anchor "
                  f"jumped cursor to source word {start} "
                  f"(ratio {ratio:.1%}){RESET}")

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
            save_decision_checkpoint(jsonl_path, key, decisions[key], cursor, generation)
            auto_ct += 1
            if auto_ct % 200 == 0:
                generation = save_checkpoint(jsonl_path, decisions, cursor, checkpoint_identity)
                print(f"{DIM}  [{idx+1}/{total}] {auto_ct} auto-approved, checkpoint saved{RESET}")
            idx += 1
            continue

        # ── Show for review ───────────────────────────────────────────────────
        review_ct += 1
        a_col, b_col = color_diff(chunk_words, orig_span_words)

        # Manual decisions/undo may change the cursor; recompute subsequent work.
        prescan_matches.clear()

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
            f"{DIM}{get_terminal_text(entry.get('audio_filepath','?'))}{RESET}  "
            f"{fmt_time(entry.get('start', 0))} → {fmt_time(entry.get('end', 0))}  "
            f"Match: {YELLOW}{ratio:.1%}{RESET}"
        )
        print(SEP)
        print(f"{CYAN}ANNOTATED :{RESET}  {get_terminal_text(chunk_text)}")
        print(f"{CYAN}ORIGINAL  :{RESET}  {get_terminal_text(orig_span_display)}")
        if merge_preview:
            print(f"{CYAN}MERGED    :{RESET}  {get_terminal_text(merge_preview)}  "
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

                generation = save_checkpoint(jsonl_path, decisions, cursor, checkpoint_identity)
                tail = f" + {len(to_remove)-1} subsequent auto-approve(s)" if len(to_remove) > 1 else ""
                log_note = f", {n_log} log record(s) removed" if n_log else ""
                print(f"  {YELLOW}↶ Undone entry {target+1}{tail}{log_note}. Rewinding…{RESET}")
                idx = target
                undone = True
                break

            elif choice == 'q':
                cursor = new_cursor
                generation = save_checkpoint(jsonl_path, decisions, cursor, checkpoint_identity)
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
        # Don't log a 'skip' — the entry is re-shown and re-decided on the next
        # run, so logging it now leaves a duplicate/conflicting record for the
        # same entry_idx (and inflates the "N manual decisions" count).
        if decided['action'] != 'skip':
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
        save_decision_checkpoint(jsonl_path, key, decisions[key], cursor, generation)
        idx += 1

    # ── All entries processed ─────────────────────────────────────────────────
    save_checkpoint(jsonl_path, decisions, cursor, checkpoint_identity)
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
        clear_checkpoint(jsonl_path)
        print(f"  Checkpoint removed (all entries decided).")

    if log_path.exists():
        with open(log_path, encoding='utf-8') as _lf:
            n_logged = sum(1 for _ in _lf)
        print(f"  Review log : {get_terminal_text(log_path)} ({n_logged} manual decisions)")

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

    if not 0 < args.threshold <= 1:
        parser.error("--threshold must be greater than 0 and at most 1")

    jsonl_path  = args.jsonl
    output_path = args.output or str(
        Path(jsonl_path).with_name(Path(jsonl_path).stem + '_corrected.jsonl')
    )
    log_path = review_log_path(output_path)

    checkpoint_identity = get_checkpoint_identity(jsonl_path, args.source, output_path)

    print(f"Loading JSONL   : {get_terminal_text(jsonl_path)}")
    entries = load_jsonl(jsonl_path)
    print(f"  {len(entries)} entries")

    # Targeted reset: undo specific entries and exit before any expensive
    # source loading / alignment work runs.
    reset_indices = parse_reset_spec(
        args.reset_entry, args.reset_from, args.reset_range, len(entries)
    )
    if reset_indices:
        apply_targeted_reset(
            jsonl_path, output_path, log_path, reset_indices, args.also_clear_log,
            checkpoint_identity,
        )
        sys.exit(0)

    print(f"Loading source  : {get_terminal_text(args.source)}")
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
    proper_nouns = _build_proper_nouns(source_text)
    if proper_nouns:
        sample = ', '.join(sorted(proper_nouns)[:8])
        more = f' +{len(proper_nouns) - 8} more' if len(proper_nouns) > 8 else ''
        print(f"  {len(proper_nouns)} recurring proper nouns ({get_terminal_text(sample)}{more})")

    # Build parallel word lists: display (original form) and match (normalised).
    #
    # Hyphens and dashes are split BEFORE whitespace tokenisation (shared
    # split_compounds(), same one alexandria_preparer_rocm_compatible.py
    # uses) so that a source compound like "twenty-minute" becomes two
    # entries ["twenty", "minute"] instead of one. Without this split,
    # orig_match[i] would be the string "twenty minute" (one element with an
    # embedded space), and the audio chunk's separately-spoken "twenty" /
    # "minute" tokens fail to align with it — causing those words to
    # disappear from ORIGINAL when trim_span_to_alignment runs. Loss of the
    # hyphen in display is fine for TTS training (the audio speaks the parts
    # as separate words with a slight pause anyway).
    orig_display, orig_match = get_source_word_lists(source_text)
    print(f"  {len(orig_display):,} words")

    # Checkpoint
    if args.reset:
        cp = checkpoint_path(jsonl_path)
        if cp.exists() or checkpoint_journal_path(jsonl_path).exists():
            clear_checkpoint(jsonl_path)
            print("Checkpoint cleared — starting fresh.")
        if log_path.exists():
            log_path.unlink()
            print("Review log cleared — starting fresh.")
        decisions, cursor = {}, 0
    else:
        saved     = load_checkpoint(jsonl_path, checkpoint_identity)
        decisions = saved.get("decisions", {})
        cursor    = saved.get("cursor", 0)
        if decisions:
            print(f"Resuming checkpoint: {len(decisions)} entries already decided, "
                  f"cursor at source word {cursor}")

    fresh_session = not decisions
    anchor_entry_idx = 0

    # ── Initial alignment: figure out where in the source to start ────────────
    # Skip this whole block if we're resuming a session.
    if fresh_session:
        if args.source_start is not None:
            cursor = max(0, min(args.source_start, len(orig_match)))
            preview = ' '.join(orig_display[cursor:cursor+12])
            print(f"\nStarting at source word {cursor} (--source-start)")
            print(f"  Source: \"{get_terminal_text(preview)}...\"")

        elif args.source_start_text:
            print(f"\nSearching source for: \"{get_terminal_text(args.source_start_text)}\" ...")
            pos = find_text_in_source(args.source_start_text, orig_match)
            if pos < 0:
                sys.exit(f"{RED}Could not confidently locate that text in the source.{RESET}\n"
                         f"Try a longer / more distinctive phrase, or use --source-start N.")
            cursor = pos
            preview = ' '.join(orig_display[cursor:cursor+12])
            print(f"  ✓ Found at source word {cursor}")
            print(f"  Source: \"{get_terminal_text(preview)}...\"")

        elif args.no_auto_anchor:
            cursor = 0
            print(f"\nAuto-anchor disabled — starting at source word 0")

        else:
            # Default: auto-detect where the audio first connects to the source
            print(f"\n🔍 Searching for initial alignment anchor "
                  f"(audio intro and text front-matter often don't line up)...")
            anchor_idx, anchor_pos, anchor_ratio = auto_anchor(entries, orig_match)

            if anchor_ratio > 0:
                anchor_entry_idx = anchor_idx
                preview = ' '.join(orig_display[anchor_pos:anchor_pos+12])
                print(f"  ✓ JSONL entry {anchor_idx} anchors at source word {anchor_pos} "
                      f"({YELLOW}{anchor_ratio:.1%}{RESET} match)")
                print(f"  Source preview: \"{get_terminal_text(preview)}...\"")
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
                        save_checkpoint(jsonl_path, decisions, cursor,
                                        checkpoint_identity)
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
    alignment_prescan = None
    if fresh_session:
        print(f"\n🔍 Estimating source/audio alignment quality...")
        alignment_prescan = get_alignment_quality_prescan(
            entries, orig_match, cursor, threshold=args.threshold,
            start_entry_idx=anchor_entry_idx,
            proper_nouns=proper_nouns,
        )
        avg, n_sampled, low_ct, review_ct = alignment_prescan.metrics
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
        checkpoint_identity = checkpoint_identity,
        proper_nouns = proper_nouns,
        review_all   = args.review_all,
        jsonl_path   = jsonl_path,
        output_path  = output_path,
        log_path     = log_path,
        alignment_prescan = alignment_prescan,
    )

if __name__ == "__main__":
    main()
