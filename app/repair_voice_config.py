"""Find and merge characters cast in two voices under two spellings.

`voice_config.json` is keyed by the raw speaker string the annotator produced.
When a character appears as both 'Anastasia' and 'ANASTASIA', each spelling gets
its OWN entry, and `tts.py`'s `voice_config.get(speaker)` resolves both exactly
- so canonicalising at lookup time would never fire. The duplicate has to be
removed from the data.

Measured on the live book, 2026-08-03: EIGHT characters split this way, 238
lines belonging to them, at least 32 spoken in the wrong voice. Not a subtle
drift - 'Anastasia' is lora/Ryan across 68 lines while 'ANASTASIA' is
custom/Aiden across 2. Man 1 and Man 2 split almost evenly, so those characters
audibly change voice mid-scene.

WHICH ENTRY WINS. The more deliberately configured one: a `lora`, `clone` or
`design` voice was made for that character on purpose, while the losing entries
here are all the same `custom` fallback voice at seed -1, i.e. auto-created.
Line count breaks ties only. Ranking by lines FIRST was tried and is wrong - it
gave PUCK the auto-created custom voice over a character LoRA on a 1-vs-0 count.

Where the two rules disagree the choice is genuinely arguable, so those merges
are flagged as disputed and listed at the end rather than buried in a sort key.

REPORT ONLY BY DEFAULT. `--apply` writes, and always backs up first. This edits
a file the user has hand-tuned through the UI; it must never be a silent fix.

WHAT THIS DOES NOT DO. It does not stop the duplicates being recreated.
`generate_personas.py` writes `voice_config[speaker]` under whatever spelling
the annotator emitted, so a later persona run can reintroduce the split. Fixing
that means canonicalising at write time and is a separate change.
"""
import argparse, collections, copy, json, os, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "app"))
from voice_config_store import apply_voice_config_update

DEFAULT_CONFIG = os.path.join(REPO, "voice_config.json")
DEFAULT_ALIASES = os.path.join(REPO, "character_aliases.json")
DEFAULT_SCRIPT = os.path.join(REPO, "chunks.json")

# A deliberately configured voice beats one that is just the default shape.
# `clone` belongs here for the same reason as `lora`: someone supplied
# reference audio for that character. Omitting it ranked a clone below the
# auto-created custom entry it was competing with.
TYPE_RANK = {"lora": 3, "builtin_lora": 3, "clone": 3, "ensemble": 3, "design": 2, "custom": 1}


def get_canonical_alias_context(aliases):
    """Validate aliases and prepare the shared normalized lookup once."""
    from speaker_identity import get_validated_alias_map, get_speaker_label_index
    validated = get_validated_alias_map({} if aliases is None else aliases)
    return validated, get_speaker_label_index(validated)


def get_canonical_name(name, aliases, label_index):
    """Return the readable uppercase target using a prepared alias context."""
    if not name:
        return ""
    from speaker_identity import _identity_key
    matched = label_index.get(_identity_key(name))
    return (aliases[matched] if matched is not None else name).upper()


def canonical(name, aliases):
    """Speaker string -> canonical identity using shared generation matching."""
    if not name:
        return ""
    validated, label_index = get_canonical_alias_context(aliases)
    return get_canonical_name(name, validated, label_index)


def entry_of(config, key):
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def voice_signature(entry):
    """What makes two entries audibly identical.

    THE FIELD NAMES MATTER AND THE FIRST VERSION HAD THEM WRONG. It compared
    "adapter" and "style", which do not exist in voice_config.json - the real
    keys are `adapter_id` and `character_style`. Every LoRA entry therefore
    looked identical on ('lora', 'Ryan', None, '-1', None) regardless of which
    of the 78 adapters it actually used.

    That is not a cosmetic slip. It reported NATSUKI SUBARU - the protagonist,
    412 lines across two spellings - as SAME VOICE while one spelling was
    silky_baritone_30s_m_fantasy and the other breathy_alto_50s_f_fantasy, a
    male baritone and a fifty-year-old female alto. Subaru was also the
    fixture used to test the "harmless duplication" path, so the bug and its
    test agreed with each other.

    `ref_audio` is included because a clone voice's identity is its reference
    sample; two clone entries agreeing on everything else but pointing at
    different audio are different voices.
    """
    return (entry.get("type"), entry.get("voice"), entry.get("adapter_id"),
            entry.get("seed"), entry.get("character_style"),
            entry.get("ref_audio"),
            json.dumps(entry.get("members"), sort_keys=True) if entry.get("type") == "ensemble" else None)


