"""Publish new training datasets only after private preparation succeeds."""
import os
import shutil
import tempfile
from contextlib import contextmanager

from utils import file_lock, secure_filename


@contextmanager
def apply_dataset_publication(root, name):
    """Serialize cooperating publishers; failures clean only their private stage."""
    if not name or secure_filename(name) != name:
        raise ValueError("Invalid dataset name")
    root = os.path.realpath(root)
    os.makedirs(root, exist_ok=True)
    destination = os.path.join(root, name)
    lock_dir = os.path.join(root, ".dataset-locks")
    os.makedirs(lock_dir, exist_ok=True)
    with file_lock(os.path.join(lock_dir, name)):
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        staging = tempfile.mkdtemp(prefix=".dataset-stage-", dir=root)
        try:
            yield staging
            # Also refuse a destination created outside the cooperating protocol.
            if os.path.lexists(destination):
                raise FileExistsError(destination)
            os.rename(staging, destination)
        finally:
            if os.path.isdir(staging):
                shutil.rmtree(staging)
