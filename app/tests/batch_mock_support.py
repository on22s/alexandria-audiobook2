"""Adapt old fake-Popen fixtures to the owned-subprocess boundary."""
from unittest.mock import patch
import subprocess
import alexandria_batch_processor as batch
_native_stop = batch.stop_owned_subprocess

def stop_fixture(child, *, timeout=0, interrupt=False, force_after_grace=False):
    if isinstance(child, subprocess.Popen):
        return _native_stop(child, timeout=timeout, interrupt=interrupt,
                            force_after_grace=force_after_grace)
    if getattr(child, '_fixture_stopped', False):
        return
    if child.poll() is None:
        if interrupt:
            child.terminate()
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        else:
            child.kill()
            child.wait()
    child._fixture_stopped = True

def adapt_owner_fixture(test):
    owner = patch.object(batch, 'stop_owned_subprocess', side_effect=stop_fixture)
    owner.start()
    test.addCleanup(owner.stop)
