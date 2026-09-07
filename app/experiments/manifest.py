"""Durable record for an attribution experiment.

Aggregate tables cannot support an architecture decision: a later reader cannot
tell a real result from a prompt, roster, alias, indexing or scoring difference.
Every run writes its environment, its exact inputs, and one record per scored
line, so any number in a report can be recomputed from the artifact.

Process idleness is recorded from LM Studio and the app's own state, not
inferred from a process search - `pgrep -f` matched its own command line three
times during the 2026-07-26 experiments and gave the wrong answer each time.
"""
import collections
import hashlib
import json
import os
import platform
import subprocess
import time


def _sha(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _source_fingerprint(directory):
    """Hash of every harness source file, in name order."""
    digest = hashlib.sha256()
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(directory, name), "rb") as handle:
            digest.update(name.encode("utf-8"))
            digest.update(handle.read())
    return digest.hexdigest()


def _git_state(repo):
    def run(*args):
        try:
            out = subprocess.run(args, cwd=repo, capture_output=True, timeout=10)
            return out.stdout.decode("utf-8").strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    # Untracked notes and scratch files do not change behaviour; modified
    # tracked files do. Reporting the former as "dirty" made the flag useless -
    # it was true on every run because three markdown drafts sat in the tree.
    # OUTPUTS ARE NOT INPUT DIRT. A run that rewrites artifacts - which is
    # replay_dirty_evidence's entire job - modifies tracked files, so from its
    # second artifact onward it stamped dirty=True on evidence it had produced
    # in order to BE clean, and gpu_job.sh refused every job behind it (80
    # minutes idle, 2026-08-18). What provenance needs is "is the CODE that
    # produced this committed", not "did anything at all change".
    #
    # DVC makes this split structural: a stage declares deps (inputs, script
    # included) and outs, and dvc.lock hashes them SEPARATELY, so a rewritten
    # output can never look like a changed input. Sacred, whose provenance
    # block this most resembles, calls a bare repo.is_dirty() with no path
    # filter - the same defect - and does not rely on it: what it trusts is
    # the per-file hash of the sources that actually ran.
    #
    # So the excluded half is paid for, not dropped: `read_inputs` below hashes
    # the artifacts a run READ, which catches a locally-edited baseline that
    # this flag could only ever report as an anonymous "something changed".
    # DERIVED INDEXES ARE OUTPUTS TOO. Leaving RESULTS_INDEX.md,
    # results_index.csv and the audit JSON out of this list deadlocked the GPU
    # queue for two hours on 2026-08-19: refresh_indexes.py rewrites them at
    # the end of every chain, and gpu_job.sh's twin of this gate then refused
    # every stage a concurrent chain still had queued. Kept in step with the
    # shell by test_the_shell_gate_agrees_with_the_python_provenance.
    modified = run("git", "status", "--porcelain", "--untracked-files=no",
                   "--",
                   ":(exclude)ab_test_runtime/experiments/*.json",
                   ":(exclude)ab_test_runtime/audit/*.json",
                   ":(exclude)RESULTS_INDEX.md",
                   ":(exclude)results_index.csv",
                   ":(exclude)LEGACY_ATTRIBUTION_AUDIT_*.md",
                   )
    # An untracked harness is the dangerous case, and the first version missed
    # it: a new experiment script is untracked while it runs, so the tree
    # reported clean and the artifact claimed a commit that did not contain the
    # code that produced it. Untracked .py inside the harness directory is dirt.
    # ab_test_runtime/ is excluded: it is where runs WRITE. Scanning it
    # counted an untracked virtualenv, three cloud_backup_* trees and
    # generated .html views, making this flag true on every run (2026-08-29).
    untracked = [n for n in (run("git", "ls-files", "--others",
                                 "--exclude-standard",
                                 "--", ":(exclude)ab_test_runtime/*")
                             or "").splitlines()
                 if n.endswith((".py", ".sh", ".js", ".html"))]
    return {"commit": run("git", "rev-parse", "HEAD"),
            "branch": run("git", "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(modified) or bool(untracked),
            "modified_tracked_files": (modified or "").splitlines() or None,
            "untracked_harness_files": untracked or None,
            # The commit identifies the repository; this identifies the code
            # that actually ran, which is what a later reader needs to trust a
            # number produced from an edited working tree.
            "harness_sha256": _source_fingerprint(os.path.dirname(__file__))}


class EnvironmentCaptureError(RuntimeError):
    """The run's environment could not be recorded, so it is not comparable."""


def lmstudio_state(model_name, base_url=None):
    """What the server actually has loaded, and how it is configured.

    Raises rather than returning an error string. A GPU result whose context
    length and parallel setting are unknown cannot be compared against another
    run, and this project's determinism claim depends on both. The first
    version swallowed a TypeError from calling the helper with the wrong
    signature, and three artifacts shipped with no environment at all.

    ASK THE ENDPOINT WHAT IT IS. This machine runs llama.cpp, not LM Studio,
    and `lms ps` is the wrong instrument for it - against a llama.cpp server it
    reports available/not-loaded for a model answering in 0.2s, which reads as
    "the model is not loaded" and aborts the run. That is not hypothetical:
    local_4book_20260906 died on exactly this at 2026-09-07T01:47Z with the
    served model live on :8090 the whole time.

    `get_current_status` is already the project's one dispatch for this
    question - it probes /props first and falls through to `lms ps` - and is
    what the Setup tab and ensure_ideal_settings use. Calling `get_lmstudio_status`
    directly here was a second, independently-maintained copy of that decision,
    and it drifted exactly the way Rule 15 says it will. base_url stays optional
    so a caller with no endpoint keeps the old path rather than guessing one.
    """
    from lmstudio_settings import get_lmstudio_status
    if base_url:
        from lmstudio_settings import get_current_status
        # llm_mode=None lets is_remote_llm decide from the URL alone, which is
        # the same rule the app applies when the toggle and the URL agree.
        status = get_current_status(None, base_url, model_name)
    else:
        status = get_lmstudio_status(model_name)
    if not isinstance(status, dict) or not status.get("available"):
        raise EnvironmentCaptureError(
            f"model status unavailable for {model_name!r} at "
            f"{base_url or 'the configured LM Studio'}: {status!r}")
    if not status.get("loaded"):
        raise EnvironmentCaptureError(
            f"{model_name!r} is not loaded; refusing to record a run whose "
            "model state is unknown")
    state = {key: status.get(key) for key in
             ("loaded", "context_length", "parallel", "optimized")}
    # WHICH STACK served the run. Two engines answer this question and their
    # numbers are not interchangeable, so an artifact that records context and
    # parallel without recording who reported them is one step short.
    if status.get("runtime"):
        state["runtime"] = status["runtime"]
    # get_lmstudio_status matches on identifier/modelKey, so loaded=True is
    # itself confirmation that *this* model is the one loaded - recorded
    # explicitly rather than re-parsing `lms ps` in a second place.
    state["verified_model"] = model_name
    return state



def completeness(path_or_doc):
    """-> "complete" | "partial" | "unknown" for an artifact.

    ONE definition, because three readers need it and a fourth will. A
    checkpointed artifact is written every few items, so an interrupted run
    leaves a file indistinguishable by eye from a finished one - the n1200
    respelling block was killed at 1129 of 1200 and committed as evidence.

    Truncation is BIASED, not merely small, wherever items are ordered: in the
    respelling runs terms come in book-count order, so the missing tail is the
    rarest words - the ones a pronunciation lexicon exists for.

    Snakemake refuses to proceed on an incomplete output (IncompleteFilesException)
    and Spark expects readers to check the _SUCCESS marker rather than the data
    files. "unknown" is a third answer for artifacts written before the status
    field existed: it warns, because refusing every older artifact would be a
    worse failure than reading one.
    """
    if isinstance(path_or_doc, dict):
        doc = path_or_doc
    else:
        with open(path_or_doc, encoding="utf-8") as handle:
            doc = json.load(handle)
    status = doc.get("status")
    if status in ("complete", "partial"):
        return status
    results, requested = doc.get("results"), doc.get("candidates_considered")
    if isinstance(results, list) and isinstance(requested, int):
        return "complete" if len(results) >= requested else "partial"
    return "unknown"


def read_inputs(paths, repo):
    """-> {relative path: sha256} for the artifacts a run READ.

    THE OTHER HALF OF EXCLUDING OUTPUTS FROM THE DIRTY FLAG. Artifacts are not
    only outputs: 16 scripts here read a committed artifact as input, and the
    -eh baseline every e-row comparison pairs against is one of them. Dropping
    them from the tree check without this would trade a noisy guard for none.

    It is also strictly better than what it replaces. `dirty: true` said
    "something in the tree changed" and named no file a reader could check; a
    hash per input says WHICH input, and lets a later reader confirm the
    baseline they hold is the one that produced the number. That is the
    gold_sha256 pattern already in this file, generalised - and DVC's `deps`
    hashing, arrived at the same way.

    Missing and unreadable inputs are recorded rather than skipped: a run
    scored against a file that was not there is a result about nothing, and
    silence is how that becomes a number nobody questions.
    """
    recorded = {}
    for path in paths or ():
        if not path:
            continue
        key = os.path.relpath(path, repo)
        try:
            with open(path, "rb") as handle:
                recorded[key] = hashlib.sha256(handle.read()).hexdigest()
        except OSError as exc:
            recorded[key] = f"unreadable: {type(exc).__name__}"
    return recorded


def _checked_candidates(candidates):
    """-> the candidate list, or raise if it looks like prompt text.

    See the note at `in_candidates`. The marker is deliberately narrow: it
    catches the one decoration this repo actually produces rather than trying
    to guess at names in general.
    """
    decorated = [c for c in candidates if isinstance(c, str) and "[also:" in c]
    if decorated:
        raise ValueError(
            "candidates must be NAMES, not the lines shown to the model: %r. "
            "Pass the names the roster line stands for (canonical and aliases) "
            "so `in_candidates` means what it says; keep the decoration for the "
            "prompt." % decorated[:2])
    return candidates


def validate_stored_summary(doc):
    """-> problems where an artifact's stored summary disagrees with its rows.

    The check ExperimentRecord.validate() could not perform. Inside a live
    record the summary is derived from the rows, so they agree by
    construction; the disagreement this guards against only exists once the
    two are separate records in a file. Every number quoted from this
    repository is read out of the stored summary block, and nothing verified
    it against the rows beside it.
    """
    problems = []
    stored = doc.get("summary")
    rows = doc.get("rows")
    if not isinstance(stored, dict) or not isinstance(rows, list):
        return problems
    # ONLY THE LONG SCHEMA, where a row names its arm. Several families are
    # WIDE instead - aishell3_score, ljspeech_score and the rest put each arm
    # in its own COLUMN on every row (`lora`, `clone`, `human_vs_human`), so
    # no row carries an `arm` key at all. A first version of this check
    # counted no arms in those files, decided every summary bucket was
    # orphaned, and reported 105 of 397 artifacts inconsistent. All 105 were
    # this mismatch. Judging a schema you do not understand is not a check,
    # it is noise with a number attached.
    if not any(isinstance(r, dict) and r.get("arm") for r in rows):
        return problems
    counted = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("arm"):
            continue
        b = counted.setdefault(row["arm"], {"n": 0, "correct": 0,
                                            "answered": 0})
        b["n"] += 1
        b["correct"] += bool(row.get("correct"))
        predicted = row.get("predicted")
        if predicted is not None and str(predicted).strip():
            b["answered"] += 1
    for arm, bucket in sorted(stored.items()):
        if not isinstance(bucket, dict):
            continue
        seen = counted.get(arm)
        if seen is None:
            problems.append(f"{arm}: summary names an arm with no rows")
            continue
        if "n" in bucket and bucket["n"] != seen["n"]:
            problems.append(f"{arm}: summary n={bucket['n']} but "
                            f"{seen['n']} rows")
        if "correct" in bucket and bucket["correct"] != seen["correct"]:
            problems.append(f"{arm}: summary correct={bucket['correct']} "
                            f"but rows give {seen['correct']}")
    for arm in sorted(set(counted) - set(stored)):
        problems.append(f"{arm}: rows exist for an arm the summary omits")
    return problems


def unanswered_arms(doc):
    """-> {arm: n} for arms holding rows where NOTHING was ever predicted.

    DELIBERATELY NOT PART OF validate_stored_summary. That function answers
    one question - does the stored summary follow from the rows beside it -
    and the artifact audit treats a No as fatal, because a number that cannot
    be checked must not be indexed. This is a different question: the summary
    here follows from the rows perfectly, and both are describing a run that
    generated no text.

    Folding the two together made `audit_experiment_artifacts` refuse to build
    at all over five HISTORICAL artifacts, which blocks every regeneration and
    pressures whoever hits it into deleting evidence. A dead run is a fact
    about the corpus and belongs IN the index, not in the way of it. The live
    guard in ExperimentRecord.validate() is what stops another being written.

    Five artifacts on disk qualify as of 2026-09-06: two FP8 pairs at 383 rows
    per arm, two 6-row smoke runs, and one Q6_K serving eval.
    """
    out = {}
    rows = doc.get("rows")
    stored = doc.get("summary")
    if not isinstance(rows, list) or not isinstance(stored, dict):
        return out
    if not any(isinstance(r, dict) and r.get("arm") for r in rows):
        return out
    counted = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("arm"):
            continue
        b = counted.setdefault(row["arm"], {"n": 0, "answered": 0})
        b["n"] += 1
        predicted = row.get("predicted")
        if predicted is not None and str(predicted).strip():
            b["answered"] += 1
    for arm, b in counted.items():
        bucket = stored.get(arm)
        if (b["n"] and not b["answered"] and isinstance(bucket, dict)
                and isinstance(bucket.get("accuracy"), (int, float))):
            out[arm] = b["n"]
    return out


class ExperimentRecord:
    """Collect per-line records, then write one self-describing artifact."""

    def __init__(self, name, repo, model_name, base_url, gold_path,
                 decoding, notes="", environment=None, inputs=None):
        """environment: pass a captured state to skip the live query. Real runs
        leave it None so a missing environment aborts before any GPU time is
        spent; tests supply one so they need no server."""
        self.name = name
        self.started = time.time()
        # A MULTI-BOOK RUN USED TO STAMP ONE BOOK'S GOLD. The nine cloud
        # evaluations of 2026-08-28/29 scored 383 rows across owarimonogatari3
        # (162), mushoku16 (133) and index18 (88) and recorded
        # gold_path=attribution_gold_index18.json with a correct hash - which
        # verifies 88 of 383 rows. A change to the other two golds would have
        # left no trace.
        #
        # `gold_files` is {book: sha256}, which is NOT a new shape: 34 committed
        # artifacts already carry exactly that, written by three callers that
        # each built it themselves after construction. Building it here instead
        # is the Rule 15 half - one answer to one question - and keeping their
        # shape is the other, because a second shape in a corpus that already
        # has one is worse than the gap it would close.
        #
        # The singular gold_path/gold_sha256/gold_lines keep their meaning for
        # the first file: narration_signal.py and length_bins.py both select
        # artifacts by os.path.basename(meta["gold_path"]) and must not start
        # matching nothing.
        gold_paths = ([gold_path] if isinstance(gold_path, (str, bytes, os.PathLike))
                      else list(gold_path))
        if not gold_paths:
            raise ValueError("a run must declare at least one gold file")
        gold_files = {}
        for one in gold_paths:
            with open(one, "rb") as handle:
                raw = handle.read()
            stem = os.path.basename(one)
            if stem.startswith("attribution_gold_") and stem.endswith(".json"):
                stem = stem[len("attribution_gold_"):-len(".json")]
            gold_files[stem] = hashlib.sha256(raw).hexdigest()
        gold_path = gold_paths[0]
        with open(gold_path, "rb") as handle:
            gold_bytes = handle.read()
        self.meta = {
            "experiment": name,
            "notes": notes,
            "git": _git_state(repo),
            "host": platform.node(),
            "model": model_name,
            "endpoint": base_url,
            "lmstudio": (environment if environment is not None
                         else lmstudio_state(model_name, base_url)),
            "decoding": dict(decoding),
            "gold_path": os.path.relpath(gold_path, repo),
            "gold_sha256": hashlib.sha256(gold_bytes).hexdigest(),
            "gold_lines": len(json.loads(gold_bytes)["entries"]),
            # Every gold this run read, so a multi-book artifact verifies all
            # of its rows and not just the ones from the first book.
            "gold_files": gold_files,
            # Empty when a run reads nothing but its gold; never absent, so
            # "this run declared no inputs" and "this artifact predates the
            # field" stay distinguishable.
            "read_inputs": read_inputs(inputs, repo),
        }
        self.rows = []

    def add(self, arm, gold_id, line, expected, predicted, correct,
            candidates=None, provenance=None, prompt=None, raw=None,
            retries=None, prompt_sha256=None):
        """One scored line. Prompts are hashed; raw responses kept verbatim."""
        self.rows.append({
            "arm": arm,
            "id": gold_id,
            "line": line,
            "expected": expected,
            "predicted": predicted,
            "correct": bool(correct),
            "candidates": candidates,
            "candidate_provenance": provenance,
            # CANDIDATES ARE NAMES, NOT PROMPT TEXT. This is an exact
            # membership test, so a caller passing display lines like
            # "MRS. BENNET [also: BENNET]" gets False for every character that
            # has an alias. two_stage_attribution did exactly that and the
            # artifact reported the expected speaker missing from the cast on
            # 2,250 of 2,494 rows; the true figure was zero, and the mistake
            # inverted the finding - it reads as "the model was never given the
            # answer" when in fact it was given it every single time.
            # Refusing is the only safe response: a silently wrong field is
            # worse than a crash, because it gets analysed.
            "in_candidates": (None if candidates is None
                              else expected in _checked_candidates(candidates)),
            # A caller that never had the prompt text can still identify the
            # batch by passing the hash directly. distill_eval passes neither
            # today, which is why every row it wrote carries a NULL here - and
            # why two base runs that disagreed on 3 of 133 rows could not be
            # shown to have been asked the same question.
            "prompt_sha256": (prompt_sha256 if prompt_sha256 is not None
                              else _sha(prompt) if prompt is not None else None),
            "prompt_chars": len(prompt) if prompt is not None else None,
            "raw_response": raw,
            "retries": retries,
        })
        if getattr(self, "_ckpt_path", None):
            self._ckpt_done.add((arm, gold_id))
            self._ckpt_save()


    # ---- TEMPORARY: row-level checkpoint/resume -------------------------
    # Added 2026-07-27 because the Thunder tunnel dropped twice in ninety
    # minutes, and the second drop killed magistral-small's five-arm run three
    # minutes in. Retry handles a blip; it cannot save a run when the endpoint
    # is gone for minutes, and the arms run for hours.
    #
    # REMOVE THIS when experiments no longer run against a remote endpoint that
    # can vanish mid-run, or when a durable queue replaces ad-hoc scripts. It
    # exists to protect GPU hours, not because resumable experiments are a
    # design goal - a resumed artifact is inherently weaker evidence than a
    # single-process one, for the reason below.
    #
    # THE HAZARD: resume can silently merge rows produced under DIFFERENT
    # configurations, which is worse than losing the run because the artifact
    # still looks valid. So a checkpoint is only adopted when the experiment
    # name, model, endpoint, gold fixture hash, harness source hash AND decoding
    # settings all match. Anything else and the stale file is moved aside and
    # the run starts clean, loudly.

    def enable_checkpoint(self, path, save_every=25):
        """Resume from `path` if it matches this run exactly; else start fresh."""
        self._ckpt_path = path
        self._ckpt_every = save_every
        self._ckpt_pending = 0
        self._ckpt_done = set()
        self.meta["checkpoint"] = {"path": os.path.basename(path),
                                   "resumed": False, "rows_restored": 0,
                                   "temporary": True}
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"  checkpoint unreadable ({exc}); starting fresh", flush=True)
            return
        mine, theirs = self._ckpt_fingerprint(self.meta), saved.get("fingerprint")
        if mine != theirs:
            stale = path + ".stale"
            os.replace(path, stale)
            differing = [k for k in mine
                         if mine.get(k) != (theirs or {}).get(k)]
            print(f"  REFUSING to resume: checkpoint does not match this run "
                  f"(differs on {', '.join(differing) or 'structure'}). "
                  f"Moved to {os.path.basename(stale)}; starting fresh.",
                  flush=True)
            return
        self.rows = saved.get("rows") or []
        self._ckpt_done = {(r["arm"], r["id"]) for r in self.rows}
        self.meta["checkpoint"].update({"resumed": True,
                                        "rows_restored": len(self.rows)})
        print(f"  resumed {len(self.rows)} rows from checkpoint "
              f"({len(self._ckpt_done)} arm/id pairs already done)", flush=True)

    @staticmethod
    def _ckpt_fingerprint(meta):
        """Everything that must be identical for two runs to share an artifact."""
        return {"experiment": meta.get("experiment"),
                "model": meta.get("model"),
                "endpoint": meta.get("endpoint"),
                "gold_sha256": meta.get("gold_sha256"),
                "harness_sha256": (meta.get("git") or {}).get("harness_sha256"),
                "decoding": meta.get("decoding")}

    def done(self, arm, gold_id):
        """True if this arm/id was already scored (in this run or a resumed one)."""
        return (arm, gold_id) in getattr(self, "_ckpt_done", ())

    def _ckpt_save(self, force=False):
        path = getattr(self, "_ckpt_path", None)
        if not path:
            return
        self._ckpt_pending += 1
        if not force and self._ckpt_pending < self._ckpt_every:
            return
        self._ckpt_pending = 0
        payload = {"fingerprint": self._ckpt_fingerprint(self.meta),
                   "rows": self.rows}
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Write-then-rename: a crash mid-write must not leave a half file that
        # the next run would either refuse or, worse, partially trust.
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    # ---- end TEMPORARY -------------------------------------------------

    @staticmethod
    def _answered(rows):
        """-> {arm: rows that produced a prediction}.

        ONE DEFINITION, used by summary() and validate(), because the same
        question asked twice drifts (Rule 15). `n` counts attempts; this counts
        attempts that produced text, and they differ exactly when generation
        failed - the case `n` alone cannot see.

        NOT A FIELD ON THE SUMMARY BUCKET. Adding one there changed the shape
        every reader compares against: `audit_legacy_attribution.inspect_artifact`
        asks whether `record.summary()` still equals the summary stored in the
        file, and an extra key made all 195 legacy artifacts differ at once,
        collapsing supported_measurement, historical_only and provisional into
        exploratory. The guard has to decide the accuracy without changing what
        an artifact looks like.
        """
        out = collections.Counter()
        for row in rows:
            predicted = row.get("predicted")
            if predicted is not None and str(predicted).strip():
                out[row["arm"]] += 1
        return out

    def summary(self):
        answered = self._answered(self.rows)
        arms = {}
        for row in self.rows:
            bucket = arms.setdefault(row["arm"], {"n": 0, "correct": 0,
                                                  "available": 0, "cond": 0})
            bucket["n"] += 1
            bucket["correct"] += row["correct"]
            if row["in_candidates"]:
                bucket["available"] += 1
                bucket["cond"] += row["correct"]
        for arm, bucket in arms.items():
            # NOTHING SCORED IS NOT ZERO PERCENT. `correct / max(n, 1)` turned
            # an arm that measured nothing into a confident 0.0, which reads
            # downstream as a real accuracy - and did: an FP8 pair was reported
            # as "base 0.0 tuned 0.0, nothing was scored" on 2026-09-05 when
            # both arms held 383 rows and 71 differing predictions. A missing
            # measurement and a measured zero must not look the same, so an
            # empty arm gets None and validate() refuses the artifact.
            # NEITHER IS AN ARM THAT ANSWERED NOTHING. The n==0 guard below
            # was written for an empty arm and cannot see a full one that
            # generated no text: two FP8 arms on 2026-09-04 held 383 rows
            # EACH, every `predicted` None and every `raw_response` None, and
            # were written as "accuracy 0.0" - a dead run and a model that got
            # everything wrong are indistinguishable in that number. The guard
            # has to ask whether anything was ANSWERED, not whether any row
            # exists.
            scored = bucket["n"] and answered[arm]
            bucket["accuracy"] = (bucket["correct"] / bucket["n"]
                                  if scored else None)
            bucket["conditional"] = (bucket["cond"] / bucket["available"]
                                     if (scored and bucket["available"])
                                     else None)
        return arms

    def validate(self, contract=None):
        """Return problems that make this artifact untrustworthy.

        ``contract`` optionally states what the run was supposed to produce -
        ``expected_arms``, ``expected_ids``, ``require_clean_tree`` - because a
        run that silently drops an arm or half its lines still validates when
        the summary correctly describes the incomplete rows.

        Shared by every harness, because the same two defects have now appeared
        in three separate scripts: a duplicate (arm, gold_id) counts one
        judgement twice, and a summary that does not follow from the rows means
        the reported number cannot be checked. Relying on each new script to
        get identity and aggregation right has produced drift every time.
        """
        problems = []
        # AN ARM THAT SCORED NOTHING IS A FAILED RUN, NOT A ZERO RESULT.
        # summary() now returns None rather than 0.0 for it, and the artifact
        # must not be written: an unscored arm beside a scored one is a
        # comparison with one side missing, and reads as a catastrophic loss.
        answered = self._answered(self.rows)
        for arm, bucket in sorted(self.summary().items()):
            if not bucket["n"]:
                problems.append(
                    f"{arm}: no rows scored, so it has no accuracy. A run that "
                    f"measured nothing must not be written as a result")
            elif not answered[arm]:
                problems.append(
                    f"{arm}: {bucket['n']} rows but not one prediction - the "
                    f"model produced no output. This is a failed run, not a "
                    f"score of zero, and must not be written as a result")
        if not self.rows:
            problems.append("no rows at all; nothing was measured")
        seen = collections.Counter((row["arm"], row["id"]) for row in self.rows)
        duplicates = sorted(key for key, count in seen.items() if count > 1)
        if duplicates:
            problems.append(
                f"{len(duplicates)} duplicate (arm, id) identities, "
                f"e.g. {duplicates[:3]}")
        # THESE TWO CHECKS USED TO LIVE HERE AND COULD NOT FAIL. They compared
        # self.summary()["n"] against len(rows) and self.summary()["correct"]
        # against a recount of the same rows - but summary() DERIVES both by
        # counting self.rows three lines earlier, so each compared a value to
        # its own source. A trace over all 2,696 tests on 2026-09-04 showed
        # both lines never executed: not because nothing tried, but because no
        # input could reach them.
        #
        # The concern was right and is now checked where it can actually be
        # violated - validate_stored_summary(), against an artifact loaded from
        # disk, where the summary block and the rows are two separate records
        # that a hand edit, a merge, a truncation or an older writer can put
        # out of step.
        contract = contract or {}
        environment = self.meta.get("lmstudio") or {}
        if not environment.get("loaded"):
            problems.append("no LM Studio load state recorded")
        for field in ("context_length", "parallel"):
            if environment.get(field) is None:
                problems.append(f"environment is missing {field}")
        # Deliberately not fatal by default: "optimized" compares the load
        # against an ideal computed from live VRAM at query time, so it moves
        # with whatever else is on the card and read False during a run whose
        # settings were correct. What matters for comparability is the recorded
        # context_length and parallel, which are checked above. A contract may
        # still demand it.
        if contract and contract.get("require_optimized") and \
                environment.get("optimized") is False:
            problems.append("model was loaded with non-ideal settings")
        # A cascade runs two models in one experiment and declares both, as
        # "cheap + expensive". The environment can only ever verify the one
        # currently loaded, so accept a match against any declared component
        # rather than the whole string - the check still catches the case it
        # exists for, which is an artifact naming a model the box was not
        # actually running.
        declared = self.meta.get("model") or ""
        components = {part.strip() for part in declared.split("+") if part.strip()}
        components.add(declared)
        if environment.get("verified_model") not in components | {None}:
            problems.append(
                f"loaded model {environment.get('verified_model')!r} is not the "
                f"declared model {self.meta.get('model')!r}")
        if not self.meta.get("git", {}).get("harness_sha256"):
            problems.append("no harness fingerprint: the code that ran is unidentified")

        arms = set(self.summary())
        expected_arms = contract.get("expected_arms")
        if expected_arms is not None and arms != set(expected_arms):
            problems.append(f"arms {sorted(arms)} != expected {sorted(expected_arms)}")
        # ON BY DEFAULT, and it did not used to be. This check existed for
        # exactly the failure it kept missing: on 2026-09-04 an FP8 diagnostic
        # reported 0/383 for both arms across 766 rows, every prediction None
        # and no error recorded anywhere. Inference never ran - the model
        # could not fetch `kernels-community/finegrained-fp8` while Hugging
        # Face was forced offline, 428 batches failed before a single token,
        # and each was retried four times. The artifact was structurally
        # perfect and validated "ok", so it read as a MODEL RESULT: the
        # adapter answers nothing. That is a claim about a model, made by a
        # missing dependency.
        #
        # It was opt-in, so a run that did not think to ask for it got a clean
        # bill of health. A guard that must be requested does not guard the
        # case nobody anticipated.
        #
        # SCOPED TO ARMS THAT PREDICT AT ALL: an arm whose rows carry no
        # `predicted` key is not a prediction experiment (crossbook
        # normalization compares raw against normalized TEXT) and is left
        # alone. Measured over all 280 artifacts with arm rows: 16 arms flagged,
        # all of them genuinely all-empty, 0 false positives.
        if contract.get("require_any_prediction", True):
            for arm in sorted(arms):
                arm_rows = [r for r in self.rows if r["arm"] == arm]
                if not arm_rows or not any("predicted" in r for r in arm_rows):
                    continue
                if not any(r.get("predicted") for r in arm_rows):
                    problems.append(
                        f"{arm}: every prediction is empty; this can be an "
                        "inference failure, not a measured null result")
                elif len(arm_rows) >= 20:
                    # ALL-IDENTICAL IS THE OTHER SHAPE OF NOTHING, and the
                    # empty check above cannot see it. On 2026-09-04 a
                    # stage1_only arm answered UNKNOWN on all 88 lines of both
                    # arms - every batch accepted, every row carrying a
                    # "prediction", validation "ok", and a diagnostic that
                    # measured nothing. The model had in fact emitted clean
                    # JSON; the serialiser looked for another format and fell
                    # through to a constant.
                    #
                    # A predictor that returns one value regardless of input
                    # carries no information, whether that value is None or a
                    # word. Threshold 20 because a genuinely tiny arm can share
                    # an answer by chance; measured over the artifact store,
                    # 16 of 723 arms trip this and all 16 are the all-empty
                    # ones already caught above - so it adds no false
                    # positives to anything already recorded.
                    distinct = {str(r.get("predicted")) for r in arm_rows}
                    if len(distinct) == 1:
                        problems.append(
                            f"{arm}: every one of {len(arm_rows)} predictions is "
                            f"{distinct.pop()!r}; a constant predictor carries no "
                            "information and is usually a parse failure")
        if contract.get("require_raw_response"):
            for arm in sorted(arms):
                arm_rows = [r for r in self.rows if r["arm"] == arm]
                if arm_rows and not any(
                        r.get("raw_response") not in (None, "None")
                        for r in arm_rows):
                    problems.append(
                        f"{arm}: no raw response was recorded; generation "
                        "success cannot be verified")
        if contract.get("require_accepted_generation"):
            diagnostics = self.meta.get("generation_diagnostics")
            if not isinstance(diagnostics, list):
                problems.append("generation diagnostics were not recorded")
            else:
                for arm in sorted(arms):
                    arm_diagnostics = [row for row in diagnostics
                                       if row.get("arm") == arm]
                    if not arm_diagnostics:
                        problems.append(
                            f"{arm}: no generation diagnostics were recorded")
                    elif not any(row.get("outcome") == "accepted"
                                 for row in arm_diagnostics):
                        problems.append(
                            f"{arm}: every recorded generation batch failed; "
                            "this is not a model measurement")
        expected_ids = contract.get("expected_ids")
        if expected_ids is not None:
            expected_ids = set(expected_ids)
            for arm in sorted(arms):
                got = {r["id"] for r in self.rows if r["arm"] == arm}
                if got != expected_ids:
                    problems.append(
                        f"{arm}: scored {len(got)} ids, expected "
                        f"{len(expected_ids)} (missing {len(expected_ids - got)}, "
                        f"unexpected {len(got - expected_ids)})")
        elif len(arms) > 1:
            # Even without a declared set, every arm must score the same lines
            # or the arms are not comparable.
            per_arm = {arm: {r["id"] for r in self.rows if r["arm"] == arm}
                       for arm in arms}
            reference = per_arm[sorted(arms)[0]]
            for arm, ids in sorted(per_arm.items()):
                if ids != reference:
                    problems.append(f"{arm} scored a different set of ids")
        if contract.get("require_clean_tree") and self.meta.get("git", {}).get("dirty"):
            problems.append("tree had modified tracked files: "
                            f"{self.meta['git'].get('modified_tracked_files')}")
        return problems

    def write(self, path, require_valid=True, contract=None):
        problems = self.validate(contract)
        if problems and require_valid:
            raise EnvironmentCaptureError(
                "refusing to write an unverifiable artifact: " + "; ".join(problems))
        self.meta["validation"] = problems or "ok"
        self.meta["finished"] = time.time()
        self.meta["elapsed_s"] = round(self.meta["finished"] - self.started, 1)
        payload = {"meta": self.meta, "summary": self.summary(), "rows": self.rows}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, ensure_ascii=False)
        ckpt = getattr(self, "_ckpt_path", None)
        if ckpt and os.path.exists(ckpt):
            os.remove(ckpt)
        return path
