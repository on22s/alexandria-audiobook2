"""Free Hugging Face storage by purging weight files, keeping a written lesson for each.

Deleting a file in a commit frees nothing: the Hub bills every LFS object ever pushed.
Only ``permanently_delete_lfs_files`` reclaims space, and it cannot be undone. This runs
one purge batch with the safeguards learned while freeing ~26 GB on 2026-09-27.

Spec (JSON)::

    {"repo": "Om22s/alexandria-adapters-archive", "label": "batch B (...)",
     "lessons": {"<folder>/LESSONS.md": "<local .md>", ...},
     "purge": ["<folder>/adapter_model.safetensors", ...],
     "expect_bytes": 6690000000}

Every check that only reads runs before the first write, so a refusal changes nothing:

1. the repo is private, and every purge path exists at HEAD;
2. no OTHER path at HEAD of this repo holds one of the purge objects (same sha256) --
   ``list_lfs_files`` names each object by the path it was FIRST pushed under, so a
   folder move or a copy on another box makes a "history-only" object live again;
3. no weight file at HEAD of any other repo by the same author holds one either -- three
   archive folders were byte-identical to published adapters;
4. the lessons are committed and each is read back byte-identical;
5. the purge paths are deleted in one commit, and check 2 is repeated on the new HEAD;
6. exactly those objects are purged, and the freed size must match ``expect_bytes``.

Usage: ``python tools/hf_purge_batch.py <spec.json>``
"""
import json
import sys

FREED_TOLERANCE_BYTES = 50_000_000


class PurgeRefused(Exception):
    pass


def get_head_sha_by_path(api, repo, repo_type='model'):
    """{path: sha256} for every LFS file at HEAD of ``repo``."""
    paths = api.list_repo_files(repo, repo_type=repo_type)
    out = {}
    for i in range(0, len(paths), 200):
        for info in api.get_paths_info(repo, paths[i:i + 200], repo_type=repo_type):
            if getattr(info, 'lfs', None):
                out[info.path] = info.lfs.sha256
    return out


def get_head_sha_elsewhere(api, author, exclude_repo):
    """{sha256: ["repo:path", ...]} for weight files at HEAD of the author's other repos."""
    repos = [('model', m.id) for m in api.list_models(author=author)]
    repos += [('dataset', d.id) for d in api.list_datasets(author=author)]
    repos += [('space', s.id) for s in api.list_spaces(author=author)]
    out = {}
    for repo_type, repo in repos:
        if repo == exclude_repo:
            continue
        for path, sha in get_head_sha_by_path(api, repo, repo_type).items():
            out.setdefault(sha, []).append(f"{repo}:{path}")
    return out


def get_shared_paths(head, target, purge):
    """Paths outside ``purge`` whose object is one of ``target``."""
    return {p: s for p, s in head.items() if s in target and p not in purge}


def run_batch(spec, api, log=print):
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete

    repo, purge = spec['repo'], set(spec['purge'])
    expected = spec.get('expect_bytes')
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        raise PurgeRefused('expect_bytes must be a positive integer')
    lessons = spec.get('lessons') or {}
    overlap = sorted(set(lessons) & purge)
    if overlap:
        raise PurgeRefused(f'lesson paths overlap purge paths: {overlap}')
    if not api.model_info(repo).private:
        raise PurgeRefused(f'{repo} is not private')
    missing = sorted(purge - set(api.list_repo_files(repo)))
    if missing:
        raise PurgeRefused(f'purge paths not at HEAD: {missing}')

    head = get_head_sha_by_path(api, repo)
    target = {head[p] for p in purge if p in head}
    if len(target) == 0 or any(p not in head for p in purge):
        raise PurgeRefused('a purge path is not an LFS file')
    shared = get_shared_paths(head, target, purge)
    if shared:
        raise PurgeRefused(f'another path in {repo} uses a purge object: {shared}')
    elsewhere = {s: v for s, v in get_head_sha_elsewhere(api, repo.split('/')[0], repo).items() if s in target}
    if elsewhere:
        raise PurgeRefused(f'purge object is at HEAD of another repo: {elsewhere}')

    stored = {f.file_oid: f.size for f in api.list_lfs_files(repo)}
    if not target <= stored.keys():
        raise PurgeRefused('target LFS object is missing from storage listing')
    target_bytes = sum(stored[sha] for sha in target)
    if abs(target_bytes - expected) >= FREED_TOLERANCE_BYTES:
        raise PurgeRefused(f'expected {expected} bytes, target objects total {target_bytes}')
    if lessons:
        api.create_commit(repo, operations=[CommitOperationAdd(path_in_repo=k, path_or_fileobj=v)
                                            for k, v in lessons.items()],
                          commit_message=f"LESSONS before purge: {spec['label']}")
        for k, v in lessons.items():
            with open(api.hf_hub_download(repo, k, force_download=True), 'rb') as got, open(v, 'rb') as want:
                if got.read() != want.read():
                    raise PurgeRefused(f'lesson read back differs: {k}')
        log(f'lessons read back identical: {len(lessons)}')

    before = sum(f.size for f in api.list_lfs_files(repo))
    api.create_commit(repo, operations=[CommitOperationDelete(path_in_repo=p) for p in sorted(purge)],
                      commit_message=f"Remove {len(purge)} weight files; lessons kept: {spec['label']}")
    shared = get_shared_paths(get_head_sha_by_path(api, repo), target, purge)
    if shared:
        raise PurgeRefused(f'after delete, another path uses a purge object: {shared}')

    objs = [f for f in api.list_lfs_files(repo) if f.file_oid in target]
    if {f.file_oid for f in objs} != target:
        raise PurgeRefused('LFS objects do not match the purge sha256s')
    api.permanently_delete_lfs_files(repo, objs)
    after = sum(f.size for f in api.list_lfs_files(repo))
    freed = before - after
    log(f"purged {len(objs)} objects; stored {before / 1e9:.2f} -> {after / 1e9:.2f} GB "
        f"(freed {freed / 1e9:.2f}, expected {spec['expect_bytes'] / 1e9:.2f})")
    if any(f.file_oid in target for f in api.list_lfs_files(repo)):
        raise PurgeRefused('purged objects are still listed')
    if abs(freed - spec['expect_bytes']) >= FREED_TOLERANCE_BYTES:
        raise PurgeRefused(f"freed {freed} bytes, expected {spec['expect_bytes']}")
    return freed


if __name__ == '__main__':
    from huggingface_hub import HfApi
    with open(sys.argv[1]) as fh:
        run_batch(json.load(fh), HfApi())
