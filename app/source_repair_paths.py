"""Validate the paths used by both source-repair command line tools."""

import os


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
