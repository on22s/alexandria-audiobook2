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
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        raise RuntimeError(missing_msg)

    if cache.get("mtime") == mtime and cache.get("prompts") is not None:
        return cache["prompts"]

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        raise RuntimeError(f"Error reading {path}: {e}")

    parts = raw.split("---SEPARATOR---", maxsplit=num_parts - 1)
    if len(parts) != num_parts:
        raise RuntimeError(malformed_msg)

    prompts = tuple(p.strip() for p in parts)
    cache["mtime"] = mtime
    cache["prompts"] = prompts
    return prompts
