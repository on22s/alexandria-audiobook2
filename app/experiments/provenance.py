"""Make a TTS artifact say how it was produced.

THE ARTIFACT THAT COULD NOT IDENTIFY ITSELF. On 2026-08-04 the work log quoted
`instruct_value.json` as the seeded run. It was the unseeded one. The seeded
result was in `instruct_value_seeded.json`, written three hours later, and
NOTHING INSIDE EITHER FILE DISTINGUISHED THEM:

    instruct_value.json          rows=18  arms=[per_line, per_char, none]
    instruct_value_seeded.json   rows=18  arms=[per_line, per_char, none]

Same shape, same arms, same row count, and neither recorded a seed - even
though `instruct_value.py` takes `--seed` and its own docstring is about the
seed bug. The only thing separating them was a filename suffix and an mtime: a
naming convention and a filesystem accident, neither of which is data.

An external reviewer caught it. Building a second index over those files was
considered and rejected, because an index can only show what an artifact
recorded - it would have printed `seed=-` on both rows and laundered the
ambiguity into a table.

WHY THIS IS NOT `ExperimentRecord`. `manifest.py` already does this properly
for the attribution experiments, and Rule 15 says one answer per question. But
`ExperimentRecord` requires a `gold_path` it can hash and queries LM Studio for
the model environment, and a TTS run has neither - there is no gold file and no
LLM endpoint. So the GIT capture is imported from `manifest` rather than
rewritten (that is the part that would drift), and the rest is what a
generation run actually has: seed, script, arguments.

USE IT LIKE THIS, at the point of writing the artifact:

    from experiments.provenance import provenance
    json.dump({"summary": summary, "rows": rows,
               "provenance": provenance(__file__, args)}, fh, indent=1)

`seed` is pulled from args automatically when present, because the seed is the
single field whose absence caused this.
"""
import base64
import hashlib
import functools
import io
import os
import platform
import re
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_CALLER_DEFAULT = object()


def _git_object_id(kind, content):
    return hashlib.sha1(f"{kind} {len(content)}\0".encode("ascii") + content).hexdigest()


def _commit_tree(content):
    match = re.fullmatch(rb"tree ([0-9a-f]{40})", content.split(b"\n", 1)[0])
    if match is None:
        raise ValueError("commit proof has no root tree")
    return match.group(1).decode("ascii")


def _tree_directory(content, name):
    offset, found = 0, None
    while offset < len(content):
        separator = content.find(b"\0", offset)
        end = separator + 21
        if separator < 0 or end > len(content):
            raise ValueError("invalid tree proof framing")
        header = content[offset:separator].split(b" ", 1)
        if len(header) != 2:
            raise ValueError("invalid tree proof entry")
        mode, entry = header
        if entry == name.encode("utf-8"):
            if mode != b"40000" or found is not None:
                raise ValueError("harness proof path is not a unique directory")
            found = content[separator + 1:end].hex()
        offset = end
    if found is None:
        raise ValueError("harness proof path is missing")
    return found


def _harness_path_parts(harness_path):
    parts = harness_path.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("invalid harness proof path")
    return parts


def get_harness_commit_proof(repo, commit, harness_path="app/experiments"):
    """Retain the commit and tree-path objects a squash merge may discard."""
    def read(kind, object_id):
        return subprocess.run(["git", "cat-file", kind, object_id], cwd=repo,
                              capture_output=True, check=True, timeout=10).stdout
    content = read("commit", commit)
    object_id = _commit_tree(content)
    trees = []
    for part in _harness_path_parts(harness_path):
        tree = read("tree", object_id)
        trees.append(base64.b64encode(tree).decode("ascii"))
        object_id = _tree_directory(tree, part)
    return {"scheme": "git-commit-tree-v1",
            "commit": base64.b64encode(content).decode("ascii"), "trees": trees}


def _get_proven_harness_tree(proof, commit, harness_path):
    if not isinstance(proof, dict) or proof.get("scheme") != "git-commit-tree-v1":
        raise ValueError("unknown harness commit proof")
    parts = _harness_path_parts(harness_path)
    trees = proof.get("trees")
    if not isinstance(trees, list) or len(trees) != len(parts):
        raise ValueError("incomplete harness commit proof")
    def decode(value):
        if not isinstance(value, str):
            raise ValueError("invalid encoded Git object")
        return base64.b64decode(value, validate=True)
    content = decode(proof.get("commit"))
    if _git_object_id("commit", content) != commit:
        raise ValueError("harness proof does not identify the recorded commit")
    object_id = _commit_tree(content)
    for part, encoded in zip(parts, trees):
        content = decode(encoded)
        if _git_object_id("tree", content) != object_id:
            raise ValueError("harness tree proof does not match its parent")
        object_id = _tree_directory(content, part)
    return object_id


