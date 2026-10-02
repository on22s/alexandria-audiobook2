"""Install gated retrained adapters over their shipped counterparts.

THIS OVERWRITES SHIPPED VOICES. Every adapter promoted here replaces one a
user may already have assigned to a character in a saved book, so the whole
design is built around being able to put it back: the originals are copied to
a timestamped backup directory BEFORE anything is written, and `--rollback`
restores them from it. Originals remain in their durable backups.

WHAT QUALIFIES. An adapter is promoted only if it passed an independent
identity gate - `verify_adapter_identity.py`, re-measuring held-out ECAPA
against the adapter's own dataset rather than trusting the score the training
run reported about itself - AND beats the shipped adapter it replaces. Both
conditions are re-checked here from the gate artifacts on disk, so this refuses
to promote anything whose evidence is missing, unreadable, or below threshold,
regardless of what the caller passes on the command line.

The weights are copied, not moved: the retrain directories stay intact as the
provenance for what was installed. Interrupted publications leave a durable
journal; --recover restores uncommitted adapter bundles, manifest and receipt
before the HTTP writers admit another mutation. Committed journals resume only
cleanup. All writers coordinate through the shared manifest lock.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "app"))
from adapter_artifacts import AdapterValidationError, validate_adapter_artifacts
from adapter_publication import (
    CLI_PUBLICATION_JOURNAL, is_adapter_naming_recovery_pending,
    is_adapter_checkpoint_recovery_pending, get_adapter_checkpoint_generation_recovery_command,
    get_adapter_bundle_sha256 as _get_bundle_sha256,
    sync_adapter_directory as _sync_directory,
    sync_adapter_tree as _sync_tree,
    save_adapter_publication_bytes as _save_bytes,
)
from utils import atomic_json_write, file_lock
from gate_campaign import get_campaign_gate_evidence, get_campaign_gate_evidence_snapshot
from voice_manifest import get_adapter_id_map_locked, get_resolved_adapter_manifest_rows_locked
MODELS = os.path.join(REPO, "lora_models")
GATES = os.path.join(REPO, "ab_test_runtime", "experiments")
SOURCE = os.path.join(REPO, "ab_test_runtime", "retrain_honest")
DECONTAMINATE_SOURCE = os.path.join(REPO, "ab_test_runtime", "decontaminate")
REFERENCE_RANK1_SOURCE = os.path.join(
    REPO, "ab_test_runtime", "reference_rank1_all21")
REFERENCE_RANK2_SOURCE = os.path.join(
    REPO, "ab_test_runtime", "reference_rank2_failed")
# the goal 2.7 small-set retrain of 2026-09-28: gated on unseen clips, and three
# of its voices beat shipped but were refused as "no retrained adapter on disk"
GOAL27_SMALL_SOURCE = os.path.join(
    REPO, "ab_test_runtime", "goal27_small_20260928")
BACKUPS = os.path.join(REPO, "ab_test_runtime", "promotion_backups")

# ONE TABLE, so a campaign cannot be half-added. The prefix used to live in an
# if/else next to a separate `choices` tuple, and adding reference-rank2 in one
# place but not the other is exactly what happened: the rank-2 gates were
# written, passed, and then invisible to the promoter, which went on reading
# gate_promote__ and reported "no gate artifact" for adapters that had one.
GATE_CAMPAIGNS = {
    "promote": "gate_promote__",
    "reference-rank1": "gate_reference_rank1__",
    "reference-rank2": "gate_reference_rank2__",
    # Both adapters scored on clips NEITHER trained on (build_unseen_holdout +
    # verify_adapter_identity, GOALS 2.7 2026-09-04): the artifact pair is
    # unseen_gate__<name>__clean.json and unseen_gate__<name>__shipped.json,
    # and "beats the shipped adapter" is decided between those two, not
    # against a shipped score measured on the shipped adapter's own training
    # clips - the comparison that blocked fourteen passing retrains.
    "unseen": "unseen_gate__",
}
UNSEEN_PREFIX = GATE_CAMPAIGNS["unseen"]
GATE_PREFIX = GATE_CAMPAIGNS["promote"]


def retrain_sources(gate_prefix=None):
    """Supported retrain directories, restricted by a selected rank campaign.

    A promotion resolves only inside these, so a gate artifact naming some
    other path cannot talk the promoter into installing it. General promotion
    and unseen-clip gates cover all supported retrains; reference-rank gates
    cover only their corresponding rank's retrain directory.

    A FUNCTION, not a tuple, because a module-level tuple snapshots these
    constants at import and then stops tracking them - which silently defeats
    the tests that patch the roots to temporary directories, and quietly makes
    this a SECOND place the source list lives.
    """
    if gate_prefix == GATE_CAMPAIGNS["reference-rank1"]:
        return (REFERENCE_RANK1_SOURCE,)
    if gate_prefix == GATE_CAMPAIGNS["reference-rank2"]:
        return (REFERENCE_RANK2_SOURCE,)
    if gate_prefix is not None and gate_prefix not in GATE_CAMPAIGNS.values():
        return ()
    return (SOURCE, DECONTAMINATE_SOURCE,
            REFERENCE_RANK1_SOURCE, REFERENCE_RANK2_SOURCE, GOAL27_SMALL_SOURCE)


MIN_ECAPA = 0.45

# Only the model itself and its provenance move. Files a promotion must NOT
# carry over are excluded by listing what it copies rather than what it skips:
# a shipped directory can hold sample renders whose filenames encode the old
# training run, and copying those would attach stale provenance to new weights.
PROMOTE_FILES = ("adapter_config.json", "adapter_model.safetensors",
                 "training_meta.json", "README.md", "ref_sample.wav")


def get_adapter_source(name, gate=None):
    """Return a gated adapter only inside the selected campaign's sources."""
    roots = [os.path.realpath(root) for root in retrain_sources(GATE_PREFIX)]

    def get_permitted_path(path):
        candidate = os.path.realpath(path)
        if (os.path.isdir(candidate)
                and any(os.path.commonpath((candidate, root)) == root for root in roots)):
            return candidate
        return None

    gate = gate_result(name) if gate is None else gate
    gated_path = gate.get("adapter") if gate else None
    if gated_path:
        return get_permitted_path(gated_path if os.path.isabs(gated_path)
                                  else os.path.join(REPO, gated_path))
    for root in roots:
        ranked = get_permitted_path(os.path.join(root, name, "adapter"))
        if ranked:
            return ranked
    if os.path.realpath(DECONTAMINATE_SOURCE) not in roots:
        return None
    import glob
    matches = sorted(glob.glob(os.path.join(
        DECONTAMINATE_SOURCE, "batch*", name, "adapter")))
    return get_permitted_path(matches[0]) if len(matches) == 1 else None


