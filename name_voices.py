#!/usr/bin/env python3
"""
name_voices.py — Stage 5 of the voice→LoRA pipeline: turn each trained+profiled
adapter into a concise descriptive slug and rename it.

After batch_train_lora.py trains an adapter (folder/id = the raw dataset stem,
e.g. ``narrator_alyssa_poon…char1_vol01``) and voice_profiler.py writes a prose
``voice_profile`` + ``voice_features`` into lora_models/manifest.json, this script
derives the canonical name

    {timbre}_{register}_{age}_{gender}[_{genre}][_N]
    e.g.  silky_baritone_30s_m_fantasy

from the prose + acoustic features, then renames the adapter directory and updates
the manifest ``id``/``name`` (with ``_1/_2`` suffixes on collisions). A backup of the
manifest is written before any change.

This replaces the manual renaming step that was previously done by hand.

Usage:
    python name_voices.py --verify          # re-derive names for already-named
                                            #   adapters and confirm they match
    python name_voices.py                   # DRY RUN: show what unnamed adapters
                                            #   would be renamed to
    python name_voices.py --apply           # actually rename dirs + update manifest
    python name_voices.py --apply --overwrite   # also re-name already-named adapters

Pure standard library — no ML dependencies — so it runs under any interpreter.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODELS_DIR = os.path.join(SCRIPT_DIR, "lora_models")
DEFAULT_MANIFEST = os.path.join(DEFAULT_MODELS_DIR, "manifest.json")

# ── Naming vocabulary (derived from, and verified against, the existing corpus) ──

# Timbre: the descriptive adjective(s) before the vocal register. When more than
# one is present we keep the most specific — "warm"/"bright" are generic fallbacks.
TIMBRE_PRIORITY = ["silky", "husky", "gravelly", "velvety", "crisp", "breathy", "warm", "bright"]

REGISTERS = ["soprano", "mezzo", "alto", "tenor", "baritone", "bass"]
FEMALE_REGISTERS = {"soprano", "mezzo", "alto"}

# Genre token <- keyword(s), scanned in priority order (first match wins). Only the
# audience clause ("best for …") is searched, so genre words used to colour the
# description ("gothic menace", "sci-fi world") don't leak into the name.
GENRE_RULES = [
    ("cyberpunk",    ["cyberpunk"]),
    ("postapoc",     ["post-apocalyptic", "postapoc", "post apocalyptic"]),
    ("anime",        ["anime", "isekai"]),
    ("gothic",       ["gothic"]),
    ("supernatural", ["supernatural"]),
    ("fantasy",      ["fantasy"]),
    ("scifi",        ["sci-fi", "scifi", "science fiction"]),
    ("military",     ["military"]),
    ("literary",     ["literary"]),
]


def derive_base_slug(voice_profile: str, mean_f0=None) -> str:
    """Derive the un-suffixed descriptive slug from a prose profile + mean f0.

    Verified to reproduce every existing slug in the shipped manifest.
    """
    p = (voice_profile or "").lower()

    # Register, and the text before it (timbre lives before the register word)
    reg_positions = {r: p.find(r) for r in REGISTERS if r in p}
    if reg_positions:
        register = min(reg_positions, key=reg_positions.get)
        before = p[:reg_positions[register]]
    else:
        register, before = "", p

    before_words = set(re.findall(r"[a-z]+", before))
    timbre = next((t for t in TIMBRE_PRIORITY if t in before_words), None)
    if timbre is None:
        all_words = re.findall(r"[a-z]+", p)
        timbre = next((t for t in TIMBRE_PRIORITY if t in set(all_words)),
                      all_words[0] if all_words else "voice")

    decades = re.findall(r"(\d+)s", p)
    age = (decades[0] + "s") if decades else ""

    if register in FEMALE_REGISTERS:
        gender = "f"
    elif register:
        gender = "m"
    else:
        gender = "f" if (mean_f0 and mean_f0 >= 165) else "m"

    tail = p.split("best for", 1)[1] if "best for" in p else ""
    genre = next((tok for tok, kws in GENRE_RULES if any(k in tail for k in kws)), "")

    parts = [timbre, register, age, gender] + ([genre] if genre else [])
    return "_".join(part for part in parts if part)


def _strip_suffix(slug: str) -> str:
    """Remove a trailing _N collision suffix."""
    return re.sub(r"_\d+$", "", slug)


def _is_named(entry: dict) -> bool:
    """An entry is 'named' once its id differs from the raw dataset stem.

    An entry with NO dataset_id is treated as named (conservative): we can't
    tell it's unnamed, and re-renaming a shipped voice id other configs
    reference is worse than skipping it. Use --overwrite to force.
    """
    ds = entry.get("dataset_id")
    if not ds:
        return True
    return entry.get("id") != ds


def _mean_f0(entry: dict):
    return (entry.get("voice_features") or {}).get("mean_f0")


def assign_unique_names(entries, reserved):
    """Given entries to (re)name and a set of reserved/existing names, return a list
    of (entry, new_name). Bases shared by >1 entry get _1.._n in order; a base used
    by exactly one entry stays bare unless it collides with a reserved name."""
    bases = [derive_base_slug(e.get("voice_profile"), _mean_f0(e)) for e in entries]
    counts = {}
    for b in bases:
        counts[b] = counts.get(b, 0) + 1

    used = set(reserved)
    idx = {}
    result = []
    for entry, base in zip(entries, bases):
        if counts[base] > 1:
            idx[base] = idx.get(base, 0) + 1
            name = f"{base}_{idx[base]}"
            # extremely unlikely, but never clobber a reserved name
            bump = idx[base]
            while name in used:
                bump += 1
                name = f"{base}_{bump}"
        else:
            name = base
            bump = 1
            while name in used:
                bump += 1
                name = f"{base}_{bump}"
        used.add(name)
        result.append((entry, name))
    return result


def cmd_verify(manifest):
    """Re-derive names for already-named adapters and confirm they match."""
    named = [e for e in manifest if _is_named(e) and e.get("voice_profile")]
    print(f"Verifying {len(named)} already-named adapter(s)…\n")
    mismatches = []
    for e in named:
        derived = derive_base_slug(e.get("voice_profile"), _mean_f0(e))
        actual_base = _strip_suffix(e["id"])
        if derived != actual_base:
            mismatches.append((e["id"], derived, (e.get("voice_profile") or "")[:80]))
    if mismatches:
        print(f"✗ {len(mismatches)} mismatch(es):")
        for actual, derived, prof in mismatches:
            print(f"  id={actual}\n  derived_base={derived}\n    {prof}\n")
        return 1
    print(f"✓ All {len(named)} names reproduce exactly from their profile.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--models-dir", default=DEFAULT_MODELS_DIR,
                    help="Folder containing the adapter directories")
    ap.add_argument("--apply", action="store_true",
                    help="Actually rename dirs + update manifest (default is a dry run)")
    ap.add_argument("--overwrite", action="store_true",
                    help="Also re-name adapters that already have a descriptive slug")
    ap.add_argument("--verify", action="store_true",
                    help="Re-derive names for already-named adapters and confirm they match")
    args = ap.parse_args()

    if not os.path.exists(args.manifest):
        print(f"ERROR: manifest not found: {args.manifest}")
        return 1
    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)
    if not isinstance(manifest, list):
        print("ERROR: manifest is not a list of entries.")
        return 1

    if args.verify:
        return cmd_verify(manifest)

    # Candidates: entries with a profile that still need a name (or all, if --overwrite)
    candidates = [e for e in manifest
                  if e.get("voice_profile") and (args.overwrite or not _is_named(e))]
    if not candidates:
        print("Nothing to name. (All profiled adapters already named; use --overwrite to redo.)")
        return 0

    # Reserve the ids of everything we're NOT renaming, so we never collide with
    # them. Reserve by object identity (id()), not dict-equality `in candidates`:
    # a non-candidate entry value-equal to a candidate would otherwise be dropped
    # from the reserved set, and a manifest entry missing 'id' would KeyError.
    candidate_ids = {id(e) for e in candidates}
    untouched_ids = {e.get("id") for e in manifest
                     if id(e) not in candidate_ids and e.get("id")}
    plan = assign_unique_names(candidates, reserved=untouched_ids)

    renames = [(e, new) for e, new in plan if e["id"] != new]
    print(f"{len(candidates)} candidate(s); {len(renames)} would be renamed "
          f"({'APPLY' if args.apply else 'dry run'}).\n")
    for e, new in plan:
        flag = "  " if e["id"] == new else "→ "
        print(f"{flag}{e['id']}")
        if e["id"] != new:
            print(f"     {new}")

    if not args.apply:
        print("\nDry run — nothing changed. Re-run with --apply to rename.")
        return 0

    if not renames:
        print("\nNothing to rename.")
        return 0

    # Backup manifest before touching anything
    backup = args.manifest + ".bak"
    shutil.copy2(args.manifest, backup)
    print(f"\nBacked up manifest → {backup}")

    renamed = 0
    for e, new in renames:
        old_dir = os.path.join(args.models_dir, e["id"])
        new_dir = os.path.join(args.models_dir, new)
        if os.path.isdir(old_dir):
            if os.path.exists(new_dir):
                print(f"  SKIP {e['id']} → {new}: target dir already exists")
                continue
            try:
                os.rename(old_dir, new_dir)
            except OSError as exc:
                print(f"  SKIP {e['id']} → {new}: rename failed ({exc}); manifest entry left unchanged so a re-run can retry it")
                continue
        else:
            print(f"  NOTE: adapter dir missing for {e['id']} (updating manifest only)")
        e["id"] = new
        e["name"] = new
        renamed += 1
        print(f"  renamed → {new}")

    manifest_dir = os.path.dirname(os.path.abspath(args.manifest))
    fd, tmp_manifest = tempfile.mkstemp(prefix=".manifest_", suffix=".json", dir=manifest_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_manifest, args.manifest)
    finally:
        if os.path.exists(tmp_manifest):
            os.remove(tmp_manifest)
    print(f"\n✓ Renamed {renamed} adapter(s); manifest updated ({time.strftime('%H:%M:%S')}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
