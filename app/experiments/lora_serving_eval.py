"""Does the adapter still work once it is quantised and served by llama.cpp?

The +11.7 was measured on Qwen3-14B in **bf16 through transformers** with a
peft adapter. Nothing anyone can run on a 16GB card looks like that. The
shippable configuration is a **Q4_K_M base with an f16 LoRA through
llama.cpp**, which is a different numeric stack end to end, and quantisation
could eat some or all of the gain.

This measures the shippable configuration against the same gold.

ONE SERVER, ONE LOADED MODEL. llama-server exposes `POST /lora-adapters`, so
the adapter's scale is toggled between arms rather than restarting with
different weights. Two servers would let a different context size, sampler or
build creep into the comparison and be read as the adapter's effect - the same
reason `distill_eval` used peft's `disable_adapter()` instead of loading two
models.

    base   adapter scale 0.0
    lora   adapter scale 1.0

Everything else - prompts, batching, the text-freeze validator, the retry
policy - is the production `attribute_batch` path, identical between arms.

WHAT A SHORTFALL WOULD MEAN. If `lora` lands well below the +11.7 measured in
bf16, the adapter is not broken: Q4 quantisation of the BASE is the most likely
cause, and the fix is a higher-precision base (Q6_K or Q8_0), not retraining.
Reporting a shortfall as "distillation does not work" would be wrong, and the
bf16 result stands on its own artifact.
"""
import argparse, collections, json, os, re, sys, time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = REPO + "/app/"
sys.path.insert(0, APP)
from openai import OpenAI
from experiments.manifest import ExperimentRecord
from experiments.scoring import (alias_groups, roster_membership_names,
                                 same_speaker)
from experiments.stats import clopper_pearson, paired
from generate_script import LLMGenParams
from three_pass_generate import (attribute_batch, build_roster,
                                 get_deterministic_named_entry)

M = REPO + "/ab_test_runtime/results/matrix_20260725-115148/"
INPUT_RUN = "qwen3.5-9b-uncensored-hauhaucs-aggressive"
SPECIAL = {"UNKNOWN", "UNNAMED", "NOT_DIALOGUE"}
BATCH = 25


def norm(t):
    return re.sub(r"\W+", "", t or "").lower()