def get_publication_adapter_names_locked(names):
    """Resolve destinations, retaining original gate/backup names; caller locks root."""
    aliases = get_adapter_id_map_locked(MODELS)
    mapping = {}
    for name in names:
        _validate_component(name)
        mapping[name] = aliases.get(os.path.normcase(name), name)
    if len(set(mapping.values())) != len(names):
        raise ValueError("duplicate publication adapters")
    return mapping


def shipped_scores():
    """Adapter -> best recorded score for the weights currently shipped."""
    path = os.path.join(GATES, "library_voice_fidelity_n10.json")
    try:
        with open(path, encoding="utf-8") as handle:
            scores = {r["adapter"]: r.get("ecapa")
                      for r in json.load(handle)["results"]}
        manifest = os.path.join(MODELS, "manifest.json")
        if os.path.exists(manifest):
            with open(manifest, encoding="utf-8") as handle:
                for entry in json.load(handle):
                    if entry.get("gate_ecapa") is not None:
                        aliases = get_adapter_id_map_locked(MODELS)
                        current = aliases.get(os.path.normcase(entry["id"]), entry["id"])
                        scores[current] = entry["gate_ecapa"]
                        for old, target in aliases.items():
                            if os.path.normcase(target) == os.path.normcase(current):
                                scores[old] = entry["gate_ecapa"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"Cannot read shipped-score evidence: {exc}", file=sys.stderr)
        return None
    return scores


def get_gate_evidence(path):
    """Read a gate object, refusing unreadable or malformed evidence."""
    if not os.path.exists(path):
        return None
    try:
        return get_campaign_gate_evidence(path)
    except (OSError, ValueError) as exc:
        print(f"Cannot read gate evidence {path}: {exc}", file=sys.stderr)
        return None


