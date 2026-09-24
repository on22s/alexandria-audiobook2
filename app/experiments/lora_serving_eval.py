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
import argparse, collections, concurrent.futures, json, os, re, sys, time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = REPO + "/app/"
sys.path.insert(0, APP)
from openai import OpenAI
from experiments.manifest import ExperimentRecord, strict_shared_summary
from experiments.scoring import (alias_groups, roster_membership_names,
                                 same_speaker)
from experiments.stats import clopper_pearson, paired
from generate_script import LLMGenParams
from experiments.attribution_prompt_variants import make_provider
from three_pass_generate import (PassExhausted, attribute_batch, build_roster,
                                 get_deterministic_named_entry)

M = REPO + "/ab_test_runtime/results/matrix_20260725-115148/"
INPUT_RUN = "qwen3.5-9b-uncensored-hauhaucs-aggressive"
SPECIAL = {"UNKNOWN", "UNNAMED", "NOT_DIALOGUE"}
BATCH = 25


def bind_last_attempt(entries, size):
    """Align an exhausted window's final response to its sent entries by `n`.

    validate_attribution rejects the whole response when ONE spoken line comes
    back unnamed, so without this every gold row in the window was recorded as
    batch_failed - including the lines the model answered. The rejected line
    stays unanswered (None); nothing is invented for it. Returns None when the
    response cannot be bound at all (no parseable entries), which keeps the
    batch_failed path for genuine transport failures."""
    if not entries:
        return None
    out = [None] * size
    bound = 0
    for item in entries:
        try:
            n = int(item.get("n"))
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 <= n < size and out[n] is None:
            sp = item.get("speaker")
            sp = sp.strip() if isinstance(sp, str) else None
            out[n] = {"speaker": sp if sp and sp.upper() != "NARRATOR" else None}
            bound += 1
    return out if bound else None


def norm(t):
    return re.sub(r"\W+", "", t or "").lower()


def mentioned_roster(roster, groups, texts, carried=(), cap=30):
    """-> the roster names attested (by name or alias) in `texts`, plus
    `carried` (the previous window's attributed speakers), in roster order,
    capped. XinchaoGou/alexandria-audiobook's related_context() builds its
    roster this way for serial novels - string match on the chunk, not
    embeddings, plus the previous chapter's cast - and it is a cheap attack on
    the usual-suspect prior: a lead who is not on the page is not offered."""
    hay = norm(" ".join(t or "" for t in texts))
    forms = {}
    for name in roster:
        key = norm(name)
        forms[name] = {key} | {a for g in groups if key in g for a in g}
    kept = [name for name in roster
            if any(f and f in hay for f in forms[name])]
    for name in carried:
        if name in roster and name not in kept:
            kept.append(name)
    return kept[:cap]


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
    if gold.get("roster_additions", {}).get("attest_in_source"):
        # PDNC fixtures name characters the way the corpus does ("A WAITER",
        # "THE COUNT"), which the text only ever writes in lower case, so the
        # production speaker-attestation gate (pass_quality.is_attested_name)
        # rejects a correct answer and burns every retry. The corpus cast list
        # is the roster the model is shown and the gold defines correctness,
        # so the gate is switched off for these books: a wrong name simply
        # scores wrong. Recorded in the artifact's environment notes.
        src = None
    occ = collections.Counter(norm(e.get("text")) for e in seg)
    want = {norm(g["line"]): g for g in gold["entries"]
            if occ[norm(g["line"])] == 1
            and g["expected_speaker"].upper() not in SPECIAL}
    return gold, src, seg, roster, want


def window_surround(seg, win, send, chars):
    """The window as the model could see it whole: every entry of the window
    in order, sent entries carrying their frozen index `n` (unsent narration
    carries None), plus up to `chars` characters of segmented text before and
    after the window. The harness otherwise shows a line only its +-1
    neighbours; Michel et al. attribute inside 4,096-token chunks of the
    complete text and every context-size ablation found (2025-26) says the
    surrounding text is where the accuracy is."""
    pos = {i: n for n, i in enumerate(send)}
    entries = [{"type": seg[i]["type"], "text": seg[i]["text"], "n": pos.get(i)} for i in win]

    def gather(indices, take_from_end):
        out, total = [], 0
        for i in indices:
            t = seg[i].get("text") or ""
            if seg[i].get("type") == "SPOKEN":
                t = f"\u201c{t}\u201d"   # the segmenter strips quote marks; put them back as evidence
            if total + len(t) > chars:
                break
            out.append(t)
            total += len(t) + 1
        return " ".join(reversed(out) if take_from_end else out)
    before = gather(range(win[0] - 1, -1, -1), True) if win and win[0] else ""
    after = gather(range(win[-1] + 1, len(seg)), False) if win else ""
    return {"entries": entries, "before": before, "after": after}