def set_adapter_scale(base_url, scale):
    """Toggle the served adapter. Verified by reading the state back: a silent
    no-op here would make both arms identical and look like a null result."""
    root = base_url.rsplit("/v1", 1)[0]
    body = json.dumps([{"id": 0, "scale": scale}]).encode()
    req = urllib.request.Request(root + "/lora-adapters", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    urllib.request.urlopen(req, timeout=30).read()
    with urllib.request.urlopen(root + "/lora-adapters", timeout=30) as fh:
        state = json.loads(fh.read())
    got = float(state[0].get("scale", -1))
    if abs(got - scale) > 1e-6:
        raise RuntimeError(f"adapter scale did not take: asked {scale}, "
                           f"server reports {got}")
    return got


def get_book_paths(book, input_dir=None, checkpoint_dir=None):
    source_path = (os.path.join(input_dir, f"{book}.txt") if input_dir
                   else M + f"inputs/{book}.txt")
    checkpoint_path = (
        os.path.join(checkpoint_dir,
                     f"{book}__three_pass.json.threepass_checkpoint.json")
        if checkpoint_dir else
        M + INPUT_RUN + f"/{book}/result.json.threepass_checkpoint.json")
    return source_path, checkpoint_path


def load_book(book, input_dir=None, checkpoint_dir=None):
    gold = json.load(open(APP + f"fixtures/attribution_gold_{book}.json"))
    source_path, checkpoint_path = get_book_paths(
        book, input_dir, checkpoint_dir)
    src = open(source_path, encoding="utf-8").read()
    cp = json.load(open(checkpoint_path))
    seg = cp["segmented"]
    roster = [r.upper() for r in
              build_roster([e for e in (cp.get("named") or []) if e], src)]
    roster = sorted(set(roster) | {n.upper() for n in
                                   gold.get("roster_additions", {}).get("names", [])})
    occ = collections.Counter(norm(e.get("text")) for e in seg)
    want = {norm(g["line"]): g for g in gold["entries"]
            if occ[norm(g["line"])] == 1
            and g["expected_speaker"].upper() not in SPECIAL}
    return gold, src, seg, roster, want


def get_eval_arms(base_only=False):
    """Return serving arms; a base-only server has no adapter endpoint state."""
    return (("base", None),) if base_only else (("base", 0.0), ("lora", 1.0))


def get_eval_metadata(base_only=False):
    """Describe only settings this evaluator controls or directly observes."""
    arms = get_eval_arms(base_only)
    decoding = {"temperature": 0.0, "batch": BATCH, "max_tokens": 2000,
                "arms": [arm for arm, _ in arms]}
    if base_only:
        notes = (
            "Base-only serving evaluation; no adapter was loaded or toggled. "
            "The evaluator does not observe base quantisation or adapter "
            "precision and makes no claim about either.")
    else:
        notes = (
            "Paired serving evaluation. Arms share one server and differ only "
            "by adapter scale, toggled via POST /lora-adapters. The evaluator "
            "does not observe base quantisation or adapter precision and makes "
            "no claim about either.")
    return decoding, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--books", nargs="+",
                    default=["grimgar03", "owarimonogatari3"])
    ap.add_argument("--model", default="qwen/qwen3-14b")
    ap.add_argument("--base_url", default="http://127.0.0.1:8090/v1")
    ap.add_argument("--tag", default="local-rocm-lora")
    ap.add_argument("--base-only", action="store_true",
                    help="score an untuned server without querying its empty "
                         "/lora-adapters state")
    ap.add_argument("--input-dir",
                    help="directory containing corrected <book>.txt inputs")
    ap.add_argument("--checkpoint-dir", help="directory containing corrected "
                    "<book>__three_pass.json.threepass_checkpoint.json files")
    args = ap.parse_args()

    client = OpenAI(base_url=args.base_url, api_key="local")
    params = LLMGenParams(max_tokens=2000, context_length=32768,
                          temperature=0.0, attribute_temperature=0.0,
                          top_p=0.8, reasoning_effort="none")
    _env = os.environ.get("EXPERIMENT_ENV")
    decoding, notes = get_eval_metadata(args.base_only)
    record = ExperimentRecord(
        "lora_serving_eval", REPO, args.model, args.base_url,
        # Every book, so gold_files covers every row this run scores.
        [APP + f"fixtures/attribution_gold_{b}.json" for b in args.books],
        decoding,
        environment=json.loads(_env) if _env else None,
        notes=notes)
    record.enable_checkpoint(os.path.join(
        REPO, "ab_test_runtime", "experiments",
        f"lora_serving_eval__{args.tag}.json.ckpt"))
    per_book, answers = {}, {"base": {}, "lora": {}}
    for book in args.books:
        gold, src, seg, roster, want = load_book(
            book, args.input_dir, args.checkpoint_dir)
        groups = alias_groups(gold)
        # What `in_candidates` is tested against: the names these roster
        # lines stand for, per ExperimentRecord.add's contract. The roster
        # itself is still what the model is SHOWN.
        membership = roster_membership_names(roster, groups)
        windows = [list(range(s, min(s + BATCH, len(seg))))
                   for s in range(0, len(seg), BATCH)]
        windows = [w for w in windows
                   if any(norm(seg[i].get("text")) in want for i in w)]
        print(f"\n{book}: {len(want)} scoreable lines, roster {len(roster)}, "
              f"{len(windows)} windows", flush=True)
        for arm, scale in get_eval_arms(args.base_only):
            if scale is not None:
                got = set_adapter_scale(args.base_url, scale)
                print(f"  adapter scale now {got}", flush=True)
            started = time.time()
            for k, win in enumerate(windows, 1):
                send = [i for i in win
                        if get_deterministic_named_entry(seg[i]) is None]
                rows = [i for i in send if norm(seg[i].get("text")) in want]
                if not rows:
                    continue
                if all(record.done(
                        arm,
                        f"{book}:{want[norm(seg[i].get('text'))]['id']}")
                       for i in rows):
                    continue
                frozen = [{"type": seg[i]["type"], "text": seg[i]["text"]}
                          for i in send]
                ctx = [{"previous_context": seg[i - 1] if i else None,
                        "next_context": seg[i + 1] if i + 1 < len(seg) else None}
                       for i in send]
                try:
                    out = attribute_batch(client, args.model, frozen, params,
                                          roster, neighbor_contexts=ctx,
                                          source_text=src)
                except Exception as exc:
                    print(f"  {arm} window {k}: {type(exc).__name__}", flush=True)
                    for i in rows:
                        g = want[norm(seg[i].get("text"))]
                        if not record.done(arm, f"{book}:{g['id']}"):
                            record.add(arm, f"{book}:{g['id']}", g["line"],
                                       g["expected_speaker"].upper(), None,
                                       False, candidates=membership,
                                       provenance=f"{arm}|batch_failed")
                    continue
                for off, i in enumerate(send):
                    key = norm(seg[i].get("text"))
                    if key not in want:
                        continue
                    g = want[key]
                    row_id = f"{book}:{g['id']}"
                    if record.done(arm, row_id):
                        continue
                    sp = (out[off] or {}).get("speaker") if off < len(out) else None
                    record.add(arm, row_id, g["line"],
                               g["expected_speaker"].upper(), sp,
                               same_speaker(g["expected_speaker"], sp, groups),
                               # The roster the model was shown; see distill_eval.
                               candidates=membership,
                               provenance=f"{arm}|scale={scale}")
                if k % 25 == 0:
                    print(f"  {arm} {k}/{len(windows)} ...", flush=True)
            arm_rows = [r for r in record.rows
                        if r["arm"] == arm and r["id"].startswith(book + ":")]
            hit = sum(1 for r in arm_rows if r["correct"])
            per_book.setdefault(book, {})[arm] = (hit, len(arm_rows))
            answers[arm].update({r["id"]: r["correct"] for r in arm_rows})
            lo, hi = clopper_pearson(hit, max(len(arm_rows), 1))
            unanswered = sum(1 for r in arm_rows if not r["predicted"])
            print(f"  {arm:5} {hit}/{len(arm_rows)} = "
                  f"{hit/max(len(arm_rows),1)*100:5.1f}%  [{lo:.1f}-{hi:.1f}]  "
                  f"unanswered {unanswered}  {time.time()-started:.0f}s",
                  flush=True)

    print("\n  per book")
    for book, arms in per_book.items():
        b = arms.get("base", (0, 0))
        line = f"    {book:18} base {b[0]/max(b[1],1)*100:5.1f}%"
        if not args.base_only:
            l = arms.get("lora", (0, 0))
            line += (f"  lora {l[0]/max(l[1],1)*100:5.1f}%  "
                     f"{(l[0]/max(l[1],1)-b[0]/max(b[1],1))*100:+6.1f}")
        print(line)
    tb = sum(v["base"][0] for v in per_book.values())
    nb = sum(v["base"][1] for v in per_book.values())
    print(f"\n  pooled  base {tb}/{nb} = {tb/max(nb,1)*100:.1f}%")
    if not args.base_only:
        p, x, y, n = paired(answers["base"], answers["lora"])
        tl = sum(v["lora"][0] for v in per_book.values())
        nl = sum(v["lora"][1] for v in per_book.values())
        print(f"  pooled  lora {tl}/{nl} = {tl/max(nl,1)*100:.1f}%")
        print(f"  paired  {(tl/max(nl,1)-tb/max(nb,1))*100:+.1f} points  "
              f"+{y}/-{x} of {n}  p={p:.4g}")

    out = record.write(os.path.join(
        REPO, "ab_test_runtime", "experiments",
        f"lora_serving_eval__{args.tag}.json"),
        contract={"expected_arms": tuple(a for a, _ in
                                           get_eval_arms(args.base_only))})
    print("wrote", out)


if __name__ == "__main__":
    main()