def get_gate_result_path(name):
    suffix = "__clean" if GATE_PREFIX == UNSEEN_PREFIX else ""
    return os.path.join(GATES, f"{GATE_PREFIX}{name}{suffix}.json")


def gate_result(name):
    """The gate's verdict for one adapter, or None if it was never gated."""
    return get_gate_evidence(get_gate_result_path(name))


def shipped_unseen_score(name):
    """The shipped adapter's median on the same unseen clips, or None."""
    path = os.path.join(GATES, f"{UNSEEN_PREFIX}{name}__shipped.json")
    gate = get_gate_evidence(path)
    return get_shipped_unseen_score(gate)


def get_shipped_unseen_score(gate):
    if gate is None:
        return None
    if gate.get("generation_failures"):
        return None
    return gate.get("median_ecapa", gate.get("ecapa"))


def is_finite_score(score):
    """Accept JSON numbers usable in score comparisons, excluding booleans."""
    if type(score) not in (int, float):
        return False
    try:
        return math.isfinite(score)
    except OverflowError:
        return False


def check(name, before, destination=None, gate=None, shipped_gate=None):
    """-> (ok, score, reason). Refuses on missing or insufficient evidence."""
    gate = gate_result(name) if gate is None else gate
    if gate is None:
        return False, None, "no gate artifact - run verify_adapter_identity"
    score = gate.get("median_ecapa", gate.get("ecapa"))
    if score is None:
        return False, None, "gate artifact carries no score"
    if not is_finite_score(score):
        return False, None, "gate score must be a finite real number"
    # THE GATE'S VERDICT, NOT A RE-DERIVED ONE. The docstring above promises an
    # adapter is promoted only if it passed the identity gate, but this used to
    # re-decide that from MIN_ECAPA alone and never read `passed`. The gate
    # computes `passed` against its own --min-ecapa, so a gate run at a
    # stricter threshold could report FAIL and be promoted anyway. Every gate
    # on disk today happens to have run at the default 0.45, so this refuses
    # nothing that was previously accepted - it closes the gap before a
    # non-default threshold opens it.
    if gate.get("passed") is not True:
        verdict = "FAIL" if gate.get("passed") is False else "missing"
        return False, score, f"gate verdict is {verdict} at its own threshold " \
                             f"{gate.get('threshold', '?')}"
    # An identity check that could not generate every line measured a median
    # over the survivors. That is not the held-out evidence promotion claims.
    failures = gate.get("generation_failures") or 0
    if failures:
        return False, score, f"gate had {failures} generation failure(s)"
    if score < MIN_ECAPA:
        return False, score, f"gate score {score:.3f} below {MIN_ECAPA}"
    if GATE_PREFIX == UNSEEN_PREFIX:
        old = shipped_unseen_score(name) if shipped_gate is None else get_shipped_unseen_score(shipped_gate)
        if old is None:
            return False, score, "no shipped arm on the same unseen clips"
    else:
        old = before.get(name)
        if old is None:
            return False, score, "no shipped score to compare against"
    if not is_finite_score(old):
        return False, score, "shipped score must be a finite real number"
    if score <= old:
        if GATE_PREFIX == UNSEEN_PREFIX:
            return False, score, (f"clean {score:.3f} does not beat shipped "
                                  f"{old:.3f} on unseen clips")
        return False, score, f"gate {score:.3f} does not beat shipped {old:.3f}"
    source = get_adapter_source(name, gate=gate)
    if source is None:
        return False, score, "no retrained adapter on disk"
    try:
        validate_adapter_artifacts(source)
    except AdapterValidationError as error:
        return False, score, str(error)
    destination = destination or get_publication_adapter_names_locked([name])[name]
    if not os.path.isdir(os.path.join(MODELS, destination)):
        return False, score, "no shipped adapter to replace"
    return True, score, f"{old:.3f} -> {score:.3f}"


def backup_dir(stamp):
    return os.path.join(BACKUPS, stamp)


def _validate_component(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or os.path.basename(value) != value or "/" in value or "\\" in value):
        raise ValueError(f"invalid publication path component: {value!r}")


