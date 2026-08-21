class _Upload:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


def app_python_files(app_dir):
    """-> our own .py files under app/, never the virtualenv's.

    `app/env` is a virtualenv inside the directory being walked, so a bare
    rglob returns several thousand site-packages modules. CI and worktrees have
    no venv, so a sweep written that way passes there and fails only in the
    live checkout - which is exactly where the suite most needs to run, because
    that is the tree the GPU queue runs from.

    Two other sweeps already excluded it by hand, each with its own spelling.
    This is the one place that answers "which .py files are ours".
    """
    import pathlib
    app_dir = pathlib.Path(app_dir)
    roots = {p.parent for p in app_dir.rglob("pyvenv.cfg")}
    out = []
    for path in sorted(app_dir.rglob("*.py")):
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            continue
        if any(root in path.parents for root in roots):
            continue
        out.append(path)
    return out
