"""Validate the paths used by both source-repair command line tools."""

import os
import json
from pathlib import Path
import tempfile
import shutil


def _same_target(first, second):
    if os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second)):
        return True
    return os.path.exists(first) and os.path.exists(second) and os.path.samefile(first, second)


def validate_repair_paths(source, output, report):
    """Reject paths that could overwrite the source or each other."""
    paths = (("source", source), ("output", output), ("report", report))
    for index, (first_name, first_path) in enumerate(paths):
        for second_name, second_path in paths[index + 1:]:
            if _same_target(first_path, second_path):
                raise ValueError(f"{first_name} and {second_name} must be different files")


def save_repair_text(path, text):
    """Publish UTF-8 text only after its staged file is complete and synced."""
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=destination.parent,
                                         prefix=f'.{destination.name}.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_source_repair_result(report_path, report, output_path=None, repaired_text=None):
    """Leave unapplied evidence until the accepted repaired output is published."""
    published = {**report, "applied": False}
    backup = None
    destination = Path(output_path) if output_path is not None else None
    try:
        save_repair_text(report_path, json.dumps(published, indent=2, ensure_ascii=False))
        if destination is not None and destination.exists():
            with tempfile.NamedTemporaryFile('wb', dir=destination.parent,
                                             prefix=f'.{destination.name}.backup.', suffix='.tmp',
                                             delete=False) as stream:
                backup = Path(stream.name)
                with destination.open('rb') as prior:
                    shutil.copyfileobj(prior, stream)
                stream.flush()
                os.fsync(stream.fileno())
        if destination is not None:
            save_repair_text(destination, repaired_text)
            try:
                applied = {**published, "applied": True}
                save_repair_text(report_path, json.dumps(applied, indent=2, ensure_ascii=False))
            except BaseException:
                if backup is not None:
                    try:
                        backup.replace(destination)
                    except OSError as rollback_error:
                        recovery_path = backup
                        backup = None  # retain the prior output for explicit recovery
                        raise OSError(f"Repair rollback failed; prior output preserved at {recovery_path}") from rollback_error
                else:
                    destination.unlink()
                raise
            published = applied
        return published
    finally:
        if backup is not None:
            backup.unlink(missing_ok=True)