def _recover_publication_locked():
    """Undo an uncommitted transaction, or clean a durably committed one.

    Caller holds the manifest lock. Original directories are renamed back,
    never reconstructed file by file; interrupted recovery is itself repeatable.
    """
    path = os.path.join(MODELS, CLI_PUBLICATION_JOURNAL)
    if not os.path.lexists(path):
        return False
    with open(path, encoding="utf-8") as handle:
        journal = json.load(handle)
    if (not isinstance(journal, dict) or journal.get("version") != 1
            or journal.get("operation") not in ("promotion", "rollback")
            or journal.get("status") not in ("pending", "committed", "recovered")
            or journal.get("backup_root") != os.path.realpath(BACKUPS)):
        raise ValueError("invalid publication recovery journal")
    for field in ("workspace", "stamp"):
        _validate_component(journal.get(field))
    if not journal["workspace"].startswith(".promotion-"):
        raise ValueError("invalid publication recovery workspace")
    names = journal.get("names")
    if not isinstance(names, list) or len(set(names)) != len(names):
        raise ValueError("invalid publication recovery names")
    for name in names:
        _validate_component(name)
    workspace = os.path.join(MODELS, journal["workspace"])
    if os.path.islink(workspace):
        raise ValueError("invalid publication recovery workspace")
    if journal["status"] == "pending":
        if not os.path.isdir(workspace) or not isinstance(journal.get("manifest_present"), bool):
            raise ValueError("invalid publication recovery snapshot")
        for subdir in ("originals", "failed", "staged"):
            directory = os.path.join(workspace, subdir)
            if not os.path.isdir(directory) or os.path.islink(directory):
                raise ValueError("invalid publication recovery directory")
        manifest = os.path.join(MODELS, "manifest.json")
        snapshot = os.path.join(workspace, "manifest.before")
        if os.path.islink(snapshot):
            raise ValueError("invalid publication manifest snapshot")
        with open(snapshot, "rb") as handle:
            original = handle.read()
        if hashlib.sha256(original).hexdigest() != journal.get("manifest_sha256"):
            raise ValueError("publication manifest snapshot is corrupt")
        hashes = journal.get("original_bundle_sha256")
        if not isinstance(hashes, dict) or set(hashes) != set(names):
            raise ValueError("invalid publication recovery bundle hashes")
        for name in names:
            target = os.path.join(MODELS, name)
            old = os.path.join(workspace, "originals", name)
            if (os.path.islink(old) or os.path.islink(target)
                    or not (os.path.isdir(old) or os.path.isdir(target))):
                raise ValueError(f"invalid publication recovery original: {name}")
            original_path = old if os.path.isdir(old) else target
            if _get_bundle_sha256(original_path) != hashes[name]:
                raise ValueError(f"publication recovery original is corrupt or missing: {name}")
        for name in reversed(names):
            target = os.path.join(MODELS, name)
            old = os.path.join(workspace, "originals", name)
            if os.path.isdir(old):
                if os.path.lexists(target):
                    failed = os.path.join(workspace, "failed", name)
                    if os.path.exists(failed):
                        shutil.rmtree(failed)
                    os.replace(target, failed)
                    _sync_directory(MODELS)
                    _sync_directory(os.path.dirname(failed))
                os.replace(old, target)
                _sync_directory(os.path.dirname(old))
                _sync_directory(MODELS)
            elif not os.path.isdir(target):
                raise ValueError(f"publication recovery original is missing: {name}")
        if journal.get("manifest_present") is True:
            _save_bytes(manifest, original)
        elif journal.get("manifest_present") is False:
            if os.path.lexists(manifest):
                os.remove(manifest)
                _sync_directory(MODELS)
        else:
            raise ValueError("publication journal has invalid manifest presence")
        if journal["operation"] == "promotion":
            receipt = os.path.join(BACKUPS, journal["stamp"] + ".json")
            if os.path.lexists(receipt):
                os.remove(receipt)
                _sync_directory(BACKUPS)
        journal["status"] = "recovered"
        atomic_json_write(journal, path)
    # Terminal state is durable before deleting scratch evidence. Cleanup may
    # itself be interrupted; a terminal journal needs no deleted snapshots.
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    _sync_directory(MODELS)
    os.remove(path)
    _sync_directory(MODELS)
    print(f"publication {journal['operation']} {journal['stamp']}: {journal['status']}")
    return True


