"""Bounded owned media checks without model or export-policy imports."""
from collections import deque
import os
import subprocess
import threading
import time

from subprocess_ownership import start_owned_subprocess, stop_owned_subprocess


def run_owned_media_check(command, check_active, timeout, *, output_limit=0,
                         consume_line=None, progress_callback=None, consume_output=None,
                         cancellation_errors=(), input_source=None):
    """Drain finite chunks on both pipes while the caller controls lifetime."""
    output, errors = bytearray(), []
    diagnostics = deque(maxlen=12)
    process = None
    readers = []
    started = time.monotonic()

    def raise_reader_error():
        if errors:
            if isinstance(errors[0], cancellation_errors):
                raise errors[0]
            raise ValueError(errors[0])

    def read_output():
        size = 0
        try:
            while block := process.stdout.read(65536):
                size += len(block)
                if size > output_limit:
                    if not errors:
                        errors.append('Media probe exceeded its bounded output allowance.')
                elif not errors:
                    if consume_output is None:
                        output.extend(block)
                    else:
                        consume_output(block)
        except BaseException as error:
            if not errors:
                errors.append(error if isinstance(error, cancellation_errors) else str(error)[:240])

    def read_errors():
        try:
            while raw := process.stderr.readline(4097):
                line = raw.decode('utf-8', errors='replace').rstrip()
                diagnostics.append(line[-512:])
                if consume_line is not None:
                    consume_line(line)
        except BaseException as error:
            if not errors:
                errors.append(error if isinstance(error, cancellation_errors) else str(error)[:240])

    try:
        check_active()
        # FFREPORT would bypass bounded pipe diagnostics with an unbounded file.
        environment = dict(os.environ)
        environment.pop('FFREPORT', None)
        process = start_owned_subprocess(command, stdout=subprocess.PIPE if output_limit else subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, stdin=input_source, start_new_session=True,
                                         env=environment)
        for function in ([read_output, read_errors] if output_limit else [read_errors]):
            thread = threading.Thread(target=function, daemon=True)
            readers.append(thread)
            thread.start()
        next_progress = started + 2
        while True:
            check_active()
            raise_reader_error()
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            if progress_callback and time.monotonic() >= next_progress:
                progress_callback(f'Checking finished export: {elapsed:.1f} seconds elapsed')
                next_progress = time.monotonic() + 2
            try:
                result = process.wait(timeout=min(.1, timeout - elapsed))
                break
            except subprocess.TimeoutExpired:
                continue
        for thread in readers:
            thread.join(timeout=2)
        raise_reader_error()
        if any(thread.is_alive() for thread in readers):
            raise ValueError('Media check retained an incomplete diagnostic stream.')
        check_active()
        if result:
            raise ValueError('Finished export could not be decoded: ' + '\n'.join(diagnostics)[-2048:])
        return bytes(output)
    finally:
        if process is not None:
            try:
                if process.poll() is None or any(thread.is_alive() for thread in readers):
                    stop_owned_subprocess(process, timeout=1, force_after_grace=True)
                for thread in readers:
                    thread.join(timeout=2)
                if consume_output is not None:
                    # A sink may still own a caller's file. Do not release its
                    # worker/lease while that file operation is unwinding.
                    for thread in readers:
                        thread.join()
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None and not any(thread.is_alive() for thread in readers):
                        pipe.close()
            finally:
                control = getattr(process, '_alexandria_control', None)
                if control is not None:
                    control.close()