def find_splits(config, aliases, line_counts):
    """-> [{canonical, keys, winner, reason, signatures}] for split characters.

    Only characters whose spellings disagree on the VOICE are reported. Two
    spellings sharing one voice are harmless duplication, not a defect, and
    flagging them would bury the eight that matter.
    """
    from speaker_identity import _identity_key
    groups = collections.defaultdict(list)
    displays = {}
    validated, label_index = get_canonical_alias_context(aliases)
    for key in config:
        if isinstance(config.get(key), dict):
            display = get_canonical_name(key, validated, label_index)
            displays[key] = display
            identity = _identity_key(display)
            group = ("identity", identity) if identity else ("empty", display)
            groups[group].append(key)

    splits = []
    for keys in sorted(groups.values(), key=lambda keys: min(displays[key] for key in keys)):
        canon = min(displays[key] for key in keys)
        if len(keys) < 2:
            continue
        sigs = {k: voice_signature(entry_of(config, k)) for k in keys}
        if len(set(sigs.values())) < 2:
            continue
        # A fixed custom seed is evidence of deliberate configuration, too.
        # Compute ranks once for both winner selection and ambiguity checks.
        ranks = {}
        for key in keys:
            entry = entry_of(config, key)
            rank = TYPE_RANK.get(entry.get("type"), 0)
            if entry.get("type") == "custom":
                try:
                    seed = int(entry.get("seed", -1))
                except (TypeError, ValueError, OverflowError):
                    seed = -1
                if seed >= 0:
                    rank = TYPE_RANK["design"]
            ranks[key] = rank
        # Deliberate configuration outranks line count; existing voice-type
        # priority still prefers a LoRA over an unseeded custom fallback.
        ranked = sorted(
            keys,
            key=lambda k: (ranks[k], line_counts.get(k, 0)),
            reverse=True)
        winner = ranked[0]
        by_lines = max(keys, key=lambda k: line_counts.get(k, 0))
        # Where the two rules disagree the choice is genuinely arguable, so it
        # is surfaced rather than buried in a sort key.
        disputed = (by_lines != winner
                    and line_counts.get(by_lines, 0) > line_counts.get(winner, 0))
        reason = "richer voice configuration"
        if disputed:
            reason += (f"; NOTE {by_lines!r} has more lines "
                       f"({line_counts.get(by_lines, 0)} vs "
                       f"{line_counts.get(winner, 0)})")
        # When winning candidates are DELIBERATE voices of the same rank, there
        # is no principled winner and line count is a coin flip. Subaru is the
        # case in point: 244 lines on breathy_alto_50s_f_fantasy against 168 on
        # silky_baritone_30s_m_fantasy, for a male protagonist. Picking by
        # lines would give him a fifty-year-old female alto for the whole book.
        # Nothing here knows a character's gender or intent, so it must not
        # guess - these are reported and skipped unless --force-ambiguous.
        best_rank = max(ranks.values())
        ambiguous = (best_rank >= TYPE_RANK["design"] and
                     len({sigs[k] for k in keys if ranks[k] == best_rank}) > 1)
        if ambiguous:
            reason = ("AMBIGUOUS - conflicting deliberate voice configurations; "
                      "needs a human choice")
        splits.append({"canonical": canon, "keys": ranked, "winner": winner,
                       "reason": reason, "disputed": disputed,
                       "ambiguous": ambiguous,
                       "lines": {k: line_counts.get(k, 0) for k in ranked},
                       "signatures": {k: sigs[k] for k in ranked}})
    return splits


def apply_merges(config, splits, force_ambiguous=False):
    """Point every spelling at the winner's settings. Returns a NEW dict.

    Keys are kept rather than deleted: the script still refers to them by their
    original spelling, and removing them would send those lines to a fallback
    voice - trading a wrong voice for no voice.
    """
    merged = dict(config)
    for split in splits:
        if split.get("ambiguous") and not force_ambiguous:
            continue
        winning = dict(entry_of(config, split["winner"]))
        for key in split["keys"]:
            if key != split["winner"]:
                merged[key] = dict(winning)
    return merged



def get_unseeded_voice_seed_changes(raw):
    """Preview the existing opt-in seed policy without changing any entry."""
    from utils import character_voice_seed
    config = raw["characters"] if isinstance(raw.get("characters"), dict) else raw
    return [{"name": name, "seed": str(character_voice_seed(name))}
            for name, entry in config.items() if isinstance(entry, dict)
            and str(entry.get("seed", "-1")).strip() in ("", "-1")]


def apply_unseeded_voice_seeds(raw):
    """Return a detached config with only the previewed seeds replaced."""
    updated = copy.deepcopy(raw)
    config = updated["characters"] if isinstance(updated.get("characters"), dict) else updated
    for change in get_unseeded_voice_seed_changes(raw):
        config[change["name"]]["seed"] = change["seed"]
    return updated