def get_publication_file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _apply_publication(names, stamp, operation, publish_metadata, sources=None,
                       receipt_record=None, source_names=None):
    _validate_component(stamp)
    for name in names:
        _validate_component(name)
    if len(set(names)) != len(names):
        raise ValueError("duplicate publication adapters")
    path = os.path.join(MODELS, CLI_PUBLICATION_JOURNAL)
    if os.path.lexists(path):
        raise ValueError("publication recovery is required first")
    receipt_rows = {}
    workspace = tempfile.mkdtemp(prefix=".promotion-", dir=MODELS)
    try:
        staged = os.path.join(workspace, "staged")
        os.mkdir(staged)
        os.mkdir(os.path.join(workspace, "originals"))
        os.mkdir(os.path.join(workspace, "failed"))
        try:
            for name in names:
                live = os.path.join(MODELS, name)
                if not os.path.isdir(live) or os.path.islink(live):
                    raise ValueError(f"invalid publication adapter: {name}")
                if is_adapter_checkpoint_recovery_pending(live):
                    raise ValueError(f"checkpoint recovery is required first: {name}")
                target = os.path.join(staged, name)
                if operation == "rollback":
                    if is_adapter_checkpoint_recovery_pending(sources[name]):
                        raise ValueError(f"rollback backup contains a pending checkpoint: {name}")
                    shutil.copytree(sources[name], target, symlinks=True)
                else:
                    # Preserve non-owned assets and candidate/rollback history.
                    shutil.copytree(live, target, symlinks=True)
                    evidence_name = (source_names or {}).get(name, name)
                    source = sources[name] if sources is not None else get_adapter_source(evidence_name)
                    if source is None or os.path.realpath(source) != source:
                        raise AdapterValidationError(f"Adapter source disappeared or changed: {name}")
                    owned = os.path.join(workspace, "source-" + name)
                    os.mkdir(owned)
                    for filename in PROMOTE_FILES:
                        if os.path.exists(os.path.join(source, filename)):
                            shutil.copy2(os.path.join(source, filename), os.path.join(owned, filename))
                    identity = os.path.join(source, "identity_check")
                    if os.path.isdir(identity):
                        shutil.copytree(identity, os.path.join(owned, "identity_check"))
                    validate_adapter_artifacts(owned)
                    source_hashes = {filename: get_publication_file_sha256(os.path.join(owned, filename))
                                     for filename in PROMOTE_FILES if os.path.isfile(os.path.join(owned, filename))}
                    for filename in PROMOTE_FILES:
                        if os.path.exists(os.path.join(owned, filename)):
                            os.replace(os.path.join(owned, filename), os.path.join(target, filename))
                    if os.path.isdir(os.path.join(owned, "identity_check")):
                        previous = os.path.join(target, "identity_check")
                        if os.path.islink(previous):
                            os.remove(previous)
                        elif os.path.exists(previous):
                            shutil.rmtree(previous)
                        os.replace(os.path.join(owned, "identity_check"), previous)
                    validate_adapter_artifacts(target)
                    receipt_rows[name] = {"source": os.path.realpath(source),
                                          "source_file_sha256": source_hashes,
                                          "installed_file_sha256": {
                                              filename: get_publication_file_sha256(os.path.join(target, filename))
                                              for filename in PROMOTE_FILES if os.path.isfile(os.path.join(target, filename))},
                                          "installed_bundle_sha256": _get_bundle_sha256(target)}
        except OSError as error:
            if operation == "promotion":
                raise AdapterValidationError(f"Unable to stage adapter bundle: {error}") from error
            raise
        manifest = os.path.join(MODELS, "manifest.json")
        manifest_present = os.path.exists(manifest)
        original = b""
        if manifest_present:
            with open(manifest, "rb") as handle:
                original = handle.read()
        _save_bytes(os.path.join(workspace, "manifest.before"), original)
        if operation == "promotion":
            os.makedirs(BACKUPS, exist_ok=True)
            receipt = os.path.join(BACKUPS, stamp + ".json")
            if os.path.lexists(receipt):
                raise FileExistsError(f"promotion receipt already exists: {receipt}")
            dest = backup_dir(stamp)
            os.mkdir(dest)  # A stamp is never silently reused or overwritten.
            incomplete = os.path.join(dest, ".incomplete")
            _save_bytes(incomplete, b"backup copy is not complete")
            _sync_directory(BACKUPS)
            for name in names:
                shutil.copytree(os.path.join(MODELS, name), os.path.join(dest, name), symlinks=True)
            _sync_tree(dest)
            os.remove(incomplete)
            _sync_directory(dest)
            _sync_directory(BACKUPS)
        _sync_tree(workspace)
        _sync_directory(MODELS)
        original_hashes = {name: _get_bundle_sha256(os.path.join(MODELS, name)) for name in names}
        journal = {"version": 1, "operation": operation, "status": "pending",
                   "stamp": stamp, "workspace": os.path.basename(workspace),
                   "backup_root": os.path.realpath(BACKUPS), "names": names,
                   "manifest_present": manifest_present,
                   "original_bundle_sha256": original_hashes,
                   "manifest_sha256": hashlib.sha256(original).hexdigest()}
        atomic_json_write(journal, path)
        try:
            for name in names:
                target = os.path.join(MODELS, name)
                old = os.path.join(workspace, "originals", name)
                os.replace(target, old)
                _sync_directory(MODELS)
                _sync_directory(os.path.dirname(old))
                os.replace(os.path.join(staged, name), target)
                _sync_directory(staged)
                _sync_directory(MODELS)
            publish_metadata({name: os.path.join(MODELS, name) for name in names})
            if receipt_record is not None:
                receipt = dict(receipt_record,
                               manifest_before_sha256=hashlib.sha256(original).hexdigest(),
                               adapters=[dict(row, **receipt_rows[row["adapter"]])
                                         for row in receipt_record["adapters"]])
                atomic_json_write(receipt, os.path.join(BACKUPS, stamp + ".json"))
            journal["status"] = "committed"
            atomic_json_write(journal, path)
        except BaseException:
            _recover_publication_locked()
            raise
        _recover_publication_locked()
    finally:
        # A failed recovery owns its evidence until a later --recover succeeds.
        if not os.path.lexists(path) and os.path.isdir(workspace):
            shutil.rmtree(workspace)