def make_windows(n, batch, cuts=()):
    """Fixed-stride windows over n entries, restarted at every index in cuts
    (the default, no cuts, is the product's own windowing)."""
    edges = sorted({0, n, *(c for c in cuts if 0 < c < n)})
    out = []
    for a, b in zip(edges, edges[1:]):
        for s in range(a, b, batch):
            out.append(list(range(s, min(s + batch, b))))
    return out


def get_eval_arms(base_only=False):
    """Return serving arms; a base-only server has no adapter endpoint state."""
    return (("base", None),) if base_only else (("base", 0.0), ("lora", 1.0))


# The product's own default (generate_script.LLMGenParams.max_tokens). This
# harness used to pin 2000 - half the product's room - which Qwen never
# noticed and Muse overflowed on 70% of batch-25 windows (2026-09-12).
MAX_TOKENS = 4096


def get_eval_metadata(base_only=False, batch=BATCH, reasoning_effort="none",
                      max_tokens=MAX_TOKENS, structured_output="auto"):
    """Describe only settings this evaluator controls or directly observes."""
    arms = get_eval_arms(base_only)
    decoding = {"temperature": 0.0, "batch": batch, "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
                # The product's own default since 2026-09-14 (#522 s9.1); the
                # server may still refuse it, which the run log records.
                "structured_output": structured_output,
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
    ap.add_argument("--workers", type=int, default=1,
                    help="windows in flight at once. Throughput only: every "
                         "request is independent and identical to what it "
                         "would be at 1, so the measurement is unchanged and "
                         "a one-slot card reproduces it, slower. The SERVER "
                         "must run --parallel >= this AND -c of (per-slot "
                         "context x workers): llama.cpp divides its context "
                         "pool across slots, so raising parallel alone shrinks "
                         "every slot and truncates prompts silently. Not "
                         "allowed with --roster-mode mentioned.")
    ap.add_argument("--batch-size", type=int, default=BATCH,
                    help="segmented entries per attribution request")
    ap.add_argument("--window-limit", type=int, default=0,
                    help="score at most this many evenly spaced windows per book (0 = all)")
    ap.add_argument("--surround-chars", type=int, default=2000,
                    help="characters of segmented text before and after the window "
                         "handed to prompt variants that show the whole passage")
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                    help="completion budget per request before escalation "
                         "(default: the product's own)")
    ap.add_argument("--reasoning-effort", default="none",
                    choices=("none", "minimal", "low", "medium", "high",
                             "xhigh", "max"))
    ap.add_argument("--prompt-variant", default="default",
                    help="attribution_prompt_variants.VARIANTS: how the question is asked; "
                         "the output contract and gates are unchanged")
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="attribution sampling temperature; 0 is the product's (deterministic). "
                         "Qwen3.5/3.6 thinking mode documents greedy decoding as degrading "
                         "and looping, so those arms pass the model card's value")
    ap.add_argument("--keep-traces", action="store_true",
                    help="store each window's reasoning trace (message.reasoning_content) "
                         "on its rows, to compare how base and adapter reason")
    ap.add_argument("--api-key-env", default=None,
                    help="environment variable holding the API key for a hosted endpoint")
    ap.add_argument("--provider-extra-body", default=None,
                    help='JSON merged into every request body, e.g. \'{"thinking":{"type":"disabled"}}\'')
    ap.add_argument("--roster-mode", default="full", choices=("full", "mentioned"),
                    help="full: the established roster on every window (the product); "
                         "mentioned: only names attested in this or the previous window's "
                         "text, plus the previous window's attributed speakers, cap 30")
    ap.add_argument("--structured-output", default="auto", choices=("auto", "off"),
                    help="request-level JSON schema on attribution calls "
                         "(the product default is auto)")
    ap.add_argument("--input-dir",
                    help="directory containing corrected <book>.txt inputs")
    ap.add_argument("--checkpoint-dir", help="directory containing corrected "
                    "<book>__three_pass.json.threepass_checkpoint.json files")
    ap.add_argument("--window-cuts", help="chapter_cuts.py output: entry "
                    "indices where a window is forced to start")
    ap.add_argument("--cut-arm", choices=("chapter", "control"),
                    help="which index list in --window-cuts to apply")
    args = ap.parse_args()
    if bool(args.window_cuts) != bool(args.cut_arm):
        ap.error("--window-cuts and --cut-arm go together")
    cuts = (json.load(open(args.window_cuts))["books"]
            if args.window_cuts else {})
    if args.batch_size < 1:
        ap.error("--batch-size must be at least 1")

    # A hosted API needs a key and, for DeepSeek, the thinking switch in the
    # request body; a local llama-server needs neither. ConfiguredOpenAI is
    # the product's own wrapper, so the extra body merges exactly as it does
    # for a profile's provider_extra_body.
    api_key = os.environ.get(args.api_key_env, "local") if args.api_key_env else "local"
    client = OpenAI(base_url=args.base_url, api_key=api_key)
    if args.provider_extra_body:
        from llm_provider import ConfiguredOpenAI
        client = ConfiguredOpenAI(client, json.loads(args.provider_extra_body))
    if args.max_tokens < 1:
        ap.error("--max-tokens must be at least 1")
    params = LLMGenParams(max_tokens=args.max_tokens, context_length=32768,
                          temperature=args.temperature, attribute_temperature=args.temperature,
                          top_p=0.8,
                          reasoning_effort=args.reasoning_effort,
                          structured_output=args.structured_output)
    _env = os.environ.get("EXPERIMENT_ENV")
    decoding, notes = get_eval_metadata(
        args.base_only, args.batch_size, args.reasoning_effort, args.max_tokens,
        args.structured_output)
    if cuts:
        decoding["window_cuts"] = {"file": os.path.abspath(args.window_cuts),
                                   "arm": args.cut_arm}
    decoding["prompt_variant"] = args.prompt_variant
    decoding["window_limit"] = args.window_limit
    decoding["roster_mode"] = args.roster_mode
    decoding["temperature"] = args.temperature
    decoding["keep_traces"] = args.keep_traces
    decoding["provider_extra_body"] = args.provider_extra_body
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
        provider = (None if args.prompt_variant == "default" else
                    make_provider(args.prompt_variant, [[n.upper() for n in g] for g in groups]))
        # What `in_candidates` is tested against: the names these roster
        # lines stand for, per ExperimentRecord.add's contract. The roster
        # itself is still what the model is SHOWN.
        membership = roster_membership_names(roster, groups)
        windows = make_windows(len(seg), args.batch_size,
                               cuts.get(book, {}).get(args.cut_arm, ()))
        windows = [w for w in windows
                   if any(norm(seg[i].get("text")) in want for i in w)]
        if args.window_limit and len(windows) > args.window_limit:
            # breadth over depth: the same number of windows from every book,
            # evenly spaced through it, so a nine-novel fixture costs what a
            # four-novel one does and no single long book dominates
            step = len(windows) / args.window_limit
            windows = [windows[int(k * step)] for k in range(args.window_limit)]
        print(f"\n{book}: {len(want)} scoreable lines, roster {len(roster)}, "
              f"{len(windows)} windows", flush=True)
        for arm, scale in get_eval_arms(args.base_only):
            if scale is not None:
                got = set_adapter_scale(args.base_url, scale)
                print(f"  adapter scale now {got}", flush=True)
            started = time.time()
            carried = []
            # One window's work, split so the network call can be done by a
            # pool while every mutation of `record` stays on this thread and in
            # window order. With --workers 1 this runs exactly as it always has.
            def prepare(k, win, carried):
                send = [i for i in win
                        if get_deterministic_named_entry(seg[i]) is None]
                if args.roster_mode == "mentioned":
                    prev = windows[k - 2] if k >= 2 else []
                    shown_roster = mentioned_roster(
                        roster, groups,
                        [seg[i].get("text") for i in list(prev) + list(win)], carried)
                else:
                    shown_roster = roster
                # What the model was SHOWN is what in_candidates must mean
                # (scoring.roster_membership_names): a restricted roster that
                # dropped the gold speaker is recorded as such, not hidden
                # behind the full roster's membership.
                membership = roster_membership_names(shown_roster, groups)
                rows = [i for i in send if norm(seg[i].get("text")) in want]
                if not rows:
                    return None
                if all(record.done(
                        arm,
                        f"{book}:{want[norm(seg[i].get('text'))]['id']}")
                       for i in rows):
                    return None
                frozen = [{"type": seg[i]["type"], "text": seg[i]["text"]}
                          for i in send]
                ctx = [{"previous_context": seg[i - 1] if i else None,
                        "next_context": seg[i + 1] if i + 1 < len(seg) else None}
                       for i in send]
                surround = window_surround(seg, win, send, args.surround_chars)
                return {"k": k, "send": send, "rows": rows, "frozen": frozen,
                        "ctx": ctx, "surround": surround,
                        "shown_roster": shown_roster, "membership": membership}

            def call(p):
                why = f"{arm}|scale={scale}"
                traces = []
                observer = ((lambda rec: traces.append(rec.get("reasoning_content")))
                            if args.keep_traces else None)
                try:
                    out = attribute_batch(client, args.model, p["frozen"], params,
                                          p["shown_roster"], neighbor_contexts=p["ctx"],
                                          source_text=src,
                                          entries_provider=provider,
                                          attempt_observer=observer,
                                          surround=p["surround"])
                except PassExhausted as exc:
                    # The model answered; one line failed the speaker check and
                    # took the window with it. Score what it said, per row.
                    out = bind_last_attempt(exc.last_entries, len(p["send"]))
                    print(f"  {arm} window {p['k']}: PassExhausted, "
                          f"{'scoring last attempt' if out else 'nothing to bind'}",
                          flush=True)
                    why = f"{arm}|scale={scale}|exhausted_last_attempt"
                except Exception as exc:
                    print(f"  {arm} window {p['k']}: {type(exc).__name__}", flush=True)
                    out = None
                return out, why, traces

            def commit(p, out, why, traces):
                if out is None:
                    for i in p["rows"]:
                        g = want[norm(seg[i].get("text"))]
                        if not record.done(arm, f"{book}:{g['id']}"):
                            record.add(arm, f"{book}:{g['id']}", g["line"],
                                       g["expected_speaker"].upper(), None,
                                       False, candidates=p["membership"],
                                       provenance=f"{arm}|batch_failed")
                    return None
                for off, i in enumerate(p["send"]):
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
                               candidates=p["membership"],
                               provenance=why,
                               reasoning=(next((t for t in reversed(traces) if t), None)
                                          if args.keep_traces else None))
                return sorted({str((o or {}).get("speaker") or "").upper()
                               for o in (out or []) if (o or {}).get("speaker")}
                              & set(roster))

            def progress(k):
                if k % 25 == 0:
                    print(f"  {arm} {k}/{len(windows)} ...", flush=True)

            if args.workers > 1:
                # `carried` feeds the NEXT window's roster, so "mentioned" mode is
                # inherently sequential and must not be silently parallelised.
                if args.roster_mode == "mentioned":
                    raise SystemExit(
                        "--workers > 1 cannot be used with --roster-mode mentioned: "
                        "each window's roster depends on the previous window's answer")
                prepared = [q for q in (prepare(k, win, carried)
                                        for k, win in enumerate(windows, 1))
                            if q is not None]
                with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
                    # Results are applied in window order, so the artifact does not
                    # depend on which request finished first.
                    for q, (out, why, traces) in zip(
                            prepared, pool.map(call, prepared)):
                        got = commit(q, out, why, traces)
                        if got is not None:
                            carried = got
                        progress(q["k"])
            else:
                for k, win in enumerate(windows, 1):
                    q = prepare(k, win, carried)
                    if q is None:
                        continue
                    out, why, traces = call(q)
                    got = commit(q, out, why, traces)
                    if got is not None:
                        carried = got
                    progress(k)
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
        strict = strict_shared_summary(record.rows)
        if strict:
            sa, sb = strict["arms"]["base"], strict["arms"]["lora"]
            dropped = {a: len(v) for a, v in strict["dropped_ids_by_arm"].items() if v}
            print(f"  strict  base {sa['correct']}/{sa['n']} = {100*(sa['accuracy'] or 0):.1f}%  "
                  f"lora {sb['correct']}/{sb['n']} = {100*(sb['accuracy'] or 0):.1f}%  "
                  f"{100*((sb['accuracy'] or 0)-(sa['accuracy'] or 0)):+.1f} points  "
                  f"+{strict['paired']['improved']}/-{strict['paired']['regressed']} "
                  f"of {strict['shared_ids']}  p={strict['paired']['p']:.4g}  "
                  f"unanswered {dropped or 'none'}")

    out = record.write(os.path.join(
        REPO, "ab_test_runtime", "experiments",
        f"lora_serving_eval__{args.tag}.json"),
        contract={"expected_arms": tuple(a for a, _ in
                                           get_eval_arms(args.base_only))})
    print("wrote", out)


if __name__ == "__main__":
    main()