def _get_squash_harness_source(repo, harness_path, tree, target, scheme):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          check=True, text=True, timeout=10).stdout.strip()
    history = subprocess.run(["git", "rev-list", head, "--", harness_path], cwd=repo,
                             capture_output=True, check=True, text=True, timeout=10).stdout.splitlines()
    for commit in dict.fromkeys([head] + history):
        candidate = subprocess.run(["git", "rev-parse", "--verify", f"{commit}:{harness_path}"],
                                   cwd=repo, capture_output=True, text=True, timeout=10)
        if candidate.returncode == 0 and candidate.stdout.strip() == tree:
            if get_harness_sha256_at_commit(repo, commit, harness_path, scheme) == target:
                return commit
    return None


def file_sha256(path):
    """Return the content identity used by experiment provenance."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def input_sha256(paths):
    """Return repo-relative content identities, failing on missing inputs."""
    return {os.path.relpath(path, REPO): file_sha256(path) for path in paths}


@functools.lru_cache(maxsize=256)
def get_harness_sha256_at_commit(repo, commit, harness_path="app/experiments",
                                 scheme=_CALLER_DEFAULT):
    """Reconstruct the harness identity from committed blobs in two Git calls.

    Legacy records retain their exact raw-byte rule when their scheme marker
    is absent. Never reinterpret a legacy fingerprint as a canonical one.
    """
    from experiments.manifest import HARNESS_DIGEST_SCHEME, _source_identity_bytes
    if scheme is _CALLER_DEFAULT:
        scheme = HARNESS_DIGEST_SCHEME
    listing = subprocess.run(
        ["git", "ls-tree", "-rz", commit, "--", harness_path],
        cwd=repo, capture_output=True, check=True, timeout=10).stdout
    prefix = harness_path.rstrip("/") + "/"
    entries = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        unused_mode, object_type, object_id = metadata.split()
        path = raw_path.decode("utf-8")
        if (path.startswith(prefix) and "/" not in path[len(prefix):]
                and path.endswith(".py")):
            if object_type != b"blob":
                raise ValueError(f"harness entry is not a blob: {path}")
            entries.append((path, object_id))
    entries.sort()
    if not entries:
        raise ValueError(f"no Python harness files at {commit}:{harness_path}")
    result = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=repo,
        input=b"".join(object_id + b"\n" for path, object_id in entries),
        capture_output=True, check=True, timeout=10)
    stream = io.BytesIO(result.stdout)
    digest = hashlib.sha256()
    for path, object_id in entries:
        header = stream.readline().rstrip(b"\n").split()
        if (len(header) != 3 or header[0] != object_id
                or header[1] != b"blob" or not header[2].isdigit()):
            raise ValueError(f"invalid Git blob response for {path}")
        size = int(header[2])
        if size > len(result.stdout) - stream.tell() - 1:
            raise ValueError(f"incomplete Git blob response for {path}")
        content = stream.read(size)
        if stream.read(1) != b"\n":
            raise ValueError(f"incomplete Git blob response for {path}")
        digest.update(os.path.basename(path).encode("utf-8"))
        digest.update(_source_identity_bytes(content, scheme))
    if stream.read(1):
        raise ValueError("unexpected trailing Git blob response")
    return digest.hexdigest()


def _get_clean_source_inconsistency(git):
    """Missing legacy diagnostics are allowed; contradictory ones are not."""
    if git.get("dirty") is not False:
        return None
    if "source_state" in git and git["source_state"] != "clean":
        return "clean claim contradicts recorded source_state"
    for field in ("modified_tracked_files", "untracked_harness_files"):
        if field in git and git[field] not in (None, []):
            return f"clean claim contradicts recorded {field}"
    if git.get("source_state_detail"):
        return "clean claim contradicts recorded source_state_detail"
    return None


def get_reproducible_harness_source(
        provenance_block, repo=REPO, harness_path="app/experiments"):
    """Return the exact commit (or WORKTREE) reproducing a recorded harness.

    A dirty run often records the parent commit and is committed immediately
    afterward. Only descendants of that recorded commit are eligible: finding
    the same bytes on an unrelated branch would not establish the run's
    history. Clean runs must match their recorded commit. A commit/tree proof
    can bind a squash-preserved source tree to that same recorded commit even
    when its PR-only object is gone. A later working tree cannot rescue them.
    """
    git = provenance_block.get("git") if isinstance(provenance_block, dict) \
        else None
    if not isinstance(git, dict):
        return None
    if _get_clean_source_inconsistency(git):
        return None
    scheme = git.get("harness_sha256_scheme")
    if "harness_sha256_scheme" in git:
        from experiments.manifest import HARNESS_DIGEST_SCHEME
        if scheme != HARNESS_DIGEST_SCHEME:
            return None
    target, recorded = git.get("harness_sha256"), git.get("commit")
    if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{64}", target):
        return None
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{7,40}", recorded):
        return None
    proof_tree = None
    if "harness_commit_proof" in git:
        try:
            proof_tree = _get_proven_harness_tree(git["harness_commit_proof"], recorded, harness_path)
        except (ValueError, TypeError):
            return None
    try:
        if get_harness_sha256_at_commit(repo, recorded, harness_path, scheme) == target:
            return recorded
    except (OSError, ValueError, subprocess.SubprocessError):
        if proof_tree and git.get("dirty") is False and scheme is not None:
            try:
                return _get_squash_harness_source(repo, harness_path, proof_tree, target, scheme)
            except (OSError, ValueError, subprocess.SubprocessError):
                return None
        return None
    if git.get("dirty") is not True:
        return None
    try:
        descendants = subprocess.run(
            ["git", "rev-list", "--ancestry-path", f"{recorded}..HEAD", "--",
             harness_path], cwd=repo, capture_output=True, check=True,
            text=True, timeout=10).stdout.splitlines()
        for commit in reversed(descendants):
            if get_harness_sha256_at_commit(
                    repo, commit, harness_path, scheme) == target:
                return commit
        from experiments.manifest import _source_fingerprint
        if _source_fingerprint(os.path.join(repo, harness_path), scheme) == target:
            return "WORKTREE"
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def check_harness_provenance(
        provenance_block, repo=REPO, harness_path="app/experiments", *,
        require_current=False):
    """-> (status, detail) for a STORED provenance block.

    ONE check, built on get_reproducible_harness_source so "which commit holds
    these bytes" is never answered twice. Statuses:

    verified      the recorded harness was reproduced at the recorded commit,
                  a descendant eligible only for a run that recorded dirty=True,
                  the current worktree of such a run, or a squash-preserved
                  tree cryptographically bound to its clean recorded commit.
    inconsistent  the run recorded a CLEAN tree, and its harness does not match
                  the commit it names. A clean run has no other explanation.
    unverifiable  nothing could be reproduced and the source state does not
                  support that conclusion: dirty=True whose bytes were never
                  found, or dirty=None, which is UNKNOWN and is never read as
                  clean. A missing legacy diagnostic field is not a defect by
                  itself; a record that actually verifies passes.

    require_current additionally binds a derived-file check to the current
    harness, using the stored digest scheme. Historical experiment consumers
    can still identify an older harness without requiring it to be current.
    Neither mode is a verdict about the measurement.
    """
    git = (provenance_block.get("git")
           if isinstance(provenance_block, dict) else None)
    if not isinstance(git, dict):
        return "unverifiable", "no git provenance block was recorded"
    inconsistent = _get_clean_source_inconsistency(git)
    if inconsistent:
        return "inconsistent", inconsistent
    target = git.get("harness_sha256")
    if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{64}", target):
        return "unverifiable", "no harness fingerprint was recorded"
    recorded = git.get("commit")
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{7,40}", recorded):
        return "unverifiable", "no recorded commit"
    resolved = get_reproducible_harness_source(
        provenance_block, repo=repo, harness_path=harness_path)
    if resolved:
        if require_current:
            from experiments.manifest import _source_fingerprint
            try:
                current = _source_fingerprint(
                    os.path.join(repo, harness_path), git.get("harness_sha256_scheme"))
            except (OSError, ValueError) as exc:
                return "unverifiable", f"current harness could not be fingerprinted: {exc}"
            if current != target:
                return "inconsistent", "current harness differs from the recorded harness"
        return "verified", f"harness reproduced at {resolved}"
    if git.get("dirty") is False:
        return ("inconsistent",
                f"harness does not match the clean recorded commit {recorded[:12]}")
    return ("unverifiable",
            f"recorded harness was not recovered from {recorded[:12]} and the "
            "source state does not prove it was clean")


def provenance(script_file, args=None, **extra):
    """-> a dict describing how this artifact was produced.

    `script_file` should be the caller's `__file__`. `args` is the argparse
    Namespace, recorded whole: an experiment's arguments ARE its identity, and
    guessing which ones matter is how `--seed` came to be omitted from a file
    whose entire purpose was to be seeded.

    Never raises. A provenance block that can fail is a provenance block that
    gets wrapped in try/except and quietly dropped, which returns us to
    artifacts that cannot identify themselves.
    """
    block = {"script": os.path.basename(str(script_file)),
             "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "host": platform.node()}
    try:
        from experiments.manifest import _git_state
        block["git"] = _git_state(REPO)
        if block["git"].get("dirty") is False:
            block["git"]["harness_commit_proof"] = get_harness_commit_proof(
                REPO, block["git"]["commit"])
    except Exception as exc:                            # noqa: BLE001
        block["git"] = {"error": str(exc)[:120]}

    if args is not None:
        try:
            recorded = {k: v for k, v in vars(args).items()
                        if not k.startswith("_")}
            # Absolute paths differ per machine and say nothing about the run;
            # their basenames identify the inputs, which is the useful part.
            for k, v in list(recorded.items()):
                if isinstance(v, str) and os.path.isabs(v):
                    recorded[k] = os.path.relpath(v, REPO) \
                        if v.startswith(REPO) else os.path.basename(v)
            block["args"] = recorded
            # Promoted out of args as well as left inside it. This is the field
            # whose absence made two artifacts indistinguishable, and a reader
            # scanning for it should not have to know it lives under `args`.
            if "seed" in recorded:
                block["seed"] = recorded["seed"]
        except Exception as exc:                        # noqa: BLE001
            block["args"] = {"error": str(exc)[:120]}

    block.update(extra)
    return block