def promote(names, stamp, dry_run):
    with file_lock(os.path.join(MODELS, "manifest.json")):
        if dry_run and os.path.lexists(os.path.join(MODELS, CLI_PUBLICATION_JOURNAL)):
            raise ValueError("publication recovery is required: run --recover first")
        checkpoint_recovery = get_adapter_checkpoint_generation_recovery_command(MODELS)
        if checkpoint_recovery:
            raise ValueError('checkpoint recovery is required: run ' + checkpoint_recovery)
        if is_adapter_naming_recovery_pending(MODELS):
            raise ValueError("naming recovery is required: run name_voices.py --recover")
        _recover_publication_locked()
        before = shipped_scores()
        if before is None:
            print("REFUSE promotion: unreadable shipped-score evidence", file=sys.stderr)
            return 1
        destinations = get_publication_adapter_names_locked(names)
        plan, refused = [], []
        gate_records, source_paths, shipped_gates = {}, {}, {}
        for name in names:
            _validate_component(name)
            try:
                gate_path = get_gate_result_path(name)
                gate, digest = get_campaign_gate_evidence_snapshot(gate_path)
                gate_records[name] = {"path": os.path.realpath(gate_path), "sha256": digest}
                shipped_gate = None
                if GATE_PREFIX == UNSEEN_PREFIX:
                    shipped_path = os.path.join(GATES, f"{UNSEEN_PREFIX}{name}__shipped.json")
                    shipped_gate, shipped_digest = get_campaign_gate_evidence_snapshot(shipped_path)
                    shipped_gates[name] = {"path": os.path.realpath(shipped_path), "sha256": shipped_digest,
                                           "ecapa": get_shipped_unseen_score(shipped_gate)}
                ok, score, reason = check(name, before, destination=destinations[name],
                                          gate=gate, shipped_gate=shipped_gate)
                if ok:
                    source_paths[destinations[name]] = get_adapter_source(name, gate=gate)
            except (OSError, ValueError) as error:
                print(f"Cannot read gate evidence: {error}", file=sys.stderr)
                ok, score, reason = False, None, f"unreadable gate provenance: {error}"
            (plan if ok else refused).append((name, score, reason))
        for name, _score, reason in refused:
            print(f"  REFUSE {name[:34]:36} {reason}")
        for name, _score, reason in plan:
            print(f"  ready  {name[:34]:36} {reason}")
        if not plan:
            print("\nnothing to promote")
            return 1
        if dry_run:
            print(f"\ndry run - {len(plan)} would be promoted, no adapters changed")
            return 0
        record = {"promoted_at": stamp, "backup": backup_dir(stamp), "min_ecapa": MIN_ECAPA,
                  "gate_campaign": next((campaign for campaign, prefix in GATE_CAMPAIGNS.items()
                                         if prefix == GATE_PREFIX), GATE_PREFIX),
                  "gate_prefix": GATE_PREFIX,
                  "adapters": [{"adapter": destinations[n], "gate_ecapa": score,
                                "shipped_ecapa": before.get(n),
                                "comparison_shipped_ecapa": shipped_gates[n]["ecapa"] if n in shipped_gates else before.get(n),
                                "gate_artifact": gate_records[n],
                                **({"shipped_gate_artifact": shipped_gates[n]} if n in shipped_gates else {}),
                                **({"evidence_adapter": n} if destinations[n] != n else {})}
                               for n, score, _reason in plan]}
        try:
            _apply_publication([destinations[n] for n, _s, _r in plan], stamp, "promotion",
                               lambda staged: update_manifest({destinations[n]: score for n, score, _r in plan},
                                                              stamp, source_paths=staged),
                               receipt_record=record, sources=source_paths,
                               source_names={destinations[n]: n for n, _s, _r in plan})
        except AdapterValidationError as error:
            print(f"REFUSE promotion: unable to stage valid adapter: {error}")
            return 1
        print(f"\npromoted {len(plan)}; receipt {os.path.join(BACKUPS, stamp + '.json')}")
        print(f"rollback: python promote_adapters.py --rollback {stamp}")
        return 0


