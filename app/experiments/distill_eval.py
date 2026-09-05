"""Did distilling the 70B into the 14B actually move anything?

`distill_train` produces an adapter from 1,091 rows the 70B answered on two
books with no gold. This scores it on the four gold books, which share no rows
with the training data, so every number here is transfer rather than recall.

TWO ARMS, ONE LOADED MODEL:

    base     the 14B as it ships
    tuned    the same weights with the LoRA active

They run through the SAME model object, separated only by peft's
`disable_adapter()`. Loading the base and the tuned model separately would let
a different dtype, device map, or tokenizer revision creep into the comparison
and be read as the adapter's effect. Here the adapter is provably the only
difference.

THE PROMPT PATH IS PRODUCTION'S. Both arms call `attribute_batch`, so batching,
JSON repair, the text-freeze validator and the retry policy are identical to
what ships. That is also why this does not talk to llama.cpp: the adapter is
peft-format, so inference runs through transformers behind a shim that mimics
the sliver of the OpenAI client `call_llm_for_entries` touches. The shim exists
to keep the parsing and validation code in the comparison, not to reimplement
it.

WHAT WOULD MAKE THIS A NULL. Training was one entry per example and inference
sends 25, the mismatch `distill_train` records up front. If `tuned` collapses -
answering with one name everywhere, or breaking the JSON contract - the
batch-shape mismatch is the first suspect, and the per-arm unanswered and
distinct-speaker counts printed below are what distinguish "learned nothing"
from "cannot follow the batch format any more". A model that has stopped
producing parseable batches is not a model that failed to learn attribution.

The comparison that matters is not base vs tuned alone. A tuned 14B is only
interesting if it approaches what the 70B cascade buys, so the cascade's
measured gains on these same books are the standard to read it against.
"""
import argparse, collections, contextlib, json, os, random, re, sys, time, warnings

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = REPO + "/app/"
sys.path.insert(0, APP)

from experiments.manifest import ExperimentRecord
from experiments.scoring import alias_groups, same_speaker
from experiments.stats import clopper_pearson, paired
from generate_script import LLMGenParams
from three_pass_generate import (attribute_batch, build_roster,
                                 get_deterministic_named_entry)

M = REPO + "/ab_test_runtime/results/matrix_20260725-115148/"
INPUT_RUN = "qwen3.5-9b-uncensored-hauhaucs-aggressive"
BOOKS = ("grimgar03", "index18", "mushoku16", "owarimonogatari3")
SPECIAL = {"UNKNOWN", "UNNAMED", "NOT_DIALOGUE"}
BATCH = 25


def norm(t):
    return re.sub(r"\W+", "", t or "").lower()


# --- the shim -------------------------------------------------------------
# call_llm_for_entries reads exactly: response.choices[0].message.content,
# .finish_reason, and (getattr-safe) .usage. Nothing else. Anything more
# elaborate would be inventing an interface the caller does not use.

class _Msg:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = None


class _Choice:
    def __init__(self, content, finish_reason):
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, completion_tokens):
        self.prompt_tokens = None
        self.completion_tokens = completion_tokens
        self.completion_tokens_details = None


class _Response:
    def __init__(self, content, finish_reason, completion_tokens):
        self.choices = [_Choice(content, finish_reason)]
        self.usage = _Usage(completion_tokens)