def seed_characters(args):
    """Offer stable character seeds without changing defaults or existing audio.

    Individual generation honors character seeds; fast-batch generation uses
    a separate batch seed. Applying this opt-in repair can change regenerated
    audio and does not establish audible identity across a whole book.
    """
    with open(args.config, encoding="utf-8") as source:
        raw = json.load(source)
    nested = isinstance(raw.get("characters"), dict)
    config = raw["characters"] if nested else raw

    changes = [(change["name"], str(config[change["name"]].get("seed", "-1")).strip() or "(none)", change["seed"])
               for change in get_unseeded_voice_seed_changes(raw)]

    if not changes:
        print("Every character already has a stable seed. Nothing to do.")
        return

    print(f"{len(changes)} of {len(config)} characters have unseeded settings:\n")
    for name, was, now in changes[:40]:
        print(f"  {name[:34]:36} {was:>6} -> {now}")
    if len(changes) > 40:
        print(f"  ... and {len(changes) - 40} more")
    print("\n  Individual rendering uses character seeds. Fast batches use a separate batch seed.")
    print("  Existing rendered audio is kept; regenerated audio may sound different.")

    if not args.apply:
        print("\nReport only. Re-run with --apply to write.")
        return

    backup = f"{args.config}.bak-{time.time_ns()}"

    apply_voice_config_update(args.config, apply_unseeded_voice_seeds, expected=raw,
                              backup_path=backup)
    print(f"\nSeeded {len(changes)} characters. Backup at {backup}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--aliases", default=DEFAULT_ALIASES)
    ap.add_argument("--script", default=DEFAULT_SCRIPT,
                    help="chunks.json or annotated_script.json, for line counts")
    ap.add_argument("--force-ambiguous", action="store_true",
                    dest="force_ambiguous",
                    help="also merge splits where every spelling is a "
                         "deliberate voice and the tool cannot know which is "
                         "intended")
    ap.add_argument("--apply", action="store_true",
                    help="write the merge (backs up first); default is report only")
    ap.add_argument("--seed-characters", action="store_true",
                    dest="seed_characters",
                    help="offer stable per-name seeds for unseeded character settings. "
                         "Individual rendering uses these; fast batches use a separate "
                         "batch seed. Reports by default; needs --apply to write.")
    args = ap.parse_args()

    if args.seed_characters:
        return seed_characters(args)

    with open(args.config, encoding="utf-8") as source:
        raw = json.load(source)
    config = raw.get("characters") if isinstance(raw.get("characters"), dict) else raw
    aliases = {}
    if os.path.exists(args.aliases):
        with open(args.aliases, encoding="utf-8") as source:
            aliases = json.load(source)

    line_counts = collections.Counter()
    if os.path.exists(args.script):
        with open(args.script, encoding="utf-8") as source:
            doc = json.load(source)
        entries = doc if isinstance(doc, list) else (doc.get("entries") or [])
        for e in entries:
            if isinstance(e, dict) and e.get("speaker"):
                line_counts[e["speaker"]] += 1

    splits = find_splits(config, aliases, line_counts)
    if not splits:
        print("No character is cast in two voices. Nothing to repair.")
        return

    affected = sum(sum(s["lines"].values()) for s in splits)
    wrong = sum(sum(n for k, n in s["lines"].items() if k != s["winner"])
                for s in splits)
    print(f"{len(splits)} characters cast in more than one voice\n")
    for s in splits:
        print(f"  {s['canonical']}  (keeping {s['winner']!r} - {s['reason']})")
        for k in s["keys"]:
            mark = "KEEP" if k == s["winner"] else "->  "
            print(f"    {mark} {k!r:24} {s['lines'][k]:4} lines  "
                  f"{s['signatures'][k]}")
    disputed = [s for s in splits if s.get("disputed")]
    print(f"\n  {affected} lines belong to these characters; "
          f"{wrong} are spoken in the losing voice.")
    if disputed:
        print(f"  {len(disputed)} choice(s) arguable - the line-majority "
              f"spelling lost to a richer\n  voice type. Review these before "
              f"applying: "
              f"{', '.join(s['canonical'] for s in disputed)}")

    if not args.apply:
        print("\n  Report only. Re-run with --apply to merge (a backup is "
              "written first).")
        return

    backup = f"{args.config}.bak-{time.time_ns()}"

    def apply_reported_merges(current):
        updated = dict(current)
        if isinstance(current.get("characters"), dict):
            updated["characters"] = apply_merges(current["characters"], splits,
                                                  args.force_ambiguous)
        else:
            updated = apply_merges(current, splits, args.force_ambiguous)
        return updated

    updated = apply_voice_config_update(args.config, apply_reported_merges,
                                        expected=raw, backup_path=backup)
    saved_config = updated["characters"] if isinstance(updated.get("characters"), dict) else updated
    merged_lines = sum(line_counts.get(key, 0) for key in config
                       if voice_signature(entry_of(config, key)) !=
                       voice_signature(entry_of(saved_config, key)))
    print(f"\n  backed up to {backup}")
    print(f"  merged {merged_lines} lines onto their character's main voice")


if __name__ == "__main__":
    main()