def update_manifest(promoted, stamp, source_paths=None):
    """Record on each entry that its weights were replaced, and by what."""
    path = os.path.join(MODELS, "manifest.json")
    with open(path, encoding="utf-8") as handle:
        entries = get_resolved_adapter_manifest_rows_locked(MODELS, json.load(handle))
    for entry in entries:
        score = promoted.get(entry.get("id"))
        if score is None:
            continue
        source = (source_paths.get(entry["id"]) if source_paths is not None
                  else get_adapter_source(entry["id"]))
        meta = os.path.join(source, "training_meta.json") if source else ""
        if os.path.exists(meta):
            with open(meta, encoding="utf-8") as handle:
                fresh = json.load(handle)
            for field in ("epochs_run", "epoch_losses", "final_loss",
                          "best_loss", "sample_count", "lora_r", "lr"):
                if field in fresh:
                    entry[field] = fresh[field]
            if "num_samples" in fresh:
                entry["sample_count"] = fresh["num_samples"]
        entry["retrained_at"] = stamp
        entry["gate_ecapa"] = score
    atomic_json_write(entries, path)
    print(f"  manifest updated for {len(promoted)} entries")


def revert_manifest(names, stamp, backup_names=None):
    """Undo what update_manifest recorded, from the receipt and the backups.

    WHY THIS IS NOT OPTIONAL. `shipped_scores` PREFERS manifest `gate_ecapa`
    over the fidelity file, and `check` refuses to promote anything that does
    not beat the shipped score. Leaving the retrained score in the manifest
    after the weights were restored means the next promotion compares against
    a number the shipped weights do not have - a phantom baseline, set higher
    than reality, so a genuine improvement gets refused. The old code printed
    a NOTE about this and left it to the reader.

    The receipt records each adapter's pre-promotion `shipped_ecapa`, and the
    backup holds its original training_meta.json, so both halves of what
    update_manifest overwrote are recoverable.
    """
    path = os.path.join(MODELS, "manifest.json")
    if not os.path.exists(path):
        return 0
    receipt_path = os.path.join(BACKUPS, f"{stamp}.json")
    previous = {}
    if os.path.exists(receipt_path):
        try:
            with open(receipt_path, encoding="utf-8") as handle:
                previous = {row["adapter"]: row.get("shipped_ecapa")
                            for row in json.load(handle).get("adapters", [])}
        except (OSError, ValueError, KeyError, TypeError):
            previous = {}
    with open(path, encoding="utf-8") as handle:
        entries = get_resolved_adapter_manifest_rows_locked(MODELS, json.load(handle))
    reverted = 0
    for entry in entries:
        name = entry.get("id")
        if name not in names:
            continue
        backup_name = (backup_names or {}).get(name, name)
        meta = os.path.join(backup_dir(stamp), backup_name, "training_meta.json")
        if os.path.exists(meta):
            original_loaded = True
            try:
                with open(meta, encoding="utf-8") as handle:
                    original = json.load(handle)
            except (OSError, ValueError):
                original = {}
                original_loaded = False
            for field in ("epochs_run", "epoch_losses", "final_loss",
                          "best_loss", "sample_count", "lora_r", "lr"):
                if field in original:
                    entry[field] = original[field]
                elif original_loaded:
                    entry.pop(field, None)
            if "num_samples" in original:
                entry["sample_count"] = original["num_samples"]
        entry.pop("retrained_at", None)
        if previous.get(backup_name) is not None:
            entry["gate_ecapa"] = previous[backup_name]
        else:
            # Either the receipt is gone, or the adapter had no shipped score
            # before promotion. Removing the key is the honest option: it falls
            # back to the measured fidelity file rather than leaving a score
            # that belongs to weights no longer installed.
            entry.pop("gate_ecapa", None)
        reverted += 1
    atomic_json_write(entries, path)
    return reverted