class LocalClient:
    """Mimics the sliver of the OpenAI client that the LLM path touches."""

    def __init__(self, model, tok, thinking_mode="off"):
        self.model, self.tok = model, tok
        self.thinking_mode = thinking_mode
        self.chat = self
        self.completions = self
        self.adapter_enabled = True
        self.diagnostics = []

    def create(self, model=None, messages=None, temperature=0.0, top_p=1.0,
               presence_penalty=0.0, max_tokens=512, extra_body=None):
        import torch
        # Qwen3 emits <think> blocks by default. Production suppresses them
        # through extra_body (reasoning_effort="none"), which a local
        # tokenizer never sees - so without this the arms would run WITH
        # reasoning while the thing they are compared against ran without it,
        # and each call generated thousands of thinking tokens (~7 minutes per
        # batch, against ~8 seconds). Wrong configuration first, slow second.
        template_kwargs = ({"enable_thinking": False}
                           if self.thinking_mode == "off" else
                           {"enable_thinking": True,
                            "reasoning_effort": self.thinking_mode})
        try:
            prompt = self.tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True,
                **template_kwargs)
        except TypeError:
            # Tokenizers without the flag never had the behaviour to disable.
            prompt = self.tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
        enc = self.tok(prompt, return_tensors="pt").to(self.model.device)
        kw = dict(max_new_tokens=max_tokens,
                  pad_token_id=self.tok.pad_token_id or self.tok.eos_token_id)
        # temperature=0 must mean greedy, not a near-zero sample: every other
        # harness in this investigation ran deterministic and a sampled arm
        # would not be comparable to any of them.
        if temperature and temperature > 0:
            kw.update(do_sample=True, temperature=temperature, top_p=top_p)
        else:
            kw.update(do_sample=False)
        try:
            with torch.no_grad():
                out = self.model.generate(**enc, **kw)
        except Exception as exc:
            # RECORD THE CAUSE BEFORE IT BECOMES A SYMPTOM. diagnostics used to
            # be appended only on the success path, so a generate() that raised
            # recorded NOTHING here; three_pass retried, gave up, and raised
            # `PassExhausted: attribution failed for a 16-entry batch`, which is
            # what reached the artifact.
            #
            # On 2026-09-04 that produced an FP8 evaluation with 766 rows, both
            # arms 0 correct, and 428 batch_failed diagnostics whose `attempts`
            # were all empty. The real reason - the model could not fetch
            # `kernels-community/finegrained-fp8` with Hugging Face forced
            # offline - appeared nowhere in the artifact, only in a log on one
            # machine. A reader saw "every batch failed" and could not learn
            # why.
            self.diagnostics.append({
                "finish_reason": "error",
                "generated_tokens": 0,
                "emitted_eos": False,
                "raw_response": None,
                "thinking_mode": self.thinking_mode,
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise
        gen = out[0][enc["input_ids"].shape[1]:]
        text = self.tok.decode(gen, skip_special_tokens=True)
        finish = "length" if len(gen) >= max_tokens else "stop"
        token_ids = gen.tolist() if hasattr(gen, "tolist") else list(gen)
        eos_ids = self.tok.eos_token_id
        if not isinstance(eos_ids, (list, tuple, set)):
            eos_ids = [eos_ids]
        import hashlib
        self.diagnostics.append({
            # The batch's identity. Two base runs disagreed on 3 of 133 rows
            # on 2026-09-04 and nothing could show whether they had been asked
            # the same question: every row's prompt_sha256 was NULL, because
            # nothing ever passed a prompt to record.add.
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
            "finish_reason": finish,
            "generated_tokens": len(token_ids),
            "emitted_eos": bool(token_ids and token_ids[-1] in eos_ids),
            "raw_response": text,
            "thinking_mode": self.thinking_mode,
        })
        return _Response(text, finish, len(token_ids))


def preflight_generation(client, model_name):
    """Prove the model can emit one token before spending hours proving it cannot.

    The FP8 run above attempted 428 batches, each retried four times, against a
    model that could never produce a token. Every attempt failed identically
    and deterministically. One tiny generation first turns that into a
    ten-second abort carrying the real exception.

    Raises SystemExit with the underlying error rather than returning a value,
    because a preflight that can be ignored is not a preflight.
    """
    try:
        response = client.create(
            model=model_name,
            messages=[{"role": "user", "content": "Reply with the word OK."}],
            max_tokens=8)
    except Exception as exc:                                # noqa: BLE001
        raise SystemExit(
            "PREFLIGHT FAILED - the model cannot generate, so no evaluation "
            "below would be a measurement of it: "
            f"{type(exc).__name__}: {exc}")
    if not (response.choices[0].message.content or "").strip():
        raise SystemExit(
            "PREFLIGHT FAILED - the model loaded but generated nothing. An "
            "evaluation now would record an all-empty arm as a model result.")
    return True


def get_book_paths(book, input_dir=None, checkpoint_dir=None):
    source_path = (os.path.join(input_dir, f"{book}.txt") if input_dir
                   else M + f"inputs/{book}.txt")
    checkpoint_path = (
        os.path.join(checkpoint_dir,
                     f"{book}__three_pass.json.threepass_checkpoint.json")
        if checkpoint_dir else
        M + INPUT_RUN + f"/{book}/result.json.threepass_checkpoint.json")
    return source_path, checkpoint_path


def get_model_load_kwargs(torch, quantization_config=None):
    """Return one explicit loader policy for BF16 or NF4 evaluation."""
    kwargs = {"torch_dtype": torch.bfloat16, "device_map": "auto",
              "trust_remote_code": True}
    if quantization_config is not None:
        kwargs["quantization_config"] = quantization_config
    return kwargs


def get_model_loader_name(architectures):
    """Match the model wrapper used to create Qwen3.5 adapter keys."""
    conditional = {"Qwen3_5ForConditionalGeneration",
                   "Qwen3_5MoeForConditionalGeneration"}
    if conditional.intersection(architectures or []):
        return "AutoModelForImageTextToText"
    return "AutoModelForCausalLM"


def apply_inference_seed(torch, seed, deterministic=False):
    """Seed inference and optionally reject nondeterministic operations."""
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if deterministic:
        torch.use_deterministic_algorithms(True)


def load_peft_adapter_or_raise(peft_model, base, adapter):
    """Load an adapter and reject PEFT's plausible-looking inert fallback."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        model = peft_model.from_pretrained(base, adapter)
    missing = [str(item.message) for item in caught
               if "missing adapter keys" in str(item.message).lower()]
    if missing:
        raise RuntimeError(
            "PEFT did not activate the adapter; model wrapper and adapter "
            f"keys are incompatible: {missing[0]}")
    return model


def classify_gold_population(gold, seg):
    """Return scoreable rows and a complete ledger of excluded gold rows."""
    occ = collections.Counter(norm(e.get("text")) for e in seg)
    by_text = {norm(entry.get("text")): entry for entry in seg}
    want, exclusions = {}, []
    for entry in gold["entries"]:
        key = norm(entry["line"])
        speaker = entry["expected_speaker"].upper()
        reason = None
        if speaker in SPECIAL:
            reason = "special_speaker"
        elif occ[key] == 0:
            reason = "missing_from_checkpoint"
        elif occ[key] > 1:
            reason = "duplicate_in_checkpoint"
        elif get_deterministic_named_entry(by_text[key]) is not None:
            reason = "deterministically_resolved"
        if reason:
            exclusions.append({
                "id": entry["id"], "expected_speaker": speaker,
                "reason": reason, "checkpoint_occurrences": occ[key],
            })
        else:
            want[key] = entry
    return want, exclusions


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
    want, exclusions = classify_gold_population(gold, seg)
    return gold, src, seg, roster, want, exclusions


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--adapter", default=REPO + "/ab_test_runtime/distill/adapter")
    ap.add_argument("--model", default="Qwen/Qwen3-14B")
    ap.add_argument("--books", nargs="+", default=list(BOOKS))
    ap.add_argument("--tag", default=os.environ.get("EXPERIMENT_TAG", "distill"))
    ap.add_argument("--limit", type=int, default=0,
                    help="cap scored rows per book, for a smoke run")
    ap.add_argument("--only-gold-ids", default="",
                    help="comma-separated gold ids; only windows containing "
                         "one of them are run. Repeats the exact batches that "
                         "disagreed instead of a whole book - three rows cost "
                         "two batches, not 62 windows.")
    # A 25-entry response is ~25 objects of {n, head, speaker} - about 600
    # tokens. The production default of 12000 exists for a server that stops at
    # EOS; here a degenerate generation runs the full budget at ~15 tok/s, so
    # one bad batch cost ~13 minutes per attempt and ~50 across its retries.
    # Capping bounds the failure without touching well-formed responses.
    ap.add_argument("--max_tokens", type=int, default=2000)
    ap.add_argument("--input-dir")
    ap.add_argument("--checkpoint-dir")
    ap.add_argument("--attribution-mode",
                    choices=("one_pass", "two_step", "stage1_only"),
                    default="one_pass",
                    help="how the speaker is asked for. two_step reasons in "
                         "prose then a model serialises; stage1_only reasons "
                         "then a REGEX serialises, with no second call. All "
                         "three replace ONLY the LLM call, so the text freeze, "
                         "index binding and scoring are identical. Run against "
                         "an adapter, stage1_only asks whether the adapter "
                         "learned attribution even when its JSON is broken - "
                         "tuned>base here with tuned<base under one_pass would "
                         "mean the evaluation has been measuring format "
                         "compliance, not attribution.")
    ap.add_argument("--load-in-4bit", action="store_true",
                    help="load the base with bitsandbytes NF4 when BF16 "
                         "weights exceed available VRAM")
    ap.add_argument("--thinking-mode", choices=("off", "low", "medium", "xhigh"),
                    default="off", help="Qwen chat-template reasoning mode")
    ap.add_argument("--seed", type=int, default=42,
                    help="explicit inference RNG seed, recorded in the artifact")
    ap.add_argument("--deterministic", action="store_true",
                    help="require deterministic PyTorch algorithms and fail "
                         "if an operation has no deterministic implementation")
    args = ap.parse_args()
    only_ids = {i.strip() for i in args.only_gold_ids.split(",") if i.strip()}

    # two_step reasons in prose then serialises. It is injected as a provider
    # so attribute_batch keeps its text freeze, index binding and exhaustion
    # path; only the model call changes, and both modes are scored by the same
    # code. stage1_stats separates "the reasoning never decided" from "the
    # conversion lost the decision" - the distinction the generate_script
    # two-step run could not make, because validation runs only after stage 2.
    provider = None
    stage1_stats = []
    if args.attribution_mode in ("two_step", "stage1_only"):
        from experiments.two_step_attribution import build_provider
        provider = build_provider(stage1_stats, mode=args.attribution_mode)

    import torch
    from transformers import (AutoConfig, AutoModelForCausalLM,
                              AutoModelForImageTextToText, AutoTokenizer)
    from peft import PeftModel

    apply_inference_seed(torch, args.seed, args.deterministic)

    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    config = AutoConfig.from_pretrained(args.model, trust_remote_code=True)
    loader = (AutoModelForImageTextToText
              if get_model_loader_name(config.architectures)
              == "AutoModelForImageTextToText" else AutoModelForCausalLM)
    quantization_config = None
    if args.load_in_4bit:
        from transformers import BitsAndBytesConfig
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True)
    base = loader.from_pretrained(
        args.model, **get_model_load_kwargs(torch, quantization_config))
    model = load_peft_adapter_or_raise(PeftModel, base, args.adapter)
    model.eval()
    client = LocalClient(model, tok, args.thinking_mode)
    # BEFORE the record exists, so a model that cannot generate produces no
    # artifact at all rather than a structurally perfect all-empty one.
    preflight_generation(client, args.model)
    client.diagnostics.clear()   # the probe is not part of the measurement
    params = LLMGenParams(max_tokens=args.max_tokens, context_length=32768,
                          temperature=0.0, attribute_temperature=0.0,
                          top_p=0.8, reasoning_effort="none")

    # There is no LM Studio here, so the environment is stated rather than
    # queried: `parallel` is 1 because generate() is called serially, and
    # `context_length` is what the params actually enforce. The runtime marker
    # keeps this from being read later as an LM Studio run.
    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    environment = {"loaded": True, "context_length": 32768, "parallel": 1,
                   "optimized": None, "runtime": "transformers+peft",
                   "gpu": gpu, "torch": torch.__version__,
                   "dtype": "bfloat16",
                   "quantization": "nf4" if args.load_in_4bit else "none"}
    # The constructor hashes ONE fixture; this run spans four. Record the first
    # for the schema and every book's hash alongside it, so the artifact cannot
    # imply it was scored against a single gold file.
    record = ExperimentRecord(
        "distill_eval", REPO, args.model, f"peft:{args.adapter}",
        # EVERY book, not args.books[0]. ExperimentRecord hashes each one and
        # keeps the singular gold_path/gold_sha256 fields pointing at the first
        # for the consumers that select on them.
        [APP + f"fixtures/attribution_gold_{b}.json" for b in args.books],
        {"temperature": 0.0, "batch": BATCH, "adapter": args.adapter,
         "max_tokens": args.max_tokens, "thinking_mode": args.thinking_mode,
         "seed": args.seed, "deterministic_algorithms": args.deterministic},
        environment=environment,
        notes="Paired base-versus-LoRA evaluation. Training-data provenance "
              "belongs to the adapter's training manifest; this evaluator "
              "does not infer it. Arms share one loaded model and differ only "
              "by peft disable_adapter().")
    # Six hours of GPU with no resume point was a bad trade the first time.
    record.enable_checkpoint(os.path.join(
        REPO, "ab_test_runtime", "experiments",
        f"distill_eval__{args.tag}.json.ckpt"))
    # gold_files is NOT built here. ExperimentRecord already hashes every gold
    # it was handed, and a second definition assigned after the constructor
    # silently overwrote the first with a different shape - dict of book->sha
    # against a list of {gold_path, sha256, lines}. Two answers to one question
    # is the drift Rule 15 exists to stop, and neither side's tests could see
    # the other. One hashing path, in the class every experiment shares.
    record.meta["gold_population"] = {}
    record.meta["generation_diagnostics"] = []

    totals = {"base": [0, 0], "tuned": [0, 0]}
    per_book, answers = {}, {"base": {}, "tuned": {}}
    for book in args.books:
        gold, src, seg, roster, want, exclusions = load_book(
            book, args.input_dir, args.checkpoint_dir)
        record.meta["gold_population"][book] = {
            "fixture_entries": len(gold["entries"]),
            "eligible_entries": len(want),
            "excluded_entries": len(exclusions),
            "exclusions": exclusions,
        }
        groups = alias_groups(gold)
        windows = [list(range(s, min(s + BATCH, len(seg))))
                   for s in range(0, len(seg), BATCH)]
        windows = [w for w in windows
                   if any(norm(seg[i].get("text")) in want for i in w)]
        print(f"\n{book}: {len(want)} scoreable lines, roster {len(roster)}, "
              f"{len(windows)} windows", flush=True)
        for arm in ("base", "tuned"):
            started, scored = time.time(), 0
            for k, win in enumerate(windows, 1):
                if args.limit and scored >= args.limit:
                    break
                send = [i for i in win
                        if get_deterministic_named_entry(seg[i]) is None]
                if not send or not any(norm(seg[i].get("text")) in want
                                       for i in send):
                    continue
                rows = [i for i in send if norm(seg[i].get("text")) in want]
                # BATCH SELECTION. A determinism probe needs to repeat the two
                # batches that disagreed, not all 62 windows of the book. The
                # evaluator could not name a subset, so the only way to re-ask
                # three rows was to re-run everything - hours of GPU to check
                # three lines, which is why it had not been done.
                if only_ids and not any(
                        f"{book}:{want[norm(seg[i].get('text'))]['id']}" in only_ids
                        for i in rows):
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
                ctxmgr = (model.disable_adapter() if arm == "base"
                          else contextlib.nullcontext())
                diagnostic_start = len(client.diagnostics)
                try:
                    with ctxmgr:
                        out = attribute_batch(client, args.model, frozen, params,
                                              roster, neighbor_contexts=ctx,
                                              source_text=src,
                                              entries_provider=provider)
                except Exception as exc:
                    batch_diagnostics = client.diagnostics[diagnostic_start:]
                    record.meta["generation_diagnostics"].append({
                        "arm": arm, "book": book, "window": k,
                        "outcome": "batch_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "attempts": batch_diagnostics,
                    })
                    raw = (batch_diagnostics[-1]["raw_response"]
                           if batch_diagnostics else None)
                    prompt_sha = (batch_diagnostics[-1].get("prompt_sha256")
                                  if batch_diagnostics else None)
                    print(f"  {arm} window {k}: {type(exc).__name__}", flush=True)
                    # A failed batch is a failure, not an absence. Dropping it
                    # would remove from the denominator exactly the rows this
                    # arm could not handle.
                    for i in send:
                        key = norm(seg[i].get("text"))
                        if key in want:
                            g = want[key]
                            row_id = f"{book}:{g['id']}"
                            if record.done(arm, row_id):
                                continue
                            record.add(arm, row_id, g["line"],
                                       g["expected_speaker"].upper(), None, False,
                                       candidates=roster,
                                       provenance=f"{arm}|{book}|batch_failed",
                                       raw=raw)
                            scored += 1
                    continue
                batch_diagnostics = client.diagnostics[diagnostic_start:]
                record.meta["generation_diagnostics"].append({
                    "arm": arm, "book": book, "window": k,
                    "outcome": "accepted", "error": None,
                    "attempts": batch_diagnostics,
                })
                raw = (batch_diagnostics[-1]["raw_response"]
                       if batch_diagnostics else None)
                prompt_sha = (batch_diagnostics[-1].get("prompt_sha256")
                              if batch_diagnostics else None)
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
                               # The roster the model was shown. Without it
                               # in_candidates is None on every row and a blank
                               # answer cannot be told apart from a roster that
                               # never held it - the question the unanswered-row
                               # finding of 2026-08-30 arrived at.
                               candidates=roster,
                               provenance=f"{arm}|{book}", raw=raw,
                               prompt_sha256=prompt_sha)
                    scored += 1
                if k % 20 == 0:
                    print(f"  {arm} {k}/{len(windows)} ...", flush=True)
            rows = [r for r in record.rows
                    if r["arm"] == arm and r["id"].startswith(book + ":")]
            hit = sum(1 for r in rows if r["correct"])
            totals[arm][0] += hit
            totals[arm][1] += len(rows)
            per_book.setdefault(book, {})[arm] = (hit, len(rows))
            answers[arm].update({r["id"]: r["correct"] for r in rows})
            unanswered = sum(1 for r in rows if not r["predicted"])
            distinct = len({(r["predicted"] or "").upper() for r in rows})
            lo, hi = clopper_pearson(hit, max(len(rows), 1))
            print(f"  {arm:6} {hit}/{len(rows)} = {hit/max(len(rows),1)*100:5.1f}%"
                  f"  [{lo:.1f}-{hi:.1f}]  unanswered {unanswered}"
                  f"  distinct names {distinct}  {time.time()-started:.0f}s",
                  flush=True)

    populations = record.meta["gold_population"].values()
    record.meta["gold_population_summary"] = {
        "fixture_entries": sum(row["fixture_entries"] for row in populations),
        "eligible_entries": sum(row["eligible_entries"] for row in populations),
        "excluded_entries": sum(row["excluded_entries"] for row in populations),
    }

    print("\n  per book")
    for book, arms in per_book.items():
        b, t = arms.get("base", (0, 0)), arms.get("tuned", (0, 0))
        d = (t[0] / max(t[1], 1) - b[0] / max(b[1], 1)) * 100
        print(f"    {book:18} base {b[0]/max(b[1],1)*100:5.1f}%  "
              f"tuned {t[0]/max(t[1],1)*100:5.1f}%  {d:+6.1f}")
    p, x, y, n = paired(answers["base"], answers["tuned"])
    tb, tt = totals["base"], totals["tuned"]
    print(f"\n  pooled  base {tb[0]}/{tb[1]} = {tb[0]/max(tb[1],1)*100:.1f}%"
          f"   tuned {tt[0]}/{tt[1]} = {tt[0]/max(tt[1],1)*100:.1f}%")
    print(f"  paired  {(tt[0]/max(tt[1],1) - tb[0]/max(tb[1],1))*100:+.1f} points"
          f"  +{y}/-{x} of {n}  p={p:.4g}")
    print("\n  Read this against the cascade's measured gains on these same "
          "books.\n  A tuned 14B that does not approach them has not replaced "
          "the 70B,\n  whatever its sign.")
    out = record.write(os.path.join(
        REPO, "ab_test_runtime", "experiments",
        f"distill_eval__{args.tag}.json"),
        contract={"expected_arms": ("base", "tuned"),
                  "require_any_prediction": True,
                  "require_raw_response": True,
                  "require_accepted_generation": True})
    print("wrote", out)


if __name__ == "__main__":
    main()
