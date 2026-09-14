"""Cue-book probe for the selection gap, in the spirit of grammar-book-guided
probing (Li et al., LREC 2026): instead of one accuracy, which attribution CUES
does the model get right and which does it get wrong - and when it is wrong,
WHICH wrong name does it pick?

Reads existing predictions (no inference): two_stage_attribution_w3200.json,
2,494 PDNC rows where roster recall was 100%, so every error is a selection
error. Cues are detected from the gold's own context fields.
"""
import collections, json, os, re, sys
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402
art = json.load(open(f"{REPO}/ab_test_runtime/experiments/two_stage_attribution_w3200.json"))
SPEECH = r"(said|says|cried|replied|answered|asked|exclaimed|returned|continued|observed|added|whispered|muttered|called|remarked|repeated|interrupted|inquired|demanded|shouted|murmured|began|resumed|thought)"

golds, alias_of, canon_of = {}, {}, {}
for book in ("prideandprejudice", "thesignofthefour", "theawakening"):
    g = json.load(open(f"{REPO}/app/fixtures/attribution_gold_pdnc_{book}_w3200.json"))
    key = f"attribution_gold_pdnc_{book}_w3200"
    golds[key] = {e["id"]: e for e in g["entries"]}
    groups = g["aliases"] if isinstance(g["aliases"], list) else list(g["aliases"].values())
    a2c = {}
    for grp in groups:
        for a in grp:
            a2c[a.upper()] = grp[0].upper()
    for name in g["roster"]:
        a2c.setdefault(name.upper(), name.upper())
    alias_of[key] = a2c
    canon_of[key] = lambda n, a2c=a2c: a2c.get((n or "").upper(), (n or "").upper())


def names_in(text, a2c):
    """-> canonical names mentioned in text, in order of appearance (longest alias first)."""
    found = []
    up = text.upper()
    for alias in sorted(a2c, key=len, reverse=True):
        for m in re.finditer(r"(?<![A-Z])" + re.escape(alias) + r"(?![A-Z])", up):
            found.append((m.start(), a2c[alias]))
    found.sort()
    return [n for _, n in found]


rows = []
for r in art["rows"]:
    key, gid = r["id"].split(":", 1)
    e = golds[key].get(gid)
    if not e:
        continue
    a2c = alias_of[key]; canon = canon_of[key]
    exp, pred = canon(r["expected"]), canon(r["predicted"])
    inner = e.get("inner_narration") or ""
    after = (e.get("next_context") or "")[:200]
    before = (e.get("prev_context") or "")[-200:]
    tag_after = re.search(r'^[\s"”,]*' + SPEECH + r"\s+([A-Z][\w.\s]{1,30})", after) or \
                re.search(r'^[\s"”,]*([A-Z][\w.\s]{1,30}?)\s+' + SPEECH, after)
    tag_inner = re.search(SPEECH, inner or "", re.I)
    vocatives = [n for n in names_in(e["line"], a2c) if n != exp]
    nearest = None
    b = names_in(before, a2c); a = names_in(after, a2c)
    if inner and names_in(inner, a2c):
        nearest = names_in(inner, a2c)[0]
    elif b or a:
        nearest = b[-1] if b else a[0]
    rows.append({
        "book": key.split("_")[3], "type": e["quote_type"], "exp": exp, "pred": pred, "correct": str(r["correct"]) == "True", "my_correct": exp == pred,
        "tag_names_speaker": bool(inner and exp in names_in(inner, a2c)) or bool(tag_after and exp in names_in(after[:80], a2c)),
        "tag_pronoun_only": bool((tag_inner or tag_after) and not names_in(inner, a2c) and not names_in(after[:80], a2c)),
        "vocative": bool(vocatives), "nearest_is_exp": nearest == exp, "nearest": nearest, "vocatives": vocatives,
        "line": e["line"][:90],
    })
print(f"{len(rows)} rows joined; overall {sum(r['correct'] for r in rows)/len(rows)*100:.1f}%; my alias canon disagrees with the artifact on {sum(r['correct']!=r['my_correct'] for r in rows)} rows\n")


def table(title, keyfn):
    buckets = collections.defaultdict(list)
    for r in rows:
        buckets[keyfn(r)].append(r)
    print(title)
    for k in sorted(buckets, key=lambda k: -len(buckets[k])):
        v = buckets[k]; acc = sum(x["correct"] for x in v) / len(v) * 100
        print(f"  {str(k):48s} n={len(v):5d}  acc={acc:5.1f}%")
    print()


table("by quote type", lambda r: r["type"])
table("by cue: tag names the speaker / pronoun-only tag / no tag", lambda r:
      "tag names speaker" if r["tag_names_speaker"] else "tag with pronoun only" if r["tag_pronoun_only"] else "no tag detected")
table("by vocative (another roster name inside the quote)", lambda r: f"vocative={r['vocative']}")
table("by whether the NEAREST roster mention is the speaker", lambda r: f"nearest_is_speaker={r['nearest_is_exp']}")
table("cue pair: tag-names-speaker x vocative", lambda r: f"tag={r['tag_names_speaker']} vocative={r['vocative']}")
table("cue pair: nearest-is-speaker x vocative", lambda r: f"nearest={r['nearest_is_exp']} vocative={r['vocative']}")

wrong = [r for r in rows if not r["correct"]]
picked_voc = sum(1 for r in wrong if r["pred"] in r["vocatives"])
picked_near = sum(1 for r in wrong if r["nearest"] and r["pred"] == r["nearest"] and r["pred"] not in r["vocatives"])
print(f"WHICH wrong name: of {len(wrong)} errors, picked the ADDRESSEE named in the quote {picked_voc} ({picked_voc/len(wrong)*100:.1f}%), "
      f"picked the nearest other mention {picked_near} ({picked_near/len(wrong)*100:.1f}%), other {len(wrong)-picked_voc-picked_near}")
w_voc = [r for r in rows if r["vocative"]]
print(f"  among rows WITH a vocative (n={len(w_voc)}): wrong {sum(not r['correct'] for r in w_voc)}, of which addressee picked {sum(1 for r in w_voc if not r['correct'] and r['pred'] in r['vocatives'])}")
w_tag = [r for r in rows if r["tag_names_speaker"]]
print(f"  among rows where the TAG NAMES the speaker (n={len(w_tag)}): acc {sum(r['correct'] for r in w_tag)/len(w_tag)*100:.1f}%; wrong picks = addressee {sum(1 for r in w_tag if not r['correct'] and r['pred'] in r['vocatives'])}")
print("\nhand-check: 6 'tag names speaker' rows the model got WRONG")
for r in [r for r in w_tag if not r["correct"]][:6]:
    print(f"  [{r['type']}] exp={r['exp']} pred={r['pred']} voc={r['vocatives']} | {r['line']!r}")
json.dump({"rows": rows, "provenance": provenance(__file__, {"out": sys.argv[1]})},
          open(sys.argv[1], "w"), indent=0)