def rollback(stamp):
    with file_lock(os.path.join(MODELS, "manifest.json")):
        checkpoint_recovery = get_adapter_checkpoint_generation_recovery_command(MODELS)
        if checkpoint_recovery:
            raise ValueError('checkpoint recovery is required: run ' + checkpoint_recovery)
        if is_adapter_naming_recovery_pending(MODELS):
            raise ValueError("naming recovery is required: run name_voices.py --recover")
        _recover_publication_locked()
        _validate_component(stamp)
        dest = backup_dir(stamp)
        if not os.path.isdir(dest) or os.path.islink(dest):
            print(f"no backup at {dest}")
            return 1
        names = sorted(os.listdir(dest))
        destinations = get_publication_adapter_names_locked(names)
        for name in names:
            _validate_component(name)
            source, target = os.path.join(dest, name), os.path.join(MODELS, destinations[name])
            if (not os.path.isdir(source) or os.path.islink(source)
                    or not os.path.isdir(target) or os.path.islink(target)):
                raise ValueError(f"invalid rollback adapter: {name}")
        _apply_publication(list(destinations.values()), stamp, "rollback",
                           lambda _staging: revert_manifest(set(destinations.values()), stamp,
                               backup_names={current: old for old, current in destinations.items()}),
                           sources={destinations[name]: os.path.join(dest, name) for name in names})
        print(f"restored {len(names)} adapters from {stamp}")
        return 0


def recover_publication():
    with file_lock(os.path.join(MODELS, "manifest.json")):
        checkpoint_recovery = get_adapter_checkpoint_generation_recovery_command(MODELS)
        if checkpoint_recovery:
            raise ValueError('checkpoint recovery is required: run ' + checkpoint_recovery)
        if is_adapter_naming_recovery_pending(MODELS):
            raise ValueError("naming recovery is required: run name_voices.py --recover")
        return _recover_publication_locked()


def main():
    global GATE_PREFIX
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--adapters", nargs="*", default=None,
                    help="default: every adapter with a passing gate artifact")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", metavar="STAMP")
    ap.add_argument("--recover", action="store_true", help="recover an interrupted publication without promoting")
    ap.add_argument("--gate-campaign", choices=tuple(GATE_CAMPAIGNS),
                    default="promote",
                    help="select the independently generated gate campaign")
    args = ap.parse_args()

    if args.recover:
        recover_publication()
        return 0

    if args.rollback:
        return rollback(args.rollback)

    GATE_PREFIX = GATE_CAMPAIGNS[args.gate_campaign]
    names = args.adapters
    if not names:
        import glob
        names = sorted(os.path.basename(p)[len(GATE_PREFIX):-len(".json")]
                       for p in glob.glob(os.path.join(
                           GATES, f"{GATE_PREFIX}*.json")))
        if GATE_PREFIX == UNSEEN_PREFIX:
            names = sorted(n[:-len("__clean")] for n in names if n.endswith("__clean"))
    if not names:
        print("no gate artifacts found")
        return 1
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return promote(names, stamp, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
