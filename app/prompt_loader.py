import os


def load_prompts_file(path, num_parts, missing_msg, malformed_msg, cache):
    """Read a ---SEPARATOR----delimited prompts file and split it into
    `num_parts` stripped strings.

    Shared by default_prompts.py/persona_prompts.py/review_prompts.py, which
    differ only in path/part-count/messages. Uses an mtime-based cache (the
    caller's own dict, so each prompt file gets its own) to pick up edits
    without restarting the app while avoiding a redundant disk read+split on
    every call within one process - app.py's get_config()/get_default_prompts()
    call all three loaders together, repeatedly, on the same request paths.
    """
    try:
        stat = os.stat(path)
        mtime = stat.st_mtime
        file_version = (stat.st_dev, stat.st_ino, stat.st_size,
                        stat.st_mtime_ns, stat.st_ctime_ns)
    except FileNotFoundError:
        raise RuntimeError(missing_msg)
    except OSError as exc:
        raise RuntimeError(f"Error reading {path}: {exc}") from exc

    if cache.get("malformed_version") == file_version:
        raise RuntimeError(malformed_msg)
    if cache.get("mtime") == mtime and cache.get("prompts") is not None:
        return cache["prompts"]

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        raise RuntimeError(f"Error reading {path}: {e}")

    # No maxsplit: a file with one extra stray delimiter (e.g. a copy-paste
    # slip while editing) must still produce the wrong part count and raise
    # malformed_msg, rather than silently absorbing the extra delimiter (and
    # everything after it) into the last part's text.
    parts = raw.split("---SEPARATOR---")
    if len(parts) != num_parts:
        cache["malformed_version"] = file_version
        raise RuntimeError(malformed_msg)

    prompts = tuple(p.strip() for p in parts)
    if not all(prompts):
        cache["malformed_version"] = file_version
        raise RuntimeError(malformed_msg)
    cache.pop("malformed_version", None)
    cache["mtime"] = mtime
    cache["prompts"] = prompts
    return prompts
